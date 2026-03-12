"""Tool execution abstraction layer.

Provides a ``ToolExecutor`` ABC so the agent loop can dispatch tool calls
to different backends:

  * **LocalToolExecutor** — runs tools directly on a local filesystem path
    (current behaviour).
  * **RemoteToolExecutor** — (future) delegates tool calls over WebSocket to
    the VS Code extension for cloud-deployed backends that cannot access the
    host's filesystem directly.
"""
from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from typing import Any, Dict

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
