"""Second-pass PR re-check.

The first pass (``/review``) reviews a fresh diff. The second pass
(``/recheck``) runs *after* the author has pushed fixes in response to review
comments. It:

  1. reads every review thread on the PR (AI findings + human-reviewer
     comments; system threads like votes / ref updates are filtered out),
  2. verifies — against the CURRENT code in the worktree, not the thread's
     status flag — whether each prior comment is actually addressed,
  3. feeds that verified status into the PR Brain as ``prior_review_context``
     so the re-review focuses on still-open items + regressions the fixes
     introduced, and
  4. lets the caller auto-resolve the threads it confirmed fixed and post a
     consolidated "Second Pass" report.

Design: verification is a single ``fork_call`` (the same primitive the P11
verifiers use) — no agent loop. It reads a small window of the current file at
each comment's line and asks the strong model for a per-comment JSON verdict,
so the auto-resolve decision is structured and reliable rather than parsed out
of free-form synthesis.
"""

from __future__ import annotations

import glob
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

from app.agent_loop.forked import fork_call
from app.ai_provider.base import AIProvider

logger = logging.getLogger(__name__)

# ADO threads GET returns status as a string; create/patch use ints.
_STATUS_STR_TO_INT = {
    "active": 1,
    "fixed": 2,
    "wontfix": 3,
    "closed": 4,
    "bydesign": 5,
    "pending": 6,
}
_STATUS_INT_TO_NAME = {v: k for k, v in _STATUS_STR_TO_INT.items()}

# Our own summary comment header — skip it when collecting prior findings to
# verify (it's a roll-up, not an actionable comment).
_BOT_SUMMARY_MARKER = "Conductor AI Code Review"


@dataclass
class PriorComment:
    """A single actionable review comment from a prior pass (root comment of a thread)."""

    thread_id: int
    file_path: Optional[str]  # repo-relative, leading slash stripped; None for PR-level
    line: Optional[int]  # right-side (modified) line, None for PR-level
    text: str  # root comment markdown
    author: str
    status: int  # ADO thread status (int)
    replies: List[str] = field(default_factory=list)

    @property
    def is_inline(self) -> bool:
        return bool(self.file_path and self.line)

    @property
    def marked_resolved_in_ado(self) -> bool:
        return self.status in (2, 4, 5)  # fixed / closed / byDesign


@dataclass
class PriorVerdict:
    """Verified resolution status of a prior comment against the CURRENT code."""

    comment: PriorComment
    addressed: bool
    confidence: float
    reason: str


def parse_review_threads(threads: List[dict]) -> List[PriorComment]:
    """Turn raw ADO threads into actionable prior comments.

    Keeps threads whose root comment is user-authored (``commentType`` text),
    drops system threads (votes, ref updates, status changes) and our own
    summary roll-up. Inline and PR-level comments are both kept; the caller
    decides what to do with PR-level ones (no code window to verify against).
    """
    out: List[PriorComment] = []
    for t in threads:
        comments = t.get("comments") or []
        if not comments:
            continue
        root = comments[0]
        # commentType: "text"/1 = human/AI; "system"/4 = ADO event noise.
        ctype = root.get("commentType")
        if ctype in ("system", 4):
            continue
        content = (root.get("content") or "").strip()
        if not content:
            continue
        if _BOT_SUMMARY_MARKER in content:
            continue  # our own summary roll-up — not an actionable item

        ctx = t.get("threadContext") or {}
        fp = (ctx.get("filePath") or "").lstrip("/") or None
        line = (ctx.get("rightFileStart") or {}).get("line")

        raw_status = t.get("status")
        if isinstance(raw_status, str):
            status_int = _STATUS_STR_TO_INT.get(raw_status.lower(), 1)
        else:
            status_int = int(raw_status) if raw_status else 1

        replies = [
            (c.get("content") or "").strip()
            for c in comments[1:]
            if c.get("commentType") not in ("system", 4) and (c.get("content") or "").strip()
        ]

        out.append(
            PriorComment(
                thread_id=int(t.get("id", 0)),
                file_path=fp,
                line=line,
                text=content,
                author=(root.get("author") or {}).get("displayName", ""),
                status=status_int,
                replies=replies,
            )
        )
    return out


