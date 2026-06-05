"""Adversarial finding recheck — post-review false-positive catcher.

A first-pass PR review (``/review``) can post an **overconfident** finding that is
actually a false positive — e.g. a "critical" that was reasoned from the diff
alone without checking the surrounding system. On PR 14471 a "admin password
incompatible with bcrypt" critical drove a ``-5``; the real storage was MD5 (in a
file the diff never touched), so the finding — and its fix — were a no-op.

This module is the automated safety net. For each **vote-driving** finding
(critical / high) already posted on a PR it spawns a **tool-using Opus judge**
with an *adversarial* stance: the judge MUST grep the actual code (storage / write
/ definition sites, not just the diff) and may only **refute** a finding when it
can cite concrete contradicting code. Refuted-with-evidence findings get their
thread **resolved** (closed + an evidence note). It **never changes the vote** —
unverified findings may still be real, so approval stays a human decision.

Design mirrors ``recheck.py`` (parse threads → judge → act → report) but swaps the
zero-tool ``fork_call`` verifier for a tool-using SDK/Opus judge, because the
decisive evidence usually lives *outside* the diff.

The core is **engine-agnostic**: it takes a ``judge`` callable. Two engines ship:

* ``make_sdk_judge`` — ``SdkWorkerRunner`` on the Claude Agent SDK (Opus). Used by
  the ``/adversarial-recheck`` endpoint (runs in the container, production path).
* ``make_inhouse_judge`` — ``AgentLoopService`` (Opus). Used by the standalone demo
  script on the host venv for fast iteration without a container rebuild.

Both are tool-using Opus adversarial judges; the difference is only the loop engine.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .recheck import (
    PriorComment,
    _file_diff,
)

logger = logging.getLogger(__name__)

# Severity that drives a request_changes vote (merge_recommendation buckets
# CRITICAL + HIGH together). Only these are worth an (expensive) Opus recheck.
VOTE_DRIVING = frozenset({"critical", "high"})

# Opus on Bedrock (EU). The judge runs on the strongest model — this is the one
# call where extra reasoning + tool use pays for itself by preventing a bad vote.
OPUS_MODEL_ID = "eu.anthropic.claude-opus-4-8"

# Read-only tool set for the judge (∩ WORKER_MCP_TOOLS, no edit/run/uplink tools).
JUDGE_READONLY_TOOLS = [
    "read_file",
    "grep",
    "list_files",
    "glob",
    "find_symbol",
    "find_references",
    "file_outline",
    "get_dependencies",
    "get_dependents",
    "ast_search",
    "get_callees",
    "get_callers",
    "trace_variable",
    "compressed_view",
    "module_summary",
    "expand_symbol",
    "detect_patterns",
    "list_endpoints",
    "extract_docstrings",
    "db_schema",
    "find_tests",
    "test_outline",
    "search_facts",
]

# ADO thread statuses that mean "already actioned" — skip in apply mode.
_RESOLVED_STATUSES = (2, 4, 5)  # fixed / closed / byDesign
_CLOSED_STATUS = 4

# Hidden marker on correction comments this pass posts, so a future recheck does
# not re-judge its own output (rendered invisible by Markdown).
_ADV_CORRECTION_MARKER = "<!-- conductor-adversarial-recheck -->"

# ---------------------------------------------------------------------------
# Severity parsing from a posted comment's badge
# ---------------------------------------------------------------------------
# Posted findings start with a badge line, e.g. "🔴 **Critical**", "⚠️ **Issue**",
# "🟢 **Nice**". Map both the emoji and the bold word → canonical severity.
_BADGE_EMOJI_SEVERITY = {
    "\U0001f534": "critical",  # 🔴
    "\U0001f7e0": "high",  # 🟠 (formatter "Warning" badge)
    "⚠": "high",  # ⚠ (Issue)
    "\U0001f7e1": "medium",  # 🟡
    "\U0001f535": "nit",  # 🔵 (Suggestion)
    "\U0001f7e2": "praise",  # 🟢 (Nice)
}
_BADGE_WORD_SEVERITY = {
    "critical": "critical",
    "high": "high",
    "warning": "high",
    "issue": "high",
    "medium": "medium",
    "low": "low",
    "suggestion": "nit",
    "nit": "nit",
    "nice": "praise",
    "praise": "praise",
}
_BADGE_WORD_RE = re.compile(r"\*\*\s*([A-Za-z]+)\s*\*\*")


def parse_severity(text: str) -> Optional[str]:
    """Canonical severity from a posted finding's badge line, or None if it has no
    recognizable badge (i.e. it's a human comment, not one of our findings)."""
    if not text:
        return None
    head = text.lstrip()[:80]
    for emoji, sev in _BADGE_EMOJI_SEVERITY.items():
        if emoji in head:
            return sev
    m = _BADGE_WORD_RE.search(head)
    if m:
        return _BADGE_WORD_SEVERITY.get(m.group(1).strip().lower())
    return None


def parse_title(text: str) -> str:
    """The finding's title — the first bold line after the badge line."""
    bolds = re.findall(r"\*\*(.+?)\*\*", text or "", flags=re.DOTALL)
    for b in bolds:
        b = b.strip()
        # skip the badge word itself (Critical/Issue/...)
        if b.lower() in _BADGE_WORD_SEVERITY:
            continue
        return b[:200]
    # fall back to first non-empty, non-badge line
    for raw in (text or "").splitlines():
        s = raw.strip().lstrip("#").strip()
        if s and parse_severity(s) is None:
            return re.sub(r"[*_`]", "", s)[:200]
    return (text or "").strip()[:200]


@dataclass
class PostedFinding:
    """A vote-driving finding reconstructed from a posted ADO review thread."""

    thread_id: int
    severity: str
    title: str
    body: str  # full posted comment markdown
    file: Optional[str]
    line: Optional[int]
    status: int  # ADO thread status (int)

    @property
    def is_resolved(self) -> bool:
        return self.status in _RESOLVED_STATUSES

    @property
    def location(self) -> str:
        return f"{self.file}:{self.line}" if self.file and self.line else "PR-level"


# Phrases that mark an "evidence" item as the ABSENCE of code rather than
# contradicting code. A judge that can't find the cited file/symbol must HOLD —
# "I couldn't locate it" is NOT proof the finding is false. Without this filter a
# "file not found" / "grep returned 0" item (which still carries a `file` key)
# would satisfy the guardrail and wrongly resolve a REAL finding. (Caught on the
# PR 14471 demo when the workspace was on the wrong branch.)
_ABSENCE_MARKERS = (
    "not found",
    "no such",
    "does not exist",
    "doesn't exist",
    "returned 0",
    "0 result",
    "0 match",
    "no match",
    "returned nothing",
    "cannot locate",
    "could not locate",
    "could not find",
    "couldn't find",
    "error:",
    "read_file error",
    "file not",
)


def _is_real_evidence(e: Any) -> bool:
    """True only for a positive code reference the judge actually read: real file
    path + line > 0 + a NON-EMPTY, substantive snippet that is not an absence/error
    marker.

    A citation with NO snippet is not evidence — pointing at "file X line N" without
    showing the contradicting code proves nothing, and a judge that emits a
    placeholder/hallucinated file+line with no snippet must never be able to resolve
    a real finding (review finding #1). LLMs also commonly quote line numbers as
    strings, so coerce tolerantly (review finding #10)."""
    if not isinstance(e, dict):
        return False
    file = str(e.get("file", "")).strip()
    fl = file.lower()
    if not file or file.startswith("(") or "workspace-wide" in fl or "grep)" in fl:
        return False
    line = e.get("line")
    if isinstance(line, bool):
        return False
    try:
        line_int = int(line)
    except (TypeError, ValueError):
        return False
    if line_int <= 0:
        return False
    snip = str(e.get("snippet", "")).strip().lower()
    if len(snip) < 3:  # no/placeholder snippet is not positive evidence
        return False
    return not any(m in snip for m in _ABSENCE_MARKERS)


@dataclass
class AdversarialVerdict:
    """The judge's structured ruling on one finding."""

    finding: PostedFinding
    verdict: str  # holds | refuted | downgrade
    new_severity: Optional[str]
    evidence: List[Dict[str, Any]]
    reason: str
    raw_answer: str = ""
    cost_usd: Optional[float] = None
    tool_calls: int = 0
    error: Optional[str] = None

    @property
    def has_evidence(self) -> bool:
        """Real positive contradicting code — not the absence of code."""
        return any(_is_real_evidence(e) for e in (self.evidence or []))

    @property
    def is_actionable_refutation(self) -> bool:
        """Evidence-grounded guardrail: only act on a refutation that has concrete
        code evidence AND came from a clean judge run. An errored/budget-capped run
        (``error`` set) must never resolve a real finding (fail-safe, review #2/#3)."""
        return self.verdict == "refuted" and self.has_evidence and not self.error


# ---------------------------------------------------------------------------
# Finding extraction
# ---------------------------------------------------------------------------
def extract_findings(
    priors: List[PriorComment],
    *,
    severities: frozenset = VOTE_DRIVING,
    include_resolved: bool = False,
) -> List[PostedFinding]:
    """Reconstruct our vote-driving findings from parsed prior comments.

    Drops comments without a recognizable severity badge (human comments) and
    keeps only ``severities``. Already-resolved threads are skipped unless
    ``include_resolved`` (used by the demo so it can re-judge a closed thread).
    """
    out: List[PostedFinding] = []
    for c in priors:
        # Skip threads this pass already corrected (our hidden marker lives on the
        # reply we posted) so a re-run never re-judges its own output (review #18).
        if _ADV_CORRECTION_MARKER in c.text or any(_ADV_CORRECTION_MARKER in r for r in (c.replies or [])):
            continue
        sev = parse_severity(c.text)
        if sev is None or sev not in severities:
            continue
        f = PostedFinding(
            thread_id=c.thread_id,
            severity=sev,
            title=parse_title(c.text),
            body=c.text,
            file=c.file_path,
            line=c.line,
            status=c.status,
        )
        if f.is_resolved and not include_resolved:
            continue
        out.append(f)
    return out


# ---------------------------------------------------------------------------
# Judge prompt + message
# ---------------------------------------------------------------------------
_PROMPT_FALLBACK = (
    "You are a skeptical senior reviewer auditing ONE finding a first-pass PR "
    "review already posted. Decide, WITH EVIDENCE, whether it is a TRUE defect or "
    "a FALSE POSITIVE. You MUST use grep/read tools to verify the claim against "
    "the ACTUAL code — never reason from the finding text alone. Any claim about a "
    "value's format/type/storage (e.g. 'password is bcrypt') is only valid if you "
    "read where the value is produced/written/defined, often a different file the "
    "diff never touched. If you cannot find concrete contradicting evidence, the "
    "finding HOLDS.\n\n"
    "Return STRICT JSON as the final thing in your answer:\n"
    '{"verdict":"holds|refuted|downgrade","new_severity":"high|medium|low|nit|null",'
    '"evidence":[{"file":"path","line":1,"snippet":"the line you read"}],'
    '"reason":"one sentence citing the grepped code"}\n'
    "`refuted` is ONLY allowed with at least one real evidence item you actually "
    "read; a refutation without evidence will be ignored and the finding kept."
)


def load_judge_prompt() -> str:
    """Adversarial judge system prompt — from config (bind-mounted, tunable without
    a rebuild) with an embedded fallback."""
    candidates = [
        os.environ.get("CONDUCTOR_CONFIG_DIR"),
        "/app/config",
        str(Path(__file__).resolve().parents[4] / "config"),
    ]
    for base in candidates:
        if not base:
            continue
        p = Path(base) / "agents" / "pr_adversarial_recheck.md"
        try:
            raw = p.read_text(encoding="utf-8")
        except OSError:
            continue
        # strip YAML frontmatter if present
        if raw.startswith("---"):
            parts = raw.split("---", 2)
            if len(parts) == 3:
                raw = parts[2]
        body = raw.strip()
        if body:
            return body
    return _PROMPT_FALLBACK


def build_judge_message(finding: PostedFinding, *, worktree: str, diff_spec: str) -> str:
    """The per-finding user message: the finding + the file's diff (what changed)."""
    diff = _file_diff(worktree, diff_spec, finding.file or "") if (diff_spec and finding.file) else ""
    diff_block = f"\n\n## What the PR changed in this file\n```diff\n{diff}\n```" if diff else ""
    return (
        f"# Finding to audit (posted by the first-pass review)\n\n"
        f"**Severity:** {finding.severity}\n"
        f"**Location:** {finding.location}\n\n"
        f"## The posted finding\n{finding.body}{diff_block}\n\n"
        f"---\n"
        f"Investigate the codebase with your tools to confirm or refute this finding. "
        f"Remember: verify any claim about a value's format/type/storage by reading where "
        f"it is produced/written/defined (often a different file). Then return the STRICT "
        f"JSON verdict."
    )


# ---------------------------------------------------------------------------
# Verdict parsing
# ---------------------------------------------------------------------------
def _balanced_objects(text: str) -> List[str]:
    """All top-level {...} substrings, brace-matched with string-literal awareness.

    A regex can't balance arbitrary nesting, and evidence snippets routinely contain
    code with braces (``if (x) { y(); }``) — so we walk the text tracking depth and
    whether we're inside a JSON string (review #14)."""
    out: List[str] = []
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}" and depth > 0:
            depth -= 1
            if depth == 0 and start >= 0:
                out.append(text[start : i + 1])
                start = -1
    return out


