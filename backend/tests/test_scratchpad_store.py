"""FactStore tests — Phase 9.15 full SQLite-backed vault."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.scratchpad import FactStore, sweep_orphans


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Per-test FactStore rooted at tmp_path — no contamination of
    ~/.conductor/scratchpad during tests."""
    monkeypatch.setattr("app.scratchpad.store.SCRATCHPAD_ROOT", tmp_path)
    session_id = f"test-{uuid.uuid4().hex[:8]}"
    s = FactStore.open(session_id, workspace="/fake/ws")
    yield s
    s.delete()


class TestFactStoreBasics:
    def test_open_creates_file(self, store, tmp_path):
        assert store.path.exists()
        assert store.path.parent == tmp_path

    def test_put_then_get_roundtrip(self, store):
        store.put("k1", tool="grep", content={"matches": ["a.py:10", "b.py:20"]})
        fact = store.get("k1")
        assert fact is not None
        assert fact.tool == "grep"
        assert fact.content == {"matches": ["a.py:10", "b.py:20"]}

    def test_get_miss_returns_none(self, store):
        assert store.get("never-written") is None

    def test_put_idempotent_replace(self, store):
        store.put("k1", tool="grep", content={"v": 1})
        store.put("k1", tool="grep", content={"v": 2})
        assert store.get("k1").content == {"v": 2}

    def test_content_is_compressed(self, store):
        """BLOB column should be zlib-compressed bytes, not raw JSON."""
        big = {"data": "x" * 10_000}
        store.put("k1", tool="read_file", content=big)
        # Read raw bytes from sqlite to verify compression happened
        row = store._conn().execute("SELECT content FROM facts WHERE key='k1'").fetchone()
        raw = row["content"]
        assert isinstance(raw, bytes)
        # Compressed is much smaller than the JSON-serialised original
        assert len(raw) < 1000


class TestRangeLookup:
    def test_exact_range_hit(self, store):
        store.put(
            "v1:read_file:/abs/x.py:10:50",
            tool="read_file",
            content="the code",
            path="/abs/x.py",
            range_start=10,
            range_end=50,
        )
        hit = store.range_lookup("read_file", "/abs/x.py", 10, 50)
        assert hit is not None
        assert hit.content == "the code"

    def test_subset_range_hit_superset(self, store):
        """The core feature — request 101-130 should find cached 100-150."""
        store.put(
            "v1:read_file:/abs/x.py:100:150",
            tool="read_file",
            content="lines 100 through 150",
            path="/abs/x.py",
            range_start=100,
            range_end=150,
        )
        hit = store.range_lookup("read_file", "/abs/x.py", 101, 130)
        assert hit is not None
        assert hit.range_start == 100 and hit.range_end == 150

    def test_prefers_narrowest_superset(self, store):
        """Multiple candidates — should return the tightest fit so caller
        slices less wasted content."""
        store.put("k_wide", tool="read_file", content="wide",
                  path="/abs/x.py", range_start=0, range_end=1000)
        store.put("k_tight", tool="read_file", content="tight",
                  path="/abs/x.py", range_start=95, range_end=155)
        hit = store.range_lookup("read_file", "/abs/x.py", 100, 150)
        assert hit is not None
        assert hit.content == "tight"

    def test_no_cover_returns_none(self, store):
        """Cached range that partially overlaps but doesn't cover is a miss."""
        store.put("k1", tool="read_file", content="…",
                  path="/abs/x.py", range_start=10, range_end=50)
        assert store.range_lookup("read_file", "/abs/x.py", 40, 80) is None
        assert store.range_lookup("read_file", "/abs/x.py", 60, 100) is None

    def test_wrong_path_is_miss(self, store):
        store.put("k1", tool="read_file", content="…",
                  path="/abs/x.py", range_start=1, range_end=100)
        assert store.range_lookup("read_file", "/abs/other.py", 10, 50) is None

    def test_wrong_tool_is_miss(self, store):
        store.put("k1", tool="read_file", content="…",
                  path="/abs/x.py", range_start=1, range_end=100)
        assert store.range_lookup("git_blame", "/abs/x.py", 10, 50) is None


class TestNegativeFacts:
    def test_put_then_get(self, store):
        store.put_negative(
            "v1:find_symbol:OptimizedCursorPaginator:",
            tool="find_symbol",
            query="OptimizedCursorPaginator in sentry",
            reason="NOT DEFINED anywhere in codebase",
            confidence=0.95,
        )
        neg = store.get_negative("v1:find_symbol:OptimizedCursorPaginator:")
        assert neg is not None
        assert neg.reason == "NOT DEFINED anywhere in codebase"
        assert neg.confidence == 0.95

    def test_miss_returns_none(self, store):
        assert store.get_negative("never-cached") is None


class TestSkipFacts:
    def test_put_then_check(self, store):
        store.put_skip(
            "/abs/path/to/pathological.tsx",
            reason="tree-sitter parse timeout 30s",
            duration_ms=30000,
        )
        assert store.should_skip("/abs/path/to/pathological.tsx") == "tree-sitter parse timeout 30s"

    def test_miss_returns_none(self, store):
        assert store.should_skip("/never/skipped") is None


class TestStats:
    def test_counts(self, store):
        store.put("k1", tool="grep", content=[])
        store.put("k2", tool="read_file", content="")
        store.put_negative("n1", tool="find_symbol", query="X")
        store.put_skip("/p1", reason="timeout")

        stats = store.stats()
        assert stats == {
            "facts": 2,
            "negative_facts": 1,
            "skip_facts": 1,
            "existence_facts": 0,
            "missing_symbols": 0,
            "plan_entries": 0,
        }


