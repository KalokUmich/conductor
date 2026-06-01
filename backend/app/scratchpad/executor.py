"""CachedToolExecutor — transparent Fact Vault wrapper.

Wraps any ToolExecutor so sub-agents get cache hits on `grep`,
`read_file`, `find_symbol`, etc. without any prompt changes. The caller
doesn't need to know the vault exists — just pass the wrapped executor
where you'd pass the original.

Hit semantics per tool class:

  * **Range tools** (``read_file``, ``git_blame``): range-intersection
    lookup. A request for lines 101-130 hits a cached 100-150 entry; the
    cached content is sliced to the narrower window before return so the
    caller never pays for tokens outside its request.
  * **Symbol lookups** (``find_symbol``, ``find_references``): consult
    ``negative_facts`` before running — if a previous sub-agent already
    verified the symbol doesn't exist, short-circuit with a structured
    "not found" result instead of re-running the grep.
  * **Path-touching tools**: consult ``skip_facts`` — if the file was
    marked unsafe (e.g. Phase 9.18's per-file parse timeout blacklist),
    short-circuit with an error noting the skip reason.
  * **Everything else cacheable**: plain key lookup → hit returns cached
    content, miss runs the inner executor and writes the result.
  * **Non-cacheable tools** (``file_edit``, ``web_*``, ``run_test``):
    pass straight through to the inner executor, never touch the vault.

Failure modes:
  * If the vault raises on put/get (sqlite lock, disk full), we log and
    fall back to running the tool uncached. Caching is an optimisation,
    never a correctness requirement.
  * If the cached content is shape-incompatible with what the caller
    expects (e.g. tool schema changed), the caller handles it the same
    way it handles any tool result — the vault doesn't validate shape.
"""

from __future__ import annotations

import copy
import logging
from typing import Any, Dict, Optional

from app.code_tools.executor import ToolExecutor
from app.code_tools.schemas import ToolResult

from .keys import CACHEABLE_TOOLS, RANGE_TOOLS, build_key, extract_path, extract_range
from .store import FactStore

logger = logging.getLogger(__name__)


