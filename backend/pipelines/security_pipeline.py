#!/usr/bin/env python3
"""
security_pipeline.py

Security Agent — scans codebases for vulnerabilities using semgrep and
generates a security report with LLM-powered explanations.
Uses LangGraph for workflow orchestration and Ollama as the LLM provider.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Any, Dict, List, Optional, TypedDict

from pipelines.pipeline_utils import (
    collect_calls,
    invoke_llm,
    json_for_prompt,
    load_artifacts,
    merge_sections,
    resolve_artifact_root,
    write_json,
    REQUIRED_ARTIFACTS,
)

logger = logging.getLogger(__name__)

SEMGREP_RULESETS = [
    "p/owasp-top-ten",
    "p/security-audit",
    "p/secrets",
]

SEMGREP_TIMEOUT_SECONDS = 120

SECTION_ORDER = [
    "vulnerability_summary",
    "critical_findings",
    "code_quality_risks",
    "dependency_risks",
    "security_posture",
]

SECTION_TITLES = {
    "vulnerability_summary": "Vulnerability Summary",
    "critical_findings": "Critical Findings",
    "code_quality_risks": "Code Quality Risks",
    "dependency_risks": "Dependency Risks",
    "security_posture": "Security Posture",
}

SECTION_INSTRUCTIONS = {
    "critical_findings": (
        "For each high-severity finding from the semgrep scan, explain:\n"
        "1. What the vulnerability is — name the vulnerability class (e.g., SQL injection, command injection, XSS).\n"
        "2. Why it is dangerous — describe a concrete attack scenario against this specific code.\n"
        "3. How to fix it — provide specific remediation guidance referencing the actual code.\n"
        "If there are no high-severity findings, state: 'No high-severity issues were detected.'\n"
        "Use the file paths, line numbers, and code snippets from the findings as evidence."
    ),
    "code_quality_risks": (
        "Group the medium and low severity findings by category (e.g., all XSS findings together, "
        "all insecure configuration together). For each group, explain:\n"
        "1. What the pattern indicates.\n"
        "2. The cumulative risk if left unaddressed.\n"
        "3. Recommended improvements.\n"
        "If there are no medium/low findings, state: 'No medium or low severity issues were detected.'\n"
        "Focus on patterns rather than listing every individual finding."
    ),
    "dependency_risks": (
        "Analyze the project's external dependencies and risky function usage.\n"
        "For external dependencies: note which ones are imported and any known security concerns.\n"
        "For risky function usage (eval, exec, subprocess, pickle, yaml.load, os.system, shell=True): "
        "explain why each is risky and whether the usage appears safe or dangerous in context.\n"
        "If secrets or credentials were detected by semgrep, highlight them prominently.\n"
        "Synthesize into an overall dependency risk narrative."
    ),
    "security_posture": (
        "Produce an architectural security assessment based on the code structure:\n"
        "1. Attack surface — what modules are exposed, what accepts external input.\n"
        "2. Trust boundaries — where does validated data flow into unvalidated code.\n"
        "3. Input validation — are there patterns of validation, or is it absent.\n"
        "4. Authentication/authorization — any patterns detected, or notably absent.\n"
        "5. Areas of concern — high-coupling modules, deeply nested call paths, missing layers.\n"
        "Base this ONLY on the structural data provided (entrypoints, dependency graph, call graph, "
        "architectural layers). Do NOT reference semgrep findings — this section is about architecture."
    ),
}

SYSTEM_PROMPT = (
    "You are an expert application security engineer reviewing a codebase. "
    "Your output MUST be grounded exclusively in the provided context.\n"
    "RULES:\n"
    "1. Never invent vulnerabilities, files, functions, or behaviors not present in the context.\n"
    "2. The context contains a 'key_facts' section — treat every item as ground truth.\n"
    "3. When something cannot be determined from the analysis, say so explicitly.\n"
    "4. Cite file paths, line numbers, function names, and rule IDs as evidence.\n"
    "5. Provide actionable remediation — not just 'fix this', but how."
)



class SecurityAgentState(TypedDict, total=False):
    artifact_root: str
    source_dir: str
    output_dir: str
    model: str
    base_url: str
    skip_llm: bool
    artifacts: Dict[str, Any]
    semgrep_results: Dict[str, Any]
    analysis: Dict[str, Any]
    section_context: Dict[str, Dict[str, Any]]
    sections: Annotated[Dict[str, str], merge_sections]
    report: Dict[str, Any]


# ── Semgrep integration ────────────────────────────────────────────────────

def _normalize_severity(semgrep_severity: str) -> str:
    mapping = {"ERROR": "high", "WARNING": "medium", "INFO": "low"}
    return mapping.get(semgrep_severity.upper(), "low")


def _run_semgrep(source_dir: str) -> Dict[str, Any]:
    """Run semgrep against source_dir and return parsed results."""
    cmd = [
        "semgrep",
        "--json",
        "--quiet",
        "--no-git-ignore",
    ]
    for ruleset in SEMGREP_RULESETS:
        cmd.extend(["--config", ruleset])
    cmd.append(source_dir)

    logger.info("Running semgrep: %s", " ".join(cmd))
    t0 = time.time()

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=SEMGREP_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        logger.error("semgrep not found — is it installed? (pip install semgrep)")
        return _empty_semgrep_results("semgrep executable not found. Install with: pip install semgrep")
    except subprocess.TimeoutExpired:
        logger.error("semgrep timed out after %ds", SEMGREP_TIMEOUT_SECONDS)
        return _empty_semgrep_results(f"semgrep scan timed out after {SEMGREP_TIMEOUT_SECONDS}s")

    elapsed = time.time() - t0
    logger.info("semgrep completed in %.2fs (exit code %d)", elapsed, result.returncode)

    # semgrep returns exit code 1 when findings are present, 0 when clean
    # Only treat exit codes > 1 or missing stdout as errors
    if not result.stdout.strip():
        stderr_snippet = (result.stderr or "")[:500]
        logger.error("semgrep produced no output. stderr: %s", stderr_snippet)
        return _empty_semgrep_results(f"semgrep produced no output: {stderr_snippet}")

    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        logger.error("Failed to parse semgrep JSON output: %s", exc)
        return _empty_semgrep_results(f"Failed to parse semgrep output: {exc}")

    return _parse_semgrep_output(raw, elapsed)


def _empty_semgrep_results(error_message: str) -> Dict[str, Any]:
    return {
        "findings": [],
        "summary": {"high": 0, "medium": 0, "low": 0, "total": 0},
        "by_severity": {"high": [], "medium": [], "low": []},
        "by_category": {},
        "scan_time_seconds": 0,
        "rulesets_used": SEMGREP_RULESETS,
        "error": error_message,
    }


def _parse_semgrep_output(raw: Dict[str, Any], scan_time: float) -> Dict[str, Any]:
    results = raw.get("results", [])
    findings: List[Dict[str, Any]] = []

    for r in results:
        extra = r.get("extra", {})
        metadata = extra.get("metadata", {})

        owasp = metadata.get("owasp", [])
        if isinstance(owasp, list):
            owasp_str = ", ".join(str(o) for o in owasp) if owasp else "Uncategorized"
        else:
            owasp_str = str(owasp)

        severity = _normalize_severity(extra.get("severity", "INFO"))

        findings.append({
            "severity": severity,
            "rule_id": r.get("check_id", "unknown"),
            "message": extra.get("message", "No description"),
            "file": r.get("path", "unknown"),
            "line_start": r.get("start", {}).get("line", 0),
            "line_end": r.get("end", {}).get("line", 0),
            "code_snippet": extra.get("lines", ""),
            "owasp_category": owasp_str,
        })

    by_severity: Dict[str, List[Dict]] = {"high": [], "medium": [], "low": []}
    for f in findings:
        by_severity.get(f["severity"], by_severity["low"]).append(f)

    by_category: Dict[str, List[Dict]] = {}
    for f in findings:
        cat = f["owasp_category"]
        by_category.setdefault(cat, []).append(f)

    summary = {
        "high": len(by_severity["high"]),
        "medium": len(by_severity["medium"]),
        "low": len(by_severity["low"]),
        "total": len(findings),
    }

    return {
        "findings": findings,
        "summary": summary,
        "by_severity": by_severity,
        "by_category": by_category,
        "scan_time_seconds": round(scan_time, 2),
        "rulesets_used": SEMGREP_RULESETS,
        "error": None,
    }


# ── Deterministic analysis ─────────────────────────────────────────────────

RISKY_FUNCTIONS = {
    "eval", "exec", "compile",
    "os.system", "os.popen",
    "subprocess.call", "subprocess.run", "subprocess.Popen",
    "pickle.loads", "pickle.load",
    "yaml.load", "yaml.unsafe_load",
    "input",  # Python 2 risk
    "__import__",
}

RISKY_PATTERNS = {
    "shell=True",
}


def _find_risky_calls(modules: Dict[str, Any]) -> List[Dict[str, Any]]:
    risky: List[Dict[str, Any]] = []
    for mod_name, mod in modules.items():
        for call in collect_calls(mod):
            callee = call.get("callee_name") or ""
            qualified = call.get("callee_qualified_name") or ""
            matched = None
            for rf in RISKY_FUNCTIONS:
                if callee == rf or qualified.endswith(rf) or callee.endswith(rf):
                    matched = rf
                    break
            if matched:
                risky.append({
                    "function": matched,
                    "callee_name": callee,
                    "module": mod_name,
                    "file": mod.get("file_path", ""),
                    "line": call.get("line_number", 0),
                })
    return risky


def _find_external_imports(modules: Dict[str, Any]) -> List[Dict[str, Any]]:
    project_modules = set(modules.keys())
    project_prefixes = set()
    for m in project_modules:
        parts = m.split(".")
        for i in range(1, len(parts) + 1):
            project_prefixes.add(".".join(parts[:i]))

    import_counter: Counter = Counter()
    for mod_name, mod in modules.items():
        for imp in mod.get("imports", []):
            imp_module = imp.get("module") or imp.get("name") or ""
            if not imp_module:
                continue
            top_level = imp_module.split(".")[0]
            if imp_module not in project_prefixes and top_level not in project_prefixes:
                import_counter[imp_module] += 1

    return sorted(
        [{"name": name, "count": count} for name, count in import_counter.items()],
        key=lambda x: -x["count"],
    )


def _find_entrypoints(
    modules: Dict[str, Any], call_graph: Dict[str, List[str]],
) -> List[str]:
    module_names = sorted(modules.keys())
    entrypoints = [n for n in module_names if n == "main" or n.endswith(".main")]
    entrypoints.extend(
        c for c in sorted(call_graph.keys())
        if c.endswith(".main") and c not in entrypoints
    )
    return entrypoints


def _infer_architectural_layers(
    dep_graph: Dict[str, List[str]], modules: Dict[str, Any], entrypoints: List[str],
) -> Dict[str, str]:
    layers: Dict[str, str] = {}
    entry_bases = {ep.split(".")[0] for ep in entrypoints}

    in_degree: Counter = Counter()
    for deps in dep_graph.values():
        for d in deps:
            in_degree[d] += 1

    for mod in modules:
        parts = mod.lower().split(".")
        leaf = parts[-1]
        if mod in entry_bases or leaf == "main":
            layers[mod] = "entrypoint"
        elif any(p in parts for p in ("test", "tests")):
            layers[mod] = "test"
        elif any(p in parts for p in ("util", "utils", "helper", "helpers", "common")):
            layers[mod] = "utility"
        elif any(p in parts for p in ("model", "models", "entity", "entities", "schema")):
            layers[mod] = "model"
        elif any(p in parts for p in ("db", "database", "repo", "repository", "store")):
            layers[mod] = "data_access"
        elif any(p in parts for p in ("service", "services", "manager", "handler")):
            layers[mod] = "service"
        elif any(p in parts for p in ("route", "routes", "api", "endpoint", "view", "controller")):
            layers[mod] = "presentation"
        elif any(p in parts for p in ("config", "settings", "constants")):
            layers[mod] = "config"
        elif in_degree.get(mod, 0) >= 2:
            layers[mod] = "shared"
        else:
            layers[mod] = "other"
    return layers


def _build_key_facts(
    artifacts: Dict[str, Any],
    semgrep_results: Dict[str, Any],
    entrypoints: List[str],
) -> List[str]:
    meta = artifacts["project_metadata"]
    summary = semgrep_results["summary"]
    facts = []

    facts.append(
        f"Total source files: {meta.get('total_source_files', '?')}, "
        f"classes: {meta.get('total_classes', '?')}, "
        f"functions: {meta.get('total_functions', '?')}, "
        f"tracked calls: {meta.get('total_calls', '?')}."
    )
    facts.append(f"Primary language: {meta.get('primary_language', 'unknown')}.")
    facts.append(
        f"Likely entrypoints: {', '.join(entrypoints) if entrypoints else 'none identified'}."
    )
    facts.append(
        f"Semgrep scan: {summary['total']} findings "
        f"({summary['high']} high, {summary['medium']} medium, {summary['low']} low)."
    )
    facts.append(f"Rulesets used: {', '.join(semgrep_results['rulesets_used'])}.")
    if semgrep_results.get("error"):
        facts.append(f"Semgrep error: {semgrep_results['error']}")
    facts.append(
        f"Semgrep ran {'successfully' if not semgrep_results.get('error') else 'with errors'}."
    )
    return facts


def _build_analysis(
    artifacts: Dict[str, Any], semgrep_results: Dict[str, Any],
) -> Dict[str, Any]:
    modules = artifacts["modules"]
    call_graph = artifacts["call_graph"]
    dep_graph = artifacts["dependency_graph"]

    entrypoints = _find_entrypoints(modules, call_graph)
    risky_calls = _find_risky_calls(modules)
    external_imports = _find_external_imports(modules)
    layers = _infer_architectural_layers(dep_graph, modules, entrypoints)
    key_facts = _build_key_facts(artifacts, semgrep_results, entrypoints)

    # Compute dependency coupling
    out_degree: Counter = Counter()
    in_degree: Counter = Counter()
    for mod, deps in dep_graph.items():
        unique = set(deps)
        out_degree[mod] = len(unique)
        for d in unique:
            in_degree[d] += 1

    layer_groups: Dict[str, List[str]] = {}
    for mod, layer in sorted(layers.items()):
        layer_groups.setdefault(layer, []).append(mod)

    # Secrets findings from semgrep
    secrets_findings = [
        f for f in semgrep_results.get("findings", [])
        if "secret" in f.get("rule_id", "").lower()
        or "credential" in f.get("rule_id", "").lower()
        or "password" in f.get("rule_id", "").lower()
        or "token" in f.get("rule_id", "").lower()
        or "api-key" in f.get("rule_id", "").lower()
    ]

    return {
        "entrypoints": entrypoints,
        "risky_calls": risky_calls,
        "external_imports": external_imports,
        "layers": layers,
        "layer_groups": layer_groups,
        "key_facts": key_facts,
        "secrets_findings": secrets_findings,
        "top_fan_out": sorted(out_degree.items(), key=lambda kv: -kv[1])[:10],
        "top_fan_in": sorted(in_degree.items(), key=lambda kv: -kv[1])[:10],
        "module_count": len(modules),
    }


# ── Section context ────────────────────────────────────────────────────────

def _build_section_context(
    artifacts: Dict[str, Any],
    semgrep_results: Dict[str, Any],
    analysis: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    key_facts = analysis["key_facts"]
    dep_graph = artifacts["dependency_graph"]
    call_graph = artifacts["call_graph"]

    return {
        "vulnerability_summary": {
            "key_facts": key_facts,
            "summary": semgrep_results["summary"],
            "top_findings": semgrep_results["by_severity"].get("high", [])[:5]
                + semgrep_results["by_severity"].get("medium", [])[:3],
            "rulesets_used": semgrep_results["rulesets_used"],
            "scan_time_seconds": semgrep_results.get("scan_time_seconds", 0),
            "error": semgrep_results.get("error"),
        },
        "critical_findings": {
            "key_facts": key_facts,
            "findings": semgrep_results["by_severity"].get("high", []),
        },
        "code_quality_risks": {
            "key_facts": key_facts,
            "findings_medium": semgrep_results["by_severity"].get("medium", []),
            "findings_low": semgrep_results["by_severity"].get("low", []),
            "by_category": {
                cat: findings
                for cat, findings in semgrep_results.get("by_category", {}).items()
                if any(f["severity"] in ("medium", "low") for f in findings)
            },
        },
        "dependency_risks": {
            "key_facts": key_facts,
            "external_imports": analysis["external_imports"],
            "risky_calls": analysis["risky_calls"],
            "secrets_findings": analysis["secrets_findings"],
        },
        "security_posture": {
            "key_facts": key_facts,
            "entrypoints": analysis["entrypoints"],
            "layers": analysis["layers"],
            "layer_groups": analysis["layer_groups"],
            "dependency_graph": dep_graph,
            "call_graph": call_graph,
            "top_fan_out": analysis["top_fan_out"],
            "top_fan_in": analysis["top_fan_in"],
        },
    }


# ── Deterministic fallback sections ────────────────────────────────────────

def _render_section_fallback(section_key: str, context: Dict[str, Any]) -> str:
    title = SECTION_TITLES[section_key]

    if section_key == "vulnerability_summary":
        summary = context.get("summary", {})
        rulesets = context.get("rulesets_used", [])
        scan_time = context.get("scan_time_seconds", 0)
        error = context.get("error")
        top_findings = context.get("top_findings", [])

        lines = [f"## {title}", ""]

        if error:
            lines.append(f"**Semgrep scan error:** {error}")
            lines.append("")
            lines.append(
                "The vulnerability scan could not be completed. "
                "The dependency and security posture sections below are still available."
            )
            lines.append("")
            return "\n".join(lines)

        total = summary.get("total", 0)
        lines.append(f"**Scan completed in {scan_time}s** using rulesets: {', '.join(f'`{r}`' for r in rulesets)}")
        lines.append("")
        lines.append("| Severity | Count |")
        lines.append("|----------|-------|")
        lines.append(f"| High | {summary.get('high', 0)} |")
        lines.append(f"| Medium | {summary.get('medium', 0)} |")
        lines.append(f"| Low | {summary.get('low', 0)} |")
        lines.append(f"| **Total** | **{total}** |")
        lines.append("")

        if total == 0:
            lines.append(
                "No issues detected across all rulesets. "
                "This does not guarantee the absence of vulnerabilities — "
                "semgrep performs pattern-based matching and may not catch all issues."
            )
            lines.append("")
        elif top_findings:
            lines.append("### Top Findings")
            lines.append("")
            for f in top_findings[:8]:
                lines.append(
                    f"- **[{f['severity'].upper()}]** `{f['file']}:{f['line_start']}` — "
                    f"{f['rule_id']}: {f['message'][:120]}"
                )
            lines.append("")

        return "\n".join(lines)

    if section_key == "critical_findings":
        findings = context.get("findings", [])
        lines = [f"## {title}", ""]

        if not findings:
            lines.append("No high-severity issues were detected.")
            lines.append("")
            return "\n".join(lines)

        for f in findings:
            lines.append(f"### {f['rule_id']}")
            lines.append("")
            lines.append(f"- **Severity:** {f['severity'].upper()}")
            lines.append(f"- **File:** `{f['file']}:{f['line_start']}`")
            lines.append(f"- **OWASP:** {f['owasp_category']}")
            lines.append(f"- **Description:** {f['message']}")
            if f.get("code_snippet"):
                lines.append("")
                lines.append("```")
                lines.append(f["code_snippet"].rstrip())
                lines.append("```")
            lines.append("")

        return "\n".join(lines)

    if section_key == "code_quality_risks":
        findings_medium = context.get("findings_medium", [])
        findings_low = context.get("findings_low", [])
        lines = [f"## {title}", ""]

        if not findings_medium and not findings_low:
            lines.append("No medium or low severity issues were detected.")
            lines.append("")
            return "\n".join(lines)

        by_category = context.get("by_category", {})
        for cat, cat_findings in sorted(by_category.items()):
            lines.append(f"### {cat}")
            lines.append("")
            for f in cat_findings[:5]:
                lines.append(
                    f"- **[{f['severity'].upper()}]** `{f['file']}:{f['line_start']}` — "
                    f"{f['rule_id']}: {f['message'][:120]}"
                )
            if len(cat_findings) > 5:
                lines.append(f"- ... and {len(cat_findings) - 5} more in this category")
            lines.append("")

        return "\n".join(lines)

    if section_key == "dependency_risks":
        external_imports = context.get("external_imports", [])
        risky_calls = context.get("risky_calls", [])
        secrets = context.get("secrets_findings", [])
        lines = [f"## {title}", ""]

        if secrets:
            lines.append(f"### Secrets and Credentials ({len(secrets)} found)")
            lines.append("")
            for s in secrets:
                lines.append(
                    f"- **[{s['severity'].upper()}]** `{s['file']}:{s['line_start']}` — "
                    f"{s['rule_id']}: {s['message'][:120]}"
                )
            lines.append("")

        if risky_calls:
            lines.append(f"### Risky Function Usage ({len(risky_calls)} found)")
            lines.append("")
            for r in risky_calls:
                lines.append(f"- `{r['function']}` called in `{r['module']}` (`{r['file']}:{r['line']}`)")
            lines.append("")

        if external_imports:
            lines.append(f"### External Dependencies ({len(external_imports)} detected)")
            lines.append("")
            for imp in external_imports[:15]:
                lines.append(f"- `{imp['name']}` — imported {imp['count']} time(s)")
            lines.append("")

        if not secrets and not risky_calls and not external_imports:
            lines.append("No dependency risks identified.")
            lines.append("")

        return "\n".join(lines)

    if section_key == "security_posture":
        entrypoints = context.get("entrypoints", [])
        layer_groups = context.get("layer_groups", {})
        top_fan_in = context.get("top_fan_in", [])
        top_fan_out = context.get("top_fan_out", [])
        lines = [f"## {title}", ""]

        if entrypoints:
            lines.append(f"**Entrypoints (attack surface):** {', '.join(f'`{e}`' for e in entrypoints)}")
            lines.append("")

        presentation = layer_groups.get("presentation", [])
        if presentation:
            lines.append(f"**Externally-facing modules:** {', '.join(f'`{m}`' for m in presentation)}")
            lines.append("")

        if top_fan_in:
            lines.append("**High-impact modules (most dependents — changes propagate widely):**")
            for name, count in top_fan_in[:5]:
                lines.append(f"- `{name}` — {count} dependent(s)")
            lines.append("")

        if top_fan_out:
            lines.append("**High-coupling modules (most dependencies):**")
            for name, count in top_fan_out[:5]:
                lines.append(f"- `{name}` — depends on {count} module(s)")
            lines.append("")

        for layer_name in ("entrypoint", "presentation", "service", "data_access"):
            mods = layer_groups.get(layer_name, [])
            if mods:
                lines.append(f"**{layer_name.replace('_', ' ').title()} layer:** {', '.join(f'`{m}`' for m in mods)}")

        lines.append("")
        return "\n".join(lines)

    return f"## {title}\n\nNo template available for this section.\n"


def _build_section_prompt(section_key: str, context: Dict[str, Any]) -> str:
    title = SECTION_TITLES[section_key]
    instructions = SECTION_INSTRUCTIONS[section_key]
    context_json = json_for_prompt(context, max_chars=12000)
    return (
        f"# Section: {title}\n\n"
        f"## Instructions\n{instructions}\n\n"
        f"## Context (structured data from static analysis and semgrep scan)\n"
        f"```json\n{context_json}\n```\n\n"
        f"Write the '{title}' section now. Use markdown formatting."
    )


def _generate_section(state: SecurityAgentState, section_key: str) -> str:
    context = state["section_context"][section_key]

    # vulnerability_summary is always deterministic
    if section_key == "vulnerability_summary":
        logger.info("Section '%s': deterministic (always)", section_key)
        return _render_section_fallback(section_key, context)

    if state.get("skip_llm", False):
        logger.info("Section '%s': using deterministic fallback (skip_llm=True)", section_key)
        return _render_section_fallback(section_key, context)

    # For critical_findings and code_quality_risks, skip LLM if no findings
    if section_key == "critical_findings" and not context.get("findings"):
        logger.info("Section '%s': no findings, using fallback", section_key)
        return _render_section_fallback(section_key, context)

    if section_key == "code_quality_risks":
        if not context.get("findings_medium") and not context.get("findings_low"):
            logger.info("Section '%s': no findings, using fallback", section_key)
            return _render_section_fallback(section_key, context)

    prompt = _build_section_prompt(section_key, context)
    logger.info("Section '%s': invoking LLM (%s)", section_key, state["model"])
    t0 = time.time()
    result = invoke_llm(state["model"], state["base_url"], prompt, SYSTEM_PROMPT)
    if result is None:
        logger.warning(
            "Section '%s': LLM failed, using deterministic fallback", section_key
        )
        return _render_section_fallback(section_key, context)
    logger.info("Section '%s': LLM completed in %.2fs", section_key, time.time() - t0)
    return result


# ── LangGraph nodes ────────────────────────────────────────────────────────

def _node_load_artifacts(state: SecurityAgentState) -> Dict[str, Any]:
    artifact_root = Path(state["artifact_root"])
    artifacts = load_artifacts(artifact_root)
    return {"artifacts": artifacts}


def _node_run_semgrep(state: SecurityAgentState) -> Dict[str, Any]:
    semgrep_results = _run_semgrep(state["source_dir"])
    return {"semgrep_results": semgrep_results}


def _node_analyze(state: SecurityAgentState) -> Dict[str, Any]:
    analysis = _build_analysis(state["artifacts"], state["semgrep_results"])
    return {"analysis": analysis}


def _node_build_section_context(state: SecurityAgentState) -> Dict[str, Any]:
    section_context = _build_section_context(
        state["artifacts"], state["semgrep_results"], state["analysis"],
    )
    return {"section_context": section_context, "sections": {}}


def _section_node(section_key: str):
    def node(state: SecurityAgentState) -> Dict[str, Any]:
        return {"sections": {section_key: _generate_section(state, section_key)}}
    return node


def _node_synthesize_report(state: SecurityAgentState) -> Dict[str, Any]:
    report = {
        "report_metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pipeline": "security_agent_v1",
            "llm_provider": "ollama",
            "model": state["model"],
            "artifact_root": state["artifact_root"],
            "semgrep_scan_time": state["semgrep_results"].get("scan_time_seconds", 0),
            "semgrep_error": state["semgrep_results"].get("error"),
        },
        "ingestion_metadata": state["artifacts"]["project_metadata"],
        "sections": state.get("sections", {}),
    }
    return {"report": report}


def _build_graph():
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise RuntimeError("Missing dependency 'langgraph'.") from exc

    graph = StateGraph(SecurityAgentState)

    # Nodes
    graph.add_node("load_artifacts", _node_load_artifacts)
    graph.add_node("run_semgrep", _node_run_semgrep)
    graph.add_node("analyze", _node_analyze)
    graph.add_node("build_section_context", _node_build_section_context)
    for section in SECTION_ORDER:
        graph.add_node(section, _section_node(section))
    graph.add_node("synthesize_report", _node_synthesize_report)

    # Edges: load_artifacts → run_semgrep → analyze → build_section_context → fan-out → synthesize_report
    graph.set_entry_point("load_artifacts")
    graph.add_edge("load_artifacts", "run_semgrep")
    graph.add_edge("run_semgrep", "analyze")
    graph.add_edge("analyze", "build_section_context")
    # Fan-out: all sections in parallel
    for section in SECTION_ORDER:
        graph.add_edge("build_section_context", section)
    # Fan-in: all sections converge to synthesize_report
    for section in SECTION_ORDER:
        graph.add_edge(section, "synthesize_report")
    graph.add_edge("synthesize_report", END)

    return graph.compile()


# ── Report export ──────────────────────────────────────────────────────────

def _render_markdown_report(report: Dict[str, Any]) -> str:
    lines = [
        "# Security Agent Report",
        "",
        "## Report Metadata",
        "```json",
        json.dumps(report.get("report_metadata", {}), indent=2),
        "```",
        "",
    ]
    sections = report.get("sections", {})
    for key in SECTION_ORDER:
        lines.append(sections.get(key, ""))
        lines.append("")
    return "\n".join(lines)


def _export_report(report: Dict[str, Any], out_dir: str) -> None:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    write_json(out_path / "security_report.json", report)
    (out_path / "security_report.md").write_text(
        _render_markdown_report(report), encoding="utf-8"
    )
    sections_dir = out_path / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    for section_key, text in report.get("sections", {}).items():
        (sections_dir / f"{section_key}.md").write_text(text, encoding="utf-8")


# ── Public API ─────────────────────────────────────────────────────────────

def run_security_pipeline(
    ingestion_input: str,
    source_dir: str,
    model: str = "llama3.1:8b",
    base_url: Optional[str] = None,
    skip_llm: bool = False,
    out_dir: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_base_url = base_url or os.getenv(
        "OLLAMA_BASE_URL", "http://localhost:11434"
    )
    logger.info(
        "Security Agent pipeline started: input=%s, source_dir=%s, model=%s, skip_llm=%s",
        ingestion_input, source_dir, model, skip_llm,
    )
    pipeline_start = time.time()

    with TemporaryDirectory() as td:
        artifact_root = resolve_artifact_root(ingestion_input, Path(td))
        graph = _build_graph()
        final_state: SecurityAgentState = graph.invoke({
            "artifact_root": str(artifact_root),
            "source_dir": source_dir,
            "model": model,
            "base_url": resolved_base_url,
            "skip_llm": skip_llm,
        })

    report = final_state["report"]
    logger.info(
        "Security Agent pipeline complete in %.2fs: %d sections",
        time.time() - pipeline_start,
        len(report.get("sections", {})),
    )

    if out_dir:
        _export_report(report, out_dir)

    return report
