#!/usr/bin/env python3
"""
architecture_pipeline.py

Deterministic, graph-driven architecture explainer for ingestion pipeline outputs.
Uses LangGraph (LangChain ecosystem) for workflow orchestration and CodeLlama
through Ollama via langchain-ollama as the LLM provider.
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
from typing import Annotated, Any, Dict, List, Optional, Tuple, TypedDict

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
    "overview",
    "module_architecture",
    "execution_flow",
    "object_model",
    "dependency_analysis",
    "risk_analysis",
]

SECTION_TITLES = {
    "overview": "System Overview",
    "module_architecture": "Module Architecture",
    "execution_flow": "Execution Flow",
    "object_model": "Object Model and Inheritance",
    "dependency_analysis": "Dependency and Coupling Analysis",
    "risk_analysis": "Risks and Improvement Opportunities",
}

SECTION_INSTRUCTIONS = {
    "overview": (
        "Explain system purpose, major responsibilities, and what this codebase is optimized for. "
        "Reference concrete evidence from provided context."
    ),
    "module_architecture": (
        "For each module explain its architectural role and responsibility in plain terms: what problem it solves, "
        "which layer it belongs to (entry point, service, data access, utility, config, etc.), and how it "
        "collaborates with other modules. Describe key classes and functions by *purpose*, not just by name. "
        "Identify any architectural patterns you can infer (Service Layer, Repository, Facade, Strategy, etc.). "
        "Group modules by layer where meaningful and explain why the boundaries exist."
    ),
    "execution_flow": (
        "Explain runtime behavior in a logical order from likely entrypoints to downstream calls. "
        "Use call paths and highlight branching/critical paths. "
        "Do NOT describe any call as recursive unless the key_facts explicitly confirm recursion."
    ),
    "object_model": (
        "For each class explain its responsibility in plain terms: what it manages, what decisions it makes. "
        "Describe method *purposes* (not just signatures) and how methods collaborate within and across classes. "
        "Identify design patterns where evident (Service, Repository, Factory, Strategy, etc.). "
        "Explain the inheritance rationale: what is shared in the base and why? What do subclasses specialise? "
        "Flag any concerning OO design choices such as deep hierarchies, god classes, or anemic models."
    ),
    "dependency_analysis": (
        "Map the dependency structure from an architectural perspective. Identify tiers or layers and assess "
        "whether dependency flow is clean (higher layers depend on lower, not vice versa). "
        "Call out any circular dependencies or layering violations explicitly. "
        "Explain change impact: which modules, if modified, would propagate the furthest? "
        "Assess whether coupling level is appropriate for the codebase size and purpose."
    ),
    "risk_analysis": (
        "Identify architectural risks, unresolved/static-analysis blind spots, likely maintenance pain points, "
        "and concrete remediation opportunities."
    ),
}

SYSTEM_PROMPT = (
    "You are an expert software architecture explainer. "
    "Your output MUST be grounded exclusively in the provided context. "
    "RULES — follow all of them without exception:\n"
    "1. Never invent classes, functions, files, dependencies, patterns, or runtime behaviours not present in the context.\n"
    "2. The context contains a 'key_facts' section — treat every item in it as ground truth you MUST NOT contradict.\n"
    "3. If the context states 'no cycles detected', do NOT say cycles exist. "
    "If the context shows an inheritance chain, do NOT alter or extend it.\n"
    "4. When something cannot be determined from the static context, say so explicitly — do not guess.\n"
    "5. Base every claim on a concrete piece of evidence from the context (module name, method name, call edge, etc.)."
)


class ArchitectureState(TypedDict, total=False):
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


def _rank_counter(counter_obj: Counter, limit: int = 10) -> List[Dict[str, Any]]:
    ranked = sorted(counter_obj.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    return [{"name": name, "count": count} for name, count in ranked]


def _derive_call_paths(
    call_graph: Dict[str, List[str]],
    max_paths: int = 12,
    max_depth: int = 6,
) -> List[List[str]]:
    adjacency: Dict[str, List[str]] = {
        caller: sorted(set(callees)) for caller, callees in call_graph.items()
    }
    nodes = set(adjacency.keys())
    in_degree: Counter = Counter()
    for callees in adjacency.values():
        for callee in callees:
            nodes.add(callee)
            in_degree[callee] += 1

    roots = sorted(n for n in nodes if in_degree[n] == 0)
    if not roots:
        roots = sorted(nodes)

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
    """Per-module breakdown of classes, functions, and imports — richer than metrics."""
    detail = []
    for name, mod in sorted(modules.items()):
        classes = mod.get("classes", [])
        functions = mod.get("functions", [])
        class_info = []
        for cls in classes:
            methods = [m["function_name"] for m in cls.get("methods", [])]
            class_info.append(
                {
                    "name": cls["class_name"],
                    "qualified_name": cls["qualified_name"],
                    "bases": cls.get("inherits", []),
                    "methods": methods,
                }
            )
        func_names = [f["function_name"] for f in functions]
        imports = [
            imp.get("module") or imp.get("name")
            for imp in mod.get("imports", [])
            if imp.get("module") or imp.get("name")
        ]
        detail.append(
            {
                "module": name,
                "file_path": mod.get("file_path", ""),
                "classes": class_info,
                "functions": func_names,
                "imports": [i for i in imports if i],
            }
        )
    return detail


def _extract_class_detail(modules: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Detailed per-class breakdown including method signatures and intra-class calls."""
    classes = []
    for mod_name, mod in sorted(modules.items()):
        for cls in mod.get("classes", []):
            methods = []
            for m in cls.get("methods", []):
                params = [
                    p["name"]
                    for p in m.get("parameters", [])
                    if p["name"] != "self"
                ]
                call_names = [c["callee_name"] for c in m.get("calls", [])]
                methods.append(
                    {
                        "name": m["function_name"],
                        "params": params,
                        "return_type": m.get("return_type"),
                        "calls": call_names[:6],
                    }
                )
            classes.append(
                {
                    "qualified_name": cls["qualified_name"],
                    "class_name": cls["class_name"],
                    "module": mod_name,
                    "bases": cls.get("inherits", []),
                    "methods": methods,
                    "attribute_count": len(cls.get("attributes", [])),
                }
            )
    return classes


