"""Tests for PRBrainOrchestrator and related components in app.agent_loop.pr_brain."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from app.agent_loop.pr_brain import (
    ArbitrationVerdict,
    PRBrainOrchestrator,
)
from app.workflow.models import PRBrainConfig

# Read max_findings_per_agent from default config (same as config/brains/pr_review.yaml)
_MAX_FINDINGS_PER_AGENT = PRBrainConfig().post_processing.max_findings_per_agent
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
from app.code_tools.schemas import ToolResult
from app.workflow.models import PRBrainConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_scratchpad(monkeypatch):
    """Skip Fact Vault creation in PRBrainOrchestrator unit tests.

    These tests don't exercise `run_stream` end-to-end; they unit-test
    helper methods with mocked tool_executors. Creating a real SQLite
    file per test leaks ~40KB each into ~/.conductor/scratchpad/. Set
    the env var to 0 for the duration of this test module.
    """
    monkeypatch.setenv("CONDUCTOR_SCRATCHPAD_ENABLED", "0")


def _make_pr_brain(
    review_agents=None,
    max_findings=10,
) -> PRBrainOrchestrator:
    """Create a PRBrainOrchestrator with minimal mock dependencies."""
    config = PRBrainConfig(
        review_agents=review_agents or ["correctness", "concurrency", "security", "reliability", "test_coverage"],
        post_processing={"max_findings": max_findings},
    )
    return PRBrainOrchestrator(
        provider=MagicMock(),
        explorer_provider=MagicMock(),
        workspace_path="/tmp/repo",
        diff_spec="HEAD~1..HEAD",
        pr_brain_config=config,
        agent_registry={},
        tool_executor=MagicMock(),
    )


def _make_pr_context(
    total_changed_lines=300,
    file_count=3,
    diff_spec="HEAD~1..HEAD",
) -> PRContext:
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
        diff_spec=diff_spec,
        files=files,
        total_additions=total_changed_lines,
        total_deletions=0,
        total_changed_lines=total_changed_lines,
        file_count=file_count,
    )


def _make_finding(
    title="Test finding",
    severity=Severity.WARNING,
    confidence=0.85,
    file="app/service.py",
    start_line=10,
    category=FindingCategory.CORRECTNESS,
    agent="correctness",
    evidence=None,
) -> ReviewFinding:
    return ReviewFinding(
        title=title,
        category=category,
        severity=severity,
        confidence=confidence,
        file=file,
        start_line=start_line,
        end_line=start_line,
        evidence=evidence if evidence is not None else ["evidence item 1", "evidence item 2"],
        risk="some risk",
        suggested_fix="some fix",
        agent=agent,
    )


def _make_tool_result(agent_name: str, answer: str, tool_calls_made: int = 5) -> ToolResult:
    return ToolResult(
        tool_name="dispatch_agent",
        success=True,
        data={
            "agent_name": agent_name,
            "answer": answer,
            "tool_calls_made": tool_calls_made,
            "iterations": 3,
        },
    )


def _low_risk_profile() -> RiskProfile:
    return RiskProfile(
        correctness=RiskLevel.LOW,
        concurrency=RiskLevel.LOW,
        security=RiskLevel.LOW,
        reliability=RiskLevel.LOW,
        operational=RiskLevel.LOW,
    )


def _high_security_profile() -> RiskProfile:
    return RiskProfile(
        correctness=RiskLevel.LOW,
        concurrency=RiskLevel.LOW,
        security=RiskLevel.HIGH,
        reliability=RiskLevel.LOW,
        operational=RiskLevel.LOW,
    )


# ---------------------------------------------------------------------------
# ArbitrationVerdict
# ---------------------------------------------------------------------------


class TestArbitrationVerdict:
    def test_verdict_defaults(self):
        v = ArbitrationVerdict(index=0)
        assert v.index == 0
        assert v.counter_evidence == []
        assert v.rebuttal_confidence == pytest.approx(0.0)
        assert v.suggested_severity == ""
        assert v.reason == ""

    def test_verdict_with_data(self):
        v = ArbitrationVerdict(
            index=2,
            counter_evidence=["code is guarded", "lock prevents race"],
            rebuttal_confidence=0.85,
            suggested_severity="warning",
            reason="found guard at line 55",
        )
        assert v.index == 2
        assert len(v.counter_evidence) == 2
        assert v.rebuttal_confidence == pytest.approx(0.85)
        assert v.suggested_severity == "warning"
        assert v.reason == "found guard at line 55"

    def test_verdict_counter_evidence_is_mutable(self):
        v = ArbitrationVerdict(index=0)
        v.counter_evidence.append("new evidence")
        assert len(v.counter_evidence) == 1


# ---------------------------------------------------------------------------
# _select_agents
# ---------------------------------------------------------------------------


class TestSelectAgents:
    def test_select_agents_all_low_risk(self):
        brain = _make_pr_brain()
        risk = _low_risk_profile()
        ctx = _make_pr_context(total_changed_lines=500)  # not small PR
        agents = brain._select_agents(risk, ctx)
        # correctness and test_coverage always run; others need medium+ risk
        assert "correctness" in agents
        assert "test_coverage" in agents
        # Security/concurrency/reliability should NOT run for low risk
        assert "security" not in agents
        assert "concurrency" not in agents
        assert "reliability" not in agents

    def test_select_agents_security_high(self):
        brain = _make_pr_brain()
        risk = _high_security_profile()
        ctx = _make_pr_context(total_changed_lines=500)
        agents = brain._select_agents(risk, ctx)
        assert "security" in agents
        assert "correctness" in agents
        assert "test_coverage" in agents

    def test_select_agents_small_pr_skips_concurrency(self):
        brain = _make_pr_brain()
        # Small PR with medium concurrency risk — should still skip
        risk = RiskProfile(
            correctness=RiskLevel.LOW,
            concurrency=RiskLevel.MEDIUM,
            security=RiskLevel.LOW,
            reliability=RiskLevel.LOW,
            operational=RiskLevel.LOW,
        )
        # Use a value strictly below the threshold (100) to trigger small-PR path
        ctx = _make_pr_context(total_changed_lines=50)  # below 100 threshold → small
        agents = brain._select_agents(risk, ctx)
        # Concurrency should be SKIPPED for small PR even at medium risk
        assert "concurrency" not in agents

    def test_select_agents_small_pr_includes_high_concurrency(self):
        brain = _make_pr_brain()
        risk = RiskProfile(
            correctness=RiskLevel.LOW,
            concurrency=RiskLevel.HIGH,
            security=RiskLevel.LOW,
            reliability=RiskLevel.LOW,
            operational=RiskLevel.LOW,
        )
        ctx = _make_pr_context(total_changed_lines=50)  # below 100 threshold → small
        agents = brain._select_agents(risk, ctx)
        # HIGH concurrency risk overrides the small-PR skip
        assert "concurrency" in agents

    def test_select_agents_small_pr_skips_reliability_low_risk(self):
        brain = _make_pr_brain()
        risk = RiskProfile(
            correctness=RiskLevel.LOW,
            concurrency=RiskLevel.LOW,
            security=RiskLevel.LOW,
            reliability=RiskLevel.MEDIUM,  # medium won't override small-PR skip
            operational=RiskLevel.LOW,
        )
        ctx = _make_pr_context(total_changed_lines=50)  # very small
        agents = brain._select_agents(risk, ctx)
        assert "reliability" not in agents

    def test_select_agents_medium_risk_adds_security(self):
        brain = _make_pr_brain()
        risk = RiskProfile(security=RiskLevel.MEDIUM)
        ctx = _make_pr_context(total_changed_lines=500)
        agents = brain._select_agents(risk, ctx)
        assert "security" in agents


# ---------------------------------------------------------------------------
# _build_agent_query
# ---------------------------------------------------------------------------


class TestBuildAgentQuery:
    def _make_brain_and_ctx(self):
        brain = _make_pr_brain()
        ctx = _make_pr_context()
        risk = _low_risk_profile()
        return brain, ctx, risk

    def test_build_query_contains_focus(self):
        brain, ctx, risk = self._make_brain_and_ctx()
        query = brain._build_agent_query("correctness", ctx, risk, {}, "")
        assert "Logic errors" in query or "correctness" in query.lower()

    def test_build_query_contains_strategy(self):
        brain, ctx, risk = self._make_brain_and_ctx()
        query = brain._build_agent_query("security", ctx, risk, {}, "")
        # Strategy hint for security mentions trace_variable or taint
        assert "taint" in query.lower() or "trace" in query.lower() or "depth-first" in query.lower()

    def test_build_query_contains_diffs(self):
        brain, ctx, risk = self._make_brain_and_ctx()
        file_diffs = {"app/file0.py": "diff --git a/app/file0.py\n+changed line"}
        query = brain._build_agent_query("correctness", ctx, risk, file_diffs, "")
        assert "diffs" in query.lower() or "diff" in query

    def test_build_query_scopes_test_coverage(self):
        brain = _make_pr_brain()
        # test_coverage agent should see ALL files, including test files
        ctx = PRContext(
            diff_spec="HEAD~1..HEAD",
            files=[
                ChangedFile(path="app/service.py", additions=10, deletions=0, category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="tests/test_service.py", additions=5, deletions=0, category=FileCategory.TEST),
            ],
            total_additions=15,
            total_deletions=0,
            total_changed_lines=15,
            file_count=2,
        )
        risk = _low_risk_profile()
        query = brain._build_agent_query("test_coverage", ctx, risk, {}, "")
        # test_coverage sees all files; test file should appear in the query
        assert "tests/test_service.py" in query

    def test_build_query_security_includes_config_files(self):
        brain = _make_pr_brain()
        ctx = PRContext(
            diff_spec="HEAD~1..HEAD",
            files=[
                ChangedFile(path="app/auth.py", additions=10, deletions=0, category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="config/settings.yaml", additions=2, deletions=0, category=FileCategory.CONFIG),
            ],
            total_additions=12,
            total_deletions=0,
            total_changed_lines=12,
            file_count=2,
        )
        risk = _low_risk_profile()
        query = brain._build_agent_query("security", ctx, risk, {}, "")
        # Security agent scopes to business_logic + config files
        assert "config/settings.yaml" in query

    def test_build_query_security_widens_to_security_sensitive_paths(self):
        """Security agent picks up auth/crypto/session files even when
        classified outside business_logic (e.g. INFRA, SCHEMA). Also
        deduplicates when a file matches multiple scoping rules."""
        brain = _make_pr_brain()
        ctx = PRContext(
            diff_spec="HEAD~1..HEAD",
            files=[
                # Classified as INFRA but matches auth path — should appear
                ChangedFile(path="deploy/auth_middleware.py", additions=5, deletions=0, category=FileCategory.INFRA),
                # Classified as SCHEMA but matches permission keyword — should appear
                ChangedFile(path="migrations/0042_add_permissions.sql", additions=3, deletions=0, category=FileCategory.SCHEMA),
                # Regular business logic — always in scope
                ChangedFile(path="app/payment.py", additions=8, deletions=0, category=FileCategory.BUSINESS_LOGIC),
                # Both BUSINESS_LOGIC AND security-sensitive — must dedup
                ChangedFile(path="app/auth_service.py", additions=12, deletions=0, category=FileCategory.BUSINESS_LOGIC),
                # INFRA, no security keyword — should NOT appear
                ChangedFile(path="Dockerfile", additions=2, deletions=0, category=FileCategory.INFRA),
            ],
            total_additions=30,
            total_deletions=0,
            total_changed_lines=30,
            file_count=5,
        )
        risk = _low_risk_profile()
        query = brain._build_agent_query("security", ctx, risk, {}, "")
        # Security-sensitive paths picked up regardless of category
        assert "deploy/auth_middleware.py" in query
        assert "migrations/0042_add_permissions.sql" in query
        # Regular business logic still in scope
        assert "app/payment.py" in query
        # Dedup: auth_service.py appears exactly once, not twice
        assert query.count("app/auth_service.py") == 1
        # Non-matching infra file excluded
        assert "Dockerfile" not in query

    def test_build_query_non_security_unaffected_by_new_scoping(self):
        """correctness/concurrency/reliability/performance still get only
        business_logic files (no widening to security-sensitive paths)."""
        brain = _make_pr_brain()
        ctx = PRContext(
            diff_spec="HEAD~1..HEAD",
            files=[
                ChangedFile(path="app/payment.py", additions=8, deletions=0, category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="deploy/auth_middleware.py", additions=5, deletions=0, category=FileCategory.INFRA),
            ],
            total_additions=13,
            total_deletions=0,
            total_changed_lines=13,
            file_count=2,
        )
        risk = _low_risk_profile()
        query = brain._build_agent_query("correctness", ctx, risk, {}, "")
        assert "app/payment.py" in query
        # correctness stays narrow — auth file classified as INFRA stays out
        assert "deploy/auth_middleware.py" not in query

    def test_build_query_contains_pr_context_section(self):
        brain, ctx, risk = self._make_brain_and_ctx()
        query = brain._build_agent_query("correctness", ctx, risk, {}, "")
        assert "<pr_context>" in query
        assert ctx.diff_spec in query

    def test_build_query_includes_impact_context(self):
        brain, ctx, risk = self._make_brain_and_ctx()
        impact = "## Impact Graph\n`app/file0.py`:\n  ← app/caller.py"
        query = brain._build_agent_query("correctness", ctx, risk, {}, impact)
        assert "<impact_context>" in query
        assert "Impact Graph" in query


# ---------------------------------------------------------------------------
# _post_process
# ---------------------------------------------------------------------------


class TestPostProcess:
    @pytest.mark.asyncio
    async def test_post_process_parses_findings(self):
        brain = _make_pr_brain()
        ctx = _make_pr_context()
        findings_json = json.dumps(
            [
                {
                    "title": "Null dereference",
                    "severity": "warning",
                    "confidence": 0.85,
                    "file": "app/service.py",
                    "start_line": 10,
                    "end_line": 10,
                    "evidence": ["line 10 dereferences optional"],
                    "risk": "NullPointerException",
                    "suggested_fix": "add null check",
                }
            ]
        )
        results = [_make_tool_result("correctness", f"```json\n{findings_json}\n```")]
        output = await brain._post_process(results, ctx)
        assert len(output) >= 1
        assert output[0].title == "Null dereference"

    @pytest.mark.asyncio
    async def test_post_process_caps_per_agent(self):
        brain = _make_pr_brain()
        ctx = _make_pr_context()
        # Create more than _MAX_FINDINGS_PER_AGENT findings
        findings_list = [
            {
                "title": f"Finding {i}",
                "severity": "warning",
                "confidence": 0.85 - i * 0.01,  # descending confidence
                "file": "app/service.py",
                "start_line": i + 1,
                "end_line": i + 1,
                "evidence": [f"evidence {i}"],
                "risk": f"risk {i}",
                "suggested_fix": f"fix {i}",
            }
            for i in range(_MAX_FINDINGS_PER_AGENT + 3)
        ]
        answer = json.dumps(findings_list)
        results = [_make_tool_result("correctness", answer, tool_calls_made=5)]
        output = await brain._post_process(results, ctx)
        # Should be capped at _MAX_FINDINGS_PER_AGENT per agent
        assert len(output) <= _MAX_FINDINGS_PER_AGENT

    @pytest.mark.asyncio
    async def test_post_process_filters_test_files(self):
        brain = _make_pr_brain()
        # Create context with both source and test findings
        ctx = PRContext(
            diff_spec="HEAD~1..HEAD",
            files=[
                ChangedFile(path="app/service.py", additions=10, deletions=0, category=FileCategory.BUSINESS_LOGIC),
                ChangedFile(path="tests/test_service.py", additions=5, deletions=0, category=FileCategory.TEST),
            ],
            total_additions=15,
            total_deletions=0,
            total_changed_lines=15,
            file_count=2,
        )
        # Mix of source and test-file-only test_coverage findings
        source_finding = {
            "title": "Missing coverage for auth flow",
            "severity": "warning",
            "confidence": 0.85,
            "file": "app/service.py",
            "start_line": 10,
            "end_line": 10,
            "evidence": ["no test covers auth path"],
            "risk": "untested code",
            "suggested_fix": "add test",
        }
        test_file_finding = {
            "title": "Test assertion too weak",
            "severity": "warning",
            "confidence": 0.82,
            "file": "tests/test_service.py",
            "start_line": 20,
            "end_line": 20,
            "evidence": ["only checks status code"],
            "risk": "false confidence",
            "suggested_fix": "assert response body",
        }
        answer = json.dumps([source_finding, test_file_finding])
        results = [_make_tool_result("test_coverage", answer, tool_calls_made=5)]
        output = await brain._post_process(results, ctx)
        # The test-file-only finding should be dropped when source findings exist
        test_files = [f for f in output if "tests/" in f.file]
        source_files = [f for f in output if f.file == "app/service.py"]
        assert len(source_files) >= 1
        # test_service.py finding (test_coverage category) should be dropped
        assert len(test_files) == 0

    @pytest.mark.asyncio
    async def test_post_process_handles_failed_agent(self):
        brain = _make_pr_brain()
        ctx = _make_pr_context()
        failed_result = ToolResult(
            tool_name="dispatch_agent",
            success=False,
            error="Agent timed out",
        )
        # Should not raise — failed results are silently skipped
        output = await brain._post_process([failed_result], ctx)
        assert isinstance(output, list)

    @pytest.mark.asyncio
    async def test_post_process_empty_results(self):
        brain = _make_pr_brain()
        ctx = _make_pr_context()
        output = await brain._post_process([], ctx)
        assert output == []

    @pytest.mark.asyncio
    async def test_post_process_repairs_truncated_output(self):
        """When parse_findings fails on a substantive answer (>100 chars),
        _post_process should call repair_output via the explorer provider
        to recover findings. This catches the FORCE_CONCLUDE truncation
        case where the agent ran out of budget mid-investigation but still
        had evidence in its accumulated text."""
        brain = _make_pr_brain()
        ctx = _make_pr_context()

        # Truncated agent output: prose with file refs but no JSON.
        # 4 review agents had outputs like this in PR 13858 trace.
        truncated = (
            "I investigated the auth flow and found that the rate limiter "
            "in app/service.py at line 42 has a fail-open Redis catch that "
            "bypasses the throttle on errors. This means an attacker could "
            "trigger Redis errors to bypass the throttle entirely. Severity: warning."
        )
        assert len(truncated) > 100  # sanity: triggers repair branch

        # Mock the explorer provider's call_model (the repair LLM call)
        # to return a properly-formatted JSON array.
        repaired_json = json.dumps(
            [
                {
                    "title": "Fail-open rate limiter bypass",
                    "severity": "warning",
                    "confidence": 0.85,
                    "file": "app/service.py",
                    "start_line": 42,
                    "end_line": 42,
                    "evidence": ["fail-open Redis catch at line 42"],
                    "risk": "throttle bypass",
                    "suggested_fix": "fail closed on Redis errors",
                }
            ]
        )
        brain._explorer_provider.call_model = MagicMock(return_value=repaired_json)

        results = [_make_tool_result("correctness", truncated)]
        output = await brain._post_process(results, ctx)

        # Repair should have been called with the truncated answer
        brain._explorer_provider.call_model.assert_called_once()
        # And recovered the finding
        assert len(output) == 1
        assert output[0].title == "Fail-open rate limiter bypass"
        assert output[0].file == "app/service.py"

    @pytest.mark.asyncio
    async def test_post_process_skips_repair_for_short_answer(self):
        """If the answer is too short (<=100 chars) the repair LLM call
        is skipped — there's nothing to recover."""
        brain = _make_pr_brain()
        ctx = _make_pr_context()
        brain._explorer_provider.call_model = MagicMock(return_value="[]")

        results = [_make_tool_result("correctness", "no findings")]  # 11 chars
        output = await brain._post_process(results, ctx)

        # Repair must NOT be called for short answers
        brain._explorer_provider.call_model.assert_not_called()
        assert output == []


