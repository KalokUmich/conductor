"""REST endpoints for code intelligence tools.

Provides direct access to individual tools (for debugging / non-agent use)
and lists available tools.
"""
from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, status

from .schemas import (
    TOOL_DEFINITIONS,
    FileOutlineParams,
    FindReferencesParams,
    FindSymbolParams,
    GetDependenciesParams,
    GetDependentsParams,
    GitDiffParams,
    GitLogParams,
    GrepParams,
    ListFilesParams,
    ReadFileParams,
    ToolResult,
)
from .tools import execute_tool, invalidate_graph_cache, invalidate_symbol_cache

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/code-tools", tags=["code-tools"])


# ---------------------------------------------------------------------------
# Dependency helpers
# ---------------------------------------------------------------------------


def _get_git_workspace_service():
    from app.main import app
    return app.state.git_workspace_service


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/available")
async def list_tools() -> Dict[str, Any]:
    """List all available code intelligence tools with their schemas."""
    return {"tools": TOOL_DEFINITIONS}


@router.post("/cache/invalidate")
async def invalidate_cache(
    room_id: str,
    git_workspace=Depends(_get_git_workspace_service),
) -> Dict[str, Any]:
    """Invalidate backend-side symbol index and dependency graph caches.

    Called by the extension's "Rebuild Index" button to force a fresh scan
    on the next agent query.
    """
    worktree_path = git_workspace.get_worktree_path(room_id)
    if worktree_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workspace for room_id={room_id!r}.",
        )
    ws = str(worktree_path)
    invalidate_symbol_cache(ws)
    invalidate_graph_cache(ws)
    logger.info("Cache invalidated for room_id=%s workspace=%s", room_id, ws)
    return {"status": "ok", "room_id": room_id, "workspace": ws}


@router.post("/execute/{tool_name}", response_model=ToolResult)
async def execute(
    tool_name: str,
    room_id: str,
    params: Dict[str, Any],
    git_workspace=Depends(_get_git_workspace_service),
) -> ToolResult:
    """Execute a single code tool by name."""
    worktree_path = git_workspace.get_worktree_path(room_id)
    if worktree_path is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workspace for room_id={room_id!r}.",
        )
    return execute_tool(tool_name, str(worktree_path), params)
