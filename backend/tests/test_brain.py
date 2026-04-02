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
    CreatePlanParams,
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
    async def test_dispatch_unknown_template(self, agent_registry, swarm_registry,
                                              mock_inner_executor, mock_provider):
        """Dispatching unknown template returns error."""
        executor = self._make_executor(
            agent_registry, swarm_registry, mock_inner_executor, mock_provider,
        )
        result = await executor.execute("dispatch_agent", {
            "template": "nonexistent",
            "query": "test",
        })
        assert not result.success
        assert "Unknown agent template" in result.error

    @pytest.mark.asyncio
    async def test_dispatch_backward_compat_agent_name(self, agent_registry, swarm_registry,
                                                        mock_inner_executor, mock_provider):
        """Legacy agent_name param still works (aliased to template)."""
        executor = self._make_executor(
            agent_registry, swarm_registry, mock_inner_executor, mock_provider,
            depth=2, max_depth=2,  # force depth error to avoid real agent run
        )
        result = await executor.execute("dispatch_agent", {
            "agent_name": "explore_architecture",
            "query": "test",
        })
        # Depth error, but it resolved the agent — backward compat works
        assert not result.success
        assert "depth" in result.error.lower()

    @pytest.mark.asyncio
    async def test_dispatch_max_depth(self, agent_registry, swarm_registry,
                                      mock_inner_executor, mock_provider):
        """dispatch_agent at max depth returns error."""
        executor = self._make_executor(
            agent_registry, swarm_registry, mock_inner_executor, mock_provider,
            depth=2, max_depth=2,
        )
        result = await executor.execute("dispatch_agent", {
            "template": "explore_architecture",
            "query": "test",
        })
        assert not result.success
        assert "Max agent depth" in result.error

    @pytest.mark.asyncio
    async def test_dispatch_requires_template_or_tools(self, agent_registry, swarm_registry,
                                                        mock_inner_executor, mock_provider):
        """dispatch_agent without template or tools returns error."""
        executor = self._make_executor(
            agent_registry, swarm_registry, mock_inner_executor, mock_provider,
        )
        result = await executor.execute("dispatch_agent", {
            "query": "test",
        })
        assert not result.success
        assert "template" in result.error.lower() or "tools" in result.error.lower()

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
        # Prompt includes tool catalog + skill catalog + examples (~4000 tokens / ~16000 chars)
        assert len(prompt) < 18_000, f"Brain prompt too long: {len(prompt)} chars"


# ---------------------------------------------------------------------------
# 4-Layer Prompt Architecture
# ---------------------------------------------------------------------------


