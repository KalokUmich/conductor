"""Tests for the AI Code Review module.

Covers:
  - Diff parser (file classification, PRContext construction)
  - Risk classifier (5-dimension risk profile)
  - Dedup / merge layer
  - Ranking / scoring layer
  - Agent spec selection and query building
  - Service orchestration (with mocked agents)
  - API endpoint schemas
  - Impact graph context injection
  - Adversarial verification (defense attorney pass)
"""

from unittest.mock import MagicMock, patch

from app.code_review.models import (
    ChangedFile,
    FileCategory,
    FindingCategory,
    PRContext,
    ReviewFinding,
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
            mock_gdf.return_value = MagicMock(
                success=True,
                data=[
                    {"path": "app/service.py", "status": "modified", "additions": 30, "deletions": 10},
                    {"path": "tests/test_service.py", "status": "modified", "additions": 20, "deletions": 5},
                    {"path": "config/settings.yml", "status": "modified", "additions": 2, "deletions": 1},
                ],
            )
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

        ctx = self._make_context(
            [
                "app/auth/login.py",
                "app/auth/session.py",
                "app/auth/jwt_handler.py",
            ]
        )
        risk = classify_risk(ctx)
        assert risk.security in (RiskLevel.MEDIUM, RiskLevel.HIGH)

    def test_concurrency_risk_from_queue_consumer(self):
        from app.code_review.risk_classifier import classify_risk

        ctx = self._make_context(
            [
                "app/consumers/order_consumer.py",
                "app/handlers/webhook_handler.py",
                "app/workers/retry_worker.py",
            ]
        )
        risk = classify_risk(ctx)
        assert risk.concurrency in (RiskLevel.MEDIUM, RiskLevel.HIGH)

    def test_correctness_boosted_for_large_prs(self):
        from app.code_review.risk_classifier import classify_risk

        files = [
            ChangedFile(path=f"app/service_{i}.py", additions=100, deletions=50, category=FileCategory.BUSINESS_LOGIC)
            for i in range(12)
        ]
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

        ctx = self._make_context(
            [
                "config/app.yml",
                "config/db.yml",
                "config/cache.yml",
                "app/service.py",
            ]
        )
        risk = classify_risk(ctx)
        assert risk.operational in (RiskLevel.MEDIUM, RiskLevel.HIGH)


# =========================================================================
# Dedup
# =========================================================================


class TestDedup:
    def test_no_dedup_for_different_files(self):
        from app.code_review.dedup import dedup_findings

        findings = [
            ReviewFinding(
                title="Bug in auth",
                category=FindingCategory.CORRECTNESS,
                severity=Severity.WARNING,
                file="auth.py",
                start_line=10,
                end_line=20,
            ),
            ReviewFinding(
                title="Bug in service",
                category=FindingCategory.CORRECTNESS,
                severity=Severity.WARNING,
                file="service.py",
                start_line=10,
                end_line=20,
            ),
        ]
        result = dedup_findings(findings)
        assert len(result) == 2

    def test_dedup_overlapping_lines(self):
        from app.code_review.dedup import dedup_findings

        findings = [
            ReviewFinding(
                title="Race condition",
                category=FindingCategory.CONCURRENCY,
                severity=Severity.CRITICAL,
                confidence=0.9,
                file="handler.py",
                start_line=10,
                end_line=30,
                evidence=["check then act"],
                agent="concurrency",
            ),
            ReviewFinding(
                title="Race condition risk",
                category=FindingCategory.SECURITY,
                severity=Severity.WARNING,
                confidence=0.7,
                file="handler.py",
                start_line=15,
                end_line=25,
                evidence=["replay attack"],
                agent="security",
            ),
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
            ReviewFinding(
                title="Missing null check in handler",
                category=FindingCategory.CORRECTNESS,
                severity=Severity.WARNING,
                confidence=0.8,
                file="handler.py",
                agent="correctness",
            ),
            ReviewFinding(
                title="Null check missing in handler",
                category=FindingCategory.CORRECTNESS,
                severity=Severity.NIT,
                confidence=0.6,
                file="handler.py",
                agent="reliability",
            ),
        ]
        result = dedup_findings(findings)
        assert len(result) == 1

    def test_single_finding_no_dedup(self):
        from app.code_review.dedup import dedup_findings

        findings = [
            ReviewFinding(title="Test", category=FindingCategory.CORRECTNESS, severity=Severity.WARNING),
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

        pr_ctx = PRContext(
            diff_spec="main...f",
            files=[
                ChangedFile(path="auth.py", additions=50, deletions=10, category=FileCategory.BUSINESS_LOGIC),
            ],
            file_count=1,
        )

        findings = [
            ReviewFinding(
                title="Nit",
                category=FindingCategory.STYLE,
                severity=Severity.NIT,
                confidence=0.9,
                file="auth.py",
                start_line=1,
            ),
            ReviewFinding(
                title="Critical bug",
                category=FindingCategory.CORRECTNESS,
                severity=Severity.CRITICAL,
                confidence=0.9,
                file="auth.py",
                start_line=10,
            ),
            ReviewFinding(
                title="Warning",
                category=FindingCategory.SECURITY,
                severity=Severity.WARNING,
                confidence=0.8,
                file="auth.py",
                start_line=5,
            ),
        ]
        ranked = score_and_rank(findings, pr_ctx)
        assert ranked[0].severity == Severity.CRITICAL
        assert ranked[-1].severity == Severity.NIT

    def test_praise_ranked_last(self):
        from app.code_review.ranking import score_and_rank

        pr_ctx = PRContext(diff_spec="main...f", files=[], file_count=0)

        findings = [
            ReviewFinding(
                title="Good job", category=FindingCategory.CORRECTNESS, severity=Severity.PRAISE, file="x.py"
            ),
            ReviewFinding(
                title="Bug",
                category=FindingCategory.CORRECTNESS,
                severity=Severity.WARNING,
                confidence=0.8,
                file="x.py",
            ),
        ]
        ranked = score_and_rank(findings, pr_ctx)
        assert ranked[-1].severity == Severity.PRAISE

    def test_evidence_quality_boosts_score(self):
        from app.code_review.ranking import _evidence_quality

        # Finding with file, line, evidence, and fix
        f = ReviewFinding(
            title="Bug",
            category=FindingCategory.CORRECTNESS,
            severity=Severity.WARNING,
            file="a.py",
            start_line=10,
            evidence=["e1", "e2"],
            suggested_fix="fix it",
        )
        score = _evidence_quality(f)
        assert score >= 0.9


# =========================================================================
# Agent specs
# =========================================================================


class TestAgentSpecs:
    def test_correctness_runs_on_high_risk(self):
        from app.code_review.agents import AGENT_SPECS

        spec = next(s for s in AGENT_SPECS if s.name == "correctness")
        profile = RiskProfile(correctness=RiskLevel.HIGH)
        assert spec.should_run(profile)

    def test_correctness_skips_on_low_risk(self):
        from app.code_review.agents import AGENT_SPECS

        spec = next(s for s in AGENT_SPECS if s.name == "correctness")
        profile = RiskProfile()  # all low
        assert not spec.should_run(profile)

    def test_test_coverage_always_runs(self):
        from app.code_review.agents import AGENT_SPECS

        spec = next(s for s in AGENT_SPECS if s.name == "test_coverage")
        profile = RiskProfile()  # all low
        assert spec.should_run(profile, always_run=True)

    def test_agent_query_includes_diff_spec(self):
        from app.code_review.agents import AGENT_SPECS, _build_agent_query

        spec = AGENT_SPECS[0]
        ctx = PRContext(
            diff_spec="main...feature",
            file_count=3,
            total_changed_lines=100,
            files=[ChangedFile(path="a.py", additions=50, deletions=10, category=FileCategory.BUSINESS_LOGIC)],
        )
        profile = RiskProfile()
        query = _build_agent_query(spec, ctx, profile)
        assert "main...feature" in query
        assert "a.py" in query

    def test_git_diff_files_not_in_agent_tools(self):
        """git_diff_files should NOT be in any agent tool set (diffs are pre-fetched)."""
        from app.code_review.agents import AGENT_SPECS

        for spec in AGENT_SPECS:
            assert "git_diff_files" not in spec.tools, f"Agent '{spec.name}' still has git_diff_files in tool set"

    def test_agent_query_includes_prefetched_diffs(self):
        """When file_diffs are provided, the agent query includes them inline."""
        from app.code_review.agents import AGENT_SPECS, _build_agent_query

        spec = AGENT_SPECS[0]  # correctness
        ctx = PRContext(
            diff_spec="main...feature",
            file_count=1,
            total_changed_lines=20,
            files=[ChangedFile(path="app/service.py", additions=15, deletions=5, category=FileCategory.BUSINESS_LOGIC)],
        )
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
        ctx = PRContext(
            diff_spec="main...feature",
            file_count=1,
            total_changed_lines=20,
            files=[ChangedFile(path="app/service.py", additions=15, deletions=5, category=FileCategory.BUSINESS_LOGIC)],
        )
        profile = RiskProfile()
        query = _build_agent_query(spec, ctx, profile, file_diffs={})
        assert "diffs not available" in query

    def test_agent_query_truncates_large_diffs(self):
        """Individual file diffs exceeding 8KB are truncated."""
        from app.code_review.agents import AGENT_SPECS, _build_agent_query

        spec = AGENT_SPECS[0]
        ctx = PRContext(
            diff_spec="main...feature",
            file_count=1,
            total_changed_lines=500,
            files=[ChangedFile(path="big.py", additions=400, deletions=100, category=FileCategory.BUSINESS_LOGIC)],
        )
        profile = RiskProfile()
        large_diff = "diff --git a/big.py b/big.py\n" + "+x\n" * 5000  # >8KB
        diffs = {"big.py": large_diff}
        query = _build_agent_query(spec, ctx, profile, file_diffs=diffs)
        assert "truncated" in query
        # Should still contain the beginning of the diff
        assert "diff --git a/big.py b/big.py" in query


class TestFindingsParsing:
    def test_parse_json_findings(self):
        from app.code_review.agents import AGENT_SPECS, _parse_findings

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
        from app.code_review.agents import AGENT_SPECS, _parse_findings

        spec = AGENT_SPECS[0]
        findings = _parse_findings("No issues found.\n[]", spec)
        assert findings == []

    def test_parse_no_json(self):
        from app.code_review.agents import AGENT_SPECS, _parse_findings

        spec = AGENT_SPECS[0]
        findings = _parse_findings("The code looks good, no issues.", spec)
        assert findings == []

    def test_parse_malformed_json(self):
        from app.code_review.agents import AGENT_SPECS, _parse_findings

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

        ctx = PRContext(
            diff_spec="x",
            total_changed_lines=9000,
            file_count=50,
            files=[ChangedFile(path=f"file_{i}.py", additions=100, deletions=80) for i in range(50)],
        )
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

        findings = [ReviewFinding(title="x", category=FindingCategory.CORRECTNESS, severity=Severity.CRITICAL)]
        assert _merge_recommendation(findings) == "request_changes"

    def test_approve_with_followups(self):
        from app.code_review.service import _merge_recommendation

        findings = [ReviewFinding(title="x", category=FindingCategory.CORRECTNESS, severity=Severity.WARNING)]
        assert _merge_recommendation(findings) == "approve_with_followups"

    def test_request_changes_many_warnings(self):
        from app.code_review.service import _merge_recommendation

        findings = [
            ReviewFinding(title=f"w{i}", category=FindingCategory.CORRECTNESS, severity=Severity.WARNING)
            for i in range(4)
        ]
        assert _merge_recommendation(findings) == "request_changes"


# =========================================================================
# PRContext helpers
# =========================================================================


class TestPRContext:
    def test_business_logic_files(self):
        ctx = PRContext(
            diff_spec="x",
            files=[
                ChangedFile(path="app/service.py", category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="tests/test.py", category=FileCategory.TEST),
                ChangedFile(path="config/app.yml", category=FileCategory.CONFIG),
            ],
        )
        assert len(ctx.business_logic_files()) == 1
        assert len(ctx.test_files()) == 1
        assert len(ctx.config_files()) == 1

    def test_security_sensitive_files_matches_path_patterns(self):
        """Security scoping is category-agnostic — matches auth/crypto/session
        paths even when diff_parser classified them as INFRA or SCHEMA."""
        ctx = PRContext(
            diff_spec="x",
            files=[
                # Category-agnostic matches — pattern-matched regardless of classification
                ChangedFile(path="src/auth/middleware.py", category=FileCategory.INFRA),
                ChangedFile(path="lib/crypto/rsa.py", category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="app/session_store.py", category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="migrations/0042_add_permissions_table.sql", category=FileCategory.SCHEMA),
                ChangedFile(path="src/oauth2/callback.py", category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="src/auth_service/token_refresh.py", category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="config/secrets.yaml", category=FileCategory.CONFIG),
                # Non-matches — should NOT be included
                ChangedFile(path="src/payment/service.py", category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="tests/test_auth.py", category=FileCategory.TEST),  # test file matches on 'auth'
                ChangedFile(path="README.md", category=FileCategory.INFRA),
            ],
        )
        sensitive = ctx.security_sensitive_files()
        paths = {f.path for f in sensitive}
        assert "src/auth/middleware.py" in paths
        assert "lib/crypto/rsa.py" in paths
        assert "app/session_store.py" in paths
        assert "migrations/0042_add_permissions_table.sql" in paths
        assert "src/oauth2/callback.py" in paths
        assert "src/auth_service/token_refresh.py" in paths
        assert "config/secrets.yaml" in paths
        # Matched on 'auth' in the filename — acceptable false positive
        # (security scoping is intentionally broad to avoid false negatives)
        assert "tests/test_auth.py" in paths
        # Non-matches
        assert "src/payment/service.py" not in paths
        assert "README.md" not in paths

    def test_security_sensitive_files_empty(self):
        """Returns empty list when no path matches."""
        ctx = PRContext(
            diff_spec="x",
            files=[
                ChangedFile(path="src/payment/service.py", category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="README.md", category=FileCategory.INFRA),
            ],
        )
        assert ctx.security_sensitive_files() == []

    def test_finding_score(self):
        f = ReviewFinding(title="x", category=FindingCategory.CORRECTNESS, severity=Severity.CRITICAL, confidence=0.9)
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
        response = api_client.post(
            "/api/code-review/review",
            json={
                "room_id": "test-room",
                "diff_spec": "main...feature",
            },
        )
        assert response.status_code == 503


# =========================================================================
# Query classifier — diff_spec extraction
# =========================================================================


# =========================================================================
# Multi-agent review delegation
# =========================================================================


# TestMultiAgentDelegation and TestFormatReviewResult removed —
# multi-agent delegation and format_review_result moved to Brain orchestrator.
