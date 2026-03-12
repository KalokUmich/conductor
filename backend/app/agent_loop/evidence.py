"""Evidence Evaluator — rule-based check for answer quality.

Before the agent finalises an answer, this module checks whether the answer
contains sufficient evidence (file:line references, actual tool usage, etc.).
If evidence is insufficient **and** the agent still has budget, the evaluator
returns a guidance note that forces the LLM to do 1-2 more rounds.

This is the #1 quality gap vs. Claude Code: every Claude Code answer cites
precise ``file:line`` evidence.  Our agent sometimes gives vague answers
after only 1-2 tool calls.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# Patterns that indicate the answer cites specific code evidence
_FILE_LINE_PATTERN = re.compile(
    r"""
    (?:                             # Match any of:
        [\w./\\-]+\.[\w]+:\d+       #   file.py:42 or path/to/file.ts:100
      | L\d+                        #   L42, L100
      | line\s+\d+                  #   line 42
      | lines?\s+\d+\s*[-–]\s*\d+  #   lines 42-50
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Code block fences (```, ~~~, or indented code)
_CODE_BLOCK_PATTERN = re.compile(r"```[\s\S]*?```")


@dataclass
class EvidenceCheck:
    """Result of an evidence quality check."""
    passed: bool
    file_refs: int          # count of file:line references found
    code_blocks: int        # count of code blocks
    tool_calls_made: int    # total tools called so far
    guidance: str = ""      # guidance note to inject (empty if passed)


def check_evidence(
    answer: str,
    tool_calls_made: int,
    files_accessed: int,
    remaining_iterations: int,
) -> EvidenceCheck:
    """Check whether an answer has sufficient evidence to be finalised.

    Rules:
      1. Must have at least 1 file:line reference (``file.py:42``, ``L42``,
         ``line 42``, etc.) — unless the answer is very short (<100 chars,
         likely a simple "no" or "yes").
      2. Must have made at least 2 tool calls (an answer without investigation
         is suspicious).
      3. Must have accessed at least 1 file (read_file, compressed_view, etc.).

    If evidence is insufficient AND remaining_iterations >= 2, return a
    guidance note.  Otherwise let the answer through (better a weak answer
    than no answer).
    """
    # Short answers (direct "no"/"yes"/one-liner) get a pass
    stripped = answer.strip()
    if len(stripped) < 100:
        return EvidenceCheck(
            passed=True,
            file_refs=0,
            code_blocks=0,
            tool_calls_made=tool_calls_made,
        )

    file_refs = len(_FILE_LINE_PATTERN.findall(answer))
    code_blocks = len(_CODE_BLOCK_PATTERN.findall(answer))

    problems: list[str] = []

    if file_refs == 0 and code_blocks == 0:
        problems.append(
            "Your answer has NO file:line references or code blocks. "
            "Every claim must cite a specific file path and line number."
        )

    if tool_calls_made < 2:
        problems.append(
            "You have only made {n} tool call(s). This is too few for a "
            "substantive answer. Use compressed_view or find_symbol to "
            "gather more evidence.".format(n=tool_calls_made)
        )

    if files_accessed == 0:
        problems.append(
            "You have not accessed any files yet. Read at least one "
            "relevant source file before answering."
        )

    if not problems:
        return EvidenceCheck(
            passed=True,
            file_refs=file_refs,
            code_blocks=code_blocks,
            tool_calls_made=tool_calls_made,
        )

    # Not enough budget to retry — let the answer through
    if remaining_iterations < 2:
        return EvidenceCheck(
            passed=True,  # pass anyway — can't retry
            file_refs=file_refs,
            code_blocks=code_blocks,
            tool_calls_made=tool_calls_made,
        )

    guidance = (
        "⚠ EVIDENCE CHECK FAILED — do NOT provide your answer yet.\n"
        + "\n".join(f"  • {p}" for p in problems)
        + "\n\nGo back and investigate. Then rewrite your answer with "
        "specific file:line citations for every claim."
    )

    return EvidenceCheck(
        passed=False,
        file_refs=file_refs,
        code_blocks=code_blocks,
        tool_calls_made=tool_calls_made,
        guidance=guidance,
    )
