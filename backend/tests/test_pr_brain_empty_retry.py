"""Tests for the 0-findings empty-result safety net.

Two layers:
  * ``_empty_result_retry`` — the ONE conservative strong-model re-examination.
    Findings are normalized through the canonical ``parse_findings`` →
    ``_finding_to_dict`` pipeline (severity/category enums + evidence shape),
    adopted only when grounded (file+line) at confidence >= the bar.
  * ``_maybe_empty_result_retry`` — the CALL-SITE wrapper that makes the retry
    reachable on the PRIMARY failure mode (coordinator emits 0 findings directly,
    so the precision filter is skipped). Self-review found the original wiring put
    the trigger INSIDE the precision filter, which only runs on non-empty output.
"""

from __future__ import annotations

import pytest

from app.agent_loop import pr_brain
from app.agent_loop.pr_brain import PRBrainOrchestrator


def _make_orch(monkeypatch, fork_returns):
    """Bare orchestrator with the prefix builder + fork_call stubbed."""
    orch = PRBrainOrchestrator.__new__(PRBrainOrchestrator)
    orch._provider = object()  # unused — fork_call is stubbed
    monkeypatch.setattr(orch, "_build_verifier_system_prefix", lambda pc, fd: "PREFIX")

    calls = {"n": 0, "labels": []}

    async def fake_fork_call(**kwargs):
        i = calls["n"]
        calls["n"] += 1
        calls["labels"].append(kwargs.get("label"))
        r = fork_returns[i] if i < len(fork_returns) else fork_returns[-1]
        if isinstance(r, Exception):
            raise r
        return r

    monkeypatch.setattr("app.agent_loop.forked.fork_call", fake_fork_call)
    return orch, calls


def _finding_json(title, file, line, conf, sev="high"):
    """A single finding in the canonical bare-array format parse_findings expects."""
    return (
        f'{{"title": "{title}", "severity": "{sev}", "confidence": {conf}, '
        f'"file": "{file}", "start_line": {line}, "end_line": {line}, '
        f'"evidence": ["at line {line}"], "risk": "r", "suggested_fix": "fix"}}'
    )


# ---------------------------------------------------------------------------
# _empty_result_retry — normalization + grounding + confidence gate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recovers_grounded_high_confidence_finding(monkeypatch):
    monkeypatch.setattr(pr_brain, "_EMPTY_RESULT_RETRY", True)
    monkeypatch.setattr(pr_brain, "_SELF_CONSISTENCY", False)
    js = "[" + _finding_json("NPE", "a.py", 12, 0.9) + "]"
    orch, calls = _make_orch(monkeypatch, [js])
    out = await orch._empty_result_retry(None, {}, added_lines=120)
    assert len(out) == 1
    # normalized dict shape (from _finding_to_dict)
    assert out[0]["file"] == "a.py"
    assert out[0]["start_line"] == 12
    assert out[0]["agent"] == "empty_result_retry"
    assert out[0]["severity"] == "high"  # enum value, normalized
    assert isinstance(out[0]["evidence"], list)  # evidence coerced to list
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_low_confidence_is_filtered(monkeypatch):
    monkeypatch.setattr(pr_brain, "_EMPTY_RESULT_RETRY", True)
    monkeypatch.setattr(pr_brain, "_SELF_CONSISTENCY", False)
    js = "[" + _finding_json("maybe", "a.py", 3, 0.5) + "]"
    orch, _ = _make_orch(monkeypatch, [js])
    out = await orch._empty_result_retry(None, {}, added_lines=120)
    assert out == []


@pytest.mark.asyncio
async def test_genuinely_clean_returns_empty(monkeypatch):
    monkeypatch.setattr(pr_brain, "_EMPTY_RESULT_RETRY", True)
    monkeypatch.setattr(pr_brain, "_SELF_CONSISTENCY", False)
    orch, _ = _make_orch(monkeypatch, ["[]"])
    out = await orch._empty_result_retry(None, {}, added_lines=120)
    assert out == []


@pytest.mark.asyncio
async def test_fork_failure_degrades_gracefully(monkeypatch):
    monkeypatch.setattr(pr_brain, "_EMPTY_RESULT_RETRY", True)
    monkeypatch.setattr(pr_brain, "_SELF_CONSISTENCY", False)
    orch, _ = _make_orch(monkeypatch, [RuntimeError("bedrock boom")])
    out = await orch._empty_result_retry(None, {}, added_lines=120)
    assert out == []