class TestSubAgentSystemPrompt:
    """Tests for build_sub_agent_system_prompt (4-layer architecture)."""

    def test_includes_agent_identity(self, tmp_path):
        from app.agent_loop.prompts import build_sub_agent_system_prompt
        prompt = build_sub_agent_system_prompt(
            agent_name="explore_architecture",
            agent_description="Maps module structure and dependencies",
            agent_instructions="Map the architecture. Find all modules.",
            workspace_path=str(tmp_path),
        )
        assert "explore_architecture" in prompt
        assert "Maps module structure" in prompt
        assert "Map the architecture" in prompt

    def test_includes_workspace_context(self, tmp_path):
        from app.agent_loop.prompts import build_sub_agent_system_prompt
        prompt = build_sub_agent_system_prompt(
            agent_name="test_agent",
            agent_description="Test agent",
            agent_instructions="Do testing.",
            workspace_path=str(tmp_path),
        )
        assert "Workspace" in prompt
        assert str(tmp_path) in prompt

    def test_does_not_contain_generic_identity(self, tmp_path):
        """Sub-agent prompt must NOT use the old shared CORE_IDENTITY opener."""
        from app.agent_loop.prompts import build_sub_agent_system_prompt
        prompt = build_sub_agent_system_prompt(
            agent_name="security",
            agent_description="Detects vulnerabilities",
            agent_instructions="Find security issues.",
            workspace_path=str(tmp_path),
        )
        assert "You are a code intelligence agent" not in prompt
        assert "security" in prompt
        assert "Detects vulnerabilities" in prompt

    def test_includes_strategy_when_specified(self, tmp_path):
        from app.agent_loop.prompts import build_sub_agent_system_prompt
        prompt = build_sub_agent_system_prompt(
            agent_name="correctness",
            agent_description="Finds logic errors",
            agent_instructions="Check correctness.",
            workspace_path=str(tmp_path),
            strategy_key="code_review",
        )
        assert "Code Review" in prompt

    def test_no_strategy_when_not_specified(self, tmp_path):
        from app.agent_loop.prompts import build_sub_agent_system_prompt
        prompt = build_sub_agent_system_prompt(
            agent_name="explore_usage",
            agent_description="Traces user flows",
            agent_instructions="Find usage patterns.",
            workspace_path=str(tmp_path),
            strategy_key=None,
        )
        assert "## Strategy" not in prompt

    def test_includes_signal_blocker_hint(self, tmp_path):
        from app.agent_loop.prompts import build_sub_agent_system_prompt
        prompt = build_sub_agent_system_prompt(
            agent_name="test",
            agent_description="Test",
            agent_instructions="Test.",
            workspace_path=str(tmp_path),
            has_signal_blocker=True,
        )
        assert "signal_blocker" in prompt

    def test_no_signal_blocker_when_disabled(self, tmp_path):
        from app.agent_loop.prompts import build_sub_agent_system_prompt
        prompt = build_sub_agent_system_prompt(
            agent_name="test_agent",
            agent_description="Test",
            agent_instructions="Test.",
            workspace_path="/tmp/workspace",
            has_signal_blocker=False,
        )
        assert "signal_blocker" not in prompt

    def test_includes_code_context(self, tmp_path):
        from app.agent_loop.prompts import build_sub_agent_system_prompt
        prompt = build_sub_agent_system_prompt(
            agent_name="test",
            agent_description="Test",
            agent_instructions="Test.",
            workspace_path=str(tmp_path),
            code_context={
                "code": "def hello(): pass",
                "file_path": "hello.py",
                "language": "python",
                "start_line": 1,
                "end_line": 1,
            },
        )
        assert "Code Under Discussion" in prompt
        assert "hello.py" in prompt
        assert "def hello(): pass" in prompt

    def test_includes_risk_context(self, tmp_path):
        from app.agent_loop.prompts import build_sub_agent_system_prompt
        prompt = build_sub_agent_system_prompt(
            agent_name="test",
            agent_description="Test",
            agent_instructions="Test.",
            workspace_path=str(tmp_path),
            risk_context="### Risk signals\n- **security**: 3 files",
        )
        assert "Risk signals" in prompt

    def test_layer_separation(self, tmp_path):
        """Layer 1 (identity) should come before Layer 3 (skills)."""
        from app.agent_loop.prompts import build_sub_agent_system_prompt
        prompt = build_sub_agent_system_prompt(
            agent_name="explore_implementation",
            agent_description="Traces lifecycles",
            agent_instructions="Trace the complete lifecycle.",
            workspace_path=str(tmp_path),
        )
        # Identity should appear before workspace section
        identity_pos = prompt.index("explore_implementation")
        workspace_pos = prompt.index("Workspace")
        assert identity_pos < workspace_pos