# ---------------------------------------------------------------------------
# _parse_verdicts
# ---------------------------------------------------------------------------


class TestParseVerdicts:
    def test_parse_verdicts_valid(self):
        brain = _make_pr_brain()
        findings = [_make_finding(), _make_finding(title="Second finding")]
        verdicts_json = json.dumps(
            [
                {
                    "index": 0,
                    "counter_evidence": ["no lock needed here"],
                    "rebuttal_confidence": 0.7,
                    "suggested_severity": "warning",
                    "reason": "already guarded",
                },
                {
                    "index": 1,
                    "counter_evidence": [],
                    "rebuttal_confidence": 0.1,
                    "suggested_severity": "critical",
                    "reason": "confirmed issue",
                },
            ]
        )
        answer = f"<result>\n{verdicts_json}\n</result>"
        verdicts = brain._parse_verdicts(findings, answer)
        assert len(verdicts) == 2
        assert verdicts[0].rebuttal_confidence == pytest.approx(0.7)
        assert verdicts[1].rebuttal_confidence == pytest.approx(0.1)

    def test_parse_verdicts_missing_tags(self):
        brain = _make_pr_brain()
        findings = [_make_finding()]
        # No <result> tags → should return defaults
        verdicts = brain._parse_verdicts(findings, "No structured output here")
        assert len(verdicts) == 1
        assert verdicts[0].rebuttal_confidence == pytest.approx(0.0)
        assert verdicts[0].reason == "arbitration unavailable"

    def test_parse_verdicts_fills_gaps(self):
        brain = _make_pr_brain()
        findings = [_make_finding(), _make_finding(title="F2"), _make_finding(title="F3")]
        # Only verdict for index 1 — indices 0 and 2 should get defaults
        verdicts_json = json.dumps(
            [
                {
                    "index": 1,
                    "counter_evidence": ["found counter"],
                    "rebuttal_confidence": 0.6,
                    "suggested_severity": "nit",
                    "reason": "minor",
                },
            ]
        )
        answer = f"<result>{verdicts_json}</result>"
        verdicts = brain._parse_verdicts(findings, answer)
        assert len(verdicts) == 3
        # Sort by index to check
        sorted_v = sorted(verdicts, key=lambda v: v.index)
        assert sorted_v[0].reason == "not challenged"
        assert sorted_v[1].rebuttal_confidence == pytest.approx(0.6)
        assert sorted_v[2].reason == "not challenged"

    def test_parse_verdicts_invalid_json_in_tags(self):
        brain = _make_pr_brain()
        findings = [_make_finding()]
        answer = "<result>not valid json</result>"
        verdicts = brain._parse_verdicts(findings, answer)
        assert len(verdicts) == 1
        assert verdicts[0].rebuttal_confidence == pytest.approx(0.0)

    def test_parse_verdicts_sorted_by_index(self):
        brain = _make_pr_brain()
        findings = [_make_finding(), _make_finding(title="F2"), _make_finding(title="F3")]
        # Return verdicts out of order
        verdicts_json = json.dumps(
            [
                {
                    "index": 2,
                    "counter_evidence": [],
                    "rebuttal_confidence": 0.3,
                    "suggested_severity": "warning",
                    "reason": "ok",
                },
                {
                    "index": 0,
                    "counter_evidence": [],
                    "rebuttal_confidence": 0.9,
                    "suggested_severity": "nit",
                    "reason": "wrong",
                },
            ]
        )
        answer = f"<result>{verdicts_json}</result>"
        verdicts = brain._parse_verdicts(findings, answer)
        for i, v in enumerate(verdicts):
            assert v.index == i


