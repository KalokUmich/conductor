"""End-to-end SDK leaf-worker smoke test (local mode).

Drives the REAL SdkWorkerRunner → Claude Agent SDK → claude CLI subprocess →
Bedrock → sonnet, with our vault-aware MCP tools behind CachedToolExecutor.
Confirms the dispatched-leaf path works locally after the agent-SDK migration.

Hard 90s timeout so an auth/credential stall fails fast instead of hanging.

Run on host venv (profile mode needs ~/.aws):
    cd backend && python scripts/sdk_smoke.py
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.code_tools.executor import LocalToolExecutor
from app.scratchpad.store import FactStore
from app.scratchpad.executor import CachedToolExecutor
from app.agent_loop.sdk_worker import SdkWorkerRunner

WORKSPACE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # the backend/ dir
TOOLS = ["grep", "read_file", "list_files"]
# Pin sonnet: opus-4-8 (the first non-explorer model in settings) 400s on the
# sandbox-render-a account. Override with SMOKE_MODEL=<id>.
MODEL = os.environ.get("SMOKE_MODEL", "eu.anthropic.claude-sonnet-4-6")

SYSTEM_PROMPT = (
    "You are a code-exploration sub-agent. Use the provided tools to find a "
    "concrete answer with a file:line reference. Be terse."
)
USER_MESSAGE = (
    "Using grep, find where `SdkWorkerRunner` is defined in this workspace and "
    "report the file path and line number. One sentence."
)


async def _main() -> int:
    model = MODEL
    print(f"workspace : {WORKSPACE}")
    print(f"model     : {model}")
    print(f"tools     : {TOOLS}")

    store = FactStore(":memory:", "sdk-smoke-session")
    inner = LocalToolExecutor(WORKSPACE)
    executor = CachedToolExecutor(inner, store, agent="sdk-smoke")

    # allow_builtins=False → local-mode strategy B: ONLY our MCP tools exist, no
    # CLI built-in Read/Grep/settings (keeps the run hermetic + matches prod leaf).
    runner = SdkWorkerRunner(
        model=model,
        tool_executor=executor,
        tool_names=TOOLS,
        max_turns=6,
        max_evidence_retries=0,
        allow_builtins=False,
    )

    print("\n[running SdkWorkerRunner — SDK→CLI→Bedrock→sonnet, 90s timeout]\n")
    try:
        result = await asyncio.wait_for(
            runner.run(system_prompt=SYSTEM_PROMPT, user_message=USER_MESSAGE),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        print("RESULT: FAIL — timed out after 90s (auth stall or CLI hang).")
        return 3

    if result.error:
        print(f"RESULT: FAIL — error: {result.error}")
        return 4

    print("--- answer ---")
    print(result.answer or "(empty)")
    print("--- stats ---")
    print(f"tool_calls={result.tool_calls_made} iterations={result.iterations} "
          f"files={result.files_accessed} duration_ms={result.duration_ms:.0f}")
    print(f"budget={result.budget_summary}")
    print(f"vault stats={executor.stats}")

    ok = bool(result.answer) and result.tool_calls_made > 0
    print(f"\nRESULT: {'PASS ✅' if ok else 'PARTIAL ⚠ (no tool calls or empty answer)'}")
    return 0 if ok else 5


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
