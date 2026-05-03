"""Tests for the Domain Brain orchestrator + transfer_to_brain('domain') wiring.

Domain Brain is structurally simple — one Sonnet coordinator with code-survey
tools + dispatch_explore, driven by the domain_brain_coordinator skill. These
tests cover:

1. transfer_to_brain accepts "domain" (alongside "pr_review")
2. domain_brain_coordinator skill loads and is non-empty
3. config/brains/domain.yaml loads via the shared BrainConfig schema
4. DomainBrainOrchestrator constructs cleanly with mock providers
5. The orchestrator dispatches its coordinator as a dynamic-mode dispatch_explore
   call (mirrors PR Brain v2 pattern)
6. The General Brain prompt teaches Domain Brain handoff
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest


# ---------------------------------------------------------------------------
# 1. transfer_to_brain accepts "domain"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_transfer_to_brain_accepts_domain():
    """The General Brain's transfer_to_brain handler must accept 'domain'."""
    from app.agent_loop.brain import AgentToolExecutor

    inner = AsyncMock()
    executor = AgentToolExecutor(
        inner_executor=inner,
        agent_registry={},
        swarm_registry={},
        agent_provider=MagicMock(),
        workspace_path="/tmp/test",
    )
    result = await executor.execute(
        "transfer_to_brain",
        {"brain_name": "domain", "workspace_path": "/tmp/ws", "query": "how does X work"},
    )
    assert result.success
    assert result.data["brain"] == "domain"
    assert result.data["transfer"] is True


@pytest.mark.asyncio
async def test_transfer_to_brain_accepts_pr_review():
    """Regression guard — pr_review handoff still works after domain was added."""
    from app.agent_loop.brain import AgentToolExecutor

    inner = AsyncMock()
    executor = AgentToolExecutor(
        inner_executor=inner,
        agent_registry={},
        swarm_registry={},
        agent_provider=MagicMock(),
        workspace_path="/tmp/test",
    )
    result = await executor.execute(
        "transfer_to_brain",
        {"brain_name": "pr_review", "workspace_path": "/tmp/ws"},
    )
    assert result.success


@pytest.mark.asyncio
async def test_transfer_to_brain_rejects_unknown():
    """Unknown brain names still error out — error message must list both options."""
    from app.agent_loop.brain import AgentToolExecutor

    inner = AsyncMock()
    executor = AgentToolExecutor(
        inner_executor=inner,
        agent_registry={},
        swarm_registry={},
        agent_provider=MagicMock(),
        workspace_path="/tmp/test",
    )
    result = await executor.execute(
        "transfer_to_brain",
        {"brain_name": "made_up_brain"},
    )
    assert not result.success
    assert "domain" in result.error
    assert "pr_review" in result.error


# ---------------------------------------------------------------------------
# 2. Skill content loads
# ---------------------------------------------------------------------------

def test_domain_coordinator_skill_loads():
    """Skill file exists and contains the load-bearing rules."""
    from app.agent_loop.prompts import _load_skill

    skill = _load_skill("domain_brain_coordinator")
    assert len(skill) > 1000, "skill should be substantive (~5KB)"
    # Load-bearing content checks — these are the rules that prevent the
    # synthesis regression diagnosed on 2026-05-03.
    assert "Phase 1" in skill and "Scope Survey" in skill
    assert "DEPTH" in skill and "BREADTH" in skill
    # 8 numbered synthesis rules
    for n in (1, 2, 3, 4, 5, 6, 7, 8):
        assert f"{n}." in skill
    assert "Preserve specifics" in skill
    assert "Flow Overview" in skill
    assert "Step-by-Step" in skill
    assert "Gaps" in skill


# ---------------------------------------------------------------------------
# 3. domain.yaml loads
# ---------------------------------------------------------------------------

def test_domain_yaml_loads():
    """config/brains/domain.yaml parses via BrainConfig schema."""
    from app.workflow.loader import _resolve_path
    from app.workflow.models import BrainConfig
    import yaml as _yaml

    path = _resolve_path("brains/domain.yaml")
    data = _yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = BrainConfig(**data)
    assert cfg.model == "strong"
    assert cfg.limits.max_iterations >= 20
    # core_tools must include both code-survey tools and dispatch hints
    assert "read_file" in cfg.core_tools
    assert "grep" in cfg.core_tools


