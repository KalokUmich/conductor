"""Tests for the Azure DevOps second-pass re-check (recheck.py)."""

from __future__ import annotations

import subprocess

import pytest

from app.integrations.azure_devops import recheck
from app.integrations.azure_devops.recheck import (
    PriorComment,
    PriorVerdict,
    _extract_json_array,
    _file_diff,
    _resolve_in_worktree,
    build_prior_review_context,
    confirmed_fixed,
    format_recheck_report,
    parse_review_threads,
    still_open,
    verify_prior_comments,
)


def _make_repo_with_change(tmp_path, *, old: str, new: str, filename: str = "a.py"):
    """Tiny git repo with one file changed across two commits. Returns diff_spec."""

    def g(*args):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True, capture_output=True, text=True)

    g("init", "-q")
    g("config", "user.email", "t@t.com")
    g("config", "user.name", "t")
    (tmp_path / filename).write_text(old)
    g("add", "-A")
    g("commit", "-q", "-m", "base")
    base = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    (tmp_path / filename).write_text(new)
    g("add", "-A")
    g("commit", "-q", "-m", "change")
    head = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    return f"{base}...{head}"


# ---------------------------------------------------------------------------
# parse_review_threads
# ---------------------------------------------------------------------------


def _thread(tid, content, *, ctype="text", status="active", file=None, line=None, replies=None):
    comments = [{"id": 1, "content": content, "commentType": ctype, "author": {"displayName": "Alice"}}]
    for i, r in enumerate(replies or []):
        comments.append({"id": 2 + i, "content": r, "commentType": "text", "author": {"displayName": "Bob"}})
    t = {"id": tid, "status": status, "comments": comments}
    if file:
        t["threadContext"] = {"filePath": file, "rightFileStart": {"line": line}}
    return t


def test_parse_filters_system_and_summary_keeps_review_comments():
    threads = [
        _thread(1, "X voted -5", ctype="system"),
        _thread(2, "The reference refs/heads/x was updated", ctype="system"),
        _thread(3, "## 🤖 Conductor AI Code Review\n\nReviewed 2 files"),  # our summary → skip
        _thread(4, "Null check missing here", file="/src/Foo.java", line=42, status="active"),
        _thread(5, "General architectural concern"),  # PR-level, kept
    ]
    parsed = parse_review_threads(threads)
    ids = {p.thread_id for p in parsed}
    assert ids == {4, 5}
    inline = next(p for p in parsed if p.thread_id == 4)
    assert inline.file_path == "src/Foo.java" and inline.line == 42 and inline.is_inline
    general = next(p for p in parsed if p.thread_id == 5)
    assert not general.is_inline


def test_parse_maps_status_string_and_replies():
    threads = [
        _thread(7, "Race condition", file="/a.go", line=10, status="fixed", replies=["Fixed in latest push"]),
    ]
    p = parse_review_threads(threads)[0]
    assert p.status == 2  # fixed
    assert p.marked_resolved_in_ado is True
    assert p.replies == ["Fixed in latest push"]


def test_parse_skips_empty_and_systemint_commenttype():
    threads = [
        {"id": 9, "status": "active", "comments": [{"content": "", "commentType": "text"}]},
        {"id": 10, "status": "active", "comments": [{"content": "noise", "commentType": 4}]},  # system int
    ]
    assert parse_review_threads(threads) == []


# ---------------------------------------------------------------------------
# _extract_json_array
# ---------------------------------------------------------------------------


def test_extract_json_array_handles_fences_and_prose():
    assert _extract_json_array('```json\n[{"index":0}]\n```') == [{"index": 0}]
    assert _extract_json_array('[{"index":1}]') == [{"index": 1}]
    assert _extract_json_array('Here you go: [{"index":2}] done') == [{"index": 2}]
    assert _extract_json_array("not json at all") == []


# ---------------------------------------------------------------------------
# confidence gating
# ---------------------------------------------------------------------------


def _verdict(addressed, conf, *, inline=True):
    c = PriorComment(
        thread_id=1, file_path="a.py" if inline else None, line=1 if inline else None, text="t", author="A", status=1
    )
    return PriorVerdict(c, addressed=addressed, confidence=conf, reason="r")


def test_confirmed_fixed_requires_high_confidence():
    v_hi = _verdict(True, 0.9)
    v_lo = _verdict(True, 0.5)  # addressed but low confidence → NOT confirmed
    v_no = _verdict(False, 0.9)
    verdicts = [v_hi, v_lo, v_no]
    assert confirmed_fixed(verdicts) == [v_hi]
    assert still_open(verdicts) == [v_lo, v_no]


# ---------------------------------------------------------------------------
# context + report formatting
# ---------------------------------------------------------------------------


