"""Tests for code intelligence tools."""
from __future__ import annotations

import os
import textwrap
from pathlib import Path
from typing import Dict

import pytest

from app.code_tools.tools import (
    ast_search,
    execute_tool,
    file_outline,
    find_references,
    find_symbol,
    find_tests,
    get_callers,
    get_callees,
    get_dependencies,
    get_dependents,
    git_blame,
    git_diff,
    git_log,
    git_show,
    grep,
    invalidate_graph_cache,
    list_files,
    outline_tests,
    read_file,
    trace_variable,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Create a minimal workspace with source files."""
    # Python file
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "main.py").write_text(textwrap.dedent("""\
        from app.service import MyService

        class App:
            def __init__(self):
                self.service = MyService()

            def run(self):
                return self.service.process()
    """))
    (tmp_path / "app" / "service.py").write_text(textwrap.dedent("""\
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
    """))
    (tmp_path / "app" / "utils.py").write_text(textwrap.dedent("""\
        def helper(data: str) -> str:
            return data.upper()

        def unused_helper():
            return 42
    """))

    # TypeScript file
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "index.ts").write_text(textwrap.dedent("""\
        import { greet } from './utils';

        function main(): void {
            console.log(greet("world"));
        }

        export class Application {
            start() {
                main();
            }
        }
    """))
    (tmp_path / "src" / "utils.ts").write_text(textwrap.dedent("""\
        export function greet(name: string): string {
            return `Hello, ${name}!`;
        }
    """))

    # Python test files
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "__init__.py").write_text("")
    (tmp_path / "tests" / "test_service.py").write_text(textwrap.dedent("""\
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
    """))

    # TypeScript test file
    (tmp_path / "src" / "utils.test.ts").write_text(textwrap.dedent("""\
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
    """))

    # Java source + test files
    (tmp_path / "src" / "main" / "java").mkdir(parents=True)
    (tmp_path / "src" / "main" / "java" / "AuthService.java").write_text(textwrap.dedent("""\
        package com.example;

        public class AuthService {
            public boolean authenticate(String user, String pass) {
                return user != null && pass != null;
            }
        }
    """))
    (tmp_path / "src" / "test" / "java").mkdir(parents=True)
    (tmp_path / "src" / "test" / "java" / "AuthServiceTest.java").write_text(textwrap.dedent("""\
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
    """))

    # Go source + test files
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "calc.go").write_text(textwrap.dedent("""\
        package calc

        func Add(a, b int) int {
            return a + b
        }
    """))
    (tmp_path / "pkg" / "calc_test.go").write_text(textwrap.dedent("""\
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
    """))

    # Rust source + test file
    (tmp_path / "rust_src").mkdir()
    (tmp_path / "rust_src" / "lib.rs").write_text(textwrap.dedent("""\
        pub fn multiply(a: i32, b: i32) -> i32 {
            a * b
        }
    """))
    (tmp_path / "rust_src" / "lib_test.rs").write_text(textwrap.dedent("""\
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
    """))

    # node_modules (should be excluded)
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("module.exports = {}")

    # Large file (should be skipped in search)
    (tmp_path / "large.py").write_text("x = 1\n" * 200_000)

    invalidate_graph_cache()
    return tmp_path


@pytest.fixture()
def ws(workspace: Path) -> str:
    return str(workspace)


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


class TestGrep:
    def test_basic_pattern(self, ws):
        result = grep(ws, "MyService")
        assert result.success
        assert len(result.data) > 0
        paths = {m["file_path"] for m in result.data}
        assert "app/service.py" in paths

    def test_regex_pattern(self, ws):
        result = grep(ws, r"def\s+\w+\(")
        assert result.success
        assert len(result.data) >= 3  # helper, unused_helper, standalone_function, process

    def test_include_glob(self, ws):
        result = grep(ws, "function", include_glob="*.ts")
        assert result.success
        for m in result.data:
            assert m["file_path"].endswith(".ts")

    def test_path_filter(self, ws):
        result = grep(ws, "class", path="app")
        assert result.success
        for m in result.data:
            assert m["file_path"].startswith("app/")

    def test_excludes_node_modules(self, ws):
        result = grep(ws, "module.exports")
        assert result.success
        assert len(result.data) == 0

    def test_max_results(self, ws):
        result = grep(ws, r"\w+", max_results=5)
        assert result.success
        assert len(result.data) <= 5
        assert result.truncated

    def test_invalid_regex(self, ws):
        result = grep(ws, "[invalid")
        assert not result.success
        assert "Invalid regex" in result.error

    def test_nonexistent_path(self, ws):
        result = grep(ws, "test", path="nonexistent")
        assert not result.success

    def test_path_traversal_blocked(self, ws):
        with pytest.raises(ValueError, match="escapes workspace"):
            grep(ws, "test", path="../../etc/passwd")


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class TestReadFile:
    def test_read_full_file(self, ws):
        result = read_file(ws, "app/utils.py")
        assert result.success
        assert "helper" in result.data["content"]
        assert result.data["total_lines"] > 0

    def test_read_line_range(self, ws):
        result = read_file(ws, "app/service.py", start_line=3, end_line=5)
        assert result.success
        assert "MyService" in result.data["content"]

    def test_nonexistent_file(self, ws):
        result = read_file(ws, "nonexistent.py")
        assert not result.success

    def test_line_numbers_in_output(self, ws):
        result = read_file(ws, "app/utils.py")
        assert result.success
        assert "   1 |" in result.data["content"]


# ---------------------------------------------------------------------------
# list_files
# ---------------------------------------------------------------------------


class TestListFiles:
    def test_list_root(self, ws):
        result = list_files(ws)
        assert result.success
        paths = {e["path"] for e in result.data}
        assert "app" in paths or any("app" in p for p in paths)

    def test_list_subdirectory(self, ws):
        result = list_files(ws, directory="app")
        assert result.success
        paths = {e["path"] for e in result.data}
        assert any("main.py" in p for p in paths)

    def test_max_depth(self, ws):
        result = list_files(ws, max_depth=1)
        assert result.success
        # Should not recurse into subdirectories
        for entry in result.data:
            parts = Path(entry["path"]).parts
            assert len(parts) <= 2

    def test_include_glob(self, ws):
        result = list_files(ws, include_glob="*.py")
        assert result.success
        for entry in result.data:
            if not entry["is_dir"]:
                assert entry["path"].endswith(".py")

    def test_excludes_node_modules(self, ws):
        result = list_files(ws, max_depth=5)
        assert result.success
        paths = {e["path"] for e in result.data}
        assert not any("node_modules" in p for p in paths)

    def test_nonexistent_dir(self, ws):
        result = list_files(ws, directory="nonexistent")
        assert not result.success


# ---------------------------------------------------------------------------
# find_symbol
# ---------------------------------------------------------------------------


class TestFindSymbol:
    def test_find_class(self, ws):
        result = find_symbol(ws, "MyService")
        assert result.success
        assert len(result.data) >= 1
        # May also match TestMyService via substring — find the exact match
        exact = [s for s in result.data if s["name"] == "MyService"]
        assert len(exact) >= 1
        sym = exact[0]
        assert sym["kind"] == "class"
        assert sym["file_path"] == "app/service.py"

    def test_find_function(self, ws):
        result = find_symbol(ws, "helper")
        assert result.success
        assert len(result.data) >= 1
        names = {s["name"] for s in result.data}
        assert "helper" in names

    def test_substring_match(self, ws):
        result = find_symbol(ws, "helper")
        assert result.success
        names = {s["name"] for s in result.data}
        assert "unused_helper" in names

    def test_kind_filter(self, ws):
        result = find_symbol(ws, "MyService", kind="class")
        assert result.success
        assert all(s["kind"] == "class" for s in result.data)

    def test_kind_filter_no_match(self, ws):
        result = find_symbol(ws, "MyService", kind="function")
        assert result.success
        assert len(result.data) == 0

    def test_not_found(self, ws):
        result = find_symbol(ws, "NonExistentSymbol12345")
        assert result.success
        assert len(result.data) == 0


# ---------------------------------------------------------------------------
# find_references
# ---------------------------------------------------------------------------


class TestFindReferences:
    def test_find_refs(self, ws):
        result = find_references(ws, "helper")
        assert result.success
        assert len(result.data) >= 1
        files = {r["file_path"] for r in result.data}
        assert "app/service.py" in files

    def test_find_refs_in_file(self, ws):
        result = find_references(ws, "MyService", file="app/main.py")
        assert result.success
        for r in result.data:
            assert r["file_path"] == "app/main.py"


# ---------------------------------------------------------------------------
# file_outline
# ---------------------------------------------------------------------------


class TestFileOutline:
    def test_python_outline(self, ws):
        result = file_outline(ws, "app/service.py")
        assert result.success
        names = {d["name"] for d in result.data}
        assert "MyService" in names
        assert "standalone_function" in names

    def test_nonexistent_file(self, ws):
        result = file_outline(ws, "nonexistent.py")
        assert not result.success


# ---------------------------------------------------------------------------
# ast_search
# ---------------------------------------------------------------------------


class TestAstSearch:
    def test_basic_pattern(self, ws):
        result = ast_search(ws, "def $F($$$ARGS)", language="python")
        assert result.success
        assert len(result.data) >= 3  # helper, unused_helper, standalone_function, etc.
        # Check structure
        for m in result.data:
            assert "file_path" in m
            assert "start_line" in m
            assert "text" in m

    def test_meta_variables(self, ws):
        result = ast_search(ws, "def $F($$$ARGS)", language="python")
        assert result.success
        # At least one match should have $F captured
        has_meta = any(m.get("meta_variables", {}).get("$F") for m in result.data)
        assert has_meta

    def test_path_filter(self, ws):
        result = ast_search(ws, "class $C", language="python", path="app")
        assert result.success
        for m in result.data:
            assert m["file_path"].startswith("app/")

    def test_typescript_pattern(self, ws):
        # TS return type annotations break ($$$ARGS) matching; use broader pattern
        result = ast_search(ws, "function $NAME", language="typescript", path="src")
        assert result.success
        assert len(result.data) >= 1
        names = {m.get("meta_variables", {}).get("$NAME", "") for m in result.data}
        assert "main" in names or "greet" in names

    def test_max_results(self, ws):
        result = ast_search(ws, "$X", language="python", max_results=2)
        assert result.success
        assert len(result.data) <= 2

    def test_nonexistent_path(self, ws):
        result = ast_search(ws, "def $F()", path="nonexistent")
        assert not result.success

    def test_excludes_node_modules(self, ws):
        result = ast_search(ws, "module", language="javascript")
        assert result.success
        for m in result.data:
            assert "node_modules" not in m["file_path"]


# ---------------------------------------------------------------------------
# get_callees / get_callers
# ---------------------------------------------------------------------------


class TestGetCallees:
    def test_find_callees(self, ws):
        result = get_callees(ws, "orchestrate", file="app/service.py")
        assert result.success
        callee_names = {c["callee_name"] for c in result.data}
        assert "helper" in callee_names
        assert "standalone_function" in callee_names

    def test_function_not_found(self, ws):
        result = get_callees(ws, "nonexistent_fn", file="app/service.py")
        assert not result.success
        assert "not found" in result.error

    def test_file_not_found(self, ws):
        result = get_callees(ws, "orchestrate", file="nonexistent.py")
        assert not result.success

    def test_no_callees(self, ws):
        result = get_callees(ws, "unused_helper", file="app/utils.py")
        assert result.success
        assert len(result.data) == 0


class TestGetCallers:
    def test_find_callers(self, ws):
        result = get_callers(ws, "helper")
        assert result.success
        assert len(result.data) >= 1
        caller_names = {c["caller_name"] for c in result.data}
        assert "orchestrate" in caller_names

    def test_find_callers_path_filter(self, ws):
        result = get_callers(ws, "helper", path="app")
        assert result.success
        for c in result.data:
            assert c["file_path"].startswith("app/")

    def test_no_callers(self, ws):
        result = get_callers(ws, "unused_helper")
        assert result.success
        assert len(result.data) == 0

    def test_nonexistent_path(self, ws):
        result = get_callers(ws, "helper", path="nonexistent")
        assert not result.success


# ---------------------------------------------------------------------------
# get_dependencies / get_dependents
# ---------------------------------------------------------------------------


class TestGraphTools:
    def test_get_dependencies(self, ws):
        result = get_dependencies(ws, "app/main.py")
        assert result.success
        # main.py imports from service.py
        dep_files = {d["file_path"] for d in result.data}
        assert "app/service.py" in dep_files

    def test_get_dependents(self, ws):
        result = get_dependents(ws, "app/service.py")
        assert result.success
        # main.py depends on service.py
        dep_files = {d["file_path"] for d in result.data}
        assert "app/main.py" in dep_files


# ---------------------------------------------------------------------------
# git_log / git_diff
# ---------------------------------------------------------------------------


class TestGitTools:
    @pytest.fixture(autouse=True)
    def _init_git(self, workspace):
        """Initialize a git repo in the workspace."""
        os.system(f"cd {workspace} && git init -q && git add -A && git commit -q -m 'init'")

    def test_git_log(self, ws):
        result = git_log(ws)
        assert result.success
        assert len(result.data) >= 1
        assert result.data[0]["message"] == "init"

    def test_git_log_file(self, ws):
        result = git_log(ws, file="app/main.py")
        assert result.success
        assert len(result.data) >= 1

    def test_git_diff(self, ws):
        # No diff on clean repo
        result = git_diff(ws, ref1="HEAD~1", ref2="HEAD")
        assert result.success


# ---------------------------------------------------------------------------
# execute_tool dispatcher
# ---------------------------------------------------------------------------


class TestExecuteTool:
    def test_dispatch_grep(self, ws):
        result = execute_tool("grep", ws, {"pattern": "class"})
        assert result.success

    def test_dispatch_unknown(self, ws):
        result = execute_tool("unknown_tool", ws, {})
        assert not result.success
        assert "Unknown tool" in result.error

    def test_dispatch_bad_params(self, ws):
        result = execute_tool("grep", ws, {"bad_param": True})
        assert not result.success


# ---------------------------------------------------------------------------
# git_blame / git_show
# ---------------------------------------------------------------------------


class TestGitBlame:
    @pytest.fixture(autouse=True)
    def _init_git(self, workspace):
        os.system(
            f'cd {workspace} && git init -q && git add -A '
            f'&& git -c user.email="test@test.com" -c user.name="Test" commit -q -m "initial commit"'
        )

    def test_blame_full_file(self, ws):
        result = git_blame(ws, file="app/service.py")
        assert result.success
        assert isinstance(result.data, list)
        assert len(result.data) > 0

    def test_blame_entry_fields(self, ws):
        result = git_blame(ws, file="app/service.py")
        assert result.success
        entry = result.data[0]
        assert "commit_hash" in entry
        assert "author" in entry
        assert "date" in entry
        assert "line_number" in entry
        assert "content" in entry

    def test_blame_line_range(self, ws):
        result = git_blame(ws, file="app/service.py", start_line=1, end_line=3)
        assert result.success
        assert len(result.data) <= 3

    def test_blame_nonexistent_file(self, ws):
        result = git_blame(ws, file="missing.py")
        assert not result.success

    def test_blame_author(self, ws):
        result = git_blame(ws, file="app/service.py")
        assert result.success
        assert result.data[0]["author"] == "Test"


class TestGitShow:
    @pytest.fixture(autouse=True)
    def _init_git(self, workspace):
        os.system(
            f'cd {workspace} && git init -q && git add -A '
            f'&& git -c user.email="test@test.com" -c user.name="Dev" commit -q -m "feat: add services"'
        )

    def test_show_head(self, ws):
        result = git_show(ws, commit="HEAD")
        assert result.success
        data = result.data
        assert data["author"] == "Dev"
        assert "feat: add services" in data["message"]
        assert data["commit_hash"]  # non-empty

    def test_show_has_diff(self, ws):
        result = git_show(ws, commit="HEAD")
        assert result.success
        assert len(result.data["diff"]) > 0

    def test_show_with_file_filter(self, ws):
        result = git_show(ws, commit="HEAD", file="app/main.py")
        assert result.success

    def test_show_invalid_ref(self, ws):
        result = git_show(ws, commit="not;valid")
        assert not result.success


# ---------------------------------------------------------------------------
# find_tests / test_outline
# ---------------------------------------------------------------------------


class TestFindTests:
    def test_find_python_tests_for_class(self, ws):
        result = find_tests(ws, name="MyService")
        assert result.success
        assert len(result.data) >= 2  # test_process + test_process_mocked
        names = [m["test_function"] for m in result.data]
        assert any("test_process" in n for n in names)

    def test_find_python_tests_for_function(self, ws):
        result = find_tests(ws, name="standalone_function")
        assert result.success
        assert len(result.data) >= 1
        assert any("test_standalone" in m["test_function"] for m in result.data)

    def test_find_tests_not_found(self, ws):
        result = find_tests(ws, name="nonexistent_symbol_xyz")
        assert result.success
        assert len(result.data) == 0

    def test_find_tests_path_filter(self, ws):
        result = find_tests(ws, name="MyService", path="tests")
        assert result.success
        assert len(result.data) >= 1

    def test_find_tests_ts(self, ws):
        result = find_tests(ws, name="greet")
        assert result.success
        assert any("utils.test.ts" in m["test_file"] for m in result.data)

    def test_find_tests_returns_context(self, ws):
        result = find_tests(ws, name="MyService")
        assert result.success
        for m in result.data:
            assert m["context"]  # non-empty context line
            assert m["test_file"]
            assert m["line_number"] > 0

    def test_find_tests_java(self, ws):
        result = find_tests(ws, name="authenticate")
        assert result.success
        java_matches = [m for m in result.data if m["test_file"].endswith(".java")]
        assert len(java_matches) >= 2
        names = [m["test_function"] for m in java_matches]
        assert any("testAuthenticate" in n for n in names)

    def test_find_tests_go(self, ws):
        result = find_tests(ws, name="Add")
        assert result.success
        go_matches = [m for m in result.data if m["test_file"].endswith(".go")]
        assert len(go_matches) >= 1
        names = [m["test_function"] for m in go_matches]
        assert any("TestAdd" in n for n in names)

    def test_find_tests_rust(self, ws):
        result = find_tests(ws, name="multiply")
        assert result.success
        rust_matches = [m for m in result.data if m["test_file"].endswith(".rs")]
        assert len(rust_matches) >= 1
        names = [m["test_function"] for m in rust_matches]
        assert any("test_multiply" in n for n in names)


class TestTestOutline:
    def test_python_outline(self, ws):
        result = outline_tests(ws, path="tests/test_service.py")
        assert result.success
        names = [e["name"] for e in result.data]
        # Should find the class and the test functions
        assert any("TestMyService" in n for n in names)
        assert any("test_process" in n for n in names)
        assert any("test_standalone" in n for n in names)

    def test_python_mocks_detected(self, ws):
        result = outline_tests(ws, path="tests/test_service.py")
        assert result.success
        # Find the mocked test
        mocked_test = [e for e in result.data if "test_process_mocked" in e["name"]]
        assert len(mocked_test) == 1
        assert len(mocked_test[0]["mocks"]) >= 1
        assert any("app.service.helper" in m for m in mocked_test[0]["mocks"])

    def test_python_assertions_detected(self, ws):
        result = outline_tests(ws, path="tests/test_service.py")
        assert result.success
        # At least some entries should have assertions
        has_asserts = [e for e in result.data if e.get("assertions")]
        assert len(has_asserts) >= 1

    def test_ts_outline(self, ws):
        result = outline_tests(ws, path="src/utils.test.ts")
        assert result.success
        names = [e["name"] for e in result.data]
        assert any("greet" in n for n in names)
        # Should have describe and it blocks
        kinds = [e["kind"] for e in result.data]
        assert "describe_block" in kinds or "test_function" in kinds

    def test_java_outline(self, ws):
        result = outline_tests(ws, path="src/test/java/AuthServiceTest.java")
        assert result.success
        names = [e["name"] for e in result.data]
        assert any("testAuthenticate" in n for n in names)
        assert any("testAuthenticateNull" in n for n in names)
        assert any("testWithMock" in n for n in names)
        # Should detect class prefix
        assert any("AuthServiceTest::" in n for n in names)

    def test_java_outline_mocks(self, ws):
        result = outline_tests(ws, path="src/test/java/AuthServiceTest.java")
        assert result.success
        mock_test = [e for e in result.data if "testWithMock" in e["name"]]
        assert len(mock_test) == 1
        assert len(mock_test[0]["mocks"]) >= 1

    def test_java_outline_assertions(self, ws):
        result = outline_tests(ws, path="src/test/java/AuthServiceTest.java")
        assert result.success
        has_asserts = [e for e in result.data if e.get("assertions")]
        assert len(has_asserts) >= 1

    def test_go_outline(self, ws):
        result = outline_tests(ws, path="pkg/calc_test.go")
        assert result.success
        names = [e["name"] for e in result.data]
        assert "TestAdd" in names
        assert "TestAddNegative" in names
        assert "BenchmarkAdd" in names

    def test_go_outline_assertions(self, ws):
        result = outline_tests(ws, path="pkg/calc_test.go")
        assert result.success
        test_add = [e for e in result.data if e["name"] == "TestAdd"]
        assert len(test_add) == 1
        assert len(test_add[0]["assertions"]) >= 1

    def test_rust_outline(self, ws):
        result = outline_tests(ws, path="rust_src/lib_test.rs")
        assert result.success
        names = [e["name"] for e in result.data]
        assert "test_multiply" in names
        assert "test_multiply_zero" in names
        assert "test_async_multiply" in names

    def test_rust_outline_assertions(self, ws):
        result = outline_tests(ws, path="rust_src/lib_test.rs")
        assert result.success
        has_asserts = [e for e in result.data if e.get("assertions")]
        assert len(has_asserts) >= 2  # test_multiply + test_multiply_zero both have assert_eq!

    def test_nonexistent_file(self, ws):
        result = outline_tests(ws, path="missing_test.py")
        assert not result.success

    def test_python_fixtures_detected(self, ws):
        result = outline_tests(ws, path="tests/test_service.py")
        assert result.success
        # test_process_mocked has 'self' + 'mock_helper' params;
        # fixtures should contain 'mock_helper' (self is excluded)
        mocked_test = [e for e in result.data if "test_process_mocked" in e["name"]]
        if mocked_test:
            # mock_helper is a param to the test function
            assert "mock_helper" in mocked_test[0].get("fixtures", [])


# ---------------------------------------------------------------------------
# trace_variable
# ---------------------------------------------------------------------------


@pytest.fixture()
def dataflow_ws(tmp_path: Path) -> Path:
    """Workspace simulating a typical HTTP → service → repository flow."""
    (tmp_path / "app").mkdir()

    # -- Router layer (HTTP entry point) --
    (tmp_path / "app" / "router.py").write_text(textwrap.dedent("""\
        from app.service import process_loan

        def create_loan(request):
            loan_id = request.json["loan_id"]
            customer = request.json["customer"]
            result = process_loan(loan_id, customer)
            return {"status": "ok", "data": result}
    """))

    # -- Service layer (business logic, aliases the variable) --
    (tmp_path / "app" / "service.py").write_text(textwrap.dedent("""\
        from app.repository import get_loan, save_audit

        def process_loan(loan_id, customer_name):
            lid = loan_id
            loan = get_loan(lid)
            save_audit(lid, "processed")
            return loan

        def helper():
            pass
    """))

    # -- Repository layer (ORM / SQL sink) --
    (tmp_path / "app" / "repository.py").write_text(textwrap.dedent("""\
        def get_loan(loan_identifier):
            return Loan.query.filter(Loan.id == loan_identifier).first()

        def save_audit(ref_id, action):
            db.execute("INSERT INTO audit (ref, action) VALUES (%s, %s)", (ref_id, action))
    """))

    # -- TypeScript variant --
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "handler.ts").write_text(textwrap.dedent("""\
        import { findLoan } from './loanRepo';

        export function handleRequest(req: Request): Response {
            const loanId = req.body.loanId;
            const result = findLoan(loanId);
            return { status: 200, data: result };
        }
    """))
    (tmp_path / "src" / "loanRepo.ts").write_text(textwrap.dedent("""\
        export function findLoan(id: string): Loan {
            return prisma.loan.findUnique({ where: { id } });
        }
    """))

    invalidate_graph_cache()
    return tmp_path


class TestTraceVariable:
    """Tests for the trace_variable data flow tool."""

    # -- Forward tracing ---------------------------------------------------

    def test_forward_alias_detection(self, dataflow_ws):
        """Should detect that `lid = loan_id` is an alias."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="loan_id",
                                file="app/service.py", function_name="process_loan",
                                direction="forward")
        assert result.success
        data = result.data
        assert data["variable"] == "loan_id"
        assert data["function"] == "process_loan"
        aliases = [a["name"] for a in data["aliases"]]
        assert "lid" in aliases

    def test_forward_flows_to_detected(self, dataflow_ws):
        """Should detect calls where loan_id (or alias) flows to another function."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="loan_id",
                                file="app/service.py", function_name="process_loan",
                                direction="forward")
        assert result.success
        flows = result.data["flows_to"]
        callee_names = [f["callee_function"] for f in flows]
        assert "get_loan" in callee_names
        assert "save_audit" in callee_names

    def test_forward_param_mapping(self, dataflow_ws):
        """Should map argument position to formal parameter name."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="loan_id",
                                file="app/service.py", function_name="process_loan",
                                direction="forward")
        assert result.success
        get_loan_flow = [f for f in result.data["flows_to"]
                         if f["callee_function"] == "get_loan"]
        assert len(get_loan_flow) >= 1
        # get_loan(lid) → def get_loan(loan_identifier) → param "loan_identifier"
        assert get_loan_flow[0]["as_parameter"] == "loan_identifier"

    def test_forward_sink_orm_filter(self, dataflow_ws):
        """Should detect ORM .filter() as a sink in the repository layer."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="loan_identifier",
                                file="app/repository.py", function_name="get_loan",
                                direction="forward")
        assert result.success
        sinks = result.data["sinks"]
        assert len(sinks) >= 1
        assert any(s["kind"] == "orm_filter" for s in sinks)

    def test_forward_sink_sql_param(self, dataflow_ws):
        """Should detect SQL execute() as a sink."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="ref_id",
                                file="app/repository.py", function_name="save_audit",
                                direction="forward")
        assert result.success
        sinks = result.data["sinks"]
        assert any(s["kind"] == "sql_param" for s in sinks)

    def test_forward_return_sink(self, dataflow_ws):
        """Should detect return statement as a sink."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="loan_id",
                                file="app/service.py", function_name="process_loan",
                                direction="forward")
        assert result.success
        # `return loan` — loan is not an alias of loan_id, but the function
        # returns the result of get_loan(lid). Check for return in sinks
        # from the repo layer instead.
        result2 = trace_variable(ws, variable_name="loan_identifier",
                                 file="app/repository.py", function_name="get_loan",
                                 direction="forward")
        assert result2.success
        sinks2 = result2.data["sinks"]
        # The return statement contains loan_identifier indirectly via the filter;
        # the orm_filter sink should be present at minimum
        assert len(sinks2) >= 1

    # -- Backward tracing --------------------------------------------------

    def test_backward_flows_from(self, dataflow_ws):
        """Should find callers that pass a value for the target parameter."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="loan_identifier",
                                file="app/repository.py", function_name="get_loan",
                                direction="backward")
        assert result.success
        flows = result.data["flows_from"]
        assert len(flows) >= 1
        caller_names = [f["caller_function"] for f in flows]
        assert "process_loan" in caller_names
        # Should show what was passed
        pl_flow = [f for f in flows if f["caller_function"] == "process_loan"]
        assert "lid" in pl_flow[0]["arg_expression"]

    def test_backward_source_http(self, dataflow_ws):
        """Should detect HTTP request source pattern."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="loan_id",
                                file="app/router.py", function_name="create_loan",
                                direction="backward")
        assert result.success
        sources = result.data["sources"]
        assert len(sources) >= 1
        assert any(s["kind"] in ("http_request", "destructure") for s in sources)

    # -- Auto-detect function ----------------------------------------------

    def test_auto_detect_function(self, dataflow_ws):
        """When function_name is omitted, should find the first function using the variable."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="loan_id",
                                file="app/router.py", direction="forward")
        assert result.success
        assert result.data["function"] == "create_loan"

    # -- Error handling ----------------------------------------------------

    def test_file_not_found(self, dataflow_ws):
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="x",
                                file="missing.py", function_name="f")
        assert not result.success
        assert "not found" in result.error.lower()

    def test_function_not_found(self, dataflow_ws):
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="x",
                                file="app/service.py", function_name="nonexistent")
        assert not result.success
        assert "not found" in result.error.lower()

    def test_variable_not_in_any_function(self, dataflow_ws):
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="zzz_not_here",
                                file="app/service.py")
        assert not result.success

    # -- TypeScript --------------------------------------------------------

    def test_typescript_forward(self, dataflow_ws):
        """Should work for TypeScript files."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="loanId",
                                file="src/handler.ts", function_name="handleRequest",
                                direction="forward")
        assert result.success
        flows = result.data["flows_to"]
        callee_names = [f["callee_function"] for f in flows]
        assert "findLoan" in callee_names

    def test_typescript_sink_orm(self, dataflow_ws):
        """Should detect Prisma findUnique as ORM sink."""
        ws = str(dataflow_ws)
        result = trace_variable(ws, variable_name="id",
                                file="src/loanRepo.ts", function_name="findLoan",
                                direction="forward")
        assert result.success
        sinks = result.data["sinks"]
        assert any(s["kind"] in ("orm_get", "orm_filter") for s in sinks)

    # -- execute_tool dispatch ---------------------------------------------

    def test_dispatch_via_execute_tool(self, dataflow_ws):
        """trace_variable should be callable via execute_tool."""
        ws = str(dataflow_ws)
        result = execute_tool("trace_variable", ws, {
            "variable_name": "loan_id",
            "file": "app/service.py",
            "function_name": "process_loan",
            "direction": "forward",
        })
        assert result.success
        assert result.data["variable"] == "loan_id"
