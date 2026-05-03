"""Workflow execution engine.

Hosts the Brain orchestrator entry point used by the streaming context
endpoint. Brain (strong model) decides which specialist sub-agents to
dispatch via ``dispatch_explore`` / ``transfer_to_brain`` /
``transfer_to_brain``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Dict, Optional

from app.agent_loop.budget import BudgetConfig
from app.agent_loop.service import AgentLoopService
from app.ai_provider.base import AIProvider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Agent event (for SSE streaming)
# ---------------------------------------------------------------------------


class WorkflowEvent:
    """A progress event emitted during workflow execution."""

    def __init__(self, kind: str, data: Dict[str, Any]) -> None:
        self.kind = kind
        self.data = data

    def __repr__(self) -> str:
        return f"WorkflowEvent(kind={self.kind!r}, data=...)"


# ---------------------------------------------------------------------------
# WorkflowEngine
# ---------------------------------------------------------------------------


class WorkflowEngine:
    """Host the Brain orchestrator entry point.

    Args:
        provider: Strong AI provider (used for the Brain itself).
        explorer_provider: Lightweight provider for sub-agents dispatched
            by Brain.  Falls back to ``provider`` if not supplied.
        trace_writer: Optional trace persistence (passed to AgentLoopService).
        tool_executor: Optional pre-built ToolExecutor (e.g. RemoteToolExecutor
            for local-mode workspaces).  Falls back to ``LocalToolExecutor``.
        interactive: Reserved for future use; currently unused on the Brain
            path because Brain always runs interactive.
    """

    def __init__(
        self,
        provider: AIProvider,
        explorer_provider: Optional[AIProvider] = None,
        trace_writer=None,
        tool_executor=None,
        interactive: bool = False,
    ) -> None:
        self._provider = provider
        self._explorer_provider = explorer_provider or provider
        self._trace_writer = trace_writer
        self._tool_executor = tool_executor
        self._interactive = interactive
        self._event_queue: Optional[asyncio.Queue] = None

    # -----------------------------------------------------------------
    # Brain mode — LLM orchestrator
    # -----------------------------------------------------------------

    async def run_brain_stream(
        self,
        context: Dict[str, Any],
    ) -> AsyncGenerator[WorkflowEvent, None]:
        """Execute a query using the Brain orchestrator.

        Brain replaces the classifier + route pipeline. It uses LLM
        intelligence to understand queries, dispatch specialist agents
        via ``dispatch_explore`` / ``transfer_to_brain`` tools, evaluate
        findings, and synthesize comprehensive answers.

        Args:
            context: Must include ``query`` and ``workspace_path``.
        """
        from app.agent_loop.brain import AgentToolExecutor, BrainBudgetManager
        from app.agent_loop.prompts import build_brain_prompt
        from app.code_tools.executor import LocalToolExecutor

        from .loader import load_agent_registry, load_brain_config, load_swarm_registry

        start_time = time.monotonic()
        brain_config = load_brain_config()
        agent_registry = load_agent_registry()
        swarm_registry = load_swarm_registry()

        logger.info(
            "Starting Brain orchestrator (agents=%d, swarms=%d, max_iter=%d)",
            len(agent_registry),
            len(swarm_registry),
            brain_config.limits.max_iterations,
        )

        # Set up event queue for streaming
        self._event_queue = asyncio.Queue()

        # Build Brain's system prompt
        qa_cache: Dict[str, str] = {}
        brain_prompt = build_brain_prompt(
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            max_iterations=brain_config.limits.max_iterations,
            qa_cache=qa_cache,
        )

        # Budget manager
        budget_mgr = BrainBudgetManager(brain_config.limits.total_session_tokens)

        # Build the Brain's tool executor
        workspace_path = context.get("workspace_path", "")
        inner_executor = self._tool_executor or LocalToolExecutor(workspace_path)

        from app.agent_loop.config import BrainExecutorConfig

        brain_executor = AgentToolExecutor(
            inner_executor=inner_executor,
            agent_registry=agent_registry,
            swarm_registry=swarm_registry,
            agent_provider=self._explorer_provider,
            strong_provider=self._provider,
            config=BrainExecutorConfig(
                workspace_path=workspace_path,
                current_depth=0,
                max_depth=brain_config.limits.max_depth,
                max_concurrent=brain_config.limits.max_concurrent_agents,
                sub_agent_timeout=brain_config.limits.sub_agent_timeout,
            ),
            brain_config=brain_config,
            trace_writer=self._trace_writer,
            event_sink=self._event_queue,
            budget_manager=budget_mgr,
            qa_cache=qa_cache,
        )

        query = context.get("query") or context.get("query_text", "")
        code_context = context.get("code_context")

        # Propagate code_context so sub-agents can include the snippet
        brain_executor._code_context = code_context

        # Create Brain agent loop
        from app.agent_loop.config import AgentLoopConfig

        brain = AgentLoopService(
            provider=self._provider,  # strong model for Brain
            config=AgentLoopConfig(
                max_iterations=brain_config.limits.max_iterations,
                budget_config=BudgetConfig(max_input_tokens=brain_config.limits.budget_tokens),
                interactive=True,
                is_brain=True,
                brain_system_prompt=brain_prompt,
            ),
            tool_executor=brain_executor,
            trace_writer=self._trace_writer,
        )

        # Run Brain in background task, drain events from queue
        async def _execute():
            try:
                async for event in brain.run_stream(query, workspace_path, code_context=code_context):
                    if event.kind == "transfer":
                        # Hand off to specialized brain (one-way)
                        await self._run_specialized_brain(event.data, context)
                        return
                    await self._event_queue.put(WorkflowEvent(event.kind, event.data))
            except Exception as exc:
                logger.error("Brain failed: %s", exc, exc_info=True)
                await self._event_queue.put(WorkflowEvent("error", {"error": str(exc)}))
            await self._event_queue.put(None)  # sentinel

        task = asyncio.create_task(_execute())

        # Drain and yield events with keepalive heartbeat.
        # During long LLM calls (Brain synthesis), the queue may be empty for
        # 20-30s.  Proxies (ngrok, CDNs) can drop idle SSE connections before
        # the response arrives.  A periodic SSE comment keeps the connection alive.
        _HEARTBEAT_INTERVAL = 15  # seconds
        while True:
            try:
                event = await asyncio.wait_for(
                    self._event_queue.get(),
                    timeout=_HEARTBEAT_INTERVAL,
                )
            except TimeoutError:
                # Yield an SSE comment as keepalive (invisible to EventSource clients)
                yield WorkflowEvent("keepalive", {})
                continue
            if event is None:
                break
            yield event

        await task

        duration_ms = (time.monotonic() - start_time) * 1000
        yield WorkflowEvent(
            "done",
            {
                "workflow": "brain",
                "duration_ms": duration_ms,
            },
        )

        self._event_queue = None

    async def _run_specialized_brain(
        self,
        transfer_data: Dict[str, Any],
        context: Dict[str, Any],
    ) -> None:
        """Launch a specialized brain after a transfer_to_brain handoff.

        The specialized brain streams events into the same event queue,
        so they propagate to the client via SSE.
        """
        brain_name = transfer_data.get("brain", "")
        params = transfer_data.get("params", {})

        if brain_name == "pr_review":
            from app.agent_loop.pr_brain import PRBrainOrchestrator
            from app.code_tools.executor import LocalToolExecutor

            from .loader import load_agent_registry, load_pr_brain_config

            workspace_path = params.get("workspace_path", context.get("workspace_path", ""))
            diff_spec = params.get("diff_spec", "HEAD~1..HEAD")
            # Prefer caller-supplied task_id; fall back to room_id or
            # session_id so the scratchpad filename still traces back to the
            # chat session when the source didn't set one explicitly.
            task_id = (
                params.get("task_id")
                or context.get("room_id")
                or context.get("session_id")
            )

            orchestrator = PRBrainOrchestrator(
                provider=self._provider,
                explorer_provider=self._explorer_provider,
                workspace_path=workspace_path,
                diff_spec=diff_spec,
                pr_brain_config=load_pr_brain_config(),
                agent_registry=load_agent_registry(),
                tool_executor=self._tool_executor or LocalToolExecutor(workspace_path),
                trace_writer=self._trace_writer,
                event_sink=self._event_queue,
                task_id=task_id,
            )

            try:
                async for event in orchestrator.run_stream():
                    await self._event_queue.put(event)
            except Exception as exc:
                logger.error("PR Brain failed: %s", exc, exc_info=True)
                await self._event_queue.put(WorkflowEvent("error", {"error": f"PR Brain failed: {exc}"}))
            finally:
                # Phase 9.15 — release the session-scoped Fact Vault.
                # No-op when the orchestrator doesn't own one (caller passed a vault).
                orchestrator.cleanup()
        elif brain_name == "domain":
            # Domain Brain — coordinator self-surveys + dispatches workers
            # for end-to-end / business-flow questions. See
            # DomainBrainOrchestrator for the Phase 1 / 2 / 3 design.
            from app.agent_loop.domain_brain import DomainBrainOrchestrator
            from app.code_tools.executor import LocalToolExecutor

            from .loader import load_agent_registry

            # Domain Brain workspace_path: ALWAYS prefer the engine's own
            # workspace context over what the LLM-emitted transfer call
            # provided. Brains have been observed to pass placeholder strings
            # ("/path/to/ws") or empty strings; trusting those leaves the
            # downstream agent loop's _read_key_docs() walking from the
            # filesystem root and crashing on /lost+found/README.md.
            workspace_path = (
                context.get("workspace_path")
                or params.get("workspace_path")
                or ""
            )
            query = (
                params.get("query")
                or context.get("query")
                or context.get("query_text", "")
            )
            task_id = (
                params.get("task_id")
                or context.get("room_id")
                or context.get("session_id")
            )

            orchestrator = DomainBrainOrchestrator(
                provider=self._provider,
                explorer_provider=self._explorer_provider,
                workspace_path=workspace_path,
                agent_registry=load_agent_registry(),
                tool_executor=self._tool_executor or LocalToolExecutor(workspace_path),
                query=query,
                trace_writer=self._trace_writer,
                event_sink=self._event_queue,
                task_id=task_id,
            )

            try:
                async for event in orchestrator.run_stream():
                    await self._event_queue.put(event)
            except Exception as exc:
                logger.error("Domain Brain failed: %s", exc, exc_info=True)
                await self._event_queue.put(WorkflowEvent("error", {"error": f"Domain Brain failed: {exc}"}))
            finally:
                orchestrator.cleanup()
        else:
            await self._event_queue.put(WorkflowEvent("error", {"error": f"Unknown specialized brain: {brain_name}"}))

        await self._event_queue.put(None)  # sentinel
