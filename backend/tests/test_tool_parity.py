"""Cross-language parity tests: Python backend vs TypeScript extension.

These tests create a shared workspace fixture, run each tool through both
the Python implementation (direct call) and the TypeScript implementation
(via Node.js subprocess), and assert that the outputs have the same
structure and semantically equivalent data.

Run:
    pytest tests/test_tool_parity.py -v

Skip TS side if node is not available:
    pytest tests/test_tool_parity.py -v -k "not ts"
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any, Dict, Optional

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
# Skip TS tests if node not available
# ---------------------------------------------------------------------------

NODE_BIN = "node"
EXTENSION_DIR = Path(__file__).parent.parent.parent / "extension"
RUNNER_SCRIPT = EXTENSION_DIR / "tests" / "run_complex_tool.js"

_node_available: Optional[bool] = None


def _check_node() -> bool:
    global _node_available
    if _node_available is None:
        try:
            subprocess.run([NODE_BIN, "--version"], capture_output=True, timeout=5)
            _node_available = RUNNER_SCRIPT.is_file()
        except Exception:
            _node_available = False
    return _node_available


def run_ts_tool(tool: str, workspace: str, params: dict) -> Dict[str, Any]:
    """Run a tool through the TypeScript complexToolRunner via Node.js."""
    if not _check_node():
        pytest.skip("Node.js or TS runner script not available")

    result = subprocess.run(
        [NODE_BIN, str(RUNNER_SCRIPT), tool, workspace, json.dumps(params)],
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(EXTENSION_DIR),
    )
    if result.returncode != 0:
        return {"success": False, "data": None, "error": result.stderr.strip()}
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# Shared workspace fixture (matches test_code_tools.py)
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Create a workspace matching test_code_tools.py for parity testing."""
    # Python files
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "main.py").write_text(
        textwrap.dedent("""\
        from app.service import MyService

        class App:
            def __init__(self):
                self.service = MyService()

            def run(self):
                return self.service.process()
    """)
    )
    (tmp_path / "app" / "service.py").write_text(
        textwrap.dedent("""\
        from app.utils import helper

        class MyService:
            def process(self):
                return helper("data")

        def standalone_function():
            pass

        def orchestrate():
            result = helper("input")
            standalone_function()
            return result
    """)
    )
    (tmp_path / "app" / "utils.py").write_text(
        textwrap.dedent("""\
        def helper(data: str) -> str:
            return data.upper()

        def unused_helper():
            return 42
    """)
    )

    # TypeScript files
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text(
        textwrap.dedent("""\
        import { greet } from './utils';

        function main(): void {
            console.log(greet("world"));
        }

        export class Application {
            start() {
                main();
            }
        }
    """)
    )
    (tmp_path / "src" / "utils.ts").write_text(
        textwrap.dedent("""\
        export function greet(name: string): string {
            return `Hello, ${name}!`;
        }
    """)
    )

    # Python test files
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "test_service.py").write_text(
        textwrap.dedent("""\
        from unittest.mock import patch, MagicMock
        from app.service import MyService, standalone_function

        class TestMyService:
            def test_process(self):
                svc = MyService()
                result = svc.process()
                assert result == "DATA"

            @patch("app.service.helper")
            def test_process_mocked(self, mock_helper):
                mock_helper.return_value = "MOCKED"
                svc = MyService()
                result = svc.process()
                assert result == "MOCKED"
                mock_helper.assert_called_once_with("data")

        def test_standalone():
            result = standalone_function()
            assert result is None
    """)
    )

    # TS test file
    (tmp_path / "src" / "utils.test.ts").write_text(
        textwrap.dedent("""\
        import { greet } from './utils';

        describe('greet', () => {
            it('should return greeting', () => {
                expect(greet('world')).toBe('Hello, world!');
            });

            it('should handle empty string', () => {
                const result = greet('');
                expect(result).toBe('Hello, !');
            });
        });
    """)
    )

    # Java files
    (tmp_path / "src" / "main" / "java").mkdir(parents=True)
    (tmp_path / "src" / "main" / "java" / "AuthService.java").write_text(
        textwrap.dedent("""\
        package com.example;

        public class AuthService {
            public boolean authenticate(String user, String pass) {
                return user != null && pass != null;
            }
        }
    """)
    )
    (tmp_path / "src" / "test" / "java").mkdir(parents=True)
    (tmp_path / "src" / "test" / "java" / "AuthServiceTest.java").write_text(
        textwrap.dedent("""\
        package com.example;

        import org.junit.jupiter.api.Test;
        import org.junit.jupiter.params.ParameterizedTest;
        import static org.junit.jupiter.api.Assertions.*;
        import static org.mockito.Mockito.*;

        public class AuthServiceTest {

            @Test
            public void testAuthenticate() {
                AuthService svc = new AuthService();
                assertTrue(svc.authenticate("admin", "pass"));
            }

            @ParameterizedTest
            public void testAuthenticateNull() {
                AuthService svc = new AuthService();
                assertFalse(svc.authenticate(null, "pass"));
            }

            @Test
            public void testWithMock() {
                AuthService svc = mock(AuthService.class);
                when(svc.authenticate("a", "b")).thenReturn(true);
                verify(svc).authenticate("a", "b");
            }
        }
    """)
    )

    # Go files
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "calc.go").write_text(
        textwrap.dedent("""\
        package calc

        func Add(a, b int) int {
            return a + b
        }
    """)
    )
    (tmp_path / "pkg" / "calc_test.go").write_text(
        textwrap.dedent("""\
        package calc

        import (
            "testing"
            "github.com/stretchr/testify/assert"
        )

        func TestAdd(t *testing.T) {
            result := Add(2, 3)
            assert.Equal(t, 5, result)
        }

        func TestAddNegative(t *testing.T) {
            t.Run("negative numbers", func(t *testing.T) {
                result := Add(-1, -2)
                t.Fatal("should not reach here")
            })
        }

        func BenchmarkAdd(b *testing.B) {
            for i := 0; i < b.N; i++ {
                Add(1, 2)
            }
        }
    """)
    )

    # Rust files
    (tmp_path / "rust_src").mkdir()
    (tmp_path / "rust_src" / "lib.rs").write_text(
        textwrap.dedent("""\
        pub fn multiply(a: i32, b: i32) -> i32 {
            a * b
        }
    """)
    )
    (tmp_path / "rust_src" / "lib_test.rs").write_text(
        textwrap.dedent("""\
        use super::*;

        #[test]
        fn test_multiply() {
            assert_eq!(multiply(3, 4), 12);
        }

        #[test]
        fn test_multiply_zero() {
            let result = multiply(0, 5);
            assert_eq!(result, 0);
        }

        #[tokio::test]
        async fn test_async_multiply() {
            assert!(multiply(2, 3) > 0);
        }
    """)
    )

    # node_modules (should be excluded)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("module.exports = {}")

    return tmp_path


