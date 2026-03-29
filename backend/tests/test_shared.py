"""Tests for shared code review utilities in app.code_review.shared."""
from __future__ import annotations

import pytest
from app.code_review.shared import (
    parse_findings,
    raw_to_finding,
    evidence_gate,
    post_filter,
    merge_recommendation,
    build_diffs_section,
    compute_budget_multiplier,
    should_reject_pr,
    AGENT_CATEGORIES,
    MAX_FILE_DIFF_CHARS,
)
from app.code_review.models import (
    FindingCategory,
    PRContext,
    ChangedFile,
    ReviewFinding,
    Severity,
    FileCategory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_finding(
    title="Bug found",
    severity=Severity.WARNING,
    confidence=0.85,
    file="app/service.py",
    start_line=10,
    end_line=10,
    evidence=None,
    category=FindingCategory.CORRECTNESS,
    agent="correctness",
) -> ReviewFinding:
    return ReviewFinding(
        title=title,
        category=category,
        severity=severity,
        confidence=confidence,
        file=file,
        start_line=start_line,
        end_line=end_line,
        evidence=evidence if evidence is not None else ["line 10 shows a null dereference", "caller passes None"],
        agent=agent,
    )


def _make_pr_context(total_changed_lines=300, file_count=3) -> PRContext:
    files = [
        ChangedFile(
            path=f"app/file{i}.py",
            additions=total_changed_lines // max(file_count, 1),
            deletions=0,
            category=FileCategory.BUSINESS_LOGIC,
        )
        for i in range(file_count)
    ]
    return PRContext(
        diff_spec="HEAD~1..HEAD",
        files=files,
        total_additions=total_changed_lines,
        total_deletions=0,
        total_changed_lines=total_changed_lines,
        file_count=file_count,
    )


# ---------------------------------------------------------------------------
# parse_findings
# ---------------------------------------------------------------------------

class TestParseFindings:
    def test_parse_findings_json_code_block(self):
        answer = """Here are the findings:
```json
[{"title": "Null pointer", "severity": "critical", "confidence": 0.9, "file": "app.py", "start_line": 5, "end_line": 5, "evidence": ["line 5 dereferences None"], "risk": "crash", "suggested_fix": "add null check"}]
```
"""
        findings = parse_findings(answer, "correctness", FindingCategory.CORRECTNESS)
        assert len(findings) == 1
        assert findings[0].title == "Null pointer"
        assert findings[0].severity == Severity.CRITICAL
        assert findings[0].file == "app.py"

    def test_parse_findings_bare_json_array(self):
        answer = '[{"title": "SQL injection", "severity": "critical", "confidence": 0.88, "file": "db.py", "start_line": 20, "end_line": 20, "evidence": ["user input concatenated", "no parameterization"], "risk": "data breach", "suggested_fix": "use params"}]'
        findings = parse_findings(answer, "security", FindingCategory.SECURITY)
        assert len(findings) == 1
        assert findings[0].category == FindingCategory.SECURITY

    def test_parse_findings_individual_objects(self):
        # No array wrapper — should fall back to individual object matching
        answer = (
            'Some text. {"title": "Missing check", "severity": "warning", "confidence": 0.80, '
            '"file": "svc.py", "start_line": 3, "end_line": 3, "evidence": ["no validation"], '
            '"risk": "bad input", "suggested_fix": "validate"} more text.'
        )
        findings = parse_findings(answer, "correctness", FindingCategory.CORRECTNESS)
        assert len(findings) == 1
        assert findings[0].severity == Severity.WARNING

    def test_parse_findings_empty_answer(self):
        findings = parse_findings("", "correctness", FindingCategory.CORRECTNESS, warn_on_empty=False)
        assert findings == []

    def test_parse_findings_no_json(self):
        findings = parse_findings("No issues found in this PR.", "correctness", FindingCategory.CORRECTNESS, warn_on_empty=False)
        assert findings == []

    def test_parse_findings_invalid_json(self):
        # Malformed JSON should not raise, just return empty
        answer = "```json\n[{broken json here\n```"
        findings = parse_findings(answer, "correctness", FindingCategory.CORRECTNESS, warn_on_empty=False)
        assert isinstance(findings, list)

    def test_parse_findings_multiple_items(self):
        answer = """
```json
[
  {"title": "Issue A", "severity": "critical", "confidence": 0.9, "file": "a.py", "start_line": 1, "end_line": 1, "evidence": ["ev1", "ev2"], "risk": "r", "suggested_fix": "f"},
  {"title": "Issue B", "severity": "warning", "confidence": 0.8, "file": "b.py", "start_line": 2, "end_line": 2, "evidence": ["ev1"], "risk": "r", "suggested_fix": "f"}
]
```
"""
        findings = parse_findings(answer, "correctness", FindingCategory.CORRECTNESS)
        assert len(findings) == 2
        assert findings[0].title == "Issue A"
        assert findings[1].title == "Issue B"

    def test_parse_findings_assigns_category(self):
        answer = '[{"title": "T", "severity": "nit", "confidence": 0.9, "file": "x.py", "start_line": 1, "end_line": 1, "evidence": ["e"], "risk": "r", "suggested_fix": "f"}]'
        findings = parse_findings(answer, "test_coverage", FindingCategory.TEST_COVERAGE)
        assert findings[0].category == FindingCategory.TEST_COVERAGE


# ---------------------------------------------------------------------------
# raw_to_finding
# ---------------------------------------------------------------------------

class TestRawToFinding:
    def test_raw_to_finding_valid(self):
        raw = {
            "title": "Cache miss race",
            "severity": "critical",
            "confidence": 0.91,
            "file": "cache.py",
            "start_line": 42,
            "end_line": 44,
            "evidence": ["line 42 reads without lock", "line 44 writes without lock"],
            "risk": "data corruption",
            "suggested_fix": "wrap in lock",
        }
        f = raw_to_finding(raw, "concurrency", FindingCategory.CONCURRENCY)
        assert f is not None
        assert f.title == "Cache miss race"
        assert f.severity == Severity.CRITICAL
        assert f.confidence == pytest.approx(0.91)
        assert f.agent == "concurrency"
        assert f.category == FindingCategory.CONCURRENCY

    def test_raw_to_finding_missing_title_and_file(self):
        # Missing both title and file → should return None
        raw = {"severity": "warning", "confidence": 0.8}
        result = raw_to_finding(raw, "correctness", FindingCategory.CORRECTNESS)
        assert result is None

    def test_raw_to_finding_defaults(self):
        # Only title provided (no optional fields)
        raw = {"title": "Bare minimum finding"}
        f = raw_to_finding(raw, "reliability", FindingCategory.RELIABILITY)
        assert f is not None
        assert f.severity == Severity.WARNING  # default
        assert f.start_line == 0
        assert f.evidence == []

    def test_raw_to_finding_invalid_confidence(self):
        # Non-float confidence should not raise — either coerces or falls back
        raw = {"title": "Test finding", "confidence": "not_a_number", "file": "x.py"}
        # Should either return None (ValueError) or a finding with a default
        result = raw_to_finding(raw, "correctness", FindingCategory.CORRECTNESS)
        assert result is None  # float("not_a_number") raises ValueError

    def test_raw_to_finding_severity_mapping(self):
        for sev_str, expected in [
            ("critical", Severity.CRITICAL),
            ("warning", Severity.WARNING),
            ("nit", Severity.NIT),
            ("praise", Severity.PRAISE),
            ("unknown", Severity.WARNING),  # fallback
        ]:
            raw = {"title": "x", "severity": sev_str, "file": "f.py"}
            f = raw_to_finding(raw, "a", FindingCategory.CORRECTNESS)
            assert f is not None
            assert f.severity == expected, f"Severity {sev_str!r} → expected {expected}, got {f.severity}"

    def test_raw_to_finding_not_a_dict(self):
        result = raw_to_finding("not a dict", "correctness", FindingCategory.CORRECTNESS)
        assert result is None


# ---------------------------------------------------------------------------
# evidence_gate
# ---------------------------------------------------------------------------

class TestEvidenceGate:
    def test_evidence_gate_keeps_well_evidenced_critical(self):
        f = _make_finding(
            severity=Severity.CRITICAL,
            evidence=["null dereference at line 10", "caller passes None from line 5"],
            start_line=10,
            file="app/service.py",
        )
        result = evidence_gate([f], tool_calls_made=5)
        assert len(result) == 1
        assert result[0].severity == Severity.CRITICAL

    def test_evidence_gate_downgrades_low_evidence(self):
        # Only 1 evidence item — below the CRITICAL_MIN_EVIDENCE of 2
        f = _make_finding(
            severity=Severity.CRITICAL,
            evidence=["just one piece of evidence"],
            start_line=10,
            file="app/service.py",
        )
        result = evidence_gate([f], tool_calls_made=5)
        assert result[0].severity == Severity.WARNING

    def test_evidence_gate_downgrades_no_file(self):
        f = _make_finding(
            severity=Severity.CRITICAL,
            evidence=["ev1", "ev2"],
            start_line=10,
            file="",  # no file reference
        )
        result = evidence_gate([f], tool_calls_made=5)
        assert result[0].severity == Severity.WARNING

    def test_evidence_gate_downgrades_no_line(self):
        f = _make_finding(
            severity=Severity.CRITICAL,
            evidence=["ev1", "ev2"],
            start_line=0,  # no line number
            file="app/service.py",
        )
        result = evidence_gate([f], tool_calls_made=5)
        assert result[0].severity == Severity.WARNING

    def test_evidence_gate_downgrades_few_tool_calls(self):
        f = _make_finding(
            severity=Severity.CRITICAL,
            evidence=["ev1", "ev2"],
            start_line=10,
            file="app/service.py",
        )
        # Only 2 tool calls — below the ≥3 threshold
        result = evidence_gate([f], tool_calls_made=2)
        assert result[0].severity == Severity.WARNING

    def test_evidence_gate_ignores_warnings(self):
        # Warning severity findings should pass through untouched (no evidence check)
        f = _make_finding(
            severity=Severity.WARNING,
            evidence=[],  # no evidence — doesn't matter for warnings
            start_line=0,
            file="",
        )
        result = evidence_gate([f], tool_calls_made=0)
        assert len(result) == 1
        assert result[0].severity == Severity.WARNING

    def test_evidence_gate_appends_reason(self):
        f = _make_finding(
            severity=Severity.CRITICAL,
            evidence=["single evidence"],
            start_line=0,
            file="",
        )
        result = evidence_gate([f], tool_calls_made=1)
        # Should have an auto-downgraded note appended
        assert any("[auto-downgraded" in ev for ev in result[0].evidence)

    def test_evidence_gate_passes_nits_through(self):
        f = _make_finding(severity=Severity.NIT, evidence=[], start_line=0, file="")
        result = evidence_gate([f], tool_calls_made=0)
        assert result[0].severity == Severity.NIT


# ---------------------------------------------------------------------------
# post_filter
# ---------------------------------------------------------------------------

class TestPostFilter:
    def test_post_filter_drops_low_confidence(self):
        findings = [
            _make_finding(confidence=0.74),  # below MIN_CONFIDENCE of 0.75
            _make_finding(confidence=0.70),
        ]
        result = post_filter(findings)
        assert result == []

    def test_post_filter_keeps_high_confidence(self):
        findings = [
            _make_finding(confidence=0.75),  # exactly at boundary
            _make_finding(confidence=0.90),
        ]
        result = post_filter(findings)
        assert len(result) == 2

    def test_post_filter_caps_test_coverage_critical(self):
        f = _make_finding(
            title="Missing unit test",
            severity=Severity.CRITICAL,
            confidence=0.85,
            category=FindingCategory.TEST_COVERAGE,
        )
        result = post_filter([f])
        assert len(result) == 1
        assert result[0].severity == Severity.WARNING

    def test_post_filter_caps_missing_test_critical(self):
        f = _make_finding(
            title="Missing test for payment flow",
            severity=Severity.CRITICAL,
            confidence=0.85,
            category=FindingCategory.CORRECTNESS,  # not test_coverage but title matches
        )
        result = post_filter([f])
        assert len(result) == 1
        assert result[0].severity == Severity.WARNING

    def test_post_filter_does_not_cap_warning_test_coverage(self):
        f = _make_finding(
            severity=Severity.WARNING,
            confidence=0.85,
            category=FindingCategory.TEST_COVERAGE,
        )
        result = post_filter([f])
        assert result[0].severity == Severity.WARNING  # unchanged

    def test_post_filter_preserves_non_test_critical(self):
        f = _make_finding(
            title="SQL injection vulnerability",
            severity=Severity.CRITICAL,
            confidence=0.85,
            category=FindingCategory.SECURITY,
        )
        result = post_filter([f])
        assert result[0].severity == Severity.CRITICAL


# ---------------------------------------------------------------------------
# merge_recommendation
# ---------------------------------------------------------------------------

class TestMergeRecommendation:
    def test_merge_recommendation_approve(self):
        # No criticals, no warnings
        findings = [_make_finding(severity=Severity.NIT)]
        assert merge_recommendation(findings) == "approve"

    def test_merge_recommendation_approve_no_findings(self):
        assert merge_recommendation([]) == "approve"

    def test_merge_recommendation_approve_with_followups(self):
        # 1-2 warnings → approve_with_followups
        findings = [
            _make_finding(severity=Severity.WARNING),
            _make_finding(severity=Severity.WARNING),
        ]
        assert merge_recommendation(findings) == "approve_with_followups"

    def test_merge_recommendation_approve_with_followups_one_warning(self):
        findings = [_make_finding(severity=Severity.WARNING)]
        assert merge_recommendation(findings) == "approve_with_followups"

    def test_merge_recommendation_request_changes_critical(self):
        findings = [_make_finding(severity=Severity.CRITICAL)]
        assert merge_recommendation(findings) == "request_changes"

    def test_merge_recommendation_request_changes_many_warnings(self):
        # 3+ warnings → request_changes
        findings = [_make_finding(severity=Severity.WARNING) for _ in range(3)]
        assert merge_recommendation(findings) == "request_changes"

    def test_merge_recommendation_request_changes_four_warnings(self):
        findings = [_make_finding(severity=Severity.WARNING) for _ in range(4)]
        assert merge_recommendation(findings) == "request_changes"


# ---------------------------------------------------------------------------
# build_diffs_section
# ---------------------------------------------------------------------------

class TestBuildDiffsSection:
    def test_build_diffs_section_empty(self):
        files = [ChangedFile(path="app/foo.py", additions=5, deletions=0)]
        result = build_diffs_section(files, {})
        assert "diffs not available" in result

    def test_build_diffs_section_with_diffs(self):
        files = [ChangedFile(path="app/foo.py", additions=5, deletions=0)]
        file_diffs = {"app/foo.py": "diff --git a/app/foo.py b/app/foo.py\n+new line"}
        result = build_diffs_section(files, file_diffs)
        assert "app/foo.py" in result
        assert "new line" in result

    def test_build_diffs_section_truncates_large(self):
        files = [ChangedFile(path="app/big.py", additions=5000, deletions=0)]
        large_diff = "x" * (MAX_FILE_DIFF_CHARS + 5000)
        file_diffs = {"app/big.py": large_diff}
        result = build_diffs_section(files, file_diffs)
        # The diff should be truncated — look for truncation marker
        assert "truncated" in result

    def test_build_diffs_section_no_matching_files(self):
        files = [ChangedFile(path="app/foo.py", additions=5, deletions=0)]
        file_diffs = {"app/other.py": "diff content"}  # file not in list
        result = build_diffs_section(files, file_diffs)
        assert "no diffs available" in result

    def test_build_diffs_section_multiple_files(self):
        files = [
            ChangedFile(path="app/a.py", additions=5, deletions=0),
            ChangedFile(path="app/b.py", additions=3, deletions=1),
        ]
        file_diffs = {
            "app/a.py": "diff a content",
            "app/b.py": "diff b content",
        }
        result = build_diffs_section(files, file_diffs)
        assert "app/a.py" in result
        assert "app/b.py" in result


# ---------------------------------------------------------------------------
# compute_budget_multiplier
# ---------------------------------------------------------------------------

class TestComputeBudgetMultiplier:
    def test_budget_multiplier_small_pr(self):
        ctx = _make_pr_context(total_changed_lines=200)
        assert compute_budget_multiplier(ctx) == pytest.approx(0.5)

    def test_budget_multiplier_boundary_499(self):
        ctx = _make_pr_context(total_changed_lines=499)
        assert compute_budget_multiplier(ctx) == pytest.approx(0.5)

    def test_budget_multiplier_medium_pr(self):
        ctx = _make_pr_context(total_changed_lines=500)
        assert compute_budget_multiplier(ctx) == pytest.approx(1.0)

    def test_budget_multiplier_medium_pr_mid(self):
        ctx = _make_pr_context(total_changed_lines=1000)
        assert compute_budget_multiplier(ctx) == pytest.approx(1.0)

    def test_budget_multiplier_medium_pr_boundary(self):
        ctx = _make_pr_context(total_changed_lines=1999)
        assert compute_budget_multiplier(ctx) == pytest.approx(1.0)

    def test_budget_multiplier_large_pr(self):
        ctx = _make_pr_context(total_changed_lines=2000)
        assert compute_budget_multiplier(ctx) == pytest.approx(1.5)

    def test_budget_multiplier_large_pr_mid(self):
        ctx = _make_pr_context(total_changed_lines=3500)
        assert compute_budget_multiplier(ctx) == pytest.approx(1.5)

    def test_budget_multiplier_very_large_pr(self):
        ctx = _make_pr_context(total_changed_lines=5000)
        assert compute_budget_multiplier(ctx) == pytest.approx(2.0)

    def test_budget_multiplier_very_large_pr_huge(self):
        ctx = _make_pr_context(total_changed_lines=10000)
        assert compute_budget_multiplier(ctx) == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# should_reject_pr
# ---------------------------------------------------------------------------

class TestShouldRejectPr:
    def test_should_reject_pr_within_limit(self):
        ctx = _make_pr_context(total_changed_lines=7999)
        assert should_reject_pr(ctx) is None

    def test_should_reject_pr_exact_limit(self):
        ctx = _make_pr_context(total_changed_lines=8000)
        assert should_reject_pr(ctx) is None

    def test_should_reject_pr_over_limit(self):
        ctx = _make_pr_context(total_changed_lines=8001)
        result = should_reject_pr(ctx)
        assert result is not None
        assert "8,001" in result

    def test_should_reject_pr_message_content(self):
        ctx = _make_pr_context(total_changed_lines=10000, file_count=2)
        result = should_reject_pr(ctx)
        assert "too large" in result
        assert "split" in result.lower()


# ---------------------------------------------------------------------------
# AGENT_CATEGORIES constant
# ---------------------------------------------------------------------------

class TestAgentCategories:
    def test_agent_categories_has_expected_keys(self):
        expected = {"correctness", "concurrency", "security", "reliability", "test_coverage"}
        assert set(AGENT_CATEGORIES.keys()) == expected

    def test_agent_categories_values_are_finding_categories(self):
        for category in AGENT_CATEGORIES.values():
            assert isinstance(category, FindingCategory)
