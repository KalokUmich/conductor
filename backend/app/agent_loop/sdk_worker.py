"""SdkWorkerRunner — runs a dispatched sub-agent on the Claude Agent SDK.

Step 06b of the agent-SDK migration. This is the production engine that replaces
``AgentLoopService`` for the *dispatched sub-agent* path (the coordinator Brain
still runs on AgentLoopService). The SDK/CLI owns the iterate→call-LLM→exec-tools
loop and context compaction; we keep the orchestration moat:

  * **Tools** — our vault-aware MCP tools (``sdk_tools.build_worker_mcp_server``)
    behind the SAME ``CachedToolExecutor`` instance, so the Fact Vault still
    dedups across parallel sub-agents.
  * **Prompt** — the shared 4-layer ``build_sub_agent_system_prompt`` output is
    passed verbatim as ``system_prompt`` (full replace of the CLI default).
  * **Evidence gate** — applied *post-call*: after the SDK run we score the answer
    with ``check_evidence`` and, if it's thin and budget/turns remain, re-run once
    with guidance prepended. Deterministic and unit-testable (the SDK Stop-hook
    variant is a roadmap experiment).
  * **Return contract** — produces an ``AgentResult``-shaped object that
    ``brain.condense_result`` accepts unchanged (answer, thinking_steps,
    context_chunks, files_accessed, tool_calls_made, iterations, duration_ms,
    error, budget_summary).
  * **Concurrency** — respects the Brain's ``llm_semaphore``.

Honors design §5.4's five permanent contracts.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
    query,
)

from app.config import load_config
from app.scratchpad.executor import CachedToolExecutor

from .evidence import check_evidence
from .sdk_tools import (
    MCP_SERVER_NAME,
    build_worker_mcp_server,
    qualified_tool_names,
)

logger = logging.getLogger(__name__)

_QUALIFIED_PREFIX = f"mcp__{MCP_SERVER_NAME}__"

# C0 control bytes except tab/newline/carriage-return. The SDK hands prompts to
# the CLI as subprocess args; a NUL (e.g. a UTF-16 project doc read as text leaves
# \x00 between ASCII chars) makes os.exec raise "embedded null byte" at spawn. The
# old HTTP/Bedrock provider path tolerated these in the JSON body.
_CTRL_BYTES_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _sanitize_for_cli(text: str) -> str:
    """Strip NUL + other C0 control bytes so the prompt is safe as a subprocess arg."""
    if not text:
        return text
    return _CTRL_BYTES_RE.sub("", text)


@dataclass
class SdkAgentResult:
    """AgentResult-shaped result of an SDK worker run.

    Field set mirrors ``service.AgentResult`` so ``condense_result`` (brain.py)
    consumes it unchanged.
    """

    answer: str = ""
    context_chunks: List[Any] = field(default_factory=list)
    thinking_steps: List[Dict[str, Any]] = field(default_factory=list)
    tool_calls_made: int = 0
    iterations: int = 0
    duration_ms: float = 0.0
    error: Optional[str] = None
    budget_summary: Optional[Dict[str, Any]] = None
    files_accessed: List[str] = field(default_factory=list)


def bedrock_env() -> Dict[str, str]:
    """Env the spawned Claude Code CLI reads to target Bedrock (Claude-only)."""
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


def _file_param(params: Dict[str, Any]) -> Optional[str]:
    """Extract the file path a tool call touched (for files_accessed tracking)."""
    if not isinstance(params, dict):
        return None
    return params.get("path") or params.get("file") or params.get("file_path")


def _usage_to_budget_summary(usage: Optional[Dict[str, Any]], iterations: int) -> Dict[str, Any]:
    """Map the SDK ``ResultMessage.usage`` dict onto our budget_summary keys.

    Brain reads ``total_input_tokens`` / ``total_output_tokens`` (brain.py:1488,
    1524-1525); we also surface cache + raw usage for telemetry.
    """
    u = usage or {}
    in_tok = u.get("input_tokens", 0) or 0
    out_tok = u.get("output_tokens", 0) or 0
    cache_read = u.get("cache_read_input_tokens", 0) or 0
    cache_write = u.get("cache_creation_input_tokens", 0) or 0
    return {
        "total_input_tokens": in_tok,
        "total_output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "cache_read_input_tokens": cache_read,
        "cache_creation_input_tokens": cache_write,
        "iterations": iterations,
        "raw_usage": u or None,
    }


class SdkWorkerRunner:
    """Runs one dispatched sub-agent on the Claude Agent SDK.

    Constructed per-dispatch by the Brain (mirrors how AgentLoopService was built),
    bound to the same ``CachedToolExecutor`` so vault state is shared.
    """

    def __init__(
        self,
        *,
        model: str,
        tool_executor: CachedToolExecutor,
        tool_names: List[str],
        max_turns: int = 8,
        max_evidence_retries: int = 1,
        min_file_refs: int = 1,
        min_tool_calls: int = 2,
        temperature: Optional[float] = None,
        llm_semaphore: Optional[asyncio.Semaphore] = None,
        allow_builtins: bool = True,
    ) -> None:
        self._model = model
        self._executor = tool_executor
        self._tool_names = list(tool_names)
        self._max_turns = max_turns
        self._max_evidence_retries = max_evidence_retries
        self._min_file_refs = min_file_refs
        self._min_tool_calls = min_tool_calls
        self._temperature = temperature
        self._llm_semaphore = llm_semaphore
        self._allow_builtins = allow_builtins

    # --- SDK options -------------------------------------------------------
    def _build_options(self, system_prompt: str) -> ClaudeAgentOptions:
        server = build_worker_mcp_server(self._executor, self._tool_names)
        opts = ClaudeAgentOptions(
            model=self._model,
            env=bedrock_env(),
            mcp_servers={MCP_SERVER_NAME: server},
            allowed_tools=qualified_tool_names(self._tool_names),
            system_prompt=system_prompt,
            max_turns=self._max_turns,
            permission_mode="bypassPermissions",
            # allow_builtins=False → only our MCP tools exist (local-mode strategy B).
            setting_sources=None if self._allow_builtins else [],
        )
        if self._temperature is not None and hasattr(opts, "temperature"):
            # Surfaced if the SDK build supports it; harmless to skip if not.
            with contextlib.suppress(Exception):
                opts.temperature = self._temperature  # type: ignore[attr-defined]
        return opts

    # --- one SDK pass ------------------------------------------------------
    async def _run_once(self, system_prompt: str, user_message: str) -> SdkAgentResult:
        t0 = time.time()
        # Prompts become subprocess args — strip control bytes that would abort spawn.
        system_prompt = _sanitize_for_cli(system_prompt)
        user_message = _sanitize_for_cli(user_message)
        opts = self._build_options(system_prompt)

        answer_parts: List[str] = []
        thinking_steps: List[Dict[str, Any]] = []
        tool_calls = 0
        files: set[str] = set()
        usage: Optional[Dict[str, Any]] = None
        iterations = 0
        error: Optional[str] = None

        async def _drive() -> None:
            nonlocal tool_calls, iterations, usage, error
            async for msg in query(prompt=user_message, options=opts):
                if isinstance(msg, AssistantMessage):
                    iterations += 1
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            answer_parts.append(block.text)
                        elif isinstance(block, ThinkingBlock):
                            thinking_steps.append({"kind": "thinking", "text": block.thinking[:500]})
                        elif isinstance(block, ToolUseBlock):
                            tool_calls += 1
                            tname = block.name.replace(_QUALIFIED_PREFIX, "")
                            params = block.input if isinstance(block.input, dict) else {}
                            fp = _file_param(params)
                            if fp:
                                files.add(fp)
                            thinking_steps.append(
                                {
                                    "kind": "tool_result",
                                    "tool": tname,
                                    "summary": json.dumps(params, default=str)[:200],
                                }
                            )
                elif isinstance(msg, ResultMessage):
                    usage = getattr(msg, "usage", None)
                    if getattr(msg, "is_error", False):
                        error = getattr(msg, "result", None) or "SDK reported is_error"

        try:
            if self._llm_semaphore is not None:
                async with self._llm_semaphore:
                    await _drive()
            else:
                await _drive()
        except Exception as exc:  # SDK / CLI / transport failure
            logger.warning("SdkWorkerRunner query failed: %s", exc)
            error = f"{type(exc).__name__}: {exc}"

        return SdkAgentResult(
            answer="\n".join(answer_parts).strip(),
            thinking_steps=thinking_steps,
            tool_calls_made=tool_calls,
            iterations=iterations,
            duration_ms=(time.time() - t0) * 1000.0,
            error=error,
            files_accessed=sorted(files),
            budget_summary=_usage_to_budget_summary(usage, iterations),
        )

    # --- public: run with post-call evidence gate --------------------------
    async def run(self, *, system_prompt: str, user_message: str) -> SdkAgentResult:
        """Run the worker, applying the post-call evidence gate.

        If the answer is thin (no file:line refs / too few tool calls) and a retry
        budget remains, re-run once with the gate's guidance prepended to the query.
        """
        result = await self._run_once(system_prompt, user_message)

        retries = 0
        while retries < self._max_evidence_retries and result.error is None and result.answer:
            check = check_evidence(
                answer=result.answer,
                tool_calls_made=result.tool_calls_made,
                files_accessed=len(result.files_accessed),
                # one more pass available → signal the gate that a retry is possible
                remaining_iterations=2,
                min_file_refs=self._min_file_refs,
                min_tool_calls=self._min_tool_calls,
            )
            if check.passed or not check.guidance:
                break
            retries += 1
            logger.info("SdkWorkerRunner evidence retry %d: %s", retries, check.guidance[:120])
            guided = f"{check.guidance}\n\n---\nOriginal task:\n{user_message}"
            retried = await self._run_once(system_prompt, guided)
            # Merge usage so budget reflects total spend across passes.
            retried.budget_summary = _merge_budget(result.budget_summary, retried.budget_summary)
            retried.tool_calls_made += result.tool_calls_made
            result = retried

        return result


def _merge_budget(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Sum token counts across two passes for an accurate budget_summary."""
    a = a or {}
    b = b or {}
    keys = (
        "total_input_tokens",
        "total_output_tokens",
        "total_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "iterations",
    )
    merged: Dict[str, Any] = {k: (a.get(k, 0) or 0) + (b.get(k, 0) or 0) for k in keys}
    return merged