@pytest.fixture()
def ws(workspace: Path) -> str:
    return str(workspace)


# Dataflow workspace for trace_variable tests
@pytest.fixture()
def dataflow_ws(tmp_path: Path) -> str:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "router.py").write_text(
        textwrap.dedent("""\
        from app.service import process_loan

        def create_loan(request):
            loan_id = request.json["loan_id"]
            customer = request.json["customer"]
            result = process_loan(loan_id, customer)
            return {"status": "ok", "data": result}
    """)
    )
    (tmp_path / "app" / "service.py").write_text(
        textwrap.dedent("""\
        from app.repository import get_loan, save_audit

        def process_loan(loan_id, customer_name):
            lid = loan_id
            loan = get_loan(lid)
            save_audit(lid, "processed")
            return loan

        def helper():
            pass
    """)
    )
    (tmp_path / "app" / "repository.py").write_text(
        textwrap.dedent("""\
        def get_loan(loan_identifier):
            return Loan.query.filter(Loan.id == loan_identifier).first()

        def save_audit(ref_id, action):
            db.execute("INSERT INTO audit (ref, action) VALUES (%s, %s)", (ref_id, action))
    """)
    )
    return str(tmp_path)


# Side-effect workspace for detect_patterns / compressed_view
@pytest.fixture()
def effects_ws(tmp_path: Path) -> str:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "payment.py").write_text(
        textwrap.dedent("""\
        import requests

        class PaymentService:
            def charge(self, amount, card_token):
                session.add(Payment(amount=amount))
                session.commit()
                requests.post("https://api.stripe.com/charges", json={"amount": amount})
                return {"charged": amount}

            def refund(self, payment_id):
                db.execute("UPDATE payments SET refunded=1 WHERE id=%s", (payment_id,))
                send_email(payment_id, "refund processed")
                return True

        @retry(max_retries=3)
        def with_retry():
            pass
    """)
    )
    (tmp_path / "app" / "auth.py").write_text(
        textwrap.dedent("""\
        def generate_token(user_id):
            token = jwt.encode({"sub": user_id})
            return token

        def validate_token(token):
            return jwt.decode(token)
    """)
    )
    return str(tmp_path)


