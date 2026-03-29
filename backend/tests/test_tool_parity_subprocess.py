"""Parity tests for subprocess tools: Python direct vs Python CLI output.

Validates that the Python CLI (``python -m app.code_tools``) returns
the same field names and structure as the direct Python call.  This is
the same CLI that the extension's subprocess fallback calls, so these
tests catch field-name mismatches before they reach the LLM.

Run:
    pytest tests/test_tool_parity_subprocess.py -v
"""
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.code_tools.tools import execute_tool

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
FIXTURE_REPO = REPO_ROOT / "tests" / "fixtures" / "parity_repo"
WS = str(FIXTURE_REPO)
PYTHON = sys.executable


def cli(tool: str, params: dict) -> Dict[str, Any]:
    """Run a tool via ``python -m app.code_tools`` and parse the JSON output."""
    result = subprocess.run(
        [PYTHON, "-m", "app.code_tools", tool, WS, json.dumps(params)],
        capture_output=True, text=True, timeout=30,
        cwd=str(REPO_ROOT / "backend"),
    )
    assert result.returncode == 0 or result.stdout, (
        f"CLI failed: {result.stderr}"
    )
    return json.loads(result.stdout)


def direct(tool: str, params: dict) -> Dict[str, Any]:
    """Run a tool via the direct Python function and return raw data."""
    result = execute_tool(tool, WS, params)
    return {
        "success": result.success,
        "data": result.data,
        "error": result.error,
    }


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


class TestGrepParity:
    def test_field_names(self):
        """grep returns [{file_path, line_number, content}]"""
        r = direct("grep", {"pattern": "OrderService", "max_results": 5})
        assert r["success"]
        assert isinstance(r["data"], list)
        assert len(r["data"]) > 0
        item = r["data"][0]
        assert "file_path" in item
        assert "line_number" in item
        assert "content" in item

    def test_cli_matches_direct(self):
        """CLI returns same structure as direct call."""
        r = cli("grep", {"pattern": "OrderService", "max_results": 5})
        assert r["success"]
        assert isinstance(r["data"], list)
        assert len(r["data"]) > 0
        item = r["data"][0]
        assert "file_path" in item
        assert "line_number" in item
        assert "content" in item

    def test_results_same(self):
        """Direct and CLI return the same matches."""
        d = direct("grep", {"pattern": "class OrderService", "max_results": 10})
        c = cli("grep", {"pattern": "class OrderService", "max_results": 10})
        assert d["success"] and c["success"]
        d_files = {m["file_path"] for m in d["data"]}
        c_files = {m["file_path"] for m in c["data"]}
        assert d_files == c_files

    def test_alternation_pattern(self):
        """Pipe alternation works (the pattern that was broken in TS grep)."""
        r = direct("grep", {"pattern": "OrderService|UserModel", "max_results": 20})
        assert r["success"]
        assert len(r["data"]) > 1

    def test_zero_results_empty_list(self):
        """Zero matches returns empty list, not None or error."""
        r = direct("grep", {"pattern": "zzz_nonexistent_pattern_zzz"})
        assert r["success"]
        assert r["data"] == []

    def test_include_glob(self):
        """include_glob filters to specified extension."""
        r = direct("grep", {"pattern": "import", "include_glob": "*.py", "max_results": 5})
        assert r["success"]
        for m in r["data"]:
            assert m["file_path"].endswith(".py")


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class TestReadFileParity:
    def test_field_names(self):
        """read_file returns {path, total_lines, content}."""
        r = direct("read_file", {"path": "app/service.py"})
        assert r["success"]
        assert "path" in r["data"]
        assert "total_lines" in r["data"]
        assert "content" in r["data"]

    def test_cli_matches(self):
        c = cli("read_file", {"path": "app/service.py"})
        assert c["success"]
        assert "path" in c["data"]
        assert "total_lines" in c["data"]
        assert "content" in c["data"]

    def test_line_range(self):
        """Line range returns subset of content."""
        full = direct("read_file", {"path": "app/service.py"})
        part = direct("read_file", {"path": "app/service.py", "start_line": 1, "end_line": 5})
        assert full["success"] and part["success"]
        assert full["data"]["total_lines"] == part["data"]["total_lines"]
        # Partial read has fewer lines in content
        full_lines = full["data"]["content"].count("\n")
        part_lines = part["data"]["content"].count("\n")
        assert part_lines < full_lines

    def test_nonexistent_file(self):
        r = direct("read_file", {"path": "nonexistent.py"})
        assert not r["success"]
        assert r["error"]


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


