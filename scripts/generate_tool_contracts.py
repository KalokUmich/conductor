#!/usr/bin/env python3
"""Generate tool contracts from Python Pydantic models.

Produces:
  - contracts/tool_contracts.json   (JSON Schema per tool)
  - extension/src/services/toolContracts.d.ts  (TypeScript interfaces)

Usage:
  python scripts/generate_tool_contracts.py          # generate files
  python scripts/generate_tool_contracts.py --check   # diff-only (CI)
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

# ---------------------------------------------------------------------------
# Path setup — make sure we can import from the backend package
# ---------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent.parent
BACKEND_DIR = ROOT_DIR / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from app.code_tools.schemas import (  # noqa: E402
    # Param models
    GrepParams,
    ReadFileParams,
    ListFilesParams,
    FindSymbolParams,
    FindReferencesParams,
    FileOutlineParams,
    GetDependenciesParams,
    GetDependentsParams,
    GitLogParams,
    GitDiffParams,
    GitDiffFilesParams,
    AstSearchParams,
    GetCalleesParams,
    GetCallersParams,
    GitBlameParams,
    GitShowParams,
    FindTestsParams,
    TestOutlineParams,
    TraceVariableParams,
    CompressedViewParams,
    ModuleSummaryParams,
    ExpandSymbolParams,
    DetectPatternsParams,
    RunTestParams,
    # Result models
    GrepMatch,
    SymbolLocation,
    ReferenceLocation,
    FileEntry,
    AstMatch,
    CallerInfo,
    CalleeInfo,
    DependencyInfo,
    GitCommit,
    DiffFileEntry,
    BlameEntry,
    TestMatch,
    TestOutlineEntry,
)

# ---------------------------------------------------------------------------
# Output paths
# ---------------------------------------------------------------------------

CONTRACTS_JSON_PATH = ROOT_DIR / "contracts" / "tool_contracts.json"
TS_DECL_PATH = ROOT_DIR / "extension" / "src" / "services" / "toolContracts.d.ts"

# ---------------------------------------------------------------------------
# Tool -> result type mapping
# ---------------------------------------------------------------------------

# Each entry:
#   tool_name -> (output_type, model_or_none, field_overrides_or_none)
#
# output_type is one of: "list", "dict", "string"
# For "list" with a Pydantic model, we derive output_item_fields from the model.
# For "dict", we provide explicit fields.
# For "string", output is raw text.

from pydantic import BaseModel  # noqa: E402


class _DictSpec:
    """Describes a dict-shaped output that doesn't have a dedicated Pydantic model."""

    def __init__(self, fields: Dict[str, str]):
        self.fields = fields  # field_name -> description


TOOL_RESULT_MAP: Dict[str, tuple] = {
    "grep": ("list", GrepMatch),
    "read_file": ("dict", _DictSpec({
        "content": "File contents (string)",
        "total_lines": "Total line count of the file",
        "path": "File path",
        "start_line": "First line returned (if ranged read)",
        "end_line": "Last line returned (if ranged read)",
    })),
    "list_files": ("list", FileEntry),
    "find_symbol": ("list", SymbolLocation),
    "find_references": ("list", ReferenceLocation),
    "file_outline": ("list", SymbolLocation),
    "get_dependencies": ("list", DependencyInfo),
    "get_dependents": ("list", DependencyInfo),
    "git_log": ("list", GitCommit),
    "git_diff": ("string", None),
    "git_diff_files": ("list", DiffFileEntry),
    "ast_search": ("list", AstMatch),
    "get_callees": ("list", CalleeInfo),
    "get_callers": ("list", CallerInfo),
    "git_blame": ("list", BlameEntry),
    "git_show": ("dict", _DictSpec({
        "hash": "Commit hash",
        "message": "Commit message",
        "author": "Author name",
        "date": "Commit date",
        "diff": "Unified diff text",
    })),
    "find_tests": ("list", TestMatch),
    "test_outline": ("list", TestOutlineEntry),
    "trace_variable": ("dict", _DictSpec({
        "forward": "Forward trace results",
        "backward": "Backward trace results",
    })),
    "compressed_view": ("dict", _DictSpec({
        "content": "Compressed file view",
        "path": "File path",
        "total_lines": "Total lines in original file",
        "symbol_count": "Number of symbols found",
    })),
    "module_summary": ("dict", _DictSpec({
        "content": "Module summary text",
        "file_count": "Number of files in the module",
        "loc": "Total lines of code",
    })),
    "expand_symbol": ("dict", _DictSpec({
        "symbol_name": "Name of the symbol",
        "kind": "Symbol kind (function, class, etc.)",
        "file_path": "File containing the symbol",
        "start_line": "First line of the symbol",
        "end_line": "Last line of the symbol",
        "signature": "Symbol signature",
        "source": "Full source code of the symbol",
    })),
    "detect_patterns": ("list", _DictSpec({
        "file_path": "File where pattern was found",
        "line": "Line number",
        "category": "Pattern category",
        "pattern": "Pattern name",
        "snippet": "Code snippet",
    })),
    "run_test": ("dict", _DictSpec({
        "passed": "Whether the test passed",
        "output": "Test runner output",
        "failures": "Failure details (if any)",
    })),
}