# =========================================================================
# Python-only tests (always run)
# =========================================================================


class TestGetDependenciesPython:
    def test_success(self, ws):
        result = get_dependencies(ws, "app/main.py")
        assert result.success
        assert isinstance(result.data, list)
        dep_files = {d["file_path"] for d in result.data}
        assert "app/service.py" in dep_files

    def test_each_dep_has_required_fields(self, ws):
        result = get_dependencies(ws, "app/main.py")
        for dep in result.data:
            assert "file_path" in dep
            assert "symbols" in dep
            assert "weight" in dep
            assert isinstance(dep["symbols"], list)
            assert isinstance(dep["weight"], int)
            assert dep["weight"] >= 1

    def test_ts_dependencies(self, ws):
        result = get_dependencies(ws, "src/index.ts")
        assert result.success
        dep_files = {d["file_path"] for d in result.data}
        assert any("utils" in f for f in dep_files)

    def test_nonexistent_file(self, ws):
        result = get_dependencies(ws, "does_not_exist.py")
        # Python returns success=True with empty data (graph has no edges for unknown file)
        assert result.data == [] or not result.success


class TestGetDependentsPython:
    def test_success(self, ws):
        result = get_dependents(ws, "app/service.py")
        assert result.success
        assert isinstance(result.data, list)
        dep_files = {d["file_path"] for d in result.data}
        assert "app/main.py" in dep_files

    def test_each_dep_has_required_fields(self, ws):
        result = get_dependents(ws, "app/service.py")
        for dep in result.data:
            assert "file_path" in dep
            assert "symbols" in dep
            assert "weight" in dep

    def test_utils_dependents(self, ws):
        result = get_dependents(ws, "app/utils.py")
        assert result.success
        dep_files = {d["file_path"] for d in result.data}
        assert "app/service.py" in dep_files


class TestTestOutlinePython:
    def test_python_outline(self, ws):
        result = outline_tests(ws, path="tests/test_service.py")
        assert result.success
        assert isinstance(result.data, list)
        names = [e["name"] for e in result.data]
        assert any("TestMyService" in n for n in names)

    def test_python_entry_fields(self, ws):
        result = outline_tests(ws, path="tests/test_service.py")
        for entry in result.data:
            assert "name" in entry
            assert "kind" in entry
            assert "line_number" in entry
            assert "mocks" in entry
            assert "assertions" in entry
            assert isinstance(entry["mocks"], list)
            assert isinstance(entry["assertions"], list)

    def test_python_mocks_detected(self, ws):
        result = outline_tests(ws, path="tests/test_service.py")
        mocked = [e for e in result.data if "test_process_mocked" in e["name"]]
        assert len(mocked) >= 1
        assert len(mocked[0]["mocks"]) >= 1
        assert any("app.service.helper" in m for m in mocked[0]["mocks"])

    def test_python_assertions_detected(self, ws):
        result = outline_tests(ws, path="tests/test_service.py")
        has_asserts = [e for e in result.data if e.get("assertions")]
        assert len(has_asserts) >= 1

    def test_ts_outline(self, ws):
        result = outline_tests(ws, path="src/utils.test.ts")
        assert result.success
        kinds = {e["kind"] for e in result.data}
        assert "describe_block" in kinds or "test_function" in kinds

    def test_java_outline(self, ws):
        result = outline_tests(ws, path="src/test/java/AuthServiceTest.java")
        assert result.success
        names = [e["name"] for e in result.data]
        assert any("testAuthenticate" in n for n in names)
        assert any("AuthServiceTest::" in n for n in names)

    def test_java_mocks(self, ws):
        result = outline_tests(ws, path="src/test/java/AuthServiceTest.java")
        mock_test = [e for e in result.data if "testWithMock" in e["name"]]
        assert len(mock_test) >= 1
        assert len(mock_test[0]["mocks"]) >= 1

    def test_go_outline(self, ws):
        result = outline_tests(ws, path="pkg/calc_test.go")
        assert result.success
        names = [e["name"] for e in result.data]
        assert "TestAdd" in names
        assert "BenchmarkAdd" in names

    def test_go_assertions(self, ws):
        result = outline_tests(ws, path="pkg/calc_test.go")
        test_add = [e for e in result.data if e["name"] == "TestAdd"]
        assert len(test_add) >= 1
        assert len(test_add[0]["assertions"]) >= 1

    def test_rust_outline(self, ws):
        result = outline_tests(ws, path="rust_src/lib_test.rs")
        assert result.success
        names = [e["name"] for e in result.data]
        assert "test_multiply" in names
        assert "test_multiply_zero" in names

    def test_nonexistent(self, ws):
        result = outline_tests(ws, path="nonexistent.py")
        assert not result.success


