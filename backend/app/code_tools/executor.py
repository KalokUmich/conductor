"""Tool execution abstraction layer.

Provides a ``ToolExecutor`` ABC so the agent loop can dispatch tool calls
to different backends:

  * **LocalToolExecutor** — runs tools directly on a local filesystem path
    (backend has direct access to the workspace).
  * **RemoteToolExecutor** — delegates tool calls over WebSocket to the
    VS Code extension for cloud-deployed backends (ECS) that cannot access
    the developer's local filesystem.
  * **TracingToolExecutor** — wraps any executor and records every tool
    call with its params, result, and latency for offline comparison.
"""
from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List

from .schemas import ToolResult
from .tools import execute_tool


class ToolExecutor(ABC):
    """Abstract interface for executing code-intelligence tools."""

    @abstractmethod
    async def execute(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        """Execute a tool and return its result."""


class LocalToolExecutor(ToolExecutor):
    """Executes tools directly on a local filesystem path.

    This is the default executor used when the backend has direct access to
    the workspace (same machine, or network-mounted path).
    """

    def __init__(self, workspace_path: str) -> None:
        self._workspace_path = workspace_path

    @property
    def workspace_path(self) -> str:
        return self._workspace_path

    async def execute(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        return await asyncio.to_thread(
            execute_tool, tool_name, self._workspace_path, params,
        )


class RemoteToolExecutor(ToolExecutor):
    """Proxies tool calls to the VS Code extension via WebSocket.

    Used when the workspace is in "local mode" — the developer's code
    lives on their machine, not on the server.  Each tool call is sent
    to the extension, executed locally, and the result is returned.

    **Exception**: Backend-only tools (e.g. browser/Playwright tools) run
    directly on the backend because they don't need filesystem access and
    the extension doesn't implement them.
    """

    # Tools that execute on the backend even in remote/local mode.
    # These don't require workspace filesystem access.
    _BACKEND_ONLY_TOOLS = frozenset([
        "web_search", "web_navigate", "web_click", "web_fill",
        "web_screenshot", "web_extract",
        "jira_search", "jira_get_issue", "jira_create_issue", "jira_update_issue", "jira_list_projects",
    ])

    def __init__(self, room_id: str, workspace_path: str) -> None:
        self._room_id = room_id
        self._workspace_path = workspace_path

    @property
    def workspace_path(self) -> str:
        return self._workspace_path

    async def execute(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        # Browser tools run on the backend — no need to proxy to extension
        if tool_name in self._BACKEND_ONLY_TOOLS:
            return await asyncio.to_thread(
                execute_tool, tool_name, self._workspace_path, params,
            )
        from .proxy import tool_proxy
        return await tool_proxy.execute(
            room_id=self._room_id,
            tool_name=tool_name,
            params=params,
            workspace=self._workspace_path,
        )


# ---------------------------------------------------------------------------
# Tracing wrapper
# ---------------------------------------------------------------------------


@dataclass
class TracedCall:
    """One recorded tool invocation."""
    tool_name: str
    params: Dict[str, Any]
    success: bool
    data: Any
    error: str | None
    truncated: bool
    latency_ms: float


class TracingToolExecutor(ToolExecutor):
    """Wraps another executor and records every call for offline comparison.

    After a run, inspect ``.calls`` for the full tool-call log including
    params, results, and per-call latency.
    """

    def __init__(self, inner: ToolExecutor) -> None:
        self._inner = inner
        self.calls: List[TracedCall] = []

    @property
    def workspace_path(self) -> str:
        return getattr(self._inner, "workspace_path", "")

    async def execute(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        t0 = time.monotonic()
        result = await self._inner.execute(tool_name, params)
        elapsed_ms = (time.monotonic() - t0) * 1000
        self.calls.append(TracedCall(
            tool_name=tool_name,
            params=params,
            success=result.success,
            data=result.data,
            error=result.error,
            truncated=result.truncated if hasattr(result, "truncated") else False,
            latency_ms=round(elapsed_ms, 1),
        ))
        return result
