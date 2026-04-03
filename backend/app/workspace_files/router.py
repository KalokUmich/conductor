"""REST endpoints for reading/writing files inside a git worktree.

These endpoints back the ``conductor://`` virtual file-system provider in the
VS Code extension (``ConductorFileSystemProvider``).

URL layout (matches what the FS provider emits):

    /workspace/{room_id}/files/{path:path}/stat     GET   → FileStat
    /workspace/{room_id}/files/{path:path}/content   GET   → raw bytes
    /workspace/{room_id}/files/{path:path}/content   PUT   → write bytes
    /workspace/{room_id}/files/{path:path}/rename    POST  → rename / move
    /workspace/{room_id}/files/{path:path}           GET   → dir listing
    /workspace/{room_id}/files/{path:path}           POST  → mkdir
    /workspace/{room_id}/files/{path:path}           DELETE→ rm
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel

from ..git_workspace.service import GitWorkspaceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/workspace", tags=["workspace-files"])


# ---------------------------------------------------------------------------
# Dependency
# ---------------------------------------------------------------------------


def _get_git_service() -> GitWorkspaceService:  # pragma: no cover
    from ..main import app

    return app.state.git_workspace_service


def _guard_writable(room_id: str, svc: GitWorkspaceService) -> None:
    """Raise 403 if the workspace is in local (read-only) mode."""
    if svc.is_local_workspace(room_id):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Workspace is in local mode (read-only for remote users).",
        )


# Directories that VS Code auto-probes when a workspace folder is mounted.
# Blocking access prevents the remote workspace from overriding local settings,
# loading unknown tasks/launch configs, or triggering slow extension scans.
_BLOCKED_ROOTS = {".vscode", ".idea", ".devcontainer", "node_modules", ".git"}


def _resolve(
    room_id: str,
    file_path: str,
    svc: GitWorkspaceService,
) -> Path:
    """Resolve *file_path* to an absolute path inside the room's worktree.

    Raises 404 if the workspace doesn't exist or the path is in a blocked
    directory, 403 if the path escapes the worktree root (path traversal guard).
    """
    wt = svc.get_worktree_path(room_id)
    if wt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")

    # Block access to directories that cause VS Code to misbehave
    first_segment = file_path.split("/")[0] if file_path else ""
    if first_segment in _BLOCKED_ROOTS:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")

    resolved = (wt / file_path).resolve()
    # Guard against path traversal
    if not str(resolved).startswith(str(wt.resolve())):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Path traversal denied")
    return resolved


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class FileStatResponse(BaseModel):
    type: str  # "file" | "directory"
    size: int
    ctime: float
    mtime: float


class DirEntry(BaseModel):
    name: str
    type: str  # "file" | "directory"


class RenameRequest(BaseModel):
    new_path: str
    overwrite: bool = False


class MkdirRequest(BaseModel):
    type: str = "directory"


# ---------------------------------------------------------------------------
# stat
# ---------------------------------------------------------------------------


@router.get("/{room_id}/files/stat", response_model=FileStatResponse)
@router.get("/{room_id}/files/{file_path:path}/stat", response_model=FileStatResponse)
async def file_stat(
    room_id: str,
    svc: GitWorkspaceService = Depends(_get_git_service),
    file_path: str = "",
) -> FileStatResponse:
    resolved = _resolve(room_id, file_path, svc)
    if not resolved.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    st = resolved.stat()
    return FileStatResponse(
        type="directory" if resolved.is_dir() else "file",
        size=st.st_size,
        ctime=st.st_ctime_ns // 1_000_000,
        mtime=st.st_mtime_ns // 1_000_000,
    )


# ---------------------------------------------------------------------------
# readFile (content)
# ---------------------------------------------------------------------------


@router.get("/{room_id}/files/{file_path:path}/content")
async def read_file_content(
    room_id: str,
    file_path: str,
    svc: GitWorkspaceService = Depends(_get_git_service),
) -> Response:
    resolved = _resolve(room_id, file_path, svc)
    if not resolved.exists() or resolved.is_dir():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    data = resolved.read_bytes()
    return Response(content=data, media_type="application/octet-stream")


# ---------------------------------------------------------------------------
# writeFile (content)
# ---------------------------------------------------------------------------


@router.put("/{room_id}/files/{file_path:path}/content")
async def write_file_content(
    room_id: str,
    file_path: str,
    request: Request,
    svc: GitWorkspaceService = Depends(_get_git_service),
) -> dict:
    _guard_writable(room_id, svc)
    resolved = _resolve(room_id, file_path, svc)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    body = await request.body()
    resolved.write_bytes(body)
    return {"ok": True}


# ---------------------------------------------------------------------------
# readDirectory
# ---------------------------------------------------------------------------


@router.get("/{room_id}/files", response_model=List[DirEntry])
@router.get("/{room_id}/files/{file_path:path}", response_model=List[DirEntry])
async def read_directory(
    room_id: str,
    svc: GitWorkspaceService = Depends(_get_git_service),
    file_path: str = "",
) -> List[DirEntry]:
    resolved = _resolve(room_id, file_path, svc)
    if not resolved.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if not resolved.is_dir():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Not a directory")
    # Directories that VS Code probes automatically when a workspace folder
    # is mounted.  Hiding them from listings prevents VS Code from loading
    # remote settings/tasks/extensions that slow down or conflict with the
    # local workspace.
    _HIDDEN_DIRS = {".git", ".vscode", ".idea", ".devcontainer", "node_modules"}

    entries: List[DirEntry] = []
    for child in sorted(resolved.iterdir()):
        if child.name in _HIDDEN_DIRS:
            continue
        entries.append(
            DirEntry(
                name=child.name,
                type="directory" if child.is_dir() else "file",
            )
        )
    return entries


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


@router.post("/{room_id}/files/{file_path:path}/rename")
async def rename_file(
    room_id: str,
    file_path: str,
    body: RenameRequest,
    svc: GitWorkspaceService = Depends(_get_git_service),
) -> dict:
    _guard_writable(room_id, svc)
    resolved = _resolve(room_id, file_path, svc)
    if not resolved.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    new_resolved = _resolve(room_id, body.new_path, svc)
    if new_resolved.exists() and not body.overwrite:
        raise HTTPException(status.HTTP_409_CONFLICT, "Target already exists")
    new_resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.rename(new_resolved)
    return {"ok": True}


# ---------------------------------------------------------------------------
# createDirectory
# ---------------------------------------------------------------------------


@router.post("/{room_id}/files/{file_path:path}")
async def create_directory(
    room_id: str,
    file_path: str,
    body: MkdirRequest,
    svc: GitWorkspaceService = Depends(_get_git_service),
) -> dict:
    _guard_writable(room_id, svc)
    resolved = _resolve(room_id, file_path, svc)
    if resolved.exists():
        raise HTTPException(status.HTTP_409_CONFLICT, "Already exists")
    resolved.mkdir(parents=True, exist_ok=True)
    return {"ok": True}


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@router.delete("/{room_id}/files/{file_path:path}")
async def delete_file(
    room_id: str,
    file_path: str,
    recursive: bool = False,
    svc: GitWorkspaceService = Depends(_get_git_service),
) -> dict:
    _guard_writable(room_id, svc)
    resolved = _resolve(room_id, file_path, svc)
    if not resolved.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not found")
    if resolved.is_dir():
        if recursive:
            shutil.rmtree(resolved)
        else:
            try:
                resolved.rmdir()
            except OSError as exc:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    "Directory not empty; use recursive=true",
                ) from exc
    else:
        resolved.unlink()
    return {"ok": True}


# ---------------------------------------------------------------------------
# search (text grep)
# ---------------------------------------------------------------------------


class SearchRequest(BaseModel):
    pattern: str
    glob: str = ""  # e.g. "*.py", empty = all files
    max_results: int = 200


class SearchMatch(BaseModel):
    path: str
    line: int  # 1-based
    text: str  # line content
    col_start: int  # 0-based byte offset
    col_end: int


class SearchResponse(BaseModel):
    matches: List[SearchMatch]
    truncated: bool = False


@router.post("/{room_id}/search", response_model=SearchResponse)
async def search_text(
    room_id: str,
    body: SearchRequest,
    svc: GitWorkspaceService = Depends(_get_git_service),
) -> SearchResponse:
    """Run a ripgrep / grep over the worktree and return matches."""
    import asyncio as _aio

    wt = svc.get_worktree_path(room_id)
    if wt is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")

    # Try ripgrep first, fall back to grep
    rg_args = [
        "rg",
        "--json",
        "--max-count=5",
        "--max-filesize=1M",
    ]
    for br in _BLOCKED_ROOTS:
        rg_args += [f"--glob=!{br}"]
    if body.glob:
        rg_args += [f"--glob={body.glob}"]
    rg_args += ["--", body.pattern, str(wt)]

    try:
        proc = await _aio.create_subprocess_exec(
            *rg_args,
            stdout=_aio.subprocess.PIPE,
            stderr=_aio.subprocess.PIPE,
        )
        stdout_b, _ = await proc.communicate()
    except FileNotFoundError:
        # rg not installed — fall back to grep
        grep_args = ["grep", "-rn", "--include=" + (body.glob or "*"), "-m", "5", body.pattern, str(wt)]
        proc = await _aio.create_subprocess_exec(
            *grep_args,
            stdout=_aio.subprocess.PIPE,
            stderr=_aio.subprocess.PIPE,
        )
        stdout_b, _ = await proc.communicate()
        # Parse grep output: file:line:text
        matches: List[SearchMatch] = []
        wt_str = str(wt) + "/"
        for raw_line in stdout_b.decode(errors="replace").splitlines():
            parts = raw_line.split(":", 2)
            if len(parts) < 3:
                continue
            fpath = parts[0].replace(wt_str, "", 1)
            if any(fpath.startswith(b + "/") for b in _BLOCKED_ROOTS):
                continue
            try:
                line_num = int(parts[1])
            except ValueError:
                continue
            text = parts[2]
            idx = text.lower().find(body.pattern.lower())
            matches.append(
                SearchMatch(
                    path=fpath,
                    line=line_num,
                    text=text,
                    col_start=max(idx, 0),
                    col_end=max(idx, 0) + len(body.pattern),
                )
            )
            if len(matches) >= body.max_results:
                return SearchResponse(matches=matches, truncated=True)
        return SearchResponse(matches=matches)

    # Parse ripgrep JSON output
    import json as _json

    matches = []
    wt_str = str(wt) + "/"
    for raw_line in stdout_b.decode(errors="replace").splitlines():
        try:
            obj = _json.loads(raw_line)
        except _json.JSONDecodeError:
            continue
        if obj.get("type") != "match":
            continue
        data = obj["data"]
        fpath = data["path"]["text"].replace(wt_str, "", 1)
        for sub in data.get("submatches", []):
            matches.append(
                SearchMatch(
                    path=fpath,
                    line=data["line_number"],
                    text=data["lines"]["text"].rstrip("\n"),
                    col_start=sub["start"],
                    col_end=sub["end"],
                )
            )
            if len(matches) >= body.max_results:
                return SearchResponse(matches=matches, truncated=True)

    return SearchResponse(matches=matches)