@pytest.mark.asyncio
async def test_self_consistency_unions_two_passes(monkeypatch):
    monkeypatch.setattr(pr_brain, "_EMPTY_RESULT_RETRY", True)
    monkeypatch.setattr(pr_brain, "_SELF_CONSISTENCY", True)
    pass1 = "[" + _finding_json("A", "a.py", 12, 0.9) + "]"
    pass2 = "[" + _finding_json("A", "a.py", 12, 0.9) + "," + _finding_json("B", "b.py", 20, 0.85) + "]"
    orch, calls = _make_orch(monkeypatch, [pass1, pass2])
    out = await orch._empty_result_retry(None, {}, added_lines=120)
    assert calls["n"] == 2  # two passes
    files = sorted(f["file"] for f in out)
    assert files == ["a.py", "b.py"]  # A deduped, B added


@pytest.mark.asyncio
async def test_disabled_skips_fork_call(monkeypatch):
    monkeypatch.setattr(pr_brain, "_EMPTY_RESULT_RETRY", False)
    orch, calls = _make_orch(monkeypatch, ["[" + _finding_json("x", "a.py", 1, 0.9) + "]"])
    out = await orch._empty_result_retry(None, {}, added_lines=120)
    assert out == []
    assert calls["n"] == 0  # short-circuited, no LLM call


# ---------------------------------------------------------------------------
# _maybe_empty_result_retry — the CALL-SITE wiring (reachability fix)
# ---------------------------------------------------------------------------
def _orch_with_stubbed_retry(monkeypatch, recovered, added_lines):
    orch = PRBrainOrchestrator.__new__(PRBrainOrchestrator)
    invoked = {"n": 0}

    async def fake_retry(pr_context, file_diffs, *, added_lines):
        invoked["n"] += 1
        return list(recovered)

    monkeypatch.setattr(orch, "_empty_result_retry", fake_retry)
    monkeypatch.setattr(pr_brain, "_added_lines_by_file", lambda fd: {"a.py": list(range(added_lines))})
    return orch, invoked


@pytest.mark.asyncio
async def test_maybe_retry_noop_when_findings_present(monkeypatch):
    """If the coordinator already produced findings, the retry must not run."""
    orch, invoked = _orch_with_stubbed_retry(monkeypatch, recovered=[{"file": "x"}], added_lines=200)
    ro = {"findings": [{"title": "real", "file": "a.py", "start_line": 1, "severity": "high"}]}
    out = await orch._maybe_empty_result_retry(ro, None, {"a.py": "diff"})
    assert out["findings"] == ro["findings"]  # untouched
    assert invoked["n"] == 0  # retry NOT invoked


@pytest.mark.asyncio
async def test_maybe_retry_noop_on_small_pr(monkeypatch):
    """0 findings but a tiny PR (< substantial threshold) → no retry."""
    orch, invoked = _orch_with_stubbed_retry(monkeypatch, recovered=[{"file": "x"}], added_lines=5)
    ro = {"findings": []}
    out = await orch._maybe_empty_result_retry(ro, None, {"a.py": "diff"})
    assert out["findings"] == []
    assert invoked["n"] == 0


@pytest.mark.asyncio
async def test_maybe_retry_fires_on_empty_substantial_pr(monkeypatch):
    """PRIMARY failure mode: coordinator emitted 0 findings on a substantial PR.

    This is the case the original in-filter wiring could NEVER reach.
    """
    recovered = [{"title": "recovered bug", "file": "a.py", "start_line": 9, "severity": "high"}]
    orch, invoked = _orch_with_stubbed_retry(monkeypatch, recovered=recovered, added_lines=200)
    ro = {"findings": [], "merge_recommendation": "approve", "synthesis": "Looks clean."}
    out = await orch._maybe_empty_result_retry(ro, None, {"a.py": "diff"})
    assert invoked["n"] == 1  # retry DID fire
    assert out["findings"] == recovered  # merged in
    assert "safety net" in out["synthesis"].lower()  # note appended
    assert out["merge_recommendation"] != "approve"  # recomputed from the recovered finding
