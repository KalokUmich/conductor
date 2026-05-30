"""Seam 3: per-worker model switch + the permanent return contract.

Run the SAME exploration query once on Haiku-Bedrock and once on Sonnet-Bedrock
through the real SDK→CLI→Bedrock path, each using only our proxied tools. Both
must produce an SdkAgentResult that condense_result() (brain.py:308) accepts
unchanged — i.e. the coordinator can't tell an SDK worker from an AgentLoopService
worker.

REAL BEDROCK CALLS. Pre-flight: bash scripts/refactor/check_creds.sh

    cd backend && ../.venv/bin/python -m spikes.sdk_worker.seam3_modelswitch
"""

from __future__ import annotations

import asyncio
import json

from app.agent_loop.brain import condense_result
from spikes.sdk_worker.runner import HAIKU, SONNET, make_cached_executor, run_sdk_worker

SYSTEM = (
    "You are a code exploration sub-agent. Use the conductor tools to investigate "
    "the workspace and answer concisely with file:line evidence. Stop as soon as you "
    "have enough evidence."
)
QUERY = "What does the OrderService class do and what does create_order call? Use the tools."

CONDENSE_KEYS = {
    "answer", "context_chunks", "files_accessed", "tools_summary",
    "gaps_identified", "confidence", "iterations", "tool_calls_made",
    "duration_ms", "error",
}


async def _run_one(label: str, model: str) -> bool:
    executor, store = make_cached_executor(agent=f"spike-{label}")
    try:
        res = await run_sdk_worker(
            model=model, system_prompt=SYSTEM, user_message=QUERY, executor=executor, max_turns=8,
        )
        # The permanent contract: condense_result must accept the shim unchanged.
        condensed = condense_result(res)
        keys_ok = CONDENSE_KEYS.issubset(condensed.keys())
        ran_ok = res.error is None and res.tool_calls_made >= 1 and bool(res.answer)
        print(f"\n[seam3:{label}] model={model}")
        print(f"  tool_calls={res.tool_calls_made} iterations={res.iterations} "
              f"files={len(res.files_accessed)} dur={res.duration_ms:.0f}ms err={res.error}")
        print(f"  answer[:160]: {res.answer[:160]!r}")
        print(f"  usage: {json.dumps(res.budget_summary.get('usage') if res.budget_summary else None, default=str)[:200]}")
        print(f"  condense_result keys present: {'PASS' if keys_ok else 'FAIL'} "
              f"(missing={CONDENSE_KEYS - set(condensed.keys()) or 'none'})")
        print(f"  worker ran + answered + used tools: {'PASS' if ran_ok else 'FAIL'}")
        print(f"  condensed.files_accessed={condensed.get('files_accessed')}")
        return keys_ok and ran_ok
    finally:
        store.close()


async def main() -> int:
    haiku_ok = await _run_one("haiku", HAIKU)
    sonnet_ok = await _run_one("sonnet", SONNET)
    ok = haiku_ok and sonnet_ok
    print(f"\n[seam3] VERDICT: {'PASS — both models run on SDK; condense_result accepts both unchanged' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
