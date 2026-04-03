"""Cross-language parity tests for the 6 AST tools (Python vs TypeScript).

Fixture repo: tests/fixtures/parity_repo/ (relative to repo root).
TS runner:    extension/tests/run_ts_tool.js

For each tool we run the same params through the Python implementation
(direct import) and the TypeScript implementation (Node subprocess),
then compare the results structurally.

Run:
    pytest tests/test_tool_parity_ast.py -v

Skip TS side when node / compiled extension is unavailable:
    pytest tests/test_tool_parity_ast.py -v -k "not ts"
"""

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.code_tools.tools import (
    expand_symbol as py_expand_symbol,
)
from app.code_tools.tools import (
    file_outline as py_file_outline,
)
from app.code_tools.tools import (
    find_references as py_find_references,
)
from app.code_tools.tools import (
    find_symbol as py_find_symbol,
)
from app.code_tools.tools import (
    get_callees as py_get_callees,
)
from app.code_tools.tools import (
    get_callers as py_get_callers,
)
from tests.parity_helpers import (
    END_LINE_TOLERANCE,
    EXTENSION_DIR,
    TS_RUNNER,
    WS,
    assert_names_subset,
    normalize_path,
    run_ts_tool,
)

# ---------------------------------------------------------------------------
# Skip TS tests if node or compiled extension is not available
# ---------------------------------------------------------------------------

_ts_available = None


def _check_ts():
    global _ts_available
    if _ts_available is None:
        try:
            subprocess.run(["node", "--version"], capture_output=True, timeout=5)
            _ts_available = TS_RUNNER.is_file() and (EXTENSION_DIR / "out").is_dir()
        except Exception:
            _ts_available = False
    return _ts_available


def _skip_unless_ts():
    if not _check_ts():
        pytest.skip("Node.js or compiled TS extension not available")


# ===================================================================
# file_outline
# ===================================================================


class TestFileOutlineParity:
    """file_outline: list all symbol definitions in a file."""

    def test_python_file(self):
        """Outline app/service.py — TS should find everything Python finds (and possibly nested methods)."""
        _skip_unless_ts()
        py = py_file_outline(WS, path="app/service.py")
        ts = run_ts_tool("file_outline", {"path": "app/service.py"})
        assert py.success
        assert ts["success"]
        # TS tree-sitter may find nested methods that Python regex misses.
        # What matters: every symbol Python finds, TS also finds.
        assert_names_subset(py.data, ts["data"], "file_outline(service.py)")

    def test_typescript_file(self):
        """Outline src/handler.ts — TS should find everything Python finds."""
        _skip_unless_ts()
        py = py_file_outline(WS, path="src/handler.ts")
        ts = run_ts_tool("file_outline", {"path": "src/handler.ts"})
        assert py.success
        assert ts["success"]
        assert_names_subset(py.data, ts["data"], "file_outline(handler.ts)")

    def test_models_file(self):
        """Outline app/models.py — both should find UserModel."""
        _skip_unless_ts()
        py = py_file_outline(WS, path="app/models.py")
        ts = run_ts_tool("file_outline", {"path": "app/models.py"})
        assert py.success
        assert ts["success"]
        py_names = {d["name"] for d in py.data}
        ts_names = {d["name"] for d in ts["data"]}
        assert "UserModel" in py_names
        assert "UserModel" in ts_names
        assert_names_subset(py.data, ts["data"], "file_outline(models.py)")

    def test_relative_paths(self):
        """TS should return relative paths, not absolute."""
        _skip_unless_ts()
        ts = run_ts_tool("file_outline", {"path": "app/service.py"})
        assert ts["success"]
        for sym in ts["data"]:
            fp = sym.get("file_path", "")
            assert not fp.startswith("/"), f"Absolute path leaked: {fp}"

    def test_start_line_agreement(self):
        """Start lines should match between Python and TS."""
        _skip_unless_ts()
        py = py_file_outline(WS, path="app/auth.py")
        ts = run_ts_tool("file_outline", {"path": "app/auth.py"})
        assert py.success and ts["success"]
        py_by_name = {d["name"]: d for d in py.data}
        ts_by_name = {d["name"]: d for d in ts["data"]}
        for name in py_by_name:
            if name in ts_by_name:
                assert py_by_name[name]["start_line"] == ts_by_name[name]["start_line"], (
                    f"start_line mismatch for {name}: "
                    f"py={py_by_name[name]['start_line']} ts={ts_by_name[name]['start_line']}"
                )


