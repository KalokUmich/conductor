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

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/context", tags=["context"])


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

    agent = AgentLoopService(
        provider=agent_provider,
        max_iterations=req.max_iterations,
        trace_writer=trace_writer,
        classifier_provider=classifier_provider,
    )

    result: AgentResult = await agent.run(
        query=req.query,
        workspace_path=str(worktree_path),
    )

    chunks = [
        ContextChunkResponse(
            file_path=c.file_path,
            content=c.content,
            start_line=c.start_line,
            end_line=c.end_line,
            source_tool=c.source_tool,
        )
        for c in result.context_chunks
    ]

    steps = [
        ThinkingStepResponse(
            kind=s.kind,
            iteration=s.iteration,
            text=s.text,
            tool=s.tool,
            params=s.params,
            summary=s.summary,
            success=s.success,
        )
        for s in result.thinking_steps
    ]

    return ContextQueryResponse(
        room_id=req.room_id,
        query=req.query,
        answer=result.answer,
        context_chunks=chunks,
        thinking_steps=steps,
        tool_calls_made=result.tool_calls_made,
        iterations=result.iterations,
        duration_ms=result.duration_ms,
        error=result.error,
    )


@router.post("/query/stream")
async def context_query_stream(
    req: ContextQueryRequest,
    git_workspace=Depends(_get_git_workspace_service),
    agent_provider=Depends(_get_agent_provider),
    trace_writer=Depends(_get_trace_writer),
    classifier_provider=Depends(_get_classifier_provider),
):
    """SSE streaming version of context_query.

    Streams events as the agent loop progresses so the client can display
    real-time progress (e.g. "Searching for auth patterns...").

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

    agent = AgentLoopService(
        provider=agent_provider,
        max_iterations=req.max_iterations,
        trace_writer=trace_writer,
        classifier_provider=classifier_provider,
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
