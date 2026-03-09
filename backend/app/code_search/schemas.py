from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Search request / response
# ---------------------------------------------------------------------------


class CodeSearchRequest(BaseModel):
    """Query body for a semantic code search."""

    query:             str  = Field(..., description="Natural-language or symbol search query.")
    workspace_path:    str  = Field(..., description="Absolute path to the workspace root to search.")
    top_k:             int  = Field(default=5, ge=1, le=50, description="Max results to return.")
    file_filter:       Optional[str] = Field(
        default=None,
        description="Optional glob pattern to restrict which files are indexed/searched.",
    )


class CodeChunk(BaseModel):
    """A single code chunk returned from a search."""

    file_path:    str
    start_line:   int
    end_line:     int
    content:      str
    score:        float          = Field(..., description="Cosine similarity score (0–1).")
    language:     Optional[str] = None
    symbol_name:  Optional[str] = None   # function/class name if AST detected it
    symbol_type:  Optional[str] = None   # "function" | "class" | "method" | ...


class CodeSearchResponse(BaseModel):
    """Response containing ranked code chunks."""

    query:    str
    results:  List[CodeChunk]
    total:    int
    index_id: Optional[str] = None   # CocoIndex index identifier


# ---------------------------------------------------------------------------
# Index management
# ---------------------------------------------------------------------------


class IndexBuildRequest(BaseModel):
    """Request to (re-)build the code index for a workspace."""

    workspace_path: str  = Field(..., description="Root directory to index.")
    force_rebuild:  bool = Field(
        default=False,
        description="If True, drop and rebuild the index from scratch.",
    )
    file_filter: Optional[str] = Field(
        default=None,
        description="Optional glob pattern, e.g. '**/*.py'.",
    )


class IndexBuildResult(BaseModel):
    workspace_path: str
    success:        bool
    files_indexed:  int
    chunks_indexed: int
    duration_ms:    float
    message:        str


class IndexStatusResponse(BaseModel):
    workspace_path:  str
    indexed:         bool
    files_count:     int
    chunks_count:    int
    last_updated:    Optional[str] = None   # ISO-8601
    index_id:        Optional[str] = None
    storage_backend: Optional[str] = None   # "sqlite" | "postgres"
    is_incremental:  bool          = False


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------


class CodeSearchHealth(BaseModel):
    status:            str   # "ok" | "degraded" | "error"
    embedding_model:   str   # LiteLLM model string
    storage_backend:   str   # "sqlite" | "postgres"
    index_dir:         str
    incremental:       bool  = False
    detail:            Optional[str] = None