class CachedToolExecutor(ToolExecutor):
    """A ToolExecutor that consults a FactStore before delegating.

    Hit path never calls the inner executor. Miss path calls the inner
    executor and writes the result to the vault before returning.
    """

    def __init__(self, inner: ToolExecutor, store: FactStore, *, agent: Optional[str] = None):
        self._inner = inner
        self._store = store
        self._agent = agent
        # Per-instance hit counters for debugging / telemetry export.
        self.stats = {"hits": 0, "misses": 0, "range_hits": 0, "negative_hits": 0, "skipped": 0}

    @property
    def workspace_path(self) -> str:
        return getattr(self._inner, "workspace_path", "")

    async def execute(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        # 1. Non-cacheable → straight through.
        if tool_name not in CACHEABLE_TOOLS:
            return await self._inner.execute(tool_name, params)

        # 2. Path-touching tools check skip-list first. Skipped files short-
        #    circuit so sub-agents don't re-hit pathological tree-sitter inputs.
        path = extract_path(tool_name, params)
        if path:
            try:
                skip_reason = self._store.should_skip(path)
            except Exception as e:  # sqlite hiccup
                logger.warning("FactStore.should_skip failed: %s — falling through", e)
                skip_reason = None
            if skip_reason:
                self.stats["skipped"] += 1
                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    error=f"Skipped per scratchpad: {skip_reason}",
                )

        # 3. Build canonical key. If non-cacheable, this returns None —
        #    shouldn't happen since we checked CACHEABLE_TOOLS above, but
        #    handle defensively.
        key = build_key(tool_name, params)
        if key is None:
            return await self._inner.execute(tool_name, params)

        # 4. Exact-key lookup first — cheapest hit.
        try:
            fact = self._store.get(key)
        except Exception as e:
            logger.warning("FactStore.get failed: %s — falling through", e)
            fact = None
        if fact is not None:
            self.stats["hits"] += 1
            return _fact_to_result(tool_name, fact.content)

        # 5. Range-intersection lookup for read_file / git_blame.
        if tool_name in RANGE_TOOLS and path:
            start, end = extract_range(tool_name, params)
            if start is not None and end is not None:
                try:
                    superset = self._store.range_lookup(tool_name, path, start, end)
                except Exception as e:
                    logger.warning("FactStore.range_lookup failed: %s", e)
                    superset = None
                if superset is not None:
                    # Slice the cached superset down to the requested window
                    sliced = _slice_range_content(
                        superset.content,
                        cached_start=superset.range_start,
                        cached_end=superset.range_end,
                        want_start=start,
                        want_end=end,
                    )
                    if sliced is not None:
                        self.stats["range_hits"] += 1
                        # Persist the narrower slice as its own fact so
                        # subsequent identical requests hit directly (no
                        # re-slice cost). This is a cheap write.
                        self._safe_put(
                            key=key, tool=tool_name, content=sliced,
                            path=path, range_start=start, range_end=end,
                        )
                        return _fact_to_result(tool_name, sliced)

        # 6. Negative cache for find_symbol / find_references — a prior
        #    verify-existence check may have recorded "not found".
        if tool_name in ("find_symbol", "find_references"):
            try:
                neg = self._store.get_negative(key)
            except Exception as e:
                logger.warning("FactStore.get_negative failed: %s", e)
                neg = None
            if neg is not None:
                self.stats["negative_hits"] += 1
                return ToolResult(
                    tool_name=tool_name,
                    success=False,
                    error=(
                        f"Cached negative (confidence={neg.confidence}): {neg.reason or 'symbol not found'}"
                    ),
                    data=None,
                )

        # 7. Miss — delegate to the inner executor, then cache the result.
        self.stats["misses"] += 1
        result = await self._inner.execute(tool_name, params)

        if result.success:
            start, end = extract_range(tool_name, params)
            self._safe_put(
                key=key, tool=tool_name, content=result.data,
                path=path, range_start=start, range_end=end,
            )
        return result

    # --- helpers -----------------------------------------------------------

    def _safe_put(self, *, key: str, tool: str, content: Any, path: Optional[str],
                  range_start: Optional[int], range_end: Optional[int]) -> None:
        try:
            self._store.put(
                key,
                tool=tool,
                content=content,
                path=path,
                range_start=range_start,
                range_end=range_end,
                agent=self._agent,
            )
        except Exception as e:
            # Caching failures never fail the caller — we return the result
            # fine, just uncached.
            logger.warning("FactStore.put failed (key=%s): %s", key, e)


def _fact_to_result(tool_name: str, content: Any) -> ToolResult:
    """Reconstruct a ToolResult from cached content."""
    return ToolResult(tool_name=tool_name, success=True, data=content)


def _slice_range_content(
    content: Any,
    *,
    cached_start: Optional[int],
    cached_end: Optional[int],
    want_start: int,
    want_end: int,
) -> Optional[Any]:
    """Slice a cached range result down to the requested window.

    Handles the two concrete shapes ``read_file`` returns today:
      * ``str`` — the raw file slice with newline-separated lines.
      * ``dict`` with keys like ``content``, ``start_line``, ``end_line``.

    Returns None if we can't safely slice (unknown shape) — caller falls
    back to re-running the tool.
    """
    if cached_start is None or cached_end is None:
        return None
    offset = want_start - cached_start
    length = want_end - want_start + 1
    if offset < 0 or length <= 0:
        return None

    if isinstance(content, str):
        lines = content.splitlines()
        if len(lines) < offset + length:
            # Cached content didn't actually cover the range we thought —
            # trust the cache structure less than the SQL row, skip the slice.
            return None
        return "\n".join(lines[offset:offset + length])

    if isinstance(content, dict):
        inner = content.get("content")
        if isinstance(inner, str):
            lines = inner.splitlines()
            if len(lines) < offset + length:
                return None
            new = copy.deepcopy(content)
            new["content"] = "\n".join(lines[offset:offset + length])
            new["start_line"] = want_start
            new["end_line"] = want_end
            return new

    return None
