"""Workflow execution engine.

Executes config-driven workflows by:
  1. Running the classifier to determine active routes
  2. Dispatching agents (explorer or judge) per route pipeline
  3. Collecting results through sequential stages
  4. Running post_pipeline stages (for parallel_all_matching mode)

The engine does NOT own agent execution logic — it delegates to
AgentLoopService (explorers) and provider.call_model() (judges).
"""
from __future__ import annotations

import asyncio
import copy
import logging
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

from app.agent_loop.budget import BudgetConfig
from app.agent_loop.service import AgentLoopService, AgentResult
from app.ai_provider.base import AIProvider
from app.code_tools.schemas import filter_tools

from .classifier_engine import ClassifierEngine
from .models import (
    AgentConfig,
    ClassifierResult,
    RouteConfig,
    StageConfig,
    WorkflowConfig,
)
from .observability import observe, update_trace

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
    """Execute config-driven workflows.

    Args:
        provider: Strong/main AI provider (used for judge agents and synthesis).
        explorer_provider: Lightweight provider for explorer agents.
            Falls back to ``provider`` if not supplied.
        trace_writer: Optional trace persistence (passed to AgentLoopService).
    """

    def __init__(
        self,
        provider: AIProvider,
        explorer_provider: Optional[AIProvider] = None,
        trace_writer=None,
        tool_executor=None,
        classifier_provider: Optional[AIProvider] = None,
        interactive: bool = False,
    ) -> None:
        self._provider = provider
        self._explorer_provider = explorer_provider or provider
        self._trace_writer = trace_writer
        self._tool_executor = tool_executor
        self._classifier_provider = classifier_provider
        self._interactive = interactive
        self._event_queue: Optional[asyncio.Queue] = None

    # -----------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------

    async def run(
        self,
        workflow: WorkflowConfig,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a workflow synchronously, returning aggregated results.

        Args:
            workflow: Loaded WorkflowConfig.
            context: Runtime context (signals for classifier + data for agents).
                For PR review: file_paths, changed_lines, workspace_path, diff_spec, ...
                For code explorer: query_text, workspace_path, ...

        Returns:
            Updated context dict with results from all stages.
        """
        async for _ in self.run_stream(workflow, context):
            pass  # consume all events
        return context

    async def run_stream(
        self,
        workflow: WorkflowConfig,
        context: Dict[str, Any],
    ) -> AsyncGenerator[WorkflowEvent, None]:
        """Execute a workflow, yielding progress events.

        This is the primary execution method. ``run()`` is a convenience
        wrapper that discards events.

        Agent-level events (thinking, tool_call, tool_result) are collected
        via an internal queue and yielded alongside workflow-level events,
        enabling real-time UI updates during agent execution.
        """
        start_time = time.monotonic()
        logger.info("Starting workflow '%s' (route_mode=%s)", workflow.name, workflow.route_mode)
        update_trace(
            metadata={"workflow": workflow.name, "route_mode": workflow.route_mode},
            tags=[workflow.name],
        )

        # Set up event queue for agent-level streaming
        self._event_queue = asyncio.Queue()

        # Step 1: Classify — LLM with examples first, keyword fallback
        classifier = ClassifierEngine(workflow)
        classify_result = classifier.classify(context)
        keyword_route = classify_result.best_route
        keyword_score = max(classify_result.raw_scores.values()) if classify_result.raw_scores else 0

        # If LLM classifier is available and route config has examples,
        # use example-based LLM classification (more accurate than keywords).
        # Only skip if keyword already matched strongly (score >= 3).
        if self._classifier_provider is not None and classifier.has_examples() and keyword_score < 3:
            query_text = context.get("query_text") or context.get("query", "")
            llm_route = await classifier.classify_with_llm(query_text, self._classifier_provider)
            if llm_route:
                classify_result.best_route = llm_route
                logger.info(
                    "LLM classifier (examples): keyword=%s(score=%s) → llm=%s",
                    keyword_route, keyword_score, llm_route,
                )

        context["_classify_result"] = classify_result

        yield WorkflowEvent("classify", {
            "workflow": workflow.name,
            "result": classify_result.model_dump(),
        })

        # Step 2: Route and execute — run in background task so we can
        # drain the event queue while agents are working
        async def _execute():
            if workflow.route_mode == "first_match":
                # Single-agent route: allow interactive clarification
                context["_interactive"] = self._interactive
                async for event in self._run_first_match(workflow, classify_result, context):
                    await self._event_queue.put(event)
            elif workflow.route_mode == "parallel_all_matching":
                # Multi-agent parallel: no interactive (agents can't ask user)
                context["_interactive"] = False
                async for event in self._run_parallel_all_matching(workflow, classify_result, context):
                    await self._event_queue.put(event)
            # Sentinel to signal completion
            await self._event_queue.put(None)

        task = asyncio.create_task(_execute())

        # Drain the event queue, yielding events as they arrive
        while True:
            event = await self._event_queue.get()
            if event is None:
                break
            yield event

        # Ensure the task completes (propagate exceptions)
        await task

        duration_ms = (time.monotonic() - start_time) * 1000
        context["_duration_ms"] = duration_ms

        yield WorkflowEvent("done", {
            "workflow": workflow.name,
            "duration_ms": duration_ms,
        })

        self._event_queue = None

    # -----------------------------------------------------------------
    # Brain mode — LLM orchestrator replaces classifier + pipeline
    # -----------------------------------------------------------------

    async def run_brain_stream(
        self,
        context: Dict[str, Any],
    ) -> AsyncGenerator[WorkflowEvent, None]:
        """Execute a query using the Brain orchestrator.

        Brain replaces the classifier + route pipeline. It uses LLM
        intelligence to understand queries, dispatch specialist agents
        via ``dispatch_agent`` / ``dispatch_swarm`` tools, evaluate
        findings, and synthesize comprehensive answers.

        Args:
            context: Must include ``query`` and ``workspace_path``.
        """
        from .loader import load_brain_config, load_agent_registry, load_swarm_registry
        from app.agent_loop.brain import AgentToolExecutor, BrainBudgetManager
        from app.agent_loop.prompts import build_brain_prompt
        from app.agent_loop.service import AgentLoopService
        from app.agent_loop.budget import BudgetConfig
        from app.code_tools.executor import LocalToolExecutor

        start_time = time.monotonic()
        brain_config = load_brain_config()
        agent_registry = load_agent_registry()
        swarm_registry = load_swarm_registry()

        logger.info(
            "Starting Brain orchestrator (agents=%d, swarms=%d, max_iter=%d)",
            len(agent_registry), len(swarm_registry), brain_config.limits.max_iterations,
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
                    await self._event_queue.put(
                        WorkflowEvent(event.kind, event.data)
                    )
            except Exception as exc:
                logger.error("Brain failed: %s", exc, exc_info=True)
                await self._event_queue.put(
                    WorkflowEvent("error", {"error": str(exc)})
                )
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
                    self._event_queue.get(), timeout=_HEARTBEAT_INTERVAL,
                )
            except asyncio.TimeoutError:
                # Yield an SSE comment as keepalive (invisible to EventSource clients)
                yield WorkflowEvent("keepalive", {})
                continue
            if event is None:
                break
            yield event

        await task

        duration_ms = (time.monotonic() - start_time) * 1000
        yield WorkflowEvent("done", {
            "workflow": "brain",
            "duration_ms": duration_ms,
        })

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
            from .loader import load_pr_brain_config, load_agent_registry
            from app.code_tools.executor import LocalToolExecutor

            workspace_path = params.get("workspace_path", context.get("workspace_path", ""))
            diff_spec = params.get("diff_spec", "HEAD~1..HEAD")

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
            )

            try:
                async for event in orchestrator.run_stream():
                    await self._event_queue.put(event)
            except Exception as exc:
                logger.error("PR Brain failed: %s", exc, exc_info=True)
                await self._event_queue.put(
                    WorkflowEvent("error", {"error": f"PR Brain failed: {exc}"})
                )
        else:
            await self._event_queue.put(
                WorkflowEvent("error", {"error": f"Unknown specialized brain: {brain_name}"})
            )

        await self._event_queue.put(None)  # sentinel

    # -----------------------------------------------------------------
    # first_match mode (Code Explorer)
    # -----------------------------------------------------------------

    async def _run_first_match(
        self,
        workflow: WorkflowConfig,
        classify_result: ClassifierResult,
        context: Dict[str, Any],
    ) -> AsyncGenerator[WorkflowEvent, None]:
        """Execute the best-matching route's pipeline."""
        if not workflow.routes:
            logger.error("Workflow '%s' has no routes", workflow.name)
            return

        route_name = classify_result.best_route
        if not route_name or route_name not in workflow.routes:
            logger.warning("No matching route found, using first route")
            route_name = next(iter(workflow.routes))

        route = workflow.routes[route_name]
        context["_active_route"] = route_name

        yield WorkflowEvent("route_selected", {
            "route": route_name,
            "mode": "first_match",
        })

        # Handle delegate
        if route.delegate:
            yield WorkflowEvent("delegate", {
                "route": route_name,
                "delegate_to": route.delegate,
            })
            # Delegate execution is handled by the caller (service layer)
            # which loads the delegate workflow and calls engine.run() again.
            context["_delegate"] = route.delegate
            return

        # Execute the route's pipeline
        async for event in self._run_pipeline(
            route.pipeline, workflow, context, f"route:{route_name}",
        ):
            yield event

    # -----------------------------------------------------------------
    # parallel_all_matching mode (PR Review)
    # -----------------------------------------------------------------

    async def _run_parallel_all_matching(
        self,
        workflow: WorkflowConfig,
        classify_result: ClassifierResult,
        context: Dict[str, Any],
    ) -> AsyncGenerator[WorkflowEvent, None]:
        """Execute all matching routes in parallel, then post_pipeline."""
        # Determine which routes to activate
        active_routes: List[str] = []
        for route_name, route in workflow.routes.items():
            level = classify_result.matched_routes.get(route_name, "low")
            # Check if any agent in this route has always=True trigger
            has_always = False
            for stage in route.pipeline:
                for agent_path in stage.agents:
                    agent = workflow.resolved_agents.get(agent_path)
                    if agent and agent.trigger.always:
                        has_always = True
                        break

            if level in ("medium", "high", "critical") or has_always:
                active_routes.append(route_name)

        context["_active_routes"] = active_routes

        yield WorkflowEvent("routes_selected", {
            "routes": active_routes,
            "mode": "parallel_all_matching",
            "all_levels": classify_result.matched_routes,
        })

        if not active_routes:
            logger.warning("No routes activated for workflow '%s'", workflow.name)
            return

        # Execute all active routes in parallel
        # Collect events from all routes and yield them
        llm_semaphore = asyncio.Semaphore(2)
        context["_llm_semaphore"] = llm_semaphore

        route_results = {}

        async def _run_one_route(rname: str) -> None:
            route = workflow.routes[rname]
            # Deep copy for proper isolation — shared mutables (like lists/dicts)
            # in context could be corrupted by concurrent routes.
            # Preserve _llm_semaphore (asyncio.Semaphore cannot be deepcopied).
            semaphore = context.get("_llm_semaphore")
            route_ctx = copy.deepcopy(context)
            if semaphore is not None:
                route_ctx["_llm_semaphore"] = semaphore
            route_ctx["_route_name"] = rname
            async for event in self._run_pipeline(
                route.pipeline, workflow, route_ctx, f"route:{rname}",
            ):
                pass  # events from parallel routes are not yielded (too noisy)
            route_results[rname] = route_ctx.get("_stage_results", {})

        # Dispatch all routes concurrently
        yield WorkflowEvent("parallel_dispatch", {
            "routes": active_routes,
            "count": len(active_routes),
        })

        await asyncio.gather(*[_run_one_route(rn) for rn in active_routes])

        # Merge route results into main context
        context["_route_results"] = route_results

        yield WorkflowEvent("parallel_complete", {
            "routes": active_routes,
        })

        # Execute post_pipeline (arbitrate, synthesize, etc.)
        if workflow.post_pipeline:
            async for event in self._run_pipeline(
                workflow.post_pipeline, workflow, context, "post_pipeline",
            ):
                yield event

    # -----------------------------------------------------------------
    # Pipeline execution (shared between modes)
    # -----------------------------------------------------------------

    async def _run_pipeline(
        self,
        stages: List[StageConfig],
        workflow: WorkflowConfig,
        context: Dict[str, Any],
        pipeline_name: str,
    ) -> AsyncGenerator[WorkflowEvent, None]:
        """Execute a sequence of stages."""
        stage_results = context.setdefault("_stage_results", {})

        for stage in stages:
            yield WorkflowEvent("stage_start", {
                "stage": stage.stage,
                "pipeline": pipeline_name,
                "parallel": stage.parallel,
                "agent_count": len(stage.agents),
            })

            agents = [
                workflow.resolved_agents[path]
                for path in stage.agents
                if path in workflow.resolved_agents
            ]

            if stage.parallel and len(agents) > 1:
                # Parallel agent dispatch
                results = await asyncio.gather(*[
                    self._run_agent(agent, workflow, context)
                    for agent in agents
                ])
                for agent, result in zip(agents, results):
                    if result.get("error"):
                        logger.warning("Agent '%s' failed: %s", agent.name, result["error"])
                stage_results[stage.stage] = dict(zip(
                    [a.name for a in agents], results,
                ))
            else:
                # Sequential agent dispatch
                for agent in agents:
                    result = await self._run_agent(agent, workflow, context)
                    if result.get("error"):
                        logger.warning("Agent '%s' failed: %s", agent.name, result["error"])
                    stage_results[stage.stage] = {agent.name: result}

            yield WorkflowEvent("stage_complete", {
                "stage": stage.stage,
                "pipeline": pipeline_name,
            })

    # -----------------------------------------------------------------
    # Agent execution
    # -----------------------------------------------------------------

    @observe(name="agent")
    async def _run_agent(
        self,
        agent: AgentConfig,
        workflow: WorkflowConfig,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Execute a single agent (explorer or judge).

        Returns:
            Dict with agent results (answer, findings, tokens, etc.).
        """
        logger.info("Running agent '%s' (type=%s)", agent.name, agent.type)
        update_trace(metadata={"agent_name": agent.name, "agent_type": agent.type})

        if agent.type == "explorer":
            return await self._run_explorer(agent, workflow, context)
        elif agent.type == "judge":
            return await self._run_judge(agent, workflow, context)
        else:
            raise ValueError(f"Unknown agent type: {agent.type}")

    async def _run_explorer(
        self,
        agent: AgentConfig,
        workflow: WorkflowConfig,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run an explorer agent using AgentLoopService."""
        workspace_path = context.get("workspace_path", "")

        # Compute budget
        budget = workflow.budget
        weight = agent.budget_weight
        budget_tokens = int(budget.base_tokens * budget.sub_fraction * weight)
        max_iterations = max(
            int(budget.base_iterations * budget.sub_fraction * weight),
            budget.min_iterations,
        )

        # Apply size multiplier if available
        multiplier = context.get("_budget_multiplier", 1.0)
        budget_tokens = int(budget_tokens * multiplier)
        max_iterations = min(int(max_iterations * multiplier), 40)

        budget_config = BudgetConfig(
            max_input_tokens=budget_tokens,
            max_iterations=max_iterations,
        )

        # Resolve provider based on model_role
        provider = self._resolve_provider(agent.model_role)

        # Build query from prompt template + agent instructions
        query = self._build_agent_query(agent, workflow, context)

        # Resolve tools
        tool_defs = filter_tools(agent.tools.extra) if agent.tools.extra else None

        # Pass the workflow route name so the agent uses the correct classification
        # (e.g. "business_flow_tracing") instead of re-classifying independently.
        route_name = context.get("_active_route") or context.get("_route_name", "")

        # Create and run agent loop — use run_stream to collect events for UI.
        # NOTE: workflow_config takes priority over _is_sub_agent
        # in service.py classification logic. When both are set,
        # _is_sub_agent wins (checked first), bypassing the
        # workflow-driven classification. So we only set _is_sub_agent
        # when there's NO workflow route to use.
        use_workflow_classification = bool(route_name)
        from app.agent_loop.config import AgentLoopConfig
        svc = AgentLoopService(
            provider=provider,
            config=AgentLoopConfig(
                max_iterations=max_iterations,
                budget_config=budget_config,
                is_sub_agent=not use_workflow_classification,
                workflow_config=agent if use_workflow_classification else None,
                workflow_route_name=route_name,
                perspective=agent.instructions,     # agent role for scoped verification
                interactive=context.get("_interactive", False),
                agent_identity={
                    "name": agent.name,
                    "description": getattr(agent, "description", "") or "",
                    "instructions": agent.instructions or "",
                },
            ),
            trace_writer=self._trace_writer,
            llm_semaphore=context.get("_llm_semaphore"),
            tool_executor=self._tool_executor,
            verifier_provider=self._provider,  # strong model for completeness check
        )

        # Stream agent events to UI in real-time (required for ask_user)
        collected_events: list = []
        result: Optional[AgentResult] = None

        async for event in svc.run_stream(
            query=query,
            workspace_path=workspace_path,
            code_context=context.get("code_context"),
        ):
            collected_events.append(event)

            # Forward events immediately so ask_user reaches the client
            # before the generator pauses waiting for the user's answer.
            if self._event_queue is not None:
                await self._event_queue.put(
                    WorkflowEvent(event.kind, {"agent": agent.name, **event.data})
                )

            if event.kind == "done":
                result = AgentResult(
                    answer=event.data.get("answer", ""),
                    context_chunks=event.data.get("context_chunks", []),
                    thinking_steps=event.data.get("thinking_steps", []),
                    tool_calls_made=event.data.get("tool_calls_made", 0),
                    iterations=event.data.get("iterations", 0),
                    duration_ms=event.data.get("duration_ms", 0),
                    budget_summary=event.data.get("budget_summary"),
                )

        if result is None:
            return {"answer": "", "error": "Agent produced no result", "context_chunks": []}

        return {
            "answer": result.answer,
            "context_chunks": result.context_chunks,
            "thinking_steps": result.thinking_steps,
            "tool_calls_made": result.tool_calls_made,
            "iterations": result.iterations,
            "tokens_input": result.budget_summary.get("total_input_tokens", 0) if result.budget_summary else 0,
            "tokens_output": result.budget_summary.get("total_output_tokens", 0) if result.budget_summary else 0,
            "duration_ms": result.duration_ms,
            "error": result.error,
        }

    async def _run_judge(
        self,
        agent: AgentConfig,
        workflow: WorkflowConfig,
        context: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Run a judge agent (single LLM call, no tools)."""
        provider = self._resolve_provider(agent.model_role)

        # Build prompt from agent instructions + context data
        prompt = self._build_judge_prompt(agent, context)
        max_tokens = agent.max_tokens or 4096

        try:
            loop = asyncio.get_event_loop()
            # prompt already includes agent.instructions + evidence,
            # so we don't also pass instructions as system to avoid duplication.
            response = await loop.run_in_executor(
                None,
                lambda: provider.call_model(
                    prompt=prompt,
                    max_tokens=max_tokens,
                ),
            )
            return {
                "answer": response,
                "error": None,
            }
        except Exception as exc:
            logger.warning("Judge agent '%s' failed: %s", agent.name, exc)
            return {
                "answer": "",
                "error": str(exc),
            }

    # -----------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------

    def _resolve_provider(self, model_role: str) -> AIProvider:
        """Resolve a model role to an AI provider."""
        if model_role == "strong":
            return self._provider
        elif model_role in ("explorer", "classifier"):
            return self._explorer_provider
        else:
            return self._provider

    def _build_agent_query(
        self,
        agent: AgentConfig,
        workflow: WorkflowConfig,
        context: Dict[str, Any],
    ) -> str:
        """Build the query string for an explorer agent (Layer 4 only).

        Returns just the user's question — agent identity is now in the
        system prompt (Layer 1) via agent_identity, not in the user message.
        """
        return context.get("query", "") or "Review the code changes."

    def _build_judge_prompt(
        self,
        agent: AgentConfig,
        context: Dict[str, Any],
    ) -> str:
        """Build the prompt for a judge agent from context data.

        Assembles the agent's instructions (markdown template) with actual
        evidence collected by previous stages. The agent's ``input`` field
        declares which context keys it expects (e.g. query, perspective_answers,
        raw_evidence, findings).
        """
        parts: list[str] = []

        # 1. Agent instructions (the markdown template)
        if agent.instructions:
            parts.append(agent.instructions)

        # 2. The original query
        query = context.get("query") or context.get("query_text") or ""
        if query:
            parts.append(f"\n## Question\n\n{query}")

        # 3. Inject evidence from previous stages
        stage_results = context.get("_stage_results", {})

        # Collect perspective answers and raw evidence from explore/investigate stages
        perspective_answers: list[str] = []
        raw_evidence: list[str] = []

        for stage_name, agents_dict in stage_results.items():
            if stage_name in ("synthesize", "arbitrate"):
                continue  # skip synthesis stages
            if not isinstance(agents_dict, dict):
                continue
            for agent_name, result in agents_dict.items():
                if not isinstance(result, dict):
                    continue
                answer = result.get("answer", "")
                if answer:
                    perspective_answers.append(
                        f"### Agent: {agent_name}\n\n{answer}"
                    )
                # Collect context_chunks as raw evidence
                chunks = result.get("context_chunks", [])
                for chunk in chunks:
                    if hasattr(chunk, 'file_path'):
                        raw_evidence.append(
                            f"**{chunk.file_path}:{chunk.start_line}-{chunk.end_line}**\n```\n{chunk.content}\n```"
                        )
                    elif isinstance(chunk, dict) and "file_path" in chunk:
                        raw_evidence.append(
                            f"**{chunk['file_path']}:{chunk.get('start_line', '?')}-{chunk.get('end_line', '?')}**\n```\n{chunk.get('content', '')}\n```"
                        )

        if perspective_answers:
            parts.append("\n## Perspective Answers\n")
            parts.append("\n\n---\n\n".join(perspective_answers))

        if raw_evidence:
            parts.append("\n## Raw Evidence\n")
            # Limit to avoid token explosion
            parts.append("\n\n".join(raw_evidence[:30]))
            if len(raw_evidence) > 30:
                parts.append(f"\n... and {len(raw_evidence) - 30} more evidence blocks")

        # For PR review: collect findings from route results
        route_results = context.get("_route_results", {})
        if route_results:
            findings_parts = []
            for route_name, stages in route_results.items():
                if not isinstance(stages, dict):
                    continue
                for stage_name, agents_dict in stages.items():
                    if not isinstance(agents_dict, dict):
                        continue
                    for agent_name, result in agents_dict.items():
                        if isinstance(result, dict) and result.get("answer"):
                            findings_parts.append(f"### {agent_name}\n\n{result['answer']}")
            if findings_parts:
                parts.append("\n## Review Findings\n")
                parts.append("\n\n---\n\n".join(findings_parts))

        # Inject code snippet context if available
        code_ctx = context.get("code_context")
        if code_ctx and isinstance(code_ctx, dict):
            lang = code_ctx.get("language", "")
            parts.append(
                f"\n## Code Under Discussion\n\n"
                f"`{code_ctx['file_path']}` "
                f"(lines {code_ctx.get('start_line', '?')}\u2013{code_ctx.get('end_line', '?')}):\n\n"
                f"```{lang}\n{code_ctx['code']}\n```"
            )

        # Inject diff_snippets if available
        diff_snippets = context.get("diff_snippets", "")
        if diff_snippets:
            parts.append(f"\n## Code Diff\n\n```diff\n{diff_snippets}\n```")

        # Inject other PR context
        for key in ("pr_context", "risk_profile"):
            value = context.get(key)
            if value:
                parts.append(f"\n## {key.replace('_', ' ').title()}\n\n{value}")

        return "\n\n".join(parts)