# Param model mapping (same order as schemas.py TOOL_PARAM_MODELS)
TOOL_PARAM_MODELS: Dict[str, Type[BaseModel]] = {
    "grep": GrepParams,
    "read_file": ReadFileParams,
    "list_files": ListFilesParams,
    "find_symbol": FindSymbolParams,
    "find_references": FindReferencesParams,
    "file_outline": FileOutlineParams,
    "get_dependencies": GetDependenciesParams,
    "get_dependents": GetDependentsParams,
    "git_log": GitLogParams,
    "git_diff": GitDiffParams,
    "git_diff_files": GitDiffFilesParams,
    "ast_search": AstSearchParams,
    "get_callees": GetCalleesParams,
    "get_callers": GetCallersParams,
    "git_blame": GitBlameParams,
    "git_show": GitShowParams,
    "find_tests": FindTestsParams,
    "test_outline": TestOutlineParams,
    "trace_variable": TraceVariableParams,
    "compressed_view": CompressedViewParams,
    "module_summary": ModuleSummaryParams,
    "expand_symbol": ExpandSymbolParams,
    "detect_patterns": DetectPatternsParams,
    "run_test": RunTestParams,
}

# All Pydantic result models (used for TS interface generation)
RESULT_MODELS: List[Type[BaseModel]] = [
    GrepMatch,
    SymbolLocation,
    ReferenceLocation,
    FileEntry,
    AstMatch,
    CallerInfo,
    CalleeInfo,
    DependencyInfo,
    GitCommit,
    DiffFileEntry,
    BlameEntry,
    TestMatch,
    TestOutlineEntry,
]

# ---------------------------------------------------------------------------
# JSON Schema generation
# ---------------------------------------------------------------------------


def _output_item_fields(spec) -> Optional[List[str]]:
    """Extract field names from a Pydantic model or _DictSpec."""
    if isinstance(spec, type) and issubclass(spec, BaseModel):
        return list(spec.model_fields.keys())
    if isinstance(spec, _DictSpec):
        return list(spec.fields.keys())
    return None


def _output_item_schema(spec) -> Optional[Dict[str, Any]]:
    """Get JSON schema for the output item if it's a Pydantic model."""
    if isinstance(spec, type) and issubclass(spec, BaseModel):
        return spec.model_json_schema()
    return None


def generate_contracts_json() -> str:
    """Generate the contracts/tool_contracts.json content."""
    tools: Dict[str, Any] = {}

    for tool_name, param_model in TOOL_PARAM_MODELS.items():
        output_type, result_spec = TOOL_RESULT_MAP[tool_name]
        entry: Dict[str, Any] = {
            "input_schema": param_model.model_json_schema(),
            "output_type": output_type,
        }

        fields = _output_item_fields(result_spec)
        if fields is not None:
            entry["output_item_fields"] = fields

        item_schema = _output_item_schema(result_spec)
        if item_schema is not None:
            entry["output_item_schema"] = item_schema

        tools[tool_name] = entry

    contract = {
        "version": "1.0",
        "generated_from": "backend/app/code_tools/schemas.py",
        "tools": tools,
    }

    return json.dumps(contract, indent=2) + "\n"


# ---------------------------------------------------------------------------
# TypeScript declaration generation
# ---------------------------------------------------------------------------

# Pydantic/JSON-Schema type -> TypeScript type
_PY_TO_TS: Dict[str, str] = {
    "string": "string",
    "integer": "number",
    "number": "number",
    "boolean": "boolean",
}


