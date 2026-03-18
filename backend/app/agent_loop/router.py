"""Agent loop router — replaces the old hybrid retrieval context endpoint.

Provides:
  POST /api/context/query        — run an agent loop to answer a code question
  POST /api/context/explain-rich — agentic code explanation (replaces XML-prompt pipeline)
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .service import AgentEvent, AgentLoopService, AgentResult
from .budget import BudgetConfig

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/context", tags=["context"])


# ---------------------------------------------------------------------------
# Workflow-driven routing
# ---------------------------------------------------------------------------

_workflow_cache = None


def _get_code_explorer_workflow():
    """Load and cache the code-explorer workflow config."""
    global _workflow_cache
    if _workflow_cache is None:
        try:
            from app.workflow.loader import load_workflow
            _workflow_cache = load_workflow("workflows/code_explorer.yaml")
            logger.info("Loaded code-explorer workflow for query routing (%d routes)", len(_workflow_cache.routes))
        except Exception as exc:
            logger.warning("Could not load code-explorer workflow, falling back to direct agent: %s", exc)
    return _workflow_cache


async def _classify_and_route(query: str, classifier_provider=None):
    """Classify a query using the code-explorer workflow.

    Strategy:
      1. Keyword patterns first (zero cost, instant)
      2. If keyword match is weak (score <= 1), fall back to LLM classifier
      3. PR/code-review queries always match on keywords (no LLM needed)

    Returns (route_name, route_config, agent_config) or (None, None, None) for fallback.
    """
    wf = _get_code_explorer_workflow()
    if wf is None:
        return None, None, None

    from app.workflow.classifier_engine import ClassifierEngine
    engine = ClassifierEngine(wf)
    result = engine.classify({"query_text": query})

    route_name = result.best_route
    best_score = max(result.raw_scores.values()) if result.raw_scores else 0

    # If keyword match is weak and we have an LLM classifier, use it
    if best_score <= 1 and classifier_provider is not None:
        try:
            from .query_classifier import classify_query_with_llm
            llm_result = await classify_query_with_llm(query, classifier_provider)
            # Map LLM classification type to workflow route name
            if llm_result.query_type and llm_result.query_type in wf.routes:
                route_name = llm_result.query_type
                logger.info("LLM classifier override: keyword=%s(score=%s) → llm=%s",
                            result.best_route, best_score, route_name)
        except Exception as exc:
            logger.warning("LLM classifier failed, using keyword result: %s", exc)

    if not route_name or route_name not in wf.routes:
        # Fallback: first non-delegate route
        for rn, rc in wf.routes.items():
            if not rc.delegate:
                route_name = rn
                break
        if not route_name:
            return None, None, None

    route = wf.routes[route_name]

    # For delegate routes (e.g. code_review → pr_review.yaml), signal delegation
    if route.delegate:
        logger.info("Workflow routing: query → route=%s (delegate to %s)", route_name, route.delegate)
        return route_name, route, None

    # Resolve the first agent in the route's pipeline
    agent_config = None
    for stage in route.pipeline:
        for agent_path in stage.agents:
            agent_config = wf.resolved_agents.get(agent_path)
            if agent_config:
                break
        if agent_config:
            break

    logger.info("Workflow routing: query → route=%s, agent=%s (keyword_score=%s)",
                route_name, agent_config.name if agent_config else "none", best_score)
    return route_name, route, agent_config


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class ContextQueryRequest(BaseModel):
    room_id: str
    query: str = Field(..., description="Natural-language question about the codebase.")
    max_iterations: int = Field(default=40, ge=1, le=80)
    model_id: Optional[str] = Field(
        default=None,
        description="Override model for this request. Uses default if null.",
    )


class ContextChunkResponse(BaseModel):
    file_path: str
    content: str
    start_line: int = 0
    end_line: int = 0
    source_tool: str = ""


class ThinkingStepResponse(BaseModel):
    kind: str
    iteration: int = 0
    text: str = ""
    tool: str = ""
    params: Dict[str, Any] = {}
    summary: str = ""
    success: bool = True


class ContextQueryResponse(BaseModel):
    room_id: str
    query: str
    answer: str
    context_chunks: List[ContextChunkResponse]
    thinking_steps: List[ThinkingStepResponse] = []
    tool_calls_made: int
    iterations: int
    duration_ms: float
    error: Optional[str] = None


class ExplainRichRequest(BaseModel):
    """Request for the agentic code-explanation endpoint."""
    room_id: str = Field(..., description="Room / workspace ID (maps to a git worktree).")
    code: str = Field(..., description="Selected code snippet to explain.")
    file_path: str = Field(..., description="Workspace-relative path of the file.")
    language: str = Field(default="", description="VS Code language ID, e.g. 'typescript'.")
    start_line: int = Field(default=0, description="1-based start line of the selection.")
    end_line: int = Field(default=0, description="1-based end line of the selection.")
    question: Optional[str] = Field(
        default=None,
        description="Optional specific question. Defaults to a general explanation request.",
    )


class ExplainRichResponse(BaseModel):
    """Response from the agentic code-explanation endpoint."""
    explanation: str
    model: str
    structured: Optional[Dict[str, str]] = None
    thinking_steps: List[ThinkingStepResponse] = []
    tool_calls_made: int = 0
    iterations: int = 0
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_git_workspace_service():
    from app.main import app
    return app.state.git_workspace_service


def _get_agent_provider():
    """Get the AI provider configured for agent loop."""
    from app.main import app
    return getattr(app.state, "agent_provider", None)


def _get_trace_writer():
    """Get the TraceWriter from app state (created during lifespan)."""
    from app.main import app
    return getattr(app.state, "trace_writer", None)


def _get_classifier_provider():
    """Get the lightweight model used for query pre-classification."""
    from app.main import app
    return getattr(app.state, "classifier_provider", None)


def _get_explorer_provider():
    """Get the sub-agent model used for code exploration (thinking disabled for Alibaba)."""
    from app.main import app
    return getattr(app.state, "explorer_provider", None)



# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


def _build_agent_from_route(
    agent_config,
    workflow_config,
    agent_provider,
    explorer_provider,
    trace_writer,
    max_iterations: int,
) -> AgentLoopService:
    """Create an AgentLoopService configured from workflow route's agent config."""
    # Use explorer provider for explorer agents, main provider for judges
    provider = explorer_provider or agent_provider
    if agent_config.model_role == "strong":
        provider = agent_provider

    # Budget from workflow config
    budget = workflow_config.budget
    weight = agent_config.budget_weight
    budget_tokens = int(budget.base_tokens * budget.sub_fraction * weight)
    budget_iters = max(
        int(budget.base_iterations * budget.sub_fraction * weight),
        budget.min_iterations,
    )
    budget_iters = min(budget_iters, max_iterations)

    return AgentLoopService(
        provider=provider,
        max_iterations=budget_iters,
        budget_config=BudgetConfig(max_input_tokens=budget_tokens),
        trace_writer=trace_writer,
        workflow_config=agent_config,
    )


