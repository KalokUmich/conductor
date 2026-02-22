"""Pydantic schemas for the RAG (codebase retrieval) API."""
from typing import List, Optional

from pydantic import BaseModel, Field


class FileChange(BaseModel):
    """A single file to index or delete."""

    path: str = Field(..., description="Workspace-relative file path")
    content: Optional[str] = Field(
        default=None,
        description="File content (required for upsert, omit for delete)",
    )
    action: str = Field(
        default="upsert",
        description="'upsert' to add/update, 'delete' to remove",
    )


class IndexRequest(BaseModel):
    """Request body for POST /rag/index (incremental indexing)."""

    workspace_id: str = Field(..., description="Unique workspace identifier")
    files: List[FileChange] = Field(
        ..., min_length=1, description="Files to index or delete"
    )


class IndexResponse(BaseModel):
    """Response for indexing operations."""

    chunks_added: int
    chunks_removed: int
    files_processed: int


class ReindexRequest(BaseModel):
    """Request body for POST /rag/reindex (full rebuild)."""

    workspace_id: str = Field(..., description="Unique workspace identifier")
    files: List[FileChange] = Field(
        ..., min_length=1, description="All workspace files to index"
    )


class SearchFilters(BaseModel):
    """Optional filters for search queries."""

    languages: Optional[List[str]] = Field(
        default=None, description="Filter by language IDs (e.g. ['python', 'typescript'])"
    )
    file_patterns: Optional[List[str]] = Field(
        default=None, description="Filter by glob patterns (e.g. ['src/**/*.py'])"
    )


class SearchRequest(BaseModel):
    """Request body for POST /rag/search."""

    workspace_id: str = Field(..., description="Unique workspace identifier")
    query: str = Field(..., min_length=1, description="Natural language or code query")
    top_k: int = Field(default=10, ge=1, le=50, description="Max results to return")
    filters: Optional[SearchFilters] = Field(
        default=None, description="Optional search filters"
    )


class SearchResultItem(BaseModel):
    """A single search result."""

    file_path: str
    start_line: int
    end_line: int
    symbol_name: str = ""
    symbol_type: str = ""
    content: str
    score: float
    language: str = ""


class SearchResponse(BaseModel):
    """Response for POST /rag/search."""

    results: List[SearchResultItem]
    query: str
    workspace_id: str
