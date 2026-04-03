"""Tests for the workspace_files router (conductor:// FS backend)."""

from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.workspace_files.router import _get_git_service

ROOM = "test-room-123"


@pytest.fixture
def tmp_worktree(tmp_path):
    """Create a fake worktree directory with sample files."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hello')")
    (tmp_path / "src" / "util.py").write_text("def add(a,b): return a+b")
    (tmp_path / "README.md").write_text("# Project")
    (tmp_path / ".git").mkdir()  # should be hidden from listings
    (tmp_path / ".vscode").mkdir()  # should be hidden from listings
    (tmp_path / ".vscode" / "settings.json").write_text("{}")
    (tmp_path / "node_modules").mkdir()  # should be hidden from listings
    return tmp_path


@pytest.fixture
def client(tmp_worktree):
    """TestClient with a mocked GitWorkspaceService."""
    mock_svc = MagicMock()
    mock_svc.get_worktree_path.return_value = tmp_worktree
    mock_svc.is_local_workspace.return_value = False
    app.dependency_overrides[_get_git_service] = lambda: mock_svc
    yield TestClient(app)
    app.dependency_overrides.pop(_get_git_service, None)


@pytest.fixture
def client_no_workspace():
    """TestClient where workspace doesn't exist."""
    mock_svc = MagicMock()
    mock_svc.get_worktree_path.return_value = None
    mock_svc.is_local_workspace.return_value = False
    app.dependency_overrides[_get_git_service] = lambda: mock_svc
    yield TestClient(app)
    app.dependency_overrides.pop(_get_git_service, None)


# ---- stat ----