class TestCompressedViewPython:
    def test_success(self, ws):
        result = compressed_view(ws, "app/service.py")
        assert result.success
        data = result.data
        assert "content" in data
        assert "path" in data
        assert "total_lines" in data
        assert "symbol_count" in data
        assert isinstance(data["total_lines"], int)
        assert isinstance(data["symbol_count"], int)

    def test_content_has_signatures(self, ws):
        result = compressed_view(ws, "app/service.py")
        content = result.data["content"]
        assert "MyService" in content
        assert "orchestrate" in content

    def test_content_has_calls(self, ws):
        result = compressed_view(ws, "app/service.py")
        content = result.data["content"]
        assert "calls:" in content
        assert "helper()" in content

    def test_side_effects_detected(self, effects_ws):
        result = compressed_view(effects_ws, "app/payment.py")
        content = result.data["content"]
        assert "side_effects:" in content

    def test_raises_detected(self, ws):
        # Create a file with raise statements
        (Path(ws) / "app" / "errors.py").write_text(
            textwrap.dedent("""\
            def validate(x):
                if x < 0:
                    raise ValueError("negative")
                return x
        """)
        )
        result = compressed_view(ws, "app/errors.py")
        content = result.data["content"]
        assert "raises:" in content
        assert "ValueError" in content

    def test_focus_filter(self, ws):
        result = compressed_view(ws, "app/service.py", focus="orchestrate")
        assert result.success
        assert result.data["symbol_count"] >= 1
        content = result.data["content"]
        assert "orchestrate" in content

    def test_nonexistent(self, ws):
        result = compressed_view(ws, "nonexistent.py")
        assert not result.success


class TestModuleSummaryPython:
    def test_success(self, ws):
        result = module_summary(ws, "app")
        assert result.success
        data = result.data
        assert "content" in data
        assert "file_count" in data
        assert "loc" in data
        assert data["file_count"] >= 3  # main.py, service.py, utils.py
        assert data["loc"] > 0

    def test_content_has_classes(self, ws):
        result = module_summary(ws, "app")
        content = result.data["content"]
        assert "MyService" in content

    def test_content_has_files_list(self, ws):
        result = module_summary(ws, "app")
        content = result.data["content"]
        assert "Files (" in content
        assert "lines)" in content

    def test_not_directory(self, ws):
        result = module_summary(ws, "app/main.py")
        assert not result.success

    def test_nonexistent(self, ws):
        result = module_summary(ws, "nonexistent_dir")
        assert not result.success