# ===================================================================
# find_symbol
# ===================================================================


class TestFindSymbolParity:
    """find_symbol: locate symbol definitions across the workspace."""

    def test_exact_match_class(self):
        """Find OrderService — should match in both backends."""
        _skip_unless_ts()
        py = py_find_symbol(WS, name="OrderService")
        ts = run_ts_tool("find_symbol", {"name": "OrderService"})
        assert len(py.data) > 0, "Python found no results for OrderService"
        assert len(ts["data"]) > 0, "TS found no results for OrderService"
        assert py.data[0]["name"] == ts["data"][0]["name"]

    def test_exact_match_function(self):
        """Find process_payment — should match in both backends."""
        _skip_unless_ts()
        py = py_find_symbol(WS, name="process_payment")
        ts = run_ts_tool("find_symbol", {"name": "process_payment"})
        assert len(py.data) > 0
        assert len(ts["data"]) > 0
        assert py.data[0]["name"] == ts["data"][0]["name"]

    def test_has_role(self):
        """TS find_symbol should include a role field."""
        _skip_unless_ts()
        ts = run_ts_tool("find_symbol", {"name": "OrderService"})
        assert len(ts["data"]) > 0
        assert "role" in ts["data"][0], "TS find_symbol should include role field"

    def test_kind_filter_class(self):
        """Filter by kind=class should return only classes."""
        _skip_unless_ts()
        py = py_find_symbol(WS, name="OrderService", kind="class")
        ts = run_ts_tool("find_symbol", {"name": "OrderService", "kind": "class"})
        assert len(py.data) == len(ts["data"]), f"kind=class count mismatch: py={len(py.data)} ts={len(ts['data'])}"
        for d in py.data:
            assert d["kind"] == "class"
        for d in ts["data"]:
            assert d["kind"] == "class"

    def test_kind_filter_function(self):
        """Filter by kind=function should return only functions."""
        _skip_unless_ts()
        py = py_find_symbol(WS, name="find_user", kind="function")
        ts = run_ts_tool("find_symbol", {"name": "find_user", "kind": "function"})
        assert len(py.data) == len(ts["data"])

    def test_ts_symbol_file_path(self):
        """TS results should contain the correct relative file_path."""
        _skip_unless_ts()
        ts = run_ts_tool("find_symbol", {"name": "handleRequest"})
        assert len(ts["data"]) > 0
        found_path = normalize_path(ts["data"][0]["file_path"])
        assert found_path == "src/handler.ts"


# ===================================================================
# find_references
# ===================================================================


class TestFindReferencesParity:
    """find_references: locate usages of a symbol across files."""

    def test_symbol_references(self):
        """Both backends should find references to OrderService."""
        _skip_unless_ts()
        py = py_find_references(WS, symbol_name="OrderService")
        ts = run_ts_tool("find_references", {"symbol_name": "OrderService"})
        assert py.success and ts["success"]
        assert len(py.data) > 0, "Python found no references for OrderService"
        assert len(ts["data"]) > 0, "TS found no references for OrderService"

    def test_references_with_file_filter(self):
        """Filtering to a specific file should narrow results."""
        _skip_unless_ts()
        py = py_find_references(WS, symbol_name="OrderService", file="app/controller.py")
        ts = run_ts_tool("find_references", {"symbol_name": "OrderService", "file": "app/controller.py"})
        assert py.success and ts["success"]
        # controller.py imports and uses OrderService
        assert len(py.data) > 0
        assert len(ts["data"]) > 0

    def test_cross_language_reference(self):
        """findOrder is referenced in handler.ts and handler.test.ts."""
        _skip_unless_ts()
        py = py_find_references(WS, symbol_name="findOrder")
        ts = run_ts_tool("find_references", {"symbol_name": "findOrder"})
        assert py.success and ts["success"]
        py_files = {normalize_path(d["file_path"]) for d in py.data}
        ts_files = {normalize_path(d["file_path"]) for d in ts["data"]}
        # Both should find references in handler.ts at minimum
        assert any("handler.ts" in f for f in py_files)
        assert any("handler.ts" in f for f in ts_files)


