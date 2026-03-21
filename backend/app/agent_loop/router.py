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
from app.code_tools.executor import LocalToolExecutor, RemoteToolExecutor, ToolExecutor

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


async def _ensure_repo_graph(room_id: str, workspace_path: str, executor: ToolExecutor) -> None:
    """Fetch the repo graph from the extension and load it into RepoMapService.

    The extension lazily builds the graph on first request (or returns a cached
    version).  The backend uses it for dependency analysis and file ranking.
    """
    try:
        from app.repo_graph.service import RepoMapService

        # Check if we already have a cached graph
        svc = RepoMapService()
        cached_stats = svc.get_graph_stats(workspace_path)
        if cached_stats.get("cached"):
            return  # already loaded

        # Request the graph from the extension via the tool proxy
        result = await executor.execute("get_repo_graph", {})
        if result.success and result.data and result.data.get("files"):
            svc.load_graph_from_json(workspace_path, result.data)
            logger.info("Repo graph loaded from extension for %s", workspace_path)
        else:
            logger.info("No repo graph available from extension (empty workspace?)")
    except Exception as exc:
        logger.warning("Failed to load repo graph from extension: %s", exc)


def _extract_workflow_result(wf_context: dict):
    """Extract the final answer from a completed workflow context.

    The workflow engine stores stage results in ``_stage_results``.
    For ``first_match`` workflows with a synthesize stage (e.g. business_flow_tracing),
    the synthesizer's answer is the final answer. For single-agent routes,
    the explore stage answer is used directly.
    """
    stage_results = wf_context.get("_stage_results", {})
    answer = ""
    chunks = []
    total_tool_calls = 0
    total_iters = 0
    total_ms = int(wf_context.get("_duration_ms", 0))

    # Prefer synthesize stage (judge output)
    synth = stage_results.get("synthesize", {})
    for agent_name, result in synth.items():
        if isinstance(result, dict) and result.get("answer"):
            answer = result["answer"]
            break

    # If no synthesize, use explore stage
    if not answer:
        explore = stage_results.get("explore", stage_results.get("investigate", {}))
        for agent_name, result in explore.items():
            if isinstance(result, dict):
                if result.get("answer"):
                    answer = result["answer"]
                chunks.extend(result.get("context_chunks", []))
                total_tool_calls += result.get("tool_calls_made", 0)
                total_iters += result.get("iterations", 0)

    return answer, chunks, total_tool_calls, total_iters, total_ms


