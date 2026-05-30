"""Seam 2 (R1): the Fact Vault sits behind the SDK tool. The SAME
CachedToolExecutor instance the SDK @tool handlers delegate to must dedup
repeated reads (exact hit) and satisfy a narrower range from a cached wider
range (range hit).

We drive the executor exactly as runner._build_mcp_tools' handler does (that
handler is a thin `await executor.execute(...)` wrapper), so this proves the
vault behaviour on the proxied path without paying for a model turn.

    cd backend && ../.venv/bin/python -m spikes.sdk_worker.seam2_vault
"""

from __future__ import annotations

import asyncio

from spikes.sdk_worker.runner import make_cached_executor


async def main() -> int:
    executor, store = make_cached_executor()
    try:
        # 1. First read — miss (populates the vault).
        r1 = await executor.execute("read_file", {"path": "app/service.py", "start_line": 1, "end_line": 20})
        # 2. Identical read — exact hit.
        r2 = await executor.execute("read_file", {"path": "app/service.py", "start_line": 1, "end_line": 20})
        # 3. Narrower sub-range of the cached 1-20 window — range hit.
        r3 = await executor.execute("read_file", {"path": "app/service.py", "start_line": 5, "end_line": 10})

        s = executor.stats
        exact_ok = s["hits"] >= 1
        range_ok = s["range_hits"] >= 1
        content_ok = r1.success and r2.success and r3.success

        print(f"[seam2] stats = {s}")
        print(f"[seam2] exact-hit (repeat read)      : {'PASS' if exact_ok else 'FAIL'} (hits={s['hits']})")
        print(f"[seam2] range-hit (5-10 within 1-20) : {'PASS' if range_ok else 'FAIL'} (range_hits={s['range_hits']})")
        print(f"[seam2] all reads succeeded          : {'PASS' if content_ok else 'FAIL'}")

        ok = exact_ok and range_ok and content_ok
        print(f"\n[seam2] VERDICT: {'PASS — vault dedup + range-intersection work behind the SDK tool' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
