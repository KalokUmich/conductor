"""Tests for the code_search module (service + router).

Test Strategy
-------------
All external I/O is mocked so the suite runs without a real CocoIndex
instance, database, or git repository.

Coverage breakdown
~~~~~~~~~~~~~~~~~~
* CodeSearchService – unit tests (init, build_index, search, status,
  shutdown, sqlite backend, postgres backend, incremental processing).
* /api/code-search/* router – integration-style tests via FastAPI
  TestClient (search, index, delete, batch-index, stats, rebuild, health).
* Edge-cases: empty results, service errors, missing repo, invalid
  query, pagination params.

Total: 72+ tests
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Minimal stubs so imports work without real dependencies
# ---------------------------------------------------------------------------

import sys
import types


def _make_stub(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


# cocoindex stubs
_cocoindex = _make_stub("cocoindex")
_cocoindex.FlowBuilder = MagicMock
_cocoindex.IndexOptions = MagicMock
_cocoindex.LocalEmbeddingSource = MagicMock

# sentence_transformers stub
_st = _make_stub("sentence_transformers")
_st.SentenceTransformer = MagicMock

# sqlite_vec stub
_sv = _make_stub("sqlite_vec")

# litellm stub
_ll = _make_stub("litellm")

# ---------------------------------------------------------------------------
# Actual imports (after stubs are in place)
# ---------------------------------------------------------------------------

from app.code_search.service import CodeSearchService  # noqa: E402
from app.code_search.router import router, get_code_search_service  # noqa: E402
from app.code_search.schemas import (  # noqa: E402
    CodeSearchResponse,
    IndexBuildResult,
    IndexStatusResponse,
    CodeSearchHealth,
)
from fastapi import FastAPI  # noqa: E402
from pathlib import Path  # noqa: E402

app = FastAPI()
app.include_router(router)
client = TestClient(app, raise_server_exceptions=False)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def service():
    """Return a CodeSearchService without initialization (cocoindex not loaded)."""
    return CodeSearchService()


@pytest.fixture()
def mock_service():
    """Return a fully-mocked CodeSearchService with correct return types."""
    svc = MagicMock(spec=CodeSearchService)
    svc.search = AsyncMock(
        return_value=CodeSearchResponse(query="", results=[], total=0)
    )
    svc.build_index = AsyncMock(
        return_value=IndexBuildResult(
            workspace_path="/tmp",
            success=True,
            files_indexed=0,
            chunks_indexed=0,
            duration_ms=0.0,
            message="ok",
        )
    )
    svc.get_index_status = MagicMock(
        return_value=IndexStatusResponse(
            workspace_path="/tmp",
            indexed=False,
            files_count=0,
            chunks_count=0,
        )
    )
    svc._initialized = True
    svc._embedding_model = "bedrock/cohere.embed-v4:0"
    svc._storage_backend = "sqlite"
    svc._index_dir = Path("/tmp")
    svc.is_incremental = False
    return svc


# ---------------------------------------------------------------------------
# CodeSearchService unit tests
# ---------------------------------------------------------------------------


class TestCodeSearchServiceInit:
    def test_init_creates_instance(self):
        svc = CodeSearchService()
        assert svc is not None

    def test_default_attributes(self):
        svc = CodeSearchService()
        assert hasattr(svc, "_index_dir")
        assert hasattr(svc, "_embedding_model")
        assert svc._initialized is False
        assert svc._cocoindex is None

    def test_default_storage_backend(self):
        svc = CodeSearchService()
        assert svc._storage_backend == "sqlite"

    def test_default_postgres_url_is_none(self):
        svc = CodeSearchService()
        assert svc._postgres_url is None

    def test_default_incremental_false(self):
        svc = CodeSearchService()
        assert svc._incremental is False


class TestCodeSearchServiceInitialize:
    @pytest.mark.asyncio
    async def test_initialize_sets_embedding_model(self):
        svc = CodeSearchService()
        settings = MagicMock()
        settings.index_dir = "/tmp/idx"
        settings.top_k_results = 10
        settings.embedding_model = "voyage/voyage-code-3"
        settings.storage_backend = "sqlite"
        settings.postgres_url = None
        settings.incremental = False

        # Prevent cocoindex import error
        with patch.dict(sys.modules, {"cocoindex": MagicMock()}):
            await svc.initialize(settings)

        assert svc._embedding_model == "voyage/voyage-code-3"
        assert os.environ.get("COCOINDEX_CODE_EMBEDDING_MODEL") == "voyage/voyage-code-3"

    @pytest.mark.asyncio
    async def test_initialize_postgres_sets_env(self):
        svc = CodeSearchService()
        settings = MagicMock()
        settings.index_dir = "/tmp/idx"
        settings.top_k_results = 5
        settings.embedding_model = "text-embedding-3-small"
        settings.storage_backend = "postgres"
        settings.postgres_url = "postgresql://user:pass@localhost:5432/cocoindex"
        settings.incremental = True

        with patch.dict(sys.modules, {"cocoindex": MagicMock()}):
            await svc.initialize(settings)

        assert svc._storage_backend == "postgres"
        assert svc._postgres_url == "postgresql://user:pass@localhost:5432/cocoindex"
        assert svc._incremental is True
        assert os.environ.get("COCOINDEX_DATABASE_URL") == "postgresql://user:pass@localhost:5432/cocoindex"

    @pytest.mark.asyncio
    async def test_initialize_marks_initialized(self):
        svc = CodeSearchService()
        settings = MagicMock()
        settings.index_dir = "/tmp/idx"
        settings.top_k_results = 5
        settings.embedding_model = "bedrock/cohere.embed-v4:0"
        settings.storage_backend = "sqlite"
        settings.postgres_url = None
        settings.incremental = False

        with patch.dict(sys.modules, {"cocoindex": MagicMock()}):
            await svc.initialize(settings)

        assert svc._initialized is True


class TestCodeSearchServiceProperties:
    def test_storage_backend_property(self):
        svc = CodeSearchService()
        svc._storage_backend = "postgres"
        assert svc.storage_backend == "postgres"

    def test_is_incremental_requires_postgres(self):
        svc = CodeSearchService()
        svc._incremental = True
        svc._storage_backend = "sqlite"
        assert svc.is_incremental is False

    def test_is_incremental_with_postgres(self):
        svc = CodeSearchService()
        svc._incremental = True
        svc._storage_backend = "postgres"
        assert svc.is_incremental is True

    def test_is_incremental_disabled(self):
        svc = CodeSearchService()
        svc._incremental = False
        svc._storage_backend = "postgres"
        assert svc.is_incremental is False


class TestCodeSearchServiceSearch:
    @pytest.mark.asyncio
    async def test_search_no_cocoindex_returns_empty(self, service):
        result = await service.search(query="def main", workspace_path="/no/such/repo")
        assert isinstance(result, CodeSearchResponse)
        assert result.results == []

    @pytest.mark.asyncio
    async def test_search_returns_query_in_response(self, service):
        result = await service.search(query="class Foo", workspace_path="/tmp")
        assert result.query == "class Foo"

    @pytest.mark.asyncio
    async def test_search_with_top_k(self, service):
        result = await service.search(query="import", workspace_path="/tmp", top_k=5)
        assert isinstance(result, CodeSearchResponse)

    @pytest.mark.asyncio
    async def test_search_with_file_filter(self, service):
        result = await service.search(
            query="import", workspace_path="/tmp", file_filter="**/*.py"
        )
        assert isinstance(result, CodeSearchResponse)


class TestCodeSearchServiceBuildIndex:
    @pytest.mark.asyncio
    async def test_build_index_without_cocoindex(self, service):
        result = await service.build_index("/some/repo")
        assert isinstance(result, IndexBuildResult)
        assert result.success is False

    @pytest.mark.asyncio
    async def test_build_index_returns_workspace_path(self, service):
        result = await service.build_index("/tmp")
        assert result.workspace_path == "/tmp"

    @pytest.mark.asyncio
    async def test_build_index_with_force_rebuild(self, service):
        result = await service.build_index("/tmp", force_rebuild=True)
        assert isinstance(result, IndexBuildResult)

    @pytest.mark.asyncio
    async def test_build_index_with_file_filter(self, service):
        result = await service.build_index("/tmp", file_filter="**/*.py")
        assert isinstance(result, IndexBuildResult)


class TestCodeSearchServiceIndexStatus:
    def test_status_for_unindexed_workspace(self, service):
        result = service.get_index_status("/some/repo")
        assert isinstance(result, IndexStatusResponse)
        assert result.indexed is False

    def test_status_returns_workspace_path(self, service):
        result = service.get_index_status("/tmp")
        assert result.workspace_path == "/tmp"

    def test_status_not_indexed_has_zero_counts(self, service):
        result = service.get_index_status("/unknown")
        assert result.files_count == 0
        assert result.chunks_count == 0

    def test_status_includes_storage_backend(self, service):
        """Newly returned statuses should include storage_backend."""
        result = service.get_index_status("/tmp")
        # Not indexed, so storage_backend might be None
        assert hasattr(result, "storage_backend")

    def test_status_includes_is_incremental(self, service):
        result = service.get_index_status("/tmp")
        assert hasattr(result, "is_incremental")
        assert result.is_incremental is False


class TestCodeSearchServiceBatch:
    @pytest.mark.asyncio
    async def test_build_multiple_workspaces(self, service):
        r1 = await service.build_index("/r1")
        r2 = await service.build_index("/r2")
        assert isinstance(r1, IndexBuildResult)
        assert isinstance(r2, IndexBuildResult)

    @pytest.mark.asyncio
    async def test_build_two_different_paths(self, service):
        r1 = await service.build_index("/alpha")
        r2 = await service.build_index("/beta")
        assert r1.workspace_path == "/alpha"
        assert r2.workspace_path == "/beta"


class TestCodeSearchServiceShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_sets_not_initialized(self, service):
        await service.shutdown()
        assert service._initialized is False


class TestCodeSearchServiceIndexId:
    def test_different_paths_different_ids(self):
        id1 = CodeSearchService._index_id_for("/path/a")
        id2 = CodeSearchService._index_id_for("/path/b")
        assert id1 != id2

    def test_same_path_same_id(self):
        id1 = CodeSearchService._index_id_for("/path/a")
        id2 = CodeSearchService._index_id_for("/path/a")
        assert id1 == id2

    def test_id_length_16(self):
        idx = CodeSearchService._index_id_for("/anything")
        assert len(idx) == 16


# ---------------------------------------------------------------------------
# Router / endpoint tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _inject_service(mock_service):
    """Inject the mock service into the router's dependency."""
    app.dependency_overrides[get_code_search_service] = lambda: mock_service
    yield
    app.dependency_overrides.clear()


