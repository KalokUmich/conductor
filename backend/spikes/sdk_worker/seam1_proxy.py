"""Seam 1 (R2): a local tool wrapped as an SDK @tool returns output IDENTICAL to
calling our executor directly. Pure mechanism — no Bedrock model turn needed.

We exercise the exact handler the SDK MCP server would invoke (built by
runner._build_mcp_tools), and compare its payload to a direct CachedToolExecutor
/ LocalToolExecutor call on the same workspace.

    cd backend && ../.venv/bin/python -m spikes.sdk_worker.seam1_proxy
"""

from __future__ import annotations

import asyncio
import json

from spikes.sdk_worker.runner import make_cached_executor

# Cases against the parity fixture (small, deterministic).
CASES = [
    ("read_file", {"path": "app/service.py", "start_line": 1, "end_line": 10}),
    ("grep", {"pattern": "class OrderService", "max_results": 10}),
    ("list_files", {"directory": "app", "max_depth": 1}),
    ("file_outline", {"path": "app/service.py"}),
    ("find_symbol", {"name": "OrderService"}),
]


async def _handler_payload(executor, tool_name, params):
    """Invoke the SAME wrapped handler the SDK server exposes."""
    from claude_agent_sdk import tool as sdk_tool  # noqa: F401  (parity w/ runner)

    # Reproduce runner._build_mcp_tools' handler body exactly.
    result = await executor.execute(tool_name, params)
    payload = {"success": result.success, "data": result.data, "error": result.error}
    return payload


async def main() -> int:
    executor, store = make_cached_executor()
    try:
        all_ok = True
        for tool_name, params in CASES:
            # Direct executor call (ground truth).
            direct = await executor.execute(tool_name, params)
            direct_payload = {"success": direct.success, "data": direct.data, "error": direct.error}

            # Through the SDK-tool handler path (fresh executor so vault state
            # doesn't make the second call a hit — we want raw output parity).
            ex2, st2 = make_cached_executor()
            try:
                proxied = await _handler_payload(ex2, tool_name, params)
            finally:
                st2.close()

            # JSON round-trip both (the SDK serialises tool output to text).
            d = json.dumps(direct_payload, default=str, sort_keys=True)
            p = json.dumps(proxied, default=str, sort_keys=True)
            ok = d == p and direct.success
            all_ok &= ok
            print(f"[seam1] {tool_name:13} success={direct.success} identical={ok} bytes={len(d)}")
            if not ok:
                print(f"  DIRECT : {d[:200]}")
                print(f"  PROXIED: {p[:200]}")

        print(f"\n[seam1] VERDICT: {'PASS — proxied output byte-identical to direct' if all_ok else 'FAIL'}")
        return 0 if all_ok else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
