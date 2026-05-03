"""Unit tests for PR Brain v2's P12b dimension-worker primitive.

Covers:
- Pydantic schema validation (dimension vocab, budget floor/ceiling)
- Trigger detector (_detect_dimension_triggers) behaviour on simulated
  dependency data
- Cap function (_dimension_dispatch_cap) thresholds
- Coordinator query renderer injects the trigger hint block
- AgentToolExecutor dispatch wiring + severity=null + dispatch tag

Does NOT run a real sub-agent (that needs Bedrock); uses a fake
_dispatch_explore returning canned JSON.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from app.agent_loop.brain import AgentToolExecutor
from app.agent_loop.pr_brain import (
    _detect_dimension_triggers,
    _dimension_dispatch_cap,
)
from app.code_tools.schemas import DispatchSweepParams, ToolResult

# ---------------------------------------------------------------------------
# Pydantic schema validation
# ---------------------------------------------------------------------------


class TestDispatchSweepParams:
    def test_minimum_valid_params(self):
        p = DispatchSweepParams(
            dimension="security",
            success_criteria="verify every caller respects the new contract",
        )
        assert p.dimension == "security"
        assert p.budget_tokens == 150_000
        assert p.model_tier == "explorer"
        assert p.triggering_symbols is None
        assert p.direction_hint is None

    def test_budget_floor_enforced(self):
        with pytest.raises(ValidationError):
            DispatchSweepParams(
                dimension="security",
                success_criteria="x" * 20,
                budget_tokens=50_000,
            )

    def test_budget_ceiling_enforced(self):
        with pytest.raises(ValidationError):
            DispatchSweepParams(
                dimension="security",
                success_criteria="x" * 20,
                budget_tokens=250_000,
            )

    def test_success_criteria_min_length(self):
        with pytest.raises(ValidationError):
            DispatchSweepParams(
                dimension="security",
                success_criteria="short",
            )

    def test_triggering_symbols_cap(self):
        # max_length=20 on triggering_symbols
        with pytest.raises(ValidationError):
            DispatchSweepParams(
                dimension="security",
                success_criteria="x" * 20,
                triggering_symbols=[f"sym{i}" for i in range(21)],
            )

    def test_direction_hint_length_cap(self):
        with pytest.raises(ValidationError):
            DispatchSweepParams(
                dimension="security",
                success_criteria="x" * 20,
                direction_hint="x" * 501,
            )

    def test_all_factory_dimensions_accepted(self):
        for dim in [
            "security", "correctness", "concurrency",
            "reliability", "performance", "test_coverage",
            "api_contract",
        ]:
            p = DispatchSweepParams(
                dimension=dim,
                success_criteria="x" * 20,
            )
            assert p.dimension == dim


# ---------------------------------------------------------------------------
# Cap thresholds
# ---------------------------------------------------------------------------


class TestDimensionDispatchCap:
    def test_small_pr_zero_dimensions(self):
        assert _dimension_dispatch_cap(0) == 0
        assert _dimension_dispatch_cap(1) == 0
        assert _dimension_dispatch_cap(4) == 0

    def test_medium_pr_one_dimension(self):
        assert _dimension_dispatch_cap(5) == 1
        assert _dimension_dispatch_cap(10) == 1
        assert _dimension_dispatch_cap(14) == 1

    def test_large_pr_two_dimensions(self):
        assert _dimension_dispatch_cap(15) == 2
        assert _dimension_dispatch_cap(50) == 2
        assert _dimension_dispatch_cap(200) == 2


# ---------------------------------------------------------------------------
# Trigger detector
# ---------------------------------------------------------------------------


def _make_pr_context_with_files(paths):
    """Build a PRContext-ish object with .business_logic_files() -> files."""
    from app.code_review.models import (
        ChangedFile,
        FileCategory,
        PRContext,
    )
    files = [
        ChangedFile(
            path=p,
            additions=10,
            deletions=2,
            category=FileCategory.BUSINESS_LOGIC,
        )
        for p in paths
    ]
    return PRContext(diff_spec="test...main", files=files)


class TestDetectDimensionTriggers:
    def test_empty_pr_returns_empty(self, monkeypatch):
        pr = _make_pr_context_with_files([])
        out = _detect_dimension_triggers("/tmp/ws", pr)
        assert out == []

    def test_single_caller_does_not_trigger(self, monkeypatch):
        """1 caller file doesn't meet the ≥3 threshold."""
        pr = _make_pr_context_with_files(["src/core/foo.py"])

        def fake_get_dependents(**kwargs):
            return ToolResult(
                tool_name="get_dependents",
                success=True,
                data=[
                    {"file_path": "src/uses/caller_a.py",
                     "symbols": ["foo_func"]},
                ],
            )

        monkeypatch.setattr(
            "app.code_tools.tools.get_dependents",
            fake_get_dependents,
        )
        out = _detect_dimension_triggers("/tmp/ws", pr)
        assert out == []

    def test_three_caller_files_trigger(self, monkeypatch):
        pr = _make_pr_context_with_files(["src/core/api.py"])

        def fake_get_dependents(**kwargs):
            return ToolResult(
                tool_name="get_dependents",
                success=True,
                data=[
                    {"file_path": "src/ui/handler.py",
                     "symbols": ["issue_token"]},
                    {"file_path": "src/cli/admin.py",
                     "symbols": ["issue_token", "refresh_token"]},
                    {"file_path": "src/mobile/auth.py",
                     "symbols": ["issue_token"]},
                ],
            )

        monkeypatch.setattr(
            "app.code_tools.tools.get_dependents",
            fake_get_dependents,
        )
        out = _detect_dimension_triggers("/tmp/ws", pr)
        assert len(out) == 1
        trig = out[0]
        assert trig["file"] == "src/core/api.py"
        assert trig["caller_count"] == 3
        assert "src/ui/handler.py" in trig["caller_files"]
        assert "issue_token" in trig["hotspot_symbols"]

    def test_five_symbols_trigger_even_with_two_caller_files(self, monkeypatch):
        """Secondary condition: ≥5 distinct calling symbols (wide API surface)."""
        pr = _make_pr_context_with_files(["src/shared/lib.py"])

        def fake_get_dependents(**kwargs):
            return ToolResult(
                tool_name="get_dependents",
                success=True,
                data=[
                    {"file_path": "src/a.py",
                     "symbols": ["f1", "f2", "f3"]},
                    {"file_path": "src/b.py",
                     "symbols": ["f4", "f5"]},
                ],
            )

        monkeypatch.setattr(
            "app.code_tools.tools.get_dependents",
            fake_get_dependents,
        )
        out = _detect_dimension_triggers("/tmp/ws", pr)
        assert len(out) == 1
        assert out[0]["caller_count"] == 2
        # 5 distinct symbols across 2 files still fires
        assert len(out[0]["hotspot_symbols"]) == 5

    def test_self_reference_is_filtered(self, monkeypatch):
        """A file's own path should not be counted as a caller."""
        pr = _make_pr_context_with_files(["src/core/api.py"])

        def fake_get_dependents(**kwargs):
            return ToolResult(
                tool_name="get_dependents",
                success=True,
                data=[
                    {"file_path": "src/core/api.py",
                     "symbols": ["_helper"]},
                    {"file_path": "src/a.py", "symbols": ["api_fn"]},
                    {"file_path": "src/b.py", "symbols": ["api_fn"]},
                ],
            )

        monkeypatch.setattr(
            "app.code_tools.tools.get_dependents",
            fake_get_dependents,
        )
        out = _detect_dimension_triggers("/tmp/ws", pr)
        # Only 2 distinct external callers — doesn't meet ≥3 caller-file
        # threshold, and 2 symbols < 5 symbol threshold, so no trigger.
        assert out == []

    def test_get_dependents_exception_is_swallowed(self, monkeypatch):
        pr = _make_pr_context_with_files(["src/x.py"])

        def fake_get_dependents(**kwargs):
            raise RuntimeError("tool blew up")

        monkeypatch.setattr(
            "app.code_tools.tools.get_dependents",
            fake_get_dependents,
        )
        # Fails soft — just skips the file.
        out = _detect_dimension_triggers("/tmp/ws", pr)
        assert out == []

    def test_no_data_returns_empty(self, monkeypatch):
        pr = _make_pr_context_with_files(["src/x.py"])

        def fake_get_dependents(**kwargs):
            return ToolResult(
                tool_name="get_dependents",
                success=True,
                data=[],
            )

        monkeypatch.setattr(
            "app.code_tools.tools.get_dependents",
            fake_get_dependents,
        )
        out = _detect_dimension_triggers("/tmp/ws", pr)
        assert out == []


