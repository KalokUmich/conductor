"""Unit tests for the adversarial finding recheck (pure logic — no SDK / network).

Covers the parts that must be correct for the guardrail to be safe:
- severity badge parsing (which posted comments are our vote-driving findings)
- the evidence-grounded refutation guardrail (no evidence ⇒ keep the finding)
- verdict JSON extraction
- the orchestration: refuted-with-evidence resolves the thread (apply), and
  refuted-WITHOUT-evidence does NOT, and the vote is never touched.
"""

from __future__ import annotations

import asyncio

import pytest

from app.integrations.azure_devops.adversarial_recheck import (
    AdversarialVerdict,
    PostedFinding,
    _extract_json_object,
    build_correction_comment,
    extract_findings,
    parse_severity,
    parse_title,
    parse_verdict,
    run_adversarial_recheck,
)
from app.integrations.azure_devops.recheck import PriorComment


# --- severity parsing ------------------------------------------------------
@pytest.mark.parametrize(
    "text,expected",
    [
        ("\U0001f534 **Critical**\n\n**Admin password broken**", "critical"),
        ("⚠️ **Issue**\n\n**Timing side channel**", "high"),
        ("\U0001f7e0 **Warning**\n\nsomething", "high"),
        ("\U0001f7e2 **Nice**\n\nGood job", "praise"),
        ("\U0001f535 **Suggestion**\n\nconsider", "nit"),
        ("Just a human comment with no badge", None),
        ("", None),
    ],
)
def test_parse_severity(text, expected):
    assert parse_severity(text) == expected


def test_parse_title_skips_badge_word():
    text = "\U0001f534 **Critical**\n\n**Admin password validation incompatible with bcrypt**\n\nrisk..."
    assert parse_title(text) == "Admin password validation incompatible with bcrypt"


# --- finding extraction ----------------------------------------------------
def _prior(tid, text, status=1, file="a.java", line=10):
    return PriorComment(thread_id=tid, file_path=file, line=line, text=text, author="bot", status=status)


def test_extract_findings_keeps_only_vote_driving():
    priors = [
        _prior(1, "\U0001f534 **Critical**\n\n**A**"),
        _prior(2, "⚠️ **Issue**\n\n**B**"),
        _prior(3, "\U0001f7e2 **Nice**\n\n**C praise**"),  # praise — skip
        _prior(4, "\U0001f535 **Suggestion**\n\n**D nit**"),  # nit — skip
        _prior(5, "plain human comment"),  # no badge — skip
    ]
    out = extract_findings(priors)
    sevs = sorted(f.severity for f in out)
    assert sevs == ["critical", "high"]


def test_extract_findings_skips_resolved_by_default():
    priors = [_prior(1, "\U0001f534 **Critical**\n\n**A**", status=4)]  # closed
    assert extract_findings(priors) == []
    assert len(extract_findings(priors, include_resolved=True)) == 1


# --- guardrail: evidence-grounded refutation -------------------------------
def _finding():
    return PostedFinding(thread_id=1, severity="critical", title="X", body="b", file="a.java", line=1, status=1)


def test_refuted_with_evidence_is_actionable():
    v = AdversarialVerdict(
        _finding(), "refuted", None, [{"file": "a.java", "line": 5, "snippet": "md5"}], "md5 not bcrypt"
    )
    assert v.has_evidence
    assert v.is_actionable_refutation


def test_refuted_without_evidence_is_NOT_actionable():
    v = AdversarialVerdict(_finding(), "refuted", None, [], "I think it's wrong")
    assert not v.has_evidence
    assert not v.is_actionable_refutation  # guardrail: no evidence ⇒ keep


def test_holds_is_not_actionable():
    v = AdversarialVerdict(_finding(), "holds", None, [{"file": "a", "line": 1, "snippet": "x"}], "stands")
    assert not v.is_actionable_refutation


def test_absence_of_code_is_NOT_evidence():
    # Regression for the PR 14471 demo hole: "file not found" / "grep 0 results"
    # carry a `file` key but must NOT satisfy the guardrail (would resolve a real finding).
    for ev in (
        [{"file": "x.java", "line": 0, "snippet": "read_file ERROR: File not found"}],
        [{"file": "(workspace-wide grep)", "line": 0, "snippet": "grep returned 0 results"}],
        [{"file": "x.java", "line": 12, "snippet": "could not find the symbol"}],
        [{"file": "x.java", "line": 12, "snippet": "does not exist in the workspace"}],
    ):
        v = AdversarialVerdict(_finding(), "refuted", None, ev, "absence")
        assert not v.has_evidence, ev
        assert not v.is_actionable_refutation, ev


def test_real_positive_evidence_passes():
    v = AdversarialVerdict(
        _finding(),
        "refuted",
        None,
        [{"file": "AdminServiceImpl.java", "line": 1058, "snippet": "MD5Utils.getMD5Digest(request.getPwd())"}],
        "stored as MD5",
    )
    assert v.has_evidence and v.is_actionable_refutation


def test_empty_or_missing_snippet_is_NOT_evidence():
    # review #1: a file+line citation with no actual code snippet is not evidence
    for ev in (
        [{"file": "a.java", "line": 5, "snippet": ""}],
        [{"file": "a.java", "line": 5, "snippet": "   "}],
        [{"file": "a.java", "line": 5}],  # no snippet key
        [{"file": "a.java", "line": 5, "snippet": "x"}],  # too short
    ):
        v = AdversarialVerdict(_finding(), "refuted", None, ev, "no real code")
        assert not v.has_evidence, ev
        assert not v.is_actionable_refutation, ev


