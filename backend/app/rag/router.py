"""RAG router — codebase retrieval endpoints.

Endpoints:
    POST /rag/index    — Incremental file indexing
    POST /rag/reindex  — Full workspace reindex
    POST /rag/search   — Semantic code search
"""
import logging
from typing import Optional

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .indexer import RagIndexer
from .schemas import (
    IndexRequest,
    IndexResponse,
    ReindexRequest,
    SearchRequest,
    SearchResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/rag", tags=["rag"])

# ---------------------------------------------------------------------------
# Singleton indexer management
# ---------------------------------------------------------------------------

_indexer: Optional[RagIndexer] = None


def get_indexer() -> Optional[RagIndexer]:
    """Return the global RagIndexer, or None if not configured."""
    return _indexer


def set_indexer(indexer: Optional[RagIndexer]) -> None:
    """Set (or clear) the global RagIndexer."""
    global _indexer
    _indexer = indexer


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post("/index", response_model=IndexResponse)
async def index_files(request: IndexRequest) -> IndexResponse | JSONResponse:
    """Incrementally index (upsert or delete) files for a workspace."""
    indexer = get_indexer()
    if indexer is None:
        return JSONResponse(
            {"error": "RAG indexer not configured"},
            status_code=503,
        )

    try:
        files = [f.model_dump() for f in request.files]
        added, removed = indexer.index_files(request.workspace_id, files)
        return IndexResponse(
            chunks_added=added,
            chunks_removed=removed,
            files_processed=len(request.files),
        )
    except Exception as exc:
        logger.exception("[rag/index] Indexing failed: %s", exc)
        return JSONResponse(
            {"error": f"Indexing failed: {exc}"},
            status_code=500,
        )


@router.post("/reindex", response_model=IndexResponse)
async def reindex_workspace(request: ReindexRequest) -> IndexResponse | JSONResponse:
    """Clear and rebuild the index for a workspace."""
    indexer = get_indexer()
    if indexer is None:
        return JSONResponse(
            {"error": "RAG indexer not configured"},
            status_code=503,
        )

    try:
        files = [f.model_dump() for f in request.files]
        added, removed = indexer.reindex(request.workspace_id, files)
        return IndexResponse(
            chunks_added=added,
            chunks_removed=removed,
            files_processed=len(request.files),
        )
    except Exception as exc:
        logger.exception("[rag/reindex] Reindex failed: %s", exc)
        return JSONResponse(
            {"error": f"Reindex failed: {exc}"},
            status_code=500,
        )


@router.post("/search", response_model=SearchResponse)
async def search_code(request: SearchRequest) -> SearchResponse | JSONResponse:
    """Search the indexed codebase for chunks relevant to a query."""
    indexer = get_indexer()
    if indexer is None:
        return JSONResponse(
            {"error": "RAG indexer not configured"},
            status_code=503,
        )

    try:
        results = indexer.search(
            workspace_id=request.workspace_id,
            query=request.query,
            top_k=request.top_k,
            filters=request.filters,
        )
        return SearchResponse(
            results=results,
            query=request.query,
            workspace_id=request.workspace_id,
        )
    except Exception as exc:
        logger.exception("[rag/search] Search failed: %s", exc)
        return JSONResponse(
            {"error": f"Search failed: {exc}"},
            status_code=500,
        )
