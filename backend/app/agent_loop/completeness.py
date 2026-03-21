"""Completeness Verifier — LLM-based check for explorer answer coverage.

Fires once when an explorer agent first says `end_turn` and the rule-based
EvidenceEvaluator has already passed.  A single LLM call (not a full agent)
assesses whether the answer covers the main aspects of the question and
whether obvious investigation paths were missed.

Design reference: Anthropic's 3-agent Code Review architecture where a
Verification Agent filters findings before the Overview Agent synthesizes.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List

from app.ai_provider.base import AIProvider

logger = logging.getLogger(__name__)

_COMPLETENESS_PROMPT = """\
You are reviewing an AI agent's investigation of a codebase question.

Question: {question}
{perspective_block}
Agent's answer:
{answer}

Tools called ({tool_count} total):
{tool_history_summary}

Files accessed ({file_count} total):
{files_list}

Assess:
1. Does the answer address the main aspects of the question{perspective_scope}? (85% coverage is sufficient)
2. Are there obvious search paths the agent missed? (e.g. only searched source code but question requires config/SQL/test data)
3. Did the agent mention something but not follow up on it?

Respond in JSON only — no other text:
- If sufficient: {{"sufficient": true}}
- If gaps found: {{"sufficient": false, "hints": ["hint1", "hint2"]}}

Keep hints to 1-3 specific, actionable directions. Do not repeat what the agent already found.\
"""


@dataclass
class CompletenessCheck:
    """Result of the completeness verification."""
    sufficient: bool
    hints: List[str] = field(default_factory=list)


def _build_tool_summary(tool_history: List[Dict[str, Any]], max_entries: int = 30) -> str:
    """Summarize tool call history for the completeness prompt."""
    if not tool_history:
        return "(none)"
    lines = []
    for entry in tool_history[:max_entries]:
        tool = entry.get("tool", "?")
        params = entry.get("params", {})
        summary = entry.get("summary", "")
        param_str = json.dumps(params, default=str)
        if len(param_str) > 120:
            param_str = param_str[:120] + "..."
        line = f"- {tool}({param_str})"
        if summary:
            line += f" → {summary[:80]}"
        lines.append(line)
    if len(tool_history) > max_entries:
        lines.append(f"  ... and {len(tool_history) - max_entries} more")
    return "\n".join(lines)


async def check_completeness(
    provider: AIProvider,
    question: str,
    answer: str,
    tool_history: List[Dict[str, Any]],
    files_accessed: List[str],
    perspective: str = "",
) -> CompletenessCheck:
    """LLM-based completeness check for explorer answers.

    Makes a single LLM call to assess whether the agent's answer
    sufficiently covers the question. Returns hints for further
    investigation if gaps are found.

    Args:
        provider:       The AI provider (verifier model).
        question:       The original user question.
        answer:         The explorer's current answer text.
        tool_history:   List of dicts with keys: tool, params, summary.
        files_accessed: List of file paths the agent accessed.
        perspective:    Optional agent role/perspective description.
                        When set, the verifier judges completeness
                        relative to this role, not the full question.
    """
    tool_summary = _build_tool_summary(tool_history)
    files_list = "\n".join(f"- {f}" for f in sorted(files_accessed)) if files_accessed else "(none)"

    if perspective:
        perspective_block = f"\nAgent perspective (this agent's assigned role — judge completeness within this scope):\n{perspective}\n"
        perspective_scope = " within its assigned perspective"
    else:
        perspective_block = ""
        perspective_scope = ""

    prompt = _COMPLETENESS_PROMPT.format(
        question=question,
        perspective_block=perspective_block,
        answer=answer,
        tool_count=len(tool_history),
        tool_history_summary=tool_summary,
        file_count=len(files_accessed),
        files_list=files_list,
        perspective_scope=perspective_scope,
    )

    logger.debug(
        "Completeness check input — question: %.120s | perspective: %.80s | "
        "answer_len: %d chars | tool_count: %d | files(%d): %s",
        question, perspective or "(none)", len(answer), len(tool_history),
        len(files_accessed),
        ", ".join(files_accessed[:10]) + ("..." if len(files_accessed) > 10 else ""),
    )

    try:
        import asyncio
        response_text = await asyncio.to_thread(
            provider.call_model,
            prompt=prompt,
            max_tokens=256,
            system="You are a code investigation reviewer. Respond only with valid JSON.",
        )

        parsed = _parse_response(response_text)
        logger.info(
            "Completeness check: sufficient=%s, hints=%d",
            parsed.sufficient, len(parsed.hints),
        )
        return parsed

    except Exception as exc:
        logger.warning("Completeness check failed, treating as sufficient: %s", exc)
        return CompletenessCheck(sufficient=True)


def _parse_response(text: str) -> CompletenessCheck:
    """Parse the LLM's JSON response into a CompletenessCheck."""
    text = text.strip()

    # Try to extract JSON from the response (handle markdown code blocks)
    if "```" in text:
        # Extract content between code fences
        parts = text.split("```")
        for part in parts[1:]:
            # Skip the language identifier line if present
            lines = part.strip().split("\n")
            if lines[0].strip() in ("json", ""):
                lines = lines[1:]
            candidate = "\n".join(lines).strip()
            if candidate:
                text = candidate
                break

    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        logger.warning("Could not parse completeness response as JSON: %s", text[:200])
        return CompletenessCheck(sufficient=True)

    sufficient = data.get("sufficient", True)
    hints = data.get("hints", [])

    if not isinstance(sufficient, bool):
        sufficient = True
    if not isinstance(hints, list):
        hints = []
    hints = [str(h) for h in hints if h]

    return CompletenessCheck(sufficient=sufficient, hints=hints)
