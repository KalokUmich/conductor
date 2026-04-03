"""FastAPI router for the Git Workspace module.

All endpoints are under the /api/git-workspace prefix (registered in main.py).
"""

from __future__ import annotations

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, status

from .delegate_broker import DelegateBroker
from .schemas import (
    CredentialPayload,
    GitWorkspaceHealth,
    ListRemoteBranchesRequest,
    ListRemoteBranchesResponse,
    LocalWorkspaceRequest,
    LocalWorkspaceResult,
    SetupAndIndexRequest,
    SetupAndIndexResult,
    WorkspaceCommitRequest,
    WorkspaceCommitResult,
    WorkspaceCreateRequest,
    WorkspaceDestroyResult,
    WorkspaceInfo,
    WorkspacePushRequest,
    WorkspacePushResult,
    WorkspaceSyncRequest,
    WorkspaceSyncResult,
    WorktreeStatus,
)
from .service import GitWorkspaceService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/git-workspace", tags=["git-workspace"])

# Service instances are injected via FastAPI dependency injection.
# In main.py, a single instance is created at startup and stored in app.state.


def get_git_service(  # pragma: no cover
    # This will be overridden in tests via app.dependency_overrides
) -> GitWorkspaceService:
    from app.main import app  # lazy import to avoid circular dependency

    return app.state.git_workspace_service


def get_delegate_broker() -> DelegateBroker:  # pragma: no cover
    from app.main import app

    return app.state.delegate_broker


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------


@router.get("/token-cache")
async def list_token_cache(
    svc: GitWorkspaceService = Depends(get_git_service),
) -> dict:
    """List cached repo tokens (tokens redacted).

    Returns metadata about which repos have cached credentials, and when they
    expire.  Useful for debugging authentication issues.
    """
    cache = svc.token_cache
    if cache is None:
        return {"enabled": False, "entries": []}
    entries = await cache.list_entries()
    return {"enabled": True, "count": len(entries), "entries": entries}


@router.get("/health", response_model=GitWorkspaceHealth)
async def health(
    svc: GitWorkspaceService = Depends(get_git_service),
) -> GitWorkspaceHealth:
    """Basic health check for the git workspace module."""
    import subprocess

    try:
        result = subprocess.run(["git", "--version"], capture_output=True, text=True, timeout=5)
        git_version = result.stdout.strip()
    except Exception as exc:  # pylint: disable=broad-except
        return GitWorkspaceHealth(
            status="error",
            active_rooms=0,
            git_version="unknown",
            detail=str(exc),
        )

    workspaces = svc.list_workspaces()
    return GitWorkspaceHealth(
        status="ok",
        active_rooms=len(workspaces),
        git_version=git_version,
    )