def _detect_dependency_cycles(dep_graph: Dict[str, List[str]]) -> List[List[str]]:
    """Return all simple cycles in the module dependency graph (DFS)."""
    cycles: List[List[str]] = []
    visited: set = set()

    def dfs(node: str, stack: List[str]) -> None:
        visited.add(node)
        stack.append(node)
        for neighbor in dep_graph.get(node, []):
            if neighbor in stack:
                cycle_start = stack.index(neighbor)
                cycles.append(stack[cycle_start:] + [neighbor])
            elif neighbor not in visited:
                dfs(neighbor, stack)
        stack.pop()

    for node in list(dep_graph.keys()):
        if node not in visited:
            dfs(node, [])
    return cycles


def _infer_architectural_layers(
    dep_graph: Dict[str, List[str]],
    modules: Dict[str, Any],
    entrypoints: List[str],
) -> Dict[str, str]:
    """Assign each module to an architectural layer based on naming and dependency position."""
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
        elif any(p in parts for p in ("test", "tests", "testing")):
            layers[mod] = "test"
        elif any(p in parts for p in ("util", "utils", "helper", "helpers", "common", "shared", "mixin")):
            layers[mod] = "utility"
        elif any(p in parts for p in ("model", "models", "entity", "entities", "schema", "schemas", "dto")):
            layers[mod] = "model"
        elif any(p in parts for p in ("db", "database", "repo", "repository", "store", "storage", "orm")):
            layers[mod] = "data_access"
        elif any(p in parts for p in ("service", "services", "manager", "managers", "handler", "handlers")):
            layers[mod] = "service"
        elif any(p in parts for p in ("route", "routes", "api", "endpoint", "view", "views", "controller", "controllers")):
            layers[mod] = "presentation"
        elif any(p in parts for p in ("config", "settings", "constants", "env", "conf")):
            layers[mod] = "config"
        elif in_degree[mod] >= 2:
            layers[mod] = "shared"
        else:
            layers[mod] = "other"
    return layers


def _build_key_facts(
    artifacts: Dict[str, Any],
    analysis: Dict[str, Any],
    cycles: List[List[str]],
    layers: Dict[str, str],
) -> List[str]:
    """
    Ground-truth assertions injected into every section prompt.
    The LLM must treat these as non-negotiable facts.
    """
    meta = artifacts["project_metadata"]
    inheritance = artifacts["inheritance_graph"]
    qs = analysis["quality_signals"]
    facts = []

    # Project basics
    facts.append(
        f"Total source files: {meta.get('total_source_files', '?')}, "
        f"classes: {meta.get('total_classes', '?')}, "
        f"functions: {meta.get('total_functions', '?')}, "
        f"tracked calls: {meta.get('total_calls', '?')}."
    )
    facts.append(f"Primary language: {meta.get('primary_language', 'unknown')}.")

    # Entrypoints
    eps = analysis.get("likely_entrypoints", [])
    facts.append(f"Likely entrypoints: {', '.join(eps) if eps else 'none identified'}.")

    # Inheritance — state the exact chains to prevent hallucination
    if inheritance:
        chains = []
        for cls, parents in sorted(inheritance.items()):
            if parents:
                chains.append(f"{cls} extends {', '.join(parents)}")
            else:
                chains.append(f"{cls} is a root class (no parent)")
        facts.append("Exact inheritance relationships: " + "; ".join(chains) + ".")
    else:
        facts.append("No inheritance relationships found.")

    # Cycles
    if cycles:
        facts.append(
            f"Circular dependencies detected: {len(cycles)}. "
            f"Cycles: {'; '.join(' -> '.join(c) for c in cycles[:3])}."
        )
    else:
        facts.append("No circular dependencies detected in the dependency graph.")

    # Unresolved calls
    unresolved = qs.get("total_unresolved_calls", 0)
    total_calls = meta.get("total_calls", 1)
    ratio = qs.get("unresolved_call_ratio", 0)
    facts.append(
        f"Unresolved calls: {unresolved} of {total_calls} ({ratio:.0%}). "
        "Unresolved calls are typically external library calls or built-ins, not missing code."
    )

    # Recursion detection
    call_graph = artifacts.get("call_graph", {})
    recursive = [
        caller for caller, callees in call_graph.items()
        if caller in callees
    ]
    if recursive:
        facts.append(f"Recursive calls detected: {', '.join(recursive)}.")
    else:
        facts.append("No recursive function calls detected in the call graph.")

    # Layer assignments
    if layers:
        layer_summary = {}
        for mod, layer in layers.items():
            layer_summary.setdefault(layer, []).append(mod)
        facts.append(
            "Module layers (do not reassign): "
            + "; ".join(f"{layer}: {', '.join(mods)}" for layer, mods in sorted(layer_summary.items()))
            + "."
        )

    return facts




