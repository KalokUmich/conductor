"""SQLite-backed Fact Vault — task-scoped short-term memory.

One SQLite file per PR review session at
``~/.conductor/scratchpad/{session_id}.sqlite``. WAL mode lets the worker
threads of ``execute_tool`` insert concurrently without blocking readers.

Tables:
  * ``facts`` — one row per cached tool call. ``content`` is zlib-compressed
    JSON. ``range_start`` / ``range_end`` are populated for line-range tools
    so range-intersection lookup runs as a single SQL query.
  * ``negative_facts`` — "X was verified NOT to exist" entries so Haiku
    stops hallucinating the same phantom symbol. Pairs with the
    verify-existence rule in the sub-agent skill.
  * ``skip_facts`` — "this file is too expensive to parse, don't touch" —
    populated by Phase 9.18's per-file parse timeout. Other tools check
    this list before executing.
  * ``meta`` — session metadata (workspace, started_ts, …).

Lifecycle: created by ``FactStore.open(session_id)`` at PR-review start,
deleted by the caller (Phase 9.17 ``on_synthesize_complete`` hook) when
the review finishes. An orphan sweep on backend startup removes any
session DBs older than 24h (not yet implemented).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

logger = logging.getLogger(__name__)

SCRATCHPAD_ROOT = Path.home() / ".conductor" / "scratchpad"

_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS facts (
        key         TEXT PRIMARY KEY,
        tool        TEXT NOT NULL,
        path        TEXT,
        range_start INTEGER,
        range_end   INTEGER,
        content     BLOB NOT NULL,
        agent       TEXT,
        ts_written  INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_facts_tool_path ON facts(tool, path, range_start, range_end)",
    """
    CREATE TABLE IF NOT EXISTS negative_facts (
        key        TEXT PRIMARY KEY,
        tool       TEXT NOT NULL,
        query      TEXT NOT NULL,
        reason     TEXT,
        confidence REAL,
        ts_written INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS skip_facts (
        abs_path    TEXT PRIMARY KEY,
        reason      TEXT NOT NULL,
        duration_ms INTEGER,
        ts_written  INTEGER NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS existence_facts (
        -- PR Brain v2 Phase 2: Verify. Before planning logic checks, the
        -- coordinator dispatches ONE existence-verification worker that
        -- records, for each symbol/method/import/attribute this PR
        -- introduces, whether it actually exists in the codebase.
        --
        -- These facts are CONSUMED by Brain's planning step (to decide
        -- whether to dispatch a logic check on a symbol vs flag an
        -- ImportError/NameError/TypeError) and by sub-agent verify-
        -- existence rule (to avoid re-doing grep work already answered).
        --
        -- Semantic: "the codebase asserts that X [exists | is missing]"
        -- — fundamentally different from ``facts`` (tool-call cache)
        -- which is "the last grep for X returned these rows".
        symbol_name    TEXT NOT NULL,
        symbol_kind    TEXT NOT NULL,  -- class | method | function | attribute | import
        referenced_at  TEXT NOT NULL,  -- file:line where the NEW usage lives
        exists_flag    INTEGER NOT NULL,  -- 0 | 1  (column name avoids SQL reserved word 'exists')
        evidence       TEXT,
        signature_info TEXT,  -- JSON: param mismatch detail for kind=method
        ts_written     INTEGER NOT NULL,
        PRIMARY KEY (symbol_name, referenced_at)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_existence_missing
        ON existence_facts(exists_flag) WHERE exists_flag = 0
    """,
    """
    CREATE TABLE IF NOT EXISTS plan_memory (
        -- PR Brain v2 P4: coordinator's Plan-phase decisions persisted
        -- per-dispatch. Injected back into tool results on the 3rd+
        -- dispatch so Brain's in-context plan history doesn't drift as
        -- the review loop grows. Auto-populated by `_dispatch_verify`.
        dispatch_index INTEGER NOT NULL,     -- 1-based per session
        mode           TEXT NOT NULL,        -- 'role' | 'checks' | 'combined'
        role           TEXT,                 -- factory role if role mode
        scope          TEXT NOT NULL,        -- compact scope descriptor
        success_criteria TEXT NOT NULL,
        reason         TEXT,                 -- Brain's direction_hint/context
        ts_written     INTEGER NOT NULL,
        PRIMARY KEY (dispatch_index)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS notes (
        -- Phase 9.9.3: sub-agent structured note-taking. Sub-agents
        -- write scratch observations here mid-investigation; notes
        -- survive the 3-turn context-clearing policy that would
        -- otherwise truncate tool_results and thinking.
        --
        -- The note is keyed by (agent, topic) so an agent can refine
        -- or overwrite its own prior note without littering. Reads
        -- are by agent name (for replay inside the SAME agent's
        -- later iterations) or by topic (for coordinator / sibling
        -- agents to peek at what this agent is noticing).
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        agent          TEXT NOT NULL,    -- worker role / template name
        topic          TEXT NOT NULL,    -- short slug identifying the note
        content        TEXT NOT NULL,    -- the actual observation (≤4K chars)
        file_hint      TEXT,             -- optional file path the note is about
        ts_written     INTEGER NOT NULL,
        UNIQUE(agent, topic)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_notes_agent
        ON notes(agent)
    """,
    """
    CREATE TABLE IF NOT EXISTS meta (
        k TEXT PRIMARY KEY,
        v TEXT NOT NULL
    )
    """,
]


@dataclass
class Fact:
    """A cached tool-call result."""

    key: str
    tool: str
    path: Optional[str]
    range_start: Optional[int]
    range_end: Optional[int]
    content: Any  # decoded from JSON
    agent: Optional[str]
    ts_written: int


@dataclass
class NegativeFact:
    """A verified absence (symbol not found, file missing, etc.)."""

    key: str
    tool: str
    query: str
    reason: Optional[str]
    confidence: Optional[float]
    ts_written: int


@dataclass
class PlanEntry:
    """P4: one dispatch decision the coordinator made, persisted so later
    tool-result returns can include a compact plan recap and prevent
    Plan→Synthesize drift as context fills."""

    dispatch_index: int
    mode: str                      # 'role' | 'checks' | 'combined'
    role: Optional[str]
    scope: str                     # compact "file1:120-160, file2" descriptor
    success_criteria: str
    reason: Optional[str]
    ts_written: int


@dataclass
class ExistenceFact:
    """PR Brain v2 — authoritative fact about whether a symbol exists.

    Produced by a single Phase-2 verification sub-agent at the start of a
    PR review; consumed by the coordinator when planning investigations
    and by later sub-agents to avoid re-doing existence grep work.
    """

    symbol_name: str
    symbol_kind: str           # class | method | function | attribute | import
    referenced_at: str         # "file.py:12"
    exists_flag: bool
    evidence: Optional[str]
    signature_info: Optional[Dict[str, Any]]  # for kind=method: param mismatches
    ts_written: int


@dataclass
class Note:
    """Phase 9.9.3 — a sub-agent's scratch observation persisted for its
    own later iterations (surviving the 3-turn context clearing) and
    cross-agent peeking.

    Keyed by (agent, topic) — an agent can refine or overwrite its own
    prior note without littering. The topic is a short slug ("auth_flow",
    "validate_fn_signature") so the same agent can maintain a handful of
    coherent running observations.
    """

    id: int
    agent: str
    topic: str
    content: str
    file_hint: Optional[str]
    ts_written: int


class FactStore:
    """SQLite-backed per-session fact vault.

    Thread-safe via per-thread connections — SQLite connections can't be
    shared across threads by default, so we cache one per thread in TLS.
    The underlying file uses WAL mode so writes from different threads
    (via their own connections) don't block readers.

    Usage:
        store = FactStore.open("session-abc123", workspace="/home/user/repo")
        hit = store.get("v1:grep:foo::py::")
        if hit is None:
            result = run_the_tool()
            store.put("v1:grep:foo::py::", tool="grep", content=result)
        store.close()  # closes all thread-local connections
    """

    def __init__(self, db_path: Path, session_id: str):
        self._db_path = db_path
        self._session_id = session_id
        self._tls = threading.local()
        # An "owner" connection used for schema init + close(), kept
        # around so we can explicitly close it rather than relying on GC.
        self._owner_conn: Optional[sqlite3.Connection] = None

    # --- lifecycle ---------------------------------------------------------

    @classmethod
    def open(
        cls,
        session_id: str,
        *,
        workspace: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> FactStore:
        """Create (or reopen) the session DB and initialise schema.

        ``task_id`` is free-form metadata identifying which PR / eval case /
        external task this vault belongs to (e.g. ``"ado-pr-12345"`` or
        ``"greptile-sentry-006"``). Concurrent PR reviews each get their own
        session_id → own SQLite file, so facts are *physically* isolated;
        task_id is the human-readable label that lets you tell the files
        apart in activity logs or ``python -m app.scratchpad list``.
        """
        SCRATCHPAD_ROOT.mkdir(parents=True, exist_ok=True)
        db_path = SCRATCHPAD_ROOT / f"{session_id}.sqlite"
        store = cls(db_path, session_id)
        conn = store._conn()
        # Keep a reference to this init-time connection so close() can
        # explicitly shut it and we don't leak until GC.
        store._owner_conn = conn
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")  # durability vs speed tradeoff; WAL + NORMAL is crash-safe
        for ddl in _SCHEMA:
            conn.execute(ddl)
        conn.execute(
            "INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?), (?, ?), (?, ?)",
            (
                "session_id", session_id,
                "workspace", workspace or "",
                "task_id", task_id or "",
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)",
            ("started_ms", str(int(time.time() * 1000))),
        )
        conn.commit()
        logger.info(
            "FactStore opened: %s (session=%s, task=%s)",
            db_path, session_id, task_id or "-",
        )
        return store

    @property
    def path(self) -> Path:
        return self._db_path

    @property
    def session_id(self) -> str:
        return self._session_id

    def close(self) -> None:
        """Close the thread-local connection for the CURRENT thread, plus
        the owner connection. Other threads' connections will be closed
        on their next GC pass — adequate since we're about to delete the
        file anyway.
        """
        import contextlib

        conn = getattr(self._tls, "conn", None)
        if conn is not None:
            with contextlib.suppress(sqlite3.Error):
                conn.close()
            self._tls.conn = None
        if self._owner_conn is not None and self._owner_conn is not conn:
            with contextlib.suppress(sqlite3.Error):
                self._owner_conn.close()
            self._owner_conn = None

    def delete(self) -> None:
        """Close and remove the SQLite file + WAL sidecars."""
        import contextlib

        self.close()
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self._db_path) + suffix)
            try:
                with contextlib.suppress(FileNotFoundError):
                    p.unlink()
            except OSError as e:
                logger.warning("Failed to remove %s: %s", p, e)

    # --- connection --------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        """Get-or-create the thread-local SQLite connection.

        SQLite forbids cross-thread use of a single connection unless we
        pass ``check_same_thread=False`` AND serialise writes ourselves.
        Per-thread connections are simpler and let WAL do its job.
        """
        conn = getattr(self._tls, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self._db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            self._tls.conn = conn
        return conn

    # --- facts: put/get/range_lookup ---------------------------------------

    def put(
        self,
        key: str,
        *,
        tool: str,
        content: Any,
        path: Optional[str] = None,
        range_start: Optional[int] = None,
        range_end: Optional[int] = None,
        agent: Optional[str] = None,
    ) -> None:
        """Store a fact. Idempotent — REPLACE semantics on key conflict."""
        payload = zlib.compress(json.dumps(content, default=str).encode("utf-8"), level=6)
        self._conn().execute(
            """
            INSERT OR REPLACE INTO facts
              (key, tool, path, range_start, range_end, content, agent, ts_written)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                key,
                tool.lower(),
                path,
                range_start,
                range_end,
                payload,
                agent,
                int(time.time() * 1000),
            ),
        )
        self._conn().commit()

    def get(self, key: str) -> Optional[Fact]:
        """Exact-key lookup. Returns None on miss."""
        row = self._conn().execute(
            "SELECT * FROM facts WHERE key = ?", (key,)
        ).fetchone()
        return _row_to_fact(row) if row else None

    def range_lookup(
        self,
        tool: str,
        path: str,
        start: int,
        end: int,
    ) -> Optional[Fact]:
        """Find a cached fact that RANGE-CONTAINS the requested window.

        Request ``read_file(path=X, start=101, end=130)``. A cached entry
        with range 100-150 satisfies it — callers slice the narrower
        window from the cached content themselves.

        Among candidates, prefer the narrowest superset (smallest
        ``end - start``) to minimise token waste on the caller side.
        """
        if start is None or end is None:
            return None
        row = self._conn().execute(
            """
            SELECT * FROM facts
             WHERE tool = ? AND path = ?
               AND range_start IS NOT NULL AND range_end IS NOT NULL
               AND range_start <= ? AND range_end >= ?
             ORDER BY (range_end - range_start) ASC
             LIMIT 1
            """,
            (tool.lower(), path, start, end),
        ).fetchone()
        return _row_to_fact(row) if row else None

    # --- negative facts ----------------------------------------------------

    def put_negative(
        self,
        key: str,
        *,
        tool: str,
        query: str,
        reason: Optional[str] = None,
        confidence: Optional[float] = None,
    ) -> None:
        self._conn().execute(
            """
            INSERT OR REPLACE INTO negative_facts
              (key, tool, query, reason, confidence, ts_written)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (key, tool.lower(), query, reason, confidence, int(time.time() * 1000)),
        )
        self._conn().commit()

    def get_negative(self, key: str) -> Optional[NegativeFact]:
        row = self._conn().execute(
            "SELECT * FROM negative_facts WHERE key = ?", (key,)
        ).fetchone()
        if not row:
            return None
        return NegativeFact(
            key=row["key"],
            tool=row["tool"],
            query=row["query"],
            reason=row["reason"],
            confidence=row["confidence"],
            ts_written=row["ts_written"],
        )

    # --- skip facts --------------------------------------------------------

    def put_skip(self, abs_path: str, reason: str, duration_ms: Optional[int] = None) -> None:
        self._conn().execute(
            """
            INSERT OR REPLACE INTO skip_facts (abs_path, reason, duration_ms, ts_written)
            VALUES (?, ?, ?, ?)
            """,
            (abs_path, reason, duration_ms, int(time.time() * 1000)),
        )
        self._conn().commit()

    def should_skip(self, abs_path: str) -> Optional[str]:
        """Return the skip reason if this file is on the skip-list, else None."""
        row = self._conn().execute(
            "SELECT reason FROM skip_facts WHERE abs_path = ?", (abs_path,)
        ).fetchone()
        return row["reason"] if row else None

    # --- existence facts (PR Brain v2 Phase 2) --------------------------

    def put_existence(
        self,
        symbol_name: str,
        symbol_kind: str,
        referenced_at: str,
        exists: bool,
        evidence: Optional[str] = None,
        signature_info: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Record an existence fact. Idempotent — REPLACE semantics on
        (symbol_name, referenced_at) conflict so re-verifying a symbol
        just updates the record."""
        sig_json = json.dumps(signature_info) if signature_info is not None else None
        self._conn().execute(
            """
            INSERT OR REPLACE INTO existence_facts
              (symbol_name, symbol_kind, referenced_at, exists_flag, evidence,
               signature_info, ts_written)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                symbol_name,
                symbol_kind,
                referenced_at,
                1 if exists else 0,
                evidence,
                sig_json,
                int(time.time() * 1000),
            ),
        )
        self._conn().commit()

    def get_existence(
        self, symbol_name: str, referenced_at: Optional[str] = None,
    ) -> Optional[ExistenceFact]:
        """Look up a single existence fact. ``referenced_at`` disambiguates
        when the same symbol is referenced in multiple places; when
        omitted, returns the most recent entry for the name."""
        if referenced_at is not None:
            row = self._conn().execute(
                "SELECT * FROM existence_facts WHERE symbol_name = ? AND referenced_at = ?",
                (symbol_name, referenced_at),
            ).fetchone()
        else:
            row = self._conn().execute(
                "SELECT * FROM existence_facts WHERE symbol_name = ? "
                "ORDER BY ts_written DESC LIMIT 1",
                (symbol_name,),
            ).fetchone()
        return _row_to_existence(row) if row else None

    def iter_existence(
        self, exists: Optional[bool] = None,
    ) -> Iterable[ExistenceFact]:
        """Iterate existence facts. When ``exists`` is set, filter to that
        side (handy: ``iter_existence(exists=False)`` = all missing
        symbols — these are the runtime-error findings the coordinator
        promotes directly)."""
        if exists is None:
            rows = self._conn().execute(
                "SELECT * FROM existence_facts ORDER BY ts_written DESC"
            )
        else:
            rows = self._conn().execute(
                "SELECT * FROM existence_facts WHERE exists_flag = ? "
                "ORDER BY ts_written DESC",
                (1 if exists else 0,),
            )
        for row in rows:
            yield _row_to_existence(row)

    # --- plan memory (P4) -------------------------------------------------

    def put_plan_entry(
        self,
        *,
        dispatch_index: int,
        mode: str,
        role: Optional[str],
        scope: str,
        success_criteria: str,
        reason: Optional[str] = None,
    ) -> None:
        """Record one dispatch decision. Idempotent on dispatch_index."""
        self._conn().execute(
            """
            INSERT OR REPLACE INTO plan_memory
              (dispatch_index, mode, role, scope, success_criteria, reason, ts_written)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dispatch_index,
                mode,
                role,
                scope[:500],
                success_criteria[:500],
                (reason or "")[:500] or None,
                int(time.time() * 1000),
            ),
        )
        self._conn().commit()

    def iter_plan_entries(self) -> List[PlanEntry]:
        """All recorded plan entries, ordered by dispatch_index."""
        rows = self._conn().execute(
            "SELECT * FROM plan_memory ORDER BY dispatch_index ASC"
        ).fetchall()
        return [
            PlanEntry(
                dispatch_index=r["dispatch_index"],
                mode=r["mode"],
                role=r["role"],
                scope=r["scope"],
                success_criteria=r["success_criteria"],
                reason=r["reason"],
                ts_written=r["ts_written"],
            )
            for r in rows
        ]

    def count_plan_entries(self) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) FROM plan_memory"
        ).fetchone()
        return int(row[0]) if row else 0

    # --- Phase 9.9.3: sub-agent notes -------------------------------------

    def put_note(
        self,
        *,
        agent: str,
        topic: str,
        content: str,
        file_hint: Optional[str] = None,
    ) -> None:
        """Upsert a note by (agent, topic). Idempotent — writing the
        same (agent, topic) twice overwrites the prior content."""
        self._conn().execute(
            """
            INSERT INTO notes (agent, topic, content, file_hint, ts_written)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(agent, topic) DO UPDATE SET
                content = excluded.content,
                file_hint = excluded.file_hint,
                ts_written = excluded.ts_written
            """,
            (
                agent[:64],
                topic[:64],
                content[:4000],
                (file_hint or None) if not file_hint else file_hint[:512],
                int(time.time() * 1000),
            ),
        )
        self._conn().commit()

    def iter_notes_by_agent(self, agent: str) -> List[Note]:
        """All notes written by one agent, latest-first. Used by the
        agent to restore its own observations after context clearing.

        Tiebreaker ``id DESC`` ensures deterministic order when two
        notes happen to share a ms timestamp.
        """
        rows = self._conn().execute(
            "SELECT * FROM notes WHERE agent = ? ORDER BY ts_written DESC, id DESC",
            (agent[:64],),
        ).fetchall()
        return [
            Note(
                id=r["id"],
                agent=r["agent"],
                topic=r["topic"],
                content=r["content"],
                file_hint=r["file_hint"],
                ts_written=r["ts_written"],
            )
            for r in rows
        ]

    def iter_all_notes(self) -> List[Note]:
        """All notes across all agents, latest-first. Used by the
        coordinator's Synthesize step and by the INDEX CLI dump.

        Tiebreaker ``id DESC`` ensures deterministic order when notes
        share a ms timestamp.
        """
        rows = self._conn().execute(
            "SELECT * FROM notes ORDER BY ts_written DESC, id DESC"
        ).fetchall()
        return [
            Note(
                id=r["id"],
                agent=r["agent"],
                topic=r["topic"],
                content=r["content"],
                file_hint=r["file_hint"],
                ts_written=r["ts_written"],
            )
            for r in rows
        ]

    # --- inspection --------------------------------------------------------

    def stats(self) -> Dict[str, int]:
        """Counts by table for the INDEX dump and Langfuse export."""
        c = self._conn()
        return {
            "facts": c.execute("SELECT COUNT(*) FROM facts").fetchone()[0],
            "negative_facts": c.execute("SELECT COUNT(*) FROM negative_facts").fetchone()[0],
            "skip_facts": c.execute("SELECT COUNT(*) FROM skip_facts").fetchone()[0],
            "existence_facts": c.execute("SELECT COUNT(*) FROM existence_facts").fetchone()[0],
            "missing_symbols": c.execute(
                "SELECT COUNT(*) FROM existence_facts WHERE exists_flag = 0"
            ).fetchone()[0],
            "plan_entries": c.execute("SELECT COUNT(*) FROM plan_memory").fetchone()[0],
            "notes": c.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
        }

    def facts_by_tool(self, tool: str) -> List[Fact]:
        """All facts for a given tool, sorted by (path, range_start)."""
        rows = self._conn().execute(
            "SELECT * FROM facts WHERE tool = ? ORDER BY path, range_start",
            (tool.lower(),),
        ).fetchall()
        return [_row_to_fact(r) for r in rows]

    def iter_all_facts(self) -> Iterable[Fact]:
        for row in self._conn().execute("SELECT * FROM facts ORDER BY ts_written DESC"):
            yield _row_to_fact(row)