# ---------------------------------------------------------------------------
# 4. DomainBrainOrchestrator constructs cleanly
# ---------------------------------------------------------------------------

def test_domain_brain_constructs():
    """Orchestrator init takes the documented args and stores them."""
    from app.agent_loop.domain_brain import DomainBrainOrchestrator

    inner = AsyncMock()
    orch = DomainBrainOrchestrator(
        provider=MagicMock(),
        explorer_provider=MagicMock(),
        workspace_path="/tmp/ws",
        agent_registry={},
        tool_executor=inner,
        query="How does Open Banking work in this system?",
    )
    assert orch._workspace_path == "/tmp/ws"
    assert orch._query.startswith("How does")
    # cleanup is a no-op but must be callable for engine handoff parity
    assert orch.cleanup() is None


# ---------------------------------------------------------------------------
# 5. Coordinator dispatch shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_orchestrator_dispatches_coordinator_as_dynamic_agent():
    """Domain Brain dispatches its coordinator via dispatch_explore (dynamic mode)
    with skill='domain_brain_coordinator' and a tool list that includes
    dispatch_explore (so the coordinator can dispatch workers)."""
    from app.agent_loop.domain_brain import DomainBrainOrchestrator
    from app.agent_loop.brain import AgentToolExecutor

    captured_calls = []

    async def fake_execute(self, tool_name, params):  # bound method signature
        captured_calls.append((tool_name, params))
        from app.code_tools.executor import ToolResult
        return ToolResult(
            tool_name=tool_name,
            success=True,
            data={
                "answer": "synthesized domain answer",
                "tool_calls_made": 5,
                "files_accessed": ["CLAUDE.md", "src/auth.py"],
            },
        )

    # Patch AgentToolExecutor.execute on the instance the orchestrator
    # creates internally. We do this by monkey-patching the class method
    # since the orchestrator builds the executor itself.
    original_execute = AgentToolExecutor.execute
    AgentToolExecutor.execute = fake_execute  # type: ignore[method-assign]
    try:
        orch = DomainBrainOrchestrator(
            provider=MagicMock(),
            explorer_provider=MagicMock(),
            workspace_path="/tmp/ws",
            agent_registry={},
            tool_executor=AsyncMock(),
            query="How does X work?",
        )

        events = []
        async for ev in orch.run_stream():
            events.append(ev)
    finally:
        AgentToolExecutor.execute = original_execute  # type: ignore[method-assign]

    # Validate exactly one dispatch_explore call (the coordinator dispatch)
    dispatch_calls = [c for c in captured_calls if c[0] == "dispatch_explore"]
    assert len(dispatch_calls) == 1
    _, params = dispatch_calls[0]
    # Skill + perspective + tools shape
    assert params["skill"] == "domain_brain_coordinator"
    assert params["model"] == "strong"
    assert "perspective" in params and len(params["perspective"]) > 50
    assert "dispatch_explore" in params["tools"], (
        "Coordinator must have dispatch_explore in its tools so it can dispatch workers"
    )
    # Code-survey tools present so coordinator can do Phase 1 self-survey
    for t in ("read_file", "grep", "list_files", "module_summary"):
        assert t in params["tools"], f"coordinator missing {t}"
    # The user query passes through unchanged
    assert params["query"] == "How does X work?"

    # Event stream must contain start + done with the answer
    kinds = [e.kind for e in events]
    assert "domain_brain_start" in kinds
    assert "done" in kinds
    done = next(e for e in events if e.kind == "done")
    assert done.data["answer"] == "synthesized domain answer"
    assert done.data["workflow"] == "domain_brain"


# ---------------------------------------------------------------------------
# 6. General Brain prompt teaches the handoff
# ---------------------------------------------------------------------------

def test_general_brain_prompt_mentions_domain_handoff():
    """The General Brain's prompt must teach when to transfer to Domain Brain."""
    from app.agent_loop.prompts import build_brain_prompt
    from app.workflow.loader import load_agent_registry, load_swarm_registry

    prompt = build_brain_prompt(
        agent_registry=load_agent_registry(),
        swarm_registry=load_swarm_registry(),
        max_iterations=20,
    )
    # Multi-perspective branch now points to Domain Brain
    assert 'transfer_to_brain("domain")' in prompt
    # Trigger keywords are listed so Brain knows when to hand off
    assert "how does" in prompt.lower() or "end-to-end" in prompt