def _build_deterministic_analysis(artifacts: Dict[str, Any]) -> Dict[str, Any]:
    modules: Dict[str, Any] = artifacts["modules"]
    call_graph: Dict[str, List[str]] = artifacts["call_graph"]
    dependency_graph: Dict[str, List[str]] = artifacts["dependency_graph"]
    inheritance_graph: Dict[str, List[str]] = artifacts["inheritance_graph"]

    module_metrics: List[Dict[str, Any]] = []
    total_unresolved_calls = 0
    external_imports: Counter = Counter()
    local_modules = set(modules.keys())

    for module_name in sorted(modules.keys()):
        module_obj = modules[module_name]
        classes = module_obj.get("classes", [])
        functions = module_obj.get("functions", [])
        method_count = sum(len(cls.get("methods", [])) for cls in classes)
        all_calls = collect_calls(module_obj)
        resolved_calls = sum(1 for c in all_calls if c.get("is_resolved"))
        unresolved_calls = len(all_calls) - resolved_calls
        total_unresolved_calls += unresolved_calls

        for imp in module_obj.get("imports", []):
            target = imp.get("module") or imp.get("name")
            if not target:
                continue
            root_pkg = target.split(".")[0]
            if (
                target not in local_modules
                and root_pkg not in local_modules
                and imp.get("level", 0) == 0
            ):
                external_imports[root_pkg] += 1

        module_metrics.append(
            {
                "module": module_name,
                "file_path": module_obj.get("file_path"),
                "imports": len(module_obj.get("imports", [])),
                "classes": len(classes),
                "functions": len(functions),
                "methods": method_count,
                "calls_total": len(all_calls),
                "calls_resolved": resolved_calls,
                "calls_unresolved": unresolved_calls,
            }
        )

    module_metrics.sort(
        key=lambda m: (
            -m["calls_total"],
            -(m["classes"] + m["functions"] + m["methods"]),
            m["module"],
        )
    )

    in_degree: Counter = Counter()
    out_degree: Counter = Counter()
    for caller, callees in call_graph.items():
        unique_callees = set(callees)
        out_degree[caller] = len(unique_callees)
        for callee in unique_callees:
            in_degree[callee] += 1

    dependency_in_degree: Counter = Counter()
    dependency_out_degree: Counter = Counter()
    for module_name, deps in dependency_graph.items():
        unique_deps = set(deps)
        dependency_out_degree[module_name] = len(unique_deps)
        for dep in unique_deps:
            dependency_in_degree[dep] += 1

    inheritance_root_classes = sorted(
        cls for cls, parents in inheritance_graph.items() if not parents
    )

    call_paths = _derive_call_paths(call_graph, max_paths=12, max_depth=6)

    module_names_sorted = sorted(modules.keys())
    likely_entrypoints = [
        name for name in module_names_sorted if name == "main" or name.endswith(".main")
    ]
    likely_entrypoints.extend(
        caller
        for caller in sorted(call_graph.keys())
        if caller.endswith(".main") and caller not in likely_entrypoints
    )

    analysis = {
        "project": artifacts["project_metadata"],
        "module_count": len(modules),
        "symbol_count": len(artifacts["symbol_table"]),
        "module_metrics": module_metrics,
        "top_modules_by_call_volume": module_metrics[:10],
        "call_graph_hotspots": {
            "top_callers": _rank_counter(out_degree),
            "top_callees": _rank_counter(in_degree),
            "derived_paths": call_paths,
        },
        "dependency_hotspots": {
            "top_most_dependent_modules": _rank_counter(dependency_out_degree),
            "top_most_depended_on_modules": _rank_counter(dependency_in_degree),
        },
        "inheritance_summary": {
            "class_node_count": len(inheritance_graph),
            "root_classes": inheritance_root_classes[:20],
        },
        "quality_signals": {
            "total_unresolved_calls": total_unresolved_calls,
            "unresolved_call_ratio": (
                (total_unresolved_calls / max(1, sum(m["calls_total"] for m in module_metrics)))
            ),
            "external_imports": _rank_counter(external_imports),
        },
        "likely_entrypoints": likely_entrypoints,
    }
    return analysis