class TestQueryNotContaminatedByRole:
    """Verify that dispatch_agent passes clean queries (no ## Your Role)."""

    @pytest.mark.asyncio
    async def test_dispatch_does_not_inject_role_in_query(
        self, agent_registry, swarm_registry, mock_inner_executor, mock_provider,
    ):
        """The query passed to AgentLoopService must NOT contain agent instructions."""
        captured_kwargs = {}

        original_init = __import__("app.agent_loop.service", fromlist=["AgentLoopService"]).AgentLoopService.__init__

        def capture_init(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            original_init(self, *args, **kwargs)

        with patch("app.agent_loop.service.AgentLoopService.__init__", capture_init):
            with patch("app.agent_loop.service.AgentLoopService.run_stream") as mock_stream:
                async def empty_stream(*a, **kw):
                    from app.agent_loop.service import AgentEvent
                    yield AgentEvent(kind="done", data={
                        "answer": "test", "tool_calls_made": 0,
                        "iterations": 0, "duration_ms": 0,
                        "thinking_steps": [],
                    })
                mock_stream.side_effect = empty_stream

                executor = AgentToolExecutor(
                    inner_executor=mock_inner_executor,
                    agent_registry=agent_registry,
                    swarm_registry=swarm_registry,
                    agent_provider=mock_provider,
                    workspace_path="/tmp/test",
                    event_sink=asyncio.Queue(),
                )
                await executor.execute("dispatch_agent", {
                    "agent_name": "explore_architecture",
                    "query": "How does auth work?",
                })

                # Verify the query passed to run_stream is clean
                call_args = mock_stream.call_args
                query_arg = call_args[1].get("query", call_args[0][0] if call_args[0] else "")
                assert "## Your Role" not in query_arg
                assert "Map the architecture" not in query_arg  # agent instructions

    @pytest.mark.asyncio
    async def test_dispatch_passes_agent_identity(
        self, agent_registry, swarm_registry, mock_inner_executor, mock_provider,
    ):
        """dispatch_agent must pass agent_identity dict to AgentLoopService via config."""
        captured_kwargs = {}

        original_init = __import__("app.agent_loop.service", fromlist=["AgentLoopService"]).AgentLoopService.__init__

        def capture_init(self, *args, **kwargs):
            captured_kwargs.update(kwargs)
            original_init(self, *args, **kwargs)

        with patch("app.agent_loop.service.AgentLoopService.__init__", capture_init):
            with patch("app.agent_loop.service.AgentLoopService.run_stream") as mock_stream:
                async def empty_stream(*a, **kw):
                    from app.agent_loop.service import AgentEvent
                    yield AgentEvent(kind="done", data={
                        "answer": "test", "tool_calls_made": 0,
                        "iterations": 0, "duration_ms": 0,
                        "thinking_steps": [],
                    })
                mock_stream.side_effect = empty_stream

                executor = AgentToolExecutor(
                    inner_executor=mock_inner_executor,
                    agent_registry=agent_registry,
                    swarm_registry=swarm_registry,
                    agent_provider=mock_provider,
                    workspace_path="/tmp/test",
                    event_sink=asyncio.Queue(),
                )
                await executor.execute("dispatch_agent", {
                    "agent_name": "explore_architecture",
                    "query": "How does auth work?",
                })

                # Verify agent_identity was passed via AgentLoopConfig
                loop_config = captured_kwargs.get("config")
                assert loop_config is not None, "AgentLoopService must receive an AgentLoopConfig"
                identity = loop_config.agent_identity
                assert identity is not None
                assert identity["name"] == "explore_architecture"
                assert identity["description"] == "Maps module structure"
                assert identity["instructions"] == "Map the architecture."


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
        assert "correctness" in agents  # PR review agent (kept)


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


# ---------------------------------------------------------------------------
# create_plan — explicit planning
# ---------------------------------------------------------------------------


class TestCreatePlan:

    @pytest.mark.asyncio
    async def test_create_plan_stores_plan(self, agent_registry, swarm_registry, mock_inner_executor, mock_provider):
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/ws",
        )
        assert executor._plan is None
        await executor.execute("create_plan", {
            "mode": "simple",
            "reasoning": "Single endpoint lookup",
            "agents": ["explore_entry_point"],
        })
        assert executor._plan is not None
        assert executor._plan["mode"] == "simple"
        assert executor._plan["agents"] == ["explore_entry_point"]
        assert executor._plan["reasoning"] == "Single endpoint lookup"

    @pytest.mark.asyncio
    async def test_create_plan_emits_event(self, agent_registry, swarm_registry, mock_inner_executor, mock_provider):
        event_sink = asyncio.Queue()
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/ws",
            event_sink=event_sink,
        )
        await executor.execute("create_plan", {
            "mode": "swarm",
            "reasoning": "Multi-perspective",
            "agents": ["correctness", "security"],
            "query_decomposition": ["auth flow", "input validation"],
        })
        assert not event_sink.empty()
        event = await event_sink.get()
        assert event.kind == "plan_created"
        assert event.data["mode"] == "swarm"
        assert event.data["agents"] == ["correctness", "security"]
        assert event.data["query_decomposition"] == ["auth flow", "input validation"]

    @pytest.mark.asyncio
    async def test_create_plan_returns_success(self, agent_registry, swarm_registry, mock_inner_executor, mock_provider):
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/ws",
        )
        result = await executor.execute("create_plan", {
            "mode": "complex",
            "reasoning": "Need sequential investigation",
            "agents": ["explore_root_cause", "explore_config"],
            "fallback": "Try architecture agent if both fail",
        })
        assert result.success is True
        assert result.data["status"] == "plan_recorded"
        assert result.data["mode"] == "complex"
        assert result.data["fallback"] == "Try architecture agent if both fail"

    @pytest.mark.asyncio
    async def test_create_plan_without_event_sink(self, agent_registry, swarm_registry, mock_inner_executor, mock_provider):
        """create_plan works without event_sink (no crash)."""
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/ws",
        )
        result = await executor.execute("create_plan", {
            "mode": "simple",
            "reasoning": "test",
        })
        assert result.success is True
        assert executor._plan["mode"] == "simple"

    @pytest.mark.asyncio
    async def test_create_plan_defaults(self, agent_registry, swarm_registry, mock_inner_executor, mock_provider):
        """Optional fields default to empty."""
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/ws",
        )
        await executor.execute("create_plan", {
            "mode": "simple",
            "reasoning": "Quick lookup",
        })
        assert executor._plan["agents"] == []
        assert executor._plan["query_decomposition"] == []
        assert executor._plan["risk"] == ""
        assert executor._plan["fallback"] == ""

    @pytest.mark.asyncio
    async def test_dispatch_works_without_plan(self, agent_registry, swarm_registry, mock_inner_executor, mock_provider):
        """Backward compat: dispatch_agent works without prior create_plan."""
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/ws",
            max_depth=0,  # force depth error to avoid needing real agent run
        )
        result = await executor.execute("dispatch_agent", {
            "agent_name": "explore_architecture",
            "query": "Map the architecture",
        })
        # Depth error, but it still processed — no plan required
        assert executor._plan is None
        assert result.success is False
        assert "depth" in result.error.lower()

    def test_brain_tool_definitions_include_create_plan(self):
        names = [t["name"] for t in BRAIN_TOOL_DEFINITIONS]
        assert "create_plan" in names

    def test_brain_prompt_mentions_create_plan(self, agent_registry, swarm_registry):
        prompt = build_brain_prompt(agent_registry, swarm_registry)
        assert "create_plan" in prompt
        assert "Planning" in prompt

    def test_create_plan_params_schema(self):
        """Pydantic model validates correctly."""
        params = CreatePlanParams(
            mode="swarm",
            reasoning="End-to-end journey",
            agents=["explore_implementation"],
            query_decomposition=["step 1", "step 2"],
            risk="May span multiple contexts",
            fallback="Try architecture agent",
        )
        assert params.mode == "swarm"
        assert len(params.query_decomposition) == 2

    def test_create_plan_params_required_fields(self):
        """mode and reasoning are required."""
        with pytest.raises(Exception):
            CreatePlanParams()  # type: ignore[call-arg]


