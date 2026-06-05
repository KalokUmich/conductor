"""CachedToolExecutor tests — Phase 9.15 full."""

from __future__ import annotations

import uuid
from typing import Any, Dict

import pytest

from app.code_tools.executor import ToolExecutor
from app.code_tools.schemas import ToolResult
from app.scratchpad import CachedToolExecutor, FactStore


class CountingInnerExecutor(ToolExecutor):
    """Test double — records calls, returns scripted results."""

    def __init__(self, scripted: Dict[str, Any]) -> None:
        self._scripted = scripted
        self.call_log: list = []

    @property
    def workspace_path(self) -> str:
        return "/fake/ws"

    async def execute(self, tool_name: str, params: Dict[str, Any]) -> ToolResult:
        self.call_log.append((tool_name, dict(params)))
        data = self._scripted.get(tool_name)
        if data is None:
            return ToolResult(tool_name=tool_name, success=False, error="no script")
        return ToolResult(tool_name=tool_name, success=True, data=data)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr("app.scratchpad.store.SCRATCHPAD_ROOT", tmp_path)
    session_id = f"exec-{uuid.uuid4().hex[:8]}"
    s = FactStore.open(session_id, workspace="/fake/ws")
    yield s
    s.delete()


@pytest.fixture
def inner():
    return CountingInnerExecutor(
        {
            "grep": [{"file": "x.py", "line": 10, "text": "match"}],
            "read_file": "line1\nline2\nline3\nline4\nline5",
            "find_symbol": [{"name": "Foo", "file": "x.py", "line": 1}],
            "file_outline": {"classes": ["Foo"], "functions": ["bar"]},
        }
    )


class TestCacheHitMiss:
    @pytest.mark.asyncio
    async def test_first_call_is_miss_second_is_hit(self, store, inner):
        exec_ = CachedToolExecutor(inner, store)

        r1 = await exec_.execute("grep", {"pattern": "foo", "path": "/abs/src"})
        r2 = await exec_.execute("grep", {"pattern": "foo", "path": "/abs/src"})

        assert r1.success and r2.success
        # Inner was called exactly once — second time was served from cache
        assert len(inner.call_log) == 1
        assert exec_.stats == {"hits": 1, "misses": 1, "range_hits": 0, "negative_hits": 0, "skipped": 0}
        # Cached data byte-identical to fresh
        assert r1.data == r2.data

    @pytest.mark.asyncio
    async def test_different_params_miss(self, store, inner):
        exec_ = CachedToolExecutor(inner, store)
        await exec_.execute("grep", {"pattern": "foo", "path": "/abs/src"})
        await exec_.execute("grep", {"pattern": "bar", "path": "/abs/src"})  # different pattern
        assert len(inner.call_log) == 2

    @pytest.mark.asyncio
    async def test_failed_result_not_cached(self, store):
        """Errors should NOT be stored — a transient failure shouldn't
        poison future calls."""

        class FailingInner(CountingInnerExecutor):
            async def execute(self, tool_name, params):
                self.call_log.append((tool_name, params))
                return ToolResult(tool_name=tool_name, success=False, error="transient")

        inner_fail = FailingInner({})
        exec_ = CachedToolExecutor(inner_fail, store)

        await exec_.execute("grep", {"pattern": "x", "path": "/a"})
        await exec_.execute("grep", {"pattern": "x", "path": "/a"})
        # Both calls hit the inner — failure wasn't cached
        assert len(inner_fail.call_log) == 2


class TestNonCacheablePassthrough:
    @pytest.mark.asyncio
    async def test_file_edit_never_cached(self, store):
        inner = CountingInnerExecutor({"file_edit": {"ok": True}})
        exec_ = CachedToolExecutor(inner, store)

        await exec_.execute("file_edit", {"path": "/x.py", "content": "…"})
        await exec_.execute("file_edit", {"path": "/x.py", "content": "…"})
        assert len(inner.call_log) == 2  # never cached
        assert exec_.stats["hits"] == 0

    @pytest.mark.asyncio
    async def test_run_test_never_cached(self, store):
        inner = CountingInnerExecutor({"run_test": {"passed": 1, "failed": 0}})
        exec_ = CachedToolExecutor(inner, store)
        await exec_.execute("run_test", {"path": "tests/"})
        await exec_.execute("run_test", {"path": "tests/"})
        assert len(inner.call_log) == 2


