"""Tests for the AI Code Review module.

Covers:
  - Diff parser (file classification, PRContext construction)
  - Risk classifier (5-dimension risk profile)
  - Dedup / merge layer
  - Ranking / scoring layer
  - Agent spec selection and query building
  - Service orchestration (with mocked agents)
  - API endpoint schemas
"""
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.code_review.models import (
    AgentReviewResult,
    ChangedFile,
    FileCategory,
    FindingCategory,
    PRContext,
    ReviewFinding,
    ReviewResult,
    RiskLevel,
    RiskProfile,
    Severity,
)


# =========================================================================
# Diff Parser — file classification
# =========================================================================


class TestFileClassification:
    def test_python_test_file(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("tests/test_auth.py") == FileCategory.TEST

    def test_java_test_file(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("src/test/java/AuthServiceTest.java") == FileCategory.TEST

    def test_js_spec_file(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("src/auth.spec.ts") == FileCategory.TEST

    def test_yaml_config(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("config/application.yml") == FileCategory.CONFIG

    def test_env_file(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file(".env.production") == FileCategory.CONFIG

    def test_dockerfile(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("Dockerfile") == FileCategory.INFRA

    def test_github_workflow(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file(".github/workflows/ci.yml") == FileCategory.INFRA

    def test_migration_file(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("alembic/versions/001_init.py") == FileCategory.SCHEMA

    def test_sql_file(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("db/schema.sql") == FileCategory.SCHEMA

    def test_lock_file(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("package-lock.json") == FileCategory.GENERATED

    def test_vendor_dir(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("vendor/github.com/pkg/errors/errors.go") == FileCategory.GENERATED

    def test_business_logic(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("app/services/auth_service.py") == FileCategory.BUSINESS_LOGIC

    def test_java_controller(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("src/main/java/com/app/UserController.java") == FileCategory.BUSINESS_LOGIC

    def test_go_test(self):
        from app.code_review.diff_parser import _classify_file
        assert _classify_file("pkg/auth/handler_test.go") == FileCategory.TEST


class TestDiffParser:
    def test_parse_diff_empty(self):
        from app.code_review.diff_parser import parse_diff
        with patch("app.code_review.diff_parser.git_diff_files") as mock_gdf:
            mock_gdf.return_value = MagicMock(success=True, data=[])
            ctx = parse_diff("/fake/ws", "main...feature")
            assert ctx.file_count == 0
            assert ctx.total_changed_lines == 0

    def test_parse_diff_with_files(self):
        from app.code_review.diff_parser import parse_diff
        with patch("app.code_review.diff_parser.git_diff_files") as mock_gdf:
            mock_gdf.return_value = MagicMock(success=True, data=[
                {"path": "app/service.py", "status": "modified", "additions": 30, "deletions": 10},
                {"path": "tests/test_service.py", "status": "modified", "additions": 20, "deletions": 5},
                {"path": "config/settings.yml", "status": "modified", "additions": 2, "deletions": 1},
            ])
            ctx = parse_diff("/fake/ws", "main...feature")
            assert ctx.file_count == 3
            assert ctx.total_additions == 52
            assert ctx.total_deletions == 16
            assert ctx.total_changed_lines == 68
            assert len(ctx.business_logic_files()) == 1
            assert len(ctx.test_files()) == 1
            assert len(ctx.config_files()) == 1

    def test_parse_diff_failure(self):
        from app.code_review.diff_parser import parse_diff
        with patch("app.code_review.diff_parser.git_diff_files") as mock_gdf:
            mock_gdf.return_value = MagicMock(success=False, error="bad ref", data=None)
            ctx = parse_diff("/fake/ws", "bad...ref")
            assert ctx.file_count == 0


# =========================================================================
# Risk Classifier
# =========================================================================


class TestRiskClassifier:
    def _make_context(self, paths):
        files = [ChangedFile(path=p, additions=50, deletions=20) for p in paths]
        return PRContext(
            diff_spec="main...feature",
            files=files,
            total_additions=sum(f.additions for f in files),
            total_deletions=sum(f.deletions for f in files),
            total_changed_lines=sum(f.additions + f.deletions for f in files),
            file_count=len(files),
        )

    def test_low_risk_simple_change(self):
        from app.code_review.risk_classifier import classify_risk
        ctx = self._make_context(["app/utils.py", "app/helpers.py"])
        risk = classify_risk(ctx)
        assert risk.correctness == RiskLevel.LOW

    def test_security_risk_from_auth_file(self):
        from app.code_review.risk_classifier import classify_risk
        ctx = self._make_context([
            "app/auth/login.py",
            "app/auth/session.py",
            "app/auth/jwt_handler.py",
        ])
        risk = classify_risk(ctx)
        assert risk.security in (RiskLevel.MEDIUM, RiskLevel.HIGH)

    def test_concurrency_risk_from_queue_consumer(self):
        from app.code_review.risk_classifier import classify_risk
        ctx = self._make_context([
            "app/consumers/order_consumer.py",
            "app/handlers/webhook_handler.py",
            "app/workers/retry_worker.py",
        ])
        risk = classify_risk(ctx)
        assert risk.concurrency in (RiskLevel.MEDIUM, RiskLevel.HIGH)

    def test_correctness_boosted_for_large_prs(self):
        from app.code_review.risk_classifier import classify_risk
        files = [ChangedFile(path=f"app/service_{i}.py", additions=100, deletions=50,
                            category=FileCategory.BUSINESS_LOGIC)
                 for i in range(12)]
        ctx = PRContext(
            diff_spec="main...feature",
            files=files,
            total_additions=1200,
            total_deletions=600,
            total_changed_lines=1800,
            file_count=12,
        )
        risk = classify_risk(ctx)
        assert risk.correctness in (RiskLevel.MEDIUM, RiskLevel.HIGH)

    def test_operational_risk_from_config_changes(self):
        from app.code_review.risk_classifier import classify_risk
        ctx = self._make_context([
            "config/app.yml",
            "config/db.yml",
            "config/cache.yml",
            "app/service.py",
        ])
        risk = classify_risk(ctx)
        assert risk.operational in (RiskLevel.MEDIUM, RiskLevel.HIGH)


# =========================================================================
# Dedup
# =========================================================================


class TestDedup:
    def test_no_dedup_for_different_files(self):
        from app.code_review.dedup import dedup_findings
        findings = [
            ReviewFinding(title="Bug in auth", category=FindingCategory.CORRECTNESS,
                         severity=Severity.WARNING, file="auth.py", start_line=10, end_line=20),
            ReviewFinding(title="Bug in service", category=FindingCategory.CORRECTNESS,
                         severity=Severity.WARNING, file="service.py", start_line=10, end_line=20),
        ]
        result = dedup_findings(findings)
        assert len(result) == 2

    def test_dedup_overlapping_lines(self):
        from app.code_review.dedup import dedup_findings
        findings = [
            ReviewFinding(title="Race condition", category=FindingCategory.CONCURRENCY,
                         severity=Severity.CRITICAL, confidence=0.9,
                         file="handler.py", start_line=10, end_line=30,
                         evidence=["check then act"], agent="concurrency"),
            ReviewFinding(title="Race condition risk", category=FindingCategory.SECURITY,
                         severity=Severity.WARNING, confidence=0.7,
                         file="handler.py", start_line=15, end_line=25,
                         evidence=["replay attack"], agent="security"),
        ]
        result = dedup_findings(findings)
        assert len(result) == 1
        # Should keep the critical severity
        assert result[0].severity == Severity.CRITICAL
        # Evidence merged
        assert len(result[0].evidence) == 2
        # Both agents attributed
        assert "concurrency" in result[0].agent
        assert "security" in result[0].agent

    def test_dedup_similar_titles(self):
        from app.code_review.dedup import dedup_findings
        findings = [
            ReviewFinding(title="Missing null check in handler",
                         category=FindingCategory.CORRECTNESS,
                         severity=Severity.WARNING, confidence=0.8,
                         file="handler.py", agent="correctness"),
            ReviewFinding(title="Null check missing in handler",
                         category=FindingCategory.CORRECTNESS,
                         severity=Severity.NIT, confidence=0.6,
                         file="handler.py", agent="reliability"),
        ]
        result = dedup_findings(findings)
        assert len(result) == 1

    def test_single_finding_no_dedup(self):
        from app.code_review.dedup import dedup_findings
        findings = [
            ReviewFinding(title="Test", category=FindingCategory.CORRECTNESS,
                         severity=Severity.WARNING),
        ]
        assert len(dedup_findings(findings)) == 1

    def test_empty_findings(self):
        from app.code_review.dedup import dedup_findings
        assert dedup_findings([]) == []


# =========================================================================
# Ranking
# =========================================================================


class TestRanking:
    def test_critical_ranked_first(self):
        from app.code_review.ranking import score_and_rank
        pr_ctx = PRContext(diff_spec="main...f", files=[
            ChangedFile(path="auth.py", additions=50, deletions=10,
                       category=FileCategory.BUSINESS_LOGIC),
        ], file_count=1)

        findings = [
            ReviewFinding(title="Nit", category=FindingCategory.STYLE,
                         severity=Severity.NIT, confidence=0.9, file="auth.py", start_line=1),
            ReviewFinding(title="Critical bug", category=FindingCategory.CORRECTNESS,
                         severity=Severity.CRITICAL, confidence=0.9, file="auth.py", start_line=10),
            ReviewFinding(title="Warning", category=FindingCategory.SECURITY,
                         severity=Severity.WARNING, confidence=0.8, file="auth.py", start_line=5),
        ]
        ranked = score_and_rank(findings, pr_ctx)
        assert ranked[0].severity == Severity.CRITICAL
        assert ranked[-1].severity == Severity.NIT

    def test_praise_ranked_last(self):
        from app.code_review.ranking import score_and_rank
        pr_ctx = PRContext(diff_spec="main...f", files=[], file_count=0)

        findings = [
            ReviewFinding(title="Good job", category=FindingCategory.CORRECTNESS,
                         severity=Severity.PRAISE, file="x.py"),
            ReviewFinding(title="Bug", category=FindingCategory.CORRECTNESS,
                         severity=Severity.WARNING, confidence=0.8, file="x.py"),
        ]
        ranked = score_and_rank(findings, pr_ctx)
        assert ranked[-1].severity == Severity.PRAISE

    def test_evidence_quality_boosts_score(self):
        from app.code_review.ranking import _evidence_quality
        # Finding with file, line, evidence, and fix
        f = ReviewFinding(
            title="Bug", category=FindingCategory.CORRECTNESS,
            severity=Severity.WARNING, file="a.py", start_line=10,
            evidence=["e1", "e2"], suggested_fix="fix it",
        )
        score = _evidence_quality(f)
        assert score >= 0.9


# =========================================================================
# Agent specs
# =========================================================================


class TestAgentSpecs:
    def test_correctness_runs_on_high_risk(self):
        from app.code_review.agents import AGENT_SPECS
        spec = [s for s in AGENT_SPECS if s.name == "correctness"][0]
        profile = RiskProfile(correctness=RiskLevel.HIGH)
        assert spec.should_run(profile)

    def test_correctness_skips_on_low_risk(self):
        from app.code_review.agents import AGENT_SPECS
        spec = [s for s in AGENT_SPECS if s.name == "correctness"][0]
        profile = RiskProfile()  # all low
        assert not spec.should_run(profile)

    def test_test_coverage_always_runs(self):
        from app.code_review.agents import AGENT_SPECS
        spec = [s for s in AGENT_SPECS if s.name == "test_coverage"][0]
        profile = RiskProfile()  # all low
        assert spec.should_run(profile, always_run=True)

    def test_agent_query_includes_diff_spec(self):
        from app.code_review.agents import AGENT_SPECS, _build_agent_query
        spec = AGENT_SPECS[0]
        ctx = PRContext(diff_spec="main...feature", file_count=3, total_changed_lines=100,
                       files=[ChangedFile(path="a.py", additions=50, deletions=10,
                                         category=FileCategory.BUSINESS_LOGIC)])
        profile = RiskProfile()
        query = _build_agent_query(spec, ctx, profile)
        assert "main...feature" in query
        assert "a.py" in query

    def test_git_diff_files_not_in_agent_tools(self):
        """git_diff_files should NOT be in any agent tool set (diffs are pre-fetched)."""
        from app.code_review.agents import AGENT_SPECS
        for spec in AGENT_SPECS:
            assert "git_diff_files" not in spec.tools, \
                f"Agent '{spec.name}' still has git_diff_files in tool set"

    def test_agent_query_includes_prefetched_diffs(self):
        """When file_diffs are provided, the agent query includes them inline."""
        from app.code_review.agents import AGENT_SPECS, _build_agent_query
        spec = AGENT_SPECS[0]  # correctness
        ctx = PRContext(diff_spec="main...feature", file_count=1, total_changed_lines=20,
                       files=[ChangedFile(path="app/service.py", additions=15, deletions=5,
                                         category=FileCategory.BUSINESS_LOGIC)])
        profile = RiskProfile()
        diffs = {"app/service.py": "diff --git a/app/service.py b/app/service.py\n+new line"}
        query = _build_agent_query(spec, ctx, profile, file_diffs=diffs)
        assert "+new line" in query
        assert "app/service.py" in query
        # Should NOT tell agent to call git_diff_files
        assert "Use **git_diff_files**" not in query

    def test_agent_query_without_diffs_shows_fallback(self):
        """When no diffs are available, prompt tells agent to use git_diff."""
        from app.code_review.agents import AGENT_SPECS, _build_agent_query
        spec = AGENT_SPECS[0]
        ctx = PRContext(diff_spec="main...feature", file_count=1, total_changed_lines=20,
                       files=[ChangedFile(path="app/service.py", additions=15, deletions=5,
                                         category=FileCategory.BUSINESS_LOGIC)])
        profile = RiskProfile()
        query = _build_agent_query(spec, ctx, profile, file_diffs={})
        assert "diffs not available" in query

    def test_agent_query_truncates_large_diffs(self):
        """Individual file diffs exceeding 8KB are truncated."""
        from app.code_review.agents import AGENT_SPECS, _build_agent_query
        spec = AGENT_SPECS[0]
        ctx = PRContext(diff_spec="main...feature", file_count=1, total_changed_lines=500,
                       files=[ChangedFile(path="big.py", additions=400, deletions=100,
                                         category=FileCategory.BUSINESS_LOGIC)])
        profile = RiskProfile()
        large_diff = "diff --git a/big.py b/big.py\n" + "+x\n" * 5000  # >8KB
        diffs = {"big.py": large_diff}
        query = _build_agent_query(spec, ctx, profile, file_diffs=diffs)
        assert "truncated" in query
        # Should still contain the beginning of the diff
        assert "diff --git a/big.py b/big.py" in query


class TestFindingsParsing:
    def test_parse_json_findings(self):
        from app.code_review.agents import _parse_findings, AGENT_SPECS
        spec = AGENT_SPECS[0]  # correctness
        answer = """
Here are my findings:

```json
[
  {
    "title": "Null check missing",
    "severity": "warning",
    "confidence": 0.85,
    "file": "handler.py",
    "start_line": 42,
    "end_line": 55,
    "evidence": ["user can be None", "no None check before .name"],
    "risk": "NullPointerException in production",
    "suggested_fix": "Add `if user is None: return 404`"
  }
]
```
"""
        findings = _parse_findings(answer, spec)
        assert len(findings) == 1
        assert findings[0].title == "Null check missing"
        assert findings[0].severity == Severity.WARNING
        assert findings[0].confidence == 0.85
        assert findings[0].agent == "correctness"

    def test_parse_empty_array(self):
        from app.code_review.agents import _parse_findings, AGENT_SPECS
        spec = AGENT_SPECS[0]
        findings = _parse_findings("No issues found.\n[]", spec)
        assert findings == []

    def test_parse_no_json(self):
        from app.code_review.agents import _parse_findings, AGENT_SPECS
        spec = AGENT_SPECS[0]
        findings = _parse_findings("The code looks good, no issues.", spec)
        assert findings == []

    def test_parse_malformed_json(self):
        from app.code_review.agents import _parse_findings, AGENT_SPECS
        spec = AGENT_SPECS[0]
        findings = _parse_findings("[{bad json}]", spec)
        assert findings == []


# =========================================================================
# Service — budget calculation
# =========================================================================


class TestBudgetCalculation:
    def test_small_pr_half_budget(self):
        from app.code_review.service import _compute_budget_multiplier
        ctx = PRContext(diff_spec="x", total_changed_lines=200)
        assert _compute_budget_multiplier(ctx) == 0.5

    def test_medium_pr_standard_budget(self):
        from app.code_review.service import _compute_budget_multiplier
        ctx = PRContext(diff_spec="x", total_changed_lines=1000)
        assert _compute_budget_multiplier(ctx) == 1.0

    def test_large_pr_boosted_budget(self):
        from app.code_review.service import _compute_budget_multiplier
        ctx = PRContext(diff_spec="x", total_changed_lines=3000)
        assert _compute_budget_multiplier(ctx) == 1.5

    def test_very_large_pr_double_budget(self):
        from app.code_review.service import _compute_budget_multiplier
        ctx = PRContext(diff_spec="x", total_changed_lines=6000)
        assert _compute_budget_multiplier(ctx) == 2.0


class TestPRRejection:
    def test_normal_pr_not_rejected(self):
        from app.code_review.service import _should_reject_pr
        ctx = PRContext(diff_spec="x", total_changed_lines=3000, file_count=20)
        assert _should_reject_pr(ctx) is None

    def test_huge_pr_rejected(self):
        from app.code_review.service import _should_reject_pr
        ctx = PRContext(diff_spec="x", total_changed_lines=9000, file_count=50,
                       files=[ChangedFile(path=f"file_{i}.py", additions=100, deletions=80)
                              for i in range(50)])
        msg = _should_reject_pr(ctx)
        assert msg is not None
        assert "9,000" in msg


class TestPrefetchDiffs:
    """Test the _prefetch_diffs function that fetches all diffs in one git call."""

    def test_prefetch_splits_by_file(self):
        from app.code_review.service import _prefetch_diffs
        fake_diff = (
            "diff --git a/app/auth.py b/app/auth.py\n"
            "index abc..def 100644\n"
            "--- a/app/auth.py\n"
            "+++ b/app/auth.py\n"
            "@@ -1,3 +1,4 @@\n"
            "+import jwt\n"
            " class Auth:\n"
            "diff --git a/app/db.py b/app/db.py\n"
            "index 111..222 100644\n"
            "--- a/app/db.py\n"
            "+++ b/app/db.py\n"
            "@@ -10,2 +10,3 @@\n"
            "+new_query()\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=fake_diff)
            diffs = _prefetch_diffs("/fake/ws", "main...feature")

        assert len(diffs) == 2
        assert "app/auth.py" in diffs
        assert "app/db.py" in diffs
        assert "+import jwt" in diffs["app/auth.py"]
        assert "+new_query()" in diffs["app/db.py"]

    def test_prefetch_returns_empty_on_failure(self):
        from app.code_review.service import _prefetch_diffs
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=128, stderr="bad ref")
            diffs = _prefetch_diffs("/fake/ws", "bad...ref")
        assert diffs == {}

    def test_prefetch_returns_empty_on_exception(self):
        from app.code_review.service import _prefetch_diffs
        with patch("subprocess.run", side_effect=OSError("no git")):
            diffs = _prefetch_diffs("/fake/ws", "main...feature")
        assert diffs == {}

    def test_prefetch_handles_empty_diff(self):
        from app.code_review.service import _prefetch_diffs
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            diffs = _prefetch_diffs("/fake/ws", "main...feature")
        assert diffs == {}

    def test_prefetch_passes_diff_spec_as_args(self):
        """Diff spec like 'main...feature' should be split and passed to git."""
        from app.code_review.service import _prefetch_diffs
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="")
            _prefetch_diffs("/fake/ws", "main...feature/branch")
            args = mock_run.call_args[0][0]
            assert args == ["git", "diff", "--unified=10", "main...feature/branch"]


class TestMergeRecommendation:
    def test_approve_no_issues(self):
        from app.code_review.service import _merge_recommendation
        assert _merge_recommendation([]) == "approve"

    def test_request_changes_on_critical(self):
        from app.code_review.service import _merge_recommendation
        findings = [ReviewFinding(title="x", category=FindingCategory.CORRECTNESS,
                                 severity=Severity.CRITICAL)]
        assert _merge_recommendation(findings) == "request_changes"

    def test_approve_with_followups(self):
        from app.code_review.service import _merge_recommendation
        findings = [ReviewFinding(title="x", category=FindingCategory.CORRECTNESS,
                                 severity=Severity.WARNING)]
        assert _merge_recommendation(findings) == "approve_with_followups"

    def test_request_changes_many_warnings(self):
        from app.code_review.service import _merge_recommendation
        findings = [ReviewFinding(title=f"w{i}", category=FindingCategory.CORRECTNESS,
                                 severity=Severity.WARNING) for i in range(4)]
        assert _merge_recommendation(findings) == "request_changes"


# =========================================================================
# PRContext helpers
# =========================================================================


class TestPRContext:
    def test_business_logic_files(self):
        ctx = PRContext(diff_spec="x", files=[
            ChangedFile(path="app/service.py", category=FileCategory.BUSINESS_LOGIC),
            ChangedFile(path="tests/test.py", category=FileCategory.TEST),
            ChangedFile(path="config/app.yml", category=FileCategory.CONFIG),
        ])
        assert len(ctx.business_logic_files()) == 1
        assert len(ctx.test_files()) == 1
        assert len(ctx.config_files()) == 1

    def test_finding_score(self):
        f = ReviewFinding(title="x", category=FindingCategory.CORRECTNESS,
                         severity=Severity.CRITICAL, confidence=0.9)
        assert f.score() == 0.9  # 1.0 * 0.9

    def test_risk_profile_max(self):
        profile = RiskProfile(
            correctness=RiskLevel.LOW,
            concurrency=RiskLevel.HIGH,
            security=RiskLevel.MEDIUM,
        )
        assert profile.max_risk() == RiskLevel.HIGH


# =========================================================================
# API endpoint test
# =========================================================================


class TestCodeReviewAPI:
    def test_review_endpoint_no_provider(self, api_client):
        """POST /api/code-review/review returns 503 when no provider."""
        from app.main import app
        # Ensure git_workspace_service exists so the dependency resolves
        if not hasattr(app.state, "git_workspace_service"):
            app.state.git_workspace_service = MagicMock()
        app.state.agent_provider = None
        response = api_client.post("/api/code-review/review", json={
            "room_id": "test-room",
            "diff_spec": "main...feature",
        })
        assert response.status_code == 503


# =========================================================================
# Query classifier — diff_spec extraction
# =========================================================================


class TestClassifierDiffSpec:
    """Verify that classify_query extracts diff_spec for PR patterns."""

    def test_do_pr_pattern(self):
        from app.agent_loop.query_classifier import classify_query
        c = classify_query("do PR master...feature/auth")
        assert c.query_type == "code_review"
        assert c.diff_spec == "master...feature/auth"

    def test_review_diff_pattern(self):
        from app.agent_loop.query_classifier import classify_query
        c = classify_query("review diff main...feature/payment")
        assert c.query_type == "code_review"
        assert c.diff_spec == "main...feature/payment"

    def test_at_ai_do_pr(self):
        from app.agent_loop.query_classifier import classify_query
        c = classify_query("@AI do PR master...feature/branch-name")
        assert c.query_type == "code_review"
        assert c.diff_spec == "master...feature/branch-name"

    def test_code_review_head(self):
        from app.agent_loop.query_classifier import classify_query
        c = classify_query("code review HEAD~5")
        # HEAD~5 doesn't contain ... so _detect_pr_pattern may or may not match
        assert c.query_type == "code_review"

    def test_non_pr_query_no_diff_spec(self):
        from app.agent_loop.query_classifier import classify_query
        c = classify_query("how does the auth module work?")
        assert c.diff_spec is None

    def test_chinese_review(self):
        from app.agent_loop.query_classifier import classify_query
        c = classify_query("审核 PR main...feature/login")
        assert c.query_type == "code_review"
        assert c.diff_spec == "main...feature/login"


# =========================================================================
# Multi-agent review delegation
# =========================================================================


class TestMultiAgentDelegation:
    """Test the run_stream delegation to CodeReviewService."""

    @pytest.mark.asyncio
    async def test_delegation_yields_events(self):
        """When diff_spec is detected, run_stream yields review events."""
        from app.agent_loop.service import AgentLoopService, AgentEvent
        from app.ai_provider.base import AIProvider, ToolUseResponse

        # Mock provider
        provider = MagicMock(spec=AIProvider)

        # Mock CodeReviewService.review() to return a canned result
        mock_result = ReviewResult(
            diff_spec="main...feature/auth",
            pr_summary="## Code Review: main...feature/auth\n\nLooks good.",
            risk_profile=RiskProfile(
                correctness=RiskLevel.LOW,
                security=RiskLevel.MEDIUM,
            ),
            findings=[
                ReviewFinding(
                    title="Missing null check",
                    category=FindingCategory.CORRECTNESS,
                    severity=Severity.WARNING,
                    confidence=0.8,
                    file="app/auth.py",
                    start_line=10,
                    agent="correctness",
                ),
            ],
            agent_results=[
                AgentReviewResult(
                    agent_name="correctness",
                    findings=[ReviewFinding(
                        title="Missing null check",
                        category=FindingCategory.CORRECTNESS,
                        severity=Severity.WARNING,
                        confidence=0.8,
                    )],
                    tokens_used=5000,
                    iterations=3,
                    duration_ms=1200.0,
                ),
                AgentReviewResult(
                    agent_name="security",
                    findings=[],
                    tokens_used=3000,
                    iterations=2,
                    duration_ms=800.0,
                ),
            ],
            total_tokens=8000,
            total_iterations=5,
            total_duration_ms=2000.0,
            merge_recommendation="approve_with_followups",
        )

        agent = AgentLoopService(
            provider=provider,
            max_iterations=20,
        )

        events = []
        with patch(
            "app.code_review.service.CodeReviewService"
        ) as MockService:
            mock_svc = MockService.return_value
            mock_svc.review = AsyncMock(return_value=mock_result)

            async for event in agent.run_stream(
                query="@AI do PR main...feature/auth",
                workspace_path="/tmp/test-workspace",
            ):
                events.append(event)

        # Verify event sequence
        kinds = [e.kind for e in events]
        assert "thinking" in kinds
        assert "tool_call" in kinds
        assert "tool_result" in kinds
        assert "done" in kinds

        # Verify the done event has the answer
        done_event = [e for e in events if e.kind == "done"][0]
        assert "Missing null check" in done_event.data["answer"]
        assert done_event.data["tool_calls_made"] == 2  # 2 agents

        # Verify per-agent results
        agent_results = [e for e in events if e.kind == "tool_result"]
        agent_names = {e.data["tool"] for e in agent_results}
        assert "correctness" in agent_names
        assert "security" in agent_names

    @pytest.mark.asyncio
    async def test_delegation_error_handling(self):
        """If CodeReviewService fails, yield an error event."""
        from app.agent_loop.service import AgentLoopService
        from app.ai_provider.base import AIProvider

        provider = MagicMock(spec=AIProvider)
        agent = AgentLoopService(provider=provider, max_iterations=20)

        events = []
        with patch(
            "app.code_review.service.CodeReviewService"
        ) as MockService:
            mock_svc = MockService.return_value
            mock_svc.review = AsyncMock(side_effect=RuntimeError("git diff failed"))

            async for event in agent.run_stream(
                query="do PR main...feature/broken",
                workspace_path="/tmp/test-workspace",
            ):
                events.append(event)

        kinds = [e.kind for e in events]
        assert "error" in kinds
        error_event = [e for e in events if e.kind == "error"][0]
        assert "git diff failed" in error_event.data["error"]

    @pytest.mark.asyncio
    async def test_non_pr_query_uses_normal_loop(self):
        """Queries without diff_spec should use the normal agent loop."""
        from app.agent_loop.service import AgentLoopService
        from app.ai_provider.base import AIProvider, ToolUseResponse

        provider = MagicMock(spec=AIProvider)
        provider.chat_with_tools.return_value = ToolUseResponse(
            text="Auth uses JWT tokens.", stop_reason="end_turn",
        )
        agent = AgentLoopService(provider=provider, max_iterations=5)

        events = []
        async for event in agent.run_stream(
            query="how does auth work?",
            workspace_path="/tmp/test-workspace",
        ):
            events.append(event)

        # Should NOT have delegated to CodeReviewService
        kinds = [e.kind for e in events]
        assert "done" in kinds
        done_event = [e for e in events if e.kind == "done"][0]
        # Normal agent loop answer, not a review
        assert "Missing null check" not in done_event.data.get("answer", "")


# =========================================================================
# Format review result
# =========================================================================


class TestFormatReviewResult:
    """Test _format_review_result markdown output."""

    def test_basic_format(self):
        from app.agent_loop.service import AgentLoopService

        result = ReviewResult(
            diff_spec="main...feature/test",
            pr_summary="## Code Review\n\n2 files reviewed",
            findings=[
                ReviewFinding(
                    title="SQL injection risk",
                    category=FindingCategory.SECURITY,
                    severity=Severity.CRITICAL,
                    confidence=0.95,
                    file="app/db.py",
                    start_line=42,
                    end_line=45,
                    evidence=["Unsanitized input in query"],
                    risk="User input flows to SQL",
                    suggested_fix="Use parameterized queries",
                    agent="security",
                ),
                ReviewFinding(
                    title="Good error handling",
                    category=FindingCategory.RELIABILITY,
                    severity=Severity.PRAISE,
                    confidence=0.9,
                    file="app/auth.py",
                    start_line=10,
                    agent="reliability",
                ),
            ],
            agent_results=[
                AgentReviewResult(
                    agent_name="security",
                    findings=[],
                    tokens_used=5000,
                    duration_ms=1000.0,
                ),
            ],
            total_tokens=5000,
            total_duration_ms=1000.0,
        )

        md = AgentLoopService._format_review_result(result)

        # Contains the summary
        assert "Code Review" in md
        # Contains the finding
        assert "SQL injection risk" in md
        assert "CRITICAL" in md
        assert "`app/db.py:42-45`" in md
        assert "parameterized queries" in md
        # Contains praise
        assert "Good error handling" in md
        # Contains agent summary
        assert "security" in md
        assert "5,000 tokens" in md

    def test_no_findings(self):
        from app.agent_loop.service import AgentLoopService

        result = ReviewResult(
            diff_spec="main...feature/clean",
            pr_summary="No issues found.",
            findings=[],
            agent_results=[],
            total_tokens=1000,
            total_duration_ms=500.0,
        )

        md = AgentLoopService._format_review_result(result)
        assert "No issues found" in md
        assert "Findings" not in md  # no findings section

    def test_synthesis_preferred_over_structured(self):
        """When synthesis is present, it should be used instead of structured findings."""
        from app.agent_loop.service import AgentLoopService

        result = ReviewResult(
            diff_spec="main...feature/synth",
            pr_summary="## Structured Summary\n\nThis is the fallback.",
            findings=[
                ReviewFinding(
                    title="Some issue",
                    category=FindingCategory.CORRECTNESS,
                    severity=Severity.WARNING,
                    file="app/foo.py",
                    start_line=10,
                    agent="correctness",
                ),
            ],
            agent_results=[
                AgentReviewResult(
                    agent_name="correctness",
                    findings=[],
                    tokens_used=3000,
                    duration_ms=800.0,
                ),
            ],
            total_tokens=10000,
            total_duration_ms=2000.0,
            synthesis="## Code Review Summary\n\nThis PR looks good overall.\n\n### Recommendation\n**Approve**",
        )

        md = AgentLoopService._format_review_result(result)

        # Synthesis content should appear
        assert "This PR looks good overall." in md
        assert "**Approve**" in md
        # Structured fallback should NOT appear
        assert "Structured Summary" not in md
        assert "This is the fallback" not in md
        # Agent summary should still appear
        assert "correctness" in md
        assert "3,000 tokens" in md

    def test_empty_synthesis_falls_back_to_structured(self):
        """When synthesis is empty, structured format should be used."""
        from app.agent_loop.service import AgentLoopService

        result = ReviewResult(
            diff_spec="main...feature/fallback",
            pr_summary="## Structured Summary\n\nFallback content.",
            findings=[],
            agent_results=[],
            total_tokens=1000,
            total_duration_ms=500.0,
            synthesis="",  # empty synthesis
        )

        md = AgentLoopService._format_review_result(result)
        assert "Structured Summary" in md
        assert "Fallback content" in md


class TestSynthesis:
    """Test the synthesis pass (strong model produces polished review)."""

    @pytest.mark.asyncio
    async def test_synthesis_calls_strong_model(self):
        """Synthesis calls provider.call_model with findings and PR context."""
        from app.code_review.service import _synthesize_findings

        mock_provider = MagicMock()
        mock_provider.call_model.return_value = "## Synthesized Review\n\nAll good."

        pr_context = PRContext(
            diff_spec="main...feature/synth",
            files=[
                ChangedFile(path="app/foo.py", additions=10, deletions=5),
            ],
            total_additions=10,
            total_deletions=5,
            total_changed_lines=15,
            file_count=1,
        )
        risk_profile = RiskProfile(correctness=RiskLevel.MEDIUM)
        findings = [
            ReviewFinding(
                title="Null check missing",
                category=FindingCategory.CORRECTNESS,
                severity=Severity.WARNING,
                confidence=0.85,
                file="app/foo.py",
                start_line=10,
                risk="Could crash on None input",
                suggested_fix="Add if x is None check",
                agent="correctness",
            ),
        ]

        result = await _synthesize_findings(
            provider=mock_provider,
            pr_context=pr_context,
            risk_profile=risk_profile,
            findings=findings,
            merge_rec="approve_with_followups",
            file_diffs={"app/foo.py": "+def foo():\n+    return x.bar()"},
        )

        assert result == "## Synthesized Review\n\nAll good."
        mock_provider.call_model.assert_called_once()
        call_kwargs = mock_provider.call_model.call_args
        # Prompt should contain finding details
        assert "Null check missing" in call_kwargs.kwargs.get("prompt", call_kwargs[1].get("prompt", ""))

    @pytest.mark.asyncio
    async def test_synthesis_fallback_on_error(self):
        """If synthesis fails, returns empty string (triggers structured fallback)."""
        from app.code_review.service import _synthesize_findings

        mock_provider = MagicMock()
        mock_provider.call_model.side_effect = Exception("Model unavailable")

        pr_context = PRContext(diff_spec="main...feature/err", file_count=1, total_changed_lines=10)
        risk_profile = RiskProfile()

        result = await _synthesize_findings(
            provider=mock_provider,
            pr_context=pr_context,
            risk_profile=risk_profile,
            findings=[],
            merge_rec="approve",
            file_diffs={},
        )

        assert result == ""
