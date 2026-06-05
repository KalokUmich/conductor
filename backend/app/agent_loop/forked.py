"""Forked-agent primitive (Phase 9.16).

The PR Brain v2 verifier path (P11 3-band precision filter) currently
spins up a full ``AgentLoopService`` for each verifier dispatch. That
pays for:
  - fresh system prompt build (4-layer assembly)
  - fresh tool definitions (the worker doesn't actually call any —
    it just answers a JSON question)
  - fresh prompt cache write
  - the agent-loop overhead (iteration tracking, budget controller,
    evidence gate, …) — none of which the verifier needs

This module provides a lighter primitive for that exact shape:
"single LLM call against a stable system prefix, no tool use, return
text". By construction:

1. The system prompt is constructed with the **PR-specific context
   block as the static cacheable prefix**, followed by the
   verifier-specific instruction block. When the same PR review has
   N verifier calls, calls 2..N hit the prompt cache on the prefix
   (input cost ~10% of fresh).

2. No tool definitions are sent — verifier work is "answer the
   question from what's in front of you", not "go look something up".
   Tool definitions are otherwise the largest cache-write cost.

3. Bypasses ``AgentLoopService`` entirely. The verifier loop is a
   pure ``provider.chat_with_tools(messages, tools=[], system=...)``
   call. Saves the ~50ms of agent loop setup overhead per call.

This is the practical version of Claude Code's "fork agent" pattern
adapted to our verifier path. Future consumers (e.g. a strong-model
arbitration check, a single-pass classification) can use the same
primitive with a different system prefix.

The module is intentionally tiny: one function, one helper. The
caller is responsible for assembling the cache-stable prefix.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.ai_provider.base import AIProvider

logger = logging.getLogger(__name__)


async def fork_call(
    *,
    provider: AIProvider,
    system_prompt: str,
    user_message: str,
    max_tokens: int = 800,
    label: Optional[str] = None,
) -> str:
    """Run a single LLM call with no tool use, returning the answer text.

    Args:
        provider: The AI provider to use. Should be the **same** provider
            instance the parent (coordinator) is already using, so the
            shared prompt cache is hit. Different provider instance →
            different cache scope → no benefit.
        system_prompt: The full system prompt. Caller arranges cache-
            stable content (PR diff, impact graph, schema hints) at the
            START and call-specific content (this finding, this
            question) at the END. The provider's existing
            cache_control / cachePoint markers will cache the
            stable prefix.
        user_message: The user-side message — typically the
            call-specific bit (one finding to verify, one question to
            answer).
        max_tokens: Output cap. Verifier replies are JSON verdicts
            ~100-300 tokens; 800 is generous.
        label: Optional log tag so multi-fork orchestration can be
            traced. Falls through into the call log line.

    Returns:
        The model's text response (already extracted; not the raw
        ToolUseResponse). Empty string on failure (caller handles
        fallback — typically "unclear" verdict).
    """
    if not user_message.strip():
        logger.warning("fork_call[%s]: empty user_message — refusing to call", label or "?")
        return ""

    messages = [{"role": "user", "content": [{"text": user_message}]}]

    try:
        response = await asyncio.to_thread(
            provider.chat_with_tools,
            messages,
            [],  # no tools — pure question/answer
            max_tokens,
            system_prompt,
        )
    except Exception as exc:
        logger.warning(
            "fork_call[%s] failed: %s — caller should treat as 'unclear' verdict",
            label or "?",
            exc,
        )
        return ""

    text = (getattr(response, "text", None) or "").strip()
    if not text:
        logger.info(
            "fork_call[%s] returned empty text — caller should treat as 'unclear'",
            label or "?",
        )
    return text


def build_pr_context_prefix(
    pr_title: str,
    pr_description: str,
    file_diffs_text: str,
    impact_graph: str = "",
    ticket_context: str = "",
    *,
    diff_budget_chars: int = 30_000,
) -> str:
    """Assemble the cache-stable PR context prefix for fork_call.

    Same content across every verifier call within one PR review.
    Returned text goes into the FRONT of the system_prompt so the
    cache-prefix matches across calls; verifier-specific instructions
    follow.

    Args:
        pr_title / pr_description: PR metadata — anchors the
            verifier's interpretation on stated intent.
        file_diffs_text: Already-formatted diff block (e.g. from
            the coordinator's existing per-file ```diff blocks).
            Truncated to ``diff_budget_chars``.
        impact_graph: Optional impact context from
            ``code_review.shared.build_impact_context``.
        diff_budget_chars: Cap on diff text. Verifier doesn't need
            byte-fidelity — just enough to resolve file:line refs.

    Returns:
        Formatted markdown chunk; safe to splice into the front of
        the system_prompt.
    """
    parts: list[str] = []
    if pr_title:
        parts.append(f"## PR title\n{pr_title}")
    if pr_description:
        parts.append(f"## PR description\n{pr_description}")

    diff_excerpt = file_diffs_text or ""
    if len(diff_excerpt) > diff_budget_chars:
        diff_excerpt = diff_excerpt[:diff_budget_chars] + f"\n\n[...PR diff truncated at {diff_budget_chars} chars...]"
    if diff_excerpt:
        parts.append(f"## PR diff\n{diff_excerpt}")

    if impact_graph:
        parts.append(f"## Impact graph\n{impact_graph}")

    if ticket_context:
        parts.append(f"## Linked tickets & docs\n{ticket_context}")

    return "\n\n".join(parts)


__all__ = ["build_pr_context_prefix", "fork_call"]
