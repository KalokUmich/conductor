"""Domain Brain — specialised orchestrator for business / domain logic queries.

Activated via ``transfer_to_brain("domain")`` from the General Brain when the
query asks about end-to-end flows, business features, or "how does X work" in
domain terms.

Design (mirrors PR Brain v2's coordinator-as-tool pattern, but much lighter):

  Phase 1  Coordinator Self-Survey  (~3-6 own tool calls)
           ├─ Read project docs (CLAUDE.md, README) for vocabulary
           ├─ list_files / module_summary to map relevant sub-modules
           ├─ grep domain keywords to anchor on the domain model
           └─ Coordinator builds an internal ScopePlan
  Phase 2  Dispatch  (parallel dispatch_explore calls — DEPTH or BREADTH)
           └─ Workers return prose + a semi-structured JSON envelope so
              synthesis can enumerate without losing fields
  Phase 3  Synthesis (coordinator's final answer)
           └─ 8 rules + 4-section format anchored to domain model

The whole thing is ONE Sonnet coordinator with code-survey tools +
``dispatch_explore``. The coordinator skill (``config/skills/domain_brain_coordinator.md``)
drives the loop — there is no Python state machine.

Unlike PR Brain v2:
- No deterministic pre-compute (no diff to parse — scope is unknown)
- No structured findings JSON post-processing (output is prose narrative)
- No precision filter / verifier loop (no severity classification needed)
- Workers use ``dispatch_explore``, not ``dispatch_verify``, because domain
  questions are open exploration not scope+checks contracts.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Dict, Optional

from app.ai_provider.base import AIProvider
from app.code_tools.executor import ToolExecutor

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------


class WorkflowEvent:
    """Lightweight event container compatible with WorkflowEngine's event queue."""

    def __init__(self, kind: str, data: Dict[str, Any]):
        self.kind = kind
        self.data = data


# Coordinator perspective — Layer 1 identity sentence injected into the
# dispatched coordinator's system prompt alongside the domain_brain_coordinator
# skill. The skill carries the workflow, rules, and output format; this
# perspective sentence says "who you are" in one breath.
_DOMAIN_COORDINATOR_PERSPECTIVE = (
    "You are a domain logic specialist. You read enterprise codebases as "
    "encoded business processes, anchored on domain models (Request / DTO / "
    "Entity classes with composite gates). You self-survey scope before "
    "dispatching workers, choose DEPTH or BREADTH based on the question, and "
    "synthesise with technical-term fidelity (never paraphrase field names, "
    "role names, or enum values)."
)

# Tools the coordinator gets directly. Read-only structural exploration only —
# anything that needs deeper code reading is delegated to workers via
# dispatch_explore. dispatch_explore is included so the coordinator can dispatch
# workers; it lives in BRAIN_TOOL_DEFINITIONS and is recognised by
# AgentToolExecutor.
_COORDINATOR_TOOLS = [
    "read_file",
    "list_files",
    "glob",
    "grep",
    "file_outline",
    "module_summary",
    "find_symbol",
    "find_references",
    "get_dependencies",
    "dispatch_explore",
]