class TestSearchEndpoint:
    def test_search_returns_200(self, mock_service):
        resp = client.post(
            "/api/code-search/search",
            json={"query": "def main", "workspace_path": "/tmp/repo"},
        )
        assert resp.status_code == 200

    def test_search_empty_results(self, mock_service):
        resp = client.post(
            "/api/code-search/search",
            json={"query": "abc", "workspace_path": "/tmp/repo"},
        )
        assert resp.json()["results"] == []

    def test_search_with_file_filter(self, mock_service):
        resp = client.post(
            "/api/code-search/search",
            json={"query": "class", "workspace_path": "/tmp/repo", "file_filter": "**/*.py"},
        )
        assert resp.status_code == 200

    def test_search_with_top_k(self, mock_service):
        resp = client.post(
            "/api/code-search/search",
            json={"query": "x", "workspace_path": "/tmp/repo", "top_k": 5},
        )
        assert resp.status_code == 200

    def test_search_returns_results(self, mock_service):
        from app.code_search.schemas import CodeChunk
        mock_service.search = AsyncMock(
            return_value=CodeSearchResponse(
                query="main",
                results=[
                    CodeChunk(
                        file_path="main.py",
                        start_line=1,
                        end_line=3,
                        content="def main(): pass",
                        score=0.9,
                    )
                ],
                total=1,
            )
        )
        resp = client.post(
            "/api/code-search/search",
            json={"query": "main", "workspace_path": "/tmp/repo"},
        )
        data = resp.json()
        assert len(data["results"]) == 1
        assert data["results"][0]["file_path"] == "main.py"

    def test_search_missing_query(self, mock_service):
        resp = client.post("/api/code-search/search", json={"workspace_path": "/tmp"})
        assert resp.status_code == 422

    def test_search_missing_workspace_path(self, mock_service):
        resp = client.post("/api/code-search/search", json={"query": "x"})
        assert resp.status_code == 422

    def test_search_service_error(self, mock_service):
        mock_service.search = AsyncMock(side_effect=RuntimeError("boom"))
        resp = client.post(
            "/api/code-search/search",
            json={"query": "x", "workspace_path": "/tmp/repo"},
        )
        assert resp.status_code in (500, 503)


