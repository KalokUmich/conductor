"""Subprocess-backed tree-sitter parse pool with SIGKILL timeout.

Why this exists
---------------

tree-sitter's Python binding holds the GIL through the entire C-level
``parser.parse()`` call. That makes in-process timeout mechanisms
unusable — py-spy on sentry-007 caught the MainThread blocked in
``Thread.start() → _started.wait()`` for 8+ minutes because the worker
thread acquired the GIL, entered tree-sitter, and never released it.
A ``queue.get(timeout=60)`` sitting a frame away is dead code in that
scenario: main cannot reacquire the GIL to raise ``Empty``.

``signal.SIGALRM`` has the same problem — Python signal handlers only
run between bytecode instructions, so a long-running C call delays
delivery indefinitely.

The only reliable primitive is a **separate OS process** we can
``SIGKILL``. This module owns a single persistent worker subprocess;
each parse is shipped over a pipe. On timeout we kill and respawn.

Design choices
--------------

* **Single worker, not a pool.** The existing workspace scan is
  sequential (one file at a time); a pool would only help if we also
  parallelise the scan loop, which is a separate effort. A single
  worker keeps the pickling surface and process-management logic
  minimal.
* **"forkserver" start method (with "spawn" fallback on Windows).**
  The backend is a heavily multi-threaded asyncio process; ``fork`` in
  that environment is known-unsafe (threads other than the forker
  vanish in the child but their held mutexes stay locked, causing
  deadlocks). ``forkserver`` spawns a single, single-threaded server
  process at first use, and every subsequent worker forks from *that*
  server — so parent-thread state never leaks into the child. Windows
  falls back to spawn. **Both start methods still require an
  ``if __name__ == "__main__":`` guard in any script that uses the
  pool** — Python's multiprocessing bootstrap guard runs before the
  start method is selected, so you'll get a ``RuntimeError`` from
  ``Process.start()`` either way. This is unrelated to our design;
  it's a Python multiprocessing constraint.
* **Lazy, process-global.** The pool is created on first use and lives
  until interpreter shutdown. Test code can override with a fresh
  instance.
* **Best-effort.** Every failure mode (pipe broken, worker crash,
  pickle error) falls through to the regex fallback in the caller.
  Caching is never load-bearing.
"""

from __future__ import annotations

import logging
import multiprocessing as _mp
import threading
from typing import Optional

from .parser import FileSymbols

logger = logging.getLogger(__name__)


def _choose_start_method() -> str:
    """Prefer forkserver (POSIX) — avoids spawn's __main__ re-import hazard
    and fork's multi-threaded unsafety. Fall back to spawn on Windows."""
    available = _mp.get_all_start_methods()
    if "forkserver" in available:
        return "forkserver"
    return "spawn"


_START_METHOD = _choose_start_method()


def _worker_loop(conn) -> None:  # pragma: no cover — runs in child process
    """Child-process main loop. Reads (source, language, file_path) tuples
    from ``conn`` and sends back ("ok", FileSymbols) or ("err", str)."""
    # Delay the heavy import until the subprocess is alive so we fail
    # predictably if tree-sitter isn't installed in the child.
    from app.repo_graph.parser import _extract_with_tree_sitter

    while True:
        try:
            req = conn.recv()
        except EOFError:
            return
        if req is None:  # shutdown sentinel
            return
        source, language, file_path = req
        try:
            result = _extract_with_tree_sitter(source, language, file_path)
            conn.send(("ok", result))
        except Exception as e:
            # Send a lightweight string — full exception objects don't
            # always survive pickling cleanly.
            conn.send(("err", f"{type(e).__name__}: {e}"))


