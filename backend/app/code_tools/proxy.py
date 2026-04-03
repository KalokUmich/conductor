"""Remote tool execution proxy for local-mode workspaces.

When the backend runs on ECS and the workspace lives on the developer's
machine, tool calls are sent to the VS Code extension via WebSocket.
The extension executes them locally and returns the result.

Protocol
--------
Backend → Extension::

    {
        "type": "tool_request",
        "requestId": "<uuid>",
        "tool": "grep",
        "params": {"pattern": "authenticate", ...},
        "workspace": "/home/user/project"
    }

Extension → Backend::

    {
        "type": "tool_response",
        "requestId": "<uuid>",
        "success": true,
        "data": {...},
        "error": null,
        "truncated": false
    }
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any, Dict

from .schemas import ToolResult

logger = logging.getLogger(__name__)

# Default timeout for a single tool call (seconds).
# Some tools (grep on large repos, git log) can be slow.
TOOL_REQUEST_TIMEOUT = 60.0


class LocalToolProxy:
    """Sends tool calls to the VS Code extension via WebSocket.

    One global instance is created at startup and shared across all requests.
    Pending requests are tracked by ``requestId`` with asyncio Futures.
    """

    def __init__(self) -> None:
        self._pending: Dict[str, asyncio.Future] = {}

    async def execute(
        self,
        room_id: str,
        tool_name: str,
        params: Dict[str, Any],
        workspace: str,
        timeout: float = TOOL_REQUEST_TIMEOUT,
    ) -> ToolResult:
        """Send a tool request to the extension and await the response.

        Args:
            room_id: Chat room whose host will execute the tool.
            tool_name: Tool name (e.g. ``grep``, ``read_file``).
            params: Tool-specific parameters.
            workspace: Workspace root path on the developer's machine.
            timeout: Max seconds to wait for the response.

        Returns:
            ``ToolResult`` with the tool output.
        """
        from ..chat.manager import manager

        request_id = str(uuid.uuid4())
        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict] = loop.create_future()
        self._pending[request_id] = future

        # Send to the room host's WebSocket
        sent = await manager.send_to_host(
            room_id,
            {
                "type": "tool_request",
                "requestId": request_id,
                "tool": tool_name,
                "params": params,
                "workspace": workspace,
            },
        )

        if not sent:
            self._pending.pop(request_id, None)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error="Host not connected — cannot execute tool on local workspace",
            )

        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        except TimeoutError:
            self._pending.pop(request_id, None)
            return ToolResult(
                tool_name=tool_name,
                success=False,
                error=f"Tool execution timed out after {timeout}s",
            )
        finally:
            self._pending.pop(request_id, None)

        return ToolResult(
            tool_name=response.get("tool", tool_name),
            success=response.get("success", False),
            data=response.get("data"),
            error=response.get("error"),
            truncated=response.get("truncated", False),
        )

    def handle_response(self, data: dict) -> None:
        """Resolve a pending Future when the extension sends a tool_response.

        Called from the WebSocket message handler in the chat router.
        """
        request_id = data.get("requestId")
        if not request_id:
            logger.warning("tool_response without requestId — ignoring")
            return

        future = self._pending.get(request_id)
        if future is None:
            logger.warning("tool_response for unknown requestId %s — ignoring", request_id)
            return

        if not future.done():
            future.set_result(data)
        else:
            logger.warning("tool_response for already-resolved requestId %s", request_id)


# Global singleton — created at import time, wired in lifespan.
tool_proxy = LocalToolProxy()