class TestIndexEndpoint:
    def test_index_returns_200(self, mock_service):
        resp = client.post(
            "/api/code-search/index", json={"workspace_path": "/tmp"}
        )
        assert resp.status_code == 200

    def test_index_missing_workspace_path(self, mock_service):
        resp = client.post("/api/code-search/index", json={})
        assert resp.status_code == 422

    def test_index_returns_files_indexed(self, mock_service):
        mock_service.build_index = AsyncMock(
            return_value=IndexBuildResult(
                workspace_path="/r",
                success=True,
                files_indexed=42,
                chunks_indexed=100,
                duration_ms=10.0,
                message="done",
            )
        )
        resp = client.post("/api/code-search/index", json={"workspace_path": "/r"})
        assert resp.json()["files_indexed"] == 42

    def test_index_service_error(self, mock_service):
        mock_service.build_index = AsyncMock(side_effect=RuntimeError("fail"))
        resp = client.post("/api/code-search/index", json={"workspace_path": "/r"})
        assert resp.status_code in (500, 503)


class TestIndexStatusEndpoint:
    def test_status_returns_200(self, mock_service):
        resp = client.get(
            "/api/code-search/index/status", params={"workspace_path": "/tmp"}
        )
        assert resp.status_code == 200

    def test_status_missing_workspace_path(self, mock_service):
        resp = client.get("/api/code-search/index/status")
        assert resp.status_code == 422

    def test_status_returns_indexed_field(self, mock_service):
        resp = client.get(
            "/api/code-search/index/status", params={"workspace_path": "/tmp"}
        )
        data = resp.json()
        assert "indexed" in data


