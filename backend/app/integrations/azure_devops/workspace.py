"""Azure DevOps workspace management.

Handles auto-cloning and fetching of the target repository so that
the code review pipeline has a local git workspace to operate on.

The workspace is a full clone (not --no-checkout) because the review
agents need to read arbitrary files on disk (grep, read_file,
find_symbol, get_dependencies, etc.), not just the diff.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Default root for auto-managed workspaces
_DEFAULT_ROOT = Path.home() / ".conductor" / "azure_workspaces"


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
    """Ensure a local git workspace exists and is up to date.

    If *workspace_path* is provided and already exists, runs ``git fetch``.
    If *workspace_path* is empty, derives a default path under
    ``~/.conductor/azure_workspaces/{repo_name}``.
    If the directory does not exist, runs ``git clone``.

    Returns:
        The absolute workspace path, or None if clone/fetch failed.
    """
    if not repo_url:
        logger.warning("[AzureDevOps] No repo_url configured — workspace not available")
        return None

    # Resolve workspace path
    if workspace_path:
        ws = Path(workspace_path)
    else:
        repo_name = _repo_name_from_url(repo_url)
        ws = _DEFAULT_ROOT / repo_name

    ws_str = str(ws.resolve())
    auth_url = _inject_pat(repo_url, pat)

    if ws.exists() and (ws / ".git").exists():
        # Already cloned — fetch latest
        logger.info("[AzureDevOps] Fetching latest for %s", ws_str)
        rc, _, stderr = await _run(["git", "fetch", "--all", "--prune"], cwd=ws_str)
        if rc != 0:
            logger.error("[AzureDevOps] git fetch failed: %s", stderr.strip())
            return ws_str  # still usable, just not latest
        logger.info("[AzureDevOps] Fetch complete for %s", ws_str)
        return ws_str

    # Full clone — agents need files on disk for grep, read_file, etc.
    ws.parent.mkdir(parents=True, exist_ok=True)
    logger.info("[AzureDevOps] Cloning %s → %s", repo_url, ws_str)
    rc, _, stderr = await _run(["git", "clone", auth_url, ws_str])
    if rc != 0:
        logger.error("[AzureDevOps] git clone failed: %s", stderr.strip())
        return None

    logger.info("[AzureDevOps] Clone complete: %s", ws_str)
    return ws_str


async def prepare_for_review(
    workspace_path: str,
    source_branch: str,
    target_branch: str,
) -> bool:
    """Prepare the workspace for a PR review.

    1. Fetch all remotes to get latest branch refs.
    2. Checkout the source branch so agents can read the PR's code on disk.

    Returns True if workspace is ready.
    """
    ws = workspace_path

    # Fetch latest
    rc, _, stderr = await _run(["git", "fetch", "--all", "--prune"], cwd=ws)
    if rc != 0:
        logger.error("[AzureDevOps] git fetch failed: %s", stderr.strip())
        return False

    # Checkout source branch (the PR's code) so agents see the new files.
    # Try local branch first; if it doesn't exist, create from origin/.
    rc, _, _ = await _run(["git", "checkout", source_branch], cwd=ws)
    if rc != 0:
        # Local branch doesn't exist — create tracking branch
        rc, _, stderr = await _run(
            ["git", "checkout", "-b", source_branch, f"origin/{source_branch}"],
            cwd=ws,
        )
        if rc != 0:
            logger.error("[AzureDevOps] checkout %s failed: %s", source_branch, stderr.strip())
            return False
    else:
        # Local branch exists — pull latest from origin
        await _run(["git", "reset", "--hard", f"origin/{source_branch}"], cwd=ws)

    logger.info("[AzureDevOps] Workspace checked out to %s", source_branch)
    return True
