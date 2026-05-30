"""Step 05 SDK-worker spike — shared harness (THROWAWAY, not imported by app/).

Proves design §7's seams: a Claude worker running on the Claude Agent SDK, using
ONLY our tools (proxied through the same CachedToolExecutor the in-house worker
uses), returning a result the coordinator's condense_result() accepts unchanged.

Run a seam directly, e.g.:
    cd backend && ../.venv/bin/python -m spikes.sdk_worker.seam1_proxy

NOT wired into brain.py. The deliverable is the seam verdicts + (maybe) this
runner skeleton carried into Step 06.
"""

from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

# Make `app` importable when run as a module from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.code_tools.executor import LocalToolExecutor  # noqa: E402
from app.config import load_config  # noqa: E402
from app.scratchpad.executor import CachedToolExecutor  # noqa: E402
from app.scratchpad.store import FactStore  # noqa: E402

from claude_agent_sdk import (  # noqa: E402
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
    create_sdk_mcp_server,
    query,
    tool,
)

# Bedrock model ids (from config/conductor.settings.yaml).
HAIKU = "eu.anthropic.claude-haiku-4-5-20251001-v1:0"
SONNET = "eu.anthropic.claude-sonnet-4-6"

PARITY_REPO = str(Path(__file__).resolve().parent.parent.parent.parent / "tests" / "fixtures" / "parity_repo")

# Representative tool subset for the spike (full 46-tool port is Step 06).
SPIKE_TOOLS = ["read_file", "grep", "list_files", "file_outline", "find_symbol"]


# ---------------------------------------------------------------------------
# AgentResult-shaped shim — must satisfy condense_result() (brain.py:308),
# which duck-types on: answer, thinking_steps, context_chunks, files_accessed,
# tool_calls_made, iterations, duration_ms, error, budget_summary.
# ---------------------------------------------------------------------------
@dataclass
class SdkAgentResult:
    answer: str = ""
    context_chunks: List[Any] = field(default_factory=list)
    thinking_steps: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls_made: int = 0
    iterations: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None
    budget_summary: Optional[Dict[str, Any]] = None
    files_accessed: List[str] = field(default_factory=list)


def _bedrock_env() -> Dict[str, str]:
    """Env vars the spawned Claude Code CLI reads to target Bedrock."""
    cfg = load_config()
    bedrock = cfg.ai_providers.aws_bedrock
    env = {
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "AWS_ACCESS_KEY_ID": bedrock.access_key_id,
        "AWS_SECRET_ACCESS_KEY": bedrock.secret_access_key,
        "AWS_REGION": bedrock.region,
        "AWS_DEFAULT_REGION": bedrock.region,
    }
    if bedrock.session_token:
        env["AWS_SESSION_TOKEN"] = bedrock.session_token
    return env


def make_cached_executor(workspace: str = PARITY_REPO, *, agent: str = "spike") -> tuple[CachedToolExecutor, FactStore]:
    """Build the SAME executor stack the in-house worker uses: a FactStore-backed
    CachedToolExecutor wrapping a LocalToolExecutor."""
    store = FactStore.open(f"spike-{int(time.time())}", workspace=workspace, task_id="step05-spike")
    inner = LocalToolExecutor(workspace)
    return CachedToolExecutor(inner, store, agent=agent), store


def _build_mcp_tools(executor: CachedToolExecutor) -> list:
    """Wrap the representative tool subset as SDK @tool handlers that delegate to
    the shared CachedToolExecutor (so the Fact Vault sits behind every call)."""
    sdk_tools = []
    for name in SPIKE_TOOLS:

        # Bind name via default arg to avoid late-binding closure bug.
        @tool(name, f"Conductor code tool: {name}", {"params": dict})
        async def _handler(args: Dict[str, Any], _name: str = name) -> Dict[str, Any]:
            params = args.get("params", args) or {}
            result = await executor.execute(_name, params)
            payload = {
                "success": result.success,
                "data": result.data,
                "error": result.error,
            }
            return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}

        sdk_tools.append(_handler)
    return sdk_tools


async def run_sdk_worker(
    *,
    model: str,
    system_prompt: str,
    user_message: str,
    executor: CachedToolExecutor,
    max_turns: int = 8,
    allow_builtins: bool = False,
) -> SdkAgentResult:
    """Drive one SDK worker query and map the result to the AgentResult shim."""
    t0 = time.time()
    server = create_sdk_mcp_server("conductor", tools=_build_mcp_tools(executor))

    allowed = [f"mcp__conductor__{n}" for n in SPIKE_TOOLS]
    opts = ClaudeAgentOptions(
        model=model,
        env=_bedrock_env(),
        mcp_servers={"conductor": server},
        allowed_tools=allowed,
        system_prompt=system_prompt,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        # all-MCP: when built-ins are off, only our proxied tools exist (seam 4).
        setting_sources=None if allow_builtins else [],
    )

    answer_parts: List[str] = []
    thinking_steps: List[Dict[str, Any]] = []
    tool_calls = 0
    files: set[str] = set()
    usage: Optional[Dict[str, Any]] = None
    iterations = 0

    async for msg in query(prompt=user_message, options=opts):
        if isinstance(msg, AssistantMessage):
            iterations += 1
            for block in msg.content:
                if isinstance(block, TextBlock):
                    answer_parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    tool_calls += 1
                    tname = block.name.replace("mcp__conductor__", "")
                    p = (block.input or {}).get("params", block.input or {})
                    fp = p.get("path") or p.get("file") or p.get("file_path")
                    if fp:
                        files.add(fp)
                    thinking_steps.append({"kind": "tool_result", "tool": tname, "summary": json.dumps(p, default=str)[:200]})
        elif isinstance(msg, ResultMessage):
            usage = getattr(msg, "usage", None)

    return SdkAgentResult(
        answer="\n".join(answer_parts).strip(),
        thinking_steps=thinking_steps,
        tool_calls_made=tool_calls,
        iterations=iterations,
        duration_ms=(time.time() - t0) * 1000.0,
        files_accessed=sorted(files),
        budget_summary={"usage": usage} if usage else None,
    )
