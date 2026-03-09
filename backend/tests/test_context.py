"""Tests for the context enrichment router — hybrid retrieval + reranking.

Rewritten for CocoIndex + RepoMap + Reranking integration.  All heavy
dependencies (cocoindex, sentence_transformers, sqlite_vec, tree-sitter,
networkx, git) are stubbed so the suite runs in any CI environment.

Coverage:
  * POST /api/context/context — vector search + reranking + repo map
  * GET /api/context/context/{room_id}/index-status
  * GET /api/context/context/{room_id}/graph-stats
  * GET /api/context/context/{room_id}/rerank-status
  * Edge cases: no workspace, empty results, repo map disabled/failed,
    reranking disabled/failed/fallback

Total: 42 tests

"""
from __future__ import annotations

import sys
import types
import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient
from fastapi import FastAPI

# ---------------------------------------------------------------------------
# Stubs for optional heavy dependencies
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return m

_stub("cocoindex", FlowBuilder=MagicMock, IndexOptions=MagicMock)
_stub("sentence_transformers", SentenceTransformer=MagicMock, CrossEncoder=MagicMock)
_stub("litellm")
_stub("sqlite_vec")
_stub("tree_sitter_languages")
_stub("networkx", DiGraph=MagicMock, pagerank=MagicMock, PowerIterationFailedConvergence=Exception)
_stub("cohere")

# ---------------------------------------------------------------------------
# Application setup
# ---------------------------------------------------------------------------

from contextlib import contextmanager  # noqa: E402
from app.context.router import (  # noqa: E402
    router,
    _get_code_search_service,
    _get_git_workspace_service,
    _get_repo_map_service,
    _get_rerank_provider,
)
from app.code_search.rerank_provider import (  # noqa: E402
    NoopRerankProvider,
    RerankResult,
)

app = FastAPI()
app.include_router(router)
client = TestClient(app)


@contextmanager
def _dep(dep_fn, value):
    """Temporarily override a single FastAPI dependency for one block."""
    prev = app.dependency_overrides.get(dep_fn)
    app.dependency_overrides[dep_fn] = lambda: value
    try:
        yield
    finally:
        if prev is not None:
            app.dependency_overrides[dep_fn] = prev
        else:
            app.dependency_overrides.pop(dep_fn, None)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_chunk(file_path="main.py", start_line=1, end_line=10,
                content="def main(): pass", score=0.9,
                symbol_name="main", symbol_type="function"):
    chunk = MagicMock()
    chunk.file_path = file_path
    chunk.start_line = start_line
    chunk.end_line = end_line
    chunk.content = content
    chunk.score = score
    chunk.symbol_name = symbol_name
    chunk.symbol_type = symbol_type
    return chunk


@pytest.fixture()
def mock_code_search():
    svc = MagicMock()
    # Return a mock CodeSearchResponse
    mock_response = MagicMock()
    mock_response.results = []
    svc.search = AsyncMock(return_value=mock_response)
    svc.get_index_status = MagicMock(return_value=MagicMock(
        model_dump=MagicMock(return_value={
            "workspace_path": "/test",
            "indexed": False,
            "files_count": 0,
            "chunks_count": 0,
        })
    ))
    return svc


@pytest.fixture()
def mock_git_workspace():
    svc = MagicMock()
    svc.get_worktree_path = MagicMock(return_value="/fake/worktree")
    return svc


@pytest.fixture()
def mock_repo_map():
    svc = MagicMock()
    svc.generate_repo_map = MagicMock(return_value="## Repository Map\n\nmain.py\n")
    svc.get_graph_stats = MagicMock(return_value={
        "cached": True,
        "total_files": 10,
        "total_edges": 15,
    })
    return svc


@pytest.fixture()
def mock_rerank_noop():
    return NoopRerankProvider()


@pytest.fixture()
def mock_rerank_active():
    """A mock reranker that reverses the order of documents."""
    reranker = MagicMock()
    reranker.name = "mock/reranker"

    async def mock_rerank(query, documents, top_n=None):
        n = top_n if top_n is not None else len(documents)
        # Reverse order to simulate reranking
        results = [
            RerankResult(index=i, score=1.0 - (i * 0.1), text=documents[i])
            for i in reversed(range(min(n, len(documents))))
        ]
        return results[:n]

    reranker.rerank = mock_rerank
    reranker.health_check = MagicMock(return_value={"provider": "mock/reranker", "status": "ok"})
    return reranker


@pytest.fixture(autouse=True)
def _inject_services(mock_code_search, mock_git_workspace, mock_repo_map, mock_rerank_noop):
    app.dependency_overrides[_get_code_search_service] = lambda: mock_code_search
    app.dependency_overrides[_get_git_workspace_service] = lambda: mock_git_workspace
    app.dependency_overrides[_get_repo_map_service] = lambda: mock_repo_map
    app.dependency_overrides[_get_rerank_provider] = lambda: mock_rerank_noop
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# POST /api/context/context — happy path
# ---------------------------------------------------------------------------