class ParsePool:
    """One persistent worker subprocess, replaced on timeout or crash."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[_mp.Process] = None
        self._conn = None

    # --- lifecycle ---------------------------------------------------------

    def _spawn(self) -> None:
        ctx = _mp.get_context(_START_METHOD)
        # forkserver: pre-import the parser so the first worker fork
        # inherits a ready-to-use module instead of paying the import
        # cost on the hot path. Harmless on spawn (where set_forkserver
        # _preload only takes effect for forkserver context).
        if _START_METHOD == "forkserver":
            try:
                ctx.set_forkserver_preload(["app.repo_graph.parser"])
            except Exception as exc:  # already set on a prior spawn is OK
                logger.debug("forkserver preload skipped: %s", exc)
        parent_conn, child_conn = ctx.Pipe(duplex=True)
        proc = ctx.Process(
            target=_worker_loop,
            args=(child_conn,),
            daemon=True,
            name="tree-sitter-parse-worker",
        )
        try:
            proc.start()
        except RuntimeError as exc:
            # Most commonly: "An attempt has been made to start a new
            # process before the current process has finished its
            # bootstrapping phase." Raised by spawn-context children
            # that re-import a parent __main__ module lacking the
            # ``if __name__ == "__main__":`` guard. Re-raise with a
            # pointer so the caller can diagnose.
            parent_conn.close()
            child_conn.close()
            raise RuntimeError(
                f"ParsePool subprocess start failed with method "
                f"'{_START_METHOD}': {exc}. If running from a script, "
                "guard module-level code with `if __name__ == '__main__':` "
                "so child processes don't re-execute setup."
            ) from exc
        # Parent doesn't need the child side; closing it avoids a leak
        # and makes recv() raise EOFError if the child exits.
        child_conn.close()
        self._proc = proc
        self._conn = parent_conn
        logger.info("ParsePool spawned worker pid=%d (method=%s)", proc.pid, _START_METHOD)

    def _kill_current(self, reason: str) -> None:
        """SIGKILL the worker and release pipe handles. Caller holds the lock."""
        proc = self._proc
        conn = self._conn
        self._proc = None
        self._conn = None
        if proc is None:
            return
        pid = proc.pid
        try:
            if proc.is_alive():
                proc.kill()  # SIGKILL on POSIX, TerminateProcess on Windows
                proc.join(timeout=1.0)
        except Exception as exc:
            logger.debug("ParsePool kill(%d) failed: %s", pid, exc)
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass
        logger.warning("ParsePool worker pid=%s killed (%s)", pid, reason)

    def shutdown(self) -> None:
        """Clean shutdown — send sentinel, then kill if it doesn't exit."""
        import contextlib

        with self._lock:
            if self._proc is None:
                return
            with contextlib.suppress(Exception):
                self._conn.send(None)
                self._proc.join(timeout=1.0)
            if self._proc and self._proc.is_alive():
                self._kill_current(reason="shutdown")
            else:
                with contextlib.suppress(Exception):
                    self._conn.close()
                self._proc = None
                self._conn = None

    # --- parse -------------------------------------------------------------

    def parse(
        self,
        source: bytes,
        language: str,
        file_path: str,
        timeout_s: float,
    ) -> Optional[FileSymbols]:
        """Dispatch a tree-sitter parse. Returns:

        * ``FileSymbols`` on success.
        * ``None`` on timeout (caller falls back to regex) — worker is
          SIGKILL'd and replaced.
        * ``None`` on any IPC / pickle / worker-crash error.

        Thread-safe: only one parse runs at a time per pool.
        """
        with self._lock:
            if self._proc is None or not self._proc.is_alive():
                try:
                    self._spawn()
                except Exception as exc:
                    logger.warning("ParsePool spawn failed: %s", exc)
                    return None

            try:
                self._conn.send((source, language, file_path))
            except (BrokenPipeError, OSError) as exc:
                logger.debug("ParsePool send failed, respawning: %s", exc)
                self._kill_current(reason="send failed")
                try:
                    self._spawn()
                    self._conn.send((source, language, file_path))
                except Exception as exc2:
                    logger.warning("ParsePool retry send failed: %s", exc2)
                    return None

            if not self._conn.poll(timeout=timeout_s):
                self._kill_current(reason=f"timeout after {timeout_s:.0f}s on {file_path}")
                return None

            try:
                kind, val = self._conn.recv()
            except (EOFError, BrokenPipeError, ConnectionResetError) as exc:
                logger.debug("ParsePool recv failed: %s", exc)
                self._kill_current(reason="recv failed")
                return None

            if kind == "err":
                logger.debug("ParsePool worker reported error: %s", val)
                return None
            return val


# --- module-level singleton ---------------------------------------------------

_pool: Optional[ParsePool] = None
_pool_lock = threading.Lock()


def get_parse_pool() -> ParsePool:
    """Return the process-global pool, creating it lazily."""
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ParsePool()
        return _pool


def shutdown_parse_pool() -> None:
    """Shut down the singleton — for tests and backend shutdown."""
    global _pool
    with _pool_lock:
        if _pool is not None:
            _pool.shutdown()
            _pool = None
