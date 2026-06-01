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
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
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

# Hidden marker appended to findings posted BY a recheck pass. A recheck verifies
# human + first-round-review comments — NOT its own output — so threads carrying
# this marker are skipped, preventing each recheck's findings from accumulating as
# the next recheck's "prior comments". (Rendered invisible by Markdown.)
_RECHECK_FINDING_MARKER = "<!-- conductor-recheck-finding -->"


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
    published_date: Optional[str] = None  # ISO 8601 when the comment was posted

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
        if _BOT_SUMMARY_MARKER in content or _RECHECK_FINDING_MARKER in content:
            continue  # our own roll-up / a prior recheck's own finding — don't re-verify

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
                published_date=root.get("publishedDate"),
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


def _file_diff(worktree_path: str, diff_spec: str, file_path: str, max_lines: int = 200) -> str:
    """The PR's diff for one file — what the author actually changed.

    This is the primary signal: the comment's line number is from an EARLIER
    iteration and may not map to the current file, but the diff shows exactly
    what changed (e.g. a new key block added below the commented line).
    """
    rel = file_path
    on_disk = _resolve_in_worktree(worktree_path, file_path)
    if on_disk:
        rel = os.path.relpath(on_disk, worktree_path)
    try:
        out = subprocess.run(
            ["git", "-C", worktree_path, "diff", diff_spec, "--", rel],
            capture_output=True,
            text=True,
            timeout=30,
        ).stdout
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("recheck _file_diff failed for %s: %s", file_path, exc)
        return ""
    if not out.strip():
        return ""
    lines = out.splitlines()
    if len(lines) > max_lines:
        lines = lines[:max_lines] + [f"... (diff truncated, {len(lines) - max_lines} more lines)"]
    return "\n".join(lines)


def _file_changed_since(worktree_path: str, file_path: str, since_iso: Optional[str]) -> bool:
    """Did any commit touch this file AFTER the comment was posted?

    A comment cannot have been addressed if the file hasn't changed since it was
    made — so when this returns False the caller marks the comment still-open
    without asking the model (this is what stops a recheck on unchanged code from
    declaring findings "fixed" just because the area appears in the PR diff).

    Fails OPEN (returns True → fall back to LLM verification) on any uncertainty:
    no date, no git history, parse error.
    """
    if not since_iso:
        return True
    rel = file_path
    on_disk = _resolve_in_worktree(worktree_path, file_path)
    if on_disk:
        rel = os.path.relpath(on_disk, worktree_path)
    try:
        out = subprocess.run(
            ["git", "-C", worktree_path, "log", "-1", "--format=%cI", "--", rel],
            capture_output=True,
            text=True,
            timeout=15,
        ).stdout.strip()
    except Exception as exc:  # pragma: no cover - defensive
        logger.debug("recheck _file_changed_since git failed for %s: %s", file_path, exc)
        return True
    if not out:
        return True
    try:
        last_commit = datetime.fromisoformat(out)
        commented = datetime.fromisoformat(since_iso.replace("Z", "+00:00"))
        return last_commit > commented
    except ValueError:
        return True


