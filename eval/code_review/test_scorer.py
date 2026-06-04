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

from runner import CaseConfig  # noqa: E402
from scorer import _match_findings, score_case  # noqa: E402

from app.code_review.models import FindingCategory, ReviewFinding, Severity  # noqa: E402


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


def test_title_only_match_cannot_steal_a_located_catch():
    """009 mechanism: a generic title_pattern (e.g. a bare 'should') must not let
    one expected finding out-bid the file+line CATCH that the finding actually
    belongs to. Location-first assignment keeps the finding with the expected bug
    it is physically located at."""
    expected = [
        # A: generic title that the finding's title also contains, but a DIFFERENT file
        {
            "title_pattern": "should",
            "file_pattern": "other.py",
            "line_range": [1, 5],
            "severity": "low",
            "category": "maintainability",
        },
        # B: the real bug at the finding's exact location, non-matching title
        {
            "title_pattern": "ZZZ_NEVER",
            "file_pattern": "recovery.py",
            "line_range": [100, 110],
            "severity": "high",
            "category": "correctness",
        },
    ]
    findings = [
        # title contains "should" (hits A's pattern) but is located at B's file+line
        _finding("Variable name should be clearer", "src/recovery.py", 100, 105, "high", "correctness"),
    ]
    score = score_case(_case("steal", expected), findings, [])
    assert score.catch_rate == 1.0, "the located finding must be credited as a catch"
    # it must be paired with B (the location it's at), not A (the title word)
    assert all(m.expected_index == 1 for m in score.matches)
    assert all(m.file_match and m.line_match for m in score.matches)


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


# ---------------------------------------------------------------------------
# 2026-06-02 audit fixes: recall location-gate + dismissal exclusion
# ---------------------------------------------------------------------------


def test_recall_not_credited_on_title_only_wrong_file():
    """A loose stopword title_pattern that hits a finding in the WRONG file must
    not count toward recall (it carries zero location agreement). Pre-fix this
    scored recall=1.0; the audit showed it let a reviewer who located nothing
    inflate the headline."""
    expected = [
        {
            "title_pattern": "refactor",  # generic word the finding's title contains
            "file_pattern": "intended_file\\.py",
            "line_range": [10, 20],
            "severity": "low",
            "category": "maintainability",
        },
    ]
    # title matches the pattern, but file + line + sev + cat are all wrong.
    findings = [_finding("please refactor this helper", "src/totally_other.py", 900, 905, "high", "correctness")]
    score = score_case(_case("titleonly", expected), findings, [])
    assert score.recall == 0.0, "title-only match in the wrong file must not count toward recall"
    assert score.catch_rate == 0.0


def test_recall_not_credited_on_severity_category_only():
    """A finding that only coincides on severity+category (eligible via
    keep_score>=2) but shares no file/line/title must not count toward recall."""
    expected = [
        {
            "title_pattern": "ZZZ_NEVER_MATCHES",
            "file_pattern": "nomatch\\.py",
            "line_range": [1, 5],
            "severity": "critical",
            "category": "security",
        },
    ]
    findings = [_finding("unrelated observation", "src/elsewhere.py", 900, 905, "critical", "security")]
    score = score_case(_case("sevcat", expected), findings, [])
    assert score.recall == 0.0, "severity+category coincidence with no location must not count toward recall"
    assert score.catch_rate == 0.0


def test_dismissal_finding_not_credited_as_catch():
    """keycloak-003 mechanism: a finding that points at the right file:line but
    DENIES the defect ("correctly guarded — planted bug is mitigated") must not
    satisfy catch_rate or recall."""
    expected = [
        {
            "title_pattern": "overflow",
            "file_pattern": "asn1\\.py",
            "line_range": [140, 160],
            "severity": "high",
            "category": "security",
        },
    ]
    findings = [
        _finding(
            "integer overflow is correctly guarded; planted bug is mitigated",
            "src/asn1.py",
            146,
            155,
            "low",
            "security",
        ),
    ]
    assert _match_findings(expected, findings) == [], "dismissal must be excluded from matching"
    score = score_case(_case("dismiss", expected), findings, [])
    assert score.catch_rate == 0.0
    assert score.recall == 0.0


