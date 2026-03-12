"""Persistent repo-scoped token cache backed by SQLite.

Stores proven-valid PATs keyed by normalised repo URL so that multiple
chat rooms cloning the same repo can reuse a non-expired token without
the user having to supply it again.

Security notes
--------------
* The DB file lives inside the workspaces directory and is listed in
  ``.gitignore`` — it is NEVER committed to version control.
* Tokens are stored in plaintext.  Treat the file with the same care as
  a ``.env`` file or SSH private key.
* The cache is opportunistic: failures to read / write are logged and
  swallowed so they never break workspace creation.

Expiry policy
-------------
When the caller provides ``CredentialPayload.expires_at``, that
timestamp is used as-is.  If the field is ``None`` (e.g. a classic
GitHub PAT with no expiry date), a configurable default TTL (default
8 hours) is applied from the moment of caching.  This is conservative
but safe: at worst the user re-enters their token once a day.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from .schemas import CredentialPayload

logger = logging.getLogger(__name__)

_DEFAULT_TTL_SECONDS = 8 * 3600  # 8 hours


def _normalize_url(url: str) -> str:
    """Return a canonical form of *url* used as the cache key.

    Strips trailing slashes and the ``.git`` suffix so that
    ``https://github.com/x/y.git`` and ``https://github.com/x/y``
    resolve to the same entry.
    """
    return url.strip().rstrip("/").removesuffix(".git")


class RepoTokenCache:
    """SQLite-backed cache of PATs keyed by (normalised) repo URL.

    Thread-safety: this class is NOT thread-safe by itself.  It is
    expected to be called from a single asyncio event loop on the main
    thread.  SQLite ``check_same_thread=False`` is used only to allow
    the connection object to be created in one coroutine and used in
    another within the same thread.
    """

    def __init__(
        self,
        db_path: Path,
        default_ttl_seconds: int = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._db_path = db_path
        self._default_ttl = default_ttl_seconds
        self._conn: Optional[sqlite3.Connection] = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def open(self) -> None:
        """Open (or create) the SQLite database and ensure the schema exists."""
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(
            str(self._db_path), check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS repo_tokens (
                repo_url   TEXT PRIMARY KEY,
                token      TEXT NOT NULL,
                username   TEXT,
                cached_at  TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
        """)
        self._conn.commit()
        logger.info("RepoTokenCache opened at %s", self._db_path)

    def close(self) -> None:
        """Close the database connection."""
        if self._conn:
            self._conn.close()
            self._conn = None
            logger.info("RepoTokenCache closed")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(self, repo_url: str, creds: CredentialPayload) -> None:
        """Insert or replace a token for *repo_url*.

        The expiry time is taken from ``creds.expires_at`` when present;
        otherwise ``default_ttl_seconds`` from now is used.

        Also evicts expired entries on every write (cheap housekeeping).
        """
        if self._conn is None:
            return

        key = _normalize_url(repo_url)
        now = datetime.now(timezone.utc)

        if creds.expires_at is not None:
            # Honour the expiry hint supplied by the client.
            # Make it timezone-aware if it isn't already.
            expires: datetime = creds.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
        else:
            expires = now + timedelta(seconds=self._default_ttl)

        # Opportunistic cleanup of OTHER expired entries (before inserting)
        self.evict_expired()

        try:
            self._conn.execute(
                """
                INSERT INTO repo_tokens (repo_url, token, username, cached_at, expires_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(repo_url) DO UPDATE SET
                    token      = excluded.token,
                    username   = excluded.username,
                    cached_at  = excluded.cached_at,
                    expires_at = excluded.expires_at
                """,
                (
                    key,
                    creds.token,
                    creds.username,
                    now.isoformat(),
                    expires.isoformat(),
                ),
            )
            self._conn.commit()
            logger.info(
                "Cached token for repo %s (expires %s)",
                key, expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
            )
        except Exception as exc:
            logger.warning("Failed to cache token for %s: %s", key, exc)

    def get(self, repo_url: str) -> Optional[CredentialPayload]:
        """Return a non-expired cached credential, or *None*.

        Expired entries are deleted before returning *None*.
        """
        if self._conn is None:
            return None

        key = _normalize_url(repo_url)
        now = datetime.now(timezone.utc)

        try:
            row = self._conn.execute(
                "SELECT token, username, expires_at FROM repo_tokens WHERE repo_url = ?",
                (key,),
            ).fetchone()
        except Exception as exc:
            logger.warning("Token cache read failed for %s: %s", key, exc)
            return None

        if row is None:
            return None

        # Parse stored expiry
        try:
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
        except ValueError:
            expires_at = now  # treat unparseable as expired

        if now >= expires_at:
            logger.info("Cached token for repo %s has expired; evicting", key)
            self._evict_one(key)
            return None

        logger.info("Using cached token for repo %s", key)
        return CredentialPayload(
            token=row["token"],
            username=row["username"] or None,
            expires_at=expires_at,
        )

    def evict_expired(self) -> int:
        """Delete all expired rows.  Returns the number of rows removed."""
        if self._conn is None:
            return 0
        now = datetime.now(timezone.utc).isoformat()
        try:
            cursor = self._conn.execute(
                "DELETE FROM repo_tokens WHERE expires_at <= ?", (now,)
            )
            self._conn.commit()
            count = cursor.rowcount
            if count:
                logger.info("RepoTokenCache: evicted %d expired token(s)", count)
            return count
        except Exception as exc:
            logger.warning("Token cache eviction failed: %s", exc)
            return 0

    def list_entries(self) -> list[dict]:
        """Return all cached entries (tokens redacted) for diagnostics."""
        if self._conn is None:
            return []
        try:
            rows = self._conn.execute(
                "SELECT repo_url, username, cached_at, expires_at FROM repo_tokens"
            ).fetchall()
            return [
                {
                    "repo_url": r["repo_url"],
                    "username": r["username"],
                    "cached_at": r["cached_at"],
                    "expires_at": r["expires_at"],
                }
                for r in rows
            ]
        except Exception as exc:
            logger.warning("Token cache list failed: %s", exc)
            return []

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _evict_one(self, repo_url: str) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute(
                "DELETE FROM repo_tokens WHERE repo_url = ?", (repo_url,)
            )
            self._conn.commit()
        except Exception as exc:
            logger.warning("Could not evict token for %s: %s", repo_url, exc)
