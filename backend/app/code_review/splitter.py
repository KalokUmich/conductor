"""PR Splitter — LLM-backed split plan for oversized PRs.

When an ADO PR exceeds the review size ceiling (2200 lines by default),
the review pipeline today posts a "please split this PR" comment and
skips with ``vote=0``. That's correct but leaves the author holding the
entire diff with no guidance on *how* to split.

This module closes that gap with a single-shot strong-model call that
proposes N logically-independent chunks with per-chunk rationale. The
output is deterministic markdown the ADO formatter can drop into the
skip comment.

Design matches ``translate_pr_summary``:
  - Single LLM call, no sub-agents
  - Fail-soft: on any error return ``None`` so the caller falls back to
    the existing generic skip message
  - Strong tier (the coordinator's main provider) — splitting requires
    reading enough of the diff to cluster, which benefits from the
    stronger tier's context comprehension

Usage:
    from app.code_review.splitter import generate_pr_split_plan

    plan_md = await generate_pr_split_plan(
        diff_text=full_diff,
        pr_title=pr["title"],
        pr_description=pr["description"],
        total_lines=changed_lines,
        provider=strong_provider,
    )
    if plan_md:
        skip_comment = original_message + "\n\n" + plan_md

The returned markdown is author-facing — no internal jargon, no
coordinator-style severity annotations.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from app.ai_provider.base import AIProvider

logger = logging.getLogger(__name__)


_SPLITTER_SYSTEM_PROMPT = """\
You are a senior reviewer helping the author split an oversized PR into
smaller, independently-reviewable chunks. The goal is NOT to guess at
correctness — you have not studied every line — but to propose a clean
decomposition from the diff shape alone.

**Teach, don't command.** This review is the author's chance to learn
how an experienced reviewer thinks about PR hygiene. Your rationales
must explain the *why* — what business concern or architectural seam
ties these files together, what can go wrong if they land mixed with
other changes, and what makes the chunk a sensible reviewable unit.
A junior developer reading your output should understand the
decomposition thinking, not just follow orders.

## Output shape (markdown)

Produce exactly this structure, with no preamble:

```
## Suggested split

The PR changes {TOTAL_LINES} lines across {FILE_COUNT} files. A
reviewable chunk lands in the 50–500 line band. Here's one way to
partition, with the reasoning behind each cluster:

### Chunk 1 — {short descriptive label, e.g. "schema migration for the new billing table"}
- {file path 1}
- {file path 2}
*Why these belong together*: {2-4 sentences. What business concern or
architectural layer does this chunk own? What single question does a
reviewer ask when reviewing it? E.g. "This chunk owns the database
shape for invoices — every file here is about the new `invoices`
table's columns, constraints, and initial seed data. A reviewer is
asking 'is this schema correct and migratable?' — no service logic to
distract from that."}
*Why separate from the rest*: {1-2 sentences. What would go wrong if
this chunk merged with the others? E.g. "Mixing this with the
billing service code means a reviewer has to context-switch between
SQL correctness and Python business rules — two different mental
models, each gets shallower attention."}
*Approx size*: {line count}

### Chunk 2 — {descriptive label}
...

... (up to 6 chunks)

## Dependencies

{2-4 sentences. If chunks must land in order, explain *why* — e.g.
"Chunk 2 (service code) queries tables Chunk 1 creates; landing 2
first breaks production." If chunks are independent, say so and
explain how that's possible — e.g. "The docs chunk and the handler
chunk touch disjoint paths and reference no shared symbols; safe to
land in parallel." If a stack forms, describe each dependency edge
concretely — not just "do 1 then 2 then 3".}

## What to drop

{Optional section. If any files look like unrelated cleanup that
doesn't belong in this PR at all, name them with a one-sentence
explanation of why they're out of scope. E.g. "`docs/old_api.md` —
looks like a docs refresh for an unrelated endpoint; revert from this
PR and submit separately so the reviewer doesn't have to wonder why
it's here." Skip this section entirely when nothing qualifies — do
not fabricate cleanup candidates.}
```

## Hard rules