class TestListFilesParity:
    def test_field_names(self):
        """list_files returns [{path, is_dir, size}]."""
        r = direct("list_files", {"directory": "app", "max_depth": 1})
        assert r["success"]
        assert isinstance(r["data"], list)
        assert len(r["data"]) > 0
        item = r["data"][0]
        assert "path" in item
        assert "is_dir" in item

    def test_cli_matches(self):
        c = cli("list_files", {"directory": "app", "max_depth": 1})
        assert c["success"]
        assert isinstance(c["data"], list)
        item = c["data"][0]
        assert "path" in item
        assert "is_dir" in item

    def test_nonexistent_dir(self):
        r = direct("list_files", {"directory": "nonexistent_dir"})
        assert not r["success"]


# ---------------------------------------------------------------------------
# find_symbol
# ---------------------------------------------------------------------------


class TestFindSymbolParity:
    def test_field_names(self):
        """find_symbol returns [{name, kind, file_path, start_line, end_line, signature}]."""
        r = direct("find_symbol", {"name": "OrderService"})
        assert r["success"]
        assert isinstance(r["data"], list)
        assert len(r["data"]) > 0
        item = r["data"][0]
        for field in ("name", "kind", "file_path", "start_line", "end_line"):
            assert field in item, f"Missing field: {field}"

    def test_kind_filter(self):
        r = direct("find_symbol", {"name": "OrderService", "kind": "class"})
        assert r["success"]
        for item in r["data"]:
            assert item["kind"] == "class"


# ---------------------------------------------------------------------------
# find_references
# ---------------------------------------------------------------------------


class TestFindReferencesParity:
    def test_field_names(self):
        """find_references returns [{file_path, line_number, content}]."""
        r = direct("find_references", {"symbol_name": "OrderService"})
        assert r["success"]
        assert isinstance(r["data"], list)
        assert len(r["data"]) > 0
        item = r["data"][0]
        assert "file_path" in item
        assert "line_number" in item
        assert "content" in item


# ---------------------------------------------------------------------------
# file_outline
# ---------------------------------------------------------------------------


class TestFileOutlineParity:
    def test_field_names(self):
        """file_outline returns [{name, kind, file_path, start_line, end_line, signature}]."""
        r = direct("file_outline", {"path": "app/service.py"})
        assert r["success"]
        assert isinstance(r["data"], list)
        assert len(r["data"]) > 0
        item = r["data"][0]
        for field in ("name", "kind", "file_path", "start_line", "end_line"):
            assert field in item, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# git_log
# ---------------------------------------------------------------------------


class TestGitLogParity:
    def test_field_names(self):
        """git_log returns [{hash, message, author, date}]."""
        r = direct("git_log", {"n": 3})
        assert r["success"]
        assert isinstance(r["data"], list)
        assert len(r["data"]) > 0
        item = r["data"][0]
        for field in ("hash", "message", "author", "date"):
            assert field in item, f"Missing field: {field}"

    def test_cli_matches(self):
        c = cli("git_log", {"n": 3})
        assert c["success"]
        assert isinstance(c["data"], list)
        item = c["data"][0]
        for field in ("hash", "message", "author", "date"):
            assert field in item, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# git_diff
# ---------------------------------------------------------------------------


class TestGitDiffParity:
    def test_field_names(self):
        """git_diff returns {diff: str}."""
        r = direct("git_diff", {"ref1": "HEAD~1", "ref2": "HEAD"})
        assert r["success"]
        assert isinstance(r["data"], dict)
        assert "diff" in r["data"]
        assert isinstance(r["data"]["diff"], str)


# ---------------------------------------------------------------------------
# git_blame
# ---------------------------------------------------------------------------


class TestGitBlameParity:
    def test_field_names(self):
        """git_blame returns [{commit_hash, author, date, line_number, content}]."""
        r = direct("git_blame", {"file": "app/service.py"})
        assert r["success"]
        assert isinstance(r["data"], list)
        assert len(r["data"]) > 0
        item = r["data"][0]
        for field in ("commit_hash", "author", "date", "line_number", "content"):
            assert field in item, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# git_show
# ---------------------------------------------------------------------------


class TestGitShowParity:
    def test_field_names(self):
        """git_show returns {commit_hash, author, date, message, diff}."""
        r = direct("git_show", {"commit": "HEAD"})
        assert r["success"]
        assert isinstance(r["data"], dict)
        for field in ("commit_hash", "author", "date", "message", "diff"):
            assert field in r["data"], f"Missing field: {field}"


# ---------------------------------------------------------------------------
# git_diff_files
# ---------------------------------------------------------------------------


class TestGitDiffFilesParity:
    def test_field_names(self):
        """git_diff_files returns [{path, status, additions, deletions}]."""
        r = direct("git_diff_files", {"ref": "HEAD~1"})
        assert r["success"]
        assert isinstance(r["data"], list)
        if len(r["data"]) > 0:
            item = r["data"][0]
            for field in ("path", "status", "additions", "deletions"):
                assert field in item, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# find_tests
