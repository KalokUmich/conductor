"""Build the in-process MCP server that exposes Conductor's tools to an SDK worker.

Step 06a of the agent-SDK migration. A dispatched sub-agent runs on the Claude
Agent SDK; the SDK/CLI owns the loop and the generic built-ins (Bash, etc.), while
OUR tools are exposed as an in-process MCP server so they:

  * run behind the SAME ``CachedToolExecutor`` the in-house worker used → the Fact
    Vault still dedups reads across parallel sub-agents (built-in Read/Grep would
    run in the CLI subprocess and bypass the vault), and
  * carry a TYPED input schema (from each tool's Pydantic param model) so the model
    fills the real parameter names first-try — the Step-05 spike used a generic
    ``{"params": dict}`` schema and the model occasionally mis-shaped args.

This module is pure/unit-testable: building the server needs no Bedrock, and each
``@tool`` handler is a thin ``await executor.execute(name, params)`` wrapper.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from claude_agent_sdk import create_sdk_mcp_server, tool
from claude_agent_sdk.types import McpSdkServerConfig

from app.code_tools.schemas import TOOL_DEFINITIONS, TOOL_PARAM_MODELS, WORKER_MCP_TOOLS
from app.scratchpad.executor import CachedToolExecutor

MCP_SERVER_NAME = "conductor"

# The qualified prefix the CLI uses for an SDK MCP server's tools:
# mcp__<server>__<tool>. Workers are allowed exactly these.
_QUALIFIED_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

_DESCRIPTIONS: Dict[str, str] = {t["name"]: t.get("description", "") for t in TOOL_DEFINITIONS}


def qualified_tool_names(tool_names: List[str]) -> List[str]:
    """Map bare tool names to their ``mcp__conductor__<name>`` form for
    ``ClaudeAgentOptions.allowed_tools``."""
    return [f"{_QUALIFIED_PREFIX}{n}" for n in tool_names]


def _make_handler(executor: CachedToolExecutor, tool_name: str):
    """One async MCP handler that delegates to the shared executor.

    Bound ``tool_name`` via default arg to avoid the late-binding closure bug.
    The SDK passes validated args as a flat dict (keys = the Pydantic fields).
    """

    async def _handler(args: Dict[str, Any], _name: str = tool_name) -> Dict[str, Any]:
        # Typed schema → args arrive flat. Tolerate a legacy {"params": {...}}
        # envelope too, in case a caller wraps it.
        params = args.get("params", args) if isinstance(args, dict) else {}
        if not isinstance(params, dict):
            params = {}
        result = await executor.execute(_name, params)
        payload = {"success": result.success, "data": result.data, "error": result.error}
        is_error = not result.success
        return {
            "content": [{"type": "text", "text": json.dumps(payload, default=str)}],
            "is_error": is_error,
        }

    return _handler


def build_worker_tools(
    executor: CachedToolExecutor,
    tool_names: List[str],
):
    """Wrap the given tool names as SDK ``@tool`` objects bound to ``executor``.

    Each tool's input schema is its Pydantic ``model_json_schema()`` so the model
    sees real parameter names/types. Unknown names (no param model) are skipped
    with no error — the caller's allow-list governs availability.
    """
    sdk_tools = []
    for name in tool_names:
        model = TOOL_PARAM_MODELS.get(name)
        if model is None:
            continue
        description = _DESCRIPTIONS.get(name, f"Conductor tool: {name}")
        decorated = tool(name, description, model.model_json_schema())(_make_handler(executor, name))
        sdk_tools.append(decorated)
    return sdk_tools


def build_worker_mcp_server(
    executor: CachedToolExecutor,
    tool_names: Optional[List[str]] = None,
) -> McpSdkServerConfig:
    """Create the in-process MCP server exposing Conductor tools to an SDK worker.

    ``tool_names`` defaults to ``WORKER_MCP_TOOLS`` (the vault-aware file family +
    genuine additions). Pass an explicit list to scope a worker to fewer tools
    (e.g. an agent's ``.md`` tool list intersected with what's allowed).
    """
    names = list(tool_names) if tool_names is not None else sorted(WORKER_MCP_TOOLS)
    return create_sdk_mcp_server(MCP_SERVER_NAME, tools=build_worker_tools(executor, names))