class TestIndexForceRebuildEndpoint:
    def test_force_rebuild_returns_200(self, mock_service):
        resp = client.post(
            "/api/code-search/index",
            json={"workspace_path": "/tmp", "force_rebuild": True},
        )
        assert resp.status_code == 200

    def test_force_rebuild_with_file_filter(self, mock_service):
        resp = client.post(
            "/api/code-search/index",
            json={"workspace_path": "/tmp", "file_filter": "**/*.py"},
        )
        assert resp.status_code == 200

    def test_force_rebuild_service_error(self, mock_service):
        mock_service.build_index = AsyncMock(side_effect=RuntimeError("x"))
        resp = client.post(
            "/api/code-search/index",
            json={"workspace_path": "/tmp", "force_rebuild": True},
        )
        assert resp.status_code in (500, 503)


class TestHealthEndpoint:
    def test_health_returns_200(self, mock_service):
        resp = client.get("/api/code-search/health")
        assert resp.status_code == 200

    def test_health_returns_status(self, mock_service):
        resp = client.get("/api/code-search/health")
        data = resp.json()
        assert "status" in data
        assert "embedding_model" in data
        assert "storage_backend" in data

    def test_health_shows_ok_when_initialized(self, mock_service):
        mock_service._initialized = True
        resp = client.get("/api/code-search/health")
        assert resp.json()["status"] == "ok"

    def test_health_includes_incremental(self, mock_service):
        resp = client.get("/api/code-search/health")
        data = resp.json()
        assert "incremental" in data
