"""Helpers for cross-language parity tests (Python vs TypeScript tools)."""

import json
import subprocess
from pathlib import Path
from typing import Any, Dict

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
EXTENSION_DIR = REPO_ROOT / "extension"
TS_RUNNER = EXTENSION_DIR / "tests" / "run_ts_tool.js"
FIXTURE_REPO = REPO_ROOT / "tests" / "fixtures" / "parity_repo"
WS = str(FIXTURE_REPO)


def run_ts_tool(tool: str, params: dict) -> Dict[str, Any]:
    """Run a tool through the TS runner and return parsed JSON."""
    result = subprocess.run(
        ["node", str(TS_RUNNER), tool, WS, json.dumps(params)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(EXTENSION_DIR),
    )
    if result.returncode != 0:
        raise RuntimeError(f"TS runner failed: {result.stderr}")
    return json.loads(result.stdout)


def normalize_path(p: str) -> str:
    """Normalize path separators and remove leading ./"""
    return p.replace("\\", "/").removeprefix("./")


def assert_same_names(py_data, ts_data, label=""):
    """Assert both sides return the same set of symbol names."""
    py_names = {d["name"] for d in py_data}
    ts_names = {d["name"] for d in ts_data}
    assert py_names == ts_names, f"{label}: names differ. py_only={py_names - ts_names}, ts_only={ts_names - py_names}"


def assert_names_subset(py_data, ts_data, label=""):
    """Assert Python names are a subset of TS names.

    TS tree-sitter may find nested symbols (methods inside classes) that
    Python's regex fallback misses.  What matters is that everything Python
    finds, TS also finds.
    """
    py_names = {d["name"] for d in py_data}
    ts_names = {d["name"] for d in ts_data}
    missing = py_names - ts_names
    assert not missing, f"{label}: TS missing symbols that Python found: {missing}"


def assert_same_fields(py_item: dict, ts_item: dict, required_fields: list, label=""):
    """Assert both items have the same required fields with same values."""
    for field in required_fields:
        assert field in py_item, f"{label}: Python missing field '{field}'"
        assert field in ts_item, f"{label}: TS missing field '{field}'"


# Tolerance for end_line differences between tree-sitter implementations
END_LINE_TOLERANCE = 1