# ---------------------------------------------------------------------------
# _default_verdicts
# ---------------------------------------------------------------------------


class TestDefaultVerdicts:
    def test_default_verdicts_length(self):
        brain = _make_pr_brain()
        findings = [_make_finding() for _ in range(5)]
        verdicts = brain._default_verdicts(findings)
        assert len(verdicts) == 5

    def test_default_verdicts_indices(self):
        brain = _make_pr_brain()
        findings = [_make_finding() for _ in range(3)]
        verdicts = brain._default_verdicts(findings)
        for i, v in enumerate(verdicts):
            assert v.index == i

    def test_default_verdicts_values(self):
        brain = _make_pr_brain()
        findings = [_make_finding(severity=Severity.CRITICAL)]
        verdicts = brain._default_verdicts(findings)
        v = verdicts[0]
        assert v.counter_evidence == []
        assert v.rebuttal_confidence == pytest.approx(0.0)
        assert v.suggested_severity == Severity.CRITICAL.value
        assert v.reason == "arbitration unavailable"

    def test_default_verdicts_empty_findings(self):
        brain = _make_pr_brain()
        verdicts = brain._default_verdicts([])
        assert verdicts == []


# ---------------------------------------------------------------------------
# _MAX_FINDINGS_PER_AGENT constant
# ---------------------------------------------------------------------------


