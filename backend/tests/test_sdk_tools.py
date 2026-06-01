"""Tests for the SDK worker tool layer (Step 06a) — no Bedrock required.

Covers:
  * TOOL_PARAM_MODELS is consistent with TOOL_DEFINITIONS (drift guard).
  * WORKER_MCP_TOOLS / WORKER_BUILTIN_TOOLS are sane (no orchestration leak).
  * build_worker_tools produces typed @tool objects whose handlers delegate to
    the given CachedToolExecutor and map ToolResult → MCP content payload.
"""

from __future__ import annotations

import json

import pytest

from app.agent_loop import sdk_tools
from app.code_tools.schemas import (
    BRAIN_TOOL_DEFINITIONS,
    TOOL_DEFINITIONS,
    TOOL_PARAM_MODELS,
    WORKER_BUILTIN_TOOLS,
    WORKER_MCP_TOOLS,
    ToolResult,
)


# ---------------------------------------------------------------------------
# Registry consistency (the drift guard)
# ---------------------------------------------------------------------------
def _all_defs():
    d = {t["name"]: t["input_schema"] for t in TOOL_DEFINITIONS}
    d.update({t["name"]: t["input_schema"] for t in BRAIN_TOOL_DEFINITIONS})
    return d


def test_every_tool_has_a_param_model():
    defs = _all_defs()
    missing = [n for n in defs if n not in TOOL_PARAM_MODELS]
    assert not missing, f"tools without a param model: {missing}"


def test_no_extra_param_models():
    defs = _all_defs()
    extra = [n for n in TOOL_PARAM_MODELS if n not in defs]
    assert not extra, f"param models for unknown tools: {extra}"


def test_param_model_schema_matches_definition():
    """Each model's JSON schema must equal the input_schema already declared."""
    defs = _all_defs()
    drift = [n for n, sch in defs.items() if TOOL_PARAM_MODELS[n].model_json_schema() != sch]
    assert not drift, f"schema drift between TOOL_PARAM_MODELS and TOOL_DEFINITIONS: {drift}"


# ---------------------------------------------------------------------------
# Worker tool-set sanity
# ---------------------------------------------------------------------------
def test_worker_mcp_tools_all_have_models():
    assert all(t in TOOL_PARAM_MODELS for t in WORKER_MCP_TOOLS)


def test_no_orchestration_tools_for_workers():
    orchestration = {"create_plan", "dispatch_explore", "dispatch_verify", "dispatch_sweep", "transfer_to_brain"}
    assert not (WORKER_MCP_TOOLS & orchestration)


def test_vault_aware_file_family_kept_as_mcp():
    # The whole point: read/grep stay MCP (behind the vault), not delegated to built-ins.
    for t in ("read_file", "grep", "list_files", "glob"):
        assert t in WORKER_MCP_TOOLS


def test_git_delegated_to_builtin_not_mcp():
    # git_* is covered by the built-in Bash; we don't re-expose our git_* MCP tools.
    assert "Bash" in WORKER_BUILTIN_TOOLS
    for t in ("git_log", "git_diff", "git_blame", "git_show", "git_diff_files", "git_hotspots"):
        assert t not in WORKER_MCP_TOOLS


def test_qualified_tool_names():
    out = sdk_tools.qualified_tool_names(["read_file", "find_symbol"])
    assert out == ["mcp__conductor__read_file", "mcp__conductor__find_symbol"]


# ---------------------------------------------------------------------------
# Tool building + handler delegation
# ---------------------------------------------------------------------------
class _FakeExecutor:
    """Minimal CachedToolExecutor stand-in: records calls, returns canned result."""

    def __init__(self):
        self.calls = []

    async def execute(self, tool_name, params):
        self.calls.append((tool_name, params))
        return ToolResult(tool_name=tool_name, success=True, data={"echo": params}, error=None)


def test_build_worker_tools_uses_typed_schema():
    ex = _FakeExecutor()
    tools = sdk_tools.build_worker_tools(ex, ["read_file", "find_symbol"])
    assert len(tools) == 2
    # Each SdkMcpTool carries the typed JSON schema (object with the model's props).
    schemas = {t.name: t.input_schema for t in tools}
    assert "path" in schemas["read_file"]["properties"]
    assert "name" in schemas["find_symbol"]["properties"]


def test_build_worker_tools_skips_unknown():
    ex = _FakeExecutor()
    tools = sdk_tools.build_worker_tools(ex, ["read_file", "not_a_real_tool"])
    assert [t.name for t in tools] == ["read_file"]


@pytest.mark.asyncio
async def test_handler_delegates_and_maps_result():
    ex = _FakeExecutor()
    (rf,) = sdk_tools.build_worker_tools(ex, ["read_file"])
    out = await rf.handler({"path": "app/service.py", "start_line": 1, "end_line": 5})
    # delegated to executor with flat params
    assert ex.calls == [("read_file", {"path": "app/service.py", "start_line": 1, "end_line": 5})]
    # mapped to MCP content payload
    payload = json.loads(out["content"][0]["text"])
    assert payload["success"] is True
    assert payload["data"] == {"echo": {"path": "app/service.py", "start_line": 1, "end_line": 5}}
    assert out["is_error"] is False


@pytest.mark.asyncio
async def test_handler_marks_error_on_failure():
    class _FailExec:
        async def execute(self, tool_name, params):
            return ToolResult(tool_name=tool_name, success=False, data=None, error="boom")

    (rf,) = sdk_tools.build_worker_tools(_FailExec(), ["read_file"])
    out = await rf.handler({"path": "x"})
    assert out["is_error"] is True
    assert json.loads(out["content"][0]["text"])["error"] == "boom"


@pytest.mark.asyncio
async def test_handler_tolerates_params_envelope():
    ex = _FakeExecutor()
    (rf,) = sdk_tools.build_worker_tools(ex, ["grep"])
    await rf.handler({"params": {"pattern": "foo"}})
    assert ex.calls == [("grep", {"pattern": "foo"})]
