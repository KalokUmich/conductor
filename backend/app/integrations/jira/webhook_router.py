"""Jira webhook receiver — Phase 7.7.11 auto-investigate MVP.

Atlassian Cloud's native webhooks (configured in Settings → System →
Webhooks) do NOT include HMAC signing on the payload. Auth is via a
shared secret token in the URL query string:

    POST /api/webhooks/jira?token={WEBHOOK_TOKEN}

Site admin sets the token when registering the webhook. Conductor
loads the same value from ``conductor.secrets.yaml`` (or env var
``CONDUCTOR_JIRA_WEBHOOK_TOKEN``).

Jira Cloud expects the receiver to respond < 10s; we offload the
investigation to a background task and 200 immediately.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request

from .auto_investigate import investigate_and_comment

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/webhooks/jira", tags=["jira-webhook"])


# Events we currently auto-investigate. issue_updated is intentionally
# OFF by default in MVP — too noisy (every status / field change fires).
_SUBSCRIBED_EVENTS: frozenset[str] = frozenset({"jira:issue_created"})

# Strong references to in-flight background tasks. Without this, the
# event loop GCs the task mid-flight (asyncio holds only weak refs).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


def _verify_token(provided: Optional[str], expected: Optional[str]) -> bool:
    """Constant-time token compare. Both must be set + match."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided, expected)


@router.post("")
async def receive_webhook(
    request: Request,
    token: Optional[str] = Query(None, description="Shared secret"),
) -> dict[str, Any]:
    """Jira-side webhook target. Validates token and dispatches."""
    cfg = request.app.state.conductor_config
    expected_token = getattr(cfg.atlassian_readonly, "webhook_token", "") or None

    if not expected_token:
        # Webhook endpoint isn't configured — treat any inbound traffic
        # as a misconfiguration on our side, not a security event.
        raise HTTPException(
            status_code=503,
            detail=(
                "Jira webhook receiver not configured — set "
                "atlassian_readonly.webhook_token in secrets."
            ),
        )

    if not _verify_token(token, expected_token):
        # Don't echo whether the token was missing vs wrong — same response.
        raise HTTPException(status_code=401, detail="invalid or missing token")

    payload = await request.json()
    event = payload.get("webhookEvent")
    issue = payload.get("issue") or {}
    issue_key = issue.get("key")

    logger.info(
        "[Jira webhook] received event=%s issue=%s",
        event,
        issue_key,
    )

    if event not in _SUBSCRIBED_EVENTS:
        return {"ok": True, "skipped": "event_not_subscribed", "event": event}
    if not issue_key:
        return {"ok": True, "skipped": "missing_issue_key"}

    jira_client = getattr(request.app.state, "jira_readonly_client", None)
    if jira_client is None or not jira_client.configured:
        # Webhook arrived but readonly client isn't set up — bail loudly
        # so the site admin notices.
        raise HTTPException(
            status_code=503,
            detail=(
                "jira_readonly_client not configured — cannot fetch ticket "
                "or post comment."
            ),
        )

    # Prefer the strong-tier provider (PR Brain's, when ADO is enabled),
    # else fall back to the generic agent provider. Both are Claude
    # Sonnet by default, so the triage call is consistent either way.
    provider = (
        getattr(request.app.state, "pr_brain_strong_provider", None)
        or getattr(request.app.state, "agent_provider", None)
    )
    if provider is None:
        raise HTTPException(
            status_code=503,
            detail="AI provider not initialised — cannot run triage.",
        )

    project_guide = getattr(request.app.state, "jira_project_guide", {}) or {}

    # Fire and forget — Jira Cloud retries on >10s response. Hold a
    # reference so the task can't be GC'd mid-flight (RUF006).
    task = asyncio.create_task(
        _safely_run_investigation(
            issue_key=issue_key,
            jira=jira_client,
            provider=provider,
            project_guide=project_guide,
        )
    )
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)

    return {"ok": True, "scheduled": issue_key, "event": event}


async def _safely_run_investigation(
    *,
    issue_key: str,
    jira: Any,
    provider: Any,
    project_guide: dict,
) -> None:
    """Background-task wrapper that swallows + logs errors.

    Webhooks must never crash the event loop — if the LLM call fails or
    the comment write 500s, we log and move on. Jira Cloud will not be
    notified (we already 200'd back), so the failure is silent from
    Atlassian's side. Operator-visible signal is the structured log.
    """
    try:
        result = await investigate_and_comment(
            issue_key,
            jira=jira,
            provider=provider,
            jira_project_guide=project_guide,
        )
        logger.info(
            "[Jira webhook] %s done: commented=%s comment_id=%s",
            issue_key,
            result.get("commented"),
            result.get("comment_id"),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.error(
            "[Jira webhook] %s investigation failed: %s",
            issue_key,
            exc,
            exc_info=True,
        )