def _extract_json_object(text: str) -> dict:
    """Pull the LAST verdict JSON object out of a model reply.

    Prefer the last fenced ```json block; fall back to the last brace-balanced
    object that parses to a dict carrying a ``verdict`` key (the model may echo the
    schema example earlier in its prose — review #11)."""
    if not text:
        return {}
    t = text.strip()
    blocks: List[str] = []
    if "```" in t:
        blocks.extend(re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", t, re.DOTALL | re.IGNORECASE))
    blocks.extend(_balanced_objects(t))
    for cand in reversed(blocks):
        try:
            val = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(val, dict) and "verdict" in val:
            return val
    return {}


def parse_verdict(
    finding: PostedFinding, answer: str, *, cost_usd=None, tool_calls=0, error=None
) -> AdversarialVerdict:
    if error:
        # Errored / incomplete judge run → fail safe to holds regardless of any
        # partial JSON the run may have emitted before failing (review #2/#3).
        return AdversarialVerdict(
            finding,
            "holds",
            None,
            [],
            "",
            raw_answer=(answer or "")[:4000],
            cost_usd=cost_usd,
            tool_calls=tool_calls,
            error=error,
        )
    obj = _extract_json_object(answer)
    verdict = str(obj.get("verdict", "holds")).strip().lower()
    if verdict not in ("holds", "refuted", "downgrade"):
        verdict = "holds"
    new_sev = obj.get("new_severity")
    if isinstance(new_sev, str) and new_sev.strip().lower() in ("null", "none", ""):
        new_sev = None
    raw_ev = obj.get("evidence") or []
    evidence = [e for e in raw_ev if isinstance(e, dict)] if isinstance(raw_ev, list) else []
    return AdversarialVerdict(
        finding=finding,
        verdict=verdict,
        new_severity=new_sev,
        evidence=evidence,
        reason=str(obj.get("reason", ""))[:600],
        raw_answer=answer[:4000],
        cost_usd=cost_usd,
        tool_calls=tool_calls,
        error=error,
    )


# ---------------------------------------------------------------------------
# Judge engines (pluggable). Each returns: async (PostedFinding) -> AdversarialVerdict
# ---------------------------------------------------------------------------
JudgeFn = Callable[[PostedFinding], Awaitable[AdversarialVerdict]]


def _safe_id(task_id: str) -> str:
    """Filesystem-safe task id for use in paths (no traversal — review #15)."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", task_id or "task")[:120]


def make_sdk_judge(
    *,
    worktree: str,
    diff_spec: str,
    task_id: str,
    model: str = OPUS_MODEL_ID,
    max_turns: int = 8,
    max_budget_usd: float = 0.75,
    timeout_s: float = 240.0,
    semaphore: Optional[asyncio.Semaphore] = None,
) -> JudgeFn:
    """Opus judge on the Claude Agent SDK (production / endpoint path)."""
    sem = semaphore or asyncio.Semaphore(3)
    system_prompt = load_judge_prompt()
    safe_task = _safe_id(task_id)

    async def judge(finding: PostedFinding) -> AdversarialVerdict:
        # The WHOLE body is guarded: a lazy-import failure (claude_agent_sdk absent),
        # FactStore.open error, or runner construction error must all fail safe to a
        # holds verdict, never raise into the gather (review #4).
        scratchpad = None
        try:
            from app.agent_loop.sdk_worker import SdkWorkerRunner  # lazy: defers SDK import
            from app.code_tools.executor import LocalToolExecutor
            from app.scratchpad.executor import CachedToolExecutor
            from app.scratchpad.store import FactStore

            session_id = f"adv-{safe_task}-t{finding.thread_id}"
            scratchpad = FactStore.open(session_id, workspace=worktree, task_id=task_id)
            cached = CachedToolExecutor(LocalToolExecutor(workspace_path=worktree), scratchpad)
            runner = SdkWorkerRunner(
                model=model,
                tool_executor=cached,
                tool_names=JUDGE_READONLY_TOOLS,
                max_turns=max_turns,
                llm_semaphore=sem,
                max_budget_usd=max_budget_usd,
                # ONLY our MCP tools (pointed at the PR worktree). Without this the
                # CLI's builtin Read/Grep search the subprocess cwd (the backend, not
                # the worktree) → the judge "can't find the repo" and holds on every
                # finding. Forcing MCP-only routes all reads through the worktree.
                allow_builtins=False,
            )
            msg = build_judge_message(finding, worktree=worktree, diff_spec=diff_spec)
            # Wall-clock bound (mirrors brain._run_worker_sdk) — a hung CLI must not
            # hang the endpoint (review #5). Timeout → fail safe to holds.
            result = await asyncio.wait_for(
                runner.run(system_prompt=system_prompt, user_message=msg), timeout=timeout_s
            )
            cost = (result.budget_summary or {}).get("total_cost_usd")
            return parse_verdict(
                finding, result.answer, cost_usd=cost, tool_calls=result.tool_calls_made, error=result.error
            )
        except Exception as exc:  # any failure (import/open/construct/timeout) → keep the finding
            logger.warning("[adv-recheck] SDK judge failed for thread %s: %s", finding.thread_id, exc)
            return AdversarialVerdict(finding, "holds", None, [], "", error=f"{type(exc).__name__}: {exc}")
        finally:
            if scratchpad is not None:
                with contextlib.suppress(Exception):
                    scratchpad.delete()  # close() + unlink file/-wal/-shm (review #13)

    return judge


def make_inhouse_judge(
    *,
    provider: Any,
    worktree: str,
    diff_spec: str,
    max_iterations: int = 10,
) -> JudgeFn:
    """Opus judge on the in-house AgentLoopService (host demo / fallback path).

    ``provider`` is an Opus ``AIProvider`` (built by the caller). Runs on the host
    venv without the SDK CLI, so the demo script can iterate fast.
    """
    from app.agent_loop.budget import BudgetConfig
    from app.agent_loop.service import AgentLoopService
    from app.code_tools.executor import LocalToolExecutor

    system_prompt = load_judge_prompt()
    executor = LocalToolExecutor(workspace_path=worktree)

    async def judge(finding: PostedFinding) -> AdversarialVerdict:
        from app.agent_loop.config import AgentLoopConfig

        svc = AgentLoopService(
            provider=provider,
            config=AgentLoopConfig(
                max_iterations=max_iterations,
                # BudgetConfig is USD-based (no token cap field); $2 is a generous
                # ceiling for one Opus judge — max_iterations is the tight bound.
                budget_config=BudgetConfig(max_usd=2.0),
                is_sub_agent=True,
                perspective=system_prompt,
                forced_tools=JUDGE_READONLY_TOOLS,
            ),
            tool_executor=executor,
        )
        # Prepend the adversarial system prompt to the query: the in-house 4-layer
        # assembly keys off agent_identity, not `perspective`, so we put the
        # instructions in the message to guarantee the model actually sees them.
        msg = system_prompt + "\n\n---\n\n" + build_judge_message(finding, worktree=worktree, diff_spec=diff_spec)
        try:
            result = await svc.run(query=msg, workspace_path=worktree)
        except Exception as exc:
            logger.warning("[adv-recheck] in-house judge failed for thread %s: %s", finding.thread_id, exc)
            return AdversarialVerdict(finding, "holds", None, [], "", error=f"{type(exc).__name__}: {exc}")
        cost = (result.budget_summary or {}).get("total_cost_usd") if result.budget_summary else None
        return parse_verdict(
            finding, result.answer or "", cost_usd=cost, tool_calls=result.tool_calls_made, error=result.error
        )

    return judge


# ---------------------------------------------------------------------------
# Correction comment
# ---------------------------------------------------------------------------
def build_correction_comment(v: AdversarialVerdict) -> str:
    """The evidence-backed correction posted when a finding is refuted."""
    lines = [
        _ADV_CORRECTION_MARKER,
        "✏️ **Correction — this finding is withdrawn (false positive).**",
        "",
        "An adversarial recheck (Opus, with code-grounded verification) refuted this finding:",
        "",
        f"> {v.reason}" if v.reason else "> (see evidence below)",
        "",
    ]
    if v.evidence:
        lines.append("**Evidence:**")
        for e in v.evidence[:5]:
            loc = e.get("file", "")
            ln = e.get("line")
            loc = f"`{loc}:{ln}`" if ln else f"`{loc}`"
            snip = str(e.get("snippet", "")).strip()
            lines.append(f"- {loc} — {snip}" if snip else f"- {loc}")
        lines.append("")
    lines.append("_Resolving this thread. The vote is unchanged — other findings on this PR may still be valid._")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def _log_path(task_id: str) -> Path:
    root = Path(os.path.expanduser("~/.conductor/adversarial_recheck"))
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{_safe_id(task_id)}.jsonl"  # sanitized — no path traversal (review #15)


_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "nit": 4, "praise": 5}


def _append_log(task_id: str, record: Dict[str, Any]) -> None:
    try:
        with open(_log_path(task_id), "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
    except OSError as exc:  # pragma: no cover - defensive
        logger.debug("[adv-recheck] log write failed: %s", exc)


async def run_adversarial_recheck(
    *,
    judge: JudgeFn,
    findings: List[PostedFinding],
    task_id: str,
    client: Any = None,
    project: str = "",
    repo: str = "",
    pr_id: int = 0,
    apply: bool = False,
    concurrency: int = 3,
    max_findings: int = 10,
) -> Dict[str, Any]:
    """Judge each vote-driving finding adversarially; resolve refuted-with-evidence
    threads (apply mode). Never changes the vote.

    Returns a report dict (findings, verdicts, actions). ``client`` is an
    ``AzureDevOpsClient`` — only needed when ``apply``.
    """
    if not findings:
        return {"findings": 0, "verdicts": [], "resolved": [], "kept": [], "note": "no vote-driving findings"}

    # Cost cap: judge at most ``max_findings``, most-severe first, so a pathological
    # PR can't fan out unbounded Opus judges (review #16).
    findings = sorted(findings, key=lambda f: _SEV_ORDER.get(f.severity, 9))
    if len(findings) > max_findings:
        log_dropped = len(findings) - max_findings
        logger.info("[adv-recheck] capping to %d findings (dropping %d lowest-severity)", max_findings, log_dropped)
        findings = findings[:max_findings]

    sem = asyncio.Semaphore(concurrency)

    async def _one(f: PostedFinding) -> AdversarialVerdict:
        async with sem:
            return await judge(f)

    # return_exceptions: one judge raising must not discard every other verdict;
    # convert any raise into a fail-safe holds (review #8/#12).
    raw = await asyncio.gather(*[_one(f) for f in findings], return_exceptions=True)
    verdicts: List[AdversarialVerdict] = []
    for f, r in zip(findings, raw):
        if isinstance(r, BaseException):
            logger.warning("[adv-recheck] judge raised for thread %s: %s", f.thread_id, r)
            verdicts.append(AdversarialVerdict(f, "holds", None, [], "", error=f"{type(r).__name__}: {r}"))
        else:
            verdicts.append(r)

    resolved: List[Dict[str, Any]] = []
    kept: List[Dict[str, Any]] = []
    ts = datetime.now(UTC).isoformat()

    for v in verdicts:
        f = v.finding
        rec = {
            "ts": ts,
            "pr_id": pr_id,
            "thread_id": f.thread_id,
            "severity": f.severity,
            "title": f.title,
            "location": f.location,
            "verdict": v.verdict,
            "new_severity": v.new_severity,
            "has_evidence": v.has_evidence,
            "evidence": v.evidence,
            "reason": v.reason,
            "tool_calls": v.tool_calls,
            "cost_usd": v.cost_usd,
            "error": v.error,
            "applied": False,
        }
        actionable = v.is_actionable_refutation and not f.is_resolved
        if actionable and apply and client is not None:
            try:
                # Close FIRST, then post the note — so if the close fails we don't
                # leave a "withdrawn / resolving this thread" comment on a still-open
                # real finding (review #7).
                await client.update_thread_status(project, repo, pr_id, f.thread_id, _CLOSED_STATUS)
                await client.reply_to_thread(project, repo, pr_id, f.thread_id, build_correction_comment(v))
                rec["applied"] = True
                logger.info("[adv-recheck] resolved thread %s (refuted: %s)", f.thread_id, v.reason[:80])
            except Exception as exc:
                rec["apply_error"] = f"{type(exc).__name__}: {exc}"
                logger.warning("[adv-recheck] failed to resolve thread %s: %s", f.thread_id, exc)

        _append_log(task_id, rec)
        if v.is_actionable_refutation:
            resolved.append(rec)
        else:
            kept.append(rec)
        # surface the rare "wanted to refute but no evidence → kept" case
        if v.verdict in ("refuted", "downgrade") and not v.has_evidence:
            logger.info(
                "[adv-recheck] thread %s judged %s WITHOUT evidence → kept (guardrail)",
                f.thread_id,
                v.verdict,
            )

    return {
        "findings": len(findings),
        "resolved_count": len(resolved),
        "kept_count": len(kept),
        "applied": apply,
        "resolved": resolved,
        "kept": kept,
        "verdicts": [
            asdict(v.finding) | {"verdict": v.verdict, "reason": v.reason, "evidence": v.evidence} for v in verdicts
        ],
    }


def format_report(report: Dict[str, Any]) -> str:
    """Human-readable before/after summary for the console / endpoint response."""
    lines = [
        "=" * 70,
        f"ADVERSARIAL RECHECK — {report['findings']} vote-driving finding(s) judged"
        + ("  [APPLY]" if report.get("applied") else "  [dry-run]"),
        "=" * 70,
    ]
    for rec in report.get("resolved", []):
        lines.append(f"\n  ❌ REFUTED (false positive){'  [resolved]' if rec.get('applied') else ''}")
        lines.append(f"     [{rec['severity']}] {rec['title']}  @ {rec['location']}")
        lines.append(f"     reason: {rec['reason']}")
        for e in (rec.get("evidence") or [])[:3]:
            lines.append(f"       - {e.get('file')}:{e.get('line')}  {str(e.get('snippet',''))[:80]}")
    for rec in report.get("kept", []):
        tag = rec["verdict"].upper()
        lines.append(f"\n  ✅ {tag} (kept)")
        lines.append(f"     [{rec['severity']}] {rec['title']}  @ {rec['location']}")
        if rec.get("reason"):
            lines.append(f"     reason: {rec['reason']}")
    lines.append("")
    lines.append(
        f"  → {report.get('resolved_count', 0)} refuted, {report.get('kept_count', 0)} held. "
        "Vote unchanged (by design)."
    )
    return "\n".join(lines)