def _build_section_context(
    artifacts: Dict[str, Any], analysis: Dict[str, Any]
) -> Dict[str, Dict[str, Any]]:
    modules = artifacts["modules"]
    module_metrics = analysis["module_metrics"]
    dependency_graph = artifacts["dependency_graph"]
    call_graph = artifacts["call_graph"]
    inheritance_graph = artifacts["inheritance_graph"]
    entrypoints = analysis["likely_entrypoints"]

    module_detail = _extract_module_detail(modules)
    class_detail = _extract_class_detail(modules)
    cycles = _detect_dependency_cycles(dependency_graph)
    layers = _infer_architectural_layers(dependency_graph, modules, entrypoints)
    key_facts = _build_key_facts(artifacts, analysis, cycles, layers)

    # Group modules by inferred layer for quick LLM/fallback consumption
    layer_groups: Dict[str, List[str]] = {}
    for mod, layer in sorted(layers.items()):
        layer_groups.setdefault(layer, []).append(mod)

    context = {
        "overview": {
            "key_facts": key_facts,
            "project_metadata": artifacts["project_metadata"],
            "module_count": analysis["module_count"],
            "symbol_count": analysis["symbol_count"],
            "top_modules_by_call_volume": analysis["top_modules_by_call_volume"],
            "likely_entrypoints": entrypoints,
            "folder_tree": artifacts.get("folder_tree"),
        },
        "module_architecture": {
            "key_facts": key_facts,
            "module_detail": module_detail,
            "module_metrics": module_metrics[:25],
            "layer_groups": layer_groups,
            "dependency_graph": dependency_graph,
            "dependency_hotspots": analysis["dependency_hotspots"],
            "cycles": cycles,
        },
        "execution_flow": {
            "key_facts": key_facts,
            "likely_entrypoints": entrypoints,
            "call_hotspots": analysis["call_graph_hotspots"],
            "raw_call_graph": call_graph,
        },
        "object_model": {
            "key_facts": key_facts,
            "class_detail": class_detail,
            "inheritance_graph": inheritance_graph,
            "inheritance_summary": analysis["inheritance_summary"],
            "layer_groups": layer_groups,
        },
        "dependency_analysis": {
            "key_facts": key_facts,
            "dependency_graph": dependency_graph,
            "layer_groups": layer_groups,
            "layers": layers,
            "cycles": cycles,
            "dependency_hotspots": analysis["dependency_hotspots"],
            "external_imports": analysis["quality_signals"]["external_imports"],
            "likely_entrypoints": entrypoints,
        },
        "risk_analysis": {
            "key_facts": key_facts,
            "quality_signals": analysis["quality_signals"],
            "module_metrics_top": module_metrics[:20],
            "call_hotspots": analysis["call_graph_hotspots"],
            "dependency_hotspots": analysis["dependency_hotspots"],
            "cycles": cycles,
        },
    }
    return context


