"""Parity tests: verify Python backend tools work correctly against the conductor workspace.

These tests run each tool against the conductor project itself to ensure
the tools produce valid, meaningful output. The same inputs are used
by the TypeScript extension in local mode — if these pass, the extension's
grep/LSP fallback implementations should produce equivalent results.
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.code_tools.tools import execute_tool

# Use the conductor project as the test workspace
WORKSPACE = str(Path(__file__).parent.parent.parent.resolve())


def _run(tool: str, **params) -> Dict[str, Any]:
    result = execute_tool(tool, WORKSPACE, params)
    return {"success": result.success, "data": result.data, "error": result.error}


class TestFileOperations:
    def test_read_file(self):
        r = _run("read_file", path="backend/app/config.py", start_line=1, end_line=10)
        assert r["success"]
        assert "content" in r["data"]
        assert r["data"]["total_lines"] > 100

    def test_list_files(self):
        r = _run("list_files", directory="backend/app/db", max_depth=1)
        assert r["success"]
        # list_files returns a list of dicts
        data = r["data"]
        files = (
            [f["path"] if isinstance(f, dict) else f for f in data] if isinstance(data, list) else data.get("files", [])
        )
        assert any("engine.py" in str(f) for f in files)

    def test_grep(self):
        r = _run("grep", pattern="class ToolResult", max_results=10)
        assert r["success"]
        data = r["data"]
        # grep returns a list of match dicts
        assert isinstance(data, list)
        assert len(data) >= 1


class TestSymbolTools:
    def test_find_symbol(self):
        r = _run("find_symbol", name="execute_tool")
        assert r["success"]

    def test_file_outline(self):
        r = _run("file_outline", path="backend/app/code_tools/executor.py")
        assert r["success"]
        data = r["data"]
        symbols = data if isinstance(data, list) else data.get("symbols", [])
        names = [str(s) for s in symbols]
        joined = " ".join(names)
        assert "ToolExecutor" in joined or "Executor" in joined

    def test_find_references(self):
        r = _run("find_references", symbol_name="execute_tool", file="backend/app/code_tools/tools.py")
        assert r["success"]

    def test_compressed_view(self):
        r = _run("compressed_view", file_path="backend/app/code_tools/executor.py")
        assert r["success"]
        data = r["data"]
        text = str(data)
        assert "ToolExecutor" in text

    def test_module_summary(self):
        r = _run("module_summary", module_path="backend/app/code_tools")
        assert r["success"]

    def test_expand_symbol(self):
        r = _run("expand_symbol", symbol_name="LocalToolExecutor", file_path="backend/app/code_tools/executor.py")
        assert r["success"]
        text = str(r["data"])
        assert "LocalToolExecutor" in text


class TestGitTools:
    def test_git_log(self):
        r = _run("git_log", n=5)
        assert r["success"]
        data = r["data"]
        text = str(data)
        assert len(text) > 10  # has some content

    def test_git_diff(self):
        r = _run("git_diff", ref1="HEAD~1", ref2="HEAD")
        assert r["success"]

    def test_git_diff_files(self):
        r = _run("git_diff_files", ref="HEAD~1")
        assert r["success"]

    def test_git_blame(self):
        r = _run("git_blame", file="backend/app/config.py")
        assert r["success"]

    def test_git_show(self):
        r = _run("git_show", commit="HEAD")
        assert r["success"]


class TestCodeNavigation:
    def test_get_callees(self):
        r = _run("get_callees", function_name="execute_tool", file="backend/app/code_tools/tools.py")
        assert r["success"]

    def test_get_callers(self):
        r = _run("get_callers", function_name="execute_tool")
        assert r["success"]

    def test_get_dependencies(self):
        r = _run("get_dependencies", file_path="backend/app/code_tools/executor.py")
        assert r["success"]

    def test_get_dependents(self):
        r = _run("get_dependents", file_path="backend/app/code_tools/executor.py")
        assert r["success"]

    def test_trace_variable(self):
        r = _run("trace_variable", variable_name="tool_name", file="backend/app/code_tools/tools.py")
        assert r["success"]


class TestTestTools:
    def test_find_tests(self):
        r = _run("find_tests", name="execute_tool")
        assert r["success"]

    def test_test_outline(self):
        r = _run("test_outline", path="backend/tests/test_db.py")
        assert r["success"]


class TestPatternDetection:
    def test_detect_patterns(self):
        r = _run("detect_patterns", path="backend/app/code_tools/executor.py")
        assert r["success"]

    @pytest.mark.skipif(os.system("which ast-grep > /dev/null 2>&1") != 0, reason="ast-grep not installed")
    def test_ast_search(self):
        r = _run("ast_search", pattern="class $C(ToolExecutor)", language="python")
        assert r["success"]
