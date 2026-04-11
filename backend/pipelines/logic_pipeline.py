#!/usr/bin/env python3
"""
logic_pipeline.py

Developer Agent — explains *how* a codebase works in plain language.
Uses LangGraph for workflow orchestration and Ollama as the LLM provider.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
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
    truncate_for_prompt,
    write_json,
    REQUIRED_ARTIFACTS,
)

logger = logging.getLogger(__name__)

SECTION_ORDER = [
    "purpose",
    "walkthrough",
    "components",
    "data_flow",
    "patterns",
]

SECTION_TITLES = {
    "purpose": "What This Project Does",
    "walkthrough": "How The Code Runs",
    "components": "What Each Piece Does",
    "data_flow": "How Data Moves Through The System",
    "patterns": "Patterns and Idioms Used",
}

SECTION_INSTRUCTIONS = {
    "purpose": (
        "Write a plain-English summary of what this project does, who would use it, and what "
        "its main capabilities are. Base this ONLY on evidence: module names, class names, "
        "function names, imports, and entrypoints. Imagine you are describing this project to "
        "a developer who has never seen it. Do NOT guess the domain — derive it from the code."
    ),
    "walkthrough": (
        "Walk through the code's execution step by step, starting from the entrypoint(s). "
        "For each step, explain what the function/method *does* in plain language, not just "
        "what it calls. Follow the main call paths and explain branching points. "
        "Use the call graph and derived paths as your source of truth. "
        "Write as if you are pair-programming and narrating: 'First, main() gets a database "
        "connection by calling get_db()…' "
        "Do NOT include code blocks or code snippets — the user has access to the source. "
        "Use only plain-English narration with function/class names in backticks."
    ),
    "components": (
        "For each module and class, explain its job in one or two sentences. Focus on *what it "
        "does* and *why it exists*, not its API surface. Group by functional role. For classes, "
        "explain what the key methods do in plain terms. Skip boilerplate (__init__, __repr__) "
        "unless they do something surprising."
    ),
    "data_flow": (
        "Trace how data enters the system (entrypoints, parameters), gets transformed (function "
        "calls, method chains), gets stored or cached, and gets returned or output. Use parameter "
        "names, return types, and call chains to infer data flow. Describe the lifecycle of the "
        "main data objects. Write as if explaining to someone: 'The password comes in as a string, "
        "gets hashed via _hash(), then the hash is compared in _verify()…'"
    ),
    "patterns": (
        "Identify coding patterns, conventions, and idioms a developer needs to understand to "
        "work in this codebase. Examples: 'The project uses a service-layer pattern where all "
        "business logic lives in classes ending in Service.' Only report patterns that are "
        "actually present in the context — never invent conventions."
    ),
}

SYSTEM_PROMPT = (
    "You are an experienced senior developer explaining a codebase to a new team member. "
    "Your tone is friendly, clear, and direct — like a good pair-programming partner. "
    "Use plain English. Avoid jargon unless the codebase itself uses it. "
    "Your output MUST be grounded exclusively in the provided context.\n"
    "RULES:\n"
    "1. Never invent classes, functions, files, or behaviours not present in the context.\n"
    "2. The context contains a 'key_facts' section — treat every item as ground truth.\n"
    "3. When something cannot be determined from static analysis, say so: 'I can't tell "
    "from the code alone whether…'\n"
    "4. Use concrete names (function names, class names, file paths) as evidence for every claim.\n"
    "5. Write as if speaking to a developer, not writing a formal document."
)



class DeveloperAgentState(TypedDict, total=False):
    artifact_root: str
    output_dir: str
    model: str
    base_url: str
    skip_llm: bool
    artifacts: Dict[str, Any]
    analysis: Dict[str, Any]
    section_context: Dict[str, Dict[str, Any]]
    sections: Annotated[Dict[str, str], merge_sections]
    report: Dict[str, Any]


# ── Deterministic analysis ──────────────────────────────────────────────────

def _derive_call_paths(
    call_graph: Dict[str, List[str]],
    max_paths: int = 15,
    max_depth: int = 8,
) -> List[List[str]]:
    adjacency = {caller: sorted(set(callees)) for caller, callees in call_graph.items()}
    nodes = set(adjacency.keys())
    in_degree: Counter = Counter()
    for callees in adjacency.values():
        for c in callees:
            nodes.add(c)
            in_degree[c] += 1
    roots = sorted(n for n in nodes if in_degree[n] == 0) or sorted(nodes)

    paths: List[List[str]] = []

    def dfs(current: str, path: List[str], depth: int) -> None:
        if len(paths) >= max_paths:
            return
        next_hops = adjacency.get(current, [])
        if depth >= max_depth or not next_hops:
            paths.append(path[:])
            return
        for nxt in next_hops:
            if nxt in path:
                paths.append(path + [nxt])
                continue
            dfs(nxt, path + [nxt], depth + 1)
            if len(paths) >= max_paths:
                return

    for root in roots:
        dfs(root, [root], 0)
        if len(paths) >= max_paths:
            break
    return paths


def _extract_module_detail(modules: Dict[str, Any]) -> List[Dict[str, Any]]:
    detail = []
    for name, mod in sorted(modules.items()):
        class_info = []
        for cls in mod.get("classes", []):
            methods = []
            for m in cls.get("methods", []):
                params = [p["name"] for p in m.get("parameters", []) if p["name"] != "self"]
                call_names = [c["callee_name"] for c in m.get("calls", [])]
                methods.append({
                    "name": m["function_name"],
                    "params": params,
                    "return_type": m.get("return_type"),
                    "calls": call_names[:6],
                })
            class_info.append({
                "name": cls["class_name"],
                "qualified_name": cls["qualified_name"],
                "bases": cls.get("inherits", []),
                "methods": methods,
            })
        func_info = []
        for f in mod.get("functions", []):
            params = [p["name"] for p in f.get("parameters", []) if p["name"] != "self"]
            call_names = [c["callee_name"] for c in f.get("calls", [])]
            func_info.append({
                "name": f["function_name"],
                "qualified_name": f["qualified_name"],
                "params": params,
                "return_type": f.get("return_type"),
                "calls": call_names[:6],
            })
        imports = [
            imp.get("module") or imp.get("name")
            for imp in mod.get("imports", [])
            if imp.get("module") or imp.get("name")
        ]
        detail.append({
            "module": name,
            "file_path": mod.get("file_path", ""),
            "classes": class_info,
            "functions": func_info,
            "imports": [i for i in imports if i],
        })
    return detail


def _infer_module_roles(
    dep_graph: Dict[str, List[str]],
    modules: Dict[str, Any],
    entrypoints: List[str],
) -> Dict[str, str]:
    roles: Dict[str, str] = {}
    entry_bases = {ep.split(".")[0] for ep in entrypoints}
    for mod in modules:
        parts = mod.lower().split(".")
        leaf = parts[-1]
        if mod in entry_bases or leaf == "main":
            roles[mod] = "entrypoint"
        elif any(p in parts for p in ("test", "tests")):
            roles[mod] = "test"
        elif any(p in parts for p in ("util", "utils", "helper", "helpers", "common")):
            roles[mod] = "utility"
        elif any(p in parts for p in ("model", "models", "entity", "entities", "schema")):
            roles[mod] = "model"
        elif any(p in parts for p in ("db", "database", "repo", "repository", "store")):
            roles[mod] = "data access"
        elif any(p in parts for p in ("service", "services", "manager", "handler")):
            roles[mod] = "business logic"
        elif any(p in parts for p in ("route", "routes", "api", "endpoint", "view", "controller")):
            roles[mod] = "presentation"
        elif any(p in parts for p in ("config", "settings", "constants")):
            roles[mod] = "configuration"
        else:
            roles[mod] = "other"
    return roles


def _build_key_facts(
    artifacts: Dict[str, Any], analysis: Dict[str, Any],
) -> List[str]:
    meta = artifacts["project_metadata"]
    inheritance = artifacts["inheritance_graph"]
    facts = []
    facts.append(
        f"Total source files: {meta.get('total_source_files', '?')}, "
        f"classes: {meta.get('total_classes', '?')}, "
        f"functions: {meta.get('total_functions', '?')}, "
        f"tracked calls: {meta.get('total_calls', '?')}."
    )
    facts.append(f"Primary language: {meta.get('primary_language', 'unknown')}.")
    eps = analysis.get("entrypoints", [])
    facts.append(f"Likely entrypoints: {', '.join(eps) if eps else 'none identified'}.")
    if inheritance:
        chains = []
        for cls, parents in sorted(inheritance.items()):
            if parents:
                chains.append(f"{cls} extends {', '.join(parents)}")
            else:
                chains.append(f"{cls} is a standalone class (no parent)")
        facts.append("Inheritance: " + "; ".join(chains) + ".")
    return facts


def _build_analysis(artifacts: Dict[str, Any]) -> Dict[str, Any]:
    modules = artifacts["modules"]
    call_graph = artifacts["call_graph"]
    dep_graph = artifacts["dependency_graph"]

    module_names = sorted(modules.keys())
    entrypoints = [n for n in module_names if n == "main" or n.endswith(".main")]
    entrypoints.extend(
        c for c in sorted(call_graph.keys())
        if c.endswith(".main") and c not in entrypoints
    )

    call_paths = _derive_call_paths(call_graph, max_paths=15, max_depth=8)
    module_detail = _extract_module_detail(modules)
    roles = _infer_module_roles(dep_graph, modules, entrypoints)

    role_groups: Dict[str, List[str]] = {}
    for mod, role in sorted(roles.items()):
        role_groups.setdefault(role, []).append(mod)

    out_degree: Counter = Counter()
    in_degree: Counter = Counter()
    for caller, callees in call_graph.items():
        unique = set(callees)
        out_degree[caller] = len(unique)
        for c in unique:
            in_degree[c] += 1

    return {
        "entrypoints": entrypoints,
        "call_paths": call_paths,
        "module_detail": module_detail,
        "roles": roles,
        "role_groups": role_groups,
        "top_callers": sorted(out_degree.items(), key=lambda kv: -kv[1])[:10],
        "top_callees": sorted(in_degree.items(), key=lambda kv: -kv[1])[:10],
        "module_count": len(modules),
    }


# ── Section context ─────────────────────────────────────────────────────────

def _build_section_context(
    artifacts: Dict[str, Any], analysis: Dict[str, Any],
) -> Dict[str, Dict[str, Any]]:
    key_facts = _build_key_facts(artifacts, analysis)
    call_graph = artifacts["call_graph"]
    dep_graph = artifacts["dependency_graph"]
    inheritance_graph = artifacts["inheritance_graph"]

    return {
        "purpose": {
            "key_facts": key_facts,
            "project_metadata": artifacts["project_metadata"],
            "entrypoints": analysis["entrypoints"],
            "role_groups": analysis["role_groups"],
            "module_detail": analysis["module_detail"],
        },
        "walkthrough": {
            "key_facts": key_facts,
            "entrypoints": analysis["entrypoints"],
            "call_paths": analysis["call_paths"],
            "call_graph": call_graph,
            "module_detail": analysis["module_detail"],
        },
        "components": {
            "key_facts": key_facts,
            "module_detail": analysis["module_detail"],
            "roles": analysis["roles"],
            "role_groups": analysis["role_groups"],
            "dependency_graph": dep_graph,
        },
        "data_flow": {
            "key_facts": key_facts,
            "entrypoints": analysis["entrypoints"],
            "call_paths": analysis["call_paths"],
            "module_detail": analysis["module_detail"],
            "inheritance_graph": inheritance_graph,
        },
        "patterns": {
            "key_facts": key_facts,
            "module_detail": analysis["module_detail"],
            "role_groups": analysis["role_groups"],
            "inheritance_graph": inheritance_graph,
            "dependency_graph": dep_graph,
            "call_graph": call_graph,
        },
    }


# ── Deterministic fallback sections ─────────────────────────────────────────

def _render_section_fallback(section_key: str, context: Dict[str, Any]) -> str:
    title = SECTION_TITLES[section_key]

    if section_key == "purpose":
        meta = context.get("project_metadata", {})
        entrypoints = context.get("entrypoints", [])
        role_groups = context.get("role_groups", {})
        module_detail = context.get("module_detail", [])
        lang = meta.get("primary_language", "unknown")
        files = meta.get("total_source_files", "?")
        classes = meta.get("total_classes", 0)
        functions = meta.get("total_functions", 0)

        lines = [f"## {title}", ""]
        lines.append(
            f"This is a **{lang}** project with **{files} source files**, "
            f"**{classes} classes**, and **{functions} functions**."
        )
        lines.append("")
        if entrypoints:
            lines.append(f"The program starts at: {', '.join(f'`{e}`' for e in entrypoints)}.")
            lines.append("")

        capabilities = []
        for role, mods in sorted(role_groups.items()):
            if role in ("other", "test"):
                continue
            mod_names = ", ".join(f"`{m}`" for m in mods[:4])
            capabilities.append(f"- **{role.title()}** — {mod_names}")
        if capabilities:
            lines.append("**What it contains:**")
            lines.extend(capabilities)
            lines.append("")

        all_class_names = []
        all_func_names = []
        for m in module_detail:
            for cls in m.get("classes", []):
                all_class_names.append(cls["name"])
            for fn in m.get("functions", []):
                all_func_names.append(fn["name"])
        if all_class_names:
            lines.append(f"**Key classes:** {', '.join(f'`{c}`' for c in all_class_names[:10])}")
            lines.append("")
        if all_func_names:
            lines.append(f"**Key functions:** {', '.join(f'`{f}`' for f in all_func_names[:10])}")
            lines.append("")
        return "\n".join(lines)

    if section_key == "walkthrough":
        entrypoints = context.get("entrypoints", [])
        call_paths = context.get("call_paths", [])
        lines = [f"## {title}", ""]
        if entrypoints:
            lines.append(f"Execution starts at: {', '.join(f'`{e}`' for e in entrypoints)}.")
            lines.append("")
        if call_paths:
            lines.append("**Main execution paths:**")
            lines.append("")
            for i, path in enumerate(call_paths[:12], 1):
                steps = []
                for j, node in enumerate(path):
                    if j == 0:
                        steps.append(f"`{node}` is called")
                    else:
                        steps.append(f"which calls `{node}`")
                lines.append(f"{i}. {', '.join(steps)}.")
            lines.append("")
        else:
            lines.append("No call paths could be derived from the call graph.")
            lines.append("")
        return "\n".join(lines)

    if section_key == "components":
        module_detail = context.get("module_detail", [])
        roles = context.get("roles", {})
        role_groups = context.get("role_groups", {})
        lines = [f"## {title}", ""]
        role_order = [
            "entrypoint", "business logic", "data access", "model",
            "presentation", "utility", "configuration", "other",
        ]
        for role in role_order:
            mods_in_role = role_groups.get(role, [])
            if not mods_in_role:
                continue
            lines.append(f"### {role.title()}")
            lines.append("")
            for mod_name in mods_in_role:
                mod = next((m for m in module_detail if m["module"] == mod_name), None)
                if not mod:
                    continue
                fpath = mod.get("file_path", "")
                lines.append(f"**`{mod_name}`** (`{fpath}`)")
                for cls in mod.get("classes", []):
                    bases = cls.get("bases", [])
                    base_str = f" (extends {', '.join(f'`{b}`' for b in bases)})" if bases else ""
                    lines.append(f"- Class `{cls['name']}`{base_str}")
                    for mth in cls.get("methods", []):
                        if mth["name"].startswith("__") and mth["name"] != "__init__":
                            continue
                        params = ", ".join(mth.get("params", []))
                        ret = f" → `{mth['return_type']}`" if mth.get("return_type") else ""
                        calls = mth.get("calls", [])
                        call_str = (
                            f" — calls: {', '.join(f'`{c}`' for c in calls[:3])}"
                            if calls else ""
                        )
                        lines.append(f"  - `{mth['name']}({params})`{ret}{call_str}")
                for fn in mod.get("functions", []):
                    params = ", ".join(fn.get("params", []))
                    ret = f" → `{fn['return_type']}`" if fn.get("return_type") else ""
                    calls = fn.get("calls", [])
                    call_str = (
                        f" — calls: {', '.join(f'`{c}`' for c in calls[:3])}"
                        if calls else ""
                    )
                    lines.append(f"- Function `{fn['name']}({params})`{ret}{call_str}")
                lines.append("")
        return "\n".join(lines)

    if section_key == "data_flow":
        entrypoints = context.get("entrypoints", [])
        call_paths = context.get("call_paths", [])
        module_detail = context.get("module_detail", [])
        lines = [f"## {title}", ""]

        func_sigs: Dict[str, Dict] = {}
        for mod in module_detail:
            for fn in mod.get("functions", []):
                func_sigs[fn.get("qualified_name", fn["name"])] = fn
            for cls in mod.get("classes", []):
                for mth in cls.get("methods", []):
                    qn = f"{cls['qualified_name']}.{mth['name']}"
                    func_sigs[qn] = mth

        if entrypoints:
            lines.append(
                f"Data enters the system at: {', '.join(f'`{e}`' for e in entrypoints)}."
            )
            lines.append("")

        if call_paths:
            lines.append("**Data transformation chains:**")
            lines.append("")
            seen = set()
            for path in call_paths[:10]:
                path_key = " → ".join(path)
                if path_key in seen:
                    continue
                seen.add(path_key)
                steps = []
                for node in path:
                    sig = func_sigs.get(node)
                    if sig:
                        params = ", ".join(sig.get("params", []))
                        ret = sig.get("return_type", "")
                        label = f"`{node}({params})`"
                        if ret:
                            label += f" → `{ret}`"
                    else:
                        label = f"`{node}`"
                    steps.append(label)
                lines.append(f"- {' → '.join(steps)}")
            lines.append("")
        else:
            lines.append("No data flow chains could be derived.")
            lines.append("")
        return "\n".join(lines)

    if section_key == "patterns":
        module_detail = context.get("module_detail", [])
        role_groups = context.get("role_groups", {})
        inheritance = context.get("inheritance_graph", {})
        lines = [f"## {title}", ""]

        patterns_found = []

        service_mods = role_groups.get("business logic", [])
        if service_mods:
            patterns_found.append(
                f"**Service Layer** — Business logic is encapsulated in dedicated service "
                f"modules ({', '.join(f'`{m}`' for m in service_mods[:4])}). "
                f"Other modules call into services rather than implementing logic directly."
            )

        for cls, parents in inheritance.items():
            if parents:
                patterns_found.append(
                    f"**Inheritance / Template** — `{cls}` extends "
                    f"`{', '.join(parents)}`, inheriting shared behaviour and specialising on top."
                )

        da_mods = role_groups.get("data access", [])
        if da_mods:
            patterns_found.append(
                f"**Centralised Data Access** — Database interaction is isolated in "
                f"{', '.join(f'`{m}`' for m in da_mods[:3])}, keeping it out of business logic."
            )

        ep_mods = role_groups.get("entrypoint", [])
        if ep_mods and len(ep_mods) == 1:
            patterns_found.append(
                f"**Single Entry Point** — The application has one entry point "
                f"(`{ep_mods[0]}`), which orchestrates the startup flow."
            )

        util_mods = role_groups.get("utility", [])
        if util_mods:
            all_funcs = []
            for mod in module_detail:
                if mod["module"] in util_mods:
                    all_funcs.extend(fn["name"] for fn in mod.get("functions", []))
            if all_funcs:
                patterns_found.append(
                    f"**Utility Belt** — Reusable helpers "
                    f"({', '.join(f'`{f}`' for f in all_funcs[:5])}) are collected in "
                    f"{', '.join(f'`{m}`' for m in util_mods)}."
                )

        if patterns_found:
            for p in patterns_found:
                lines.append(f"- {p}")
                lines.append("")
        else:
            lines.append("No strong patterns could be identified from static analysis alone.")
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
        f"## Context (structured data from static analysis)\n"
        f"```json\n{context_json}\n```\n\n"
        f"Write the '{title}' section now. Use markdown formatting."
    )


def _generate_section(state: DeveloperAgentState, section_key: str) -> str:
    context = state["section_context"][section_key]
    if state.get("skip_llm", False):
        logger.info("Section '%s': using deterministic fallback (skip_llm=True)", section_key)
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


# ── LangGraph nodes ─────────────────────────────────────────────────────────

def _node_load_artifacts(state: DeveloperAgentState) -> Dict[str, Any]:
    artifact_root = Path(state["artifact_root"])
    artifacts = load_artifacts(artifact_root)
    return {"artifacts": artifacts}


def _node_analyze(state: DeveloperAgentState) -> Dict[str, Any]:
    analysis = _build_analysis(state["artifacts"])
    return {"analysis": analysis}


def _node_build_section_context(state: DeveloperAgentState) -> Dict[str, Any]:
    section_context = _build_section_context(state["artifacts"], state["analysis"])
    return {"section_context": section_context, "sections": {}}


def _section_node(section_key: str):
    def node(state: DeveloperAgentState) -> Dict[str, Any]:
        return {"sections": {section_key: _generate_section(state, section_key)}}
    return node


def _node_synthesize_report(state: DeveloperAgentState) -> Dict[str, Any]:
    report = {
        "report_metadata": {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "pipeline": "developer_agent_v1",
            "llm_provider": "ollama",
            "model": state["model"],
            "artifact_root": state["artifact_root"],
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

    graph = StateGraph(DeveloperAgentState)
    graph.add_node("load_artifacts", _node_load_artifacts)
    graph.add_node("analyze", _node_analyze)
    graph.add_node("build_section_context", _node_build_section_context)
    for section in SECTION_ORDER:
        graph.add_node(section, _section_node(section))
    graph.add_node("synthesize_report", _node_synthesize_report)

    graph.set_entry_point("load_artifacts")
    graph.add_edge("load_artifacts", "analyze")
    graph.add_edge("analyze", "build_section_context")
    for section in SECTION_ORDER:
        graph.add_edge("build_section_context", section)
    for section in SECTION_ORDER:
        graph.add_edge(section, "synthesize_report")
    graph.add_edge("synthesize_report", END)
    return graph.compile()


# ── Report export ───────────────────────────────────────────────────────────

def _render_markdown_report(report: Dict[str, Any]) -> str:
    lines = [
        "# Developer Agent Report",
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
    write_json(out_path / "logic_report.json", report)
    (out_path / "logic_report.md").write_text(
        _render_markdown_report(report), encoding="utf-8"
    )
    sections_dir = out_path / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    for section_key, text in report.get("sections", {}).items():
        (sections_dir / f"{section_key}.md").write_text(text, encoding="utf-8")


# ── Public API ──────────────────────────────────────────────────────────────

def run_logic_pipeline(
    ingestion_input: str,
    model: str = "llama3.1:8b",
    base_url: Optional[str] = None,
    skip_llm: bool = False,
    out_dir: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_base_url = base_url or os.getenv(
        "OLLAMA_BASE_URL", "http://localhost:11434"
    )
    logger.info(
        "Developer Agent pipeline started: input=%s, model=%s, skip_llm=%s",
        ingestion_input, model, skip_llm,
    )
    pipeline_start = time.time()

    with TemporaryDirectory() as td:
        artifact_root = resolve_artifact_root(ingestion_input, Path(td))
        graph = _build_graph()
        final_state: DeveloperAgentState = graph.invoke({
            "artifact_root": str(artifact_root),
            "model": model,
            "base_url": resolved_base_url,
            "skip_llm": skip_llm,
        })

    report = final_state["report"]
    logger.info(
        "Developer Agent pipeline complete in %.2fs: %d sections",
        time.time() - pipeline_start,
        len(report.get("sections", {})),
    )

    if out_dir:
        _export_report(report, out_dir)

    return report


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate plain-language logic explanation from ingestion outputs."
    )
    parser.add_argument(
        "ingestion_input",
        help="Path to ingestion output directory or zip archive.",
    )
    parser.add_argument(
        "--out", default="logic_output",
        help="Output directory for logic report.",
    )
    parser.add_argument(
        "--model", default="llama3.1:8b",
        help="Ollama model name (default: llama3.1:8b).",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        help="Ollama base URL.",
    )
    parser.add_argument(
        "--skip-llm", action="store_true",
        help="Skip LLM and use deterministic fallback sections.",
    )
    args = parser.parse_args(argv)

    report = run_logic_pipeline(
        ingestion_input=args.ingestion_input,
        out_dir=args.out,
        model=args.model,
        base_url=args.base_url,
        skip_llm=args.skip_llm,
    )
    print(f"Developer Agent report generated in: {args.out}")
    print(f"Sections: {', '.join(report.get('sections', {}).keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
