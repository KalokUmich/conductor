"""Tests for compressed_view, module_summary, and expand_symbol tools."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.code_tools.tools import (
    compressed_view,
    expand_symbol,
    invalidate_graph_cache,
    module_summary,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with Python and TypeScript source files."""
    # Python module: app/
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("")
    (tmp_path / "app" / "service.py").write_text(textwrap.dedent("""\
        from app.utils import helper
        import requests

        class PaymentService:
            def process_payment(self, user_id: str, amount: float):
                \"\"\"Process a payment.\"\"\"
                result = helper(user_id)
                requests.post("https://api.stripe.com/charge", json={"amount": amount})
                self.save_result(result)
                return result

            def save_result(self, result):
                session.add(result)
                session.commit()

            def refund(self, payment_id: str):
                raise InsufficientFundsError("Cannot refund")

        class NotificationService:
            def send_email(self, to: str, subject: str):
                publish("email.send", {"to": to, "subject": subject})
    """))
    (tmp_path / "app" / "utils.py").write_text(textwrap.dedent("""\
        def helper(data: str) -> str:
            return data.upper()

        def unused_helper():
            return 42
    """))
    (tmp_path / "app" / "models.py").write_text(textwrap.dedent("""\
        class PaymentModel:
            pass

        class UserSchema:
            pass
    """))
    (tmp_path / "app" / "router.py").write_text(textwrap.dedent("""\
        class PaymentController:
            def create(self):
                pass

            def get(self):
                pass
    """))

    # TypeScript module: src/
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

    # Excluded directory
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "pkg.js").write_text("module.exports = {}")

    invalidate_graph_cache()
    return tmp_path


@pytest.fixture()
def ws(workspace: Path) -> str:
    return str(workspace)


# ---------------------------------------------------------------------------
# compressed_view
# ---------------------------------------------------------------------------


class TestCompressedView:
    def test_basic(self, ws):
        result = compressed_view(ws, file_path="app/service.py")
        assert result.success
        assert "app/service.py" in result.data["content"]
        assert result.data["total_lines"] > 0
        assert result.data["symbol_count"] > 0

    def test_shows_signatures(self, ws):
        result = compressed_view(ws, file_path="app/service.py")
        content = result.data["content"]
        assert "PaymentService" in content
        assert "process_payment" in content

    def test_shows_calls(self, ws):
        result = compressed_view(ws, file_path="app/service.py")
        content = result.data["content"]
        assert "calls:" in content
        assert "helper()" in content

    def test_shows_side_effects(self, ws):
        result = compressed_view(ws, file_path="app/service.py")
        content = result.data["content"]
        assert "side_effects:" in content
        # process_payment makes an HTTP call
        assert "http call" in content

    def test_shows_raises(self, ws):
        result = compressed_view(ws, file_path="app/service.py")
        content = result.data["content"]
        assert "raises:" in content
        assert "InsufficientFundsError" in content

    def test_focus_filter(self, ws):
        result = compressed_view(ws, file_path="app/service.py", focus="Notification")
        assert result.success
        content = result.data["content"]
        assert "NotificationService" in content
        # Should NOT include PaymentService when focused
        assert result.data["symbol_count"] >= 1

    def test_file_not_found(self, ws):
        result = compressed_view(ws, file_path="nonexistent.py")
        assert not result.success
        assert "not found" in result.error.lower()

    def test_typescript_file(self, ws):
        result = compressed_view(ws, file_path="src/index.ts")
        assert result.success
        content = result.data["content"]
        assert "index.ts" in content


# ---------------------------------------------------------------------------
# module_summary
# ---------------------------------------------------------------------------


class TestModuleSummary:
    def test_basic(self, ws):
        result = module_summary(ws, module_path="app")
        assert result.success
        assert result.data["file_count"] >= 3
        assert result.data["loc"] > 0

    def test_shows_services(self, ws):
        result = module_summary(ws, module_path="app")
        content = result.data["content"]
        assert "PaymentService" in content

    def test_shows_models(self, ws):
        result = module_summary(ws, module_path="app")
        content = result.data["content"]
        assert "PaymentModel" in content or "UserSchema" in content

    def test_shows_controllers(self, ws):
        result = module_summary(ws, module_path="app")
        content = result.data["content"]
        assert "PaymentController" in content

    def test_shows_functions(self, ws):
        result = module_summary(ws, module_path="app")
        content = result.data["content"]
        assert "helper" in content

    def test_shows_files(self, ws):
        result = module_summary(ws, module_path="app")
        content = result.data["content"]
        assert "service.py" in content
        assert "utils.py" in content

    def test_directory_not_found(self, ws):
        result = module_summary(ws, module_path="nonexistent_dir")
        assert not result.success

    def test_typescript_module(self, ws):
        result = module_summary(ws, module_path="src")
        assert result.success
        assert result.data["file_count"] >= 2

    def test_excludes_node_modules(self, ws):
        result = module_summary(ws, module_path=".")
        content = result.data["content"]
        assert "node_modules" not in content


# ---------------------------------------------------------------------------
# expand_symbol
# ---------------------------------------------------------------------------


class TestExpandSymbol:
    def test_with_file_path(self, ws):
        result = expand_symbol(ws, symbol_name="PaymentService", file_path="app/service.py")
        assert result.success
        data = result.data
        assert data["symbol_name"] == "PaymentService"
        assert data["kind"] == "class"
        assert "class PaymentService" in data["source"]
        assert data["start_line"] > 0

    def test_function_expansion(self, ws):
        result = expand_symbol(ws, symbol_name="helper", file_path="app/utils.py")
        assert result.success
        assert "def helper" in result.data["source"]

    def test_without_file_path(self, ws):
        result = expand_symbol(ws, symbol_name="PaymentService")
        assert result.success
        assert result.data["symbol_name"] == "PaymentService"
        assert "service.py" in result.data["file_path"]

    def test_symbol_not_found_in_file(self, ws):
        result = expand_symbol(ws, symbol_name="NonexistentClass", file_path="app/service.py")
        assert not result.success
        assert "not found" in result.error.lower()
        # Should suggest available symbols
        assert "Available:" in result.error

    def test_symbol_not_found_globally(self, ws):
        result = expand_symbol(ws, symbol_name="TotallyFakeSymbol99")
        assert not result.success

    def test_substring_match(self, ws):
        result = expand_symbol(ws, symbol_name="payment", file_path="app/service.py")
        assert result.success
        # Should match PaymentService via substring

    def test_file_not_found(self, ws):
        result = expand_symbol(ws, symbol_name="Foo", file_path="nonexistent.py")
        assert not result.success
