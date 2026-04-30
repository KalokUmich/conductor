"""Tests for Phase 7.7.11 Jira webhook auto-investigate.

Covers:
  - Token validation (200 / 401 / 503 paths)
  - Event filtering (only ``jira:issue_created`` is auto-investigated)
  - Background dispatch (asyncio.create_task is invoked)
  - investigate_and_comment dry-run (no real Jira call)
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.integrations.jira.auto_investigate import (
    _flatten_description,
    _format_jira_project_excerpt,
    investigate_and_comment,
)
from app.main import app

# ---------------------------------------------------------------
# Webhook router — token + event filter
# ---------------------------------------------------------------


@pytest_asyncio.fixture
async def webhook_setup():
    """Install the minimum app.state attrs the webhook router reads.

    Restores original values on teardown so other tests aren't polluted.
    """
    original = {
        "conductor_config": getattr(app.state, "conductor_config", None),
        "jira_readonly_client": getattr(app.state, "jira_readonly_client", None),
        "agent_provider": getattr(app.state, "agent_provider", None),
        "pr_brain_strong_provider": getattr(app.state, "pr_brain_strong_provider", None),
        "jira_project_guide": getattr(app.state, "jira_project_guide", None),
    }

    cfg = SimpleNamespace(
        atlassian_readonly=SimpleNamespace(
            site_url="https://example.atlassian.net",
            email="bot@example.com",
            api_token="x",
            webhook_token="correct-token-123",
        ),
    )
    app.state.conductor_config = cfg
    app.state.jira_readonly_client = SimpleNamespace(configured=True)
    app.state.agent_provider = SimpleNamespace(call_model=lambda **_: "ok")
    app.state.pr_brain_strong_provider = None
    app.state.jira_project_guide = {}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac, cfg

    for k, v in original.items():
        if v is None:
            if hasattr(app.state, k):
                delattr(app.state, k)
        else:
            setattr(app.state, k, v)


@pytest.mark.asyncio
async def test_webhook_rejects_missing_token(webhook_setup):
    client, _ = webhook_setup
    r = await client.post("/api/webhooks/jira", json={"webhookEvent": "jira:issue_created"})
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_rejects_wrong_token(webhook_setup):
    client, _ = webhook_setup
    r = await client.post(
        "/api/webhooks/jira?token=wrong",
        json={"webhookEvent": "jira:issue_created"},
    )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_webhook_503_when_token_unset(webhook_setup):
    """Empty webhook_token in config → 503 (operator misconfig)."""
    client, cfg = webhook_setup
    cfg.atlassian_readonly.webhook_token = ""
    r = await client.post("/api/webhooks/jira?token=any", json={})
    assert r.status_code == 503
    assert "webhook_token" in r.json().get("detail", "")


@pytest.mark.asyncio
async def test_webhook_skips_unsubscribed_event(webhook_setup):
    client, _ = webhook_setup
    r = await client.post(
        "/api/webhooks/jira?token=correct-token-123",
        json={"webhookEvent": "jira:issue_updated", "issue": {"key": "DEV-1"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["skipped"] == "event_not_subscribed"


@pytest.mark.asyncio
async def test_webhook_skips_missing_issue_key(webhook_setup):
    client, _ = webhook_setup
    r = await client.post(
        "/api/webhooks/jira?token=correct-token-123",
        json={"webhookEvent": "jira:issue_created"},
    )
    assert r.status_code == 200
    assert r.json()["skipped"] == "missing_issue_key"


@pytest.mark.asyncio
async def test_webhook_dispatches_background_task(webhook_setup):
    """Happy path: token + subscribed event + issue → asyncio task fired."""
    client, _ = webhook_setup
    with patch(
        "app.integrations.jira.webhook_router._safely_run_investigation",
        new_callable=AsyncMock,
    ) as mock_run:
        r = await client.post(
            "/api/webhooks/jira?token=correct-token-123",
            json={
                "webhookEvent": "jira:issue_created",
                "issue": {"key": "DEV-1234"},
            },
        )
    assert r.status_code == 200
    assert r.json()["scheduled"] == "DEV-1234"
    # Wait briefly for the create_task to fire
    import asyncio as _asyncio
    await _asyncio.sleep(0.05)
    mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_webhook_503_when_readonly_client_missing(webhook_setup):
    """Readonly client unset (real-world: secrets not configured) → 503."""
    client, _ = webhook_setup
    app.state.jira_readonly_client = None
    r = await client.post(
        "/api/webhooks/jira?token=correct-token-123",
        json={
            "webhookEvent": "jira:issue_created",
            "issue": {"key": "DEV-1"},
        },
    )
    assert r.status_code == 503


# ---------------------------------------------------------------
# auto_investigate helpers
# ---------------------------------------------------------------


def test_format_jira_project_excerpt_known_project():
    guide = {
        "projects": {
            "DEV": {
                "description": "Core engineering",
                "repos": {
                    "abound-server": {
                        "rules": [
                            {"paths": ["src/auth/"], "component": "Auth"},
                            {"paths": ["src/payment/"], "component": "Payment"},
                        ],
                        "default_component": "JBE",
                    },
                },
            },
        },
    }
    out = _format_jira_project_excerpt(guide, "DEV")
    assert "Core engineering" in out
    assert "abound-server" in out
    assert "src/auth/" in out
    assert "Auth" in out
    assert "JBE" in out  # default fallback line


def test_format_jira_project_excerpt_unknown_project():
    out = _format_jira_project_excerpt({"projects": {}}, "UNKNOWN")
    assert "no mapping" in out


def test_flatten_description_handles_string():
    assert _flatten_description("hello world") == "hello world"


def test_flatten_description_handles_none():
    assert _flatten_description(None) == ""


def test_flatten_description_truncates():
    long = "a" * 5000
    out = _flatten_description(long, max_chars=100)
    assert len(out) == 100


def test_flatten_description_handles_adf():
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "hi"}]}
        ],
    }
    assert "hi" in _flatten_description(adf)


# ---------------------------------------------------------------
# investigate_and_comment — dry run end-to-end
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_investigate_and_comment_dry_run():
    """dry_run=True: triage runs, but add_comment is NOT called."""
    jira = MagicMock()
    jira.get_issue = AsyncMock(
        return_value={
            "fields": {
                "summary": "Add retry to payment webhook",
                "issuetype": {"name": "Bug"},
                "priority": {"name": "High"},
                "status": {"name": "To Do"},
                "description": "Webhook fails 3% of the time, need exponential backoff.",
            }
        }
    )
    jira.add_comment = AsyncMock()

    provider = MagicMock()
    provider.call_model = MagicMock(
        return_value="**Triage**: bug.\n**First investigation steps**: 1. grep retry."
    )

    result = await investigate_and_comment(
        "DEV-9999",
        jira=jira,
        provider=provider,
        jira_project_guide={},
        dry_run=True,
    )

    assert result["issue_key"] == "DEV-9999"
    assert "Triage" in result["triage_text"]
    assert result["commented"] is False
    jira.add_comment.assert_not_called()


@pytest.mark.asyncio
async def test_investigate_and_comment_posts_when_not_dry_run():
    jira = MagicMock()
    jira.get_issue = AsyncMock(
        return_value={"fields": {"summary": "x", "description": "y"}}
    )
    jira.add_comment = AsyncMock(return_value={"id": "987654"})

    provider = MagicMock()
    provider.call_model = MagicMock(return_value="**Triage**: feature.")

    result = await investigate_and_comment(
        "DEV-1",
        jira=jira,
        provider=provider,
        jira_project_guide={},
    )

    assert result["commented"] is True
    assert result["comment_id"] == "987654"
    jira.add_comment.assert_called_once()
    posted_body = jira.add_comment.call_args[0][1]
    assert "Conductor auto-triage" in posted_body
    assert "**Triage**: feature." in posted_body


@pytest.mark.asyncio
async def test_investigate_and_comment_handles_add_comment_failure():
    jira = MagicMock()
    jira.get_issue = AsyncMock(
        return_value={"fields": {"summary": "x", "description": "y"}}
    )
    jira.add_comment = AsyncMock(side_effect=RuntimeError("network down"))

    provider = MagicMock()
    provider.call_model = MagicMock(return_value="triage text")

    result = await investigate_and_comment(
        "DEV-1",
        jira=jira,
        provider=provider,
        jira_project_guide={},
    )
    assert result["commented"] is False
    assert "network down" in result["error"]


@pytest.mark.asyncio
async def test_investigate_and_comment_skips_empty_triage():
    """If the LLM returns empty/whitespace, no comment is posted."""
    jira = MagicMock()
    jira.get_issue = AsyncMock(
        return_value={"fields": {"summary": "x", "description": "y"}}
    )
    jira.add_comment = AsyncMock()

    provider = MagicMock()
    provider.call_model = MagicMock(return_value="   \n   ")

    result = await investigate_and_comment(
        "DEV-1",
        jira=jira,
        provider=provider,
        jira_project_guide={},
    )
    assert result["commented"] is False
    jira.add_comment.assert_not_called()