@router.post("/query", response_model=ContextQueryResponse)
async def context_query(
    req: ContextQueryRequest,
    git_workspace=Depends(_get_git_workspace_service),
    agent_provider=Depends(_get_agent_provider),
    trace_writer=Depends(_get_trace_writer),
    classifier_provider=Depends(_get_classifier_provider),
    explorer_provider=Depends(_get_explorer_provider),
) -> ContextQueryResponse:
    """Run an agent loop to find relevant code context and answer a question."""
    if agent_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No AI provider configured. Enable an AI provider in conductor.settings.yaml.",
        )

    worktree_path = git_workspace.get_worktree_path(req.room_id)
    if worktree_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workspace for room_id={req.room_id!r}.",
        )

    # Workflow-driven classification (keyword first, LLM fallback for /ask)
    route_name, route, agent_config = await _classify_and_route(req.query, classifier_provider)

    if agent_config and _get_code_explorer_workflow():
        agent = _build_agent_from_route(
            agent_config, _get_code_explorer_workflow(),
            agent_provider, explorer_provider, trace_writer, req.max_iterations,
        )
    else:
        # Fallback: direct AgentLoopService (no workflow config)
        agent = AgentLoopService(
            provider=agent_provider,
            max_iterations=req.max_iterations,
            trace_writer=trace_writer,
            classifier_provider=classifier_provider,
            explorer_provider=explorer_provider,
        )

    result: AgentResult = await agent.run(
        query=req.query,
        workspace_path=str(worktree_path),
    )

    chunks = [
        ContextChunkResponse(
            file_path=c.file_path, content=c.content,
            start_line=c.start_line, end_line=c.end_line,
            source_tool=c.source_tool,
        )
        for c in result.context_chunks
    ]

    steps = [
        ThinkingStepResponse(
            kind=s.kind, iteration=s.iteration, text=s.text,
            tool=s.tool, params=s.params, summary=s.summary,
            success=s.success,
        )
        for s in result.thinking_steps
    ]

    return ContextQueryResponse(
        room_id=req.room_id, query=req.query, answer=result.answer,
        context_chunks=chunks, thinking_steps=steps,
        tool_calls_made=result.tool_calls_made,
        iterations=result.iterations, duration_ms=result.duration_ms,
        error=result.error,
    )


