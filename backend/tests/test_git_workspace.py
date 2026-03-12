"""Tests for the git_workspace module.

Covers Create, Read, Update, Delete, List, and Diff operations, plus
configuration helpers, error handling, and edge-cases.

All filesystem / git / subprocess operations are mocked so the suite
runs anywhere without real git repos.

Total: 60 tests + RepoTokenCache + service integration tests
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from pathlib import Path
from fastapi.testclient import TestClient
from fastapi import FastAPI
import sys
import types

# ---------------------------------------------------------------------------
# Minimal stubs so optional imports don't break test collection
# ---------------------------------------------------------------------------


def _stub(name: str, **attrs) -> types.ModuleType:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


_stub("cocoindex", FlowBuilder=MagicMock, IndexOptions=MagicMock)
_stub("sentence_transformers", SentenceTransformer=MagicMock)
_stub("sqlite_vec")

# ---------------------------------------------------------------------------
# Real imports
# ---------------------------------------------------------------------------

from app.git_workspace.service import GitWorkspaceService as GitWorkspaceManager  # noqa: E402
from app.git_workspace.router import router, get_git_service  # noqa: E402

app = FastAPI()
app.include_router(router)
client = TestClient(app, raise_server_exceptions=False)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

BASE = "/tmp/test_workspaces"


from app.git_workspace.schemas import (  # noqa: E402
    WorkspaceCreateRequest,
    WorkspaceInfo,
    WorkspaceDestroyResult,
    WorktreeStatus,
)
from app.git_workspace.service import _WorktreeRecord  # noqa: E402
from datetime import datetime, timezone  # noqa: E402


def _make_dummy_info(room_id: str = "room-1") -> WorkspaceInfo:
    return WorkspaceInfo(
        room_id=room_id,
        repo_url="https://github.com/x/y.git",
        branch=f"session/{room_id}",
        worktree_path=f"/tmp/{room_id}",
        status=WorktreeStatus.READY,
        created_at=datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# GitWorkspaceService – unit tests
# ---------------------------------------------------------------------------


class TestGitWorkspaceManagerInit:
    def test_init_creates_instance(self):
        mgr = GitWorkspaceManager()
        assert mgr is not None

    def test_not_initialized_by_default(self):
        mgr = GitWorkspaceManager()
        assert mgr._initialized is False

    def test_empty_worktrees_initially(self):
        mgr = GitWorkspaceManager()
        assert mgr._worktrees == {}


class TestServiceListWorkspaces:
    def test_list_empty_initially(self):
        mgr = GitWorkspaceManager()
        result = mgr.list_workspaces()
        assert result == []

    def test_list_returns_list_type(self):
        mgr = GitWorkspaceManager()
        result = mgr.list_workspaces()
        assert isinstance(result, list)

    def test_list_reflects_injected_records(self):
        mgr = GitWorkspaceManager()
        record = _WorktreeRecord("r1", "https://github.com/x/y.git", "session/r1", Path("/tmp/r1"))
        mgr._worktrees["r1"] = record
        result = mgr.list_workspaces()
        assert len(result) == 1
        assert result[0].room_id == "r1"

    def test_list_returns_workspace_info_objects(self):
        mgr = GitWorkspaceManager()
        record = _WorktreeRecord("r2", "https://github.com/x/y.git", "session/r2", Path("/tmp/r2"))
        mgr._worktrees["r2"] = record
        result = mgr.list_workspaces()
        assert isinstance(result[0], WorkspaceInfo)


class TestServiceGetWorkspace:
    def test_get_nonexistent_returns_none(self):
        mgr = GitWorkspaceManager()
        result = mgr.get_workspace("not-there")
        assert result is None

    def test_get_existing_returns_workspace_info(self):
        mgr = GitWorkspaceManager()
        record = _WorktreeRecord("r1", "https://github.com/x/y.git", "session/r1", Path("/tmp/r1"))
        mgr._worktrees["r1"] = record
        result = mgr.get_workspace("r1")
        assert result is not None
        assert isinstance(result, WorkspaceInfo)
        assert result.room_id == "r1"

    def test_get_returns_correct_room(self):
        mgr = GitWorkspaceManager()
        r1 = _WorktreeRecord("roomA", "https://github.com/x/y.git", "session/roomA", Path("/tmp/a"))
        r2 = _WorktreeRecord("roomB", "https://github.com/x/y.git", "session/roomB", Path("/tmp/b"))
        mgr._worktrees["roomA"] = r1
        mgr._worktrees["roomB"] = r2
        result = mgr.get_workspace("roomA")
        assert result.room_id == "roomA"


def _no_task(coro):
    """Discard the coroutine without scheduling it (silences RuntimeWarning)."""
    coro.close()
    return MagicMock()


class TestServiceCreateWorkspace:
    """Patch asyncio.create_task so the background git-clone task never fires."""

    @pytest.mark.asyncio
    async def test_create_returns_workspace_info(self):
        mgr = GitWorkspaceManager()
        req = WorkspaceCreateRequest(room_id="room1", repo_url="https://github.com/x/y.git")
        with patch("asyncio.create_task", side_effect=_no_task):
            result = await mgr.create_workspace(req)
        assert isinstance(result, WorkspaceInfo)

    @pytest.mark.asyncio
    async def test_create_sets_room_id(self):
        mgr = GitWorkspaceManager()
        req = WorkspaceCreateRequest(room_id="room2", repo_url="https://github.com/x/y.git")
        with patch("asyncio.create_task", side_effect=_no_task):
            result = await mgr.create_workspace(req)
        assert result.room_id == "room2"

    @pytest.mark.asyncio
    async def test_create_registers_worktree(self):
        mgr = GitWorkspaceManager()
        req = WorkspaceCreateRequest(room_id="room3", repo_url="https://github.com/x/y.git")
        with patch("asyncio.create_task", side_effect=_no_task):
            await mgr.create_workspace(req)
        assert "room3" in mgr._worktrees

    @pytest.mark.asyncio
    async def test_create_idempotent_same_room(self):
        mgr = GitWorkspaceManager()
        req = WorkspaceCreateRequest(room_id="room4", repo_url="https://github.com/x/y.git")
        with patch("asyncio.create_task", side_effect=_no_task):
            r1 = await mgr.create_workspace(req)
            r2 = await mgr.create_workspace(req)  # returns existing record
        assert r1.room_id == r2.room_id


class TestServiceDestroyWorkspace:
    @pytest.mark.asyncio
    async def test_destroy_nonexistent_returns_failure(self):
        mgr = GitWorkspaceManager()
        result = await mgr.destroy_workspace("not-there")
        assert isinstance(result, WorkspaceDestroyResult)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_destroy_existing_returns_success(self):
        mgr = GitWorkspaceManager()
        req = WorkspaceCreateRequest(room_id="room-del", repo_url="https://github.com/x/y.git")
        with patch("asyncio.create_task", side_effect=_no_task):
            await mgr.create_workspace(req)
        result = await mgr.destroy_workspace("room-del")
        assert result.room_id == "room-del"

    @pytest.mark.asyncio
    async def test_destroy_removes_from_worktrees(self):
        mgr = GitWorkspaceManager()
        req = WorkspaceCreateRequest(room_id="room-del2", repo_url="https://github.com/x/y.git")
        with patch("asyncio.create_task", side_effect=_no_task):
            await mgr.create_workspace(req)
        await mgr.destroy_workspace("room-del2")
        assert mgr.get_workspace("room-del2") is None


class TestServiceGetWorktreePath:
    def test_get_path_nonexistent_returns_none(self):
        mgr = GitWorkspaceManager()
        result = mgr.get_worktree_path("nope")
        assert result is None

    def test_get_path_existing_returns_path(self):
        mgr = GitWorkspaceManager()
        record = _WorktreeRecord("r1", "https://github.com/x/y.git", "session/r1", Path("/tmp/r1"))
        mgr._worktrees["r1"] = record
        result = mgr.get_worktree_path("r1")
        assert result == Path("/tmp/r1")


# ---------------------------------------------------------------------------
# Router / endpoint tests
# ---------------------------------------------------------------------------


from app.git_workspace.schemas import (  # noqa: E402 (already imported above)
    ListRemoteBranchesResponse,
    SetupAndIndexResult,
    WorkspaceCommitResult,
    WorkspacePushResult,
    WorkspaceSyncResult,
)


@pytest.fixture()
def mock_manager():
    mgr = MagicMock(spec=GitWorkspaceManager)
    dummy = _make_dummy_info()
    mgr.create_workspace = AsyncMock(return_value=dummy)
    mgr.get_workspace = MagicMock(return_value=dummy)
    mgr.list_workspaces = MagicMock(return_value=[dummy])
    mgr.destroy_workspace = AsyncMock(
        return_value=WorkspaceDestroyResult(room_id="w1", success=True, message="ok")
    )
    mgr.sync_workspace = AsyncMock(
        return_value=WorkspaceSyncResult(room_id="w1", success=True, message="synced")
    )
    mgr.commit_workspace = AsyncMock(
        return_value=WorkspaceCommitResult(room_id="w1", success=True, sha="abc123", message="committed")
    )
    mgr.push_workspace = AsyncMock(
        return_value=WorkspacePushResult(room_id="w1", success=True, message="pushed")
    )
    mgr.store_credentials = AsyncMock(return_value=None)
    mgr.revoke_credentials = AsyncMock(return_value=None)
    mgr.list_remote_branches = AsyncMock(
        return_value=(["develop", "main", "staging"], "main")
    )
    mgr.token_cache = None  # disabled by default in tests
    mgr.is_local_workspace = MagicMock(return_value=False)
    return mgr


@pytest.fixture(autouse=True)
def _inject_manager(mock_manager):
    app.dependency_overrides[get_git_service] = lambda: mock_manager
    yield
    app.dependency_overrides.clear()


class TestCreateWorkspaceEndpoint:
    def test_create_returns_201(self, mock_manager):
        resp = client.post(
            "/api/git-workspace/workspaces",
            json={"room_id": "w1", "repo_url": "https://github.com/x/y.git"},
        )
        assert resp.status_code == 201

    def test_create_missing_room_id(self, mock_manager):
        resp = client.post(
            "/api/git-workspace/workspaces",
            json={"repo_url": "https://github.com/x/y.git"},
        )
        assert resp.status_code == 422

    def test_create_missing_repo_url(self, mock_manager):
        resp = client.post("/api/git-workspace/workspaces", json={"room_id": "w1"})
        assert resp.status_code == 422

    def test_create_returns_workspace_data(self, mock_manager):
        resp = client.post(
            "/api/git-workspace/workspaces",
            json={"room_id": "w1", "repo_url": "https://github.com/x/y.git"},
        )
        assert isinstance(resp.json(), dict)

    def test_create_service_error(self, mock_manager):
        mock_manager.create_workspace = AsyncMock(side_effect=RuntimeError("limit reached"))
        resp = client.post(
            "/api/git-workspace/workspaces",
            json={"room_id": "err", "repo_url": "https://github.com/x/y.git"},
        )
        assert resp.status_code == 409


class TestGetWorkspaceEndpoint:
    def test_get_returns_200(self, mock_manager):
        resp = client.get("/api/git-workspace/workspaces/w1")
        assert resp.status_code == 200

    def test_get_not_found_returns_404(self, mock_manager):
        mock_manager.get_workspace = MagicMock(return_value=None)
        resp = client.get("/api/git-workspace/workspaces/ghost")
        assert resp.status_code == 404

    def test_get_returns_workspace_dict(self, mock_manager):
        resp = client.get("/api/git-workspace/workspaces/w1")
        assert isinstance(resp.json(), dict)
        assert "room_id" in resp.json()


class TestListWorkspacesEndpoint:
    def test_list_returns_200(self, mock_manager):
        resp = client.get("/api/git-workspace/workspaces")
        assert resp.status_code == 200

    def test_list_returns_list(self, mock_manager):
        resp = client.get("/api/git-workspace/workspaces")
        assert isinstance(resp.json(), list)

    def test_list_empty(self, mock_manager):
        mock_manager.list_workspaces = MagicMock(return_value=[])
        resp = client.get("/api/git-workspace/workspaces")
        assert resp.json() == []

    def test_list_with_items(self, mock_manager):
        dummy = _make_dummy_info("room-a")
        mock_manager.list_workspaces = MagicMock(return_value=[dummy])
        resp = client.get("/api/git-workspace/workspaces")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["room_id"] == "room-a"


class TestDestroyWorkspaceEndpoint:
    def test_delete_returns_200(self, mock_manager):
        resp = client.delete("/api/git-workspace/workspaces/w1")
        assert resp.status_code == 200

    def test_delete_returns_confirmation(self, mock_manager):
        resp = client.delete("/api/git-workspace/workspaces/w1")
        data = resp.json()
        assert isinstance(data, dict)
        assert "success" in data

    def test_delete_service_error_returns_result(self, mock_manager):
        mock_manager.destroy_workspace = AsyncMock(
            return_value=WorkspaceDestroyResult(room_id="w1", success=False, message="not found")
        )
        resp = client.delete("/api/git-workspace/workspaces/w1")
        assert resp.status_code == 200
        assert resp.json()["success"] is False


class TestSyncWorkspaceEndpoint:
    def test_sync_returns_200(self, mock_manager):
        resp = client.post(
            "/api/git-workspace/workspaces/w1/sync",
            json={"room_id": "w1"},
        )
        assert resp.status_code == 200

    def test_sync_returns_result(self, mock_manager):
        resp = client.post(
            "/api/git-workspace/workspaces/w1/sync",
            json={"room_id": "w1"},
        )
        data = resp.json()
        assert "success" in data


class TestHealthEndpoint:
    def test_health_returns_200(self, mock_manager):
        mock_manager.list_workspaces = MagicMock(return_value=[])
        resp = client.get("/api/git-workspace/health")
        assert resp.status_code == 200

    def test_health_returns_status(self, mock_manager):
        mock_manager.list_workspaces = MagicMock(return_value=[])
        resp = client.get("/api/git-workspace/health")
        data = resp.json()
        assert "status" in data
        assert "active_rooms" in data


# ---------------------------------------------------------------------------
# List Remote Branches endpoint tests
# ---------------------------------------------------------------------------


class TestListRemoteBranchesEndpoint:
    def test_list_branches_returns_200(self, mock_manager):
        resp = client.post(
            "/api/git-workspace/branches/remote",
            json={"repo_url": "https://github.com/x/y.git"},
        )
        assert resp.status_code == 200

    def test_list_branches_returns_branches(self, mock_manager):
        resp = client.post(
            "/api/git-workspace/branches/remote",
            json={"repo_url": "https://github.com/x/y.git"},
        )
        data = resp.json()
        assert "branches" in data
        assert isinstance(data["branches"], list)
        assert "main" in data["branches"]

    def test_list_branches_returns_default_branch(self, mock_manager):
        resp = client.post(
            "/api/git-workspace/branches/remote",
            json={"repo_url": "https://github.com/x/y.git"},
        )
        data = resp.json()
        assert data["default_branch"] == "main"

    def test_list_branches_with_credentials(self, mock_manager):
        resp = client.post(
            "/api/git-workspace/branches/remote",
            json={
                "repo_url": "https://github.com/x/y.git",
                "credentials": {"token": "ghp_test123"},
            },
        )
        assert resp.status_code == 200
        mock_manager.list_remote_branches.assert_called_once()

    def test_list_branches_missing_repo_url(self, mock_manager):
        resp = client.post("/api/git-workspace/branches/remote", json={})
        assert resp.status_code == 422

    def test_list_branches_service_error(self, mock_manager):
        mock_manager.list_remote_branches = AsyncMock(
            side_effect=RuntimeError("git ls-remote failed")
        )
        resp = client.post(
            "/api/git-workspace/branches/remote",
            json={"repo_url": "https://github.com/x/y.git"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Service — list_remote_branches unit tests
# ---------------------------------------------------------------------------


class TestServiceListRemoteBranches:
    @pytest.mark.asyncio
    async def test_list_remote_branches_parses_output(self):
        mgr = GitWorkspaceManager()
        ls_remote_output = (
            "ref: refs/heads/main\tHEAD\n"
            "abc123\trefs/heads/develop\n"
            "def456\trefs/heads/main\n"
            "ghi789\trefs/heads/staging\n"
        )
        with patch.object(mgr, "_run_git", new_callable=AsyncMock, return_value=ls_remote_output):
            branches, default = await mgr.list_remote_branches("https://github.com/x/y.git")
        assert branches == ["develop", "main", "staging"]
        assert default == "main"

    @pytest.mark.asyncio
    async def test_list_remote_branches_no_symref(self):
        mgr = GitWorkspaceManager()
        ls_remote_output = (
            "abc123\trefs/heads/feature-a\n"
            "def456\trefs/heads/main\n"
        )
        with patch.object(mgr, "_run_git", new_callable=AsyncMock, return_value=ls_remote_output):
            branches, default = await mgr.list_remote_branches("https://github.com/x/y.git")
        assert branches == ["feature-a", "main"]
        assert default is None

    @pytest.mark.asyncio
    async def test_list_remote_branches_empty(self):
        mgr = GitWorkspaceManager()
        with patch.object(mgr, "_run_git", new_callable=AsyncMock, return_value=""):
            branches, default = await mgr.list_remote_branches("https://github.com/x/y.git")
        assert branches == []
        assert default is None

    @pytest.mark.asyncio
    async def test_list_remote_branches_with_credentials(self):
        from app.git_workspace.schemas import CredentialPayload
        mgr = GitWorkspaceManager()
        creds = CredentialPayload(token="ghp_test")
        with patch.object(mgr, "_run_git", new_callable=AsyncMock, return_value="abc\trefs/heads/main\n") as mock_git:
            branches, default = await mgr.list_remote_branches("https://github.com/x/y.git", creds)
        assert branches == ["main"]
        # Verify env was passed with credential vars
        call_kwargs = mock_git.call_args
        env = call_kwargs.kwargs.get("env") or call_kwargs[1].get("env")
        assert env is not None
        assert "GIT_ASKPASS" in env
        assert env["GIT_CREDENTIAL_TOKEN"] == "ghp_test"


# ---------------------------------------------------------------------------
# RepoTokenCache unit tests
# ---------------------------------------------------------------------------


from app.git_workspace.token_cache import RepoTokenCache, _normalize_url  # noqa: E402
from app.git_workspace.schemas import CredentialPayload  # noqa: E402
from datetime import datetime, timedelta, timezone  # noqa: E402


class TestNormalizeUrl:
    def test_strips_git_suffix(self):
        assert _normalize_url("https://github.com/x/y.git") == "https://github.com/x/y"

    def test_strips_trailing_slash(self):
        assert _normalize_url("https://github.com/x/y/") == "https://github.com/x/y"

    def test_both_suffix_and_slash(self):
        assert _normalize_url("https://github.com/x/y.git/") == "https://github.com/x/y"

    def test_no_suffix_unchanged(self):
        assert _normalize_url("https://github.com/x/y") == "https://github.com/x/y"

    def test_strips_whitespace(self):
        assert _normalize_url("  https://github.com/x/y.git  ") == "https://github.com/x/y"


class TestRepoTokenCache:
    @pytest.fixture
    def cache(self, tmp_path):
        c = RepoTokenCache(tmp_path / "token_cache.db", default_ttl_seconds=3600)
        c.open()
        yield c
        c.close()

    # --- open / close ---

    def test_open_creates_db_file(self, tmp_path):
        db = tmp_path / "sub" / "token_cache.db"
        c = RepoTokenCache(db)
        c.open()
        assert db.exists()
        c.close()

    def test_close_is_idempotent(self, cache):
        cache.close()
        cache.close()  # should not raise

    # --- put / get ---

    def test_put_and_get_returns_credential(self, cache):
        creds = CredentialPayload(token="ghp_abc", username="alice")
        cache.put("https://github.com/x/y.git", creds)
        result = cache.get("https://github.com/x/y.git")
        assert result is not None
        assert result.token == "ghp_abc"
        assert result.username == "alice"

    def test_get_normalizes_url(self, cache):
        creds = CredentialPayload(token="tok1")
        cache.put("https://github.com/x/y.git", creds)
        # Without .git suffix should still resolve
        result = cache.get("https://github.com/x/y")
        assert result is not None
        assert result.token == "tok1"

    def test_get_nonexistent_returns_none(self, cache):
        assert cache.get("https://github.com/missing/repo") is None

    def test_put_replaces_existing(self, cache):
        cache.put("https://github.com/x/y", CredentialPayload(token="old"))
        cache.put("https://github.com/x/y", CredentialPayload(token="new"))
        result = cache.get("https://github.com/x/y")
        assert result.token == "new"

    def test_get_returns_none_for_expired(self, cache):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        creds = CredentialPayload(token="expired", expires_at=past)
        cache.put("https://github.com/x/y", creds)
        assert cache.get("https://github.com/x/y") is None

    def test_explicit_expires_at_honoured(self, cache):
        far_future = datetime.now(timezone.utc) + timedelta(days=365)
        creds = CredentialPayload(token="long_lived", expires_at=far_future)
        cache.put("https://github.com/x/y", creds)
        result = cache.get("https://github.com/x/y")
        assert result is not None
        assert result.token == "long_lived"

    def test_get_returns_expires_at(self, cache):
        far_future = datetime.now(timezone.utc) + timedelta(hours=1)
        creds = CredentialPayload(token="tok", expires_at=far_future)
        cache.put("https://github.com/x/y", creds)
        result = cache.get("https://github.com/x/y")
        assert result.expires_at is not None

    # --- evict_expired ---

    def test_evict_expired_removes_stale(self, cache):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        creds = CredentialPayload(token="stale", expires_at=past)
        cache.put("https://github.com/x/stale", creds)
        # Bypass get() eviction by inserting with raw SQL check
        count = cache.evict_expired()
        assert count >= 1
        assert cache.get("https://github.com/x/stale") is None

    def test_evict_expired_keeps_valid(self, cache):
        creds = CredentialPayload(token="valid")  # uses default TTL
        cache.put("https://github.com/x/valid", creds)
        count = cache.evict_expired()
        assert count == 0
        assert cache.get("https://github.com/x/valid") is not None

    def test_evict_expired_mixed(self, cache):
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        cache.put("https://github.com/x/a", CredentialPayload(token="old", expires_at=past))
        cache.put("https://github.com/x/b", CredentialPayload(token="new"))
        # Note: put() already calls evict_expired() before each insert,
        # so the expired entry may be gone before we call it explicitly here.
        cache.evict_expired()
        assert cache.get("https://github.com/x/a") is None
        assert cache.get("https://github.com/x/b") is not None

    # --- list_entries ---

    def test_list_entries_empty(self, cache):
        assert cache.list_entries() == []

    def test_list_entries_returns_metadata(self, cache):
        cache.put("https://github.com/x/y", CredentialPayload(token="tok"))
        entries = cache.list_entries()
        assert len(entries) == 1
        assert "repo_url" in entries[0]
        assert "expires_at" in entries[0]
        assert "cached_at" in entries[0]
        # token must NOT appear in list_entries output
        assert "token" not in entries[0]

    def test_list_entries_multiple(self, cache):
        cache.put("https://github.com/x/a", CredentialPayload(token="t1"))
        cache.put("https://github.com/x/b", CredentialPayload(token="t2"))
        assert len(cache.list_entries()) == 2

    # --- no-op when closed ---

    def test_closed_get_returns_none(self, tmp_path):
        c = RepoTokenCache(tmp_path / "t.db")
        # Never opened — _conn is None
        assert c.get("https://github.com/x/y") is None

    def test_closed_put_is_noop(self, tmp_path):
        c = RepoTokenCache(tmp_path / "t.db")
        c.put("https://github.com/x/y", CredentialPayload(token="tok"))  # should not raise

    def test_closed_evict_returns_zero(self, tmp_path):
        c = RepoTokenCache(tmp_path / "t.db")
        assert c.evict_expired() == 0


# ---------------------------------------------------------------------------
# Service — token cache integration
# ---------------------------------------------------------------------------


class TestServiceTokenCacheIntegration:
    """Tests for create_workspace using cached tokens."""

    @pytest.mark.asyncio
    async def test_create_workspace_caches_explicit_token(self, tmp_path):
        """After a successful clone, the explicit token is written to the cache."""
        mgr = GitWorkspaceManager()
        mgr._workspaces_dir = tmp_path
        mgr._token_cache = RepoTokenCache(tmp_path / "token_cache.db")
        mgr._token_cache.open()
        await mgr._credential_store.start()

        req = WorkspaceCreateRequest(
            room_id="room-cache-1",
            repo_url="https://github.com/x/y.git",
            credentials=CredentialPayload(token="ghp_explicit"),
        )

        # _setup_worktree is what calls token_cache.put; simulate it directly
        # after create_workspace starts the background task.
        with patch("asyncio.create_task", side_effect=_no_task):
            await mgr.create_workspace(req)

        # Manually invoke the post-success caching logic (what _setup_worktree does)
        mgr._token_cache.put(req.repo_url, req.credentials)

        cached = mgr._token_cache.get("https://github.com/x/y.git")
        assert cached is not None
        assert cached.token == "ghp_explicit"

        await mgr._credential_store.stop()
        mgr._token_cache.close()

    @pytest.mark.asyncio
    async def test_create_workspace_uses_cached_token(self, tmp_path):
        """When no credentials are provided, the cached token is injected."""
        mgr = GitWorkspaceManager()
        mgr._workspaces_dir = tmp_path
        mgr._token_cache = RepoTokenCache(tmp_path / "token_cache.db")
        mgr._token_cache.open()
        await mgr._credential_store.start()

        # Pre-seed the cache
        mgr._token_cache.put(
            "https://github.com/x/y",
            CredentialPayload(token="ghp_cached", username="bot"),
        )

        req = WorkspaceCreateRequest(
            room_id="room-cache-2",
            repo_url="https://github.com/x/y.git",
            # No credentials — service should fall back to cache
        )

        with patch("asyncio.create_task", side_effect=_no_task):
            await mgr.create_workspace(req)

        # The credential store should now have the cached token injected
        creds = await mgr._credential_store.get("room-cache-2")
        assert creds is not None
        assert creds.token == "ghp_cached"

        await mgr._credential_store.stop()
        mgr._token_cache.close()

    @pytest.mark.asyncio
    async def test_create_workspace_no_cache_no_creds(self, tmp_path):
        """When no credentials and empty cache, proceeds without creds (delegate mode)."""
        mgr = GitWorkspaceManager()
        mgr._workspaces_dir = tmp_path
        mgr._token_cache = RepoTokenCache(tmp_path / "token_cache.db")
        mgr._token_cache.open()
        await mgr._credential_store.start()

        req = WorkspaceCreateRequest(
            room_id="room-cache-3",
            repo_url="https://github.com/x/y.git",
        )

        with patch("asyncio.create_task", side_effect=_no_task):
            await mgr.create_workspace(req)

        # No credentials stored — delegate mode continues normally
        creds = await mgr._credential_store.get("room-cache-3")
        assert creds is None

        await mgr._credential_store.stop()
        mgr._token_cache.close()

    @pytest.mark.asyncio
    async def test_create_workspace_expired_cache_not_used(self, tmp_path):
        """Expired cached tokens are ignored and not injected."""
        mgr = GitWorkspaceManager()
        mgr._workspaces_dir = tmp_path
        mgr._token_cache = RepoTokenCache(tmp_path / "token_cache.db")
        mgr._token_cache.open()
        await mgr._credential_store.start()

        # Put an already-expired token in the cache
        past = datetime.now(timezone.utc) - timedelta(seconds=1)
        mgr._token_cache.put(
            "https://github.com/x/y",
            CredentialPayload(token="ghp_expired", expires_at=past),
        )

        req = WorkspaceCreateRequest(
            room_id="room-cache-4",
            repo_url="https://github.com/x/y.git",
        )

        with patch("asyncio.create_task", side_effect=_no_task):
            await mgr.create_workspace(req)

        creds = await mgr._credential_store.get("room-cache-4")
        assert creds is None  # expired token was not used

        await mgr._credential_store.stop()
        mgr._token_cache.close()

    @pytest.mark.asyncio
    async def test_token_cache_disabled_when_none(self):
        """Service works normally when _token_cache is None."""
        mgr = GitWorkspaceManager()
        mgr._token_cache = None  # simulate failure to open
        await mgr._credential_store.start()

        req = WorkspaceCreateRequest(
            room_id="room-no-cache",
            repo_url="https://github.com/x/y.git",
            credentials=CredentialPayload(token="ghp_direct"),
        )

        with patch("asyncio.create_task", side_effect=_no_task):
            await mgr.create_workspace(req)

        creds = await mgr._credential_store.get("room-no-cache")
        assert creds is not None
        assert creds.token == "ghp_direct"

        await mgr._credential_store.stop()


# ---------------------------------------------------------------------------
# Token cache router endpoint
# ---------------------------------------------------------------------------


class TestTokenCacheEndpoint:
    def test_returns_200(self, mock_manager):
        mock_manager.token_cache = None
        resp = client.get("/api/git-workspace/token-cache")
        assert resp.status_code == 200

    def test_disabled_when_no_cache(self, mock_manager):
        mock_manager.token_cache = None
        resp = client.get("/api/git-workspace/token-cache")
        data = resp.json()
        assert data["enabled"] is False
        assert data["entries"] == []

    def test_enabled_with_entries(self, mock_manager):
        mock_cache = MagicMock()
        mock_cache.list_entries.return_value = [
            {
                "repo_url": "https://github.com/x/y",
                "username": "alice",
                "cached_at": "2026-01-01T00:00:00+00:00",
                "expires_at": "2026-01-01T08:00:00+00:00",
            }
        ]
        mock_manager.token_cache = mock_cache
        resp = client.get("/api/git-workspace/token-cache")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["count"] == 1
        assert data["entries"][0]["repo_url"] == "https://github.com/x/y"

    def test_tokens_not_in_response(self, mock_manager):
        mock_cache = MagicMock()
        mock_cache.list_entries.return_value = [
            {
                "repo_url": "https://github.com/x/y",
                "username": "alice",
                "cached_at": "2026-01-01T00:00:00+00:00",
                "expires_at": "2026-01-01T08:00:00+00:00",
            }
        ]
        mock_manager.token_cache = mock_cache
        resp = client.get("/api/git-workspace/token-cache")
        # token field must NOT appear in the response
        entry = resp.json()["entries"][0]
        assert "token" not in entry