def _render_section_fallback(section_key: str, context: Dict[str, Any]) -> str:
    """Generate human-readable prose from structured context (no LLM needed)."""
    title = SECTION_TITLES[section_key]

    if section_key == "overview":
        meta = context.get("project_metadata", {})
        langs = meta.get("languages_detected", [])
        entrypoints = context.get("likely_entrypoints", [])
        top_modules = context.get("top_modules_by_call_volume", [])
        lines = [
            f"## {title}",
            "",
            f"This project contains **{meta.get('total_source_files', '?')} source files** "
            f"across **{len(langs)} language(s)** ({', '.join(langs) or 'unknown'}).",
            f"The primary language is **{meta.get('primary_language', 'unknown')}**.",
            f"The codebase defines **{meta.get('total_classes', 0)} classes** and "
            f"**{meta.get('total_functions', 0)} functions** with "
            f"**{meta.get('total_calls', 0)} call sites** tracked.",
            "",
        ]
        if entrypoints:
            lines.append(f"**Likely entrypoints:** {', '.join(f'`{e}`' for e in entrypoints)}")
            lines.append("")
        if top_modules:
            lines.append("**Most active modules by call volume:**")
            for m in top_modules[:5]:
                lines.append(f"- `{m['module']}` — {m['calls_total']} calls, {m['classes']} classes, {m['functions']} functions")
            lines.append("")
        return "\n".join(lines)

    if section_key == "module_architecture":
        module_detail = context.get("module_detail", [])
        layer_groups = context.get("layer_groups", {})
        dep_graph = context.get("dependency_graph", {})
        cycles = context.get("cycles", [])
        dep_hotspots = context.get("dependency_hotspots", {})
        lines = [f"## {title}", ""]

        # Layer summary
        layer_order = ["entrypoint", "presentation", "service", "data_access", "model", "utility", "config", "shared", "other", "test"]
        present_layers = [l for l in layer_order if l in layer_groups]
        if present_layers:
            lines.append("### Architectural Layers")
            lines.append("")
            for layer in present_layers:
                mods = layer_groups[layer]
                lines.append(f"**{layer.replace('_', ' ').title()}:** {', '.join(f'`{m}`' for m in mods)}")
            lines.append("")

        # Per-module breakdown grouped by layer
        by_layer: Dict[str, List[Dict]] = {}
        mod_by_name = {m["module"]: m for m in module_detail}
        for layer in layer_order:
            for mod_name in layer_groups.get(layer, []):
                by_layer.setdefault(layer, []).append(mod_by_name.get(mod_name, {"module": mod_name}))

        lines.append("### Module Breakdown")
        lines.append("")
        for layer in layer_order:
            mods = by_layer.get(layer, [])
            if not mods:
                continue
            lines.append(f"**{layer.replace('_', ' ').title()}**")
            lines.append("")
            for m in mods:
                name = m.get("module", "?")
                fpath = m.get("file_path", "")
                header = f"`{name}`" + (f" (`{fpath}`)" if fpath else "")
                lines.append(f"- {header}")
                classes = m.get("classes", [])
                funcs = m.get("functions", [])
                imports = m.get("imports", [])
                if classes:
                    for cls in classes:
                        method_str = ", ".join(f"`{mth}`" for mth in cls["methods"][:8])
                        bases = cls.get("bases", [])
                        base_str = f" extends {', '.join(f'`{b}`' for b in bases)}" if bases else ""
                        lines.append(f"  - Class `{cls['name']}`{base_str} — methods: {method_str or '(none)'}")
                if funcs:
                    lines.append(f"  - Functions: {', '.join(f'`{f}`' for f in funcs[:8])}")
                if imports:
                    lines.append(f"  - Imports: {', '.join(f'`{i}`' for i in imports[:6])}")
            lines.append("")

        # Most depended-on modules
        most_depended = dep_hotspots.get("top_most_depended_on_modules", [])
        if most_depended:
            lines.append("### Core / Shared Modules (highest in-degree)")
            lines.append("")
            for d in most_depended[:5]:
                lines.append(f"- `{d['name']}` — {d['count']} module(s) depend on it")
            lines.append("")

        if cycles:
            lines.append(f"**Warning: {len(cycles)} circular dependency cycle(s) detected.**")
            for cycle in cycles[:3]:
                lines.append(f"- {' → '.join(f'`{n}`' for n in cycle)}")
            lines.append("")

        return "\n".join(lines)

    if section_key == "execution_flow":
        entrypoints = context.get("likely_entrypoints", [])
        call_hotspots = context.get("call_hotspots", {})
        paths = call_hotspots.get("derived_paths", [])
        lines = [f"## {title}", ""]
        if entrypoints:
            lines.append(f"**Entrypoints:** {', '.join(f'`{e}`' for e in entrypoints)}")
            lines.append("")
        if paths:
            lines.append("**Derived call paths:**")
            lines.append("")
            for i, path in enumerate(paths[:10], 1):
                chain = " -> ".join(f"`{node}`" for node in path)
                lines.append(f"{i}. {chain}")
            lines.append("")
        top_callers = call_hotspots.get("top_callers", [])
        if top_callers:
            lines.append("**Top callers (highest fan-out):**")
            for c in top_callers[:5]:
                lines.append(f"- `{c['name']}` — calls {c['count']} unique targets")
            lines.append("")
        return "\n".join(lines)

    if section_key == "object_model":
        class_detail = context.get("class_detail", [])
        inheritance = context.get("inheritance_graph", {})
        summary = context.get("inheritance_summary", {})
        layer_groups = context.get("layer_groups", {})
        lines = [f"## {title}", ""]

        node_count = summary.get("class_node_count", 0)
        lines.append(f"The project defines **{node_count} class(es)**.")
        lines.append("")

        # Build inheritance tree (children → parents map already in inheritance_graph)
        # Derive parent → children for tree rendering
        children_of: Dict[str, List[str]] = {}
        for cls, parents in inheritance.items():
            for p in parents:
                children_of.setdefault(p, []).append(cls)

        roots = [cls for cls, parents in inheritance.items() if not parents]

        if roots:
            lines.append("### Inheritance Hierarchy")
            lines.append("")
            lines.append("```")

            def _tree(node: str, prefix: str = "") -> List[str]:
                out = [f"{prefix}{node}"]
                kids = sorted(children_of.get(node, []))
                for i, kid in enumerate(kids):
                    connector = "└── " if i == len(kids) - 1 else "├── "
                    indent = "    " if i == len(kids) - 1 else "│   "
                    out.extend(_tree(kid, prefix + connector))
                return out

            for root in sorted(roots):
                lines.extend(_tree(root))
            lines.append("```")
            lines.append("")

        # Per-class detail
        if class_detail:
            lines.append("### Class Responsibilities")
            lines.append("")
            for cls in class_detail:
                qn = cls["qualified_name"]
                bases = cls.get("bases", [])
                base_str = f" extends {', '.join(f'`{b}`' for b in bases)}" if bases else ""
                lines.append(f"**`{qn}`**{base_str}")

                methods = cls.get("methods", [])
                if methods:
                    for m in methods:
                        params = ", ".join(m.get("params", []))
                        ret = f" → `{m['return_type']}`" if m.get("return_type") else ""
                        calls = m.get("calls", [])
                        call_str = f" — calls: {', '.join(f'`{c}`' for c in calls[:4])}" if calls else ""
                        lines.append(f"- `{m['name']}({params})`{ret}{call_str}")
                else:
                    lines.append("- *(no methods)*")
                lines.append("")

        return "\n".join(lines)

    if section_key == "dependency_analysis":
        dep_graph = context.get("dependency_graph", {})
        layer_groups = context.get("layer_groups", {})
        layers = context.get("layers", {})
        cycles = context.get("cycles", [])
        dep_hotspots = context.get("dependency_hotspots", {})
        ext_imports = context.get("external_imports", [])
        lines = [f"## {title}", ""]

        # Layer flow diagram
        layer_order = ["entrypoint", "presentation", "service", "data_access", "model", "utility", "config", "shared", "other"]
        present_layers = [l for l in layer_order if l in layer_groups]
        if len(present_layers) > 1:
            lines.append("### Dependency Layer Flow")
            lines.append("")
            lines.append(" → ".join(f"**{l.replace('_', ' ').title()}**" for l in present_layers))
            lines.append("")
            lines.append("*(expected flow: higher layers depend on lower; violations indicate coupling problems)*")
            lines.append("")

        # Full internal dependency map
        lines.append("### Internal Dependency Map")
        lines.append("")
        has_deps = False
        for mod in sorted(dep_graph.keys()):
            deps = dep_graph[mod]
            if deps:
                has_deps = True
                layer = layers.get(mod, "other")
                lines.append(f"- `{mod}` [{layer}] → {', '.join(f'`{d}`' for d in sorted(deps))}")
        if not has_deps:
            lines.append("- No internal dependencies detected.")
        lines.append("")

        # Coupling metrics
        most_dependent = dep_hotspots.get("top_most_dependent_modules", [])
        most_depended = dep_hotspots.get("top_most_depended_on_modules", [])
        if most_dependent or most_depended:
            lines.append("### Coupling Analysis")
            lines.append("")
            if most_dependent:
                lines.append("**Highest fan-out (depends on most modules):**")
                for d in most_dependent[:5]:
                    lines.append(f"- `{d['name']}` — depends on {d['count']} module(s)")
                lines.append("")
            if most_depended:
                lines.append("**Highest fan-in (most modules depend on this — change risk):**")
                for d in most_depended[:5]:
                    lines.append(f"- `{d['name']}` — {d['count']} dependent(s) — changes here propagate widely")
                lines.append("")

        # Cycles
        if cycles:
            lines.append(f"### Circular Dependencies ({len(cycles)} detected)")
            lines.append("")
            lines.append("**Circular dependencies prevent clean layering and make testing harder:**")
            for cycle in cycles[:5]:
                lines.append(f"- {' → '.join(f'`{n}`' for n in cycle)}")
            lines.append("")
        else:
            lines.append("### Circular Dependencies")
            lines.append("")
            lines.append("No circular dependencies detected.")
            lines.append("")

        # External imports
        if ext_imports:
            lines.append("### External Dependencies")
            lines.append("")
            for e in ext_imports[:10]:
                lines.append(f"- `{e['name']}` — imported {e['count']} time(s)")
            lines.append("")

        return "\n".join(lines)

    if section_key == "risk_analysis":
        quality = context.get("quality_signals", {})
        metrics = context.get("module_metrics_top", [])
        dep_hotspots = context.get("dependency_hotspots", {})
        lines = [f"## {title}", ""]
        unresolved = quality.get("total_unresolved_calls", 0)
        ratio = quality.get("unresolved_call_ratio", 0)
        lines.append("**Static analysis gaps:**")
        lines.append(f"- **{unresolved} unresolved call(s)** ({ratio:.0%} of all calls)")
        lines.append("- Unresolved calls indicate symbols that could not be traced to a definition in the project.")
        lines.append("")
        high_complexity = [m for m in metrics if m.get("calls_total", 0) > 5]
        if high_complexity:
            lines.append("**High-complexity modules (>5 call sites):**")
            for m in high_complexity[:5]:
                lines.append(f"- `{m['module']}` — {m['calls_total']} calls, {m['calls_unresolved']} unresolved")
            lines.append("")
        most_depended = dep_hotspots.get("top_most_depended_on_modules", [])
        bottlenecks = [d for d in most_depended if d["count"] >= 2]
        if bottlenecks:
            lines.append("**Potential bottleneck modules (2+ dependents):**")
            for d in bottlenecks:
                lines.append(f"- `{d['name']}` — changes here affect {d['count']} modules")
            lines.append("")
        lines.append("**Recommendations:**")
        if unresolved > 0:
            lines.append("- Investigate unresolved calls — they may indicate missing imports, dynamic dispatch, or external library usage.")
        if bottlenecks:
            lines.append("- Consider interface abstraction for high-dependency modules to reduce coupling.")
        lines.append("- Add integration tests for the main call paths identified in the execution flow.")
        lines.append("")
        return "\n".join(lines)

    return f"## {title}\n\nNo template available for this section.\n"