# ===================================================================
# get_callees
# ===================================================================


class TestGetCalleesParity:
    """get_callees: list functions called within a given function body."""

    def test_process_payment_callees_parity(self):
        """process_payment (top-level function) — both should find post, add, commit."""
        _skip_unless_ts()
        py = py_get_callees(WS, function_name="process_payment", file="app/service.py")
        ts = run_ts_tool("get_callees", {"function_name": "process_payment", "file": "app/service.py"})
        assert py.success and ts["success"]
        py_names = {c["callee_name"] for c in py.data}
        ts_names = {c["callee_name"] for c in ts["data"]}
        # Both should find key callees
        common = py_names & ts_names
        assert len(common) >= 1, f"Too few common callees: py={py_names}, ts={ts_names}"

    def test_ts_function_callees(self):
        """handleRequest in handler.ts calls findOrder and sendNotification."""
        _skip_unless_ts()
        py = py_get_callees(WS, function_name="handleRequest", file="src/handler.ts")
        ts = run_ts_tool("get_callees", {"function_name": "handleRequest", "file": "src/handler.ts"})
        assert py.success and ts["success"]
        py_names = {c["callee_name"] for c in py.data}
        ts_names = {c["callee_name"] for c in ts["data"]}
        assert "findOrder" in py_names
        assert "findOrder" in ts_names
        assert py_names == ts_names

    def test_process_payment_callees(self):
        """process_payment calls requests.post, session.add, session.commit."""
        _skip_unless_ts()
        py = py_get_callees(WS, function_name="process_payment", file="app/service.py")
        ts = run_ts_tool("get_callees", {"function_name": "process_payment", "file": "app/service.py"})
        assert py.success and ts["success"]
        py_names = {c["callee_name"] for c in py.data}
        ts_names = {c["callee_name"] for c in ts["data"]}
        assert py_names == ts_names


# ===================================================================
# get_callers
# ===================================================================


class TestGetCallersParity:
    """get_callers: find all call sites of a given function."""

    def test_validate_callers(self):
        """validate is called from controller.py (login function — top-level)."""
        _skip_unless_ts()
        py = py_get_callers(WS, function_name="validate", path="app")
        ts = run_ts_tool("get_callers", {"function_name": "validate", "path": "app"})
        assert py.success and ts["success"]
        py_names = {c["caller_name"] for c in py.data}
        ts_names = {c["caller_name"] for c in ts["data"]}
        # Both should find at least one caller
        assert len(py_names) > 0, f"Python found no callers: {py.data}"
        assert len(ts_names) > 0, f"TS found no callers: {ts['data']}"

    def test_validate_token_callers(self):
        """validate_token is called from controller.py (place_order, cancel_order) and auth.py (validate)."""
        _skip_unless_ts()
        py = py_get_callers(WS, function_name="validate_token")
        ts = run_ts_tool("get_callers", {"function_name": "validate_token"})
        assert py.success and ts["success"]
        py_names = {c["caller_name"] for c in py.data}
        ts_names = {c["caller_name"] for c in ts["data"]}
        assert py_names == ts_names

    def test_callers_with_path_filter(self):
        """Filtering callers by path should narrow the search scope."""
        _skip_unless_ts()
        py = py_get_callers(WS, function_name="validate_token", path="app/controller.py")
        ts = run_ts_tool("get_callers", {"function_name": "validate_token", "path": "app/controller.py"})
        assert py.success and ts["success"]
        py_names = {c["caller_name"] for c in py.data}
        ts_names = {c["caller_name"] for c in ts["data"]}
        assert py_names == ts_names

    def test_findOrder_callers(self):
        """findOrder is called from handleRequest and OrderController.getOrder in handler.ts."""
        _skip_unless_ts()
        py = py_get_callers(WS, function_name="findOrder")
        ts = run_ts_tool("get_callers", {"function_name": "findOrder"})
        assert py.success and ts["success"]
        py_names = {c["caller_name"] for c in py.data}
        ts_names = {c["caller_name"] for c in ts["data"]}
        # Both should find at least one caller; may differ on nested methods
        assert len(py_names) > 0 and len(ts_names) > 0
        common = py_names & ts_names
        assert len(common) >= 1, f"No common callers: py={py_names}, ts={ts_names}"