class TestTraceVariablePython:
    def test_forward_aliases(self, dataflow_ws):
        result = trace_variable(
            dataflow_ws,
            variable_name="loan_id",
            file="app/service.py",
            function_name="process_loan",
            direction="forward",
        )
        assert result.success
        data = result.data
        assert data["variable"] == "loan_id"
        assert data["function"] == "process_loan"
        assert data["direction"] == "forward"
        aliases = [a["name"] for a in data["aliases"]]
        assert "lid" in aliases

    def test_forward_alias_fields(self, dataflow_ws):
        result = trace_variable(
            dataflow_ws,
            variable_name="loan_id",
            file="app/service.py",
            function_name="process_loan",
            direction="forward",
        )
        for alias in result.data["aliases"]:
            assert "name" in alias
            assert "line" in alias
            assert "expression" in alias

    def test_forward_flows_to(self, dataflow_ws):
        result = trace_variable(
            dataflow_ws,
            variable_name="loan_id",
            file="app/service.py",
            function_name="process_loan",
            direction="forward",
        )
        flows = result.data["flows_to"]
        callee_names = [f["callee_function"] for f in flows]
        assert "get_loan" in callee_names
        assert "save_audit" in callee_names

    def test_forward_flows_to_fields(self, dataflow_ws):
        result = trace_variable(
            dataflow_ws,
            variable_name="loan_id",
            file="app/service.py",
            function_name="process_loan",
            direction="forward",
        )
        for flow in result.data["flows_to"]:
            assert "callee_function" in flow
            assert "as_parameter" in flow
            assert "arg_expression" in flow
            assert "call_line" in flow

    def test_forward_sinks_orm(self, dataflow_ws):
        result = trace_variable(
            dataflow_ws,
            variable_name="loan_identifier",
            file="app/repository.py",
            function_name="get_loan",
            direction="forward",
        )
        assert result.success
        sinks = result.data["sinks"]
        assert len(sinks) >= 1
        assert any(s["kind"] == "orm_filter" for s in sinks)

    def test_forward_sinks_sql(self, dataflow_ws):
        result = trace_variable(
            dataflow_ws,
            variable_name="ref_id",
            file="app/repository.py",
            function_name="save_audit",
            direction="forward",
        )
        assert result.success
        sinks = result.data["sinks"]
        assert any(s["kind"] == "sql_param" for s in sinks)

    def test_sink_fields(self, dataflow_ws):
        result = trace_variable(
            dataflow_ws,
            variable_name="loan_identifier",
            file="app/repository.py",
            function_name="get_loan",
            direction="forward",
        )
        for sink in result.data["sinks"]:
            assert "kind" in sink
            assert "expression" in sink
            assert "line" in sink

    def test_backward_sources(self, dataflow_ws):
        result = trace_variable(
            dataflow_ws,
            variable_name="loan_id",
            file="app/router.py",
            function_name="create_loan",
            direction="backward",
        )
        assert result.success
        sources = result.data["sources"]
        assert len(sources) >= 1

    def test_auto_detect_function(self, dataflow_ws):
        result = trace_variable(dataflow_ws, variable_name="loan_id", file="app/router.py", direction="forward")
        assert result.success
        assert result.data["function"] == "create_loan"

    def test_data_shape(self, dataflow_ws):
        result = trace_variable(
            dataflow_ws,
            variable_name="loan_id",
            file="app/service.py",
            function_name="process_loan",
            direction="forward",
        )
        data = result.data
        assert "variable" in data
        assert "file" in data
        assert "function" in data
        assert "direction" in data
        assert "aliases" in data
        assert "flows_to" in data
        assert "sinks" in data
        assert "flows_from" in data
        assert "sources" in data

    def test_function_not_found(self, dataflow_ws):
        result = trace_variable(dataflow_ws, variable_name="x", file="app/service.py", function_name="nonexistent")
        assert not result.success

    def test_file_not_found(self, dataflow_ws):
        result = trace_variable(dataflow_ws, variable_name="x", file="nonexistent.py", function_name="foo")
        assert not result.success


class TestDetectPatternsPython:
    def test_success(self, effects_ws):
        result = detect_patterns(effects_ws)
        assert result.success
        data = result.data
        assert "summary" in data
        assert "total_matches" in data
        assert "categories_scanned" in data
        assert "files_scanned" in data
        assert "matches" in data

    def test_detects_side_effects(self, effects_ws):
        result = detect_patterns(effects_ws)
        total = result.data["total_matches"]
        assert total >= 1

    def test_detects_retry(self, effects_ws):
        result = detect_patterns(effects_ws, categories=["retry"])
        summary = result.data["summary"]
        assert summary.get("retry", 0) >= 1

    def test_detects_token_lifecycle(self, effects_ws):
        result = detect_patterns(effects_ws, categories=["token_lifecycle"])
        summary = result.data["summary"]
        assert summary.get("token_lifecycle", 0) >= 1

    def test_match_entry_fields(self, effects_ws):
        result = detect_patterns(effects_ws)
        for _, matches in result.data["matches"].items():
            for match in matches:
                assert "file" in match
                assert "line" in match
                assert "pattern" in match
                assert "snippet" in match

    def test_category_filter(self, effects_ws):
        result = detect_patterns(effects_ws, categories=["retry"])
        assert "retry" in result.data["categories_scanned"]

    def test_invalid_category(self, effects_ws):
        result = detect_patterns(effects_ws, categories=["nonexistent_category"])
        assert not result.success

    def test_path_filter(self, effects_ws):
        result = detect_patterns(effects_ws, path="app/auth.py")
        assert result.success
        assert result.data["files_scanned"] == 1

    def test_nonexistent_path(self, effects_ws):
        result = detect_patterns(effects_ws, path="nonexistent")
        assert not result.success