- Propose **2–6** chunks. Fewer than 2 = no split possible; say so in
  a sentence under "Suggested split", explain *why* (e.g. "every file
  touches the same refactored abstraction — the split would have to
  happen at the design level, not the file level"), and omit the
  chunks / deps / drop sections.
- Each chunk must be **self-contained enough to review** — a chunk
  that needs three other chunks to make sense is wrong. If you catch
  yourself writing "depends on Chunk 2, 3, and 4", the split is off.
- **Group by intent / business concern first**, not by file type.
  Tests go in the same chunk as the code they cover — a reviewer
  checking handler logic needs the tests for that handler in front of
  them, not in a separate PR.
- **Rationale is the product.** A chunk without a substantive
  "Why these belong together" + "Why separate from the rest" is a
  failure — even if the chunk itself is correctly scoped. The author
  learns from your reasoning, not from your file lists.
- No line numbers. No code quotes. No severity labels. Author-
  friendly prose only.
- If a file has to appear in two chunks (e.g. a shared schema file
  with two logically-separate sections), note it explicitly in the
  chunk that owns the bulk of the edits AND in the rationale of the
  other chunk.
- **No fabrication.** Only reference files and directories visible in
  the diff. Don't invent filenames. Don't guess at concerns you
  can't see.

If you genuinely cannot partition the PR (e.g. one monolithic
feature touching every file), say so in one sentence under
"Suggested split", explain *why* concretely, and recommend splitting
the *feature design* before splitting the diff (e.g. "this adds one
new concept — the `Invoice` aggregate — that every file participates
in; the next PR of this shape should be preceded by a design doc that
breaks the feature into staged user-visible deliveries").

Output only the markdown — no JSON, no preamble, no commentary.\
"""


def _build_user_message(
    diff_text: str,
    pr_title: str,
    pr_description: str,
    total_lines: int,
    file_count: int,
    diff_budget_chars: int = 40_000,
) -> str:
    """Assemble the user-message portion of the splitter call."""
    parts: list[str] = []
    if pr_title:
        parts.append(f"## PR title\n{pr_title}")
    if pr_description:
        parts.append(f"## PR description\n{pr_description}")

    parts.append(f"## Diff stats\n- Total changed lines: {total_lines}\n- Files changed: {file_count}")

    # Bound the diff — splitter reasoning is about intent, not every
    # token. 40K chars covers ~400 line-chunks of rep diff; more tends
    # to be noise.
    diff_excerpt = diff_text
    if len(diff_text) > diff_budget_chars:
        diff_excerpt = (
            diff_text[:diff_budget_chars]
            + f"\n\n[...diff truncated at {diff_budget_chars} chars — "
            f"full diff is {len(diff_text)} chars...]"
        )
    parts.append(f"## Diff\n```diff\n{diff_excerpt}\n```")

    parts.append(
        "## Task\nPropose a split plan per the system prompt. Return "
        "only the markdown — no preamble, no JSON."
    )
    return "\n\n".join(parts)


async def generate_pr_split_plan(
    diff_text: str,
    pr_title: str,
    pr_description: str,
    total_lines: int,
    file_count: int,
    provider: AIProvider,
    *,
    max_tokens: int = 2000,
) -> Optional[str]:
    """Generate a suggested split plan for an oversized PR.

    Args:
        diff_text: Full PR diff (will be truncated internally to ~40K
            chars — splitting is an intent-level task, no need for
            every byte).
        pr_title / pr_description: PR metadata; anchors the splitter's
            reasoning on stated intent rather than guessing from code.
        total_lines: Total changed line count (for the opening line of
            the output and for the LLM's sense of scale).
        file_count: Number of files changed.
        provider: Strong-tier :class:`AIProvider` (typically the PR
            Brain's main provider). The fast tier can split simple PRs
            but struggles on mixed-intent oversized PRs, which is
            exactly where this helper earns its keep.
        max_tokens: Output cap. 2000 fits a 6-chunk plan with
            substantive "Why these belong together" / "Why separate"
            rationales (4-6 sentences per chunk) + deps + drop section
            with headroom. Splitter rationales are the product — we'd
            rather pay a bit more output tokens than truncate the
            teaching content.

    Returns:
        Markdown split plan, or ``None`` on any error (empty diff,
        LLM failure, empty response). Callers should fall back to
        their generic skip message when ``None`` is returned.
    """
    if not diff_text.strip():
        logger.debug("generate_pr_split_plan: empty diff, skipping")
        return None

    system_prompt = _SPLITTER_SYSTEM_PROMPT
    user_message = _build_user_message(
        diff_text=diff_text,
        pr_title=pr_title,
        pr_description=pr_description,
        total_lines=total_lines,
        file_count=file_count,
    )

    try:
        text = await asyncio.to_thread(
            provider.call_model,
            user_message,
            max_tokens,
            system_prompt,
        )
    except Exception as exc:
        logger.warning(
            "generate_pr_split_plan: LLM call failed, returning None. "
            "error=%s",
            exc,
        )
        return None

    text = (text or "").strip()
    if not text:
        logger.warning("generate_pr_split_plan: LLM returned empty text.")
        return None

    # Strip an accidental outer code fence (models occasionally wrap the output).
    if text.startswith("```") and text.endswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    # Basic shape guard — must start with the expected section header.
    if not text.lstrip().lower().startswith("## suggested split"):
        logger.info(
            "generate_pr_split_plan: LLM output didn't start with "
            "expected '## Suggested split' header — returning as-is "
            "anyway (caller can still post it, it just won't have "
            "the canonical shape). preview=%r",
            text[:120],
        )

    return text


__all__ = ["generate_pr_split_plan"]