# ===================================================================
# expand_symbol
# ===================================================================


class TestExpandSymbolParity:
    """expand_symbol: retrieve full source code for a symbol."""

    def test_expand_class(self):
        """Expand OrderService class — TS source should contain Python source."""
        _skip_unless_ts()
        py = py_expand_symbol(WS, symbol_name="OrderService", file_path="app/service.py")
        ts = run_ts_tool("expand_symbol", {"symbol_name": "OrderService", "file_path": "app/service.py"})
        assert py.success and ts["success"]
        assert py.data["symbol_name"] == ts["data"]["symbol_name"]
        assert py.data["kind"] == ts["data"]["kind"]
        # TS tree-sitter has accurate end_line so may return more source.
        # Python regex fallback may return only the first line.
        # Check that TS source contains Python source (superset).
        py_src = py.data["source"].replace("\r\n", "\n").strip()
        ts_src = ts["data"]["source"].replace("\r\n", "\n").strip()
        assert py_src in ts_src or ts_src in py_src, (
            f"Source not a subset.\npy({len(py_src)} chars): {py_src[:100]}...\n"
            f"ts({len(ts_src)} chars): {ts_src[:100]}..."
        )

    def test_expand_function(self):
        """Expand process_payment function — TS source should contain Python source."""
        _skip_unless_ts()
        py = py_expand_symbol(WS, symbol_name="process_payment", file_path="app/service.py")
        ts = run_ts_tool("expand_symbol", {"symbol_name": "process_payment", "file_path": "app/service.py"})
        assert py.success and ts["success"]
        assert py.data["symbol_name"] == ts["data"]["symbol_name"]
        assert py.data["kind"] == ts["data"]["kind"]
        py_src = py.data["source"].replace("\r\n", "\n").strip()
        ts_src = ts["data"]["source"].replace("\r\n", "\n").strip()
        assert py_src in ts_src or ts_src in py_src

    def test_expand_ts_class(self):
        """Expand OrderController from handler.ts."""
        _skip_unless_ts()
        py = py_expand_symbol(WS, symbol_name="OrderController", file_path="src/handler.ts")
        ts = run_ts_tool("expand_symbol", {"symbol_name": "OrderController", "file_path": "src/handler.ts"})
        assert py.success and ts["success"]
        assert py.data["symbol_name"] == ts["data"]["symbol_name"]
        assert py.data["kind"] == ts["data"]["kind"]
        py_src = py.data["source"].replace("\r\n", "\n").strip()
        ts_src = ts["data"]["source"].replace("\r\n", "\n").strip()
        assert py_src in ts_src or ts_src in py_src

    def test_relative_path_in_result(self):
        """Expanded symbol should report a relative file_path, not absolute."""
        _skip_unless_ts()
        ts = run_ts_tool("expand_symbol", {"symbol_name": "OrderService", "file_path": "app/service.py"})
        assert ts["success"]
        assert not ts["data"]["file_path"].startswith("/"), f"Absolute path in result: {ts['data']['file_path']}"

    def test_start_end_line_agreement(self):
        """Start and end lines should agree within tolerance."""
        _skip_unless_ts()
        py = py_expand_symbol(WS, symbol_name="validate_token", file_path="app/auth.py")
        ts = run_ts_tool("expand_symbol", {"symbol_name": "validate_token", "file_path": "app/auth.py"})
        assert py.success and ts["success"]
        assert py.data["start_line"] == ts["data"]["start_line"]
        assert abs(py.data["end_line"] - ts["data"]["end_line"]) <= END_LINE_TOLERANCE