# ---------------------------------------------------------------------------


class TestFindTestsParity:
    def test_field_names(self):
        """find_tests returns [{test_file, test_function, line_number}]."""
        r = direct("find_tests", {"name": "OrderService"})
        assert r["success"]
        assert isinstance(r["data"], list)
        if len(r["data"]) > 0:
            item = r["data"][0]
            assert "test_file" in item
            assert "test_function" in item
            assert "line_number" in item


# ---------------------------------------------------------------------------
# get_callers / get_callees
# ---------------------------------------------------------------------------


class TestGetCallersParity:
    def test_field_names(self):
        """get_callers returns [{caller_name, caller_kind, file_path, line, content}]."""
        r = direct("get_callers", {"function_name": "find_user"})
        assert r["success"]
        assert isinstance(r["data"], list)
        if len(r["data"]) > 0:
            item = r["data"][0]
            for field in ("caller_name", "caller_kind", "file_path", "line"):
                assert field in item, f"Missing field: {field}"


class TestGetCalleesParity:
    def test_field_names(self):
        """get_callees returns [{callee_name, file_path, line}]."""
        r = direct("get_callees", {"function_name": "process_payment", "file": "app/service.py"})
        if not r["success"] and "networkx" in str(r.get("error", "")):
            pytest.skip("networkx not installed")
        assert r["success"]
        assert isinstance(r["data"], list)
        if len(r["data"]) > 0:
            item = r["data"][0]
            for field in ("callee_name", "file_path", "line"):
                assert field in item, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# trace_variable
# ---------------------------------------------------------------------------


class TestTraceVariableParity:
    def test_field_names(self):
        """trace_variable returns {variable, file, function, direction, ...}."""
        r = direct("trace_variable", {
            "variable_name": "card_token",
            "file": "app/service.py",
            "function_name": "process_payment",
            "direction": "forward",
        })
        if not r["success"] and "networkx" in str(r.get("error", "")):
            pytest.skip("networkx not installed")
        assert r["success"]
        assert isinstance(r["data"], dict)
        for field in ("variable", "file", "function", "direction"):
            assert field in r["data"], f"Missing field: {field}"


# ---------------------------------------------------------------------------
# compressed_view
# ---------------------------------------------------------------------------


class TestCompressedViewParity:
    def test_field_names(self):
        """compressed_view returns {content, path, total_lines, symbol_count}."""
        r = direct("compressed_view", {"file_path": "app/service.py"})
        assert r["success"]
        assert isinstance(r["data"], dict)
        for field in ("content", "path", "total_lines", "symbol_count"):
            assert field in r["data"], f"Missing field: {field}"


# ---------------------------------------------------------------------------
# module_summary
# ---------------------------------------------------------------------------


class TestModuleSummaryParity:
    def test_field_names(self):
        """module_summary returns {content, file_count, loc}."""
        r = direct("module_summary", {"module_path": "app"})
        assert r["success"]
        assert isinstance(r["data"], dict)
        for field in ("content", "file_count", "loc"):
            assert field in r["data"], f"Missing field: {field}"


# ---------------------------------------------------------------------------
# detect_patterns
# ---------------------------------------------------------------------------


class TestDetectPatternsParity:
    def test_field_names(self):
        """detect_patterns returns {summary, total_matches, categories_scanned, files_scanned, matches}."""
        r = direct("detect_patterns", {"path": "app"})
        assert r["success"]
        assert isinstance(r["data"], dict)
        for field in ("summary", "total_matches", "categories_scanned", "files_scanned", "matches"):
            assert field in r["data"], f"Missing field: {field}"


# ---------------------------------------------------------------------------
# test_outline
# ---------------------------------------------------------------------------


class TestTestOutlineParity:
    def test_field_names(self):
        """test_outline returns [{name, kind, line_number, mocks, assertions, fixtures}]."""
        r = direct("test_outline", {"path": "tests/test_service.py"})
        assert r["success"]
        assert isinstance(r["data"], list)
        if len(r["data"]) > 0:
            item = r["data"][0]
            for field in ("name", "kind", "line_number"):
                assert field in item, f"Missing field: {field}"


# ---------------------------------------------------------------------------
# expand_symbol
# ---------------------------------------------------------------------------


class TestExpandSymbolParity:
    def test_field_names(self):
        """expand_symbol returns {symbol_name, kind, file_path, start_line, end_line, signature, source}."""
        r = direct("expand_symbol", {"symbol_name": "OrderService", "file_path": "app/service.py"})
        assert r["success"]
        assert isinstance(r["data"], dict)
        for field in ("symbol_name", "kind", "file_path", "start_line", "end_line", "source"):
            assert field in r["data"], f"Missing field: {field}"
