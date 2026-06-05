"""Scratchpad CLI — inspect session Fact Vaults.

    python -m app.scratchpad list                 # active sessions + stats
    python -m app.scratchpad dump <session_id>    # paper-style INDEX dump
    python -m app.scratchpad sweep [--hours=24]   # manual orphan cleanup

Read-only inspection — no mutations to any session DB. The dump format is
generated on demand from the SQLite file so we never have to keep a
serialised INDEX in sync; the SQLite file is the single source of truth.

Entry point for developers debugging PR review runs. Production code does
not call this — PRBrainOrchestrator opens / closes FactStore directly.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from typing import Optional

from .store import SCRATCHPAD_ROOT, FactStore, sweep_orphans


def _fmt_ts(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cmd_list(_args: argparse.Namespace) -> int:
    """List scratchpad session files with basic stats."""
    if not SCRATCHPAD_ROOT.exists():
        print(f"No scratchpad directory at {SCRATCHPAD_ROOT}", file=sys.stderr)
        return 0
    files = sorted(SCRATCHPAD_ROOT.glob("*.sqlite"))
    if not files:
        print(f"(no session DBs in {SCRATCHPAD_ROOT})")
        return 0
    print(f"# Scratchpad sessions in {SCRATCHPAD_ROOT}\n")
    for path in files:
        session_id = path.stem
        size_kb = path.stat().st_size / 1024
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        # Open read-only via FactStore constructor (no schema init this time
        # — file already exists). Use .stats() for counts and pull task_id
        # from meta so the listing maps each file back to its source PR.
        try:
            store = FactStore(path, session_id)
            stats = store.stats()
            task_row = store._conn().execute("SELECT v FROM meta WHERE k = 'task_id'").fetchone()
            task_id = (task_row["v"] if task_row else "") or "-"
            store.close()
            stats_str = ", ".join(f"{k}={v}" for k, v in stats.items())
        except Exception as e:  # corrupt or locked DB
            stats_str = f"<error: {e}>"
            task_id = "?"
        print(f"- {session_id}  task={task_id}  {size_kb:.1f} KB  mtime={mtime}  {stats_str}")
    return 0


def _cmd_dump(args: argparse.Namespace) -> int:
    """Render a session's facts as a paper-style markdown INDEX."""
    db_path = SCRATCHPAD_ROOT / f"{args.session_id}.sqlite"
    if not db_path.exists():
        print(f"Session not found: {db_path}", file=sys.stderr)
        return 1

    store = FactStore(db_path, args.session_id)
    try:
        # Read session metadata
        conn = store._conn()
        meta_rows = conn.execute("SELECT k, v FROM meta").fetchall()
        meta = {r["k"]: r["v"] for r in meta_rows}
        stats = store.stats()

        created = _fmt_ts(int(meta.get("started_ms", "0"))) if meta.get("started_ms") else "unknown"

        out = [
            f"# Scratchpad Index — session {args.session_id}",
            f"Task: {meta.get('task_id') or '(none)'}",
            f"Created: {created}  Workspace: {meta.get('workspace', '(unspecified)')}",
            "",
            "## Summary",
            f"- facts: {stats['facts']}",
            f"- negative_facts: {stats['negative_facts']}",
            f"- skip_facts: {stats['skip_facts']}",
            "",
        ]

        # Recent facts, 20 most recent (DESC on ts_written)
        recent = list(store.iter_all_facts())[:20]
        if recent:
            out.extend(["## Recent facts (newest 20)", ""])
            out.append("| Time | Tool | Path | Range | Agent |")
            out.append("|---|---|---|---|---|")
            for f in recent:
                range_str = f"{f.range_start}-{f.range_end}" if f.range_start is not None else ""
                out.append(f"| {_fmt_ts(f.ts_written)} | {f.tool} | {f.path or ''} | {range_str} | {f.agent or ''} |")
            out.append("")

        # Group by tool
        if stats["facts"]:
            by_tool: dict = {}
            for row in conn.execute("SELECT tool, COUNT(*) AS n FROM facts GROUP BY tool ORDER BY n DESC"):
                by_tool[row["tool"]] = row["n"]
            out.extend(["## Facts by tool", ""])
            for tool, count in by_tool.items():
                out.append(f"- **{tool}** — {count} cached")
            out.append("")

        # Negative facts
        neg_rows = conn.execute(
            "SELECT key, tool, query, reason, confidence, ts_written "
            "FROM negative_facts ORDER BY ts_written DESC LIMIT 20"
        ).fetchall()
        if neg_rows:
            out.extend(["## Negative facts (verified absences)", ""])
            out.append("| Time | Tool | Query | Reason | Confidence |")
            out.append("|---|---|---|---|---|")
            for r in neg_rows:
                conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else ""
                out.append(
                    f"| {_fmt_ts(r['ts_written'])} | {r['tool']} | " f"{r['query']} | {r['reason'] or ''} | {conf} |"
                )
            out.append("")

        # Skip facts
        skip_rows = conn.execute(
            "SELECT abs_path, reason, duration_ms, ts_written " "FROM skip_facts ORDER BY ts_written DESC"
        ).fetchall()
        if skip_rows:
            out.extend(["## Skipped files (expensive-to-parse blacklist)", ""])
            out.append("| Time | File | Reason | Duration |")
            out.append("|---|---|---|---|")
            for r in skip_rows:
                dur = f"{r['duration_ms']}ms" if r["duration_ms"] is not None else ""
                out.append(f"| {_fmt_ts(r['ts_written'])} | {r['abs_path']} | {r['reason']} | {dur} |")
            out.append("")

        print("\n".join(out))
    finally:
        store.close()
    return 0


def _cmd_sweep(args: argparse.Namespace) -> int:
    """Remove session DBs older than --hours (default 24)."""
    removed = sweep_orphans(max_age_hours=args.hours)
    if not removed:
        print(f"No sessions older than {args.hours}h to remove.")
        return 0
    print(f"Removed {len(removed)} session(s) older than {args.hours}h:")
    for p in removed:
        print(f"  - {p}")
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.scratchpad",
        description="Inspect per-session Fact Vault SQLite files.",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="List active scratchpad sessions with stats")

    p_dump = sub.add_parser("dump", help="Render a session as paper-style markdown INDEX")
    p_dump.add_argument("session_id", help="Session ID (filename without .sqlite)")

    p_sweep = sub.add_parser("sweep", help="Remove session DBs older than --hours")
    p_sweep.add_argument("--hours", type=int, default=24, help="Age threshold (default 24)")

    args = parser.parse_args(argv)

    if args.cmd == "list":
        return _cmd_list(args)
    if args.cmd == "dump":
        return _cmd_dump(args)
    if args.cmd == "sweep":
        return _cmd_sweep(args)
    parser.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