def _resolve_in_worktree(worktree_path: str, file_path: str) -> Optional[str]:
    """Map an ADO thread filePath to an on-disk path, tolerating prefix drift.

    ADO filePaths are repo-relative but can carry a leading repo/folder segment
    (e.g. ``abound-server/loan/...``) that isn't present in the worktree layout.
    Try the path as-is, then progressively strip leading segments, then fall
    back to a basename glob.
    """
    candidate = os.path.join(worktree_path, file_path)
    if os.path.isfile(candidate):
        return candidate

    parts = file_path.split("/")
    for i in range(1, len(parts)):
        cand = os.path.join(worktree_path, *parts[i:])
        if os.path.isfile(cand):
            return cand

    matches = glob.glob(os.path.join(worktree_path, "**", parts[-1]), recursive=True)
    return matches[0] if len(matches) == 1 else None


def _read_code_window(worktree_path: str, file_path: str, line: int, radius: int = 25) -> str:
    """Read ±radius lines around ``line`` from the current file, numbered."""
    path = _resolve_in_worktree(worktree_path, file_path)
    if not path:
        return "(file not found in current worktree — it may have been renamed or deleted)"
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except OSError as exc:
        return f"(could not read file: {exc})"
    start = max(0, line - radius - 1)
    end = min(len(lines), line + radius)
    return "".join(f"{i + 1:>5}  {lines[i]}" for i in range(start, end)).rstrip()


_VERIFY_SYSTEM_PROMPT = (
    "You are auditing whether a code author addressed prior review comments. "
    "For each comment you are given the original comment and the CURRENT code at "
    "that location (the author has pushed changes since the comment was made). "
    "Decide ONLY from the current code whether the concern is genuinely "
    "addressed — do NOT assume it is fixed just because a thread was marked "
    "resolved. Be strict: a partial or cosmetic change that doesn't remove the "
    "root cause is NOT addressed.\n\n"
    "Return STRICT JSON, an array with one object per comment index:\n"
    '[{"index": 0, "addressed": true, "confidence": 0.0-1.0, "reason": "one sentence citing the current code"}]\n'
    "No prose outside the JSON."
)


