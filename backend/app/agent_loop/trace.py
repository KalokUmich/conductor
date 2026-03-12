"""Structured session trace for offline analysis.

Records per-iteration metrics (tool calls, latencies, token usage, budget
signals) and persists them for later analysis.

Storage backends:
  * **local** — one JSON file per session in a configurable directory
  * **database** — rows in a ``session_traces`` table (SQLite or PostgreSQL)

The trace is designed for:
  * Offline evaluation of agent behaviour
  * Prompt optimization (which tools waste tokens?)
  * Query pattern analysis (what query types cost the most?)

Reference: RAG-Gym — Process Supervision for Agents
https://arxiv.org/abs/2502.13957
"""
from __future__ import annotations

import json
import logging
import sqlite3
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
    ) -> None:
        self.enabled = enabled
        self.backend = backend
        self.local_path = local_path
        self.database_url = database_url
        self._db_initialized = False

    @classmethod
    def from_settings(cls, settings) -> "TraceWriter":
        """Create from a ``TraceSettings`` config object."""
        return cls(
            enabled=settings.enabled,
            backend=settings.backend,
            local_path=settings.local_path,
            database_url=settings.database_url,
        )

    def save(self, trace: SessionTrace) -> bool:
        """Persist a trace.  Returns True on success."""
        if not self.enabled or not trace.session_id:
            return False
        try:
            if self.backend == "database" and self.database_url:
                return self._save_to_db(trace)
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

    # -- Database backend ----------------------------------------------------

    _CREATE_TABLE_SQL = """\
CREATE TABLE IF NOT EXISTS session_traces (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      TEXT UNIQUE NOT NULL,
    query           TEXT,
    workspace_path  TEXT,
    duration_ms     REAL,
    total_input_tokens  INTEGER,
    total_output_tokens INTEGER,
    total_tool_calls    INTEGER,
    iterations_count    INTEGER,
    final_answer_chars  INTEGER,
    error           TEXT,
    trace_json      TEXT,
    created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""

    _INSERT_SQL = """\
INSERT OR REPLACE INTO session_traces (
    session_id, query, workspace_path, duration_ms,
    total_input_tokens, total_output_tokens, total_tool_calls,
    iterations_count, final_answer_chars, error, trace_json
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
"""

    def _get_db_connection(self) -> sqlite3.Connection:
        """Parse database_url and return a sqlite3 connection.

        Supports:
          * ``sqlite:///path/to/file.db`` — file-based SQLite
          * ``sqlite:///traces.db``       — relative path
          * ``/path/to/file.db``          — bare path (treated as SQLite)
        """
        url = self.database_url
        if url.startswith("sqlite:///"):
            db_path = url[len("sqlite:///"):]
        elif url.startswith("sqlite://"):
            db_path = url[len("sqlite://"):]
        else:
            # Bare path — treat as SQLite file
            db_path = url

        # Ensure parent directory exists
        p = Path(db_path)
        p.parent.mkdir(parents=True, exist_ok=True)

        return sqlite3.connect(str(p))

    def _ensure_table(self, conn: sqlite3.Connection) -> None:
        if not self._db_initialized:
            conn.execute(self._CREATE_TABLE_SQL)
            conn.commit()
            self._db_initialized = True

    def _save_to_db(self, trace: SessionTrace) -> bool:
        conn = self._get_db_connection()
        try:
            self._ensure_table(conn)
            d = trace.to_dict()
            conn.execute(self._INSERT_SQL, (
                trace.session_id,
                trace.query[:2000],  # cap query length
                trace.workspace_path,
                trace.duration_ms,
                trace.total_input_tokens,
                trace.total_output_tokens,
                trace.total_tool_calls,
                len(trace.iterations),
                trace.final_answer_chars,
                trace.error,
                json.dumps(d, default=str),
            ))
            conn.commit()
            logger.info("Session trace saved to DB: %s", trace.session_id)
            return True
        finally:
            conn.close()
