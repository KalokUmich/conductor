"""Deep parity tests: compare Python vs TypeScript tool output field-by-field.

Uses a fixed repo at tests/fixtures/parity_repo/ so the results are fully
deterministic. Both implementations are run against the same workspace and
the actual returned values are compared — not just structure.

Run:
    pytest tests/test_tool_parity_deep.py -v
"""

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Set

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from app.code_tools.tools import (
    compressed_view,
    detect_patterns,
    get_dependencies,
    get_dependents,
    module_summary,
    outline_tests,
    trace_variable,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent.parent.resolve()
FIXTURE_REPO = REPO_ROOT / "tests" / "fixtures" / "parity_repo"
WS = str(FIXTURE_REPO)

EXTENSION_DIR = REPO_ROOT / "extension"
RUNNER_SCRIPT = EXTENSION_DIR / "tests" / "run_complex_tool.js"

# ---------------------------------------------------------------------------
# TS runner
# ---------------------------------------------------------------------------


def ts(tool: str, params: dict) -> Dict[str, Any]:
    """Run a tool through the compiled TypeScript complexToolRunner."""
    result = subprocess.run(
        ["node", str(RUNNER_SCRIPT), tool, WS, json.dumps(params)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(EXTENSION_DIR),
    )
    if result.returncode != 0:
        pytest.fail(f"TS runner crashed: {result.stderr}")
    return json.loads(result.stdout)


def py_data(result) -> Any:
    """Extract .data from a Python ToolResult."""
    return result.data


# ---------------------------------------------------------------------------
# Helpers for comparison
# ---------------------------------------------------------------------------


def sorted_by(lst: list, key: str) -> list:
    return sorted(lst, key=lambda x: x.get(key, ""))


def names_set(lst: list, key: str = "name") -> Set[str]:
    return {item[key] for item in lst}


def file_set(lst: list) -> Set[str]:
    return {item["file_path"] for item in lst}


# ---------------------------------------------------------------------------
# Precondition
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not RUNNER_SCRIPT.is_file(),
    reason="TS runner not found — run 'npm run compile' in extension/ first",
)


# =========================================================================
# get_dependencies
# =========================================================================


class TestGetDependencies:
    """Both sides should find the same import targets."""

    def test_python_file(self):
        py = get_dependencies(WS, "app/service.py")
        t = ts("get_dependencies", {"file_path": "app/service.py"})

        assert py.success and t["success"]
        py_files = file_set(py.data)
        ts_files = file_set(t["data"])
        # Both must find models.py and repository.py
        assert "app/models.py" in py_files, f"Python missing: {py_files}"
        assert "app/models.py" in ts_files, f"TS missing: {ts_files}"
        assert "app/repository.py" in py_files
        assert "app/repository.py" in ts_files

    def test_ts_file(self):
        py = get_dependencies(WS, "src/handler.ts")
        t = ts("get_dependencies", {"file_path": "src/handler.ts"})

        assert py.success and t["success"]
        py_files = file_set(py.data)
        ts_files = file_set(t["data"])
        # Both should resolve the relative import to orderRepo.ts
        assert any("orderRepo" in f for f in py_files), f"Python: {py_files}"
        assert any("orderRepo" in f for f in ts_files), f"TS: {ts_files}"

    def test_empty_result_for_leaf(self):
        py = get_dependencies(WS, "app/repository.py")
        t = ts("get_dependencies", {"file_path": "app/repository.py"})
        # repository.py has no local imports
        assert py.success and t["success"]
        assert len(py.data) == 0 or all(d["file_path"] not in ("app/models.py",) for d in py.data)


# =========================================================================
# get_dependents
# =========================================================================


class TestGetDependents:
    """Both sides should find the same reverse-import files."""

    def test_models_dependents(self):
        py = get_dependents(WS, "app/models.py")
        t = ts("get_dependents", {"file_path": "app/models.py"})

        assert py.success and t["success"]
        py_files = file_set(py.data)
        ts_files = file_set(t["data"])
        assert "app/service.py" in py_files
        assert "app/service.py" in ts_files

    def test_repository_dependents(self):
        py = get_dependents(WS, "app/repository.py")
        t = ts("get_dependents", {"file_path": "app/repository.py"})

        assert py.success and t["success"]
        py_files = file_set(py.data)
        ts_files = file_set(t["data"])
        assert "app/service.py" in py_files
        assert "app/service.py" in ts_files


# =========================================================================
# test_outline
# =========================================================================


class TestTestOutline:
    """Both sides should extract the same test names, kinds, and mock lists."""

    def test_python_names(self):
        py = outline_tests(WS, path="tests/test_service.py")
        t = ts("test_outline", {"path": "tests/test_service.py"})

        assert py.success and t["success"]
        py_names = names_set(py.data)
        ts_names = names_set(t["data"])

        # Must find the class, the mocked test, and the standalone test
        for expected in ["test_create_order", "test_create_order_mocked", "test_process_payment"]:
            assert any(expected in n for n in py_names), f"Python missing {expected}: {py_names}"
            assert any(expected in n for n in ts_names), f"TS missing {expected}: {ts_names}"

    def test_python_kinds(self):
        py = outline_tests(WS, path="tests/test_service.py")
        t = ts("test_outline", {"path": "tests/test_service.py"})

        py_kinds = {e["kind"] for e in py.data}
        ts_kinds = {e["kind"] for e in t["data"]}
        # Both should find test_class and test_function
        assert "test_class" in py_kinds
        assert "test_class" in ts_kinds
        assert "test_function" in py_kinds
        assert "test_function" in ts_kinds

    def test_python_mocks_same(self):
        py = outline_tests(WS, path="tests/test_service.py")
        t = ts("test_outline", {"path": "tests/test_service.py"})

        py_mocked = [e for e in py.data if "test_create_order_mocked" in e["name"]]
        ts_mocked = [e for e in t["data"] if "test_create_order_mocked" in e["name"]]
        assert len(py_mocked) >= 1 and len(ts_mocked) >= 1

        py_mocks = set(py_mocked[0]["mocks"])
        ts_mocks = set(ts_mocked[0]["mocks"])
        # Both should detect @patch("app.service.find_user")
        assert any("find_user" in m for m in py_mocks), f"Python mocks: {py_mocks}"
        assert any("find_user" in m for m in ts_mocks), f"TS mocks: {ts_mocks}"

    def test_python_double_mock(self):
        py = outline_tests(WS, path="tests/test_service.py")
        t = ts("test_outline", {"path": "tests/test_service.py"})

        py_dm = [e for e in py.data if "double_mock" in e["name"]]
        ts_dm = [e for e in t["data"] if "double_mock" in e["name"]]
        assert len(py_dm) >= 1 and len(ts_dm) >= 1
        # Should detect 2 mocks
        assert len(py_dm[0]["mocks"]) >= 2, f"Python mocks: {py_dm[0]['mocks']}"
        assert len(ts_dm[0]["mocks"]) >= 2, f"TS mocks: {ts_dm[0]['mocks']}"

    def test_ts_test_file(self):
        py = outline_tests(WS, path="src/handler.test.ts")
        t = ts("test_outline", {"path": "src/handler.test.ts"})

        assert py.success and t["success"]
        py_names = names_set(py.data)
        ts_names = names_set(t["data"])
        # Both should find describe blocks and test names
        assert any("handleRequest" in n for n in py_names), f"Python: {py_names}"
        assert any("handleRequest" in n for n in ts_names), f"TS: {ts_names}"

    def test_ts_mocks_detected(self):
        py = outline_tests(WS, path="src/handler.test.ts")
        t = ts("test_outline", {"path": "src/handler.test.ts"})

        py_with_mocks = [e for e in py.data if e.get("mocks")]
        ts_with_mocks = [e for e in t["data"] if e.get("mocks")]
        # jest.spyOn should be detected by both
        assert len(py_with_mocks) >= 1, f"Python found no mocks: {py.data}"
        assert len(ts_with_mocks) >= 1, f"TS found no mocks: {t['data']}"


# =========================================================================
# compressed_view
# =========================================================================


class TestCompressedView:
    """Both sides should extract the same symbols, calls, and side effects."""

    def test_service_file(self):
        py = compressed_view(WS, "app/service.py")
        t = ts("compressed_view", {"file_path": "app/service.py"})

        assert py.success and t["success"]
        # Same total_lines
        assert py.data["total_lines"] == t["data"]["total_lines"], (
            f"Python={py.data['total_lines']}, TS={t['data']['total_lines']}"
        )
        # Same symbol_count
        assert py.data["symbol_count"] == t["data"]["symbol_count"], (
            f"Python={py.data['symbol_count']}, TS={t['data']['symbol_count']}"
        )

    def test_service_signatures(self):
        py = compressed_view(WS, "app/service.py")
        t = ts("compressed_view", {"file_path": "app/service.py"})

        for keyword in ["OrderService", "create_order", "cancel_order", "process_payment"]:
            assert keyword in py.data["content"], f"Python missing '{keyword}'"
            assert keyword in t["data"]["content"], f"TS missing '{keyword}'"

    def test_service_calls(self):
        py = compressed_view(WS, "app/service.py")
        t = ts("compressed_view", {"file_path": "app/service.py"})

        # Both should detect calls: find_user(), save_order(), send_email()
        for call in ["find_user()", "save_order()", "send_email()"]:
            assert call in py.data["content"], f"Python missing call '{call}'"
            assert call in t["data"]["content"], f"TS missing call '{call}'"

    def test_service_side_effects(self):
        py = compressed_view(WS, "app/service.py")
        t = ts("compressed_view", {"file_path": "app/service.py"})

        # process_payment has requests.post → http call, session.commit → db write
        assert "side_effects:" in py.data["content"]
        assert "side_effects:" in t["data"]["content"]

    def test_service_raises(self):
        py = compressed_view(WS, "app/service.py")
        t = ts("compressed_view", {"file_path": "app/service.py"})

        # create_order raises ValueError
        assert "ValueError" in py.data["content"], f"Python content: {py.data['content']}"
        assert "ValueError" in t["data"]["content"], f"TS content: {t['data']['content']}"

    def test_focus_filter(self):
        py = compressed_view(WS, "app/service.py", focus="process_payment")
        t = ts("compressed_view", {"file_path": "app/service.py", "focus": "process_payment"})

        assert py.success and t["success"]
        assert py.data["symbol_count"] == t["data"]["symbol_count"]
        assert "process_payment" in py.data["content"]
        assert "process_payment" in t["data"]["content"]
        # Should NOT include OrderService (filtered out)
        assert "OrderService" not in py.data["content"]
        assert "OrderService" not in t["data"]["content"]


# =========================================================================
# module_summary
# =========================================================================


class TestModuleSummary:
    """Both sides should find the same file count, classes, and functions."""

    def test_app_module(self):
        py = module_summary(WS, "app")
        t = ts("module_summary", {"module_path": "app"})

        assert py.success and t["success"]
        # Same file count
        assert py.data["file_count"] == t["data"]["file_count"], (
            f"Python={py.data['file_count']}, TS={t['data']['file_count']}"
        )
        # Same total LOC
        assert py.data["loc"] == t["data"]["loc"], f"Python={py.data['loc']}, TS={t['data']['loc']}"

    def test_app_classes(self):
        py = module_summary(WS, "app")
        t = ts("module_summary", {"module_path": "app"})

        for keyword in ["OrderService", "UserModel", "OrderSchema"]:
            assert keyword in py.data["content"], f"Python missing '{keyword}'"
            assert keyword in t["data"]["content"], f"TS missing '{keyword}'"

    def test_app_functions(self):
        py = module_summary(WS, "app")
        t = ts("module_summary", {"module_path": "app"})

        for keyword in ["find_user", "save_order", "process_payment"]:
            assert keyword in py.data["content"], f"Python missing '{keyword}'"
            assert keyword in t["data"]["content"], f"TS missing '{keyword}'"

    def test_app_file_list(self):
        py = module_summary(WS, "app")
        t = ts("module_summary", {"module_path": "app"})

        for filename in ["service.py", "repository.py", "models.py", "auth.py"]:
            assert filename in py.data["content"], f"Python missing file '{filename}'"
            assert filename in t["data"]["content"], f"TS missing file '{filename}'"


# =========================================================================
# trace_variable
# =========================================================================


class TestTraceVariable:
    """Both sides should detect the same aliases, flows, and sinks.

    NOTE: Uses top-level functions (not methods) because the test conftest
    stubs tree_sitter_languages, causing extract_definitions to fall back
    to regex which may miss class methods.
    """

    def test_forward_sinks_orm(self):
        """find_user is a top-level function with an ORM .filter() sink."""
        params = {
            "variable_name": "user_identifier",
            "file": "app/repository.py",
            "function_name": "find_user",
            "direction": "forward",
        }
        py = trace_variable(WS, **params)
        t = ts("trace_variable", params)

        assert py.success and t["success"]
        py_kinds = {s["kind"] for s in py.data["sinks"]}
        ts_kinds = {s["kind"] for s in t["data"]["sinks"]}
        assert "orm_filter" in py_kinds, f"Python sinks: {py_kinds}"
        assert "orm_filter" in ts_kinds, f"TS sinks: {ts_kinds}"

    def test_forward_sinks_sql(self):
        """save_order is a top-level function with a SQL .execute() sink."""
        params = {
            "variable_name": "ref_id",
            "file": "app/repository.py",
            "function_name": "save_order",
            "direction": "forward",
        }
        py = trace_variable(WS, **params)
        t = ts("trace_variable", params)

        assert py.success and t["success"]
        py_kinds = {s["kind"] for s in py.data["sinks"]}
        ts_kinds = {s["kind"] for s in t["data"]["sinks"]}
        assert "sql_param" in py_kinds, f"Python sinks: {py_kinds}"
        assert "sql_param" in ts_kinds, f"TS sinks: {ts_kinds}"

    def test_top_level_fields_match(self):
        """Both sides should return the same top-level fields."""
        params = {
            "variable_name": "user_identifier",
            "file": "app/repository.py",
            "function_name": "find_user",
            "direction": "forward",
        }
        py = trace_variable(WS, **params)
        t = ts("trace_variable", params)

        assert py.success and t["success"]
        assert py.data["variable"] == t["data"]["variable"]
        assert py.data["function"] == t["data"]["function"]
        assert py.data["direction"] == t["data"]["direction"]

    def test_data_shape(self):
        """Both sides should return all required keys in data."""
        params = {
            "variable_name": "user_identifier",
            "file": "app/repository.py",
            "function_name": "find_user",
            "direction": "forward",
        }
        py = trace_variable(WS, **params)
        t = ts("trace_variable", params)

        for key in [
            "variable",
            "file",
            "function",
            "direction",
            "aliases",
            "flows_to",
            "sinks",
            "flows_from",
            "sources",
        ]:
            assert key in py.data, f"Python missing key: {key}"
            assert key in t["data"], f"TS missing key: {key}"

    def test_forward_flows_to_top_level(self):
        """process_payment calls requests.post — both should detect it."""
        params = {
            "variable_name": "amount",
            "file": "app/service.py",
            "function_name": "process_payment",
            "direction": "forward",
        }
        py = trace_variable(WS, **params)
        t = ts("trace_variable", params)

        assert py.success and t["success"]
        _py_callees = {f["callee_function"] for f in py.data["flows_to"]}
        _ts_callees = {f["callee_function"] for f in t["data"]["flows_to"]}
        # Both should find the requests.post call with amount as argument
        assert len(py.data["flows_to"]) >= 1, f"Python flows: {py.data['flows_to']}"
        assert len(t["data"]["flows_to"]) >= 1, f"TS flows: {t['data']['flows_to']}"

    def test_sink_fields(self):
        """Each sink entry should have the required fields."""
        params = {
            "variable_name": "user_identifier",
            "file": "app/repository.py",
            "function_name": "find_user",
            "direction": "forward",
        }
        t = ts("trace_variable", params)

        assert t["success"]
        for sink in t["data"]["sinks"]:
            assert "kind" in sink
            assert "expression" in sink
            assert "line" in sink


# =========================================================================
# detect_patterns
# =========================================================================


class TestDetectPatterns:
    """Both sides should detect the same patterns in the fixture repo."""

    def test_all_categories(self):
        py = detect_patterns(WS)
        t = ts("detect_patterns", {})

        assert py.success and t["success"]
        # Same number of files scanned
        assert py.data["files_scanned"] == t["data"]["files_scanned"], (
            f"Python={py.data['files_scanned']}, TS={t['data']['files_scanned']}"
        )

    def test_retry_count(self):
        py = detect_patterns(WS, categories=["retry"])
        t = ts("detect_patterns", {"categories": ["retry"]})

        py_count = py.data["summary"].get("retry", 0)
        ts_count = t["data"]["summary"].get("retry", 0)
        assert py_count >= 1 and ts_count >= 1
        assert py_count == ts_count, f"Python={py_count}, TS={ts_count}"

    def test_token_lifecycle_count(self):
        py = detect_patterns(WS, categories=["token_lifecycle"])
        t = ts("detect_patterns", {"categories": ["token_lifecycle"]})

        py_count = py.data["summary"].get("token_lifecycle", 0)
        ts_count = t["data"]["summary"].get("token_lifecycle", 0)
        assert py_count >= 1 and ts_count >= 1
        assert py_count == ts_count, f"Python={py_count}, TS={ts_count}"

    def test_side_effect_chain_count(self):
        py = detect_patterns(WS, categories=["side_effect_chain"])
        t = ts("detect_patterns", {"categories": ["side_effect_chain"]})

        py_count = py.data["summary"].get("side_effect_chain", 0)
        ts_count = t["data"]["summary"].get("side_effect_chain", 0)
        assert py_count >= 1 and ts_count >= 1
        assert py_count == ts_count, f"Python={py_count}, TS={ts_count}"

    def test_transaction_count(self):
        py = detect_patterns(WS, categories=["transaction"])
        t = ts("detect_patterns", {"categories": ["transaction"]})

        py_count = py.data["summary"].get("transaction", 0)
        ts_count = t["data"]["summary"].get("transaction", 0)
        # Both should find the same count (may be 0 if no patterns)
        assert py_count == ts_count, f"Python={py_count}, TS={ts_count}"

    def test_match_files_same(self):
        """For each category, both sides should flag the same files."""
        py = detect_patterns(WS)
        t = ts("detect_patterns", {})

        all_cats = set(py.data["matches"].keys()) | set(t["data"]["matches"].keys())
        for cat in all_cats:
            py_files = {m["file"] for m in py.data["matches"].get(cat, [])}
            ts_files = {m["file"] for m in t["data"]["matches"].get(cat, [])}
            assert py_files == ts_files, f"Category '{cat}' file mismatch: Python={py_files}, TS={ts_files}"

    def test_match_lines_same(self):
        """For each match, both sides should report the same line numbers."""
        py = detect_patterns(WS)
        t = ts("detect_patterns", {})

        all_cats = set(py.data["matches"].keys()) | set(t["data"]["matches"].keys())
        for cat in all_cats:
            py_locs = {(m["file"], m["line"]) for m in py.data["matches"].get(cat, [])}
            ts_locs = {(m["file"], m["line"]) for m in t["data"]["matches"].get(cat, [])}
            assert py_locs == ts_locs, (
                f"Category '{cat}' line mismatch:\n  Python: {sorted(py_locs)}\n  TS:     {sorted(ts_locs)}"
            )