class TestConcurrency:
    def test_concurrent_writes_dont_corrupt(self, tmp_path, monkeypatch):
        """WAL mode + per-thread connections → 8 threads write 100 facts each,
        all survive, no lost writes or corruption."""
        monkeypatch.setattr("app.scratchpad.store.SCRATCHPAD_ROOT", tmp_path)
        store = FactStore.open(f"ct-{uuid.uuid4().hex[:8]}")
        try:
            def writer(tid: int):
                for i in range(100):
                    store.put(
                        f"t{tid}:k{i}",
                        tool="grep",
                        content={"tid": tid, "i": i},
                    )

            with ThreadPoolExecutor(max_workers=8) as pool:
                for r in [pool.submit(writer, tid) for tid in range(8)]:
                    r.result()

            assert store.stats()["facts"] == 800

            # Spot-check that a few reads come back intact
            f = store.get("t3:k42")
            assert f.content == {"tid": 3, "i": 42}
        finally:
            store.delete()

    def test_delete_is_idempotent(self, store):
        store.delete()
        store.delete()  # must not raise


class TestTaskIdMeta:
    """task_id (set by PRBrainOrchestrator) is persisted in meta so
    ``python -m app.scratchpad list`` can tell concurrent PRs apart."""

    def test_task_id_written_to_meta(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.scratchpad.store.SCRATCHPAD_ROOT", tmp_path)
        s = FactStore.open("task-001", workspace="/ws", task_id="ado-proj-pr-42")
        try:
            row = s._conn().execute(
                "SELECT v FROM meta WHERE k = 'task_id'"
            ).fetchone()
            assert row["v"] == "ado-proj-pr-42"
        finally:
            s.delete()

    def test_task_id_optional(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.scratchpad.store.SCRATCHPAD_ROOT", tmp_path)
        s = FactStore.open("task-002", workspace="/ws")
        try:
            row = s._conn().execute(
                "SELECT v FROM meta WHERE k = 'task_id'"
            ).fetchone()
            assert row["v"] == ""
        finally:
            s.delete()


class TestSweepOrphans:
    def test_removes_old_files(self, tmp_path, monkeypatch):
        import os
        import time

        monkeypatch.setattr("app.scratchpad.store.SCRATCHPAD_ROOT", tmp_path)
        # Create a stale and a fresh session
        stale = tmp_path / "stale.sqlite"
        fresh = tmp_path / "fresh.sqlite"
        stale.write_bytes(b"SQLite format 3\x00")
        fresh.write_bytes(b"SQLite format 3\x00")
        old = time.time() - (48 * 3600)  # 48h ago
        os.utime(stale, (old, old))

        removed = sweep_orphans(max_age_hours=24)
        assert stale in removed
        assert fresh not in removed
        assert not stale.exists()
        assert fresh.exists()

    def test_noop_when_root_missing(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.scratchpad.store.SCRATCHPAD_ROOT", tmp_path / "does-not-exist")
        assert sweep_orphans() == []


class TestPlanMemory:
    """P4 — per-dispatch plan entries persisted so coordinator sees a
    recap across replan rounds."""

    def test_empty_on_open(self, store):
        assert store.count_plan_entries() == 0
        assert store.iter_plan_entries() == []

    def test_put_then_iter(self, store):
        store.put_plan_entry(
            dispatch_index=1,
            mode="role",
            role="security",
            scope="src/auth/oauth.py:40-120",
            success_criteria="Flag token leaks or state-mismatch.",
            reason="PKCE migration",
        )
        entries = store.iter_plan_entries()
        assert len(entries) == 1
        assert entries[0].dispatch_index == 1
        assert entries[0].role == "security"
        assert entries[0].reason == "PKCE migration"
        assert store.count_plan_entries() == 1

    def test_entries_sorted_by_dispatch_index(self, store):
        for idx in (3, 1, 2):
            store.put_plan_entry(
                dispatch_index=idx,
                mode="checks",
                role=None,
                scope=f"file{idx}.py",
                success_criteria="criteria",
                reason=None,
            )
        entries = store.iter_plan_entries()
        assert [e.dispatch_index for e in entries] == [1, 2, 3]

    def test_replace_on_same_dispatch_index(self, store):
        store.put_plan_entry(
            dispatch_index=1, mode="checks", role=None,
            scope="v1", success_criteria="c1", reason=None,
        )
        store.put_plan_entry(
            dispatch_index=1, mode="role", role="correctness",
            scope="v2", success_criteria="c2", reason="updated",
        )
        entries = store.iter_plan_entries()
        assert len(entries) == 1
        assert entries[0].scope == "v2"
        assert entries[0].role == "correctness"

    def test_stats_includes_plan_entries(self, store):
        store.put_plan_entry(
            dispatch_index=1, mode="role", role="security",
            scope="s", success_criteria="c", reason=None,
        )
        store.put_plan_entry(
            dispatch_index=2, mode="checks", role=None,
            scope="s", success_criteria="c", reason=None,
        )
        assert store.stats()["plan_entries"] == 2

    def test_long_fields_truncated(self, store):
        long = "x" * 1000
        store.put_plan_entry(
            dispatch_index=1, mode="role", role=None,
            scope=long, success_criteria=long, reason=long,
        )
        entries = store.iter_plan_entries()
        assert len(entries[0].scope) == 500
        assert len(entries[0].success_criteria) == 500
        assert len(entries[0].reason) == 500