def _ts_type_for_field(field_name: str, field_info) -> str:
    """Derive a TypeScript type string from a Pydantic FieldInfo."""
    annotation = field_info.annotation

    # Handle Optional[X] — unwrap to X
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())

    # typing.Optional[X] is Union[X, NoneType]
    if origin is type(None):
        return "null"

    # Check for Optional (Union[X, None])
    is_optional = False
    inner = annotation
    if _is_union(origin):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1 and type(None) in args:
            is_optional = True
            inner = non_none[0]

    ts = _resolve_ts_type(inner)
    return ts


def _is_union(origin) -> bool:
    """Check if an origin type is a Union."""
    import typing
    if origin is getattr(typing, "Union", None):
        return True
    # Python 3.10+ types.UnionType
    try:
        import types as _types
        if origin is _types.UnionType:
            return True
    except AttributeError:
        pass
    return False


def _resolve_ts_type(annotation) -> str:
    """Resolve a Python type annotation to a TypeScript type string."""
    # Primitives
    if annotation is str:
        return "string"
    if annotation is int or annotation is float:
        return "number"
    if annotation is bool:
        return "boolean"

    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())

    # List[X]
    if origin is list:
        if args:
            inner_ts = _resolve_ts_type(args[0])
            return f"{inner_ts}[]"
        return "any[]"

    # Dict[K, V]
    if origin is dict:
        if len(args) == 2:
            k_ts = _resolve_ts_type(args[0])
            v_ts = _resolve_ts_type(args[1])
            return f"Record<{k_ts}, {v_ts}>"
        return "Record<string, any>"

    # Optional[X] / Union[X, None] at this level
    if _is_union(origin):
        non_none = [a for a in args if a is not type(None)]
        if len(non_none) == 1:
            return _resolve_ts_type(non_none[0])
        parts = [_resolve_ts_type(a) for a in non_none]
        return " | ".join(parts)

    # Fallback
    return "any"


def _is_optional_field(field_info) -> bool:
    """Check if a Pydantic field is Optional (allows None)."""
    annotation = field_info.annotation
    origin = getattr(annotation, "__origin__", None)
    args = getattr(annotation, "__args__", ())
    if _is_union(origin) and type(None) in args:
        return True
    return False


def _generate_interface(model: Type[BaseModel], extra_fields: Optional[Dict[str, str]] = None) -> str:
    """Generate a TypeScript interface from a Pydantic model."""
    lines = [f"export interface {model.__name__} {{"]
    for name, field_info in model.model_fields.items():
        ts_type = _ts_type_for_field(name, field_info)
        optional = "?" if _is_optional_field(field_info) else ""
        # Fields with defaults that aren't required — mark optional
        if not field_info.is_required() and not optional:
            optional = "?"
        lines.append(f"    {name}{optional}: {ts_type};")
    if extra_fields:
        for name, ts_type in extra_fields.items():
            lines.append(f"    {name}: {ts_type};")
    lines.append("}")
    return "\n".join(lines)


def _generate_param_interface(model: Type[BaseModel]) -> str:
    """Generate a TypeScript interface for a tool param model."""
    return _generate_interface(model)


def _generate_dict_output_interface(tool_name: str, spec: _DictSpec) -> str:
    """Generate a TypeScript interface for a dict-shaped tool output."""
    # Convert tool_name to PascalCase + "Result"
    pascal = "".join(part.capitalize() for part in tool_name.split("_")) + "Result"
    lines = [f"export interface {pascal} {{"]
    for field_name, description in spec.fields.items():
        # Infer TS type from description heuristics
        ts_type = _infer_ts_type_from_name(field_name)
        lines.append(f"    {field_name}: {ts_type};")
    lines.append("}")
    return "\n".join(lines)


def _infer_ts_type_from_name(name: str) -> str:
    """Best-effort type inference for dict output fields."""
    if name in ("passed",):
        return "boolean"
    if name in ("total_lines", "line", "start_line", "end_line", "symbol_count", "file_count", "loc", "line_number"):
        return "number"
    if name in ("forward", "backward", "failures"):
        return "any"
    return "string"