class DomainBrainOrchestrator:
    """Coordinator-driven Brain for domain logic questions.

    Lifecycle:
      1. ``__init__`` — wire providers, executor, agent registry.
      2. ``run_stream`` — yields WorkflowEvent stream; under the hood this
         dispatches ONE Sonnet coordinator (depth 1) with code tools +
         dispatch_explore. The coordinator self-surveys, dispatches workers,
         and synthesises. The coordinator's final answer becomes the
         orchestrator's final answer.

    Public API matches the relevant subset of PRBrainOrchestrator so the
    WorkflowEngine handoff path can treat them uniformly.
    """

    def __init__(
        self,
        provider: AIProvider,                  # strong (Sonnet) — coordinator
        explorer_provider: AIProvider,         # explorer (Haiku) — workers
        workspace_path: str,
        agent_registry: Dict[str, Any],
        tool_executor: ToolExecutor,
        query: str,
        trace_writer=None,
        event_sink: Optional[asyncio.Queue] = None,
        task_id: Optional[str] = None,
    ):
        self._provider = provider
        self._explorer_provider = explorer_provider
        self._workspace_path = workspace_path
        self._agent_registry = agent_registry
        self._tool_executor = tool_executor
        self._query = query
        self._trace_writer = trace_writer
        self._event_sink = event_sink
        self._task_id = task_id

    async def run_stream(self) -> AsyncGenerator[WorkflowEvent, None]:
        """Drive the coordinator loop, yielding progress events.

        The coordinator is dispatched as a dynamic-mode ``dispatch_explore``
        call so it inherits all the existing Brain infrastructure: budget
        tracking, event emission, depth control, sub-agent dispatch wiring.
        """
        from app.workflow.loader import (
            load_brain_config,
            load_swarm_registry,
        )

        from .brain import AgentToolExecutor, BrainBudgetManager
        from .config import BrainExecutorConfig

        start = time.monotonic()
        logger.info("[Domain Brain] Coordinator loop starting: query=%r", self._query[:100])

        yield WorkflowEvent(
            "domain_brain_start",
            {"query": self._query, "workspace_path": self._workspace_path},
        )

        # Load Domain Brain config (limits + core_tools + synthesis caps).
        # We deliberately reuse load_brain_config() with a domain.yaml override
        # rather than introducing a new loader — Domain Brain uses the same
        # BrainConfig schema as the general Brain (limits + core_tools).
        from .config import BrainExecutorConfig as _Cfg  # noqa: F401
        from app.workflow.loader import _resolve_path  # noqa: WPS437 — reuse

        import yaml as _yaml
        from app.workflow.models import BrainConfig as _BrainConfig

        try:
            domain_yaml = _resolve_path("brains/domain.yaml")
            domain_data = _yaml.safe_load(domain_yaml.read_text(encoding="utf-8")) or {}
            domain_config = _BrainConfig(**domain_data)
        except Exception as exc:
            logger.warning(
                "[Domain Brain] failed to load brains/domain.yaml (%s) — "
                "falling back to default Brain config", exc,
            )
            domain_config = load_brain_config()

        budget_mgr = BrainBudgetManager(domain_config.limits.total_session_tokens)

        executor_cfg = BrainExecutorConfig(
            workspace_path=self._workspace_path,
            current_depth=0,
            max_depth=domain_config.limits.max_depth,
            max_concurrent=domain_config.limits.max_concurrent_agents,
            sub_agent_timeout=domain_config.limits.sub_agent_timeout,
        )

        # Domain Brain doesn't use swarm presets — pass empty registry.
        executor = AgentToolExecutor(
            inner_executor=self._tool_executor,
            agent_registry=self._agent_registry,
            swarm_registry={},
            agent_provider=self._explorer_provider,
            strong_provider=self._provider,
            config=executor_cfg,
            brain_config=domain_config,
            trace_writer=self._trace_writer,
            event_sink=self._event_sink,
            budget_manager=budget_mgr,
        )

        # Dispatch the coordinator as a dynamic-mode agent. Mirrors how PR
        # Brain v2 spawns its coordinator (pr_brain.py:_run_v2_coordinator).
        # The coordinator gets:
        #   * skill = "domain_brain_coordinator" → Layer 3 system content
        #     (workflow, 8 rules, 4-section format, depth/breadth rubric)
        #   * tools = code-survey + dispatch_explore
        #   * model = strong (Sonnet) — coordinator needs reasoning depth
        coordinator_params = {
            "perspective": _DOMAIN_COORDINATOR_PERSPECTIVE,
            "skill": "domain_brain_coordinator",
            "tools": _COORDINATOR_TOOLS,
            "model": "strong",
            "max_iterations": domain_config.limits.max_iterations,
            "budget_tokens": domain_config.limits.budget_tokens,
            "query": self._query,
            "budget_weight": 1.0,
        }

        coordinator_result = await executor.execute("dispatch_explore", coordinator_params)

        elapsed_ms = (time.monotonic() - start) * 1000
        logger.info(
            "[Domain Brain] Coordinator loop done in %.0fms: success=%s",
            elapsed_ms,
            coordinator_result.success,
        )

        # Surface the coordinator's answer as the final Brain answer. The
        # `done` event shape mirrors what WorkflowEngine.run_brain_stream
        # would produce in the General Brain path — same SSE consumer code
        # works for both.
        if not coordinator_result.success:
            yield WorkflowEvent(
                "error",
                {"error": coordinator_result.error or "Domain Brain coordinator failed"},
            )
            return

        data = coordinator_result.data or {}
        answer = data.get("answer") or data.get("final_answer") or ""
        tool_calls_made = data.get("tool_calls_made", 0)
        files_accessed = data.get("files_accessed", [])

        yield WorkflowEvent(
            "done",
            {
                "answer": answer,
                "tool_calls_made": tool_calls_made,
                "files_accessed": files_accessed,
                "duration_ms": elapsed_ms,
                "workflow": "domain_brain",
            },
        )

    def cleanup(self) -> None:
        """No-op — Domain Brain holds no per-session state.

        Provided for interface parity with PRBrainOrchestrator so the
        WorkflowEngine handoff path can call cleanup uniformly.
        """
        return None