def _row_to_fact(row: sqlite3.Row) -> Fact:
    content_bytes = zlib.decompress(row["content"])
    content = json.loads(content_bytes.decode("utf-8"))
    return Fact(
        key=row["key"],
        tool=row["tool"],
        path=row["path"],
        range_start=row["range_start"],
        range_end=row["range_end"],
        content=content,
        agent=row["agent"],
        ts_written=row["ts_written"],
    )


def _row_to_existence(row: sqlite3.Row) -> ExistenceFact:
    raw_sig = row["signature_info"]
    sig_info: Optional[Dict[str, Any]] = None
    if raw_sig:
        try:
            sig_info = json.loads(raw_sig)
        except (ValueError, json.JSONDecodeError):
            sig_info = None
    return ExistenceFact(
        symbol_name=row["symbol_name"],
        symbol_kind=row["symbol_kind"],
        referenced_at=row["referenced_at"],
        exists_flag=bool(row["exists_flag"]),
        evidence=row["evidence"],
        signature_info=sig_info,
        ts_written=row["ts_written"],
    )


def sweep_orphans(max_age_hours: int = 24) -> List[Path]:
    """Remove session DBs older than ``max_age_hours``. Returns the list of
    removed paths. Called from backend startup — prevents scratchpad disk
    bloat if a previous run crashed without calling FactStore.delete().
    """
    import contextlib

    if not SCRATCHPAD_ROOT.exists():
        return []
    cutoff = time.time() - (max_age_hours * 3600)
    removed: List[Path] = []
    for p in SCRATCHPAD_ROOT.glob("*.sqlite"):
        try:
            if p.stat().st_mtime < cutoff:
                for suffix in ("", "-wal", "-shm"):
                    sp = Path(str(p) + suffix)
                    with contextlib.suppress(FileNotFoundError):
                        sp.unlink()
                removed.append(p)
        except OSError as e:
            logger.warning("Failed to inspect %s during orphan sweep: %s", p, e)
    if removed:
        logger.info("Scratchpad orphan sweep removed %d session(s)", len(removed))
    return removed