def test_dismissal_not_counted_as_false_positive():
    """A dismissal alongside a real catch must not drag precision (it is not a
    bug claim, like praise)."""
    expected = [
        {
            "title_pattern": "NPE|null",
            "file_pattern": "integration\\.py",
            "line_range": [501, 505],
            "severity": "critical",
            "category": "security",
        },
    ]
    findings = [
        _finding("OAuth callback NPE on null state", "src/integration.py", 501, 505, "high", "security"),
        _finding("checked the token path: no vulnerability here", "src/token.py", 12, 14, "low", "security"),
    ]
    score = score_case(_case("dismiss_fp", expected), findings, [])
    assert score.catch_rate == 1.0, "the real bug is still caught"
    assert score.precision == 1.0, "the dismissal must not count as a false positive"


def test_negated_phrase_is_not_a_dismissal():
    """The negation lookbehind keeps real findings safe: 'not correctly guarded'
    asserts a defect and must be credited normally."""
    expected = [
        {
            "title_pattern": "overflow",
            "file_pattern": "asn1\\.py",
            "line_range": [140, 160],
            "severity": "high",
            "category": "security",
        },
    ]
    findings = [
        _finding(
            "integer overflow is not correctly guarded against large input", "src/asn1.py", 146, 155, "high", "security"
        ),
    ]
    score = score_case(_case("negated", expected), findings, [])
    assert score.catch_rate == 1.0, "a real finding phrased with a negation must still catch"
    assert score.recall == 1.0


def test_catch_fraction_is_per_bug_not_binary():
    """2026-06-03 audit fix S1: on a multi-bug case, catching 1 of 2 must score
    catch_fraction=0.5 (per-bug) while the binary catch_rate stays 1.0."""
    expected = [
        {
            "title_pattern": "alpha",
            "file_pattern": "a\\.py",
            "line_range": [10, 20],
            "severity": "high",
            "category": "correctness",
        },
        {
            "title_pattern": "beta",
            "file_pattern": "b\\.py",
            "line_range": [10, 20],
            "severity": "high",
            "category": "correctness",
        },
    ]
    findings = [_finding("alpha bug here", "a.py", 12, 14, "high")]  # only catches expected[0]
    score = score_case(_case("frac", expected), findings, [])
    assert score.catch_rate == 1.0  # binary: >=1 hit
    assert abs(score.catch_fraction - 0.5) < 1e-9  # honest per-bug
    assert "catch_fraction" in score.to_dict()


def test_greptile_view_aggregate_is_mean_catch_fraction():
    from scorer import compute_aggregate

    expected = [
        {
            "title_pattern": "alpha",
            "file_pattern": "a\\.py",
            "line_range": [10, 20],
            "severity": "high",
            "category": "correctness",
        },
        {
            "title_pattern": "beta",
            "file_pattern": "b\\.py",
            "line_range": [10, 20],
            "severity": "high",
            "category": "correctness",
        },
    ]
    s1 = score_case(_case("c1", expected), [_finding("alpha", "a.py", 12, 14, "high")], [])  # 0.5
    s2 = score_case(
        _case("c2", expected), [_finding("alpha", "a.py", 12, 14, "high"), _finding("beta", "b.py", 12, 14, "high")], []
    )  # 1.0
    agg = compute_aggregate([s1, s2])
    assert abs(agg["greptile_view"] - 0.75) < 1e-9  # mean(0.5, 1.0)
    assert abs(agg["catch_fraction"] - 0.75) < 1e-9


def test_syntax_behavioral_classifier():
    """Import gate S4: only compile/behavior-affecting syntax: comments promote."""
    sys.path.insert(0, _HERE)
    from import_greptile import _syntax_is_behavioral

    assert _syntax_is_behavioral("method is called with no args -> compile error")
    assert _syntax_is_behavioral("cannot find symbol Foo")
    assert _syntax_is_behavioral("Traditional characters in a Simplified zh_CN file")
    assert not _syntax_is_behavioral("'Succesful' is misspelled")
    assert not _syntax_is_behavioral("extra double  space after the operator")
    assert not _syntax_is_behavioral("variable name has a typo: groupUuuids")