def _build_section_prompt(section_key: str, section_context: Dict[str, Any]) -> str:
    title = SECTION_TITLES[section_key]
    instruction = SECTION_INSTRUCTIONS[section_key]
    context_blob = json_for_prompt(section_context, max_chars=12000)
    return (
        f"Section: {title}\n\n"
        f"Instruction: {instruction}\n\n"
        "Output requirements:\n"
        "- Use explicit architecture vocabulary.\n"
        "- Cite module/function/class names from context wherever possible.\n"
        "- Mention limitations caused by unresolved static analysis.\n"
        "- Keep the section detailed and practical.\n\n"
        "Structured context:\n"
        f"{context_blob}"
    )


def _coverage_checks(state: ArchitectureState) -> Dict[str, Any]:
    warnings: List[str] = []
    sections = state.get("sections", {})
    analysis = state.get("analysis", {})

    top_modules = [
        item["module"] for item in analysis.get("top_modules_by_call_volume", [])[:5]
    ]
    module_section = sections.get("module_architecture", "")
    missing_module_mentions = [m for m in top_modules if m not in module_section]
    if missing_module_mentions:
        warnings.append(
            "Some top modules are missing from module architecture narrative: "
            + ", ".join(missing_module_mentions)
        )

    unresolved_calls = (
        analysis.get("quality_signals", {}).get("total_unresolved_calls", 0)
    )
    risk_section = sections.get("risk_analysis", "").lower()
    if unresolved_calls > 0 and "unresolved" not in risk_section:
        warnings.append(
            "Risk section does not explicitly discuss unresolved static call resolution gaps."
        )

    missing_sections = [k for k in SECTION_ORDER if not sections.get(k, "").strip()]
    if missing_sections:
        warnings.append("Missing section content: " + ", ".join(missing_sections))

    return {"warnings": warnings, "warning_count": len(warnings)}


