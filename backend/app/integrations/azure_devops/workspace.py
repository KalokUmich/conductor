"""Azure DevOps workspace management.

Two-tier workspace architecture:

  ~/.conductor/
  └── pr_workspaces/
      └── abound-server/               ← main clone (startup, shared .git objects)
          abound-server-pr-14126/       ← worktree per PR review (temporary)
          abound-server-pr-14130/       ← concurrent reviews don't conflict

Main clone is created once at startup via ``ensure_workspace()``.
Each PR review gets its own worktree via ``create_pr_worktree()``,
which is cleaned up after the review via ``cleanup_pr_worktree()``.

Worktrees share .git objects with the main clone — creation is near-instant
and disk usage is minimal (only checked-out files, not full history).
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Default root for PR review workspaces (separate from chat room workspaces)
_DEFAULT_ROOT = Path.home() / ".conductor" / "pr_workspaces"


def _repo_name_from_url(repo_url: str) -> str:
    """Extract the repository name from an Azure DevOps git URL.

    Handles:
      https://dev.azure.com/org/project/_git/repo-name
      git@ssh.dev.azure.com:v3/org/project/repo-name
    """
    # HTTPS format
    match = re.search(r"/_git/([^/?#]+)", repo_url)
    if match:
        return match.group(1)
    # SSH format
    match = re.search(r"/([^/]+)$", repo_url)
    if match:
        return match.group(1)
    return "repo"


def _inject_pat(repo_url: str, pat: str) -> str:
    """Inject PAT into the HTTPS clone URL for authentication.

    Transforms:
      https://dev.azure.com/org/project/_git/repo
    Into:
      https://pat@dev.azure.com/org/project/_git/repo
    """
    return re.sub(r"^https://", f"https://{pat}@", repo_url)


async def _run(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a subprocess and return (returncode, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    return proc.returncode, stdout.decode(), stderr.decode()


async def ensure_workspace(
    repo_url: str,
    pat: str,
    workspace_path: str = "",
) -> str | None:
    """Ensure the main clone exists and is up to date.

    Called once at startup. Creates the shared clone that all PR worktrees
    are derived from. If already exists, runs ``git fetch``.

    Returns:
        The absolute path to the main clone, or None on failure.
    """
    if not repo_url:
        logger.warning("[AzureDevOps] No repo_url configured — workspace not available")
        return None

    if workspace_path:
        ws = Path(workspace_path)
    else:
        repo_name = _repo_name_from_url(repo_url)
        ws = _DEFAULT_ROOT / repo_name

    ws_str = str(ws.resolve())
    auth_url = _inject_pat(repo_url, pat)

    if ws.exists() and (ws / ".git").exists():
        logger.info("[AzureDevOps] Fetching latest for %s", ws_str)
        rc, _, stderr = await _run(["git", "fetch", "--all", "--prune"], cwd=ws_str)
        if rc != 0:
            logger.error("[AzureDevOps] git fetch failed: %s", stderr.strip())
            return ws_str
        logger.info("[AzureDevOps] Fetch complete for %s", ws_str)
        return ws_str

    ws.parent.mkdir(parents=True, exist_ok=True)
    logger.info("[AzureDevOps] Cloning %s → %s", repo_url, ws_str)
    rc, _, stderr = await _run(["git", "clone", auth_url, ws_str])
    if rc != 0:
        logger.error("[AzureDevOps] git clone failed: %s", stderr.strip())
        return None

    logger.info("[AzureDevOps] Clone complete: %s", ws_str)
    return ws_str


async def create_pr_worktree(
    main_workspace: str,
    source_branch: str,
    pr_id: int,
) -> str | None:
    """Create an isolated worktree for a PR review.

    1. Fetch latest refs from all remotes.
    2. Create a worktree checked out to the source branch.

    The worktree is placed alongside the main clone:
      {main_workspace}-pr-{pr_id}/

    Returns:
        Absolute path to the worktree, or None on failure.
    """
    ws = main_workspace
    worktree_path = f"{ws}-pr-{pr_id}"

    # Clean up stale worktree if it exists (e.g., from a crashed review)
    wt = Path(worktree_path)
    if wt.exists():
        logger.info("[AzureDevOps] Cleaning stale worktree: %s", worktree_path)
        await _run(["git", "worktree", "remove", "--force", worktree_path], cwd=ws)
        # If git worktree remove fails (e.g., locked), force-delete the directory
        if wt.exists():
            import shutil

            shutil.rmtree(worktree_path, ignore_errors=True)

    # Fetch latest
    rc, _, stderr = await _run(["git", "fetch", "--all", "--prune"], cwd=ws)
    if rc != 0:
        logger.error("[AzureDevOps] git fetch failed: %s", stderr.strip())
        return None

    # Create worktree from origin/source_branch
    rc, _, stderr = await _run(
        ["git", "worktree", "add", "--detach", worktree_path, f"origin/{source_branch}"],
        cwd=ws,
    )
    if rc != 0:
        logger.error(
            "[AzureDevOps] worktree add failed for PR #%d: %s",
            pr_id,
            stderr.strip(),
        )
        return None

    logger.info("[AzureDevOps] Worktree created: %s (branch: %s)", worktree_path, source_branch)
    return worktree_path


async def fetch_latest(workspace_path: str) -> bool:
    """Run ``git fetch --all --prune`` on the main clone.

    Used to refresh remote refs before line-count checks (no worktree needed).
    """
    rc, _, stderr = await _run(["git", "fetch", "--all", "--prune"], cwd=workspace_path)
    if rc != 0:
        logger.error("[AzureDevOps] git fetch failed: %s", stderr.strip())
        return False
    return True


async def cleanup_pr_worktree(main_workspace: str, worktree_path: str) -> None:
    """Remove a PR review worktree after the review is complete."""
    rc, _, stderr = await _run(
        ["git", "worktree", "remove", "--force", worktree_path],
        cwd=main_workspace,
    )
    if rc != 0:
        logger.warning("[AzureDevOps] worktree cleanup failed: %s", stderr.strip())
        # Fallback: force-delete the directory
        import shutil

        shutil.rmtree(worktree_path, ignore_errors=True)
    else:
        logger.info("[AzureDevOps] Worktree cleaned up: %s", worktree_path)

    # Prune stale worktree refs
    await _run(["git", "worktree", "prune"], cwd=main_workspace)
