"""Parity tests: verify Python backend tools work correctly against a fixture repo.

These run each code tool against the dedicated ``tests/fixtures/parity_repo``
fixture (materialised with a real 2-commit git history by the ``git_parity_repo``
session fixture in conftest.py) rather than the live conductor source tree. The
same fixture and inputs are used by the TypeScript extension parity tests — if
these pass, the extension's grep/LSP fallback implementations should produce
equivalent results.

Why a fixture and not the real tree: the repo root contains
``eval/code_review/repos`` (~9.3 GB / 600k+ vendored files incl. 120k TSX), which
makes whole-workspace tools (find_symbol / get_callers) scan-bomb tree-sitter
until the per-file timeout accumulates and the run hangs. A 19-file fixture keeps
these checks fast, deterministic, and able to exercise the git_* tools.
"""

import os
import sys
from pathlib import Path
from typing import Any, Dict

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.code_tools.tools import execute_tool


def _run(ws: str, tool: str, **params) -> Dict[str, Any]:
    result = execute_tool(tool, ws, params)
    return {"success": result.success, "data": result.data, "error": result.error}


class TestFileOperations:
    def test_read_file(self, git_parity_repo):
        r = _run(git_parity_repo, "read_file", path="app/service.py", start_line=1, end_line=5)
        assert r["success"]
        assert "content" in r["data"]
        assert r["data"]["total_lines"] > 10

    def test_list_files(self, git_parity_repo):
        r = _run(git_parity_repo, "list_files", directory="app", max_depth=1)
        assert r["success"]
        data = r["data"]
        files = (
            [f["path"] if isinstance(f, dict) else f for f in data] if isinstance(data, list) else data.get("files", [])
        )
        assert any("service.py" in str(f) for f in files)

    def test_grep(self, git_parity_repo):
        r = _run(git_parity_repo, "grep", pattern="class OrderService", max_results=10)
        assert r["success"]
        data = r["data"]
        assert isinstance(data, list)
        assert len(data) >= 1


class TestSymbolTools:
    def test_find_symbol(self, git_parity_repo):
        r = _run(git_parity_repo, "find_symbol", name="OrderService")
        assert r["success"]

    def test_file_outline(self, git_parity_repo):
        r = _run(git_parity_repo, "file_outline", path="app/service.py")
        assert r["success"]
        data = r["data"]
        symbols = data if isinstance(data, list) else data.get("symbols", [])
        names = [str(s) for s in symbols]
        joined = " ".join(names)
        assert "OrderService" in joined

    def test_find_references(self, git_parity_repo):
        r = _run(git_parity_repo, "find_references", symbol_name="OrderService", file="app/controller.py")
        assert r["success"]

    def test_compressed_view(self, git_parity_repo):
        r = _run(git_parity_repo, "compressed_view", file_path="app/service.py")
        assert r["success"]
        text = str(r["data"])
        assert "OrderService" in text

    def test_module_summary(self, git_parity_repo):
        r = _run(git_parity_repo, "module_summary", module_path="app")
        assert r["success"]

    def test_expand_symbol(self, git_parity_repo):
        r = _run(git_parity_repo, "expand_symbol", symbol_name="OrderService", file_path="app/service.py")
        assert r["success"]
        text = str(r["data"])
        assert "OrderService" in text


class TestGitTools:
    def test_git_log(self, git_parity_repo):
        r = _run(git_parity_repo, "git_log", n=5)
        assert r["success"]
        text = str(r["data"])
        assert len(text) > 10  # has real commit content

    def test_git_diff(self, git_parity_repo):
        r = _run(git_parity_repo, "git_diff", ref1="HEAD~1", ref2="HEAD")
        assert r["success"]

    def test_git_diff_files(self, git_parity_repo):
        r = _run(git_parity_repo, "git_diff_files", ref="HEAD~1")
        assert r["success"]

    def test_git_blame(self, git_parity_repo):
        r = _run(git_parity_repo, "git_blame", file="app/service.py")
        assert r["success"]

    def test_git_show(self, git_parity_repo):
        r = _run(git_parity_repo, "git_show", commit="HEAD")
        assert r["success"]


class TestCodeNavigation:
    def test_get_callees(self, git_parity_repo):
        r = _run(git_parity_repo, "get_callees", function_name="process_payment", file="app/service.py")
        assert r["success"]

    def test_get_callers(self, git_parity_repo):
        r = _run(git_parity_repo, "get_callers", function_name="find_user")
        assert r["success"]

    def test_get_dependencies(self, git_parity_repo):
        r = _run(git_parity_repo, "get_dependencies", file_path="app/service.py")
        assert r["success"]

    def test_get_dependents(self, git_parity_repo):
        r = _run(git_parity_repo, "get_dependents", file_path="app/models.py")
        assert r["success"]

    def test_trace_variable(self, git_parity_repo):
        # Top-level function param — regex fallback (tree-sitter stubbed in tests)
        # cannot trace variables inside class methods.
        r = _run(git_parity_repo, "trace_variable", variable_name="user_identifier", file="app/repository.py")
        assert r["success"]


class TestTestTools:
    def test_find_tests(self, git_parity_repo):
        r = _run(git_parity_repo, "find_tests", name="OrderService")
        assert r["success"]

    def test_test_outline(self, git_parity_repo):
        r = _run(git_parity_repo, "test_outline", path="tests/test_service.py")
        assert r["success"]


class TestPatternDetection:
    def test_detect_patterns(self, git_parity_repo):
        r = _run(git_parity_repo, "detect_patterns", path="app/service.py")
        assert r["success"]

    @pytest.mark.skipif(os.system("which ast-grep > /dev/null 2>&1") != 0, reason="ast-grep not installed")
    def test_ast_search(self, git_parity_repo):
        r = _run(git_parity_repo, "ast_search", pattern="class $C", language="python")
        assert r["success"]
        assert "results" in r["data"] or isinstance(r["data"], list)