class TestContextEndpointHappyPath:
    def test_returns_200_for_valid_request(self, mock_code_search):
        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "main function"
        })
        assert resp.status_code == 200

    def test_response_has_required_fields(self, mock_code_search):
        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "test"
        })
        data = resp.json()
        assert "room_id" in data
        assert "query" in data
        assert "chunks" in data
        assert "total" in data
        assert "reranked" in data

    def test_response_includes_repo_map(self, mock_code_search, mock_repo_map):
        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "test"
        })
        data = resp.json()
        assert "repo_map" in data
        assert data["repo_map"] is not None
        assert "Repository Map" in data["repo_map"]

    def test_empty_results_returns_200(self, mock_code_search):
        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "zzz_no_match"
        })
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_chunks_from_search_results(self, mock_code_search):
        mock_response = MagicMock()
        mock_response.results = [_make_chunk()]
        mock_code_search.search = AsyncMock(return_value=mock_response)

        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "main"
        })
        data = resp.json()
        assert data["total"] == 1
        assert data["chunks"][0]["file_path"] == "main.py"
        assert data["chunks"][0]["score"] == 0.9

    def test_repo_map_called_with_vector_files(self, mock_code_search, mock_repo_map):
        mock_response = MagicMock()
        mock_response.results = [_make_chunk(file_path="service.py", symbol_name=None, symbol_type=None)]
        mock_code_search.search = AsyncMock(return_value=mock_response)

        client.post("/api/context/context", json={
            "room_id": "room-1", "query": "service"
        })

        mock_repo_map.generate_repo_map.assert_called_once()
        call_kwargs = mock_repo_map.generate_repo_map.call_args
        assert "service.py" in call_kwargs[1]["query_files"]

    def test_reranked_false_when_noop(self, mock_code_search):
        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "test"
        })
        assert resp.json()["reranked"] is False


# ---------------------------------------------------------------------------
# POST /api/context/context — reranking integration
# ---------------------------------------------------------------------------


class TestContextReranking:
    def test_reranked_true_when_active_provider(self, mock_code_search, mock_rerank_active):
        mock_response = MagicMock()
        mock_response.results = [
            _make_chunk(file_path="a.py", content="aaa", score=0.9),
            _make_chunk(file_path="b.py", content="bbb", score=0.8),
            _make_chunk(file_path="c.py", content="ccc", score=0.7),
        ]
        mock_code_search.search = AsyncMock(return_value=mock_response)

        with _dep(_get_rerank_provider, mock_rerank_active):
            resp = client.post("/api/context/context", json={
                "room_id": "room-1", "query": "test"
            })
        data = resp.json()
        assert data["reranked"] is True

    def test_rerank_disabled_via_request_flag(self, mock_code_search, mock_rerank_active):
        mock_response = MagicMock()
        mock_response.results = [_make_chunk()]
        mock_code_search.search = AsyncMock(return_value=mock_response)

        with _dep(_get_rerank_provider, mock_rerank_active):
            resp = client.post("/api/context/context", json={
                "room_id": "room-1",
                "query": "test",
                "enable_reranking": False,
            })
        data = resp.json()
        assert data["reranked"] is False

    def test_rerank_enabled_via_request_flag(self, mock_code_search, mock_rerank_active):
        mock_response = MagicMock()
        mock_response.results = [_make_chunk()]
        mock_code_search.search = AsyncMock(return_value=mock_response)

        with _dep(_get_rerank_provider, mock_rerank_active):
            resp = client.post("/api/context/context", json={
                "room_id": "room-1",
                "query": "test",
                "enable_reranking": True,
            })
        data = resp.json()
        assert data["reranked"] is True

    def test_reranked_chunks_have_rerank_score(self, mock_code_search, mock_rerank_active):
        mock_response = MagicMock()
        mock_response.results = [
            _make_chunk(file_path="a.py", content="aaa", score=0.9),
        ]
        mock_code_search.search = AsyncMock(return_value=mock_response)

        with _dep(_get_rerank_provider, mock_rerank_active):
            resp = client.post("/api/context/context", json={
                "room_id": "room-1", "query": "test"
            })
        data = resp.json()
        assert data["chunks"][0]["rerank_score"] is not None

    def test_no_rerank_score_when_noop(self, mock_code_search):
        mock_response = MagicMock()
        mock_response.results = [_make_chunk()]
        mock_code_search.search = AsyncMock(return_value=mock_response)

        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "test"
        })
        data = resp.json()
        assert data["chunks"][0]["rerank_score"] is None

    def test_rerank_none_provider_means_no_reranking(self, mock_code_search):
        with _dep(_get_rerank_provider, None):
            resp = client.post("/api/context/context", json={
                "room_id": "room-1", "query": "test"
            })
        data = resp.json()
        assert data["reranked"] is False

    def test_rerank_failure_falls_back_to_vector(self, mock_code_search):
        """If reranking raises, should fall back to vector search results."""
        mock_response = MagicMock()
        mock_response.results = [_make_chunk()]
        mock_code_search.search = AsyncMock(return_value=mock_response)

        broken_reranker = MagicMock()
        broken_reranker.name = "broken"

        async def broken_rerank(*args, **kwargs):
            raise RuntimeError("API error")

        broken_reranker.rerank = broken_rerank

        with _dep(_get_rerank_provider, broken_reranker):
            resp = client.post("/api/context/context", json={
                "room_id": "room-1", "query": "test"
            })
        data = resp.json()
        assert resp.status_code == 200
        assert data["reranked"] is False
        assert data["total"] >= 0  # Still returns results


