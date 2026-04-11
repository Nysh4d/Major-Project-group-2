#!/usr/bin/env python3
"""
ingestion_pipeline.py

Creates a structured JSON representation of a source code ZIP for downstream agents.

Usage: python pipelines/ingestion_pipeline.py /path/to/project.zip [--out ingestion_output]

Design notes:
- Multi-language ingestion with deterministic IDs.
- Python files use AST-based parsing for higher fidelity.
- Other languages use conservative regex-based parsing heuristics.
- Output schema is backward-compatible with prior ingestion outputs and adds
  language metadata.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import logging
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)

IGNORED_DIR_NAMES = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    "venv",
    "dist",
    "build",
    "target",
    ".idea",
    ".vscode",
}

LANGUAGE_EXTENSIONS: Dict[str, List[str]] = {
    "python": [".py"],
    "javascript": [".js", ".mjs", ".cjs", ".jsx"],
    "typescript": [".ts", ".tsx"],
    "java": [".java"],
    "kotlin": [".kt", ".kts"],
    "scala": [".scala"],
    "go": [".go"],
    "rust": [".rs"],
    "csharp": [".cs"],
    "cpp": [".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx"],
    "c": [".c", ".h"],
    "objective_c": [".m", ".mm"],
    "swift": [".swift"],
    "php": [".php"],
    "ruby": [".rb"],
    "dart": [".dart"],
    "lua": [".lua"],
    "r": [".r"],
    "shell": [".sh", ".bash", ".zsh"],
}

EXTENSION_TO_LANGUAGE: Dict[str, str] = {}
for _language_name, _extensions in LANGUAGE_EXTENSIONS.items():
    for _ext in _extensions:
        EXTENSION_TO_LANGUAGE[_ext] = _language_name

SUPPORTED_EXTENSIONS = set(EXTENSION_TO_LANGUAGE.keys())

BRACE_LANGUAGES = {
    "javascript",
    "typescript",
    "java",
    "kotlin",
    "scala",
    "go",
    "rust",
    "csharp",
    "cpp",
    "c",
    "objective_c",
    "php",
    "swift",
    "dart",
}

CALL_KEYWORDS = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "throw",
    "sizeof",
    "typeof",
    "new",
    "delete",
    "class",
    "def",
    "function",
    "with",
    "case",
    "else",
    "do",
    "try",
}

FUNCTION_NAME_BLACKLIST = {
    "if",
    "for",
    "while",
    "switch",
    "catch",
    "return",
    "throw",
    "class",
    "new",
    "delete",
    "with",
    "typeof",
    "sizeof",
    "case",
    "default",
}

RE_CALL_CANDIDATE = re.compile(r"([A-Za-z_][A-Za-z0-9_$.:]*)\s*\(")
RE_CLASS_DECL = re.compile(
    r"^\s*(?:export\s+|public\s+|private\s+|protected\s+|internal\s+|abstract\s+|final\s+|sealed\s+|partial\s+)*"
    r"class\s+([A-Za-z_][A-Za-z0-9_]*)\b(.*)$"
)
RE_TYPE_DECL = re.compile(
    r"^\s*(?:export\s+|public\s+|private\s+|protected\s+|internal\s+|sealed\s+|partial\s+)*"
    r"(?:interface|struct|trait|enum)\s+([A-Za-z_][A-Za-z0-9_]*)\b(.*)$"
)
RE_GENERIC_ASSIGN = re.compile(
    r"^\s*(?:const|let|var|final|static|public|private|protected|internal)?\s*"
    r"([A-Za-z_][A-Za-z0-9_]*)\s*(?::\s*[^=]+)?=\s*(.+?)\s*[;#]?\s*$"
)


def sha1_short(s: str, length: int = 12) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:length]


from pipelines.pipeline_utils import safe_extract_zip as extract_zip


def rel_path_from(root: str, full: str) -> str:
    return os.path.relpath(full, root).replace("\\", "/")


def is_hidden(name: str) -> bool:
    return name.startswith(".")


def language_for_path(path: str) -> Optional[str]:
    ext = Path(path).suffix.lower()
    return EXTENSION_TO_LANGUAGE.get(ext)


def is_supported_source_file(path: str) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_EXTENSIONS


def _iter_visible_entries(path: str) -> List[str]:
    try:
        entries = sorted(os.listdir(path))
    except OSError:
        return []
    visible = []
    for entry in entries:
        if entry in IGNORED_DIR_NAMES or is_hidden(entry):
            continue
        visible.append(entry)
    return visible


def find_project_root(extracted_root: str) -> str:
    # Prefer the common ancestor of all supported source files.
    source_files: List[str] = []
    for dirpath, dirnames, filenames in os.walk(extracted_root):
        dirnames[:] = [
            d for d in dirnames if d not in IGNORED_DIR_NAMES and not is_hidden(d)
        ]
        parts = Path(dirpath).parts
        if any(part in IGNORED_DIR_NAMES or part.startswith(".") for part in parts):
            continue
        for filename in filenames:
            if filename.startswith("."):
                continue
            full_path = os.path.join(dirpath, filename)
            if is_supported_source_file(full_path):
                source_files.append(full_path)

    if source_files:
        common = os.path.commonpath(source_files)
        if os.path.isfile(common):
            common = os.path.dirname(common)
        return common

    # Fallback: keep prior behavior when no supported files are found.
    best_dir = extracted_root
    best_count = 0
    for dirpath, dirnames, filenames in os.walk(extracted_root):
        dirnames[:] = [
            d for d in dirnames if d not in IGNORED_DIR_NAMES and not is_hidden(d)
        ]
        supported_count = sum(
            1
            for filename in filenames
            if not filename.startswith(".")
            and is_supported_source_file(os.path.join(dirpath, filename))
        )
        if supported_count > best_count:
            best_count = supported_count
            best_dir = dirpath
    return best_dir


def _sanitize_module_part(part: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]", "_", part)


def module_name_from_path(project_root: str, file_path: str) -> str:
    relative = rel_path_from(project_root, file_path)
    rootless, _ext = os.path.splitext(relative)
    if rootless.endswith("/__init__"):
        rootless = rootless[: -len("/__init__")]
    parts = [_sanitize_module_part(part) for part in rootless.split("/") if part]
    if not parts:
        return _sanitize_module_part(os.path.basename(project_root))
    return ".".join(parts)


def _unique_module_name(
    modules: Dict[str, Any], module_name: str, relative_path: str
) -> str:
    if module_name not in modules:
        return module_name
    return f"{module_name}__{sha1_short(relative_path, length=6)}"


def build_folder_tree(
    project_root: str,
) -> Tuple[Dict[str, Any], Dict[str, Dict[str, str]], Dict[str, int]]:
    """
    Returns:
      - folder tree
      - source file metadata map: absolute file path -> {file_id, language}
      - per-language source file counts
    """
    file_meta_map: Dict[str, Dict[str, str]] = {}
    language_counts: Counter = Counter()

    def node_for(path: str) -> Dict[str, Any]:
        name = os.path.basename(path)
        if os.path.isdir(path):
            children = []
            for entry in _iter_visible_entries(path):
                children.append(node_for(os.path.join(path, entry)))
            return {
                "name": name or os.path.basename(path),
                "type": "directory",
                "path": rel_path_from(project_root, path),
                "children": children,
            }

        relative_path = rel_path_from(project_root, path)
        entry: Dict[str, Any] = {"name": name, "type": "file", "path": relative_path}
        language = language_for_path(path)
        if language:
            file_id = "file_" + sha1_short(relative_path)
            entry["file_id"] = file_id
            entry["language"] = language
            file_meta_map[path] = {"file_id": file_id, "language": language}
            language_counts[language] += 1
        return entry

    return node_for(project_root), file_meta_map, dict(language_counts)


def safe_unparse(node: Optional[ast.AST]) -> Optional[str]:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        try:
            return ast.dump(node)
        except Exception:
            return None


def name_from_expr(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        value = name_from_expr(node.value)
        return f"{value}.{node.attr}" if value else node.attr
    if isinstance(node, ast.Subscript):
        return name_from_expr(node.value)
    if isinstance(node, ast.Call):
        return name_from_expr(node.func)
    return safe_unparse(node) or "<expr>"


def _build_call_object(
    caller_qname: str,
    callee_name: str,
    caller_file: str,
    line_number: Optional[int],
    call_key: str,
) -> Dict[str, Any]:
    return {
        "call_id": "call_" + sha1_short(call_key),
        "caller_qualified_name": caller_qname,
        "callee_name": callee_name,
        "callee_qualified_name": None,
        "caller_file": caller_file,
        "callee_file": None,
        "line_number": line_number,
        "is_resolved": False,
    }


def parse_python_module(
    project_root: str,
    abs_path: str,
    file_id: str,
    module_name: str,
    src: str,
) -> Dict[str, Any]:
    try:
        tree = ast.parse(src)
    except Exception:
        tree = ast.parse("\n")

    file_rel = rel_path_from(project_root, abs_path)
    module_entry = {
        "module": module_name,
        "language": "python",
        "file_path": file_rel,
        "file_id": file_id,
        "imports": [],
        "globals": [],
        "classes": [],
        "functions": [],
        "calls": [],
    }

    # imports
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                module_entry["imports"].append(
                    {
                        "module": alias.name,
                        "name": None,
                        "alias": alias.asname,
                        "line_number": node.lineno,
                        "level": 0,
                    }
                )
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                module_entry["imports"].append(
                    {
                        "module": node.module,
                        "name": alias.name,
                        "alias": alias.asname,
                        "line_number": node.lineno,
                        "level": getattr(node, "level", 0),
                    }
                )

    # globals
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value = safe_unparse(node.value)
            for target_name in targets:
                module_entry["globals"].append(
                    {
                        "name": target_name,
                        "value": value,
                        "line_number": node.lineno,
                    }
                )

    used_qnames = set()

    def unique_qname(base_qname: str) -> str:
        if base_qname not in used_qnames:
            used_qnames.add(base_qname)
            return base_qname
        idx = 2
        while f"{base_qname}#{idx}" in used_qnames:
            idx += 1
        qname = f"{base_qname}#{idx}"
        used_qnames.add(qname)
        return qname

    def parse_function(
        node: ast.FunctionDef | ast.AsyncFunctionDef,
        parent_class: Optional[str] = None,
    ) -> Dict[str, Any]:
        base_qname = (
            f"{module_name}.{node.name}"
            if not parent_class
            else f"{module_name}.{parent_class}.{node.name}"
        )
        qname = unique_qname(base_qname)
        function_id = "func_" + sha1_short(qname)

        parameters: List[Dict[str, Any]] = []
        args = node.args

        posonly = getattr(args, "posonlyargs", [])
        for arg in posonly:
            parameters.append(
                {
                    "name": arg.arg,
                    "type_hint": safe_unparse(arg.annotation),
                    "default": None,
                    "kind": "positional",
                }
            )
        for arg in args.args:
            parameters.append(
                {
                    "name": arg.arg,
                    "type_hint": safe_unparse(arg.annotation),
                    "default": None,
                    "kind": "positional",
                }
            )
        if args.vararg:
            parameters.append(
                {
                    "name": args.vararg.arg,
                    "type_hint": safe_unparse(args.vararg.annotation),
                    "default": None,
                    "kind": "vararg",
                }
            )
        for arg in args.kwonlyargs:
            parameters.append(
                {
                    "name": arg.arg,
                    "type_hint": safe_unparse(arg.annotation),
                    "default": None,
                    "kind": "kwonly",
                }
            )
        if args.kwarg:
            parameters.append(
                {
                    "name": args.kwarg.arg,
                    "type_hint": safe_unparse(args.kwarg.annotation),
                    "default": None,
                    "kind": "kwarg",
                }
            )

        defaults = [safe_unparse(default) for default in args.defaults]
        if defaults:
            for i in range(1, len(defaults) + 1):
                if len(parameters) >= i:
                    parameters[-i]["default"] = defaults[-i]

        func_entry = {
            "function_id": function_id,
            "function_name": node.name,
            "qualified_name": qname,
            "parameters": parameters,
            "return_type": safe_unparse(node.returns),
            "decorators": [name_from_expr(d) for d in node.decorator_list],
            "is_method": parent_class is not None,
            "is_async": isinstance(node, ast.AsyncFunctionDef),
            "start_line": getattr(node, "lineno", None),
            "end_line": getattr(node, "end_lineno", None),
            "calls": [],
        }

        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            callee_name = name_from_expr(sub.func)
            call_key = (
                f"{qname}:{file_rel}:{getattr(sub, 'lineno', 0)}:"
                f"{getattr(sub, 'col_offset', 0)}:{callee_name}"
            )
            func_entry["calls"].append(
                _build_call_object(
                    caller_qname=qname,
                    callee_name=callee_name,
                    caller_file=file_rel,
                    line_number=getattr(sub, "lineno", None),
                    call_key=call_key,
                )
            )

        return func_entry

    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            class_name = node.name
            base_qname = f"{module_name}.{class_name}"
            class_qname = unique_qname(base_qname)
            class_id = "class_" + sha1_short(class_qname)

            methods = []
            attributes = []
            for class_node in node.body:
                if isinstance(class_node, ast.Assign):
                    for target in class_node.targets:
                        if isinstance(target, ast.Name):
                            attributes.append(
                                {
                                    "name": target.id,
                                    "value": safe_unparse(class_node.value),
                                    "line_number": class_node.lineno,
                                }
                            )
                if isinstance(class_node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    methods.append(parse_function(class_node, parent_class=class_name))

            module_entry["classes"].append(
                {
                    "class_id": class_id,
                    "class_name": class_name,
                    "qualified_name": class_qname,
                    "inherits": [name_from_expr(base) for base in node.bases],
                    "decorators": [name_from_expr(d) for d in node.decorator_list],
                    "start_line": getattr(node, "lineno", None),
                    "end_line": getattr(node, "end_lineno", None),
                    "attributes": attributes,
                    "methods": methods,
                }
            )
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            module_entry["functions"].append(parse_function(node, parent_class=None))

    # module-level calls (exclude calls nested inside class/function definitions)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            callee_name = name_from_expr(sub.func)
            call_key = (
                f"{module_name}:{file_rel}:{getattr(sub, 'lineno', 0)}:"
                f"{getattr(sub, 'col_offset', 0)}:{callee_name}"
            )
            module_entry["calls"].append(
                _build_call_object(
                    caller_qname=module_name,
                    callee_name=callee_name,
                    caller_file=file_rel,
                    line_number=getattr(sub, "lineno", None),
                    call_key=call_key,
                )
            )

    return module_entry


def _read_text_file(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except Exception:
            return ""


def _normalize_import(raw_target: str) -> Tuple[str, int]:
    raw = raw_target.strip().strip("\"'").strip()
    raw = raw.rstrip(";")
    raw = raw.replace("\\", "/")

    level = 0
    while raw.startswith("../"):
        level += 1
        raw = raw[3:]
    if raw.startswith("./"):
        level = max(level, 1)
        raw = raw[2:]
    if raw.startswith("."):
        level = max(level, 1)
        raw = raw.lstrip(".")

    raw = raw.replace("/", ".").replace("::", ".")
    if raw.endswith(".*"):
        raw = raw[:-2]
    raw = raw.strip(".")
    return raw, level


def _split_csv_safely(blob: str) -> List[str]:
    parts: List[str] = []
    current: List[str] = []
    depth = 0
    quote: Optional[str] = None
    pairs = {"(": ")", "[": "]", "{": "}", "<": ">"}
    closers = set(pairs.values())
    stack: List[str] = []

    for ch in blob:
        if quote:
            current.append(ch)
            if ch == quote:
                quote = None
            continue

        if ch in ("'", '"'):
            quote = ch
            current.append(ch)
            continue

        if ch in pairs:
            stack.append(pairs[ch])
            depth += 1
            current.append(ch)
            continue

        if ch in closers and stack and ch == stack[-1]:
            stack.pop()
            depth = max(0, depth - 1)
            current.append(ch)
            continue

        if ch == "," and depth == 0:
            part = "".join(current).strip()
            if part:
                parts.append(part)
            current = []
            continue

        current.append(ch)

    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return parts


def _clean_type_name(type_name: str) -> str:
    cleaned = re.sub(r"<[^>]*>", "", type_name).strip()
    cleaned = cleaned.replace("?", "")
    cleaned = cleaned.replace("&", "").replace("*", "")
    cleaned = cleaned.strip()
    if "." in cleaned:
        cleaned = cleaned.split(".")[-1]
    if "::" in cleaned:
        cleaned = cleaned.split("::")[-1]
    return cleaned


def _extract_parent_types(tail: str) -> List[str]:
    parents: List[str] = []
    for pattern in (
        r"extends\s+([A-Za-z0-9_$.<>,\s:]+)",
        r"implements\s+([A-Za-z0-9_$.<>,\s:]+)",
        r":\s*([A-Za-z0-9_$.<>,\s:]+)",
    ):
        match = re.search(pattern, tail)
        if not match:
            continue
        raw = match.group(1)
        for item in _split_csv_safely(raw):
            candidate = _clean_type_name(item)
            if candidate:
                parents.append(candidate)
    deduped = []
    seen = set()
    for parent in parents:
        if parent in seen:
            continue
        seen.add(parent)
        deduped.append(parent)
    return deduped


def _strip_strings_and_comments(line: str) -> str:
    """Remove string literals and single-line comments so brace counting is accurate."""
    result: List[str] = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        # Single-line comment: // or #
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break
        if ch == "#":
            break
        # String literals: "...", '...', `...`
        if ch in ('"', "'", "`"):
            quote = ch
            i += 1
            while i < n and line[i] != quote:
                if line[i] == "\\" and i + 1 < n:
                    i += 2
                else:
                    i += 1
            i += 1  # skip closing quote
            continue
        result.append(ch)
        i += 1
    return "".join(result)


def _find_brace_block(lines: List[str], start_index: int) -> Tuple[int, int]:
    # Returns 1-based start_line/end_line for the nearest brace-delimited block.
    search_limit = min(len(lines), start_index + 8)
    brace_start_index = -1
    for idx in range(start_index, search_limit):
        if "{" in _strip_strings_and_comments(lines[idx]):
            brace_start_index = idx
            break
    if brace_start_index == -1:
        return start_index + 1, start_index + 1

    depth = 0
    saw_open = False
    for idx in range(brace_start_index, len(lines)):
        cleaned = _strip_strings_and_comments(lines[idx])
        open_count = cleaned.count("{")
        close_count = cleaned.count("}")
        if open_count > 0:
            saw_open = True
        depth += open_count - close_count
        if saw_open and depth <= 0:
            return brace_start_index + 1, idx + 1

    return brace_start_index + 1, len(lines)


def _extract_imports_generic(lines: List[str], language: str) -> List[Dict[str, Any]]:
    imports: List[Dict[str, Any]] = []
    in_go_import_block = False

    def add_import(line_number: int, raw_target: str, alias: Optional[str] = None) -> None:
        module_target, level = _normalize_import(raw_target)
        imports.append(
            {
                "module": module_target or raw_target.strip(),
                "name": None,
                "alias": alias,
                "line_number": line_number,
                "level": level,
            }
        )

    for line_number, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped:
            continue

        if language in {"javascript", "typescript"}:
            match = re.search(
                r"^\s*import\s+.+?\s+from\s+['\"]([^'\"]+)['\"]", line
            )
            if match:
                add_import(line_number, match.group(1))
                continue

            match = re.search(r"^\s*import\s+['\"]([^'\"]+)['\"]", line)
            if match:
                add_import(line_number, match.group(1))
                continue

            for match in re.finditer(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", line):
                add_import(line_number, match.group(1))

        elif language in {"java", "kotlin", "scala"}:
            match = re.match(r"^\s*import\s+([A-Za-z0-9_.*]+)", line)
            if match:
                add_import(line_number, match.group(1))

        elif language == "go":
            if re.match(r"^\s*import\s*\(", line):
                in_go_import_block = True
                continue
            if in_go_import_block:
                if ")" in line:
                    in_go_import_block = False
                    continue
                match = re.search(r'"([^"]+)"', line)
                if match:
                    add_import(line_number, match.group(1))
                continue

            match = re.match(
                r'^\s*import\s+(?:[A-Za-z_][A-Za-z0-9_]*\s+)?"([^"]+)"',
                line,
            )
            if match:
                add_import(line_number, match.group(1))

        elif language == "rust":
            match = re.match(r"^\s*use\s+([^;]+);", line)
            if match:
                add_import(line_number, match.group(1))

        elif language in {"c", "cpp", "objective_c"}:
            match = re.match(r"^\s*#\s*include\s*[<\"]([^>\"]+)[>\"]", line)
            if match:
                add_import(line_number, match.group(1))

        elif language == "csharp":
            match = re.match(r"^\s*using\s+([A-Za-z0-9_.]+)\s*;", line)
            if match:
                add_import(line_number, match.group(1))

        elif language == "ruby":
            match = re.match(r"^\s*require(?:_relative)?\s+['\"]([^'\"]+)['\"]", line)
            if match:
                add_import(line_number, match.group(1))

        elif language == "php":
            match = re.match(
                r"^\s*(?:require|require_once|include|include_once)\s*\(?\s*['\"]([^'\"]+)['\"]",
                line,
            )
            if match:
                add_import(line_number, match.group(1))
            match = re.match(r"^\s*use\s+([A-Za-z0-9_\\]+)", line)
            if match:
                add_import(line_number, match.group(1).replace("\\", "."))

        elif language == "swift":
            match = re.match(r"^\s*import\s+([A-Za-z0-9_]+)", line)
            if match:
                add_import(line_number, match.group(1))

        else:
            match = re.match(r"^\s*import\s+([A-Za-z0-9_./:-]+)", line)
            if match:
                add_import(line_number, match.group(1))

    return imports


def _extract_receiver_type(receiver_blob: str) -> Optional[str]:
    text = receiver_blob.strip()
    if not text:
        return None
    tokens = [token for token in re.split(r"\s+", text) if token]
    if not tokens:
        return None
    typ = tokens[-1].lstrip("*&")
    typ = _clean_type_name(typ)
    return typ or None


def _extract_parameters(params_blob: str) -> List[Dict[str, Any]]:
    params: List[Dict[str, Any]] = []
    if not params_blob.strip():
        return params

    for chunk in _split_csv_safely(params_blob):
        piece = chunk.strip()
        if not piece or piece == "...":
            continue

        default = None
        if "=" in piece:
            left, right = piece.split("=", 1)
            piece = left.strip()
            default = right.strip()

        kind = "positional"
        if piece.startswith("*") or "..." in piece:
            kind = "vararg"

        type_hint: Optional[str] = None
        name = piece

        if ":" in piece:
            left, right = piece.split(":", 1)
            name = left.strip()
            type_hint = right.strip() or None
        else:
            tokens = [tok for tok in re.split(r"\s+", piece) if tok]
            if len(tokens) >= 2:
                name = tokens[-1]
                type_hint = " ".join(tokens[:-1]) or None
            elif tokens:
                name = tokens[0]

        name = name.strip().lstrip("*&$@")
        if not name:
            name = f"param_{len(params) + 1}"

        params.append(
            {
                "name": name,
                "type_hint": type_hint,
                "default": default,
                "kind": kind,
            }
        )

    return params


def _detect_generic_function(line: str, language: str) -> Optional[Dict[str, Any]]:
    stripped = line.strip()
    if not stripped or stripped.startswith("//") or stripped.startswith("#"):
        return None

    # JS/TS function declaration
    match = re.match(
        r"^\s*(?:export\s+)?(?:async\s+)?function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
        line,
    )
    if match:
        return {
            "name": match.group(1),
            "params_blob": match.group(2),
            "return_type": None,
            "explicit_parent": None,
            "is_async": "async" in stripped,
        }

    # JS/TS arrow function assignment
    match = re.match(
        r"^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:async\s*)?\(([^)]*)\)\s*=>",
        line,
    )
    if match:
        return {
            "name": match.group(1),
            "params_blob": match.group(2),
            "return_type": None,
            "explicit_parent": None,
            "is_async": "async" in stripped,
        }

    # Go method
    match = re.match(
        r"^\s*func\s*\(([^)]*)\)\s*([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*([^{]*)",
        line,
    )
    if match:
        return {
            "name": match.group(2),
            "params_blob": match.group(3),
            "return_type": (match.group(4).strip() or None),
            "explicit_parent": _extract_receiver_type(match.group(1)),
            "is_async": False,
        }

    # Go function
    match = re.match(
        r"^\s*func\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*([^{]*)",
        line,
    )
    if match:
        return {
            "name": match.group(1),
            "params_blob": match.group(2),
            "return_type": (match.group(3).strip() or None),
            "explicit_parent": None,
            "is_async": False,
        }

    # Rust function
    match = re.match(
        r"^\s*(?:pub\s+)?fn\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)\s*(?:->\s*([^{]+))?",
        line,
    )
    if match:
        return {
            "name": match.group(1),
            "params_blob": match.group(2),
            "return_type": (match.group(3).strip() if match.group(3) else None),
            "explicit_parent": None,
            "is_async": False,
        }

    # Ruby method
    if language == "ruby":
        match = re.match(
            r"^\s*def\s+([A-Za-z_][A-Za-z0-9_!?=]*)\s*(?:\(([^)]*)\))?", line
        )
        if match:
            return {
                "name": match.group(1),
                "params_blob": match.group(2) or "",
                "return_type": None,
                "explicit_parent": None,
                "is_async": False,
            }

    # PHP function
    if language == "php":
        match = re.match(
            r"^\s*(?:public|private|protected|static|final|abstract|\s)*function\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(([^)]*)\)",
            line,
        )
        if match:
            return {
                "name": match.group(1),
                "params_blob": match.group(2),
                "return_type": None,
                "explicit_parent": None,
                "is_async": False,
            }

    # Generic C-style declaration
    match = re.match(
        r"^\s*(?:template\s*<[^>]+>\s*)?"
        r"(?:public|private|protected|internal|static|final|abstract|virtual|override|async|inline|extern|constexpr|friend|synchronized|native|sealed|partial|\s)*"
        r"(?:[A-Za-z_][A-Za-z0-9_<>\[\],:*&?\s~]+\s+)?"
        r"([A-Za-z_~][A-Za-z0-9_:~]*)\s*\(([^)]*)\)\s*(?:const\b)?\s*(?:\{|=>|$|;)",
        line,
    )
    if match:
        fn_name = match.group(1).strip()
        if fn_name in FUNCTION_NAME_BLACKLIST:
            return None
        if fn_name.startswith("operator"):
            return None
        explicit_parent = None
        if "::" in fn_name:
            explicit_parent = fn_name.split("::")[-2]
            fn_name = fn_name.split("::")[-1]

        return {
            "name": fn_name,
            "params_blob": match.group(2),
            "return_type": None,
            "explicit_parent": explicit_parent,
            "is_async": "async" in stripped,
        }

    return None


def _detect_generic_classes(lines: List[str]) -> List[Dict[str, Any]]:
    classes: List[Dict[str, Any]] = []
    for line_number, line in enumerate(lines, start=1):
        class_match = RE_CLASS_DECL.match(line)
        if class_match:
            class_name = class_match.group(1)
            tail = class_match.group(2) or ""
            classes.append(
                {
                    "class_name": class_name,
                    "start_line": line_number,
                    "end_line": line_number,
                    "inherits": _extract_parent_types(tail),
                    "methods": [],
                    "attributes": [],
                }
            )
            continue

        type_match = RE_TYPE_DECL.match(line)
        if type_match:
            class_name = type_match.group(1)
            tail = type_match.group(2) or ""
            classes.append(
                {
                    "class_name": class_name,
                    "start_line": line_number,
                    "end_line": line_number,
                    "inherits": _extract_parent_types(tail),
                    "methods": [],
                    "attributes": [],
                }
            )

    return classes


def _line_in_range(line_number: int, ranges: Iterable[Tuple[int, int]]) -> bool:
    return any(start <= line_number <= end for start, end in ranges)


def parse_generic_module(
    project_root: str,
    abs_path: str,
    file_id: str,
    module_name: str,
    language: str,
    src: str,
) -> Dict[str, Any]:
    lines = src.splitlines()
    file_rel = rel_path_from(project_root, abs_path)

    module_entry: Dict[str, Any] = {
        "module": module_name,
        "language": language,
        "file_path": file_rel,
        "file_id": file_id,
        "imports": _extract_imports_generic(lines, language),
        "globals": [],
        "classes": [],
        "functions": [],
        "calls": [],
    }

    class_entries = _detect_generic_classes(lines)
    for cls in class_entries:
        class_name = cls["class_name"]
        class_qname = f"{module_name}.{class_name}"
        cls["qualified_name"] = class_qname
        cls["class_id"] = "class_" + sha1_short(class_qname)
        if language in BRACE_LANGUAGES:
            _start, end_line = _find_brace_block(lines, cls["start_line"] - 1)
            cls["end_line"] = end_line
        module_entry["classes"].append(
            {
                "class_id": cls["class_id"],
                "class_name": class_name,
                "qualified_name": class_qname,
                "inherits": cls.get("inherits", []),
                "decorators": [],
                "start_line": cls["start_line"],
                "end_line": cls["end_line"],
                "attributes": [],
                "methods": [],
            }
        )

    module_classes_by_name = {
        cls["class_name"]: cls for cls in module_entry["classes"]  # type: ignore[index]
    }

    used_qnames = set()
    function_entries_by_qname: Dict[str, Dict[str, Any]] = {}
    function_spans: List[Tuple[int, int, str]] = []
    function_ranges: List[Tuple[int, int]] = []
    class_ranges: List[Tuple[int, int, str]] = [
        (cls["start_line"], cls["end_line"], cls["class_name"]) for cls in class_entries
    ]

    def unique_qname(base_qname: str) -> str:
        if base_qname not in used_qnames:
            used_qnames.add(base_qname)
            return base_qname
        idx = 2
        while f"{base_qname}#{idx}" in used_qnames:
            idx += 1
        qname = f"{base_qname}#{idx}"
        used_qnames.add(qname)
        return qname

    def ensure_class(class_name: str, line_number: int) -> Dict[str, Any]:
        if class_name in module_classes_by_name:
            return module_classes_by_name[class_name]
        class_qname = f"{module_name}.{class_name}"
        class_entry = {
            "class_id": "class_" + sha1_short(class_qname),
            "class_name": class_name,
            "qualified_name": class_qname,
            "inherits": [],
            "decorators": [],
            "start_line": line_number,
            "end_line": line_number,
            "attributes": [],
            "methods": [],
        }
        module_entry["classes"].append(class_entry)
        module_classes_by_name[class_name] = class_entry
        class_ranges.append((line_number, line_number, class_name))
        return class_entry

    for line_number, line in enumerate(lines, start=1):
        fn = _detect_generic_function(line, language)
        if not fn:
            continue
        function_name = fn["name"]
        if function_name in FUNCTION_NAME_BLACKLIST:
            continue

        start_line = line_number
        end_line = line_number
        if language in BRACE_LANGUAGES:
            _start, end_line = _find_brace_block(lines, line_number - 1)

        parent_class = fn.get("explicit_parent")
        if not parent_class:
            containing_classes = [
                (start, end, class_name)
                for start, end, class_name in class_ranges
                if start <= line_number <= end
            ]
            if containing_classes:
                containing_classes.sort(key=lambda item: (item[1] - item[0], item[0]))
                parent_class = containing_classes[0][2]

        if parent_class:
            class_entry = ensure_class(parent_class, line_number)
            base_qname = f"{module_name}.{parent_class}.{function_name}"
            is_method = True
        else:
            class_entry = None
            base_qname = f"{module_name}.{function_name}"
            is_method = False

        qname = unique_qname(base_qname)
        function_entry = {
            "function_id": "func_" + sha1_short(qname),
            "function_name": function_name,
            "qualified_name": qname,
            "parameters": _extract_parameters(fn.get("params_blob", "")),
            "return_type": fn.get("return_type"),
            "decorators": [],
            "is_method": is_method,
            "is_async": fn.get("is_async", False),
            "start_line": start_line,
            "end_line": end_line,
            "calls": [],
        }

        if is_method and class_entry is not None:
            class_entry["methods"].append(function_entry)
        else:
            module_entry["functions"].append(function_entry)

        function_entries_by_qname[qname] = function_entry
        function_spans.append((start_line, end_line, qname))
        function_ranges.append((start_line, end_line))

    # globals heuristics (top-level assignment-like lines)
    for line_number, line in enumerate(lines, start=1):
        if _line_in_range(line_number, function_ranges):
            continue
        match = RE_GENERIC_ASSIGN.match(line)
        if not match:
            continue
        variable = match.group(1)
        value = match.group(2).strip()
        if variable in {"if", "for", "while", "switch"}:
            continue
        module_entry["globals"].append(
            {"name": variable, "value": value, "line_number": line_number}
        )

    declaration_lines = {
        cls["start_line"] for cls in module_entry["classes"]
    } | {fn["start_line"] for fn in module_entry["functions"]}
    for cls in module_entry["classes"]:
        for method in cls.get("methods", []):
            declaration_lines.add(method["start_line"])

    # assign calls to the innermost containing function span; otherwise module-level.
    sorted_spans = sorted(function_spans, key=lambda item: (item[1] - item[0], item[0]))
    for line_number, line in enumerate(lines, start=1):
        if line_number in declaration_lines:
            continue
        stripped = line.strip()
        if (
            not stripped
            or stripped.startswith("//")
            or stripped.startswith("#")
            or stripped.startswith("*")
        ):
            continue

        for match in RE_CALL_CANDIDATE.finditer(line):
            callee = match.group(1)
            short = callee.split(".")[-1].split("::")[-1]
            if short in CALL_KEYWORDS:
                continue
            if callee in {"super", "this", "self"}:
                continue

            caller_qname = module_name
            for start, end, function_qname in sorted_spans:
                if start <= line_number <= end:
                    caller_qname = function_qname
                    break

            call_key = f"{caller_qname}:{file_rel}:{line_number}:{match.start()}:{callee}"
            call_obj = _build_call_object(
                caller_qname=caller_qname,
                callee_name=callee,
                caller_file=file_rel,
                line_number=line_number,
                call_key=call_key,
            )
            if caller_qname == module_name:
                module_entry["calls"].append(call_obj)
            else:
                function_entry = function_entries_by_qname.get(caller_qname)
                if function_entry is not None:
                    function_entry["calls"].append(call_obj)
                else:
                    module_entry["calls"].append(call_obj)

    return module_entry


def parse_source_files(
    project_root: str, file_meta_map: Dict[str, Dict[str, str]]
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, str]]]:
    modules: Dict[str, Any] = {}
    files_output: List[Dict[str, Any]] = []
    parse_warnings: List[Dict[str, str]] = []

    for abs_path in sorted(file_meta_map.keys()):
        file_meta = file_meta_map[abs_path]
        file_id = file_meta["file_id"]
        language = file_meta["language"]
        source_text = _read_text_file(abs_path)
        relative_path = rel_path_from(project_root, abs_path)

        base_module_name = module_name_from_path(project_root, abs_path)
        module_name = _unique_module_name(modules, base_module_name, relative_path)

        try:
            if language == "python":
                module_entry = parse_python_module(
                    project_root=project_root,
                    abs_path=abs_path,
                    file_id=file_id,
                    module_name=module_name,
                    src=source_text,
                )
            else:
                module_entry = parse_generic_module(
                    project_root=project_root,
                    abs_path=abs_path,
                    file_id=file_id,
                    module_name=module_name,
                    language=language,
                    src=source_text,
                )
        except Exception as exc:
            logger.warning("Failed to parse %s: %s", relative_path, exc)
            parse_warnings.append({"file": relative_path, "error": str(exc)})
            module_entry = {
                "module": module_name,
                "language": language,
                "file_path": relative_path,
                "file_id": file_id,
                "imports": [],
                "globals": [],
                "classes": [],
                "functions": [],
                "calls": [],
                "parse_error": str(exc),
            }

        logger.debug(
            "Parsed %s (%s): %d classes, %d functions",
            relative_path,
            language,
            len(module_entry.get("classes", [])),
            len(module_entry.get("functions", [])),
        )
        modules[module_name] = module_entry
        files_output.append(
            {
                "file_id": file_id,
                "file_path": relative_path,
                "module": module_name,
                "language": language,
                "parsed": module_entry,
            }
        )

    return modules, files_output, parse_warnings


def build_symbol_table(modules: Dict[str, Any]) -> Dict[str, Any]:
    table: Dict[str, Any] = {}
    for module_name, module_obj in modules.items():
        table[module_name] = {
            "symbol_type": "module",
            "file_path": module_obj["file_path"],
            "entity_id": module_obj["file_id"],
            "line_number": 1,
            "language": module_obj.get("language"),
        }
        for cls in module_obj.get("classes", []):
            table[cls["qualified_name"]] = {
                "symbol_type": "class",
                "file_path": module_obj["file_path"],
                "entity_id": cls["class_id"],
                "line_number": cls.get("start_line"),
                "language": module_obj.get("language"),
            }
            for method in cls.get("methods", []):
                table[method["qualified_name"]] = {
                    "symbol_type": "function",
                    "file_path": module_obj["file_path"],
                    "entity_id": method["function_id"],
                    "line_number": method.get("start_line"),
                    "language": module_obj.get("language"),
                }

        for fn in module_obj.get("functions", []):
            table[fn["qualified_name"]] = {
                "symbol_type": "function",
                "file_path": module_obj["file_path"],
                "entity_id": fn["function_id"],
                "line_number": fn.get("start_line"),
                "language": module_obj.get("language"),
            }
    return table


def _short_symbol_name(qualified_name: str) -> str:
    tail = qualified_name.split(".")[-1]
    return tail.split("#")[0]


def try_resolve_call(
    call: Dict[str, Any],
    caller_qname: str,
    symbol_table: Dict[str, Any],
    name_index: Dict[str, List[str]],
    import_index: Optional[Dict[str, set]] = None,
) -> None:
    candidate = (call.get("callee_name") or "").strip()
    if not candidate:
        return

    candidate = candidate.replace("::", ".")
    if candidate in symbol_table:
        call["callee_qualified_name"] = candidate
        call["is_resolved"] = True
        call["callee_file"] = symbol_table[candidate]["file_path"]
        return

    short = candidate.split(".")[-1]
    candidates = name_index.get(short, [])
    if not candidates:
        return

    if len(candidates) == 1:
        resolved = candidates[0]
        call["callee_qualified_name"] = resolved
        call["is_resolved"] = True
        call["callee_file"] = symbol_table[resolved]["file_path"]
        return

    # Same package
    caller_package = ".".join(caller_qname.split(".")[:-1])
    for resolved in candidates:
        if caller_package and resolved.startswith(caller_package + "."):
            call["callee_qualified_name"] = resolved
            call["is_resolved"] = True
            call["callee_file"] = symbol_table[resolved]["file_path"]
            return

    # Same top-level package
    caller_top = caller_qname.split(".")[0]
    for resolved in candidates:
        if resolved.startswith(caller_top + "."):
            call["callee_qualified_name"] = resolved
            call["is_resolved"] = True
            call["callee_file"] = symbol_table[resolved]["file_path"]
            return

    # Import-aware: prefer candidates whose module was explicitly imported
    if import_index:
        parts = caller_qname.split(".")
        caller_imports: set = set()
        for i in range(len(parts), 0, -1):
            parent = ".".join(parts[:i])
            if parent in import_index:
                caller_imports = import_index[parent]
                break
        if caller_imports:
            for resolved in sorted(candidates):
                resolved_module = ".".join(resolved.split(".")[:-1])
                resolved_root = resolved.split(".")[0]
                if resolved_module in caller_imports or resolved_root in caller_imports:
                    call["callee_qualified_name"] = resolved
                    call["is_resolved"] = True
                    call["callee_file"] = symbol_table[resolved]["file_path"]
                    return

    # Alphabetical fallback
    resolved = sorted(candidates)[0]
    call["callee_qualified_name"] = resolved
    call["is_resolved"] = True
    call["callee_file"] = symbol_table[resolved]["file_path"]


def resolve_calls(modules: Dict[str, Any], symbol_table: Dict[str, Any]) -> None:
    name_index: Dict[str, List[str]] = defaultdict(list)
    for qname in symbol_table:
        name_index[_short_symbol_name(qname)].append(qname)

    # Build import index: module_name -> set of imported module names
    import_index: Dict[str, set] = {}
    for module_name, module_obj in modules.items():
        imported: set = set()
        for imp in module_obj.get("imports", []):
            target = imp.get("module") or imp.get("name")
            if target:
                imported.add(target)
                imported.add(target.split(".")[0])
        import_index[module_name] = imported

    for module_name, module_obj in modules.items():
        for call in module_obj.get("calls", []):
            try_resolve_call(call, module_name, symbol_table, name_index, import_index)
        for fn in module_obj.get("functions", []):
            for call in fn.get("calls", []):
                try_resolve_call(call, fn["qualified_name"], symbol_table, name_index, import_index)
        for cls in module_obj.get("classes", []):
            for method in cls.get("methods", []):
                for call in method.get("calls", []):
                    try_resolve_call(
                        call, method["qualified_name"], symbol_table, name_index, import_index
                    )


def populate_called_by(modules: Dict[str, Any]) -> Dict[str, List[str]]:
    called_by: Dict[str, List[str]] = defaultdict(list)

    def add(call_obj: Dict[str, Any]) -> None:
        callee = call_obj.get("callee_qualified_name")
        caller = call_obj.get("caller_qualified_name")
        if not callee or not caller or not call_obj.get("is_resolved"):
            return
        if caller not in called_by[callee]:
            called_by[callee].append(caller)

    for module_obj in modules.values():
        for call in module_obj.get("calls", []):
            add(call)
        for fn in module_obj.get("functions", []):
            for call in fn.get("calls", []):
                add(call)
        for cls in module_obj.get("classes", []):
            for method in cls.get("methods", []):
                for call in method.get("calls", []):
                    add(call)

    return dict(called_by)


def _normalize_dependency_target(raw_target: str) -> str:
    target = raw_target.strip().strip("\"'").strip()
    target = target.replace("\\", ".").replace("/", ".").replace("::", ".")
    if target.endswith(".*"):
        target = target[:-2]
    target = target.strip(".")
    for ext in sorted(SUPPORTED_EXTENSIONS, key=len, reverse=True):
        if target.lower().endswith(ext):
            target = target[: -len(ext)]
            break
    return target.strip(".")


def _dependency_candidates(
    module_name: str,
    import_target: str,
    level: int,
) -> List[str]:
    normalized = _normalize_dependency_target(import_target)
    candidates: List[str] = []
    if normalized:
        candidates.append(normalized)
        parts = normalized.split(".")
        for i in range(len(parts) - 1, 0, -1):
            candidates.append(".".join(parts[:i]))

    if level > 0:
        base_parts = module_name.split(".")
        anchor = base_parts[:-1]
        if level > 1:
            trim = min(level - 1, len(anchor))
            anchor = anchor[: len(anchor) - trim]
        if normalized:
            candidates.insert(
                0, ".".join(anchor + [normalized]) if anchor else normalized
            )
        if anchor:
            candidates.insert(0, ".".join(anchor))

    deduped: List[str] = []
    seen = set()
    for candidate in candidates:
        cleaned = candidate.strip(".")
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        deduped.append(cleaned)
    return deduped


def build_graphs(
    modules: Dict[str, Any], symbol_table: Dict[str, Any]
) -> Tuple[Dict[str, List[str]], Dict[str, List[str]], Dict[str, List[str]]]:
    call_graph_set: Dict[str, set] = defaultdict(set)
    dependency_graph: Dict[str, List[str]] = {}
    inheritance_graph: Dict[str, List[str]] = {}

    for module_obj in modules.values():
        for call in module_obj.get("calls", []):
            if call.get("is_resolved") and call.get("callee_qualified_name"):
                call_graph_set[call["caller_qualified_name"]].add(
                    call["callee_qualified_name"]
                )
        for fn in module_obj.get("functions", []):
            for call in fn.get("calls", []):
                if call.get("is_resolved") and call.get("callee_qualified_name"):
                    call_graph_set[call["caller_qualified_name"]].add(
                        call["callee_qualified_name"]
                    )
        for cls in module_obj.get("classes", []):
            for method in cls.get("methods", []):
                for call in method.get("calls", []):
                    if call.get("is_resolved") and call.get("callee_qualified_name"):
                        call_graph_set[call["caller_qualified_name"]].add(
                            call["callee_qualified_name"]
                        )

    local_modules = set(modules.keys())
    for module_name, module_obj in modules.items():
        deps = set()
        for imp in module_obj.get("imports", []):
            import_target = imp.get("module") or imp.get("name")
            if not import_target:
                continue
            level = int(imp.get("level", 0) or 0)
            for candidate in _dependency_candidates(module_name, import_target, level):
                if candidate in local_modules:
                    deps.add(candidate)
                    break
        dependency_graph[module_name] = sorted(deps)

    for module_obj in modules.values():
        for cls in module_obj.get("classes", []):
            parents: List[str] = []
            for parent in cls.get("inherits", []):
                if parent in symbol_table:
                    parents.append(parent)
                    continue
                short = _clean_type_name(parent)
                if not short:
                    continue
                matches = [
                    qname
                    for qname in symbol_table
                    if qname.split(".")[-1].split("#")[0] == short
                ]
                if matches:
                    parents.append(sorted(matches)[0])
                else:
                    parents.append(short)
            inheritance_graph[cls["qualified_name"]] = parents

    call_graph = {caller: sorted(callees) for caller, callees in call_graph_set.items()}
    return call_graph, dependency_graph, inheritance_graph


def _count_total_calls(modules: Dict[str, Any]) -> int:
    total = 0
    for module_obj in modules.values():
        total += len(module_obj.get("calls", []))
        for fn in module_obj.get("functions", []):
            total += len(fn.get("calls", []))
        for cls in module_obj.get("classes", []):
            for method in cls.get("methods", []):
                total += len(method.get("calls", []))
    return total


def export_outputs(
    out_dir: str,
    project_root: str,
    modules: Dict[str, Any],
    files_output: List[Dict[str, Any]],
    folder_tree: Dict[str, Any],
    symbol_table: Dict[str, Any],
    call_graph: Dict[str, Any],
    dependency_graph: Dict[str, Any],
    inheritance_graph: Dict[str, Any],
    parse_warnings: Optional[List[Dict[str, str]]] = None,
) -> None:
    os.makedirs(out_dir, exist_ok=True)
    files_dir = os.path.join(out_dir, "files")
    graphs_dir = os.path.join(out_dir, "graphs")
    os.makedirs(files_dir, exist_ok=True)
    os.makedirs(graphs_dir, exist_ok=True)

    project_id = "proj_" + sha1_short(os.path.abspath(project_root))
    total_files = len(modules)
    total_classes = sum(len(m.get("classes", [])) for m in modules.values())
    total_functions = sum(
        len(m.get("functions", []))
        + sum(len(c.get("methods", [])) for c in m.get("classes", []))
        for m in modules.values()
    )
    total_calls = _count_total_calls(modules)
    language_counts = Counter(m.get("language", "unknown") for m in modules.values())
    languages_detected = sorted(language_counts.keys())
    primary_language = (
        max(language_counts.items(), key=lambda item: (item[1], item[0]))[0]
        if language_counts
        else "unknown"
    )

    metadata = {
        "project_id": project_id,
        "python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "runtime_python_version": f"{sys.version_info.major}.{sys.version_info.minor}",
        "schema_version": "2.0",
        "total_files": total_files,
        "total_source_files": total_files,
        "total_classes": total_classes,
        "total_functions": total_functions,
        "total_calls": total_calls,
        "languages_detected": languages_detected,
        "language_file_counts": dict(sorted(language_counts.items())),
        "primary_language": primary_language,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "warnings": parse_warnings or [],
    }

    with open(os.path.join(out_dir, "project_metadata.json"), "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2)
    with open(os.path.join(out_dir, "folder_tree.json"), "w", encoding="utf-8") as f:
        json.dump(folder_tree, f, indent=2)
    with open(os.path.join(out_dir, "symbol_table.json"), "w", encoding="utf-8") as f:
        json.dump(symbol_table, f, indent=2)
    with open(os.path.join(out_dir, "modules.json"), "w", encoding="utf-8") as f:
        json.dump(modules, f, indent=2)

    file_index = []
    for file_obj in files_output:
        file_index.append(
            {
                "file_id": file_obj["file_id"],
                "file_path": file_obj["file_path"],
                "module": file_obj["module"],
                "language": file_obj.get("language"),
            }
        )
        with open(
            os.path.join(files_dir, f"{file_obj['file_id']}.json"),
            "w",
            encoding="utf-8",
        ) as f:
            json.dump(file_obj, f, indent=2)

    with open(os.path.join(files_dir, "file_index.json"), "w", encoding="utf-8") as f:
        json.dump(file_index, f, indent=2)
    with open(os.path.join(graphs_dir, "call_graph.json"), "w", encoding="utf-8") as f:
        json.dump(call_graph, f, indent=2)
    with open(
        os.path.join(graphs_dir, "dependency_graph.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(dependency_graph, f, indent=2)
    with open(
        os.path.join(graphs_dir, "inheritance_graph.json"), "w", encoding="utf-8"
    ) as f:
        json.dump(inheritance_graph, f, indent=2)


def ingest_zip(zip_path: str, out_dir: str) -> None:
    import time
    start = time.time()
    logger.info("Ingestion started: %s", zip_path)

    with TemporaryDirectory() as temp_dir:
        extract_zip(zip_path, temp_dir)
        project_root = find_project_root(temp_dir)

        folder_tree, file_meta_map, _language_counts = build_folder_tree(project_root)
        logger.info("Found %d source files", len(file_meta_map))

        modules, files_output, parse_warnings = parse_source_files(project_root, file_meta_map)
        symbol_table = build_symbol_table(modules)
        resolve_calls(modules, symbol_table)
        called_by = populate_called_by(modules)
        call_graph, dependency_graph, inheritance_graph = build_graphs(
            modules, symbol_table
        )

        total_calls = sum(
            len(m.get("calls", []))
            + sum(len(fn.get("calls", [])) for fn in m.get("functions", []))
            + sum(
                len(method.get("calls", []))
                for cls in m.get("classes", [])
                for method in cls.get("methods", [])
            )
            for m in modules.values()
        )
        resolved_calls = sum(
            1
            for m in modules.values()
            for call in m.get("calls", [])
            if call.get("is_resolved")
        ) + sum(
            1
            for m in modules.values()
            for fn in m.get("functions", [])
            for call in fn.get("calls", [])
            if call.get("is_resolved")
        ) + sum(
            1
            for m in modules.values()
            for cls in m.get("classes", [])
            for method in cls.get("methods", [])
            for call in method.get("calls", [])
            if call.get("is_resolved")
        )
        logger.info(
            "Resolution: %d/%d calls resolved (%.0f%%)",
            resolved_calls,
            total_calls,
            (resolved_calls / max(1, total_calls)) * 100,
        )

        for qname, callers in called_by.items():
            if qname in symbol_table:
                symbol_table[qname]["called_by"] = callers

        export_outputs(
            out_dir=out_dir,
            project_root=project_root,
            modules=modules,
            files_output=files_output,
            folder_tree=folder_tree,
            symbol_table=symbol_table,
            call_graph=call_graph,
            dependency_graph=dependency_graph,
            inheritance_graph=inheritance_graph,
            parse_warnings=parse_warnings,
        )

    elapsed = time.time() - start
    logger.info("Ingestion complete in %.2fs: %d modules, output in %s", elapsed, len(modules), out_dir)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Ingest a source code ZIP into structured multi-language JSON artifacts."
    )
    parser.add_argument("zipfile", help="Path to project ZIP")
    parser.add_argument("--out", default="ingestion_output", help="Output directory")
    args = parser.parse_args(argv)

    if not os.path.exists(args.zipfile):
        print(f"ZIP file not found: {args.zipfile}")
        return 2

    ingest_zip(args.zipfile, args.out)
    print(f"Ingestion complete. Output in: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