# =========================================================================
# TS parity tests — run same inputs through TypeScript and compare structure
# =========================================================================


@pytest.mark.skipif(
    not RUNNER_SCRIPT.is_file(), reason="TS runner script not found — run 'npm run compile' in extension/ first"
)
class TestGetDependenciesTS:
    def test_same_structure(self, ws):
        py = get_dependencies(ws, "app/main.py")
        ts = run_ts_tool("get_dependencies", ws, {"file_path": "app/main.py"})

        assert py.success == ts["success"]
        assert isinstance(ts["data"], list)
        # Both should find app/service.py
        py_files = {d["file_path"] for d in py.data}
        ts_files = {d["file_path"] for d in ts["data"]}
        assert "app/service.py" in py_files
        assert "app/service.py" in ts_files

    def test_dep_entry_fields(self, ws):
        ts = run_ts_tool("get_dependencies", ws, {"file_path": "app/main.py"})
        assert ts["success"]
        for dep in ts["data"]:
            assert "file_path" in dep
            assert "symbols" in dep
            assert "weight" in dep


@pytest.mark.skipif(not RUNNER_SCRIPT.is_file(), reason="TS runner script not found")
class TestGetDependentsTS:
    def test_same_structure(self, ws):
        py = get_dependents(ws, "app/service.py")
        ts = run_ts_tool("get_dependents", ws, {"file_path": "app/service.py"})

        assert py.success == ts["success"]
        py_files = {d["file_path"] for d in py.data}
        ts_files = {d["file_path"] for d in ts["data"]}
        assert "app/main.py" in py_files
        assert "app/main.py" in ts_files


@pytest.mark.skipif(not RUNNER_SCRIPT.is_file(), reason="TS runner script not found")
class TestTestOutlineTS:
    def test_python_outline_parity(self, ws):
        py = outline_tests(ws, path="tests/test_service.py")
        ts = run_ts_tool("test_outline", ws, {"path": "tests/test_service.py"})

        assert py.success == ts["success"]
        py_names = {e["name"] for e in py.data}
        ts_names = {e["name"] for e in ts["data"]}
        # Both should find the same test names
        assert any("TestMyService" in n for n in py_names)
        assert any("TestMyService" in n for n in ts_names)

    def test_entry_fields_parity(self, ws):
        ts = run_ts_tool("test_outline", ws, {"path": "tests/test_service.py"})
        assert ts["success"]
        for entry in ts["data"]:
            assert "name" in entry
            assert "kind" in entry
            assert "line_number" in entry
            assert "mocks" in entry
            assert "assertions" in entry

    def test_go_outline_parity(self, ws):
        py = outline_tests(ws, path="pkg/calc_test.go")
        ts = run_ts_tool("test_outline", ws, {"path": "pkg/calc_test.go"})
        py_names = {e["name"] for e in py.data}
        ts_names = {e["name"] for e in ts["data"]}
        assert "TestAdd" in py_names
        assert "TestAdd" in ts_names
        assert "BenchmarkAdd" in py_names
        assert "BenchmarkAdd" in ts_names


@pytest.mark.skipif(not RUNNER_SCRIPT.is_file(), reason="TS runner script not found")
class TestCompressedViewTS:
    def test_same_structure(self, ws):
        py = compressed_view(ws, "app/service.py")
        ts = run_ts_tool("compressed_view", ws, {"file_path": "app/service.py"})

        assert py.success == ts["success"]
        assert "content" in ts["data"]
        assert "total_lines" in ts["data"]
        assert "symbol_count" in ts["data"]
        # Both should have the same total_lines
        assert py.data["total_lines"] == ts["data"]["total_lines"]

    def test_content_contains_symbols(self, ws):
        ts = run_ts_tool("compressed_view", ws, {"file_path": "app/service.py"})
        assert "MyService" in ts["data"]["content"]
        assert "orchestrate" in ts["data"]["content"]


