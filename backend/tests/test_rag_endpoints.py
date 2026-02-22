"""Tests for the RAG API endpoints: /rag/index, /rag/reindex, /rag/search.

The RagIndexer is mocked so no FAISS/embedding dependency is needed.
"""
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.rag.router import get_indexer, set_indexer
from app.rag.schemas import SearchResultItem


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def client() -> Generator[TestClient, None, None]:
    yield TestClient(app)


@pytest.fixture(autouse=True)
def reset_indexer():
    """Ensure a clean indexer singleton for each test."""
    original = get_indexer()
    yield
    set_indexer(original)


@pytest.fixture()
def mock_indexer() -> MagicMock:
    """Install a mock RagIndexer as the global singleton."""
    indexer = MagicMock()
    indexer.index_files.return_value = (5, 2)
    indexer.reindex.return_value = (10, 3)
    indexer.search.return_value = [
        SearchResultItem(
            file_path="src/main.py",
            start_line=1,
            end_line=10,
            symbol_name="main",
            symbol_type="function",
            content="def main(): ...",
            score=0.95,
            language="python",
        )
    ]
    set_indexer(indexer)
    return indexer


# ---------------------------------------------------------------------------
# POST /rag/index
# ---------------------------------------------------------------------------

class TestRagIndex:
    def test_returns_503_when_indexer_not_configured(self, client: TestClient):
        set_indexer(None)
        resp = client.post("/rag/index", json={
            "workspace_id": "ws1",
            "files": [{"path": "a.py", "content": "x = 1", "action": "upsert"}],
        })
        assert resp.status_code == 503

    def test_successful_index(self, client: TestClient, mock_indexer: MagicMock):
        resp = client.post("/rag/index", json={
            "workspace_id": "ws1",
            "files": [
                {"path": "a.py", "content": "def foo(): pass", "action": "upsert"},
                {"path": "b.py", "content": None, "action": "delete"},
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["chunks_added"] == 5
        assert data["chunks_removed"] == 2
        assert data["files_processed"] == 2
        mock_indexer.index_files.assert_called_once()

    def test_rejects_empty_files(self, client: TestClient, mock_indexer: MagicMock):
        resp = client.post("/rag/index", json={
            "workspace_id": "ws1",
            "files": [],
        })
        assert resp.status_code == 422

    def test_returns_500_on_indexer_error(self, client: TestClient, mock_indexer: MagicMock):
        mock_indexer.index_files.side_effect = RuntimeError("disk full")
        resp = client.post("/rag/index", json={
            "workspace_id": "ws1",
            "files": [{"path": "a.py", "content": "x=1", "action": "upsert"}],
        })
        assert resp.status_code == 500
        assert "disk full" in resp.json()["error"]


# ---------------------------------------------------------------------------
# POST /rag/reindex
# ---------------------------------------------------------------------------

class TestRagReindex:
    def test_returns_503_when_indexer_not_configured(self, client: TestClient):
        set_indexer(None)
        resp = client.post("/rag/reindex", json={
            "workspace_id": "ws1",
            "files": [{"path": "a.py", "content": "x = 1", "action": "upsert"}],
        })
        assert resp.status_code == 503

    def test_successful_reindex(self, client: TestClient, mock_indexer: MagicMock):
        resp = client.post("/rag/reindex", json={
            "workspace_id": "ws1",
            "files": [
                {"path": "a.py", "content": "def foo(): pass", "action": "upsert"},
            ],
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["chunks_added"] == 10
        assert data["chunks_removed"] == 3
        assert data["files_processed"] == 1
        mock_indexer.reindex.assert_called_once()


# ---------------------------------------------------------------------------
# POST /rag/search
# ---------------------------------------------------------------------------

class TestRagSearch:
    def test_returns_503_when_indexer_not_configured(self, client: TestClient):
        set_indexer(None)
        resp = client.post("/rag/search", json={
            "workspace_id": "ws1",
            "query": "how does auth work",
        })
        assert resp.status_code == 503

    def test_successful_search(self, client: TestClient, mock_indexer: MagicMock):
        resp = client.post("/rag/search", json={
            "workspace_id": "ws1",
            "query": "authentication handler",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["query"] == "authentication handler"
        assert data["workspace_id"] == "ws1"
        assert len(data["results"]) == 1
        assert data["results"][0]["file_path"] == "src/main.py"
        assert data["results"][0]["score"] == 0.95

    def test_search_with_filters(self, client: TestClient, mock_indexer: MagicMock):
        resp = client.post("/rag/search", json={
            "workspace_id": "ws1",
            "query": "auth",
            "top_k": 5,
            "filters": {
                "languages": ["python"],
                "file_patterns": ["src/*.py"],
            },
        })
        assert resp.status_code == 200
        mock_indexer.search.assert_called_once()
        call_kwargs = mock_indexer.search.call_args
        assert call_kwargs.kwargs.get("top_k") == 5 or call_kwargs[1].get("top_k") == 5

    def test_search_rejects_empty_query(self, client: TestClient, mock_indexer: MagicMock):
        resp = client.post("/rag/search", json={
            "workspace_id": "ws1",
            "query": "",
        })
        assert resp.status_code == 422

    def test_search_top_k_bounds(self, client: TestClient, mock_indexer: MagicMock):
        # top_k=0 should be rejected
        resp = client.post("/rag/search", json={
            "workspace_id": "ws1",
            "query": "test",
            "top_k": 0,
        })
        assert resp.status_code == 422

        # top_k=51 should be rejected
        resp = client.post("/rag/search", json={
            "workspace_id": "ws1",
            "query": "test",
            "top_k": 51,
        })
        assert resp.status_code == 422

    def test_returns_500_on_search_error(self, client: TestClient, mock_indexer: MagicMock):
        mock_indexer.search.side_effect = RuntimeError("index corrupted")
        resp = client.post("/rag/search", json={
            "workspace_id": "ws1",
            "query": "test",
        })
        assert resp.status_code == 500
        assert "index corrupted" in resp.json()["error"]