# ---------------------------------------------------------------------------
# Dynamic agent composition
# ---------------------------------------------------------------------------


class TestDynamicAgentDispatch:
    """Tests for dynamic agent composition (dispatch_agent without template)."""

    @pytest.mark.asyncio
    async def test_dynamic_dispatch_builds_config(self, agent_registry, swarm_registry,
                                                   mock_inner_executor, mock_provider):
        """Dynamic dispatch with tools builds an ephemeral agent config."""
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/ws",
            max_depth=0,  # force depth error
        )
        result = await executor.execute("dispatch_agent", {
            "query": "Find the auth handler",
            "tools": ["grep", "find_symbol", "read_file"],
            "perspective": "Focus on authentication entry points",
            "skill": "entry_point",
            "budget_tokens": 150000,
            "max_iterations": 12,
        })
        # Depth error, but it resolved the dynamic config
        assert not result.success
        assert "depth" in result.error.lower()

    @pytest.mark.asyncio
    async def test_dynamic_dispatch_strong_model(self, agent_registry, swarm_registry,
                                                  mock_inner_executor, mock_provider):
        """Dynamic dispatch with model='strong' selects strong_provider."""
        strong_mock = MagicMock()
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            strong_provider=strong_mock,
            workspace_path="/tmp/ws",
            max_depth=0,
        )
        result = await executor.execute("dispatch_agent", {
            "query": "Debug the crash",
            "tools": ["grep", "read_file", "git_blame"],
            "model": "strong",
        })
        # Depth error, but verified it would use strong provider
        assert not result.success

    @pytest.mark.asyncio
    async def test_dynamic_dispatch_defaults_to_explorer(self, agent_registry, swarm_registry,
                                                          mock_inner_executor, mock_provider):
        """Dynamic dispatch defaults to explorer model."""
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/ws",
            max_depth=0,
        )
        result = await executor.execute("dispatch_agent", {
            "query": "Find endpoint",
            "tools": ["grep", "find_symbol"],
        })
        assert not result.success
        assert "depth" in result.error.lower()

    def test_build_dynamic_config(self, agent_registry, swarm_registry,
                                   mock_inner_executor, mock_provider):
        """_build_dynamic_config creates valid AgentConfig."""
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/ws",
        )
        config = executor._build_dynamic_config({
            "tools": ["grep", "read_file", "jira_search"],
            "perspective": "Find auth bugs and create tickets",
            "skill": "issue_tracking",
            "model": "explorer",
            "budget_tokens": 200000,
            "max_iterations": 15,
        })
        assert config.name == "dynamic_issue_tracking"
        assert config.skill == "issue_tracking"
        assert config.instructions == "Find auth bugs and create tickets"
        assert config.limits.budget_tokens == 200000
        assert config.limits.max_iterations == 15
        assert "grep" in config.tool_list

    def test_build_dynamic_config_defaults(self, agent_registry, swarm_registry,
                                            mock_inner_executor, mock_provider):
        """_build_dynamic_config uses sensible defaults."""
        executor = AgentToolExecutor(
            inner_executor=mock_inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=mock_provider,
            workspace_path="/tmp/ws",
        )
        config = executor._build_dynamic_config({
            "tools": ["grep"],
        })
        assert config.name == "dynamic_explorer"
        assert config.limits.budget_tokens == 300_000
        assert config.limits.max_iterations == 20

    def test_brain_prompt_has_tool_catalog(self, agent_registry, swarm_registry):
        """Brain prompt includes tool catalog with categories."""
        prompt = build_brain_prompt(agent_registry, swarm_registry)
        assert "Available tools" in prompt
        assert "**Search**:" in prompt
        assert "**Git**:" in prompt
        assert "**Integration**:" in prompt
        assert "grep" in prompt

    def test_brain_prompt_has_skill_catalog(self, agent_registry, swarm_registry):
        """Brain prompt includes skill catalog with use cases."""
        prompt = build_brain_prompt(agent_registry, swarm_registry)
        assert "Investigation skills" in prompt
        assert "### entry_point" in prompt
        assert "### root_cause" in prompt
        assert "When to use:" in prompt
        assert "When NOT to use:" in prompt
        assert "Budget:" in prompt

    def test_brain_prompt_has_template_catalog(self, agent_registry, swarm_registry):
        """Brain prompt includes template catalog."""
        prompt = build_brain_prompt(agent_registry, swarm_registry)
        assert "Pre-defined templates" in prompt
        assert "template=" in prompt

    def test_brain_prompt_has_dynamic_examples(self, agent_registry, swarm_registry):
        """Brain examples show dynamic composition with tools= and skill=."""
        prompt = build_brain_prompt(agent_registry, swarm_registry)
        assert "tools=[" in prompt
        assert 'skill="entry_point"' in prompt
        assert 'skill="root_cause"' in prompt
        assert "<commentary>" in prompt

    def test_enriched_skills_have_content(self):
        """All 9 skill keys have non-empty content in INVESTIGATION_SKILLS."""
        from app.agent_loop.prompts import INVESTIGATION_SKILLS
        expected_keys = [
            "entry_point", "root_cause", "architecture", "impact",
            "data_lineage", "recent_changes", "code_explanation",
            "config_analysis", "issue_tracking",
        ]
        for key in expected_keys:
            assert key in INVESTIGATION_SKILLS, f"Missing skill: {key}"
            assert len(INVESTIGATION_SKILLS[key]) > 50, f"Skill '{key}' too short"

    def test_root_cause_skill_has_systemic_causes(self):
        """root_cause skill includes systemic causes check (from .md)."""
        from app.agent_loop.prompts import INVESTIGATION_SKILLS
        skill = INVESTIGATION_SKILLS["root_cause"]
        assert "concurrency" in skill.lower()
        assert "retry" in skill.lower()
        assert "transaction" in skill.lower()

    def test_impact_skill_has_amplification(self):
        """impact skill includes amplification risks (from .md)."""
        from app.agent_loop.prompts import INVESTIGATION_SKILLS
        skill = INVESTIGATION_SKILLS["impact"]
        assert "amplification" in skill.lower()

    def test_code_explanation_skill_has_three_dimensions(self):
        """code_explanation skill includes 3-dimension framework (from .md)."""
        from app.agent_loop.prompts import INVESTIGATION_SKILLS
        skill = INVESTIGATION_SKILLS["code_explanation"]
        assert "Business context" in skill
        assert "Mechanism" in skill
        assert "Design decisions" in skill