# ---------------------------------------------------------------------------
# AgentToolExecutor dispatch wiring
# ---------------------------------------------------------------------------


class TestDispatchDimensionWorkerExecutor:
    @pytest.mark.asyncio
    async def test_unknown_dimension_rejected(self):
        inner = MagicMock()
        executor = AgentToolExecutor(
            inner_executor=inner,
            agent_registry={},
            swarm_registry={},
            agent_provider=MagicMock(),
            max_depth=2,
        )
        result = await executor._dispatch_sweep({
            "dimension": "nonsense",
            "success_criteria": "x" * 20,
        })
        assert not result.success
        assert "Unknown dimension" in result.error

    @pytest.mark.asyncio
    async def test_missing_success_criteria_rejected(self):
        inner = MagicMock()
        executor = AgentToolExecutor(
            inner_executor=inner,
            agent_registry={},
            swarm_registry={},
            agent_provider=MagicMock(),
            max_depth=2,
        )
        result = await executor._dispatch_sweep({
            "dimension": "security",
            "success_criteria": "short",
        })
        assert not result.success
        assert "success_criteria" in result.error

    @pytest.mark.asyncio
    async def test_budget_out_of_range_rejected(self):
        inner = MagicMock()
        executor = AgentToolExecutor(
            inner_executor=inner,
            agent_registry={},
            swarm_registry={},
            agent_provider=MagicMock(),
            max_depth=2,
        )
        result = await executor._dispatch_sweep({
            "dimension": "security",
            "success_criteria": "x" * 20,
            "budget_tokens": 50_000,
        })
        assert not result.success
        assert "budget_tokens" in result.error

    @pytest.mark.asyncio
    async def test_recursion_depth_wall(self):
        inner = MagicMock()
        executor = AgentToolExecutor(
            inner_executor=inner,
            agent_registry={},
            swarm_registry={},
            agent_provider=MagicMock(),
            max_depth=2,
        )
        executor._current_depth = 2
        result = await executor._dispatch_sweep({
            "dimension": "security",
            "success_criteria": "x" * 20,
        })
        assert not result.success
        assert "depth" in result.error.lower()

    @pytest.mark.asyncio
    async def test_happy_path_returns_parsed_findings(self, monkeypatch, tmp_path):
        """Full-path dispatch with a fake _dispatch_explore returning JSON."""
        inner = MagicMock()
        executor = AgentToolExecutor(
            inner_executor=inner,
            agent_registry={},
            swarm_registry={},
            agent_provider=MagicMock(),
            max_depth=2,
        )

        canned_answer = json.dumps({
            "checks": [],
            "findings": [
                {
                    "title": "Caller drops the refresh token",
                    "file": "src/mobile/auth.py",
                    "start_line": 42,
                    "end_line": 42,
                    "severity": "high",  # should be forced to null below
                    "confidence": 0.9,
                    "evidence": ["token = TokenService.issue(...)"],
                },
            ],
            "unexpected_observations": [],
        })

        async def fake_dispatch_explore(params):
            return ToolResult(
                tool_name="dispatch_explore",
                success=True,
                data={
                    "answer": canned_answer,
                    "iterations": 3,
                    "total_input_tokens": 40_000,
                    "total_output_tokens": 1_200,
                    "files_accessed": ["src/core/api.py"],
                },
            )

        # Point _load_role_template to a fake so we don't require the factory file.
        def fake_load_role_template(role):
            return {
                "frontmatter": {
                    "tools_hint": ["grep", "read_file"],
                    "model_hint": "explorer",
                },
                "body": "stub lens body",
            }

        monkeypatch.setattr(
            "app.agent_loop.brain._load_role_template",
            fake_load_role_template,
        )
        monkeypatch.setattr(
            "app.agent_loop.brain._compose_role_system_prompt",
            lambda **kw: "composed perspective",
        )
        monkeypatch.setattr(executor, "_dispatch_explore", fake_dispatch_explore)

        result = await executor._dispatch_sweep({
            "dimension": "api_contract",
            "direction_hint": "verify callers destructure tuple",
            "triggering_symbols": ["TokenService.issue"],
            "success_criteria": "Report every caller's destructure correctness",
            "budget_tokens": 150_000,
            "model_tier": "explorer",
        })

        assert result.success
        assert result.data["_dispatch_mode"] == "dimension"
        assert result.data["_dimension"] == "api_contract"
        assert len(result.data["findings"]) == 1
        f = result.data["findings"][0]
        # severity coerced to null
        assert f["severity"] is None
        # dispatch tag present
        assert f["_dispatched_by"] == "dimension=api_contract"
