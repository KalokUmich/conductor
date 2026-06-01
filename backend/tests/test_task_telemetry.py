"""Tests for TaskTelemetryService (Step 06d) — in-memory aiosqlite, no Postgres.

Covers the usage→column mapping, the no-op-safe module helpers, per-task usage
rollup, parent/child tree reconstruction, and terminal-status recording.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from app.agent_loop.task_telemetry import (
    TaskTelemetryService,
    record_complete,
    record_start,
    usage_from_budget,
)
from app.db.models import Base


# ---------------------------------------------------------------------------
# Pure mapping
# ---------------------------------------------------------------------------
def test_usage_from_budget_maps_both_engines():
    # SDK path (has cache keys)
    assert usage_from_budget(
        {"total_input_tokens": 200, "total_output_tokens": 40, "cache_read_input_tokens": 9000,
         "cache_creation_input_tokens": 12}
    ) == {"input_tokens": 200, "output_tokens": 40, "cache_read_tokens": 9000, "cache_creation_tokens": 12}
    # in-house path (no cache keys) + None
    assert usage_from_budget({"total_input_tokens": 5, "total_output_tokens": 1}) == {
        "input_tokens": 5, "output_tokens": 1, "cache_read_tokens": 0, "cache_creation_tokens": 0,
    }
    assert usage_from_budget(None) == {
        "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_creation_tokens": 0,
    }


# ---------------------------------------------------------------------------
# No-op safety (telemetry must never crash the Brain)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_record_helpers_noop_without_instance():
    TaskTelemetryService.reset_instance()
    # Must not raise when no service is configured (tests / local mode).
    await record_start(task_id="t", root_task_id="t", kind="root")
    await record_complete(task_id="t", status="done")


# ---------------------------------------------------------------------------
# Service against a real (in-memory) async engine
# ---------------------------------------------------------------------------
@pytest_asyncio.fixture
async def svc():
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield TaskTelemetryService(engine)
    await engine.dispose()
    TaskTelemetryService.reset_instance()


@pytest.mark.asyncio
async def test_start_complete_and_usage_rollup(svc):
    await svc.start_task(task_id="root1", root_task_id="root1", kind="root", agent_name="brain")
    await svc.start_task(
        task_id="c1", parent_task_id="root1", root_task_id="root1",
        kind="sub_agent", agent_name="explore_x", depth=1, engine="sdk", model="haiku",
    )
    await svc.complete_task(
        task_id="c1", status="done",
        budget_summary={"total_input_tokens": 200, "total_output_tokens": 40, "cache_read_input_tokens": 9000},
        tool_calls=5, iterations=3, duration_ms=1234.0,
    )

    tree = await svc.get_tree("root1")
    assert {t.task_id for t in tree} == {"root1", "c1"}
    c1 = next(t for t in tree if t.task_id == "c1")
    assert c1.parent_task_id == "root1"
    assert c1.status == "done"
    assert (c1.input_tokens, c1.output_tokens, c1.cache_read_tokens) == (200, 40, 9000)
    assert (c1.tool_calls, c1.iterations, c1.duration_ms) == (5, 3, 1234.0)
    assert c1.engine == "sdk" and c1.depth == 1
    # ordered by depth → root anchor first
    assert tree[0].task_id == "root1"


@pytest.mark.asyncio
async def test_cost_source_discriminates_sdk_vs_computed(svc):
    """SDK summaries carry ``raw_usage`` → source 'sdk'; in-house carry
    ``usd_usage_ratio`` → source 'computed'; no cost → NULL."""
    await svc.start_task(task_id="r", root_task_id="r", kind="root")
    # SDK leaf: authoritative cost + raw_usage block
    await svc.start_task(task_id="sdk1", root_task_id="r", kind="sub_agent", engine="sdk")
    await svc.complete_task(
        task_id="sdk1", status="done",
        budget_summary={"total_input_tokens": 100, "total_cost_usd": 0.05, "raw_usage": {"x": 1}},
    )
    # in-house coordinator: computed cost, usd_usage_ratio, no raw_usage
    await svc.start_task(task_id="ih1", root_task_id="r", kind="coordinator", engine="in_house")
    await svc.complete_task(
        task_id="ih1", status="done",
        budget_summary={"total_input_tokens": 5000, "total_cost_usd": 0.02, "usd_usage_ratio": 0.1},
    )
    # no cost info at all → NULL source, 0 cost
    await svc.start_task(task_id="none1", root_task_id="r", kind="sub_agent")
    await svc.complete_task(task_id="none1", status="done", budget_summary={"total_input_tokens": 10})

    rows = {t.task_id: t for t in await svc.get_tree("r")}
    assert (rows["sdk1"].cost_usd, rows["sdk1"].cost_source) == (0.05, "sdk")
    assert (rows["ih1"].cost_usd, rows["ih1"].cost_source) == (0.02, "computed")
    assert (rows["none1"].cost_usd, rows["none1"].cost_source) == (0.0, None)


@pytest.mark.asyncio
async def test_two_level_tree_links_parent(svc):
    # root → c1 (coordinator) → g1 (leaf): the depth-2 dispatch our discriminator allows
    await svc.start_task(task_id="root", root_task_id="root", kind="root")
    await svc.start_task(task_id="c1", parent_task_id="root", root_task_id="root", kind="coordinator", depth=1)
    await svc.start_task(task_id="g1", parent_task_id="c1", root_task_id="root", kind="sub_agent", depth=2)
    tree = await svc.get_tree("root")
    by_id = {t.task_id: t for t in tree}
    assert by_id["g1"].parent_task_id == "c1"
    assert by_id["c1"].parent_task_id == "root"
    assert by_id["root"].parent_task_id is None
    assert [t.task_id for t in tree] == ["root", "c1", "g1"]  # depth-ordered


@pytest.mark.asyncio
async def test_record_helpers_write_via_singleton(svc):
    TaskTelemetryService._instance = svc  # wire the module helpers to this service
    try:
        await record_start(task_id="r", root_task_id="r", kind="root")
        await record_complete(
            task_id="r", status="done",
            budget_summary={"total_input_tokens": 10, "total_output_tokens": 2},
        )
        tree = await svc.get_tree("r")
        assert len(tree) == 1
        assert tree[0].input_tokens == 10 and tree[0].status == "done"
    finally:
        TaskTelemetryService.reset_instance()


@pytest.mark.asyncio
async def test_complete_error_status(svc):
    await svc.start_task(task_id="e1", root_task_id="e1", kind="sub_agent")
    await svc.complete_task(task_id="e1", status="error", error="boom", duration_ms=50.0)
    tree = await svc.get_tree("e1")
    assert tree[0].status == "error" and tree[0].error == "boom"