def _render_markdown_report(report: Dict[str, Any]) -> str:
    meta = report.get("report_metadata", {})
    lines = [
        "# Architecture Explainer Report",
        "",
        f"*Generated {meta.get('generated_at', 'N/A')} · "
        f"model: {meta.get('model', 'N/A')} · "
        f"pipeline: {meta.get('pipeline', 'N/A')}*",
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

    write_json(out_path / "architecture_report.json", report)
    (out_path / "architecture_report.md").write_text(
        _render_markdown_report(report), encoding="utf-8"
    )

    sections_dir = out_path / "sections"
    sections_dir.mkdir(parents=True, exist_ok=True)
    for section_key, text in report.get("sections", {}).items():
        (sections_dir / f"{section_key}.md").write_text(text, encoding="utf-8")


def _node_load_artifacts(state: ArchitectureState) -> Dict[str, Any]:
    artifact_root = Path(state["artifact_root"])
    artifacts = load_artifacts(artifact_root)
    return {"artifacts": artifacts}


def _node_analyze_artifacts(state: ArchitectureState) -> Dict[str, Any]:
    analysis = _build_deterministic_analysis(state["artifacts"])
    return {"analysis": analysis}


def _node_build_section_context(state: ArchitectureState) -> Dict[str, Any]:
    section_context = _build_section_context(state["artifacts"], state["analysis"])
    return {"section_context": section_context, "sections": {}}


def _generate_section(state: ArchitectureState, section_key: str) -> str:
    context = state["section_context"][section_key]
    if state.get("skip_llm", False):
        logger.info("Section '%s': using deterministic fallback (skip_llm=True)", section_key)
        return _render_section_fallback(section_key, context)
    prompt = _build_section_prompt(section_key, context)
    logger.info("Section '%s': invoking LLM (%s)", section_key, state["model"])
    t0 = time.time()
    result = invoke_llm(state["model"], state["base_url"], prompt, SYSTEM_PROMPT)
    if result is None:
        logger.warning("Section '%s': LLM failed, using deterministic fallback", section_key)
        return _render_section_fallback(section_key, context)
    logger.info("Section '%s': LLM completed in %.2fs", section_key, time.time() - t0)
    return result


def _section_node(section_key: str):
    def node(state: ArchitectureState) -> Dict[str, Any]:
        return {"sections": {section_key: _generate_section(state, section_key)}}

    return node


def _node_synthesize_report(state: ArchitectureState) -> Dict[str, Any]:
    report_metadata = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "pipeline": "architecture_explainer_v1",
        "llm_provider": "ollama",
        "model": state["model"],
        "artifact_root": state["artifact_root"],
    }
    coverage = _coverage_checks(state)
    report = {
        "report_metadata": report_metadata,
        "ingestion_metadata": state["artifacts"]["project_metadata"],
        "deterministic_analysis": state["analysis"],
        "sections": state.get("sections", {}),
        "coverage_checks": coverage,
    }
    return {"report": report}


