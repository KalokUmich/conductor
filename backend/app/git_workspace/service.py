"""Git Workspace Service — core operations.

Manages bare clones + git worktrees on the local filesystem, one worktree
per chat room.  Supports Mode A (token/GIT_ASKPASS) and Mode B (delegate)
authentication.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .credential_store import CredentialStore
from .schemas import (
    CloneProgress,
    CredentialPayload,
    FileChange,
    FileSyncEvent,
    WorkspaceCommitRequest,
    WorkspaceCommitResult,
    WorkspaceCreateRequest,
    WorkspaceDestroyResult,
    WorkspaceInfo,
    WorkspaceMode,
    WorkspacePushRequest,
    WorkspacePushResult,
    WorkspaceSyncRequest,
    WorkspaceSyncResult,
    WorktreeStatus,
)
from .token_cache import RepoTokenCache

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal state
# ---------------------------------------------------------------------------


class _WorktreeRecord:
    """In-process record for a single room's worktree."""

    __slots__ = (
        "base_branch_name",
        "branch",
        "clone_progress",
        "created_at",
        "error_detail",
        "last_synced",
        "repo_hash",
        "repo_url",
        "room_id",
        "status",
        "worktree_path",
    )

    def __init__(
        self,
        room_id: str,
        repo_url: str,
        branch: str,
        worktree_path: Path,
        repo_hash: str = "",
        base_branch_name: str = "main",
    ) -> None:
        self.room_id = room_id
        self.repo_url = repo_url
        self.branch = branch
        self.worktree_path = worktree_path
        self.repo_hash = repo_hash
        self.base_branch_name = base_branch_name
        self.status = WorktreeStatus.PENDING
        self.created_at = datetime.now(UTC)
        self.last_synced: Optional[datetime] = None
        self.error_detail: Optional[str] = None
        self.clone_progress: Optional[CloneProgress] = None

    def to_info(self) -> WorkspaceInfo:
        return WorkspaceInfo(
            room_id=self.room_id,
            repo_url=self.repo_url,
            branch=self.branch,
            worktree_path=str(self.worktree_path),
            status=self.status,
            created_at=self.created_at,
            last_synced=self.last_synced,
            error_detail=self.error_detail,
            clone_progress=self.clone_progress,
        )


# ---------------------------------------------------------------------------
# Helper – GIT_ASKPASS script
# ---------------------------------------------------------------------------

_ASKPASS_SCRIPT = """\
#!/bin/sh
# Minimal GIT_ASKPASS helper.  Reads credentials from env vars set by the
# parent process before spawning git.  Never writes credentials to stdout
# unless queried.
case "$1" in
  *Username*) echo "${GIT_CREDENTIAL_USERNAME}" ;;
  *Password*) echo "${GIT_CREDENTIAL_TOKEN}"    ;;
esac
"""