@router.post("/branches/remote", response_model=ListRemoteBranchesResponse)
async def list_remote_branches(
    req: ListRemoteBranchesRequest,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> ListRemoteBranchesResponse:
    """List branches on a remote git repository."""
    try:
        branches, default = await svc.list_remote_branches(req.repo_url, req.credentials)
        return ListRemoteBranchesResponse(branches=branches, default_branch=default)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc


@router.post("/workspaces/local", response_model=LocalWorkspaceResult)
async def register_local_workspace(
    req: LocalWorkspaceRequest,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> LocalWorkspaceResult:
    """Register a local filesystem folder as the workspace for a room.

    This is the **Local Mode** — no git clone is performed.  The host's
    local folder is used directly for code-intelligence tools.  Guests
    joining this room get read-only access (cannot edit files).
    """
    try:
        info = svc.register_local_workspace(req.room_id, req.local_path)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    return LocalWorkspaceResult(
        room_id=req.room_id,
        workspace=info,
        message="Local workspace registered.",
    )


@router.delete("/workspaces/{room_id}/local")
async def unregister_local_workspace(
    room_id: str,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> dict:
    """Remove a local workspace registration (does NOT delete any files)."""
    svc.unregister_local_workspace(room_id)
    return {"status": "ok", "room_id": room_id}


@router.post("/workspaces/setup-and-index", response_model=SetupAndIndexResult)
async def setup_and_index(
    req: SetupAndIndexRequest,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> SetupAndIndexResult:
    """Kick off workspace creation and return immediately.

    The clone runs in the background.  The client should poll
    ``GET /workspaces/{room_id}`` for ``status`` and ``clone_progress``.
    Once ``status == "ready"`` the client can optionally call
    ``POST /workspaces/{room_id}/index`` to trigger code-search indexing.
    """
    create_req = WorkspaceCreateRequest(
        room_id=req.room_id,
        repo_url=req.repo_url,
        base_branch=req.source_branch,
        credentials=req.credentials,
    )
    try:
        workspace_info = await svc.create_workspace(create_req)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # Return immediately — let the frontend poll for progress.
    return SetupAndIndexResult(
        room_id=req.room_id,
        workspace=workspace_info,
        message="Workspace clone started. Poll GET /api/git-workspace/workspaces/{room_id} for progress.",
    )


@router.post("/workspaces/{room_id}/index", response_model=SetupAndIndexResult)
async def index_workspace(
    room_id: str,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> SetupAndIndexResult:
    """Trigger code-search indexing for an already-ready workspace.

    **Deprecated** — code search now uses the agent loop (POST /api/context/query).
    This endpoint returns a no-op success when the code_search_service is not
    configured, so older extensions don't break.
    """
    info = svc.get_workspace(room_id)
    if info is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Workspace not found")
    if info.status != WorktreeStatus.READY:
        raise HTTPException(status.HTTP_409_CONFLICT, f"Workspace not ready (status={info.status})")

    # Use the shared index worktree (one per repo) instead of the
    # room-specific worktree so all rooms on the same repo share the index.
    index_wt = svc.get_index_worktree_path(room_id)
    if index_wt is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Index worktree not available yet")

    from app.main import app as _app

    code_search_svc = getattr(_app.state, "code_search_service", None)
    if code_search_svc is None:
        return SetupAndIndexResult(
            room_id=room_id,
            workspace=info,
            index_success=True,
            message="Indexing skipped — code search uses agent loop now.",
        )

    try:
        index_result = await code_search_svc.build_index(
            workspace_path=str(index_wt),
            force_rebuild=False,
        )
        return SetupAndIndexResult(
            room_id=room_id,
            workspace=info,
            index_success=index_result.success,
            files_indexed=index_result.files_indexed,
            chunks_indexed=index_result.chunks_indexed,
            index_duration_ms=index_result.duration_ms,
            message=index_result.message,
        )
    except Exception as exc:
        logger.error("Indexing failed for room %s: %s", room_id, exc)
        return SetupAndIndexResult(
            room_id=room_id,
            workspace=info,
            index_success=False,
            message=f"Indexing failed: {exc}",
        )


@router.post("/workspaces", response_model=WorkspaceInfo, status_code=status.HTTP_201_CREATED)
async def create_workspace(
    req: WorkspaceCreateRequest,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> WorkspaceInfo:
    """Create a new git-backed workspace for a room."""
    try:
        return await svc.create_workspace(req)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.get("/workspaces", response_model=List[WorkspaceInfo])
async def list_workspaces(
    svc: GitWorkspaceService = Depends(get_git_service),
) -> List[WorkspaceInfo]:
    """List all active workspaces."""
    return svc.list_workspaces()


@router.get("/workspaces/{room_id}", response_model=WorkspaceInfo)
async def get_workspace(
    room_id: str,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> WorkspaceInfo:
    """Get details for a specific workspace."""
    info = svc.get_workspace(room_id)
    if info is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No workspace found for room_id={room_id!r}",
        )
    return info


@router.post("/workspaces/{room_id}/credentials")
async def upload_credentials(
    room_id: str,
    payload: CredentialPayload,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> dict:
    """Upload (or replace) credentials for a workspace (Mode A)."""
    await svc.store_credentials(room_id, payload)
    return {"status": "ok", "room_id": room_id}


@router.delete("/workspaces/{room_id}/credentials")
async def revoke_credentials(
    room_id: str,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> dict:
    """Revoke stored credentials for a workspace."""
    await svc.revoke_credentials(room_id)
    return {"status": "ok", "room_id": room_id}


@router.post("/workspaces/{room_id}/sync", response_model=WorkspaceSyncResult)
async def sync_workspace(
    room_id: str,
    req: WorkspaceSyncRequest,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> WorkspaceSyncResult:
    """Pull the latest changes from remote into the worktree."""
    if svc.is_local_workspace(room_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Sync not available in local mode.")
    req.room_id = room_id
    return await svc.sync_workspace(req)


@router.post("/workspaces/{room_id}/commit", response_model=WorkspaceCommitResult)
async def commit_workspace(
    room_id: str,
    req: WorkspaceCommitRequest,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> WorkspaceCommitResult:
    """Stage all changes and create a commit."""
    if svc.is_local_workspace(room_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Commit not available in local mode.")
    req.room_id = room_id
    return await svc.commit_workspace(req)


@router.post("/workspaces/{room_id}/push", response_model=WorkspacePushResult)
async def push_workspace(
    room_id: str,
    req: WorkspacePushRequest,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> WorkspacePushResult:
    """Push the worktree branch to the remote."""
    if svc.is_local_workspace(room_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Push not available in local mode.")
    req.room_id = room_id
    return await svc.push_workspace(req)


@router.delete("/workspaces/{room_id}", response_model=WorkspaceDestroyResult)
async def destroy_workspace(
    room_id: str,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> WorkspaceDestroyResult:
    """Destroy a workspace and clean up the worktree."""
    return await svc.destroy_workspace(room_id)


# ---------------------------------------------------------------------------
# WebSocket — file-sync stream
# ---------------------------------------------------------------------------


@router.websocket("/ws/{room_id}/file-sync")
async def file_sync_ws(
    websocket: WebSocket,
    room_id: str,
    svc: GitWorkspaceService = Depends(get_git_service),
) -> None:
    """WebSocket endpoint for real-time file-sync events."""
    await websocket.accept()

    async def _send_event(event) -> None:
        await websocket.send_json(event.model_dump(mode="json"))

    svc.register_broadcast(room_id, _send_event)
    try:
        while True:
            await websocket.receive_text()  # keep-alive / heartbeat
    except WebSocketDisconnect:
        logger.info("file-sync WS disconnected: room=%s", room_id)
    finally:
        svc.unregister_broadcast(room_id, _send_event)


# ---------------------------------------------------------------------------
# WebSocket — credential delegation (Mode B)
# ---------------------------------------------------------------------------


@router.websocket("/ws/{room_id}/delegate-auth")
async def delegate_auth_ws(
    websocket: WebSocket,
    room_id: str,
    broker: DelegateBroker = Depends(get_delegate_broker),
) -> None:
    """WebSocket endpoint for Mode B credential delegation."""
    await websocket.accept()
    await broker.handle_client(room_id, websocket)
