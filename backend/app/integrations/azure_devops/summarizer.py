"""PR Summary Generator — auto-generates PR description from diff.

Single LLM call reads the diff and produces a structured markdown summary
that gets appended to the PR description. Fast (~5s) and cheap (~2K tokens).
"""

from __future__ import annotations

import logging
from typing import Optional

from app.ai_provider.base import AIProvider

logger = logging.getLogger(__name__)

AI_SUMMARY_MARKER = "<!-- conductor-ai-summary -->"

_SUMMARY_SYSTEM = """\
You generate concise PR summaries for developers. Your output is appended \
to the PR description in Azure DevOps.

Output ONLY the markdown below — no preamble, no commentary.

## Format

```markdown
## What changed
- <3-5 bullet points, each 1 line, most important first>

## Why
<1 sentence linking to the ticket/motivation>

## Impact
- **Scope**: N files, +X/-Y lines
- **Core changes**: <2-3 key files/modules affected>
- **Risk areas**: <brief note on what reviewers should focus on>

## Key decisions
- <2-3 notable design choices visible in the diff, if any>
```

Rules:
- Be specific — mention actual class/method names, not vague descriptions
- "Why" should reference the ticket ID if visible in branch name or commit messages
- "Key decisions" = things a reviewer would want to understand the rationale for
- Skip "Key decisions" section entirely if the changes are straightforward
- Keep total output under 200 words
"""


async def generate_pr_summary(
    provider: AIProvider,
    diff_text: str,
    pr_title: str = "",
    source_branch: str = "",
    max_diff_chars: int = 30000,
) -> Optional[str]:
    """Generate a PR summary from the diff using a single LLM call.

    Args:
        provider: AI provider (Haiku is fast enough for this).
        diff_text: The git diff text (truncated if too large).
        pr_title: PR title for context.
        source_branch: Source branch name (often contains ticket ID).
        max_diff_chars: Max diff chars to send to LLM.

    Returns:
        Markdown summary string, or None on failure.
    """
    # Truncate diff if too large
    if len(diff_text) > max_diff_chars:
        diff_text = diff_text[:max_diff_chars] + f"\n\n... (truncated, {len(diff_text)} total chars)"

    user_msg = f"PR: {pr_title}\nBranch: {source_branch}\n\n<diff>\n{diff_text}\n</diff>"

    try:
        response = provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": user_msg}]}],
            tools=[],
            system=_SUMMARY_SYSTEM,
            max_tokens=1024,
        )
        summary = (response.text or "").strip()
        if not summary:
            return None

        logger.info("[AzureDevOps] PR summary generated: %d chars", len(summary))
        return summary
    except Exception as exc:
        logger.warning("[AzureDevOps] PR summary generation failed: %s", exc)
        return None


def build_description_with_summary(
    existing_description: str,
    summary: str,
) -> str:
    """Append AI summary to existing PR description, replacing any previous one.

    Uses a hidden HTML marker to detect and replace previous AI summaries,
    so re-runs don't duplicate the summary section.
    """
    # Remove previous AI summary if present
    if AI_SUMMARY_MARKER in existing_description:
        parts = existing_description.split(AI_SUMMARY_MARKER)
        existing_description = parts[0].rstrip()

    # Build new description
    sections = []
    if existing_description.strip():
        sections.append(existing_description.strip())

    header = (
        "## \U0001f916 AI Summary\n\n"
        "_Auto-generated from diff — high-level overview only, "
        "may not capture full context or intent. "
        "See inline review comments for detailed findings._"
    )
    sections.append(f"{AI_SUMMARY_MARKER}\n\n---\n\n{header}\n\n{summary}")

    return "\n\n".join(sections)
