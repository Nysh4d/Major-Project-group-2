#!/usr/bin/env python3
"""
pipeline_utils.py

Shared utilities for all agent pipelines:
- Safe ZIP extraction (zip-slip prevention)
- Artifact loading and discovery
- LLM invocation with retry
- Section merge reducer for LangGraph state
"""

from __future__ import annotations

import json
import logging
import os
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ── Safe ZIP extraction ───────────────────────────────────────────────────

def safe_extract_zip(zip_path: str, dest_dir: str) -> None:
    """Extract a ZIP archive, raising ValueError on path traversal attempts."""
    dest = os.path.realpath(dest_dir)
    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            member_path = os.path.realpath(os.path.join(dest, member))
            if not member_path.startswith(dest + os.sep) and member_path != dest:
                raise ValueError(
                    f"Zip slip detected: member '{member}' escapes target directory"
                )
        zf.extractall(dest)


# ── Artifact loading ──────────────────────────────────────────────────────

REQUIRED_ARTIFACTS = (
    "project_metadata.json",
    "modules.json",
    "symbol_table.json",
    "graphs/call_graph.json",
    "graphs/dependency_graph.json",
    "graphs/inheritance_graph.json",
)


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)


def required_exists(root: Path) -> bool:
    return all((root / rel).exists() for rel in REQUIRED_ARTIFACTS)


def discover_artifact_root(search_root: Path) -> Path:
    if required_exists(search_root):
        return search_root
    for candidate in sorted(
        (p for p in search_root.rglob("*") if p.is_dir()),
        key=lambda p: (len(p.parts), str(p)),
    ):
        if required_exists(candidate):
            return candidate
    raise FileNotFoundError(
        f"Could not locate ingestion artifacts under '{search_root}'. "
        f"Expected: {', '.join(REQUIRED_ARTIFACTS)}"
    )


def resolve_artifact_root(ingestion_input: str, scratch_dir: Path) -> Path:
    input_path = Path(ingestion_input)
    if input_path.is_dir():
        return discover_artifact_root(input_path)
    if input_path.is_file() and input_path.suffix.lower() == ".zip":
        extract_dir = scratch_dir / "ingestion_artifacts"
        extract_dir.mkdir(parents=True, exist_ok=True)
        safe_extract_zip(str(input_path), str(extract_dir))
        return discover_artifact_root(extract_dir)
    raise ValueError(
        f"ingestion_input must be a directory or .zip archive. Got: {ingestion_input}"
    )


def load_artifacts(artifact_root: Path) -> Dict[str, Any]:
    artifacts = {
        "project_metadata": read_json(artifact_root / "project_metadata.json"),
        "modules": read_json(artifact_root / "modules.json"),
        "symbol_table": read_json(artifact_root / "symbol_table.json"),
        "call_graph": read_json(artifact_root / "graphs" / "call_graph.json"),
        "dependency_graph": read_json(artifact_root / "graphs" / "dependency_graph.json"),
        "inheritance_graph": read_json(artifact_root / "graphs" / "inheritance_graph.json"),
    }
    folder_tree_path = artifact_root / "folder_tree.json"
    artifacts["folder_tree"] = (
        read_json(folder_tree_path) if folder_tree_path.exists() else None
    )
    return artifacts


def collect_calls(module_obj: Dict[str, Any]) -> List[Dict[str, Any]]:
    calls: List[Dict[str, Any]] = []
    calls.extend(module_obj.get("calls", []))
    for fn in module_obj.get("functions", []):
        calls.extend(fn.get("calls", []))
    for cls in module_obj.get("classes", []):
        for method in cls.get("methods", []):
            calls.extend(method.get("calls", []))
    return calls


# ── Section merge reducer ─────────────────────────────────────────────────

def merge_sections(a: Dict[str, str], b: Dict[str, str]) -> Dict[str, str]:
    """Reducer for the Annotated sections field — merges parallel section updates."""
    merged = dict(a)
    merged.update(b)
    return merged


# ── LLM helpers ───────────────────────────────────────────────────────────

def truncate_for_prompt(text: str, max_chars: int = 12000) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 64] + "\n\n...[context truncated]..."


def json_for_prompt(payload: Any, max_chars: int = 12000) -> str:
    return truncate_for_prompt(json.dumps(payload, indent=2), max_chars=max_chars)


def get_chat_model(model: str, base_url: str):
    try:
        from langchain_ollama import ChatOllama
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency 'langchain-ollama'. Install requirements."
        ) from exc
    return ChatOllama(model=model, base_url=base_url, temperature=0)


def invoke_llm(
    model: str, base_url: str, prompt: str, system_prompt: str,
) -> Optional[str]:
    """Invoke LLM with retry. Returns None on failure (caller falls back to deterministic)."""
    chat = get_chat_model(model, base_url)
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        try:
            response = chat.invoke([
                ("system", system_prompt),
                ("human", prompt),
            ])
            content = getattr(response, "content", response)
            if isinstance(content, list):
                parts: List[str] = []
                for item in content:
                    if isinstance(item, dict) and "text" in item:
                        parts.append(str(item["text"]))
                    else:
                        parts.append(str(item))
                text = "\n".join(parts).strip()
            else:
                text = str(content).strip()
            if text:
                return text
        except Exception:
            logger.warning(
                "LLM attempt %d/%d failed", attempt, max_attempts, exc_info=True
            )
        if attempt < max_attempts:
            time.sleep(2 ** (attempt - 1))
    return None
