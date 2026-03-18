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
    ) -> None:
        self._provider = provider
        self._explorer_provider = explorer_provider or provider
        self._trace_writer = trace_writer

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
        """
        start_time = time.monotonic()
        logger.info("Starting workflow '%s' (route_mode=%s)", workflow.name, workflow.route_mode)
        update_trace(
            metadata={"workflow": workflow.name, "route_mode": workflow.route_mode},
            tags=[workflow.name],
        )

        # Step 1: Classify
        engine = ClassifierEngine(workflow)
        classify_result = engine.classify(context)
        context["_classify_result"] = classify_result

        yield WorkflowEvent("classify", {
            "workflow": workflow.name,
            "result": classify_result.model_dump(),
        })

        # Step 2: Route and execute
        if workflow.route_mode == "first_match":
            async for event in self._run_first_match(workflow, classify_result, context):
                yield event

        elif workflow.route_mode == "parallel_all_matching":
            async for event in self._run_parallel_all_matching(workflow, classify_result, context):
                yield event

        duration_ms = (time.monotonic() - start_time) * 1000
        context["_duration_ms"] = duration_ms

        yield WorkflowEvent("done", {
            "workflow": workflow.name,
            "duration_ms": duration_ms,
        })

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
            route_ctx = dict(context)  # shallow copy for isolation
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
                stage_results[stage.stage] = dict(zip(
                    [a.name for a in agents], results,
                ))
            else:
                # Sequential agent dispatch
                for agent in agents:
                    result = await self._run_agent(agent, workflow, context)
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

        # Create and run agent loop
        svc = AgentLoopService(
            provider=provider,
            max_iterations=max_iterations,
            budget_config=budget_config,
            trace_writer=self._trace_writer,
            _skip_review_delegation=True,
            llm_semaphore=context.get("_llm_semaphore"),
        )

        result: AgentResult = await svc.run(
            query=query,
            workspace_path=workspace_path,
        )

        return {
            "answer": result.answer,
            "context_chunks": result.context_chunks,
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
            response = await loop.run_in_executor(
                None,
                lambda: provider.call_model(
                    prompt=prompt,
                    max_tokens=max_tokens,
                    system=agent.instructions if agent.type == "judge" else None,
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
        """Build the full query string for an explorer agent.

        Composes: shared prompt template + agent instructions + runtime context.
        """
        # For now, return agent instructions as the query.
        # The full template composition (with {agent_instructions}, {diff_spec}, etc.)
        # will be done in A.5 when we integrate with the existing prompt building code.
        instructions = agent.instructions

        # If there's a query in context, prepend it
        query = context.get("query", "")
        if query:
            return f"{query}\n\n{instructions}" if instructions else query

        return instructions or "Review the code changes."

    def _build_judge_prompt(
        self,
        agent: AgentConfig,
        context: Dict[str, Any],
    ) -> str:
        """Build the prompt for a judge agent from context data.

        The actual prompt assembly (injecting findings JSON, diff snippets, etc.)
        will be done in A.5 when we integrate with existing code in service.py.
        """
        # For now, return a placeholder. The real implementation injects
        # findings_json, diff_snippets, pr_context etc. from context.
        return agent.instructions or ""
