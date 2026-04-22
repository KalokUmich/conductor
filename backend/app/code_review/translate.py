"""Platform-aware PR review summary translator.

The PR Brain v2 coordinator returns a rich, Google-style review — a
long markdown summary that embeds code quotes + per-finding details.
That's the right shape for a chat / agent UX where the reader sees the
full narrative in one place.

For collaborative code-review surfaces (Azure DevOps, GitHub, GitLab)
the shape is different: per-finding inline threads own the code quote +
specific guidance, and the PR-level comment should complement — not
duplicate — those threads. This module rewrites the coordinator's
synthesis into the platform-native shape via a short strong-model LLM
call.

Usage:
    from app.code_review.translate import translate_pr_summary

    overall = await translate_pr_summary(
        synthesis=result.synthesis,
        findings=result.findings,
        pr_title=pr_title,
        pr_description=pr_description,
        platform="azure",
        provider=agent_provider,
    )

The returned string is a drop-in replacement for the original synthesis
in the PR-level comment — short (≤ 250 words), business-intent-first,
code-free. Inline comments continue to carry file:line anchors and
suggested fixes.

Fail-safe: on any translation error the original synthesis is returned
unchanged. Translation is a "nice-to-have" polish layer, never a
blocker for posting the review.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List

from app.ai_provider.base import AIProvider
from app.code_review.models import ReviewFinding

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform-specific style guides
# ---------------------------------------------------------------------------

_AZURE_STYLE_SYSTEM_PROMPT = """\
You are rewriting a PR review for the **Azure DevOps** PR page. The
PR-level comment you produce sits at the top of the PR alongside
per-finding inline threads that already quote code and give specific
fixes. Your job is to complement those threads with a concise,
business-intent-first summary.

## Output shape (markdown)

Produce EXACTLY three sections, in this order:

1. **## Summary** — 2 to 4 sentences describing what the PR does at the
   business / feature level. Focus on intent, not code mechanics.
2. **## What went well** — 1-3 concise bullets. Positive observations
   (good patterns, thoughtful testing, clean abstractions). Skip this
   section entirely if nothing notable; do not fabricate praise.
3. **## What needs attention** — 1-3 concise bullets at the *theme*
   level (e.g. "error handling in the batch path", "auth coverage on
   internal routes"). DO NOT restate individual findings — readers see
   those in inline threads.

## Hard rules

- **NO code snippets.** No backtick-fenced blocks, no single-line
  `inline` code. Inline threads carry that material; duplicating it
  here wastes the reader's time.
- **NO file paths or line numbers.** Those live on inline threads.
- **NO severity icons.** The recommendation badge is rendered
  separately by the formatter.
- **No more than 250 words total.**
- **No section that restates the same bullet from another section.**

If the original synthesis has nothing substantive to say, return just
the `## Summary` section (2 sentences) and skip the other two. An
empty "What went well" or "What needs attention" section is always
preferable to filler.

Output only the markdown — no preamble, no commentary, no JSON.\
"""

_PLATFORM_STYLES: dict[str, str] = {
    "azure": _AZURE_STYLE_SYSTEM_PROMPT,
}

# ---------------------------------------------------------------------------
# User-message builder
# ---------------------------------------------------------------------------


def _findings_themes(findings: List[ReviewFinding]) -> str:
    """Summarise findings as themes (severity + title), no file:line."""
    if not findings:
        return "(No findings.)"
    lines = []
    for f in findings:
        sev = getattr(f.severity, "value", str(f.severity))
        lines.append(f"- [{sev}] {f.title}")
    return "\n".join(lines)


def _build_user_message(
    synthesis: str,
    findings: List[ReviewFinding],
    pr_title: str,
    pr_description: str,
) -> str:
    parts: List[str] = []
    if pr_title:
        parts.append(f"## PR title\n{pr_title}")
    if pr_description:
        parts.append(f"## PR description\n{pr_description}")
    parts.append("## Original review synthesis (coordinator output)")
    parts.append(synthesis.strip() or "(empty)")
    parts.append("## Findings (already posted as inline threads — themes only)")
    parts.append(_findings_themes(findings))
    parts.append(
        "## Task\nRewrite the above into the platform-specific shape "
        "defined in the system prompt. Return only the markdown."
    )
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def translate_pr_summary(
    synthesis: str,
    findings: List[ReviewFinding],
    platform: str,
    provider: AIProvider,
    *,
    pr_title: str = "",
    pr_description: str = "",
    max_tokens: int = 800,
) -> str:
    """Rewrite ``synthesis`` into the ``platform``-native PR-level comment.

    Args:
        synthesis: Raw coordinator output (Google-style with code quotes).
        findings: The structured findings (used for theme summary only —
            NOT quoted verbatim).
        platform: Target platform key. Currently supported: ``"azure"``.
        provider: Strong-model :class:`AIProvider` used for the rewrite
            (typically the PR Brain's Sonnet).
        pr_title / pr_description: Optional PR metadata to anchor the
            business-level summary on caller intent rather than code
            mechanics alone.
        max_tokens: Output cap. Default 800 comfortably fits the
            ≤250-word contract plus markdown chrome.

    Returns:
        Platform-formatted markdown, or the original synthesis on any
        error. Callers should treat an empty string as "no summary".
    """
    if not synthesis.strip():
        return synthesis
    system_prompt = _PLATFORM_STYLES.get(platform)
    if system_prompt is None:
        logger.debug(
            "translate_pr_summary: platform=%r has no style rule — "
            "returning original synthesis unchanged.",
            platform,
        )
        return synthesis

    user_message = _build_user_message(
        synthesis=synthesis,
        findings=findings,
        pr_title=pr_title,
        pr_description=pr_description,
    )

    try:
        # ``provider.call_model`` is synchronous; push it to a worker
        # thread so the FastAPI event loop stays responsive.
        text = await asyncio.to_thread(
            provider.call_model,
            user_message,
            max_tokens,
            system_prompt,
        )
    except Exception as exc:
        logger.warning(
            "translate_pr_summary: LLM call failed for platform=%s, "
            "returning original synthesis. error=%s",
            platform, exc,
        )
        return synthesis

    text = (text or "").strip()
    if not text:
        logger.warning(
            "translate_pr_summary: LLM returned empty text for "
            "platform=%s, returning original synthesis.",
            platform,
        )
        return synthesis

    # Basic post-validation: strip any accidental leading / trailing JSON
    # or code fences the model may have wrapped around the output.
    if text.startswith("```") and text.endswith("```"):
        text = _strip_fence(text)

    return text


def _strip_fence(text: str) -> str:
    """Remove a single outer ```...``` fence if present."""
    lines = text.splitlines()
    if not lines:
        return text
    if lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


# ---------------------------------------------------------------------------
# Backwards-compatible sync wrapper (not used by ADO — kept for flexibility)
# ---------------------------------------------------------------------------

__all__ = ["AVAILABLE_PLATFORMS", "translate_pr_summary"]

AVAILABLE_PLATFORMS: List[str] = sorted(_PLATFORM_STYLES.keys())
