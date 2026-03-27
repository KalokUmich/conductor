"""Tests for the Brain orchestrator — AgentToolExecutor, budget, and prompt."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent_loop.brain import (
    AgentFindings,
    AgentToolExecutor,
    BrainBudgetManager,
    condense_result,
)
from app.agent_loop.prompts import build_brain_prompt
from app.agent_loop.service import AgentResult, ThinkingStep
from app.code_tools.schemas import (
    BRAIN_TOOL_DEFINITIONS,
    ToolResult,
    get_brain_tool_definitions,
)
from app.workflow.loader import load_brain_config, load_swarm_registry, load_agent_registry
from app.workflow.models import (
    AgentConfig,
    AgentLimits,
    BrainConfig,
    BrainLimits,
    SwarmConfig,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def agent_registry():
    """Minimal agent registry for testing."""
    return {
        "explore_architecture": AgentConfig(
            name="explore_architecture",
            description="Maps module structure",
            model="explorer",
            limits=AgentLimits(max_iterations=5, budget_tokens=100_000, evidence_retries=1),
            instructions="Map the architecture.",
        ),
        "explore_entry_point": AgentConfig(
            name="explore_entry_point",
            description="Finds endpoint handlers",
            model="explorer",
            limits=AgentLimits(max_iterations=5, budget_tokens=100_000, evidence_retries=1),
            instructions="Find the entry point.",
        ),
        "explore_root_cause": AgentConfig(
            name="explore_root_cause",
            description="Root cause analysis",
            model="explorer",
            limits=AgentLimits(max_iterations=5, budget_tokens=100_000, evidence_retries=1),
            instructions="Find the root cause.",
        ),
    }


@pytest.fixture
def swarm_registry():
    """Minimal swarm registry for testing."""
    return {
        "test_swarm": SwarmConfig(
            name="test_swarm",
            description="Test swarm with two agents",
            mode="parallel",
            agents=["explore_architecture", "explore_entry_point"],
        ),
    }


@pytest.fixture
def mock_inner_executor():
    """Mock inner executor that returns a simple tool result."""
    executor = AsyncMock()
    executor.execute = AsyncMock(return_value=ToolResult(
        tool_name="grep",
        success=True,
        data=[{"file_path": "test.py", "line_number": 1, "content": "def hello():"}],
    ))
    return executor


@pytest.fixture
def mock_provider():
    """Mock AI provider for sub-agents."""
    provider = MagicMock()
    provider.health_check.return_value = True
    return provider


# ---------------------------------------------------------------------------
# Brain Tool Definitions
# ---------------------------------------------------------------------------


class TestBrainToolDefinitions:

    def test_brain_tools_include_dispatch(self):
        assert any(t["name"] == "dispatch_agent" for t in BRAIN_TOOL_DEFINITIONS)
        assert any(t["name"] == "dispatch_swarm" for t in BRAIN_TOOL_DEFINITIONS)

    def test_brain_tools_exclude_code_tools(self):
        for t in BRAIN_TOOL_DEFINITIONS:
            assert t["name"] not in ("grep", "read_file", "find_symbol")

    def test_get_brain_tool_definitions_includes_ask_user(self):
        full = get_brain_tool_definitions()
        names = [t["name"] for t in full]
        assert "dispatch_agent" in names
        assert "dispatch_swarm" in names
        assert "ask_user" in names

    def test_brain_tools_have_input_schema(self):
        for t in BRAIN_TOOL_DEFINITIONS:
            assert "input_schema" in t
            assert isinstance(t["input_schema"], dict)


# ---------------------------------------------------------------------------
# BrainBudgetManager
# ---------------------------------------------------------------------------


class TestBrainBudgetManager:

    @pytest.mark.asyncio
    async def test_allocate_returns_bounded_amount(self):
        mgr = BrainBudgetManager(total_tokens=500_000)
        allocated = await mgr.allocate("agent_a")
        assert 50_000 <= allocated <= 300_000

    @pytest.mark.asyncio
    async def test_report_tracks_usage(self):
        mgr = BrainBudgetManager(total_tokens=500_000)
        await mgr.report("agent_a", 100_000)
        assert mgr.used["agent_a"] == 100_000
        assert mgr.remaining < 500_000

    @pytest.mark.asyncio
    async def test_remaining_decreases(self):
        mgr = BrainBudgetManager(total_tokens=500_000)
        initial = mgr.remaining
        await mgr.report("agent_a", 200_000)
        assert mgr.remaining < initial

    @pytest.mark.asyncio
    async def test_brain_reserve(self):
        mgr = BrainBudgetManager(total_tokens=500_000, brain_reserve_ratio=0.2)
        assert mgr.brain_reserve == 100_000


# ---------------------------------------------------------------------------
# condense_result
# ---------------------------------------------------------------------------


class TestCondenseResult:

    def test_condense_basic(self):
        result = AgentResult(
            answer="The /api/users endpoint is defined in router.py at line 10. It handles GET requests and returns a list of users from the database.",
            tool_calls_made=3,
            iterations=2,
            duration_ms=500.0,
        )
        condensed = condense_result(result)
        assert "router.py" in condensed["answer"]
        assert condensed["tool_calls_made"] == 3
        assert condensed["confidence"] == "high"
        assert isinstance(condensed["files_accessed"], list)
        assert isinstance(condensed["tools_summary"], list)

    def test_condense_low_confidence_empty_answer(self):
        result = AgentResult(answer="", tool_calls_made=1)
        condensed = condense_result(result)
        assert condensed["confidence"] == "low"

    def test_condense_low_confidence_few_tools(self):
        result = AgentResult(answer="Some answer here that is long enough.", tool_calls_made=1)
        condensed = condense_result(result)
        assert condensed["confidence"] == "low"

    def test_condense_with_error(self):
        result = AgentResult(answer="", error="Timeout")
        condensed = condense_result(result)
        assert condensed["error"] == "Timeout"


# ---------------------------------------------------------------------------
# AgentToolExecutor
# ---------------------------------------------------------------------------


class TestAgentToolExecutor:

    def _make_executor(self, agent_registry, swarm_registry, mock_inner, mock_provider,
                       depth=0, max_depth=2):
        return AgentToolExecutor(
            inner_executor=mock_inner,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/test",
            current_depth=depth,
            max_depth=max_depth,
            sub_agent_timeout=5.0,  # short timeout for tests
        )

    @pytest.mark.asyncio
    async def test_passthrough_to_inner(self, agent_registry, swarm_registry,
                                        mock_inner_executor, mock_provider):
        """Non-brain tools pass through to inner executor."""
        executor = self._make_executor(
            agent_registry, swarm_registry, mock_inner_executor, mock_provider,
        )
        result = await executor.execute("grep", {"pattern": "hello"})
        assert result.success
        mock_inner_executor.execute.assert_called_once_with("grep", {"pattern": "hello"})

    @pytest.mark.asyncio
    async def test_dispatch_unknown_agent(self, agent_registry, swarm_registry,
                                          mock_inner_executor, mock_provider):
        """Dispatching unknown agent returns error."""
        executor = self._make_executor(
            agent_registry, swarm_registry, mock_inner_executor, mock_provider,
        )
        result = await executor.execute("dispatch_agent", {
            "agent_name": "nonexistent",
            "query": "test",
        })
        assert not result.success
        assert "Unknown agent" in result.error

    @pytest.mark.asyncio
    async def test_dispatch_max_depth(self, agent_registry, swarm_registry,
                                      mock_inner_executor, mock_provider):
        """dispatch_agent at max depth returns error."""
        executor = self._make_executor(
            agent_registry, swarm_registry, mock_inner_executor, mock_provider,
            depth=2, max_depth=2,
        )
        result = await executor.execute("dispatch_agent", {
            "agent_name": "explore_architecture",
            "query": "test",
        })
        assert not result.success
        assert "Max agent depth" in result.error

    @pytest.mark.asyncio
    async def test_dispatch_unknown_swarm(self, agent_registry, swarm_registry,
                                          mock_inner_executor, mock_provider):
        """Dispatching unknown swarm returns error."""
        executor = self._make_executor(
            agent_registry, swarm_registry, mock_inner_executor, mock_provider,
        )
        result = await executor.execute("dispatch_swarm", {
            "swarm_name": "nonexistent",
            "query": "test",
        })
        assert not result.success
        assert "Unknown swarm" in result.error

    @pytest.mark.asyncio
    async def test_dispatch_swarm_requires_preset(self, agent_registry, swarm_registry,
                                                   mock_inner_executor, mock_provider):
        """Dispatching swarm without a valid preset name returns error."""
        executor = self._make_executor(
            agent_registry, swarm_registry, mock_inner_executor, mock_provider,
        )
        result = await executor.execute("dispatch_swarm", {
            "swarm_name": "nonexistent_swarm",
            "query": "test",
        })
        assert not result.success
        assert "Unknown swarm" in result.error
        assert "dispatch_agent" in result.error


# ---------------------------------------------------------------------------
# Brain Prompt
# ---------------------------------------------------------------------------


class TestBrainPrompt:

    def test_build_prompt_includes_catalog(self, agent_registry, swarm_registry):
        prompt = build_brain_prompt(agent_registry, swarm_registry)
        assert "explore_architecture" in prompt
        assert "explore_entry_point" in prompt
        assert "test_swarm" in prompt

    def test_build_prompt_includes_examples(self, agent_registry, swarm_registry):
        prompt = build_brain_prompt(agent_registry, swarm_registry)
        assert "<example>" in prompt
        assert "dispatch_agent" in prompt
        assert "dispatch_swarm" in prompt
        assert "ask_user" in prompt

    def test_build_prompt_includes_qa_cache(self, agent_registry, swarm_registry):
        prompt = build_brain_prompt(
            agent_registry, swarm_registry,
            qa_cache={"payment_system": "Clearer card payments"},
        )
        assert "Clearer card payments" in prompt
        assert "Previous user clarifications" in prompt

    def test_build_prompt_no_qa_cache(self, agent_registry, swarm_registry):
        prompt = build_brain_prompt(agent_registry, swarm_registry)
        assert "Previous user clarifications" not in prompt

    def test_build_prompt_token_budget(self, agent_registry, swarm_registry):
        prompt = build_brain_prompt(agent_registry, swarm_registry)
        # Should be under ~2500 tokens (~10000 chars)
        assert len(prompt) < 12_000, f"Brain prompt too long: {len(prompt)} chars"


# ---------------------------------------------------------------------------
# Config Loading
# ---------------------------------------------------------------------------


class TestBrainConfigLoading:

    def test_load_brain_config(self):
        config = load_brain_config()
        assert config.model == "strong"
        assert config.limits.max_iterations == 20
        assert config.limits.total_session_tokens == 800_000
        assert "grep" in config.core_tools

    def test_load_swarm_registry(self):
        swarms = load_swarm_registry()
        assert "pr_review" in swarms
        assert "business_flow" in swarms
        assert len(swarms["pr_review"].agents) == 5
        assert swarms["pr_review"].mode == "parallel"

    def test_load_agent_registry(self):
        agents = load_agent_registry()
        assert len(agents) > 0
        assert "explore_architecture" in agents


# ---------------------------------------------------------------------------
# AgentFindings dataclass
# ---------------------------------------------------------------------------


class TestAgentFindings:

    def test_defaults(self):
        f = AgentFindings()
        assert f.answer == ""
        assert f.confidence == "medium"
        assert f.files_accessed == []
        assert f.error is None

    def test_with_data(self):
        f = AgentFindings(
            answer="Found it",
            files_accessed=["a.py", "b.py"],
            confidence="high",
            iterations=3,
        )
        assert f.answer == "Found it"
        assert len(f.files_accessed) == 2