class TestStat:
    def test_stat_file(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/src/main.py/stat")
        assert resp.status_code == 200
        data = resp.json()
        assert data["type"] == "file"
        assert data["size"] > 0

    def test_stat_directory(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/src/stat")
        assert resp.status_code == 200
        assert resp.json()["type"] == "directory"

    def test_stat_root(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/stat")
        assert resp.status_code == 200
        assert resp.json()["type"] == "directory"

    def test_stat_not_found(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/nonexistent.txt/stat")
        assert resp.status_code == 404

    def test_stat_no_workspace(self, client_no_workspace):
        resp = client_no_workspace.get(f"/workspace/{ROOM}/files/foo/stat")
        assert resp.status_code == 404


# ---- readFile ----


class TestReadFile:
    def test_read_content(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/src/main.py/content")
        assert resp.status_code == 200
        assert b"print('hello')" in resp.content

    def test_read_not_found(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/nope.txt/content")
        assert resp.status_code == 404

    def test_read_directory_returns_404(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/src/content")
        assert resp.status_code == 404


# ---- readDirectory ----


class TestReadDirectory:
    def test_list_root(self, client):
        resp = client.get(f"/workspace/{ROOM}/files")
        assert resp.status_code == 200
        names = [e["name"] for e in resp.json()]
        assert "README.md" in names
        assert "src" in names
        # Hidden directories should not appear
        assert ".git" not in names
        assert ".vscode" not in names
        assert "node_modules" not in names

    def test_list_subdir(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/src")
        assert resp.status_code == 200
        names = {e["name"] for e in resp.json()}
        assert names == {"main.py", "util.py"}

    def test_list_not_found(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/nonexistent")
        assert resp.status_code == 404

    def test_list_file_returns_400(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/README.md")
        assert resp.status_code == 400


# ---- writeFile ----


class TestWriteFile:
    def test_write_new_file(self, client, tmp_worktree):
        resp = client.put(
            f"/workspace/{ROOM}/files/new_file.txt/content",
            content=b"new content",
        )
        assert resp.status_code == 200
        assert (tmp_worktree / "new_file.txt").read_text() == "new content"

    def test_write_creates_parents(self, client, tmp_worktree):
        resp = client.put(
            f"/workspace/{ROOM}/files/deep/nested/file.txt/content",
            content=b"deep",
        )
        assert resp.status_code == 200
        assert (tmp_worktree / "deep" / "nested" / "file.txt").read_text() == "deep"

    def test_overwrite_existing(self, client, tmp_worktree):
        resp = client.put(
            f"/workspace/{ROOM}/files/README.md/content",
            content=b"# Updated",
        )
        assert resp.status_code == 200
        assert (tmp_worktree / "README.md").read_text() == "# Updated"


# ---- rename ----


class TestRename:
    def test_rename_file(self, client, tmp_worktree):
        resp = client.post(
            f"/workspace/{ROOM}/files/README.md/rename",
            json={"new_path": "CHANGELOG.md"},
        )
        assert resp.status_code == 200
        assert not (tmp_worktree / "README.md").exists()
        assert (tmp_worktree / "CHANGELOG.md").exists()

    def test_rename_conflict(self, client):
        resp = client.post(
            f"/workspace/{ROOM}/files/README.md/rename",
            json={"new_path": "src/main.py", "overwrite": False},
        )
        assert resp.status_code == 409

    def test_rename_overwrite(self, client, tmp_worktree):
        resp = client.post(
            f"/workspace/{ROOM}/files/README.md/rename",
            json={"new_path": "src/main.py", "overwrite": True},
        )
        assert resp.status_code == 200
        assert (tmp_worktree / "src" / "main.py").read_text() == "# Project"


# ---- createDirectory ----


class TestCreateDirectory:
    def test_mkdir(self, client, tmp_worktree):
        resp = client.post(
            f"/workspace/{ROOM}/files/newdir",
            json={"type": "directory"},
        )
        assert resp.status_code == 200
        assert (tmp_worktree / "newdir").is_dir()

    def test_mkdir_conflict(self, client):
        resp = client.post(
            f"/workspace/{ROOM}/files/src",
            json={"type": "directory"},
        )
        assert resp.status_code == 409


# ---- delete ----


class TestDelete:
    def test_delete_file(self, client, tmp_worktree):
        resp = client.delete(f"/workspace/{ROOM}/files/README.md")
        assert resp.status_code == 200
        assert not (tmp_worktree / "README.md").exists()

    def test_delete_dir_recursive(self, client, tmp_worktree):
        resp = client.delete(f"/workspace/{ROOM}/files/src?recursive=true")
        assert resp.status_code == 200
        assert not (tmp_worktree / "src").exists()

    def test_delete_nonempty_dir_fails(self, client):
        resp = client.delete(f"/workspace/{ROOM}/files/src")
        assert resp.status_code == 400

    def test_delete_not_found(self, client):
        resp = client.delete(f"/workspace/{ROOM}/files/nope.txt")
        assert resp.status_code == 404


# ---- path traversal ----


class TestPathTraversal:
    def test_traversal_blocked(self, client):
        # Starlette normalizes ../ in the URL path, so the request either
        # gets a 403 (guard triggers) or 404 (path normalized away / not found).
        # Both are safe — the important thing is we never get 200.
        resp = client.get(f"/workspace/{ROOM}/files/../../etc/passwd/stat")
        assert resp.status_code in (403, 404)


# ---- blocked roots (.vscode, .git, etc.) ----


class TestBlockedRoots:
    def test_stat_vscode_settings_blocked(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/.vscode/settings.json/stat")
        assert resp.status_code == 404

    def test_read_vscode_settings_blocked(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/.vscode/settings.json/content")
        assert resp.status_code == 404

    def test_stat_git_blocked(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/.git/stat")
        assert resp.status_code == 404

    def test_stat_node_modules_blocked(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/node_modules/stat")
        assert resp.status_code == 404

    def test_write_vscode_blocked(self, client):
        resp = client.put(
            f"/workspace/{ROOM}/files/.vscode/settings.json/content",
            content=b"{}",
        )
        assert resp.status_code == 404

    def test_delete_vscode_blocked(self, client):
        resp = client.delete(f"/workspace/{ROOM}/files/.vscode/settings.json")
        assert resp.status_code == 404

    def test_list_vscode_blocked(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/.vscode")
        assert resp.status_code == 404

    def test_idea_blocked(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/.idea/stat")
        assert resp.status_code == 404

    def test_devcontainer_blocked(self, client):
        resp = client.get(f"/workspace/{ROOM}/files/.devcontainer/stat")
        assert resp.status_code == 404


# ---- search ----


class TestSearch:
    def test_search_finds_match(self, client):
        resp = client.post(
            f"/workspace/{ROOM}/search",
            json={"pattern": "hello"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["matches"]) >= 1
        paths = [m["path"] for m in data["matches"]]
        assert any("main.py" in p for p in paths)

    def test_search_no_match(self, client):
        resp = client.post(
            f"/workspace/{ROOM}/search",
            json={"pattern": "zzz_nonexistent_zzz"},
        )
        assert resp.status_code == 200
        assert len(resp.json()["matches"]) == 0

    def test_search_with_glob_filter(self, client):
        resp = client.post(
            f"/workspace/{ROOM}/search",
            json={"pattern": "add", "glob": "*.py"},
        )
        assert resp.status_code == 200
        data = resp.json()
        matches = data["matches"]
        assert len(matches) >= 1
        assert all(m["path"].endswith(".py") for m in matches)

    def test_search_workspace_not_found(self, client_no_workspace):
        resp = client_no_workspace.post(
            f"/workspace/{ROOM}/search",
            json={"pattern": "hello"},
        )
        assert resp.status_code == 404

    def test_search_respects_blocked_roots(self, client):
        resp = client.post(
            f"/workspace/{ROOM}/search",
            json={"pattern": "settings"},
        )
        assert resp.status_code == 200
        paths = [m["path"] for m in resp.json()["matches"]]
        assert not any(p.startswith(".vscode/") for p in paths)
        assert not any(p.startswith(".git/") for p in paths)