def generate_ts_declarations() -> str:
    """Generate the TypeScript declaration file content."""
    sections: List[str] = []

    sections.append(
        "// Auto-generated from Python Pydantic models. Do not edit manually.\n"
        "// Regenerate with: python scripts/generate_tool_contracts.py\n"
    )

    # --- Result model interfaces ---
    sections.append("// ---- Result models ----\n")
    for model in RESULT_MODELS:
        sections.append(_generate_interface(model))
        sections.append("")

    # --- Dict-shaped output interfaces ---
    sections.append("// ---- Dict-shaped tool outputs ----\n")
    for tool_name, (output_type, spec) in TOOL_RESULT_MAP.items():
        if isinstance(spec, _DictSpec) and output_type == "dict":
            sections.append(_generate_dict_output_interface(tool_name, spec))
            sections.append("")
        elif isinstance(spec, _DictSpec) and output_type == "list":
            # detect_patterns: list of dicts
            pascal = "".join(part.capitalize() for part in tool_name.split("_")) + "Item"
            lines = [f"export interface {pascal} {{"]
            for field_name in spec.fields:
                ts_type = _infer_ts_type_from_name(field_name)
                lines.append(f"    {field_name}: {ts_type};")
            lines.append("}")
            sections.append("\n".join(lines))
            sections.append("")

    # --- Param model interfaces ---
    sections.append("// ---- Param models ----\n")
    for _tool_name, model in TOOL_PARAM_MODELS.items():
        sections.append(_generate_param_interface(model))
        sections.append("")

    # --- Tool output type map ---
    sections.append("// ---- Tool output type map ----\n")
    lines = ["export interface ToolOutputMap {"]
    for tool_name, (output_type, spec) in TOOL_RESULT_MAP.items():
        ts_result = _tool_result_ts_type(tool_name, output_type, spec)
        lines.append(f"    {tool_name}: {ts_result};")
    lines.append("}")
    sections.append("\n".join(lines))
    sections.append("")

    return "\n".join(sections)


def _tool_result_ts_type(tool_name: str, output_type: str, spec) -> str:
    """Return the TypeScript type for a tool's output."""
    if output_type == "string":
        return "string"
    if output_type == "dict":
        if isinstance(spec, _DictSpec):
            pascal = "".join(part.capitalize() for part in tool_name.split("_")) + "Result"
            return pascal
        return "any"
    # output_type == "list"
    if isinstance(spec, type) and issubclass(spec, BaseModel):
        return f"{spec.__name__}[]"
    if isinstance(spec, _DictSpec):
        pascal = "".join(part.capitalize() for part in tool_name.split("_")) + "Item"
        return f"{pascal}[]"
    return "any[]"


# ---------------------------------------------------------------------------
# Check mode: diff against committed files
# ---------------------------------------------------------------------------


def _diff_content(path: Path, new_content: str) -> Optional[str]:
    """Return a unified diff if the file differs, or None if identical."""
    if not path.exists():
        return f"File does not exist: {path}\n"
    existing = path.read_text()
    if existing == new_content:
        return None
    diff_lines = difflib.unified_diff(
        existing.splitlines(keepends=True),
        new_content.splitlines(keepends=True),
        fromfile=f"a/{path.relative_to(ROOT_DIR)}",
        tofile=f"b/{path.relative_to(ROOT_DIR)}",
    )
    return "".join(diff_lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate tool contracts from Pydantic models.")
    parser.add_argument(
        "--check",
        action="store_true",
        help="Check that generated files are up to date (exit 1 if stale).",
    )
    args = parser.parse_args()

    json_content = generate_contracts_json()
    ts_content = generate_ts_declarations()

    if args.check:
        stale = False
        for path, content, label in [
            (CONTRACTS_JSON_PATH, json_content, "contracts/tool_contracts.json"),
            (TS_DECL_PATH, ts_content, "extension/src/services/toolContracts.d.ts"),
        ]:
            diff = _diff_content(path, content)
            if diff is not None:
                stale = True
                print(f"STALE: {label}")
                print(diff)
        if stale:
            print("\nTool contracts are out of date. Run:")
            print("  python scripts/generate_tool_contracts.py")
            sys.exit(1)
        else:
            print("Tool contracts are up to date.")
            sys.exit(0)

    # --- Write files ---
    CONTRACTS_JSON_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONTRACTS_JSON_PATH.write_text(json_content)
    print(f"Wrote {CONTRACTS_JSON_PATH.relative_to(ROOT_DIR)}")

    TS_DECL_PATH.parent.mkdir(parents=True, exist_ok=True)
    TS_DECL_PATH.write_text(ts_content)
    print(f"Wrote {TS_DECL_PATH.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