_VERIFY_SYSTEM_PROMPT = (
    "You are auditing whether a code author addressed prior review comments. "
    "For each comment you get: the original comment text, any author/reviewer "
    "REPLIES, the thread's resolution status, the PR DIFF of that file (what the "
    "author actually changed), and the CURRENT code around the commented line.\n\n"
    "How to decide:\n"
    "- The DIFF is your primary evidence — the comment's line number is from an "
    "earlier revision and may no longer point at the relevant code, so judge by "
    "what the diff CHANGED, not by what sits at the stale line.\n"
    "- Interpret the comment by INTENT, not literally. A terse comment like 'use "
    "different key' is addressed if the diff plausibly satisfies that intent "
    "(e.g. the author added a distinct key block), even if the original value "
    "still appears elsewhere.\n"
    "- Weight the signals: if the thread is marked resolved/fixed AND the author "
    "replied that they changed it AND the diff shows a relevant change, treat it "
    "as ADDRESSED unless the diff/code clearly shows the concern still stands.\n"
    "- Still be strict about BLATANT non-fixes: if the diff shows NO relevant "
    "change to the commented concern, it is NOT addressed regardless of status.\n"
    "- 'The code is intentional' or 'the comment only asks for stakeholder/human "
    "verification' is NOT 'addressed'. Such findings stay OPEN until a human acts "
    "or the specific concern is actually resolved in code — do not auto-credit "
    "them as fixed.\n\n"
    "Return STRICT JSON, an array with one object per comment index:\n"
    '[{"index": 0, "addressed": true, "confidence": 0.0-1.0, "reason": "one sentence citing the diff or code"}]\n'
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
    diff_spec: str = "",
) -> List[PriorVerdict]:
    """Verify each INLINE prior comment in one LLM call, using the PR diff as the
    primary signal plus the current code window.

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

    # Change-gate: a comment cannot have been addressed if its file hasn't changed
    # since the comment was posted. Those get an automatic still-open verdict (no
    # LLM call) — this is what stops a recheck on unchanged code from declaring
    # findings "fixed" just because the flagged area appears in the PR diff.
    to_verify: List[PriorComment] = []
    for c in inline:
        if not _file_changed_since(worktree_path, c.file_path or "", c.published_date):
            verdicts.append(
                PriorVerdict(
                    c,
                    addressed=False,
                    confidence=0.0,
                    reason="file unchanged since this comment was posted — not addressed yet",
                )
            )
        else:
            to_verify.append(c)

    if not to_verify:
        return verdicts

    # Per-file diff, computed once and shared across same-file comments.
    diffs_by_file: dict = {}
    if diff_spec:
        for c in to_verify:
            fp = c.file_path or ""
            if fp not in diffs_by_file:
                diffs_by_file[fp] = _file_diff(worktree_path, diff_spec, fp)

    diff_sections = []
    for fp, d in diffs_by_file.items():
        if d:
            diff_sections.append(f"### {fp}\n```diff\n{d}\n```")
    diff_block = (
        ("## What the PR changed in the commented files (primary evidence)\n\n" + "\n\n".join(diff_sections) + "\n\n")
        if diff_sections
        else ""
    )

    blocks = []
    for i, c in enumerate(to_verify):
        snippet = _read_code_window(worktree_path, c.file_path or "", c.line or 0)
        reply_note = ("\n  AUTHOR/REVIEWER REPLIES: " + " || ".join(c.replies)) if c.replies else ""
        blocks.append(
            f"### Comment [{i}] — {c.file_path}:{c.line} "
            f"(thread status: {_STATUS_INT_TO_NAME.get(c.status, 'active')})\n"
            f"ORIGINAL COMMENT:\n{c.text[:1200]}{reply_note}\n\n"
            f"CURRENT CODE (line numbers may have shifted since the comment):\n```\n{snippet}\n```"
        )
    user_message = (
        f"There are {len(to_verify)} prior review comments. For each, decide if it is "
        f"addressed — judge primarily from the diff (what changed), interpreting the "
        f"comment by intent.\n\n" + diff_block + "## Prior comments to verify\n\n" + "\n\n".join(blocks)
    )

    raw = await fork_call(
        provider=provider,
        system_prompt=_VERIFY_SYSTEM_PROMPT,
        user_message=user_message,
        max_tokens=min(2000, 200 + 120 * len(to_verify)),
        label="recheck_verify",
    )
    parsed = _extract_json_array(raw)
    by_index = {int(o["index"]): o for o in parsed if isinstance(o, dict) and "index" in o}

    for i, c in enumerate(to_verify):
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


def _same_file(a: Optional[str], b: Optional[str]) -> bool:
    """Two paths refer to the same file, tolerating leading-prefix drift."""
    if not a or not b:
        return False
    a, b = a.lstrip("/"), b.lstrip("/")
    if a == b:
        return True
    sa, sb = a.split("/"), b.split("/")
    n = min(3, len(sa), len(sb))
    return sa[-n:] == sb[-n:]


def dedupe_findings_against_priors(findings: list, verdicts: List[PriorVerdict], *, line_window: int = 15):
    """Fold re-review findings that overlap a prior comment back into that comment.

    A "new" finding at the same file + nearby line as a prior comment is NOT new —
    it's the prior issue resurfacing. We drop it from the new-findings list (the
    prior comment already covers it) and, crucially, if it overlaps a comment we
    had marked verified-FIXED, that's a contradiction: the re-review still flags
    that spot, so the fix isn't real. Flip the verdict to not-addressed so the
    thread is NOT resolved and shows as still-open.

    Returns ``(kept_findings, verdicts)`` (verdicts mutated in place).
    """
    kept = []
    for f in findings:
        f_file = getattr(f, "file", "") or ""
        f_line = getattr(f, "start_line", 0) or 0
        overlap = None
        for v in verdicts:
            c = v.comment
            if not c.is_inline:
                continue
            if _same_file(f_file, c.file_path) and abs(f_line - (c.line or 0)) <= line_window:
                overlap = v
                break
        if overlap is None:
            kept.append(f)
            continue
        # Overlaps a prior comment → same issue, not a new finding.
        if overlap.addressed and overlap.confidence >= _RESOLVE_CONFIDENCE_FLOOR:
            # Contradiction: we said fixed, but the re-review re-flags this spot.
            overlap.addressed = False
            overlap.confidence = 0.0
            overlap.reason = (
                f"re-review still flags this location ({getattr(f, 'title', 'issue')}) — not actually fixed"
            )
        # else: already still-open → the prior comment covers it; just drop the finding.
    return kept, verdicts


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