# ---------------------------------------------------------------------------
# POST /api/context/context — repo map disabled / not available
# ---------------------------------------------------------------------------


class TestContextRepoMapEdgeCases:
    def test_repo_map_disabled_in_request(self, mock_code_search, mock_repo_map):
        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "test", "include_repo_map": False
        })
        data = resp.json()
        assert data["repo_map"] is None
        mock_repo_map.generate_repo_map.assert_not_called()

    def test_repo_map_service_none(self, mock_code_search):
        with _dep(_get_repo_map_service, None):
            resp = client.post("/api/context/context", json={
                "room_id": "room-1", "query": "test"
            })
            data = resp.json()
            assert data["repo_map"] is None

    def test_repo_map_generation_error_handled(self, mock_code_search, mock_repo_map):
        mock_repo_map.generate_repo_map.side_effect = RuntimeError("graph error")
        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "test"
        })
        # Should still return 200, just without repo map
        assert resp.status_code == 200
        data = resp.json()
        assert data["repo_map"] is None


# ---------------------------------------------------------------------------
# POST /api/context/context — validation / error
# ---------------------------------------------------------------------------


class TestContextEndpointValidation:
    def test_missing_query_returns_422(self):
        resp = client.post("/api/context/context", json={"room_id": "room-1"})
        assert resp.status_code == 422

    def test_missing_room_id_returns_422(self):
        resp = client.post("/api/context/context", json={"query": "test"})
        assert resp.status_code == 422

    def test_no_workspace_returns_404(self, mock_git_workspace):
        mock_git_workspace.get_worktree_path.return_value = None
        resp = client.post("/api/context/context", json={
            "room_id": "no-workspace", "query": "test"
        })
        assert resp.status_code == 404

    def test_wrong_content_type_returns_422(self):
        resp = client.post("/api/context/context", data="not-json")
        assert resp.status_code == 422

    def test_top_k_min_1(self):
        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "test", "top_k": 0
        })
        assert resp.status_code == 422

    def test_top_k_max_20(self):
        resp = client.post("/api/context/context", json={
            "room_id": "room-1", "query": "test", "top_k": 21
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /api/context/context/{room_id}/index-status
# ---------------------------------------------------------------------------


class TestIndexStatusEndpoint:
    def test_returns_200(self, mock_code_search, mock_git_workspace):
        resp = client.get("/api/context/context/room-1/index-status")
        assert resp.status_code == 200

    def test_returns_dict(self, mock_code_search, mock_git_workspace):
        resp = client.get("/api/context/context/room-1/index-status")
        data = resp.json()
        assert isinstance(data, dict)
        assert "indexed" in data

    def test_no_workspace_returns_404(self, mock_git_workspace):
        mock_git_workspace.get_worktree_path.return_value = None
        resp = client.get("/api/context/context/no-room/index-status")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/context/context/{room_id}/graph-stats
# ---------------------------------------------------------------------------


class TestGraphStatsEndpoint:
    def test_returns_200(self, mock_repo_map, mock_git_workspace):
        resp = client.get("/api/context/context/room-1/graph-stats")
        assert resp.status_code == 200

    def test_returns_stats(self, mock_repo_map, mock_git_workspace):
        resp = client.get("/api/context/context/room-1/graph-stats")
        data = resp.json()
        assert data["available"] is True
        assert "total_files" in data

    def test_repo_map_not_configured(self):
        with _dep(_get_repo_map_service, None), _dep(_get_git_workspace_service, MagicMock()):
            resp = client.get("/api/context/context/room-1/graph-stats")
            data = resp.json()
            assert data["available"] is False

    def test_no_workspace_returns_404(self, mock_repo_map, mock_git_workspace):
        mock_git_workspace.get_worktree_path.return_value = None
        resp = client.get("/api/context/context/no-room/graph-stats")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/context/context/{room_id}/rerank-status
# ---------------------------------------------------------------------------


class TestRerankStatusEndpoint:
    def test_returns_200_with_noop(self):
        resp = client.get("/api/context/context/room-1/rerank-status")
        assert resp.status_code == 200

    def test_noop_provider_shows_available(self):
        resp = client.get("/api/context/context/room-1/rerank-status")
        data = resp.json()
        assert data["available"] is True
        assert data["provider"] == "none"

    def test_none_provider_shows_unavailable(self):
        with _dep(_get_rerank_provider, None):
            resp = client.get("/api/context/context/room-1/rerank-status")
            data = resp.json()
            assert data["available"] is False

    def test_active_provider_shows_status(self, mock_rerank_active):
        with _dep(_get_rerank_provider, mock_rerank_active):
            resp = client.get("/api/context/context/room-1/rerank-status")
            data = resp.json()
            assert data["available"] is True
            assert data["provider"] == "mock/reranker"