def test_build_context_has_secondpass_framing_and_sections():
    ctx = build_prior_review_context([_verdict(True, 0.9), _verdict(False, 0.2)])
    assert "SECOND-PASS" in ctx
    assert "STILL OPEN" in ctx and "verified FIXED" in ctx


def test_format_report_counts_and_recommendation():
    md = format_recheck_report(
        [_verdict(True, 0.9), _verdict(False, 0.2)],
        new_findings_count=1,
        recommendation="request_changes",
        total_cost_usd=0.12,
        duration_ms=5000,
    )
    assert "Second Pass" in md
    assert "**1** verified fixed, **1** still open" in md
    assert "Request Changes" in md
    assert "$0.1200" in md


# ---------------------------------------------------------------------------
# _resolve_in_worktree — prefix tolerance
# ---------------------------------------------------------------------------


def test_resolve_strips_drifting_prefix(tmp_path):
    f = tmp_path / "loan" / "src" / "Foo.java"
    f.parent.mkdir(parents=True)
    f.write_text("class Foo {}\n")
    # ADO path carries an extra leading repo segment not on disk
    assert _resolve_in_worktree(str(tmp_path), "abound-server/loan/src/Foo.java") == str(f)
    assert _resolve_in_worktree(str(tmp_path), "loan/src/Foo.java") == str(f)
    assert _resolve_in_worktree(str(tmp_path), "does/not/Exist.java") is None


# ---------------------------------------------------------------------------
# verify_prior_comments — fork_call mocked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verify_prior_comments_maps_verdicts(tmp_path, monkeypatch):
    src = tmp_path / "a.py"
    src.write_text("def f():\n    return 1\n")

    inline = PriorComment(thread_id=1, file_path="a.py", line=2, text="bug here", author="A", status=1)
    general = PriorComment(thread_id=2, file_path=None, line=None, text="overall", author="A", status=1)

    async def fake_fork_call(**kwargs):
        # current code window must have been included in the user message
        assert "CURRENT CODE" in kwargs["user_message"]
        return '[{"index":0,"addressed":true,"confidence":0.9,"reason":"guarded now"}]'

    monkeypatch.setattr(recheck, "fork_call", fake_fork_call)

    verdicts = await verify_prior_comments(provider=object(), comments=[inline, general], worktree_path=str(tmp_path))
    by_thread = {v.comment.thread_id: v for v in verdicts}
    assert by_thread[1].addressed is True and by_thread[1].confidence == 0.9
    # general (PR-level) comment is returned, not auto-addressed
    assert by_thread[2].addressed is False


def test_file_diff_shows_what_changed(tmp_path):
    spec = _make_repo_with_change(tmp_path, old="key: ai-agent\n", new="key: ai-agent\ncms-key: new-distinct\n")
    diff = _file_diff(str(tmp_path), spec, "a.py")
    assert "cms-key: new-distinct" in diff
    assert diff.lstrip().startswith("diff --git")


@pytest.mark.asyncio
async def test_verify_includes_diff_as_primary_evidence(tmp_path, monkeypatch):
    # Mirrors the PR-14227 'use different key' miss: the original value still
    # appears, but a distinct key was ADDED — the diff must reach the model.
    spec = _make_repo_with_change(
        tmp_path,
        old="ai-agent:\n  public-key: AAA\n",
        new="ai-agent:\n  public-key: AAA\ncms:\n  public-key: BBB-distinct\n",
        filename="application-dev.yml",
    )
    comment = PriorComment(
        thread_id=1,
        file_path="application-dev.yml",
        line=2,
        text="use different key",
        author="K",
        status=2,
        replies=["modified"],
    )

    seen = {}

    async def fake_fork_call(**kwargs):
        seen["msg"] = kwargs["user_message"]
        return '[{"index":0,"addressed":true,"confidence":0.9,"reason":"distinct cms key added per diff"}]'

    monkeypatch.setattr(recheck, "fork_call", fake_fork_call)
    verdicts = await verify_prior_comments(
        provider=object(), comments=[comment], worktree_path=str(tmp_path), diff_spec=spec
    )
    # the diff (showing the added distinct key) reached the model
    assert "What the PR changed" in seen["msg"]
    assert "BBB-distinct" in seen["msg"]
    assert "modified" in seen["msg"]  # author reply included
    assert verdicts[0].addressed is True


@pytest.mark.asyncio
async def test_verify_handles_empty_model_reply(tmp_path, monkeypatch):
    src = tmp_path / "a.py"
    src.write_text("x = 1\n")
    inline = PriorComment(thread_id=1, file_path="a.py", line=1, text="bug", author="A", status=1)

    async def empty_fork_call(**kwargs):
        return ""

    monkeypatch.setattr(recheck, "fork_call", empty_fork_call)
    verdicts = await verify_prior_comments(provider=object(), comments=[inline], worktree_path=str(tmp_path))
    # no verdict returned → safe default: not addressed (won't auto-resolve)
    assert verdicts[0].addressed is False
