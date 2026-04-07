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