def _build_executor(git_workspace, room_id: str, worktree_path) -> ToolExecutor:
    """Build the right ToolExecutor based on workspace mode.

    Local-mode workspaces use RemoteToolExecutor (proxy to extension).
    Git-worktree workspaces use LocalToolExecutor (direct filesystem).
    """
    if git_workspace.is_local_workspace(room_id):
        return RemoteToolExecutor(room_id=room_id, workspace_path=str(worktree_path))
    return LocalToolExecutor(workspace_path=str(worktree_path))


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

    # Build the right executor (remote proxy for local workspaces, direct for git worktrees)
    is_local = git_workspace.is_local_workspace(req.room_id)
    executor = _build_executor(git_workspace, req.room_id, worktree_path)
    logger.info(
        "Tool executor: %s (room=%s, local=%s, workspace=%s)",
        type(executor).__name__, req.room_id, is_local, worktree_path,
    )

    # For local workspaces, fetch the repo graph from the extension (lazy-built)
    if is_local:
        await _ensure_repo_graph(req.room_id, str(worktree_path), executor)

    # Execute the full workflow pipeline (parallel agents, synthesis, etc.)
    workflow = _get_code_explorer_workflow()
    if workflow:
        from app.workflow.engine import WorkflowEngine
        engine = WorkflowEngine(
            provider=agent_provider,
            explorer_provider=explorer_provider,
            trace_writer=trace_writer,
            tool_executor=executor,
            classifier_provider=classifier_provider,
        )
        wf_context = {
            "query_text": req.query,
            "query": req.query,
            "workspace_path": str(worktree_path),
        }
        wf_result = await engine.run(workflow, wf_context)
        answer, chunks_raw, total_tool_calls, total_iters, total_ms = _extract_workflow_result(wf_result)
    else:
        # Fallback: direct AgentLoopService (no workflow config)
        agent = AgentLoopService(
            provider=agent_provider,
            max_iterations=req.max_iterations,
            trace_writer=trace_writer,
            classifier_provider=classifier_provider,
            explorer_provider=explorer_provider,
            tool_executor=executor,
        )
        result: AgentResult = await agent.run(
            query=req.query,
            workspace_path=str(worktree_path),
        )
        answer = result.answer
        chunks_raw = result.context_chunks
        total_tool_calls = result.tool_calls_made
        total_iters = result.iterations
        total_ms = result.duration_ms

    chunks = [
        ContextChunkResponse(
            file_path=c.file_path, content=c.content,
            start_line=c.start_line, end_line=c.end_line,
            source_tool=c.source_tool,
        )
        for c in (chunks_raw or [])
        if hasattr(c, 'file_path')
    ]

    return ContextQueryResponse(
        room_id=req.room_id, query=req.query, answer=answer or "",
        context_chunks=chunks, thinking_steps=[],
        tool_calls_made=total_tool_calls or 0,
        iterations=total_iters or 0, duration_ms=total_ms or 0,
        error=None,
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

    # Build the right executor (remote proxy for local workspaces, direct for git worktrees)
    is_local = git_workspace.is_local_workspace(req.room_id)
    executor = _build_executor(git_workspace, req.room_id, worktree_path)
    logger.info(
        "Tool executor: %s (room=%s, local=%s, workspace=%s)",
        type(executor).__name__, req.room_id, is_local, worktree_path,
    )

    # Execute the full workflow pipeline (parallel agents, synthesis, etc.)
    workflow = _get_code_explorer_workflow()

    async def event_generator():
        # Emit start event immediately so the client sees feedback < 100ms.
        # The padding comment pushes the first chunk past proxy buffer thresholds
        # (ngrok and some CDNs buffer until ~4KB before forwarding).
        yield f": padding {'.' * 2048}\n\n"
        yield f"event: start\ndata: {json.dumps({'query': req.query, 'room_id': req.room_id})}\n\n"

        if workflow:
            from app.workflow.engine import WorkflowEngine
            engine = WorkflowEngine(
                provider=agent_provider,
                explorer_provider=explorer_provider,
                trace_writer=trace_writer,
                tool_executor=executor,
            )
            wf_context = {
                "query_text": req.query,
                "query": req.query,
                "workspace_path": str(worktree_path),
            }
            async for event in engine.run_stream(workflow, wf_context):
                # Forward all events — agent events (thinking, tool_call, tool_result)
                # are now streamed in real-time through the engine's event queue.
                yield f"event: {event.kind}\ndata: {json.dumps(event.data, default=str)}\n\n"

                # When the workflow is done, emit the final synthesized answer
                if event.kind == "done":
                    answer, _, _, _, _ = _extract_workflow_result(wf_context)
                    if answer:
                        yield f"event: done\ndata: {json.dumps({'answer': answer}, default=str)}\n\n"
        else:
            # Fallback: direct AgentLoopService
            agent = AgentLoopService(
                provider=agent_provider,
                max_iterations=req.max_iterations,
                trace_writer=trace_writer,
                classifier_provider=classifier_provider,
                explorer_provider=explorer_provider,
                tool_executor=executor,
            )
            async for event in agent.run_stream(
                query=req.query,
                workspace_path=str(worktree_path),
            ):
                yield f"event: {event.kind}\ndata: {json.dumps(event.data, default=str)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",          # disable nginx proxy buffering
            "X-Content-Type-Options": "nosniff", # prevent proxy content sniffing
            "Connection": "keep-alive",
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

    executor = _build_executor(git_workspace, req.room_id, worktree_path)
    query = _build_explain_query(req)
    agent = AgentLoopService(provider=agent_provider, max_iterations=25, trace_writer=trace_writer, tool_executor=executor)
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

    executor = _build_executor(git_workspace, req.room_id, worktree_path)
    query = _build_explain_query(req)
    agent = AgentLoopService(provider=agent_provider, max_iterations=25, trace_writer=trace_writer, tool_executor=executor)
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
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",          # disable nginx proxy buffering
            "X-Content-Type-Options": "nosniff", # prevent proxy content sniffing
            "Connection": "keep-alive",
        },
    )
