"""Scratchpad — task-scoped short-term memory for PR Brain sub-agents.

Phase 9.15 full: SQLite-backed fact vault + canonical keys + in-flight dedup.
See ROADMAP.md (Phase 9.15) for the design rationale and
backend/CLAUDE.md for the runtime contract.

Public surface:
    key_lock(key) → threading.Lock
        Per-key lock; different keys don't serialise against each other.
        Used by expensive workspace-wide scans (_ensure_graph,
        _get_symbol_index) to coalesce concurrent cold-miss callers into one.

    FactStore (class)
        SQLite-backed per-session cache of tool-call results.
        Open with ``FactStore.open(session_id, workspace=...)``;
        delete with ``store.delete()`` at session end.

    build_key(tool, params) → str | None
        Canonical cache key builder. Returns None for non-cacheable tools
        (web_*, file_edit, run_test, …) so callers short-circuit to direct
        execution without consulting the vault.

    extract_path(tool, params), extract_range(tool, params)
        Pull the indexed columns out of params so FactStore.range_lookup
        can do range-intersection queries.

    SCRATCHPAD_ROOT, sweep_orphans(max_age_hours=24)
        Root directory for session DBs, startup sweep of abandoned ones.
"""

from .context import bind_factstore, current_factstore
from .executor import CachedToolExecutor
from .inflight import key_lock
from .keys import (
    CACHEABLE_TOOLS,
    RANGE_TOOLS,
    SCHEMA_VERSION,
    build_key,
    extract_path,
    extract_range,
)
from .store import (
    SCRATCHPAD_ROOT,
    ExistenceFact,
    Fact,
    FactStore,
    NegativeFact,
    PlanEntry,
    sweep_orphans,
)

__all__ = [
    "CACHEABLE_TOOLS",
    "RANGE_TOOLS",
    "SCHEMA_VERSION",
    "SCRATCHPAD_ROOT",
    "CachedToolExecutor",
    "ExistenceFact",
    "Fact",
    "FactStore",
    "NegativeFact",
    "PlanEntry",
    "bind_factstore",
    "build_key",
    "current_factstore",
    "extract_path",
    "extract_range",
    "key_lock",
    "sweep_orphans",
]