@router.post("/query/stream")
async def context_query_stream(
    req: ContextQueryRequest,
    git_workspace=Depends(_get_git_workspace_service),
    agent_provider=Depends(_get_agent_provider),
    trace_writer=Depends(_get_trace_writer),
    classifier_provider=Depends(_get_classifier_provider),
    explorer_provider=Depends(_get_explorer_provider),
):
    """SSE streaming version of context_query.

    Uses the code-explorer workflow config for classification and routing:
      * Keyword patterns from YAML determine which specialist agent handles the query
      * PR/code-review queries delegate to the multi-agent review pipeline
      * Each agent gets route-specific tools and budget from the workflow config

    Event types:
      * ``thinking``      — LLM reasoning text
      * ``tool_call``     — tool invocation starting
      * ``tool_result``   — tool execution completed (with summary)
      * ``context_chunk`` — a piece of code context collected
      * ``done``          — final answer
      * ``error``         — unrecoverable error
    """
    if agent_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No AI provider configured. Enable an AI provider in conductor.settings.yaml.",
        )

    worktree_path = git_workspace.get_worktree_path(req.room_id)
    if worktree_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workspace for room_id={req.room_id!r}.",
        )

    # Workflow-driven classification (keyword first, LLM fallback for /ask)
    route_name, route, agent_config = await _classify_and_route(req.query, classifier_provider)

    if agent_config and _get_code_explorer_workflow():
        agent = _build_agent_from_route(
            agent_config, _get_code_explorer_workflow(),
            agent_provider, explorer_provider, trace_writer, req.max_iterations,
        )
    else:
        # Fallback: direct AgentLoopService
        agent = AgentLoopService(
            provider=agent_provider,
            max_iterations=req.max_iterations,
            trace_writer=trace_writer,
            classifier_provider=classifier_provider,
            explorer_provider=explorer_provider,
        )

    async def event_generator():
        async for event in agent.run_stream(
            query=req.query,
            workspace_path=str(worktree_path),
        ):
            yield f"event: {event.kind}\ndata: {json.dumps(event.data, default=str)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _build_explain_query(req: ExplainRichRequest) -> str:
    """Build the agent query from an explain-rich request."""
    question = req.question or (
        f"Explain this {req.language} code: what it does, its inputs and outputs, "
        "the business scenario it serves, and any key dependencies or side-effects."
    )
    return (
        f"I need you to explain code from `{req.file_path}` "
        f"(lines {req.start_line}\u2013{req.end_line}).\n\n"
        f"```{req.language}\n{req.code}\n```\n\n"
        f"{question}\n\n"
        "Use the available tools to explore the codebase for additional context "
        "(e.g. read the surrounding file, find where functions are defined, "
        "check who calls this code, inspect imports). Then provide a thorough explanation."
    )


def _resolve_model_name() -> str:
    """Best-effort resolution of the active model name."""
    try:
        from app.ai_provider.resolver import get_resolver
        resolver = get_resolver()
        return resolver.active_model_id if resolver and resolver.active_model_id else "ai"
    except Exception:
        return "ai"


@router.post("/explain-rich", response_model=ExplainRichResponse)
async def explain_rich(
    req: ExplainRichRequest,
    git_workspace=Depends(_get_git_workspace_service),
    agent_provider=Depends(_get_agent_provider),
    trace_writer=Depends(_get_trace_writer),
) -> ExplainRichResponse:
    """Explain a code snippet using the agentic code-intelligence loop."""
    if agent_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No AI provider configured. Enable an AI provider in conductor.settings.yaml.",
        )

    worktree_path = git_workspace.get_worktree_path(req.room_id)
    if worktree_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workspace for room_id={req.room_id!r}.",
        )

    query = _build_explain_query(req)
    agent = AgentLoopService(provider=agent_provider, max_iterations=25, trace_writer=trace_writer)
    result: AgentResult = await agent.run(query=query, workspace_path=str(worktree_path))

    return ExplainRichResponse(
        explanation=result.answer,
        model=_resolve_model_name(),
        thinking_steps=[
            ThinkingStepResponse(
                kind=s.kind, iteration=s.iteration, text=s.text,
                tool=s.tool, params=s.params, summary=s.summary,
                success=s.success,
            )
            for s in result.thinking_steps
        ],
        tool_calls_made=result.tool_calls_made,
        iterations=result.iterations,
        duration_ms=result.duration_ms,
    )


@router.post("/explain-rich/stream")
async def explain_rich_stream(
    req: ExplainRichRequest,
    git_workspace=Depends(_get_git_workspace_service),
    agent_provider=Depends(_get_agent_provider),
    trace_writer=Depends(_get_trace_writer),
):
    """SSE streaming version of explain-rich."""
    if agent_provider is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="No AI provider configured. Enable an AI provider in conductor.settings.yaml.",
        )

    worktree_path = git_workspace.get_worktree_path(req.room_id)
    if worktree_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workspace for room_id={req.room_id!r}.",
        )

    query = _build_explain_query(req)
    agent = AgentLoopService(provider=agent_provider, max_iterations=25, trace_writer=trace_writer)
    model_name = _resolve_model_name()

    async def event_generator():
        async for event in agent.run_stream(
            query=query,
            workspace_path=str(worktree_path),
        ):
            # Inject model name into the done/error events
            if event.kind in ("done", "error"):
                event.data["model"] = model_name
            yield f"event: {event.kind}\ndata: {json.dumps(event.data, default=str)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