class TestRangeIntersection:
    @pytest.mark.asyncio
    async def test_subset_range_served_from_superset(self, store, tmp_path):
        """Cache a read_file for 100-150, request 101-130 → hit with slice."""
        real_path = str(tmp_path / "file.py")
        # Pre-populate cache with 51 lines of content for range 100-150
        content_51 = "\n".join(f"line{i}" for i in range(100, 151))
        store.put(
            key="v1:read_file:" + real_path + ":100:150",
            tool="read_file",
            content=content_51,
            path=real_path,
            range_start=100,
            range_end=150,
        )

        # Inner is a no-op shouldn't get called
        inner = CountingInnerExecutor({"read_file": "SHOULD NOT SEE THIS"})
        exec_ = CachedToolExecutor(inner, store)

        result = await exec_.execute(
            "read_file",
            {
                "path": real_path,
                "start_line": 101,
                "end_line": 130,
            },
        )

        assert result.success
        assert len(inner.call_log) == 0  # no fall-through to inner
        assert exec_.stats["range_hits"] == 1
        # Sliced content covers 30 lines (101..130 inclusive)
        lines = result.data.splitlines()
        assert len(lines) == 30
        assert lines[0] == "line101"
        assert lines[-1] == "line130"

    @pytest.mark.asyncio
    async def test_partial_overlap_is_miss(self, store, tmp_path):
        real_path = str(tmp_path / "file.py")
        store.put(
            key="v1:read_file:" + real_path + ":10:50",
            tool="read_file",
            content="…",
            path=real_path,
            range_start=10,
            range_end=50,
        )
        inner = CountingInnerExecutor({"read_file": "fresh content"})
        exec_ = CachedToolExecutor(inner, store)

        # Request 40-80 — partial overlap, not full coverage
        result = await exec_.execute(
            "read_file",
            {
                "path": real_path,
                "start_line": 40,
                "end_line": 80,
            },
        )
        assert result.success
        assert len(inner.call_log) == 1  # fell through
        assert result.data == "fresh content"


class TestNegativeCache:
    @pytest.mark.asyncio
    async def test_negative_hit_short_circuits(self, store):
        """Pre-recorded "symbol not found" → return cached negative."""
        from app.scratchpad.keys import build_key

        key = build_key("find_symbol", {"symbol": "Ghost", "path_prefix": "/src"})
        store.put_negative(
            key,
            tool="find_symbol",
            query="Ghost in /src",
            reason="not defined anywhere",
            confidence=0.95,
        )

        inner = CountingInnerExecutor({"find_symbol": [{"name": "some other result"}]})
        exec_ = CachedToolExecutor(inner, store)
        result = await exec_.execute("find_symbol", {"symbol": "Ghost", "path_prefix": "/src"})

        assert result.success is False
        assert "not defined anywhere" in (result.error or "")
        assert len(inner.call_log) == 0
        assert exec_.stats["negative_hits"] == 1


class TestSkipList:
    @pytest.mark.asyncio
    async def test_skipped_path_short_circuits(self, store, tmp_path):
        from os.path import realpath

        path = str(tmp_path / "pathological.tsx")
        store.put_skip(realpath(path), reason="tree-sitter parse timeout 30s", duration_ms=30000)

        inner = CountingInnerExecutor({"file_outline": {}})
        exec_ = CachedToolExecutor(inner, store)
        result = await exec_.execute("file_outline", {"path": path})

        assert result.success is False
        assert "Skipped per scratchpad" in (result.error or "")
        assert "timeout" in (result.error or "").lower()
        assert len(inner.call_log) == 0
        assert exec_.stats["skipped"] == 1


class TestVaultErrorIsolation:
    @pytest.mark.asyncio
    async def test_store_put_failure_does_not_fail_caller(self, store, inner, monkeypatch):
        """If FactStore.put raises, the caller STILL gets a clean result —
        caching is an optimisation, not a correctness dependency."""

        def boom(**kwargs):
            raise RuntimeError("sqlite locked up")

        exec_ = CachedToolExecutor(inner, store)
        monkeypatch.setattr(store, "put", boom)

        result = await exec_.execute("grep", {"pattern": "x", "path": "/a"})
        assert result.success
        assert result.data == [{"file": "x.py", "line": 10, "text": "match"}]