def test_string_line_number_is_accepted():
    # review #10: LLMs often emit line numbers as strings
    v = AdversarialVerdict(
        _finding(), "refuted", None, [{"file": "a.java", "line": "1058", "snippet": "MD5Utils.getMD5Digest()"}], "md5"
    )
    assert v.has_evidence and v.is_actionable_refutation


def test_errored_verdict_is_never_actionable():
    # review #2/#3: a refuted verdict from an errored run must not resolve a finding
    v = AdversarialVerdict(
        _finding(),
        "refuted",
        None,
        [{"file": "a.java", "line": 5, "snippet": "real code here"}],
        "looked false",
        error="error_max_budget_usd",
    )
    assert v.has_evidence
    assert not v.is_actionable_refutation  # error present → fail safe


def test_parse_verdict_forces_holds_on_error():
    v = parse_verdict(
        _finding(),
        '{"verdict":"refuted","evidence":[{"file":"a.java","line":5,"snippet":"code"}],"reason":"r"}',
        error="error_max_budget_usd",
    )
    assert v.verdict == "holds"
    assert not v.is_actionable_refutation


def test_extract_json_picks_last_object_and_handles_braces():
    # review #11/#14: model echoes the schema example first, real verdict last, with
    # code braces inside the snippet
    ans = (
        'Example: {"verdict":"holds","evidence":[],"reason":"schema sample"}\n\n'
        "After investigating...\n"
        '{"verdict":"refuted","evidence":[{"file":"A.java","line":7,"snippet":"if (x) { y(); }"}],'
        '"reason":"the real verdict"}'
    )
    obj = _extract_json_object(ans)
    assert obj["verdict"] == "refuted"
    assert obj["reason"] == "the real verdict"
    assert obj["evidence"][0]["snippet"] == "if (x) { y(); }"


# --- verdict JSON extraction ----------------------------------------------
def test_extract_json_object_from_fenced_block():
    ans = 'Here is my analysis...\n```json\n{"verdict":"refuted","evidence":[{"file":"a","line":1}],"reason":"r"}\n```'
    obj = _extract_json_object(ans)
    assert obj["verdict"] == "refuted"


def test_parse_verdict_defaults_to_holds_on_garbage():
    v = parse_verdict(_finding(), "no json here at all")
    assert v.verdict == "holds"


def test_parse_verdict_normalizes_null_severity():
    v = parse_verdict(
        _finding(),
        '{"verdict":"refuted","new_severity":"null",'
        '"evidence":[{"file":"a.java","line":5,"snippet":"real contradicting code"}],"reason":"r"}',
    )
    assert v.new_severity is None
    assert v.is_actionable_refutation


# --- correction comment ----------------------------------------------------
def test_correction_comment_has_marker_and_evidence_and_vote_note():
    v = AdversarialVerdict(
        _finding(),
        "refuted",
        None,
        [{"file": "AdminServiceImpl.java", "line": 1058, "snippet": "MD5Utils.getMD5Digest"}],
        "stored as MD5",
    )
    c = build_correction_comment(v)
    assert "conductor-adversarial-recheck" in c
    assert "AdminServiceImpl.java" in c
    assert "vote is unchanged" in c.lower()


# --- orchestration ---------------------------------------------------------
class _FakeClient:
    def __init__(self):
        self.replies = []
        self.status_updates = []

    async def reply_to_thread(self, project, repo, pr_id, thread_id, content):
        self.replies.append((thread_id, content))

    async def update_thread_status(self, project, repo, pr_id, thread_id, status):
        self.status_updates.append((thread_id, status))


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_orchestration_resolves_only_refuted_with_evidence_in_apply():
    findings = [
        PostedFinding(1, "critical", "false-pos", "b", "a.java", 69, 1),
        PostedFinding(2, "high", "real-one", "b", "a.java", 102, 1),
    ]

    async def judge(f: PostedFinding) -> AdversarialVerdict:
        if f.thread_id == 1:
            return AdversarialVerdict(f, "refuted", None, [{"file": "a.java", "line": 1058, "snippet": "md5"}], "md5")
        return AdversarialVerdict(f, "holds", None, [], "real timing issue")

    client = _FakeClient()
    report = _run(
        run_adversarial_recheck(
            judge=judge, findings=findings, task_id="t", client=client, project="P", repo="R", pr_id=99, apply=True
        )
    )
    assert report["resolved_count"] == 1
    assert report["kept_count"] == 1
    # only thread 1 resolved, with a correction reply + closed status (4)
    assert client.status_updates == [(1, 4)]
    assert len(client.replies) == 1 and client.replies[0][0] == 1


def test_orchestration_dry_run_resolves_nothing():
    findings = [PostedFinding(1, "critical", "x", "b", "a.java", 1, 1)]

    async def judge(f):
        return AdversarialVerdict(
            f, "refuted", None, [{"file": "a.java", "line": 1, "snippet": "actual code here"}], "r"
        )

    client = _FakeClient()
    report = _run(
        run_adversarial_recheck(
            judge=judge, findings=findings, task_id="t", client=client, project="P", repo="R", pr_id=1, apply=False
        )
    )
    assert report["resolved_count"] == 1  # judged refuted
    assert client.status_updates == []  # but nothing applied (dry-run)


def test_orchestration_no_evidence_keeps_finding_even_in_apply():
    findings = [PostedFinding(1, "critical", "x", "b", "a.java", 1, 1)]

    async def judge(f):  # refuted but NO evidence → guardrail keeps it
        return AdversarialVerdict(f, "refuted", None, [], "hunch")

    client = _FakeClient()
    report = _run(
        run_adversarial_recheck(
            judge=judge, findings=findings, task_id="t", client=client, project="P", repo="R", pr_id=1, apply=True
        )
    )
    assert report["resolved_count"] == 0
    assert report["kept_count"] == 1
    assert client.status_updates == []  # NOT resolved — no evidence
