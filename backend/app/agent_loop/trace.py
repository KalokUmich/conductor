"""Structured session trace for offline analysis.

Records per-iteration metrics (tool calls, latencies, token usage, budget
signals) and persists them for later analysis.

Storage backends:
  * **local** — one JSON file per session in a configurable directory
  * **database** — rows in a ``session_traces`` table via async SQLAlchemy
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Trace dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ToolCallTrace:
    """A single tool invocation within an iteration."""

    tool_name: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    success: bool = True
    result_chars: int = 0
    latency_ms: float = 0.0
    new_files: int = 0
    new_symbols: int = 0
    result_preview: str = ""  # First 500 chars of tool output for debugging


@dataclass
class IterationTrace:
    """One LLM turn (request + response + tool executions)."""

    iteration: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    budget_signal: str = "normal"
    tool_calls: List[ToolCallTrace] = field(default_factory=list)
    llm_latency_ms: float = 0.0
    thinking_text: str = ""
    llm_response_text: str = ""  # LLM's text output (reasoning + answer)


@dataclass
class SessionTrace:
    """Complete trace of a single agent loop run."""

    session_id: str = ""
    query: str = ""
    workspace_path: str = ""
    start_time: float = 0.0
    end_time: float = 0.0
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_tool_calls: int = 0
    iterations: List[IterationTrace] = field(default_factory=list)
    final_answer_chars: int = 0
    error: Optional[str] = None
    budget_summary: Optional[Dict[str, Any]] = None

    @property
    def duration_ms(self) -> float:
        return (self.end_time - self.start_time) * 1000 if self.end_time else 0.0

    def begin(self) -> None:
        """Mark the start of the session."""
        self.start_time = time.monotonic()

    def finish(
        self,
        answer: str = "",
        error: Optional[str] = None,
        budget_summary: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mark the end of the session and set final stats."""
        self.end_time = time.monotonic()
        self.final_answer_chars = len(answer)
        self.error = error
        self.budget_summary = budget_summary
        self.total_input_tokens = sum(it.input_tokens for it in self.iterations)
        self.total_output_tokens = sum(it.output_tokens for it in self.iterations)
        self.total_tool_calls = sum(len(it.tool_calls) for it in self.iterations)

    def add_iteration(self, iteration: IterationTrace) -> None:
        self.iterations.append(iteration)

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["duration_ms"] = self.duration_ms
        return d


# ---------------------------------------------------------------------------
# TraceWriter — pluggable storage backends
# ---------------------------------------------------------------------------


class TraceWriter:
    """Persists session traces.  Constructed once at startup."""

    def __init__(
        self,
        enabled: bool = True,
        backend: str = "local",
        local_path: str = ".conductor/session_traces",
        database_url: str = "",
        engine=None,
    ) -> None:
        self.enabled = enabled
        self.backend = backend
        self.local_path = local_path
        self.database_url = database_url
        self._engine = engine  # async SQLAlchemy engine (shared)

    @classmethod
    def from_settings(cls, settings, engine=None) -> TraceWriter:
        """Create from a ``TraceSettings`` config object."""
        return cls(
            enabled=settings.enabled,
            backend=settings.backend,
            local_path=settings.local_path,
            database_url=settings.database_url,
            engine=engine,
        )

    def save(self, trace: SessionTrace) -> bool:
        """Persist a trace (sync wrapper).  Returns True on success."""
        if not self.enabled or not trace.session_id:
            return False
        try:
            if self.backend == "database" and self._engine:
                # For async engine, caller should use save_async instead
                logger.warning("Use save_async() with async engine; falling back to local")
                return self._save_to_local(trace)
            return self._save_to_local(trace)
        except Exception as exc:
            logger.warning("Failed to save session trace %s: %s", trace.session_id, exc)
            return False

    async def save_async(self, trace: SessionTrace) -> bool:
        """Persist a trace asynchronously.  Returns True on success."""
        if not self.enabled or not trace.session_id:
            return False
        try:
            if self.backend == "database" and self._engine:
                return await self._save_to_db_async(trace)
            return self._save_to_local(trace)
        except Exception as exc:
            logger.warning("Failed to save session trace %s: %s", trace.session_id, exc)
            return False

    # -- Local file backend --------------------------------------------------

    def _save_to_local(self, trace: SessionTrace) -> bool:
        out = Path(self.local_path)
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"{trace.session_id}.json"
        path.write_text(
            json.dumps(trace.to_dict(), indent=2, default=str),
            encoding="utf-8",
        )
        logger.info("Session trace saved: %s", path)
        return True

    # -- Async database backend -----------------------------------------------

    async def _save_to_db_async(self, trace: SessionTrace) -> bool:
        from sqlalchemy import delete, select
        from sqlalchemy.ext.asyncio import async_sessionmaker

        from ..db.models import SessionTraceRecord

        session_factory = async_sessionmaker(self._engine, expire_on_commit=False)
        d = trace.to_dict()
        trace_json = json.dumps(d, default=str)

        async with session_factory() as session:
            # Upsert: delete existing then insert (works with all backends)
            existing = await session.execute(
                select(SessionTraceRecord).where(SessionTraceRecord.session_id == trace.session_id)
            )
            if existing.scalar_one_or_none() is not None:
                await session.execute(
                    delete(SessionTraceRecord).where(SessionTraceRecord.session_id == trace.session_id)
                )

            row = SessionTraceRecord(
                session_id=trace.session_id,
                query=trace.query[:2000],
                workspace_path=trace.workspace_path,
                duration_ms=trace.duration_ms,
                total_input_tokens=trace.total_input_tokens,
                total_output_tokens=trace.total_output_tokens,
                total_tool_calls=trace.total_tool_calls,
                iterations_count=len(trace.iterations),
                final_answer_chars=trace.final_answer_chars,
                error=trace.error,
                trace_json=trace_json,
            )
            session.add(row)
            await session.commit()
            logger.info("Session trace saved to DB: %s", trace.session_id)
            return True
