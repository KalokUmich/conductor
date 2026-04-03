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

# Patterns that indicate the answer cites specific code evidence.
# Models differ in citation style:
#   Claude:  file.py:42
#   Qwen:    **Line:** 175  /  **File:** X.java + **Line:** 173-180
#            [Line 42]  /  [Lines 42-50]
_FILE_LINE_PATTERN = re.compile(
    r"""
    (?:                                    # Match any of:
        [\w./\\-]+\.[\w]+:\d+              #   file.py:42 or path/to/file.ts:100
      | L\d+                               #   L42, L100
      | line\s+\d+                         #   line 42
      | lines?\s+\d+\s*[-–]\s*\d+         #   lines 42-50
      | \*{0,2}Line:?\*{0,2}\s*\d+        #   **Line:** 175  /  Line: 175  /  Line 175
      | \*{0,2}Lines?:?\*{0,2}\s*\d+\s*[-–]\s*\d+  #  **Line:** 173-180
      | \[Lines?\s+\d+(?:\s*[-–]\s*\d+)?\] #   [Line 42] / [Lines 42-50]
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
    file_refs: int  # count of file:line references found
    code_blocks: int  # count of code blocks
    tool_calls_made: int  # total tools called so far
    guidance: str = ""  # guidance note to inject (empty if passed)


def check_evidence(
    answer: str,
    tool_calls_made: int,
    files_accessed: int,
    remaining_iterations: int,
    min_file_refs: int = 1,
    min_tool_calls: int = 2,
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

    if file_refs < min_file_refs and code_blocks == 0:
        problems.append(
            f"Your answer has {file_refs} file:line references (need {min_file_refs}). "
            "Every claim must cite a specific file path and line number."
        )

    if tool_calls_made < min_tool_calls:
        problems.append(
            f"You have only made {tool_calls_made} tool call(s) (need {min_tool_calls}). "
            "Use compressed_view or find_symbol to gather more evidence."
        )

    if files_accessed == 0:
        problems.append("You have not accessed any files yet. Read at least one relevant source file before answering.")

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
        + "\n\nCall read_file or compressed_view on the most relevant files "
        "to gather concrete evidence. Then provide your answer with "
        "EXPLICIT file:line citations.\n\n"
        "REQUIRED FORMAT — your answer MUST include references like:\n"
        "  • `src/main/java/com/example/Service.java:42`\n"
        "  • `src/service/handler.py:115-120`\n"
        "  • `controllers/UserController.ts:78`\n"
        "Write the file path and line number joined by a colon. "
        "Do NOT write file and line on separate lines."
    )

    return EvidenceCheck(
        passed=False,
        file_refs=file_refs,
        code_blocks=code_blocks,
        tool_calls_made=tool_calls_made,
        guidance=guidance,
    )