def _build_graph():
    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'langgraph'. Install requirements before running architecture pipeline."
        ) from exc

    graph = StateGraph(ArchitectureState)
    graph.add_node("load_artifacts", _node_load_artifacts)
    graph.add_node("analyze_artifacts", _node_analyze_artifacts)
    graph.add_node("build_section_context", _node_build_section_context)
    graph.add_node("overview", _section_node("overview"))
    graph.add_node("module_architecture", _section_node("module_architecture"))
    graph.add_node("execution_flow", _section_node("execution_flow"))
    graph.add_node("object_model", _section_node("object_model"))
    graph.add_node("dependency_analysis", _section_node("dependency_analysis"))
    graph.add_node("risk_analysis", _section_node("risk_analysis"))
    graph.add_node("synthesize_report", _node_synthesize_report)

    section_nodes = [
        "overview",
        "module_architecture",
        "execution_flow",
        "object_model",
        "dependency_analysis",
        "risk_analysis",
    ]

    graph.set_entry_point("load_artifacts")
    graph.add_edge("load_artifacts", "analyze_artifacts")
    graph.add_edge("analyze_artifacts", "build_section_context")
    # Fan-out: all 6 section nodes run in parallel after context is built
    for section in section_nodes:
        graph.add_edge("build_section_context", section)
    # Fan-in: all section nodes converge to synthesis
    for section in section_nodes:
        graph.add_edge(section, "synthesize_report")
    graph.add_edge("synthesize_report", END)
    return graph.compile()


def run_architecture_pipeline(
    ingestion_input: str,
    model: str = "llama3.1:8b",
    base_url: Optional[str] = None,
    skip_llm: bool = False,
    out_dir: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_base_url = base_url or os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    logger.info("Architecture pipeline started: input=%s, model=%s, skip_llm=%s", ingestion_input, model, skip_llm)
    pipeline_start = time.time()

    with TemporaryDirectory() as td:
        artifact_root = resolve_artifact_root(ingestion_input, Path(td))
        graph = _build_graph()
        final_state: ArchitectureState = graph.invoke(
            {
                "artifact_root": str(artifact_root),
                "model": model,
                "base_url": resolved_base_url,
                "skip_llm": skip_llm,
            }
        )

    report = final_state["report"]
    coverage = report.get("coverage_checks", {})
    logger.info(
        "Architecture pipeline complete in %.2fs: %d sections, %d coverage warnings",
        time.time() - pipeline_start,
        len(report.get("sections", {})),
        coverage.get("warning_count", 0),
    )

    if out_dir:
        _export_report(report, out_dir)

    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate architecture explanation from ingestion outputs."
    )
    parser.add_argument(
        "ingestion_input",
        help="Path to ingestion output directory or zip archive.",
    )
    parser.add_argument(
        "--out",
        default="architecture_output",
        help="Output directory for architecture report artifacts.",
    )
    parser.add_argument(
        "--model",
        default="llama3.1:8b",
        help="Ollama model name (default: llama3.1:8b).",
    )
    parser.add_argument(
        "--base-url",
        default=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
        help="Ollama base URL.",
    )
    parser.add_argument(
        "--skip-llm",
        action="store_true",
        help="Skip LLM generation and emit deterministic fallback sections.",
    )
    args = parser.parse_args(argv)

    report = run_architecture_pipeline(
        ingestion_input=args.ingestion_input,
        out_dir=args.out,
        model=args.model,
        base_url=args.base_url,
        skip_llm=args.skip_llm,
    )

    print(f"Architecture report generated in: {args.out}")
    print(f"Sections generated: {', '.join(report.get('sections', {}).keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

