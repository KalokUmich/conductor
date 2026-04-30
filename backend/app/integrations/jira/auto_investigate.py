"""Headless Jira ticket auto-investigation (Phase 7.7.11 MVP).

When a webhook fires for a newly-created or updated ticket, this module
generates a structured first-pass analysis:
  1. Classify the ticket (bug / feature / question / chore).
  2. Identify likely-affected components from ``jira_project_guide.yaml``.
  3. Suggest concrete next-step investigations a human reviewer can run.

Posts the result back to the ticket as a comment via
``JiraReadonlyClient.add_comment``.

**Scope explicitly limited**: this MVP does NOT mount a workspace and
run a full Brain investigation — that requires per-project repo
checkouts which the Conductor backend does not maintain. The lightweight
LLM analysis here gives the assignee a useful first read while keeping
the fan-out cost low (single call, no sub-agent dispatch).

Future v2 paths (deferred): mount workspace per project → invoke full
Brain → return a real investigation plan with file:line evidence.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from app.ai_provider.base import AIProvider

from .readonly_client import JiraReadonlyClient

logger = logging.getLogger(__name__)


_INVESTIGATE_PROMPT = """\
A new Jira ticket was just created or updated. Read it and produce a
short triage note that the assignee can act on.

# Ticket

**Key**: {key}
**Type**: {issuetype}
**Priority**: {priority}
**Status**: {status}
**Summary**: {summary}

**Description**:
{description}

# Project context

The {key} project maps to these repositories and components:
{project_guide_excerpt}

# Your output

Emit exactly this structure (no extra preamble, no JSON):

**Triage**: <one sentence: what kind of ticket is this — bug / feature
/ question / chore — and what's the actual ask underneath the wording>

**Likely components**: <bullet list of 1-3 components from the project
context above. Skip this section if the ticket isn't code-related.>

**First investigation steps**: <numbered list of 2-4 concrete actions
the assignee should take first. Be specific — name files / functions /
config keys when you can infer them from the description.>

**Risks / unknowns**: <bullet list — only include if there's something
genuinely unclear or risky. Skip the section if there isn't.>

Tone: terse, technical, no fluff. The assignee is a senior engineer.
Write in the language of the ticket description (English or 中文).
"""


def _format_jira_project_excerpt(jira_project_guide: dict, project_key: str) -> str:
    """Pull the relevant project's repos + rules out of jira_project_guide.

    Returns a compact markdown block. If the project isn't in the guide,
    falls back to "no mapping configured".
    """
    projects = jira_project_guide.get("projects") or {}
    project = projects.get(project_key)
    if not project:
        return f"_(no mapping for project {project_key} in jira_project_guide.yaml)_"

    lines: list[str] = []
    desc = project.get("description")
    if desc:
        lines.append(f"_{desc}_")
    repos = project.get("repos") or {}
    for repo_name, repo_cfg in repos.items():
        lines.append(f"\n**{repo_name}**:")
        rules = repo_cfg.get("rules") or []
        for rule in rules[:8]:  # cap at 8 to keep prompt small
            paths = ", ".join(rule.get("paths") or [])
            comp = rule.get("component") or " | ".join(rule.get("candidates") or []) or "?"
            lines.append(f"- `{paths}` → {comp}")
        default = repo_cfg.get("default_component")
        if default:
            lines.append(f"- _(default: {default})_")
    return "\n".join(lines)


def _flatten_description(desc: Any, max_chars: int = 2000) -> str:
    """ADF dict, plain string, or None → plain text. Truncate at max_chars."""
    from .. import atlassian  # noqa: F401  -- ensures package is importable
    from ..atlassian.enrichment import adf_to_text

    if isinstance(desc, dict):
        return adf_to_text(desc, max_chars=max_chars)
    if isinstance(desc, str):
        return desc.strip()[:max_chars]
    return ""


async def investigate_and_comment(
    issue_key: str,
    *,
    jira: JiraReadonlyClient,
    provider: AIProvider,
    jira_project_guide: dict,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch ticket, run lightweight LLM triage, post result as comment.

    Parameters
    ----------
    issue_key:
        Ticket key, e.g. ``"DEV-1234"``.
    jira:
        Configured ``JiraReadonlyClient`` (service-account Basic auth).
    provider:
        AI provider used for the triage call. Strong tier preferred —
        this is a single zero-tool LLM call so cost is bounded.
    jira_project_guide:
        Parsed ``jira_project_guide.yaml`` content. Pass ``{}`` if the
        guide isn't loaded; the prompt will say "no mapping configured".
    dry_run:
        When True, do NOT post the comment back to Jira. Used by tests
        and by manual replay tooling.

    Returns
    -------
    dict with keys: ``issue_key``, ``triage_text``, ``commented`` (bool),
    and optionally ``comment_id`` (str) if posted.
    """
    issue = await jira.get_issue(
        issue_key,
        fields="summary,description,issuetype,priority,status,labels,assignee",
    )
    f = issue.get("fields") or {}
    summary = f.get("summary") or "(no summary)"
    issuetype = (f.get("issuetype") or {}).get("name", "?")
    priority = (f.get("priority") or {}).get("name", "?")
    status = (f.get("status") or {}).get("name", "?")
    description = _flatten_description(f.get("description"))

    project_key = issue_key.split("-", 1)[0]
    project_excerpt = _format_jira_project_excerpt(jira_project_guide, project_key)

    prompt = _INVESTIGATE_PROMPT.format(
        key=issue_key,
        issuetype=issuetype,
        priority=priority,
        status=status,
        summary=summary,
        description=description or "_(no description)_",
        project_guide_excerpt=project_excerpt,
    )

    triage_text = provider.call_model(
        prompt=prompt,
        max_tokens=1200,
        system=(
            "You are a Jira triage assistant. You produce short, "
            "technical first-pass notes for senior engineers. You do "
            "NOT speculate — if the ticket lacks detail, say so."
        ),
        temperature=0.3,
    ).strip()

    if not triage_text:
        logger.warning("[Jira webhook] empty triage text for %s — skipping comment", issue_key)
        return {"issue_key": issue_key, "triage_text": "", "commented": False}

    body = (
        "🤖 **Conductor auto-triage** _(initial pass — confirm before acting)_\n\n"
        + triage_text
    )

    if dry_run:
        return {"issue_key": issue_key, "triage_text": triage_text, "commented": False}

    try:
        result = await jira.add_comment(issue_key, body)
    except Exception as exc:
        logger.error("[Jira webhook] add_comment failed for %s: %s", issue_key, exc)
        return {
            "issue_key": issue_key,
            "triage_text": triage_text,
            "commented": False,
            "error": str(exc),
        }

    comment_id: Optional[str] = None
    if isinstance(result, dict):
        comment_id = result.get("id")
    return {
        "issue_key": issue_key,
        "triage_text": triage_text,
        "commented": True,
        "comment_id": comment_id,
    }