@pytest.mark.skipif(not RUNNER_SCRIPT.is_file(), reason="TS runner script not found")
class TestModuleSummaryTS:
    def test_same_structure(self, ws):
        py = module_summary(ws, "app")
        ts = run_ts_tool("module_summary", ws, {"module_path": "app"})

        assert py.success == ts["success"]
        assert "content" in ts["data"]
        assert "file_count" in ts["data"]
        assert "loc" in ts["data"]
        assert py.data["file_count"] == ts["data"]["file_count"]

    def test_content_contains_classes(self, ws):
        ts = run_ts_tool("module_summary", ws, {"module_path": "app"})
        assert "MyService" in ts["data"]["content"]


@pytest.mark.skipif(not RUNNER_SCRIPT.is_file(), reason="TS runner script not found")
class TestTraceVariableTS:
    def test_forward_data_shape(self, dataflow_ws):
        ts = run_ts_tool(
            "trace_variable",
            dataflow_ws,
            {
                "variable_name": "loan_id",
                "file": "app/service.py",
                "function_name": "process_loan",
                "direction": "forward",
            },
        )
        assert ts["success"]
        data = ts["data"]
        assert data["variable"] == "loan_id"
        assert data["function"] == "process_loan"
        assert "aliases" in data
        assert "flows_to" in data
        assert "sinks" in data

    def test_forward_aliases_parity(self, dataflow_ws):
        py = trace_variable(
            dataflow_ws,
            variable_name="loan_id",
            file="app/service.py",
            function_name="process_loan",
            direction="forward",
        )
        ts = run_ts_tool(
            "trace_variable",
            dataflow_ws,
            {
                "variable_name": "loan_id",
                "file": "app/service.py",
                "function_name": "process_loan",
                "direction": "forward",
            },
        )
        py_aliases = {a["name"] for a in py.data["aliases"]}
        ts_aliases = {a["name"] for a in ts["data"]["aliases"]}
        assert "lid" in py_aliases
        assert "lid" in ts_aliases

    def test_forward_flows_to_parity(self, dataflow_ws):
        py = trace_variable(
            dataflow_ws,
            variable_name="loan_id",
            file="app/service.py",
            function_name="process_loan",
            direction="forward",
        )
        ts = run_ts_tool(
            "trace_variable",
            dataflow_ws,
            {
                "variable_name": "loan_id",
                "file": "app/service.py",
                "function_name": "process_loan",
                "direction": "forward",
            },
        )
        py_callees = {f["callee_function"] for f in py.data["flows_to"]}
        ts_callees = {f["callee_function"] for f in ts["data"]["flows_to"]}
        assert "get_loan" in py_callees
        assert "get_loan" in ts_callees

    def test_forward_sinks_parity(self, dataflow_ws):
        py = trace_variable(
            dataflow_ws,
            variable_name="loan_identifier",
            file="app/repository.py",
            function_name="get_loan",
            direction="forward",
        )
        ts = run_ts_tool(
            "trace_variable",
            dataflow_ws,
            {
                "variable_name": "loan_identifier",
                "file": "app/repository.py",
                "function_name": "get_loan",
                "direction": "forward",
            },
        )
        py_kinds = {s["kind"] for s in py.data["sinks"]}
        ts_kinds = {s["kind"] for s in ts["data"]["sinks"]}
        assert "orm_filter" in py_kinds
        assert "orm_filter" in ts_kinds


@pytest.mark.skipif(not RUNNER_SCRIPT.is_file(), reason="TS runner script not found")
class TestDetectPatternsTS:
    def test_same_structure(self, effects_ws):
        ts = run_ts_tool("detect_patterns", effects_ws, {})
        assert ts["success"]
        data = ts["data"]
        assert "summary" in data
        assert "total_matches" in data
        assert "categories_scanned" in data
        assert "files_scanned" in data
        assert "matches" in data

    def test_detects_retry_parity(self, effects_ws):
        py = detect_patterns(effects_ws, categories=["retry"])
        ts = run_ts_tool("detect_patterns", effects_ws, {"categories": ["retry"]})
        assert py.data["summary"].get("retry", 0) >= 1
        assert ts["data"]["summary"].get("retry", 0) >= 1

    def test_detects_token_lifecycle_parity(self, effects_ws):
        py = detect_patterns(effects_ws, categories=["token_lifecycle"])
        ts = run_ts_tool("detect_patterns", effects_ws, {"categories": ["token_lifecycle"]})
        assert py.data["summary"].get("token_lifecycle", 0) >= 1
        assert ts["data"]["summary"].get("token_lifecycle", 0) >= 1