class TestConstants:
    def test_max_findings_per_agent_value(self):
        assert _MAX_FINDINGS_PER_AGENT == 3

    def test_max_findings_per_agent_is_int(self):
        assert isinstance(_MAX_FINDINGS_PER_AGENT, int)


# ---------------------------------------------------------------------------
# _inject_missing_symbol_findings (Phase 2 post-pass)
# ---------------------------------------------------------------------------


class TestInjectMissingSymbolFindings:
    """The post-pass must guarantee one finding per missing symbol even
    when the coordinator LLM drops or merges them. See
    SENTRY_EVAL_SUMMARY_2026-04-20.md Priority 1."""

    def _fake_store(self, missing_symbols, sig_mismatches=None):
        """Build a stub FactStore yielding given missing symbols and/or
        signature-mismatch facts. ``sig_mismatches`` is a list of dicts
        with keys: name, referenced_at, actual_params, missing_params."""
        import time

        from app.scratchpad.store import ExistenceFact

        missing_facts = [
            ExistenceFact(
                symbol_name=sym["name"],
                symbol_kind=sym.get("kind", "class"),
                referenced_at=sym["referenced_at"],
                exists_flag=False,
                evidence=sym.get("evidence", "grep → 0 matches"),
                signature_info=None,
                ts_written=int(time.time()),
            )
            for sym in missing_symbols
        ]
        present_facts = [
            ExistenceFact(
                symbol_name=s["name"],
                symbol_kind=s.get("kind", "method"),
                referenced_at=s["referenced_at"],
                exists_flag=True,
                evidence=s.get("evidence", "defined"),
                signature_info={
                    "actual_params": s.get("actual_params", []),
                    "missing_params": s.get("missing_params", []),
                },
                ts_written=int(time.time()),
            )
            for s in (sig_mismatches or [])
        ]

        class _Store:
            def iter_existence(self, exists=None):
                if exists is False:
                    yield from missing_facts
                elif exists is True:
                    yield from present_facts
                else:
                    yield from (missing_facts + present_facts)

        return _Store()

    def test_injects_finding_when_symbol_absent(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store([
            {"name": "FooBar", "referenced_at": "src/app.py:42"},
        ])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings, n = mod._inject_missing_symbol_findings([])
        assert n == 1
        assert len(findings) == 1
        synth = findings[0]
        assert synth["severity"] == "critical"
        assert synth["confidence"] == 0.99
        assert "FooBar" in synth["title"]
        assert synth["file"] == "src/app.py"
        assert synth["start_line"] == 42
        assert synth["_injected_from"] == "phase2_existence_missing"

    def test_skips_when_finding_already_mentions_symbol(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store([
            {"name": "FooBar", "referenced_at": "src/app.py:42"},
        ])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        existing = [{
            "title": "FooBar not defined — ImportError",
            "severity": "critical",
            "confidence": 0.95,
            "file": "src/app.py",
            "start_line": 42,
            "end_line": 42,
        }]
        findings, n = mod._inject_missing_symbol_findings(existing)
        assert n == 0
        assert findings == existing

    def test_skips_when_evidence_mentions_symbol(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store([
            {"name": "FooBar", "referenced_at": "src/app.py:42"},
        ])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        existing = [{
            "title": "Broken import at module load",
            "evidence": ["class FooBar not found anywhere"],
            "file": "src/app.py",
            "start_line": 42,
        }]
        _, n = mod._inject_missing_symbol_findings(existing)
        assert n == 0

    def test_injects_multiple_missing_symbols(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store([
            {"name": "FooBar", "referenced_at": "src/a.py:10"},
            {"name": "BazQux", "referenced_at": "src/b.py:20"},
            {"name": "Quux", "referenced_at": "src/c.py:30"},
        ])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings, n = mod._inject_missing_symbol_findings([])
        assert n == 3
        names = sorted(f["title"] for f in findings)
        assert any("FooBar" in t for t in names)
        assert any("BazQux" in t for t in names)
        assert any("Quux" in t for t in names)

    def test_returns_input_when_no_factstore(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: None)
        existing = [{"title": "unrelated"}]
        findings, n = mod._inject_missing_symbol_findings(existing)
        assert n == 0
        assert findings is existing or findings == existing

    def test_parse_reference_location(self):
        from app.agent_loop.pr_brain import _parse_reference_location

        assert _parse_reference_location("src/x.py:42") == ("src/x.py", 42)
        assert _parse_reference_location("src/x.py") == ("src/x.py", 0)
        assert _parse_reference_location("") == ("", 0)
        assert _parse_reference_location("src/x.py:abc") == ("src/x.py:abc", 0)

    def test_finding_covers_symbol_by_title(self):
        from app.agent_loop.pr_brain import _finding_covers_symbol

        assert _finding_covers_symbol(
            {"title": "ImportError: FooBar not defined"},
            "FooBar",
            "src/a.py:10",
        )

    def test_finding_covers_symbol_by_file_and_runtime_marker(self):
        from app.agent_loop.pr_brain import _finding_covers_symbol

        # Title doesn't mention the symbol by name, but same file and
        # title signals a runtime-load error — we accept it as coverage.
        assert _finding_covers_symbol(
            {
                "title": "ImportError at module load time",
                "file": "src/a.py",
            },
            "FooBar",
            "src/a.py:10",
        )

    def test_finding_does_not_cover_unrelated_file(self):
        from app.agent_loop.pr_brain import _finding_covers_symbol

        assert not _finding_covers_symbol(
            {
                "title": "TypeError in handler",
                "file": "src/other.py",
            },
            "FooBar",
            "src/a.py:10",
        )

    def test_injects_signature_mismatch_finding(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store(
            missing_symbols=[],
            sig_mismatches=[{
                "name": "paginate",
                "referenced_at": "src/api/list.py:82",
                "actual_params": ["self", "cursor", "on_results"],
                "missing_params": ["enable_batch_mode"],
            }],
        )
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings, n = mod._inject_missing_symbol_findings([])
        assert n == 1
        synth = findings[0]
        assert "TypeError" in synth["title"]
        assert "enable_batch_mode" in synth["title"]
        assert synth["severity"] == "high"
        assert synth["file"] == "src/api/list.py"
        assert synth["start_line"] == 82
        assert synth["_injected_from"] == "phase2_existence_sigmismatch"

    def test_skips_sig_mismatch_when_kwarg_in_title(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store(
            missing_symbols=[],
            sig_mismatches=[{
                "name": "paginate",
                "referenced_at": "src/api/list.py:82",
                "actual_params": ["self", "cursor"],
                "missing_params": ["enable_batch_mode"],
            }],
        )
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        existing = [{
            "title": "paginate() doesn't accept enable_batch_mode kwarg",
            "file": "src/api/list.py",
            "start_line": 82,
            "severity": "high",
        }]
        _, n = mod._inject_missing_symbol_findings(existing)
        assert n == 0

    def test_injects_both_missing_and_sigmismatch(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._fake_store(
            missing_symbols=[
                {"name": "FooBar", "referenced_at": "src/app.py:11"},
            ],
            sig_mismatches=[{
                "name": "paginate",
                "referenced_at": "src/api/list.py:82",
                "actual_params": ["self", "cursor"],
                "missing_params": ["enable_batch_mode"],
            }],
        )
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings, n = mod._inject_missing_symbol_findings([])
        assert n == 2
        kinds = {f["_injected_from"] for f in findings}
        assert "phase2_existence_missing" in kinds
        assert "phase2_existence_sigmismatch" in kinds


# ---------------------------------------------------------------------------
# _reflect_against_phase2_facts (P8 — external-signal reflection)
# ---------------------------------------------------------------------------


class TestReflectAgainstPhase2Facts:
    """P8: drop findings whose premise ('X is missing') contradicts a
    Phase 2 exists=True fact. Deliberately narrow — requires both the
    symbol mention AND existence-negation phrasing."""

    def _store_with_present(self, present):
        import time

        from app.scratchpad.store import ExistenceFact

        facts = [
            ExistenceFact(
                symbol_name=p["name"],
                symbol_kind=p.get("kind", "class"),
                referenced_at=p.get("referenced_at", ""),
                exists_flag=True,
                evidence=p.get("evidence", "defined"),
                signature_info=None,
                ts_written=int(time.time()),
            )
            for p in present
        ]

        class _Store:
            def iter_existence(self, exists=None):
                if exists is True:
                    yield from facts
                elif exists is False:
                    return
                    yield  # pragma: no cover
                else:
                    yield from facts

        return _Store()

    def test_drops_finding_claiming_present_symbol_is_missing(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._store_with_present([{"name": "RealClass"}])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings = [{
            "title": "ImportError: RealClass not defined in module",
            "severity": "critical",
            "confidence": 0.9,
            "file": "src/app.py",
            "start_line": 10,
        }]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 1
        assert kept == []

    def test_keeps_real_bug_on_existing_symbol(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        # RealClass exists AND a real bug on RealClass.method does not
        # claim RealClass is missing — it should stay.
        store = self._store_with_present([{"name": "RealClass"}])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings = [{
            "title": "RealClass.process mutates input without validation",
            "severity": "warning",
            "confidence": 0.85,
            "file": "src/app.py",
            "start_line": 50,
        }]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 0
        assert kept == findings

    def test_keeps_injected_phase2_findings_always(self, monkeypatch):
        """Injected findings came from the facts themselves — they can
        never contradict the facts, so reflection must never drop them."""
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._store_with_present([{"name": "RealClass"}])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings = [{
            "title": "ImportError at runtime: RealClass not defined",
            "_injected_from": "phase2_existence_missing",
            "confidence": 0.99,
        }]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 0
        assert kept == findings

    def test_returns_unchanged_when_no_factstore(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: None)

        findings = [{"title": "whatever", "file": "x.py"}]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 0
        assert kept == findings

    def test_returns_unchanged_when_no_present_facts(self, monkeypatch):
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._store_with_present([])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings = [{"title": "SomeSymbol not defined"}]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 0
        assert kept == findings

    def test_requires_both_symbol_mention_and_negation_phrase(self, monkeypatch):
        """Finding that mentions the symbol without negation phrasing
        (just a real bug on the method) must be kept."""
        import app.scratchpad as scratch_mod
        from app.agent_loop import pr_brain as mod

        store = self._store_with_present([{"name": "RealClass"}])
        monkeypatch.setattr(scratch_mod, "current_factstore", lambda: store)

        findings = [{
            "title": "RealClass uses stale cache key",
            "risk": "RealClass.compute stores key in mutable dict",
            "file": "src/a.py",
        }]
        kept, dropped = mod._reflect_against_phase2_facts(findings)
        assert dropped == 0
        assert kept == findings


# ---------------------------------------------------------------------------
# _filter_findings_to_diff_scope (P11 cheap — diff-scope verification)
# ---------------------------------------------------------------------------


class TestFilterFindingsToDiffScope:
    """P11 cheap: drop (demote) findings whose file is not touched by
    the PR diff. Inspired by UltraReview's independent-verification
    principle, implemented mechanically (no LLM)."""

    def test_keeps_finding_in_diff(self):
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/app.py": "@@ -1,3 +1,4 @@\n+new line\n"}
        findings = [{"title": "bug", "file": "src/app.py", "start_line": 2}]
        kept, demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 0
        assert kept == findings
        assert demoted == []

    def test_demotes_finding_outside_diff(self):
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/app.py": "diff"}
        findings = [{"title": "phantom", "file": "src/other.py", "start_line": 1}]
        kept, demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 1
        assert kept == []
        assert len(demoted) == 1
        assert demoted[0]["_demoted_reason"] == "file_not_in_diff"

    def test_matches_on_basename_fallback(self):
        """Coordinator sometimes reports just the basename; tolerate it."""
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/deep/path/app.py": "diff"}
        findings = [{"title": "bug", "file": "app.py", "start_line": 1}]
        kept, _demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 0
        assert len(kept) == 1

    def test_keeps_injected_phase2_findings_when_outside_diff(self):
        """Phase 2 may synthesize findings at the reference site of a
        symbol that itself lives in an un-touched file. Never demote."""
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/caller.py": "diff"}
        findings = [{
            "title": "ImportError",
            "file": "src/target.py",
            "_injected_from": "phase2_existence_missing",
        }]
        kept, demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 0
        assert len(kept) == 1
        assert demoted == []

    def test_no_filter_when_no_file_diffs(self):
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        findings = [{"title": "bug", "file": "x.py"}]
        kept, _demoted, n = _filter_findings_to_diff_scope(findings, {})
        assert n == 0
        assert kept == findings

    def test_no_filter_when_finding_has_no_file(self):
        """A finding without a file claim (synthesis-level note) is kept."""
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/app.py": "diff"}
        findings = [{"title": "general observation"}]
        kept, _demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 0
        assert kept == findings

    def test_handles_mixed_kept_and_demoted(self):
        from app.agent_loop.pr_brain import _filter_findings_to_diff_scope

        file_diffs = {"src/real.py": "diff"}
        findings = [
            {"title": "good", "file": "src/real.py"},
            {"title": "bad", "file": "src/ghost.py"},
            {"title": "also good", "file": "src/real.py"},
        ]
        kept, demoted, n = _filter_findings_to_diff_scope(findings, file_diffs)
        assert n == 1
        assert len(kept) == 2
        assert len(demoted) == 1
        assert demoted[0]["file"] == "src/ghost.py"


# ---------------------------------------------------------------------------
# _scan_new_python_imports_for_missing (P13 — deterministic import verifier)
# ---------------------------------------------------------------------------


class TestScanNewPythonImportsForMissing:
    """P13: belt-and-suspenders against LLM Phase 2 flakiness.

    We build a tiny workspace on tmp_path and verify the scan
    mechanically detects missing imports without any LLM dispatch."""

    def _diff(self, path: str, new_lines: list[str]) -> str:
        """Build a minimal unified diff adding the given lines to a file
        starting at line 1."""
        body_plus = "\n".join(f"+{ln}" for ln in new_lines)
        return (
            f"--- a/{path}\n"
            f"+++ b/{path}\n"
            f"@@ -0,0 +1,{len(new_lines)} @@\n"
            f"{body_plus}"
        )

    def test_detects_missing_imported_symbol(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        # workspace has my_mod but NOT FooBar defined
        (tmp_path / "my_mod.py").write_text(
            "class RealClass:\n    pass\n"
        )
        diff = self._diff("entry.py", ["from my_mod import FooBar"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert len(found) == 1
        assert found[0]["name"] == "FooBar"
        assert "my_mod import FooBar" in found[0]["evidence"]

    def test_accepts_existing_symbol(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        (tmp_path / "my_mod.py").write_text(
            "class RealClass:\n    pass\n"
        )
        diff = self._diff("entry.py", ["from my_mod import RealClass"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_multiple_names_in_one_import(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        (tmp_path / "my_mod.py").write_text(
            "class Alpha:\n    pass\n\n"
            "def Beta():\n    return 1\n"
        )
        diff = self._diff(
            "entry.py",
            ["from my_mod import Alpha, Beta, MissingGamma"],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        names = [f["name"] for f in found]
        assert "MissingGamma" in names
        assert "Alpha" not in names
        assert "Beta" not in names

    def test_skips_framework_modules(self, tmp_path):
        """os / sys / typing / django etc. are never our responsibility."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        diff = self._diff(
            "entry.py",
            [
                "from os import path",
                "from typing import Any",
                "from django.db import models",
            ],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_skips_relative_imports(self, tmp_path):
        """Relative imports need file-path resolution; skip for MVP."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        diff = self._diff("entry.py", ["from .foo import NoneHere"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_skips_wildcard(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        diff = self._diff("entry.py", ["from my_mod import *"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_respects_as_alias(self, tmp_path):
        """`from X import Foo as Bar` — verify Foo exists (the imported
        name), not Bar (the local alias)."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        (tmp_path / "my_mod.py").write_text(
            "class RealThing:\n    pass\n"
        )
        diff = self._diff(
            "entry.py", ["from my_mod import RealThing as RT"],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_symbol_defined_as_assignment(self, tmp_path):
        """Module-level assignments count: `CONFIG = ...`."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        (tmp_path / "my_mod.py").write_text("CONFIG = {'a': 1}\n")
        diff = self._diff("entry.py", ["from my_mod import CONFIG"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_empty_workspace_or_diff(self, tmp_path):
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        assert _scan_new_python_imports_for_missing("", {}) == []
        assert _scan_new_python_imports_for_missing(str(tmp_path), {}) == []

    def test_non_python_file_skipped(self, tmp_path):
        """A .ts diff should not trigger Python symbol checks."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        diff = self._diff("entry.ts", ["import { Foo } from './bar';"])
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.ts": diff},
        )
        assert found == []

    def test_caps_symbols_checked(self, tmp_path):
        """Hard cap prevents runtime blow-up on giant diffs."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        # Create my_mod.py so _module_is_first_party passes (else scan
        # skips external modules entirely).
        (tmp_path / "my_mod.py").write_text("# empty module\n")
        many_names = [f"Missing{i}" for i in range(40)]
        diff = self._diff(
            "entry.py", [f"from my_mod import {', '.join(many_names)}"],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff}, max_symbols_checked=5,
        )
        assert len(found) == 5

    def test_skips_non_first_party_modules(self, tmp_path):
        """External pip packages (arroyo, kombu, celery...) must NOT be
        flagged as missing. The workspace doesn't contain their source."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        # No my_mod.py in the workspace — it's an external package.
        # P13 used to false-positive on this (arroyo case on sentry-009).
        diff = self._diff(
            "entry.py",
            ["from arroyo.backends.kafka import KafkaPayload"],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        assert found == []

    def test_first_party_under_src_directory(self, tmp_path):
        """Common repo layout: code under src/<module>/. First-party
        detection must resolve `from my_pkg import X` to `src/my_pkg/...`."""
        from app.agent_loop.pr_brain import (
            _scan_new_python_imports_for_missing,
        )

        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "my_pkg").mkdir()
        (tmp_path / "src" / "my_pkg" / "__init__.py").write_text(
            "class Real:\n    pass\n"
        )
        diff = self._diff(
            "entry.py",
            ["from my_pkg import Real, Missing"],
        )
        found = _scan_new_python_imports_for_missing(
            str(tmp_path), {"entry.py": diff},
        )
        names = [f["name"] for f in found]
        assert "Missing" in names
        assert "Real" not in names


# ---------------------------------------------------------------------------
# _scan_for_stub_call_sites (P14 — mechanical stub detector)
# ---------------------------------------------------------------------------


class TestScanForStubCallSites:
    """P14: detect stub functions added in the diff whose body is a
    literal 'not implemented' error return, and flag call sites that
    also appear in the diff (on + lines or context lines). Pure
    diff-text scan — no workspace read. Aimed at grafana-009 class
    multi-site stub bugs."""

    def test_detects_go_stub_with_diff_caller(self):
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "--- a/pkg/sql/db.go\n"
            "+++ b/pkg/sql/db.go\n"
            "@@ -0,0 +1,6 @@\n"
            "+func (d *DB) RunCommands(cmds []string) (string, error) {\n"
            '+\treturn "", errors.New("not implemented")\n'
            "+}\n"
        )
        caller_diff = (
            "--- a/pkg/sql/parser.go\n"
            "+++ b/pkg/sql/parser.go\n"
            "@@ -10,5 +10,7 @@\n"
            " func Parse() {\n"
            "+\tduckDB := NewInMemoryDB()\n"
            "+\tduckDB.RunCommands([]string{\"SELECT 1\"})\n"
            " }\n"
        )
        file_diffs = {
            "pkg/sql/db.go": stub_diff,
            "pkg/sql/parser.go": caller_diff,
        }
        found = _scan_for_stub_call_sites(file_diffs)
        names = [f["stub_name"] for f in found]
        assert "RunCommands" in names
        caller_files = {f["caller_file"] for f in found}
        assert "pkg/sql/parser.go" in caller_files

    def test_detects_go_stub_on_unchanged_context_line(self):
        """A pre-existing `duckDB.RunCommands(...)` now hits a NEW stub —
        caller is on a context line, not a + line. Must still be flagged."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/pkg/sql/db.go\n"
            "@@ -0,0 +1,3 @@\n"
            "+func (d *DB) RunCommands() (string, error) {\n"
            '+\treturn "", errors.New("not implemented")\n'
            "+}\n"
        )
        caller_diff = (
            "+++ b/pkg/sql/parser.go\n"
            "@@ -20,3 +20,3 @@\n"
            " func Parse() {\n"
            " \tduckDB.RunCommands([]string{\"x\"})\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "pkg/sql/db.go": stub_diff,
            "pkg/sql/parser.go": caller_diff,
        })
        assert any(
            f["caller_file"] == "pkg/sql/parser.go" for f in found
        )

    def test_python_stub_detection(self):
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/svc.py\n"
            "@@ -0,0 +1,3 @@\n"
            "+def fetch_data():\n"
            "+    raise NotImplementedError\n"
        )
        caller_diff = (
            "+++ b/api.py\n"
            "@@ -5,3 +5,4 @@\n"
            " def handler():\n"
            "+    data = fetch_data()\n"
            "     return data\n"
        )
        found = _scan_for_stub_call_sites({
            "svc.py": stub_diff,
            "api.py": caller_diff,
        })
        assert any(f["stub_name"] == "fetch_data" for f in found)

    def test_does_not_flag_stub_without_caller(self):
        """A stub added with no caller in the diff is fine — may be
        a legitimate TODO placeholder."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/todo.py\n"
            "@@ -0,0 +1,2 @@\n"
            "+def later():\n"
            "+    raise NotImplementedError\n"
        )
        found = _scan_for_stub_call_sites({"todo.py": stub_diff})
        assert found == []

    def test_does_not_confuse_func_declaration_with_call(self):
        """`func Foo(` and `def Foo(` are declarations, not calls.
        Must not be flagged as call sites of Foo."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/a.go\n"
            "@@ -0,0 +1,3 @@\n"
            "+func (d *DB) Foo() error {\n"
            '+\treturn errors.New("not implemented")\n'
            "+}\n"
        )
        # Another file defines a same-named function. Not a call.
        other_diff = (
            "+++ b/b.go\n"
            "@@ -0,0 +1,3 @@\n"
            "+func Foo() error {\n"
            "+\treturn nil\n"
            "+}\n"
        )
        found = _scan_for_stub_call_sites({
            "a.go": stub_diff,
            "b.go": other_diff,
        })
        # Neither file should be flagged as a caller — b.go's `func Foo`
        # is a declaration, and a.go's is the stub itself.
        assert found == []

    def test_ignores_non_stub_functions(self):
        """Real function bodies (not just 'not implemented') must not
        trigger stub detection."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        real_diff = (
            "+++ b/a.go\n"
            "@@ -0,0 +1,3 @@\n"
            "+func (d *DB) Real() error {\n"
            "+\treturn d.actualWork()\n"
            "+}\n"
        )
        caller_diff = (
            "+++ b/b.go\n"
            "@@ -5,3 +5,4 @@\n"
            " func X() {\n"
            "+\td.Real()\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "a.go": real_diff,
            "b.go": caller_diff,
        })
        assert found == []

    def test_empty_diff(self):
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        assert _scan_for_stub_call_sites({}) == []

    def test_java_unsupported_operation_stub(self):
        """Canonical Java stub: `throw new UnsupportedOperationException`.
        Validates against the keycloak-003 / -005 pattern."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/impl/Foo.java\n"
            "@@ -0,0 +1,5 @@\n"
            "+    @Override\n"
            "+    public CertificateUtilsProvider getCertificateUtils() {\n"
            '+        throw new UnsupportedOperationException("Not supported yet.");\n'
            "+    }\n"
        )
        caller_diff = (
            "+++ b/api/Service.java\n"
            "@@ -10,3 +10,4 @@\n"
            " public class Service {\n"
            "+    CertificateUtilsProvider p = provider.getCertificateUtils();\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "impl/Foo.java": stub_diff,
            "api/Service.java": caller_diff,
        })
        assert any(f["stub_name"] == "getCertificateUtils" for f in found)
        assert any(
            f["caller_file"] == "api/Service.java" for f in found
        )

    def test_java_notimplementedexception_stub(self):
        """Apache Commons pattern."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/svc.java\n"
            "@@ -0,0 +1,3 @@\n"
            "+    public String fetch() {\n"
            "+        throw new NotImplementedException();\n"
            "+    }\n"
        )
        caller_diff = (
            "+++ b/api.java\n"
            "@@ -5,3 +5,4 @@\n"
            " public class Api {\n"
            "+    String data = svc.fetch();\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "svc.java": stub_diff,
            "api.java": caller_diff,
        })
        assert any(f["stub_name"] == "fetch" for f in found)

    def test_java_runtime_exception_with_not_implemented_message(self):
        """`throw new RuntimeException("not implemented")` only flagged
        when the message mentions not-implemented / not-supported."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/a.java\n"
            "@@ -0,0 +1,3 @@\n"
            "+    public void doThing() {\n"
            '+        throw new RuntimeException("feature not implemented");\n'
            "+    }\n"
        )
        caller_diff = (
            "+++ b/b.java\n"
            "@@ -5,3 +5,4 @@\n"
            " public class B {\n"
            "+    a.doThing();\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "a.java": stub_diff,
            "b.java": caller_diff,
        })
        assert any(f["stub_name"] == "doThing" for f in found)

    def test_java_real_runtime_exception_not_flagged(self):
        """`throw new RuntimeException("db connection lost")` is a
        legitimate error, NOT a stub. Must not be flagged."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/a.java\n"
            "@@ -0,0 +1,3 @@\n"
            "+    public void doThing() {\n"
            '+        throw new RuntimeException("db connection lost");\n'
            "+    }\n"
        )
        caller_diff = (
            "+++ b/b.java\n"
            "@@ -5,3 +5,4 @@\n"
            " public class B {\n"
            "+    a.doThing();\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "a.java": stub_diff,
            "b.java": caller_diff,
        })
        assert found == []

    def test_java_does_not_treat_decl_as_call(self):
        """A same-named method declared in an interface alongside the
        impl stub must not be counted as a caller."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        stub_diff = (
            "+++ b/impl/Foo.java\n"
            "@@ -0,0 +1,3 @@\n"
            "+    public void render() {\n"
            "+        throw new UnsupportedOperationException();\n"
            "+    }\n"
        )
        # Interface also in the diff — declares the method with the same
        # name. This is a DECLARATION, not a call.
        iface_diff = (
            "+++ b/iface/IFoo.java\n"
            "@@ -0,0 +1,2 @@\n"
            "+public interface IFoo {\n"
            "+    public void render();\n"
            "+}\n"
        )
        found = _scan_for_stub_call_sites({
            "impl/Foo.java": stub_diff,
            "iface/IFoo.java": iface_diff,
        })
        # Expect 0 callers: the only mention outside the stub is a decl.
        assert found == []

    def test_java_non_stub_body_not_flagged(self):
        """Real method body must not trigger stub detection."""
        from app.agent_loop.pr_brain import _scan_for_stub_call_sites

        real_diff = (
            "+++ b/a.java\n"
            "@@ -0,0 +1,3 @@\n"
            "+    public int add(int a, int b) {\n"
            "+        return a + b;\n"
            "+    }\n"
        )
        caller_diff = (
            "+++ b/b.java\n"
            "@@ -5,3 +5,4 @@\n"
            " public class B {\n"
            "+    int x = calc.add(1, 2);\n"
            " }\n"
        )
        found = _scan_for_stub_call_sites({
            "a.java": real_diff,
            "b.java": caller_diff,
        })
        assert found == []


# ---------------------------------------------------------------------------
# _inject_stub_caller_findings (P14 — finding injection from stub pairs)
# ---------------------------------------------------------------------------


class TestInjectStubCallerFindings:
    """P14 injection turns stub/caller pairs into synthetic findings.
    Caller sites not already covered by a coordinator finding must be
    flagged; covered sites must be skipped."""

    def _grafana_style_diffs(self):
        return {
            "pkg/sql/db.go": (
                "+++ b/pkg/sql/db.go\n"
                "@@ -0,0 +1,3 @@\n"
                "+func (d *DB) RunCommands() (string, error) {\n"
                '+\treturn "", errors.New("not implemented")\n'
                "+}\n"
            ),
            "pkg/sql/parser.go": (
                "+++ b/pkg/sql/parser.go\n"
                "@@ -20,3 +20,4 @@\n"
                " func Parse() {\n"
                "+\td.RunCommands()\n"
                " }\n"
            ),
        }

    def test_injects_when_coordinator_missed_caller(self):
        from app.agent_loop.pr_brain import _inject_stub_caller_findings

        kept, n = _inject_stub_caller_findings([], self._grafana_style_diffs())
        assert n == 1
        assert kept[0]["_injected_from"] == "p14_stub_caller"
        assert kept[0]["file"] == "pkg/sql/parser.go"
        assert kept[0]["severity"] == "high"

    def test_skips_when_coordinator_already_flagged(self):
        from app.agent_loop.pr_brain import _inject_stub_caller_findings

        existing = [{
            "title": "RunCommands call hits not-implemented stub",
            "file": "pkg/sql/parser.go",
            "start_line": 21,
            "severity": "high",
        }]
        kept, n = _inject_stub_caller_findings(
            existing, self._grafana_style_diffs(),
        )
        assert n == 0
        assert kept == existing

    def test_no_inject_when_no_stubs_in_diff(self):
        from app.agent_loop.pr_brain import _inject_stub_caller_findings

        kept, n = _inject_stub_caller_findings([], {})
        assert n == 0
        assert kept == []


# ---------------------------------------------------------------------------
# P9 — Java-aware Phase 2 hint injection
# ---------------------------------------------------------------------------


class TestPhase2LangHints:
    """The Phase 2 worker query should include a per-language
    `find_symbol`-preference hint for each of the 4 mainstream
    languages (Java, Python, Go, TS/JS) — and only when the diff
    actually touches that language."""

    def _make_ctx(self, paths):
        files = [
            ChangedFile(
                path=p, additions=10, deletions=0,
                category=FileCategory.BUSINESS_LOGIC,
            )
            for p in paths
        ]
        return PRContext(
            diff_spec="HEAD~1..HEAD",
            files=files,
            total_additions=10 * len(files),
            total_deletions=0,
            total_changed_lines=10 * len(files),
            file_count=len(files),
        )

    def _capture_phase2_query(self, brain, ctx, file_diffs):
        """Drive _run_v2_phase2_existence far enough to capture the query
        that gets passed to dispatch_agent, then short-circuit."""
        captured = {}

        class _FakeExecutor:
            async def execute(self, tool_name, params):
                captured["tool"] = tool_name
                captured["params"] = params
                # Return minimal success result so the generator completes.
                return ToolResult(
                    tool_name=tool_name,
                    success=True,
                    data={"answer": '{"symbols":[]}'},
                )

        async def _drive():
            gen = brain._run_v2_phase2_existence(_FakeExecutor(), ctx, file_diffs)
            async for _ in gen:
                pass

        import asyncio as _asyncio
        _asyncio.run(_drive())
        return captured

    def test_java_hint_present_when_java_in_diff(self):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["src/main/java/Foo.java"])
        captured = self._capture_phase2_query(
            brain, ctx, {"src/main/java/Foo.java": "@@ -1 +1 @@\n+class Foo {}\n"},
        )
        assert captured["tool"] == "dispatch_agent"
        query = captured["params"]["query"]
        assert "Language-specific hints" in query
        assert "find_symbol" in query
        assert "Java" in query
        # Java-specific hint should NOT contain hints for other languages
        assert "Python (" not in query
        assert "Go (" not in query

    def test_python_hint_present_when_python_in_diff(self):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["app/service.py", "app/model.py"])
        captured = self._capture_phase2_query(
            brain, ctx, {"app/service.py": "@@ -1 +1 @@\n+def foo(): pass\n"},
        )
        query = captured["params"]["query"]
        assert "Language-specific hints" in query
        assert "Python (`.py`)" in query
        assert "find_symbol" in query
        assert "MRO" in query
        # Python-only should not include Java/Go/TS hints
        assert "Java (`" not in query
        assert "Go (`.go`)" not in query
        assert "TypeScript" not in query

    def test_go_hint_present_when_go_in_diff(self):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["pkg/handler.go"])
        captured = self._capture_phase2_query(
            brain, ctx, {"pkg/handler.go": "@@ -1 +1 @@\n+func Foo() {}\n"},
        )
        query = captured["params"]["query"]
        assert "Go (`.go`)" in query
        assert "receiver" in query

    def test_ts_hint_present_for_tsx_in_diff(self):
        brain = _make_pr_brain()
        ctx = self._make_ctx(["src/components/Button.tsx"])
        captured = self._capture_phase2_query(
            brain, ctx, {"src/components/Button.tsx": "@@ -1 +1 @@\n+export const Button = () => null\n"},
        )
        query = captured["params"]["query"]
        assert "TypeScript" in query
        assert "find_symbol" in query
        assert "overload" in query.lower()

    def test_no_hint_for_non_mainstream_only_diff(self):
        """Rust/C/C++ files parse via tree-sitter but don't get a tailored
        AST-prefer hint (lower signal:noise — those agents rarely run)."""
        brain = _make_pr_brain()
        ctx = self._make_ctx(["src/lib.rs"])
        captured = self._capture_phase2_query(
            brain, ctx, {"src/lib.rs": "@@ -1 +1 @@\n+fn foo() {}\n"},
        )
        query = captured["params"]["query"]
        assert "Language-specific hints" not in query

    def test_multiple_hints_for_mixed_diff(self):
        brain = _make_pr_brain()
        ctx = self._make_ctx([
            "app/service.py",
            "src/main/java/Bar.java",
            "pkg/handler.go",
            "web/app.tsx",
        ])
        captured = self._capture_phase2_query(
            brain, ctx, {
                "app/service.py": "@@ -1 +1 @@\n+pass\n",
                "src/main/java/Bar.java": "@@ -1 +1 @@\n+class Bar {}\n",
                "pkg/handler.go": "@@ -1 +1 @@\n+func H() {}\n",
                "web/app.tsx": "@@ -1 +1 @@\n+export const A = 1\n",
            },
        )
        query = captured["params"]["query"]
        assert "Language-specific hints" in query
        assert "Java (`.java`)" in query
        assert "Python (`.py`)" in query
        assert "Go (`.go`)" in query
        assert "TypeScript" in query