def _make_askpass_script() -> str:
    """Write the GIT_ASKPASS helper to a temp file and return its path."""
    fd, path = tempfile.mkstemp(prefix="conductor_askpass_", suffix=".sh")
    try:
        os.write(fd, _ASKPASS_SCRIPT.encode())
    finally:
        os.close(fd)
    os.chmod(path, 0o700)
    return path


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class GitWorkspaceService:
    """
    Manages the full lifecycle of git-backed workspaces:

      * clone (bare) a remote repo once per URL
      * create / tear-down git worktrees per room
      * perform authenticated git operations (fetch / push)
      * broadcast file-change events to room WebSocket connections
    """

    def __init__(self) -> None:
        self._workspaces_dir: Path = Path("./workspaces")
        self._worktrees: Dict[str, _WorktreeRecord] = {}
        self._local_workspaces: Dict[str, Path] = {}  # room_id → local path
        self._credential_store = CredentialStore()
        self._token_cache: Optional[RepoTokenCache] = None
        self._broadcast_callbacks: Dict[str, list] = {}  # room_id → [callbacks]
        self._background_tasks: set = set()  # prevent GC of fire-and-forget tasks
        self._max_worktrees: int = 20
        self._cleanup_on_close: bool = True
        self._initialized: bool = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def initialize(self, settings) -> None:  # settings: GitWorkspaceSettings
        """Call once from app lifespan on startup."""
        self._workspaces_dir = Path(settings.workspaces_dir).resolve()
        self._max_worktrees = settings.max_worktrees_per_repo
        self._cleanup_on_close = settings.cleanup_on_room_close
        self._workspaces_dir.mkdir(parents=True, exist_ok=True)
        await self._credential_store.start()

        # Persistent repo-scoped token cache (PostgreSQL-backed via shared engine)
        try:
            from ..db.engine import get_engine

            db_engine = get_engine()
            self._token_cache = RepoTokenCache(engine=db_engine)
        except Exception as exc:
            logger.warning("Failed to initialise token cache: %s", exc)
            self._token_cache = None

        await self._recover_worktrees()
        self._initialized = True
        logger.info(
            "GitWorkspaceService initialized (dir=%s, auth_mode=%s)",
            self._workspaces_dir,
            settings.git_auth_mode,
        )

    async def shutdown(self) -> None:
        """Graceful shutdown — wipe credentials."""
        await self._credential_store.stop()
        # Token cache uses shared DB engine — no separate close needed
        self._initialized = False
        logger.info("GitWorkspaceService shut down.")

    # ------------------------------------------------------------------
    # Credential management
    # ------------------------------------------------------------------

    async def store_credentials(
        self,
        room_id: str,
        payload: CredentialPayload,
    ) -> None:
        await self._credential_store.put(room_id, payload)

    async def revoke_credentials(self, room_id: str) -> None:
        await self._credential_store.delete(room_id)

    # ------------------------------------------------------------------
    # Remote branch listing
    # ------------------------------------------------------------------

    async def list_remote_branches(
        self,
        repo_url: str,
        credentials: Optional[CredentialPayload] = None,
    ) -> tuple[list[str], Optional[str]]:
        """List branches from a remote repo using ``git ls-remote``."""
        env = os.environ.copy()
        if credentials:
            askpass_path = _make_askpass_script()
            env.update(
                {
                    "GIT_ASKPASS": askpass_path,
                    "GIT_CREDENTIAL_USERNAME": credentials.username or "git",
                    "GIT_CREDENTIAL_TOKEN": credentials.token,
                    "GIT_TERMINAL_PROMPT": "0",
                }
            )

        output = await self._run_git(
            ["ls-remote", "--heads", "--symref", repo_url],
            env=env,
        )

        branches: list[str] = []
        default_branch: Optional[str] = None
        for line in output.splitlines():
            if line.startswith("ref: refs/heads/"):
                # Symref line: "ref: refs/heads/main\tHEAD"
                default_branch = line.split("ref: refs/heads/")[1].split("\t")[0]
            elif "refs/heads/" in line:
                branch = line.split("refs/heads/")[-1].strip()
                if branch:
                    branches.append(branch)

        return sorted(branches), default_branch

    # ------------------------------------------------------------------
    # Workspace creation
    # ------------------------------------------------------------------

    async def create_workspace(self, req: WorkspaceCreateRequest) -> WorkspaceInfo:
        if len(self._worktrees) >= self._max_worktrees:
            raise RuntimeError(f"Maximum concurrent worktrees ({self._max_worktrees}) reached.")
        if req.room_id in self._worktrees:
            return self._worktrees[req.room_id].to_info()

        # Resolve credentials:
        # 1. Use explicitly provided credentials (Mode A)
        # 2. Fall back to cached token for this repo (if available)
        effective_creds = req.credentials
        if effective_creds is None and self._token_cache:
            cached = await self._token_cache.get(req.repo_url)
            if cached:
                effective_creds = cached
                logger.info(
                    "Room %s: using cached token for repo %s",
                    req.room_id,
                    req.repo_url,
                )

        if effective_creds:
            await self._credential_store.put(req.room_id, effective_creds)

        repo_hash = hashlib.sha256(req.repo_url.encode()).hexdigest()[:12]
        repo_dir = self._workspaces_dir / repo_hash
        bare_dir = repo_dir / "bare.git"
        worktrees_dir = repo_dir / "worktrees"
        worktree_path = worktrees_dir / req.room_id
        branch = f"session/{req.room_id}"

        record = _WorktreeRecord(
            room_id=req.room_id,
            repo_url=req.repo_url,
            branch=branch,
            worktree_path=worktree_path,
            repo_hash=repo_hash,
            base_branch_name=req.base_branch,
        )
        self._worktrees[req.room_id] = record

        task = asyncio.create_task(
            self._setup_worktree(
                record=record,
                req=req,
                bare_dir=bare_dir,
                worktrees_dir=worktrees_dir,
            )
        )
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)
        return record.to_info()

    async def _setup_worktree(
        self,
        record: _WorktreeRecord,
        req: WorkspaceCreateRequest,
        bare_dir: Path,
        worktrees_dir: Path,
    ) -> None:
        """Background task: clone bare repo (if needed) then create worktree."""
        try:
            env = await self._build_git_env(req.room_id)
            worktrees_dir.mkdir(parents=True, exist_ok=True)

            # --- 1. Bare clone (idempotent) ---
            if not bare_dir.exists():
                logger.info("Cloning %s → %s (bare)", req.repo_url, bare_dir)
                record.status = WorktreeStatus.SYNCING
                record.clone_progress = CloneProgress(phase="connecting")

                def _on_progress(p: CloneProgress) -> None:
                    record.clone_progress = p

                await self._run_git_with_progress(
                    ["clone", "--bare", "--progress", req.repo_url, str(bare_dir)],
                    on_progress=_on_progress,
                    env=env,
                )
                record.clone_progress = None  # done
            else:
                logger.debug("Bare repo already exists at %s", bare_dir)

            # --- 2. Create the worktree on a new branch ---
            # Use absolute path so git doesn't interpret it relative to bare_dir.
            abs_worktree = str(record.worktree_path.resolve())
            logger.info("Creating worktree for room %s (branch=%s)", req.room_id, record.branch)
            await self._run_git(
                [
                    "-C",
                    str(bare_dir),
                    "worktree",
                    "add",
                    "-b",
                    record.branch,
                    abs_worktree,
                    req.base_branch,
                ],
                env=env,
            )

            record.status = WorktreeStatus.READY
            record.last_synced = datetime.now(UTC)
            logger.info("Worktree ready for room %s at %s", req.room_id, record.worktree_path)

            # --- 3. Cache the token (only if explicitly provided by caller) ---
            # We cache only after a successful clone so we know the token works.
            # We do NOT cache tokens that were themselves retrieved from cache to
            # avoid accidentally resetting their original expiry.
            if req.credentials and self._token_cache:
                try:
                    await self._token_cache.put(req.repo_url, req.credentials)
                except Exception as exc:
                    logger.warning("Could not cache token for repo %s: %s", req.repo_url, exc)

            # --- 4. Ensure the shared index worktree exists for this repo ---
            await self._ensure_index_worktree(
                bare_dir=bare_dir,
                worktrees_dir=worktrees_dir,
                base_branch=req.base_branch,
                env=env,
            )

        except Exception as exc:  # TODO: narrow to (RuntimeError, OSError, ValueError)
            record.status = WorktreeStatus.ERROR
            record.error_detail = str(exc)
            logger.error("Failed to set up worktree for room %s: %s", req.room_id, exc)

    async def _ensure_index_worktree(
        self,
        bare_dir: Path,
        worktrees_dir: Path,
        base_branch: str,
        env: Dict[str, str],
    ) -> None:
        """Create a shared index worktree for the repo if it doesn't exist.

        The index worktree lives at ``worktrees/_index/`` and is checked out
        on the original base branch.  All rooms that use the same repo share
        this single worktree for code-search indexing.
        """
        index_wt = worktrees_dir / "_index"
        if index_wt.exists():
            return

        abs_index_wt = str(index_wt.resolve())
        logger.info(
            "Creating shared index worktree at %s (branch=%s)",
            abs_index_wt,
            base_branch,
        )
        await self._run_git(
            [
                "-C",
                str(bare_dir),
                "worktree",
                "add",
                "--detach",
                abs_index_wt,
                base_branch,
            ],
            env=env,
        )
        logger.info("Shared index worktree ready at %s", abs_index_wt)

    # ------------------------------------------------------------------
    # Index worktree accessor
    # ------------------------------------------------------------------

    def get_index_worktree_path(self, room_id: str) -> Optional[Path]:
        """Return the shared index worktree path for the repo that *room_id* belongs to.

        All rooms on the same repo share a single index worktree at
        ``workspaces/{repo_hash}/worktrees/_index/``.
        """
        record = self._worktrees.get(room_id)
        if record is None:
            return None
        index_wt = self._workspaces_dir / record.repo_hash / "worktrees" / "_index"
        return index_wt if index_wt.exists() else None

    # ------------------------------------------------------------------
    # Local workspace registration
    # ------------------------------------------------------------------

    def register_local_workspace(self, room_id: str, local_path: str) -> WorkspaceInfo:
        """Register a local filesystem folder as the workspace for *room_id*.

        This is the "Local Mode" — the backend does NOT clone anything; it
        simply records that the given path is the workspace root for the room.
        The path lives on the developer's machine and may not exist on the
        server (e.g. when backend runs on ECS).  Path validation is done
        by the VS Code extension before calling this endpoint.

        Tool calls for this room are proxied to the extension via WebSocket.
        """
        path = Path(local_path)

        self._local_workspaces[room_id] = path
        logger.info("Local workspace registered for room %s at %s", room_id, path)
        return WorkspaceInfo(
            room_id=room_id,
            repo_url="",
            branch="(local)",
            worktree_path=str(path),
            status=WorktreeStatus.READY,
            mode=WorkspaceMode.LOCAL,
            created_at=datetime.now(UTC),
        )

    def unregister_local_workspace(self, room_id: str) -> None:
        """Remove a previously registered local workspace."""
        removed = self._local_workspaces.pop(room_id, None)
        if removed:
            logger.info("Local workspace unregistered for room %s", room_id)

    def is_local_workspace(self, room_id: str) -> bool:
        """Return True if *room_id* uses a locally-mounted workspace."""
        return room_id in self._local_workspaces

    # ------------------------------------------------------------------
    # Sync (pull)
    # ------------------------------------------------------------------

    async def sync_workspace(self, req: WorkspaceSyncRequest) -> WorkspaceSyncResult:
        record = self._get_record(req.room_id)
        try:
            record.status = WorktreeStatus.SYNCING
            env = await self._build_git_env(req.room_id)
            verb = ["rebase"] if req.rebase else ["pull"]
            await self._run_git(["--work-tree", str(record.worktree_path)] + verb, cwd=record.worktree_path, env=env)
            record.status = WorktreeStatus.READY
            record.last_synced = datetime.now(UTC)
            return WorkspaceSyncResult(room_id=req.room_id, success=True, message="Sync complete")
        except (RuntimeError, OSError) as exc:
            record.status = WorktreeStatus.ERROR
            record.error_detail = str(exc)
            return WorkspaceSyncResult(room_id=req.room_id, success=False, message=str(exc))

    # ------------------------------------------------------------------
    # Commit
    # ------------------------------------------------------------------

    async def commit_workspace(self, req: WorkspaceCommitRequest) -> WorkspaceCommitResult:
        record = self._get_record(req.room_id)
        try:
            env = await self._build_git_env(req.room_id)
            cwd = record.worktree_path

            # Stage all changes
            await self._run_git(["add", "-A"], cwd=cwd, env=env)

            # Build commit command
            git_cmd = ["commit", "-m", req.message]
            if req.author_name and req.author_email:
                git_cmd += [f"--author={req.author_name} <{req.author_email}>"]
            await self._run_git(git_cmd, cwd=cwd, env=env)

            # Retrieve SHA
            sha = await self._get_head_sha(cwd, env)
            return WorkspaceCommitResult(room_id=req.room_id, success=True, sha=sha, message="Commit created")
        except (RuntimeError, OSError) as exc:
            return WorkspaceCommitResult(room_id=req.room_id, success=False, message=str(exc))

    # ------------------------------------------------------------------
    # Push
    # ------------------------------------------------------------------

    async def push_workspace(self, req: WorkspacePushRequest) -> WorkspacePushResult:
        record = self._get_record(req.room_id)
        try:
            env = await self._build_git_env(req.room_id)
            cwd = record.worktree_path
            args = ["push", "origin", record.branch]
            if req.force:
                args.append("--force")
            await self._run_git(args, cwd=cwd, env=env)
            sha = await self._get_head_sha(cwd, env)
            return WorkspacePushResult(
                room_id=req.room_id,
                success=True,
                remote_url=record.repo_url,
                pushed_sha=sha,
                message="Push successful",
            )
        except (RuntimeError, OSError) as exc:
            return WorkspacePushResult(room_id=req.room_id, success=False, message=str(exc))

    # ------------------------------------------------------------------
    # Destroy
    # ------------------------------------------------------------------

    async def destroy_workspace(self, room_id: str) -> WorkspaceDestroyResult:
        # Check local workspaces first — just remove the mapping (never delete local files)
        if room_id in self._local_workspaces:
            self._local_workspaces.pop(room_id)
            return WorkspaceDestroyResult(room_id=room_id, success=True, message="Local workspace unregistered")

        record = self._worktrees.pop(room_id, None)
        if record is None:
            return WorkspaceDestroyResult(room_id=room_id, success=False, message="Workspace not found")
        await self._credential_store.delete(room_id)
        if self._cleanup_on_close and record.worktree_path.exists():
            try:
                shutil.rmtree(record.worktree_path)
                logger.info("Worktree directory removed: %s", record.worktree_path)
            except OSError as exc:
                logger.warning("Could not remove worktree dir: %s", exc)
        record.status = WorktreeStatus.DESTROYED
        return WorkspaceDestroyResult(room_id=room_id, success=True, message="Workspace destroyed")

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get_worktree_path(self, room_id: str) -> Optional[Path]:
        record = self._worktrees.get(room_id)
        if record:
            return record.worktree_path
        return self._local_workspaces.get(room_id)

    @property
    def token_cache(self) -> Optional[RepoTokenCache]:
        """Expose the repo token cache for diagnostics and testing."""
        return self._token_cache

    def list_workspaces(self) -> List[WorkspaceInfo]:
        result = [r.to_info() for r in self._worktrees.values()]
        for room_id, path in self._local_workspaces.items():
            result.append(
                WorkspaceInfo(
                    room_id=room_id,
                    repo_url="",
                    branch="(local)",
                    worktree_path=str(path),
                    status=WorktreeStatus.READY,
                    mode=WorkspaceMode.LOCAL,
                    created_at=datetime.now(UTC),
                )
            )
        return result

    def get_workspace(self, room_id: str) -> Optional[WorkspaceInfo]:
        record = self._worktrees.get(room_id)
        if record:
            return record.to_info()
        local = self._local_workspaces.get(room_id)
        if local:
            return WorkspaceInfo(
                room_id=room_id,
                repo_url="",
                branch="(local)",
                worktree_path=str(local),
                status=WorktreeStatus.READY,
                mode=WorkspaceMode.LOCAL,
                created_at=datetime.now(UTC),
            )
        return None

    # ------------------------------------------------------------------
    # WebSocket file-sync broadcast
    # ------------------------------------------------------------------

    def register_broadcast(self, room_id: str, callback) -> None:  # Callable[[FileSyncEvent], Awaitable[None]]
        self._broadcast_callbacks.setdefault(room_id, []).append(callback)

    def unregister_broadcast(self, room_id: str, callback) -> None:
        cbs = self._broadcast_callbacks.get(room_id, [])
        with contextlib.suppress(ValueError):
            cbs.remove(callback)

    async def broadcast_file_sync(self, room_id: str, changes: List[FileChange], sync_id: str) -> None:
        event = FileSyncEvent(
            room_id=room_id,
            changeset=changes,
            sync_id=sync_id,
        )
        for cb in list(self._broadcast_callbacks.get(room_id, [])):
            try:
                await cb(event)
            except Exception as exc:  # callbacks are user-supplied; must catch all
                logger.warning("Broadcast callback error for room %s: %s", room_id, exc)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _recover_worktrees(self) -> None:
        """Scan the workspaces directory and recover in-memory records for any
        worktrees that already exist on disk.

        Called once at startup so that a backend restart doesn't lose the
        mapping from room_id → worktree_path.  Only sets the fields needed to
        serve file-system and code-search requests; credentials are NOT
        recovered (they are ephemeral by design).
        """
        if not self._workspaces_dir.exists():
            return

        recovered = 0
        for repo_dir in self._workspaces_dir.iterdir():
            if not repo_dir.is_dir():
                continue
            repo_hash = repo_dir.name
            worktrees_dir = repo_dir / "worktrees"
            if not worktrees_dir.is_dir():
                continue

            # Try to get the remote URL from the bare clone (best-effort)
            bare_dir = repo_dir / "bare.git"
            repo_url = ""
            if bare_dir.exists():
                with contextlib.suppress(RuntimeError, OSError):
                    repo_url = await self._run_git(["remote", "get-url", "origin"], cwd=bare_dir)

            for wt_dir in worktrees_dir.iterdir():
                if not wt_dir.is_dir():
                    continue
                room_id = wt_dir.name
                if room_id == "_index":
                    continue  # shared index worktree — not a room

                if room_id in self._worktrees:
                    continue  # already registered

                # Read current branch from git
                branch = f"session/{room_id}"
                with contextlib.suppress(RuntimeError, OSError):
                    branch = await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=wt_dir)

                record = _WorktreeRecord(
                    room_id=room_id,
                    repo_url=repo_url,
                    branch=branch,
                    worktree_path=wt_dir,
                    repo_hash=repo_hash,
                    base_branch_name="",
                )
                record.status = WorktreeStatus.READY
                record.last_synced = datetime.now(UTC)
                self._worktrees[room_id] = record
                recovered += 1

        if recovered:
            logger.info("Recovered %d worktree(s) from disk", recovered)

    def _get_record(self, room_id: str) -> _WorktreeRecord:
        record = self._worktrees.get(room_id)
        if record is None:
            raise KeyError(f"No workspace found for room_id={room_id!r}")
        return record

    async def _build_git_env(self, room_id: str) -> Dict[str, str]:
        """Build an env-var dict for git that injects credentials if available."""
        base_env = os.environ.copy()
        creds = await self._credential_store.get(room_id)
        if creds is None:
            return base_env  # delegate mode – no stored creds

        askpass_path = _make_askpass_script()
        base_env.update(
            {
                "GIT_ASKPASS": askpass_path,
                "GIT_CREDENTIAL_USERNAME": creds.username or "git",
                "GIT_CREDENTIAL_TOKEN": creds.token,
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        return base_env

    # ------------------------------------------------------------------
    # Git progress parsing
    # ------------------------------------------------------------------

    # Matches lines with a percentage, like:
    #   Receiving objects:  45% (5555/12345), 123.45 MiB | 5.67 MiB/s
    #   Resolving deltas: 100% (9876/9876), done.
    _GIT_PROGRESS_RE = re.compile(
        r"(?P<phase>Enumerating|Counting|Compressing|Receiving|Resolving)[^:]*:"
        r"\s*(?P<pct>\d+)%"
        r"\s*\((?P<cur>\d+)/(?P<tot>\d+)\)"
        r"(?:,\s*(?P<bytes>[\d.]+\s*[A-Za-z]+))?"
        r"(?:\s*\|\s*(?P<speed>[\d.]+\s*[A-Za-z/]+))?",
    )

    # Matches "Enumerating objects: 12345" (no percentage)
    _GIT_ENUM_RE = re.compile(
        r"(?P<phase>Enumerating)[^:]*:\s*(?P<count>\d+)",
    )

    @staticmethod
    def _parse_git_progress(line: str) -> Optional[CloneProgress]:
        """Parse a single git stderr progress line into a CloneProgress."""
        m = GitWorkspaceService._GIT_PROGRESS_RE.search(line)
        if m:
            phase_map = {
                "Enumerating": "counting",
                "Counting": "counting",
                "Compressing": "compressing",
                "Receiving": "receiving",
                "Resolving": "resolving",
            }
            return CloneProgress(
                phase=phase_map.get(m.group("phase"), m.group("phase").lower()),
                percent=int(m.group("pct")),
                current=int(m.group("cur")),
                total=int(m.group("tot")),
                bytes_received=m.group("bytes") or "",
                throughput=m.group("speed") or "",
            )
        # Fallback: enumerating line without percentage
        m2 = GitWorkspaceService._GIT_ENUM_RE.search(line)
        if m2:
            return CloneProgress(
                phase="counting",
                percent=0,
                current=int(m2.group("count")),
                total=0,
            )
        return None

    @staticmethod
    async def _run_git_with_progress(
        args: List[str],
        on_progress: Callable[[CloneProgress], None],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Run a git command, streaming stderr progress to *on_progress*."""
        cmd = ["git"] + args
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )

        # Read stderr incrementally.  Git progress uses \r for in-place
        # updates, so we split on both \r and \n.
        collected_stderr: list[bytes] = []
        assert proc.stderr is not None
        buf = b""
        while True:
            chunk = await proc.stderr.read(512)
            if not chunk:
                break
            collected_stderr.append(chunk)
            buf += chunk
            # Split on \r or \n to get individual progress lines
            while b"\r" in buf or b"\n" in buf:
                idx_r = buf.find(b"\r")
                idx_n = buf.find(b"\n")
                if idx_r == -1:
                    idx = idx_n
                elif idx_n == -1:
                    idx = idx_r
                else:
                    idx = min(idx_r, idx_n)
                line = buf[:idx].decode(errors="replace").strip()
                buf = buf[idx + 1 :]
                if line:
                    prog = GitWorkspaceService._parse_git_progress(line)
                    if prog:
                        on_progress(prog)

        stdout_b = await proc.stdout.read() if proc.stdout else b""
        await proc.wait()

        stderr_full = b"".join(collected_stderr).decode(errors="replace").strip()
        stdout = stdout_b.decode(errors="replace").strip()

        if proc.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed (exit {proc.returncode}): {stderr_full or stdout}")
        return stdout

    @staticmethod
    async def _run_git(
        args: List[str],
        cwd: Optional[Path] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> str:
        """Run a git sub-command asynchronously; return stdout."""
        cmd = ["git"] + args
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        stdout_b, stderr_b = await proc.communicate()
        stdout = stdout_b.decode(errors="replace").strip()
        stderr = stderr_b.decode(errors="replace").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"git {args[0]} failed (exit {proc.returncode}): {stderr or stdout}")
        return stdout

    async def _get_head_sha(self, cwd: Path, env: Dict[str, str]) -> Optional[str]:
        try:
            return await self._run_git(["rev-parse", "HEAD"], cwd=cwd, env=env)
        except (RuntimeError, OSError):
            return None
