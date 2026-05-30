"""Seam 4 (R7): local-mode all-MCP exploration quality.

Run the same exploration questions twice on the SAME model: once with built-in
tools DISABLED (only mcp__conductor__* exist — the local-mode/strategy-B case),
once with built-ins allowed. Compare whether the all-MCP worker still explores
well using only our proxied tools. The native-tool-fluency gap (§5.5.4) must be
acceptable. This is a judgment seam — it prints a comparison, not a hard assert.

REAL BEDROCK CALLS. Pre-flight: bash scripts/refactor/check_creds.sh

    cd backend && ../.venv/bin/python -m spikes.sdk_worker.seam4_allmcp
"""

from __future__ import annotations

import asyncio

from spikes.sdk_worker.runner import HAIKU, make_cached_executor, run_sdk_worker

SYSTEM = (
    "You are a code exploration sub-agent. Use the available tools to investigate "
    "the workspace and answer concisely with file:line evidence. Stop when you have "
    "enough evidence."
)
QUESTIONS = [
    "Trace what happens when login() is called in app/controller.py — which functions does it reach?",
    "Where is the OrderService used and what does create_order depend on?",
]


async def _run(label: str, allow_builtins: bool) -> list[dict]:
    rows = []
    for i, q in enumerate(QUESTIONS):
        executor, store = make_cached_executor(agent=f"seam4-{label}-{i}")
        try:
            res = await run_sdk_worker(
                model=HAIKU, system_prompt=SYSTEM, user_message=q,
                executor=executor, max_turns=10, allow_builtins=allow_builtins,
            )
            rows.append({
                "q": i, "tool_calls": res.tool_calls_made, "iters": res.iterations,
                "files": len(res.files_accessed), "dur_ms": int(res.duration_ms),
                "err": res.error, "answer_len": len(res.answer),
                "answer_head": res.answer[:240],
            })
        finally:
            store.close()
    return rows


async def main() -> int:
    print("=== seam4: built-ins DISABLED (all-MCP / local-mode strategy B) ===")
    mcp_only = await _run("mcponly", allow_builtins=False)
    for r in mcp_only:
        print(f"  Q{r['q']}: tools={r['tool_calls']} iters={r['iters']} files={r['files']} "
              f"dur={r['dur_ms']}ms err={r['err']} ans_len={r['answer_len']}")
        print(f"        ans: {r['answer_head']!r}")

    print("\n=== seam4: built-ins ALLOWED (baseline) ===")
    builtins = await _run("builtins", allow_builtins=True)
    for r in builtins:
        print(f"  Q{r['q']}: tools={r['tool_calls']} iters={r['iters']} files={r['files']} "
              f"dur={r['dur_ms']}ms err={r['err']} ans_len={r['answer_len']}")
        print(f"        ans: {r['answer_head']!r}")

    # Judgment summary: did the all-MCP worker actually use our tools + answer?
    mcp_used_tools = all(r["tool_calls"] >= 1 and r["err"] is None and r["answer_len"] > 0 for r in mcp_only)
    print("\n[seam4] all-MCP worker used our tools + answered on every question: "
          f"{'YES' if mcp_used_tools else 'NO'}")
    print("[seam4] VERDICT: judgment — compare the answers above. Acceptable if the "
          "all-MCP answers reach the same files/conclusions as the built-ins run.")
    return 0 if mcp_used_tools else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
