"""Hierarchical task telemetry (Step 06d).

Persists the Brain orchestration task tree: one ``task`` row per dispatched
unit — the root coordinator plus each sub-agent — linked ``parent_task_id`` /
``root_task_id``, with a per-task token-usage rollup. This closes the
per-worker cost-recording gap: SDK leaf-worker usage came back via
``ResultMessage.usage`` → ``budget_summary`` but was never persisted, so a
task's full token cost (and its CoT tree) was unqueryable.

Design:
  * Singleton + async SQLAlchemy, mirroring ``AuditLogService`` (engine injected
    in ``main.py`` lifespan).
  * **Telemetry must never crash the Brain.** The module-level ``record_start`` /
    ``record_complete`` helpers no-op when no service is configured (tests,
    local mode) and swallow any DB error.
  * ``start_task`` INSERTs a ``running`` row; ``complete_task`` UPDATEs it with
    the usage rollup + terminal status. Writes are awaited (a single indexed
    row is ~1ms) so parent rows land before child rows reference them.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from ..db.models import TaskRecord

logger = logging.getLogger(__name__)


def usage_from_budget(budget_summary: Optional[Dict[str, Any]]) -> Dict[str, int]:
    """Map a worker ``budget_summary`` onto the task table's usage columns.

    Both engines expose ``total_input_tokens`` / ``total_output_tokens``; the SDK
    path additionally exposes ``cache_read_input_tokens`` /
    ``cache_creation_input_tokens`` (the in-house path has no cache keys → 0).
    """
    bs = budget_summary or {}
    return {
        "input_tokens": int(bs.get("total_input_tokens", 0) or 0),
        "output_tokens": int(bs.get("total_output_tokens", 0) or 0),
        "cache_read_tokens": int(bs.get("cache_read_input_tokens", 0) or 0),
        "cache_creation_tokens": int(bs.get("cache_creation_input_tokens", 0) or 0),
    }


class TaskTelemetryService:
    """Persists the Brain task tree to Postgres (best-effort, async)."""

    _instance: Optional[TaskTelemetryService] = None

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @classmethod
    def get_instance(cls, engine: Optional[AsyncEngine] = None) -> TaskTelemetryService:
        if cls._instance is None:
            if engine is None:
                raise RuntimeError("TaskTelemetryService requires an AsyncEngine on first call")
            cls._instance = cls(engine)
        return cls._instance

    @classmethod
    def instance_or_none(cls) -> Optional[TaskTelemetryService]:
        """Return the configured instance, or None (no DB) — never raises."""
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        cls._instance = None

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------
    async def start_task(
        self,
        *,
        task_id: str,
        root_task_id: str,
        kind: str,
        parent_task_id: Optional[str] = None,
        session_id: Optional[str] = None,
        agent_name: Optional[str] = None,
        query: Optional[str] = None,
        depth: int = 0,
        engine: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        async with self._session_factory() as session:
            session.add(
                TaskRecord(
                    task_id=task_id,
                    parent_task_id=parent_task_id,
                    root_task_id=root_task_id,
                    session_id=session_id,
                    kind=kind,
                    agent_name=agent_name,
                    query=(query or "")[:4000] or None,
                    depth=depth,
                    engine=engine,
                    model=model,
                    status="running",
                )
            )
            await session.commit()

    async def complete_task(
        self,
        *,
        task_id: str,
        status: str,
        budget_summary: Optional[Dict[str, Any]] = None,
        tool_calls: int = 0,
        iterations: int = 0,
        duration_ms: Optional[float] = None,
        error: Optional[str] = None,
    ) -> None:
        usage = usage_from_budget(budget_summary)
        async with self._session_factory() as session:
            await session.execute(
                update(TaskRecord)
                .where(TaskRecord.task_id == task_id)
                .values(
                    status=status,
                    tool_calls=tool_calls,
                    iterations=iterations,
                    duration_ms=duration_ms,
                    error=(error or None),
                    ended_at=datetime.now(UTC),
                    **usage,
                )
            )
            await session.commit()

    # ------------------------------------------------------------------
    # Read path — CoT tree reconstruction
    # ------------------------------------------------------------------
    async def get_tree(self, root_task_id: str) -> List[TaskRecord]:
        """All task rows for a root, ordered depth-first by start time.

        Caller rebuilds the tree from ``parent_task_id``.
        """
        async with self._session_factory() as session:
            stmt = (
                select(TaskRecord)
                .where(TaskRecord.root_task_id == root_task_id)
                .order_by(TaskRecord.depth, TaskRecord.started_at)
            )
            return list((await session.execute(stmt)).scalars().all())


# ---------------------------------------------------------------------------
# Module-level no-op-safe helpers (the Brain calls these)
# ---------------------------------------------------------------------------
async def record_start(**kwargs: Any) -> None:
    """Best-effort task-start record. No-ops when no service / on any error."""
    svc = TaskTelemetryService.instance_or_none()
    if svc is None:
        return
    try:
        await svc.start_task(**kwargs)
    except Exception as exc:  # telemetry must never crash the Brain
        logger.debug("task telemetry start_task failed: %s", exc)


async def record_complete(**kwargs: Any) -> None:
    """Best-effort task-complete record. No-ops when no service / on any error."""
    svc = TaskTelemetryService.instance_or_none()
    if svc is None:
        return
    try:
        await svc.complete_task(**kwargs)
    except Exception as exc:
        logger.debug("task telemetry complete_task failed: %s", exc)
