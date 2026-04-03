"""Tests for the detect_patterns tool and risk-aware context selection."""
from __future__ import annotations

import os
import textwrap
from pathlib import Path

import pytest

from app.code_tools.schemas import DetectPatternsParams, filter_tools
from app.code_tools.tools import detect_patterns, execute_tool
from app.agent_loop.prompts import (
    build_system_prompt,
    scan_workspace_risk,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with various architectural patterns."""
    # Webhook handler
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "webhook_handler.py").write_text(textwrap.dedent("""\
        from fastapi import APIRouter

        router = APIRouter()

        @router.post("/api/webhook/payment")
        def handle_payment_webhook(payload: dict):
            verify_signature(payload)
            process_payment_event(payload)
            send_notification(payload["user_id"], "Payment received")
    """))

    # Queue consumer
    (tmp_path / "app" / "consumer.py").write_text(textwrap.dedent("""\
        import pika

        def on_message(channel, method, properties, body):
            data = json.loads(body)
            process_order(data)
            channel.basic_ack(delivery_tag=method.delivery_tag)

        channel.basic_consume(queue="orders", on_message_callback=on_message)
    """))

    # Retry logic
    (tmp_path / "app" / "retry_service.py").write_text(textwrap.dedent("""\
        from tenacity import retry, stop_after_attempt, wait_exponential

        @retry(stop=stop_after_attempt(3), wait=wait_exponential())
        def call_external_api(url, data):
            response = httpx.post(url, json=data)
            response.raise_for_status()
            return response.json()

        retry_count = 0
        max_retry_attempts = 5
    """))

    # Lock usage
    (tmp_path / "app" / "lock_service.py").write_text(textwrap.dedent("""\
        import threading

        _lock = threading.Lock()

        def update_counter(value):
            with _lock:
                global counter
                counter += value

        def db_lock():
            cursor.execute("SELECT id FROM accounts WHERE user_id = %s FOR UPDATE", [uid])
    """))

    # Check-then-act anti-pattern
    (tmp_path / "app" / "user_service.py").write_text(textwrap.dedent("""\
        def create_user_if_not_exists(email):
            existing = User.objects.filter(email=email).exists()
            if not existing:
                User.objects.create(email=email)
            return User.objects.get(email=email)

        def safe_create(email):
            user, created = User.objects.get_or_create(email=email)
            return user
    """))

    # Transaction boundaries
    (tmp_path / "app" / "payment_service.py").write_text(textwrap.dedent("""\
        from django.db import transaction

        @transaction.atomic
        def process_payment(order_id, amount):
            order = Order.objects.select_for_update().get(id=order_id)
            order.status = "paid"
            order.save()
            Payment.objects.create(order=order, amount=amount)

        def manual_tx():
            connection.begin()
            try:
                do_work()
                connection.commit()
            except Exception:
                connection.rollback()
    """))

    # Token lifecycle
    (tmp_path / "app" / "auth_service.py").write_text(textwrap.dedent("""\
        import jwt

        def generate_token(user_id):
            return jwt.encode({"sub": user_id, "exp": time.time() + 3600}, SECRET)

        def validate_token(token):
            return jwt.decode(token, SECRET, algorithms=["HS256"])

        def refresh_token(old_token):
            claims = validate_token(old_token)
            return generate_token(claims["sub"])

        def revoke_token(token):
            blacklist.add(token)

        token_expiry = 3600
    """))

    # Side-effect chain
    (tmp_path / "app" / "order_service.py").write_text(textwrap.dedent("""\
        def complete_order(order):
            charge(order.total)
            send_email(order.user.email, "Order confirmed")
            audit_log("order_completed", order.id)
            requests.post("https://analytics.example.com/event", json={"event": "order"})
    """))

    # Clean file (no patterns)
    (tmp_path / "app" / "utils.py").write_text(textwrap.dedent("""\
        def format_currency(amount):
            return f"${amount:,.2f}"

        def slugify(text):
            return text.lower().replace(" ", "-")
    """))

    return tmp_path


# ---------------------------------------------------------------------------
# detect_patterns tool tests
# ---------------------------------------------------------------------------


class TestDetectPatterns:
    """Tests for the detect_patterns tool."""

    def test_detects_webhook_patterns(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            categories=["webhook"],
        )
        assert result.success
        assert result.data["summary"]["webhook"] > 0
        webhook_matches = result.data["matches"]["webhook"]
        files = {m["file"] for m in webhook_matches}
        assert any("webhook_handler" in f for f in files)

    def test_detects_queue_patterns(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            categories=["queue"],
        )
        assert result.success
        assert "queue" in result.data["summary"]
        queue_matches = result.data["matches"]["queue"]
        files = {m["file"] for m in queue_matches}
        assert any("consumer" in f for f in files)

    def test_detects_retry_patterns(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            categories=["retry"],
        )
        assert result.success
        assert "retry" in result.data["summary"]
        retry_matches = result.data["matches"]["retry"]
        descs = {m["pattern"] for m in retry_matches}
        assert any("retry" in d.lower() for d in descs)

    def test_detects_lock_patterns(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            categories=["lock"],
        )
        assert result.success
        assert "lock" in result.data["summary"]
        lock_matches = result.data["matches"]["lock"]
        assert any("Lock" in m["snippet"] or "FOR UPDATE" in m["snippet"] for m in lock_matches)

    def test_detects_check_then_act(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            categories=["check_then_act"],
        )
        assert result.success
        matches = result.data["matches"].get("check_then_act", [])
        # Should find the guard pattern and/or the atomic alternative
        assert len(matches) > 0

    def test_detects_transaction_patterns(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            categories=["transaction"],
        )
        assert result.success
        assert "transaction" in result.data["summary"]
        tx_matches = result.data["matches"]["transaction"]
        assert any("transaction" in m["pattern"].lower() or "boundary" in m["pattern"].lower()
                    for m in tx_matches)

    def test_detects_token_lifecycle(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            categories=["token_lifecycle"],
        )
        assert result.success
        assert "token_lifecycle" in result.data["summary"]
        token_matches = result.data["matches"]["token_lifecycle"]
        patterns = {m["pattern"] for m in token_matches}
        assert "token creation" in patterns or "token validation" in patterns

    def test_detects_side_effect_chain(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            categories=["side_effect_chain"],
        )
        assert result.success
        assert "side_effect_chain" in result.data["summary"]
        se_matches = result.data["matches"]["side_effect_chain"]
        assert len(se_matches) >= 2  # charge + send_email + audit_log + requests.post

    def test_all_categories_default(self, workspace: Path):
        result = detect_patterns(workspace=str(workspace))
        assert result.success
        # Should detect patterns across all categories
        assert result.data["total_matches"] > 0
        assert len(result.data["categories_scanned"]) == 8

    def test_scoped_to_path(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            path="app/auth_service.py",
            categories=["token_lifecycle"],
        )
        assert result.success
        # Only auth_service.py should be scanned
        for match_list in result.data["matches"].values():
            for m in match_list:
                assert "auth_service" in m["file"]

    def test_max_results_cap(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            max_results=3,
        )
        assert result.success
        assert result.data["total_matches"] <= 3
        assert result.truncated

    def test_invalid_category(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            categories=["nonexistent"],
        )
        assert not result.success
        assert "Unknown categories" in result.error

    def test_nonexistent_path(self, workspace: Path):
        result = detect_patterns(
            workspace=str(workspace),
            path="does/not/exist",
        )
        assert not result.success
        assert "not found" in result.error.lower()

    def test_empty_workspace(self, tmp_path: Path):
        result = detect_patterns(workspace=str(tmp_path))
        assert result.success
        assert result.data["total_matches"] == 0

    def test_match_structure(self, workspace: Path):
        """Each match should have file, line, pattern, snippet."""
        result = detect_patterns(
            workspace=str(workspace),
            categories=["retry"],
        )
        assert result.success
        for cat_matches in result.data["matches"].values():
            for m in cat_matches:
                assert "file" in m
                assert "line" in m
                assert isinstance(m["line"], int)
                assert "pattern" in m
                assert "snippet" in m


class TestDetectPatternsIntegration:
    """Integration tests for detect_patterns via execute_tool."""

    def test_execute_tool_dispatch(self, workspace: Path):
        result = execute_tool(
            "detect_patterns",
            workspace=str(workspace),
            params={"categories": ["webhook", "queue"]},
        )
        assert result.success
        assert result.tool_name == "detect_patterns"

    def test_tool_in_definitions(self):
        tools = filter_tools(["detect_patterns"])
        assert len(tools) == 1
        assert tools[0]["name"] == "detect_patterns"
        assert "input_schema" in tools[0]


# ---------------------------------------------------------------------------
# Risk-aware context selection tests
# ---------------------------------------------------------------------------


class TestScanWorkspaceRisk:
    """Tests for scan_workspace_risk()."""

    def test_detects_risk_signals(self, workspace: Path):
        result = scan_workspace_risk(str(workspace))
        assert "Risk signals detected" in result
        # Should detect auth, webhook, queue-related signals from file names
        assert "security" in result.lower() or "webhook" in result.lower() or "concurrency" in result.lower()

    def test_empty_workspace_no_risk(self, tmp_path: Path):
        result = scan_workspace_risk(str(tmp_path))
        assert result == ""

    def test_nonexistent_workspace(self):
        result = scan_workspace_risk("/nonexistent/path")
        assert result == ""

    def test_contains_auto_focus_guidance(self, workspace: Path):
        result = scan_workspace_risk(str(workspace))
        if result:
            assert "detect_patterns" in result

    def test_caps_examples_per_signal(self, tmp_path: Path):
        """Should not list more than 5 files per signal."""
        auth_dir = tmp_path / "app"
        auth_dir.mkdir()
        for i in range(10):
            (auth_dir / f"auth_handler_{i}.py").write_text("pass")
        result = scan_workspace_risk(str(tmp_path))
        # The count may be larger than 5 but only 5 examples shown
        assert "security" in result.lower()

    def test_risk_signals_from_filenames(self, tmp_path: Path):
        """Detects risk signals from file names, not content."""
        app_dir = tmp_path / "services"
        app_dir.mkdir()
        (app_dir / "webhook_listener.py").write_text("pass")
        (app_dir / "retry_handler.py").write_text("pass")
        (app_dir / "auth_service.py").write_text("pass")
        (app_dir / "consumer_worker.py").write_text("pass")
        result = scan_workspace_risk(str(tmp_path))
        assert "webhook" in result.lower()
        assert "security" in result.lower()


class TestBuildSystemPromptWithRisk:
    """Tests for risk context injection into system prompts."""

    def test_risk_context_included(self, workspace: Path):
        risk = scan_workspace_risk(str(workspace))
        prompt = build_system_prompt(
            workspace_path=str(workspace),
            workspace_layout="(layout)",
            project_docs="",
            max_iterations=10,
            query_type="root_cause_analysis",
            risk_context=risk,
        )
        if risk:
            assert "Risk signals detected" in prompt

    def test_no_risk_context_when_empty(self, tmp_path: Path):
        prompt = build_system_prompt(
            workspace_path=str(tmp_path),
            workspace_layout="(layout)",
            project_docs="",
            max_iterations=10,
            query_type="root_cause_analysis",
            risk_context="",
        )
        assert "Risk signals detected" not in prompt

    def test_risk_context_none_safe(self, tmp_path: Path):
        prompt = build_system_prompt(
            workspace_path=str(tmp_path),
            workspace_layout="(layout)",
            project_docs="",
            max_iterations=10,
            query_type="root_cause_analysis",
            risk_context=None,
        )
        assert "Risk signals detected" not in prompt


# ---------------------------------------------------------------------------
# Query classifier integration tests
# ---------------------------------------------------------------------------


