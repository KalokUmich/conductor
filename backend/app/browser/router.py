"""REST endpoints for browser automation tools.

Provides direct access to Playwright browser tools for web browsing,
form filling, screenshots, and data extraction.
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, status

from ..code_tools.schemas import ToolResult
from .service import get_browser_service
from .tools import BROWSER_TOOL_REGISTRY

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/browser", tags=["browser"])


@router.get("/sessions")
async def list_sessions() -> Dict[str, Any]:
    """List active browser sessions."""
    service = get_browser_service()
    return {"sessions": service.list_sessions()}


@router.post("/execute/{tool_name}", response_model=ToolResult)
async def execute_browser_tool(
    tool_name: str,
    session_id: str,
    params: Dict[str, Any],
) -> ToolResult:
    """Execute a browser tool by name.

    session_id acts as the browser session key — all calls with the same
    session_id share the same browser page.
    """
    fn = BROWSER_TOOL_REGISTRY.get(tool_name)
    if fn is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown browser tool: {tool_name}",
        )
    return fn(workspace=session_id, **params)


@router.post("/sessions/{session_id}/close")
async def close_session(session_id: str) -> Dict[str, str]:
    """Close a browser session and free resources."""
    service = get_browser_service()
    service.close_session(session_id)
    return {"status": "ok", "session_id": session_id}
