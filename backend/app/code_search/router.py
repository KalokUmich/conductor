"""FastAPI router for CocoIndex Code Search.

All endpoints live under /api/code-search (registered in main.py).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from .schemas import (
    CodeSearchHealth,
    CodeSearchRequest,
    CodeSearchResponse,
    IndexBuildRequest,
    IndexBuildResult,
    IndexStatusResponse,
)
from .service import CodeSearchService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/code-search", tags=["code-search"])


def get_code_search_service() -> CodeSearchService:  # pragma: no cover
    from app.main import app
    return app.state.code_search_service


@router.get("/health", response_model=CodeSearchHealth)
async def health(
    svc: CodeSearchService = Depends(get_code_search_service),
) -> CodeSearchHealth:
    """Basic health check for the code search module."""
    return CodeSearchHealth(
        status          = "ok" if svc._initialized else "error",
        embedding_model = svc._embedding_model,
        storage_backend = svc._storage_backend,
        index_dir       = str(svc._index_dir),
        incremental     = svc.is_incremental,
        detail          = None if svc._initialized else "Service not initialized",
    )


@router.post("/search", response_model=CodeSearchResponse)
async def search(
    req: CodeSearchRequest,
    svc: CodeSearchService = Depends(get_code_search_service),
) -> CodeSearchResponse:
    """Semantic code search over an indexed workspace."""
    return await svc.search(
        query          = req.query,
        workspace_path = req.workspace_path,
        top_k          = req.top_k,
        file_filter    = req.file_filter,
    )


@router.post("/index", response_model=IndexBuildResult)
async def build_index(
    req: IndexBuildRequest,
    svc: CodeSearchService = Depends(get_code_search_service),
) -> IndexBuildResult:
    """Build or update the code index for a workspace."""
    return await svc.build_index(
        workspace_path = req.workspace_path,
        force_rebuild  = req.force_rebuild,
        file_filter    = req.file_filter,
    )


@router.get("/index/status", response_model=IndexStatusResponse)
async def index_status(
    workspace_path: str,
    svc: CodeSearchService = Depends(get_code_search_service),
) -> IndexStatusResponse:
    """Get the current index status for a workspace."""
    return svc.get_index_status(workspace_path)
