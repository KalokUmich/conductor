"""Unit tests for the code-review eval scorer's finding-matcher.

These pin the two scorer-matching defects that produced false catch=0 on
greptile-sentry 004/008 even though the reviewer actually found the bugs
(recall=1.0): (1) greedy assignment cross-pairing two same-file findings, and
(2) praise/positive-feedback findings being eligible to match an expected bug.

Run: cd backend && ../.venv/bin/python -m pytest ../eval/code_review/test_scorer.py -q
(deterministic; no Bedrock, no graph build).
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)  # scorer, runner
sys.path.insert(0, os.path.abspath(os.path.join(_HERE, "..", "..", "backend")))  # app.*

from app.code_review.models import FindingCategory, ReviewFinding, Severity  # noqa: E402
from runner import CaseConfig  # noqa: E402
from scorer import _match_findings, score_case  # noqa: E402


def _finding(title, file, start, end, severity, category="correctness"):
    return ReviewFinding(
        title=title,
        category=FindingCategory(category),
        severity=Severity(severity),
        confidence=0.9,
        file=file,
        start_line=start,
        end_line=end,
        evidence=["x"],
    )


def _case(case_id, expected):
    return CaseConfig(
        id=case_id,
        patch="x.patch",
        difficulty="hard",
        title="t",
        description="d",
        expected_findings=expected,
    )


def test_same_file_crosspair_is_caught_via_optimal_assignment():
    """008 mechanism: two same-file findings whose only correct distinguisher is
    line overlap, but where a non-line signal (severity/category) makes greedy
    grab the wrong one first. Greedy -> both line_match False -> catch=0.
    Optimal identity-first assignment pairs by location -> catch=1."""
    expected = [
        {
            "title_pattern": "NEVER_A",
            "file_pattern": "monitors.py",
            "line_range": [127, 137],
            "severity": "critical",
            "category": "security",
        },
        {
            "title_pattern": "NEVER_B",
            "file_pattern": "monitors.py",
            "line_range": [163, 173],
            "severity": "low",
            "category": "performance",
        },
    ]
    # actual[0] overlaps expected[1] but sev/cat-matches expected[0];
    # actual[1] overlaps expected[0] but sev/cat-matches expected[1].
    findings = [
        _finding("config thing", "src/monitors.py", 159, 171, "critical", "security"),
        _finding("typo thing", "src/monitors.py", 132, 132, "low", "performance"),
    ]
    score = score_case(_case("xpair", expected), findings, [])
    assert score.recall == 1.0
    assert score.catch_rate == 1.0, "optimal assignment must pair by location"
    # every matched pair is at the right file AND line
    assert all(m.file_match and m.line_match for m in score.matches)


def test_praise_finding_never_steals_an_expected_match():
    """004 mechanism: a praise comment that title+file matches the expected bug
    must not be paired with it (and evict the real bug-line finding)."""
    expected = [
        {
            "title_pattern": "OAuth|state|null",
            "file_pattern": "integration.py",
            "line_range": [501, 505],
            "severity": "critical",
            "category": "security",
        },
    ]
    findings = [
        _finding("OAuth state handling is solid", "src/integration.py", 389, 439, "praise", "security"),
        _finding("OAuth callback NPE on null state", "src/integration.py", 501, 505, "high", "security"),
    ]
    score = score_case(_case("praise", expected), findings, [])
    assert score.catch_rate == 1.0, "real bug finding must win over praise"
    # the matched actual is the real bug (index 1), not the praise (index 0)
    assert score.matches and all(m.actual_index == 1 for m in score.matches)
    # praise is not counted as a false positive -> precision stays 1.0
    assert score.precision == 1.0


def test_distinct_file_findings_unaffected():
    """Strong-case sanity: findings in distinct files match normally (no
    regression from the matcher change)."""
    expected = [
        {
            "title_pattern": "auth",
            "file_pattern": "auth.py",
            "line_range": [10, 20],
            "severity": "critical",
            "category": "security",
        },
        {
            "title_pattern": "race",
            "file_pattern": "worker.py",
            "line_range": [80, 90],
            "severity": "high",
            "category": "concurrency",
        },
    ]
    findings = [
        _finding("auth bypass", "src/auth.py", 12, 15, "critical", "security"),
        _finding("race condition", "src/worker.py", 82, 85, "high", "concurrency"),
    ]
    score = score_case(_case("distinct", expected), findings, [])
    assert score.recall == 1.0
    assert score.catch_rate == 1.0
    assert len(score.matches) == 2


def test_all_praise_yields_no_match():
    """A review that only emits praise catches nothing and is not credited."""
    expected = [
        {
            "title_pattern": "bug",
            "file_pattern": "x.py",
            "line_range": [1, 5],
            "severity": "high",
            "category": "correctness",
        },
    ]
    findings = [_finding("nice code", "src/x.py", 1, 5, "praise", "correctness")]
    matches = _match_findings(expected, findings)
    assert matches == []
    score = score_case(_case("allpraise", expected), findings, [])
    assert score.catch_rate == 0.0
    assert score.recall == 0.0