def _extract_json_array(text: str) -> list:
    """Best-effort: pull the first JSON array out of a model reply."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.MULTILINE).strip()
    try:
        val = json.loads(text)
        return val if isinstance(val, list) else []
    except json.JSONDecodeError:
        pass
    m = re.search(r"\[.*\]", text, re.DOTALL)
    if m:
        try:
            val = json.loads(m.group(0))
            return val if isinstance(val, list) else []
        except json.JSONDecodeError:
            return []
    return []


async def verify_prior_comments(
    *,
    provider: AIProvider,
    comments: List[PriorComment],
    worktree_path: str,
) -> List[PriorVerdict]:
    """Verify each INLINE prior comment against the current code in one LLM call.

    PR-level comments (no file/line) are returned with ``addressed=False`` and a
    reason flagging them for manual confirmation — they can't be checked against
    a code window.
    """
    inline = [c for c in comments if c.is_inline]
    general = [c for c in comments if not c.is_inline]

    verdicts: List[PriorVerdict] = [
        PriorVerdict(c, addressed=False, confidence=0.0, reason="PR-level comment — verify manually") for c in general
    ]

    if not inline:
        return verdicts

    blocks = []
    for i, c in enumerate(inline):
        snippet = _read_code_window(worktree_path, c.file_path or "", c.line or 0)
        reply_note = ("\n  Replies: " + " || ".join(c.replies)) if c.replies else ""
        blocks.append(
            f"### Comment [{i}] — {c.file_path}:{c.line} "
            f"(ADO status: {_STATUS_INT_TO_NAME.get(c.status, 'active')})\n"
            f"ORIGINAL COMMENT:\n{c.text[:1200]}{reply_note}\n\n"
            f"CURRENT CODE:\n```\n{snippet}\n```"
        )
    user_message = (
        f"There are {len(inline)} prior review comments. For each, decide if it is "
        f"addressed in the current code.\n\n" + "\n\n".join(blocks)
    )

    raw = await fork_call(
        provider=provider,
        system_prompt=_VERIFY_SYSTEM_PROMPT,
        user_message=user_message,
        max_tokens=min(2000, 200 + 120 * len(inline)),
        label="recheck_verify",
    )
    parsed = _extract_json_array(raw)
    by_index = {int(o["index"]): o for o in parsed if isinstance(o, dict) and "index" in o}

    for i, c in enumerate(inline):
        o = by_index.get(i)
        if o is None:
            # No verdict returned → treat as still-open (safe default: don't auto-resolve).
            verdicts.append(PriorVerdict(c, addressed=False, confidence=0.0, reason="no verdict returned — left open"))
        else:
            verdicts.append(
                PriorVerdict(
                    c,
                    addressed=bool(o.get("addressed", False)),
                    confidence=float(o.get("confidence", 0.0) or 0.0),
                    reason=str(o.get("reason", ""))[:400],
                )
            )
    return verdicts


# Only auto-resolve / treat as truly fixed when the model is confident.
_RESOLVE_CONFIDENCE_FLOOR = 0.7


def confirmed_fixed(verdicts: List[PriorVerdict]) -> List[PriorVerdict]:
    return [v for v in verdicts if v.addressed and v.confidence >= _RESOLVE_CONFIDENCE_FLOOR]


def still_open(verdicts: List[PriorVerdict]) -> List[PriorVerdict]:
    return [v for v in verdicts if not (v.addressed and v.confidence >= _RESOLVE_CONFIDENCE_FLOOR)]


def build_prior_review_context(verdicts: List[PriorVerdict]) -> str:
    """The block spliced into the PR Brain so the re-review is fix-aware."""
    if not verdicts:
        return ""
    open_v = still_open(verdicts)
    fixed_v = confirmed_fixed(verdicts)
    lines = [
        "This is a SECOND-PASS re-review. The author pushed changes after a prior "
        "review. The items below were already raised — do NOT re-report ones that "
        "are genuinely fixed. Focus on (a) prior items still NOT addressed, and "
        "(b) NEW problems or regressions the fixes introduced.",
        "",
    ]
    if open_v:
        lines.append("### Prior comments STILL OPEN (re-confirm and escalate if unfixed):")
        for v in open_v:
            loc = f"{v.comment.file_path}:{v.comment.line}" if v.comment.is_inline else "PR-level"
            lines.append(f"- [{loc}] {_first_line(v.comment.text)} — {v.reason}")
        lines.append("")
    if fixed_v:
        lines.append("### Prior comments verified FIXED (do not re-flag unless the fix broke something):")
        for v in fixed_v:
            loc = f"{v.comment.file_path}:{v.comment.line}" if v.comment.is_inline else "PR-level"
            lines.append(f"- [{loc}] {_first_line(v.comment.text)}")
    return "\n".join(lines).strip()


def format_recheck_report(
    verdicts: List[PriorVerdict],
    *,
    new_findings_count: int,
    recommendation: str,
    total_cost_usd: float,
    duration_ms: float,
) -> str:
    """The consolidated 'Second Pass' summary comment posted to the PR."""
    fixed_v = confirmed_fixed(verdicts)
    open_v = still_open(verdicts)

    rec_display = {
        "approve": "✅ **Approve**",
        "approve_with_followups": "✅ **Approve** (with follow-ups)",
        "request_changes": "❌ **Request Changes**",
    }.get(recommendation, f"❓ {recommendation}")

    lines = [
        "## \U0001f916 Conductor AI Code Review — Second Pass",
        "",
        f"Re-checked **{len(verdicts)}** prior comment(s): "
        f"**{len(fixed_v)}** verified fixed, **{len(open_v)}** still open. "
        f"**{new_findings_count}** new issue(s) found.",
        "",
        f"**Recommendation:** {rec_display}",
        "",
    ]
    if open_v:
        lines.append("### ❌ Still open")
        for v in open_v:
            loc = f"`{v.comment.file_path}:{v.comment.line}`" if v.comment.is_inline else "_PR-level_"
            lines.append(f"- {loc} — {_first_line(v.comment.text)}  \n  _{v.reason}_")
        lines.append("")
    if fixed_v:
        lines.append("### ✅ Verified fixed")
        for v in fixed_v:
            loc = f"`{v.comment.file_path}:{v.comment.line}`" if v.comment.is_inline else "_PR-level_"
            lines.append(f"- {loc} — {_first_line(v.comment.text)}")
        lines.append("")
    if new_findings_count:
        lines.append(f"### ⚠️ {new_findings_count} new issue(s) — see inline comments below.")
        lines.append("")

    lines.append("<details><summary>Re-check stats</summary>")
    lines.append("")
    lines.append(f"- Total budget: ${total_cost_usd:.4f}")
    lines.append(f"- Duration: {duration_ms / 1000:.1f}s")
    lines.append("")
    lines.append("</details>")
    return "\n".join(lines)


def _first_line(text: str, limit: int = 160) -> str:
    """First meaningful line of a comment (strip our badge/markdown noise)."""
    for raw in text.splitlines():
        s = raw.strip().lstrip("#").strip()
        s = re.sub(r"[*_`]", "", s)
        # skip our severity badges like "⚠️ **Issue**"
        if not s or s in ("Issue",) or s.startswith(("⚠", "\U0001f534", "\U0001f7e0", "\U0001f535")):
            continue
        return s[:limit]
    return text.strip()[:limit]
