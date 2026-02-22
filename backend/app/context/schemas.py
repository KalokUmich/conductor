"""Pydantic schemas for the context enrichment / code explanation API."""
from typing import List, Optional

from pydantic import BaseModel, Field


class RelatedFileSnippet(BaseModel):
    """A short excerpt from a file related to the target snippet."""
    relative_path: str
    snippet: str
    reason: str = "related"  # definition | reference | import | filename_match


class ExplainRequest(BaseModel):
    """Request body for POST /context/explain."""
    room_id: str = Field(..., description="Room ID (for logging / context)")
    snippet: str = Field(..., description="The selected code to explain")
    file_path: str = Field(..., description="Workspace-relative path of the file")
    line_start: int = Field(..., ge=1, description="1-based start line")
    line_end: int = Field(..., ge=1, description="1-based end line")
    language: str = Field(default="", description="VS Code language ID")
    workspace_id: Optional[str] = Field(
        default=None,
        description="Workspace ID for RAG search (defaults to room_id if absent)"
    )

    # Optional enrichment provided by the extension's ContextGatherer
    file_content: Optional[str] = Field(
        default=None,
        description="Full (or truncated) source file content"
    )
    surrounding_code: Optional[str] = Field(
        default=None,
        description="Lines immediately around the selection with line numbers"
    )
    imports: List[str] = Field(
        default_factory=list,
        description="Import statements extracted from the file"
    )
    containing_function: Optional[str] = Field(
        default=None,
        description="Signature of the enclosing function or class"
    )
    related_files: List[RelatedFileSnippet] = Field(
        default_factory=list,
        description="Relevant snippets from related workspace files"
    )


class ExplainRichRequest(BaseModel):
    """Pre-assembled prompt from the extension's 8-stage pipeline.

    Used by POST /context/explain-rich. The extension performs LSP resolution,
    ranked file gathering, semantic search, and XML assembly before sending
    the complete prompt here for direct LLM forwarding.
    """
    assembled_prompt: str = Field(..., description="Complete XML prompt ready for LLM")
    snippet: str = Field(..., description="Original selected code (for logging)")
    file_path: str = Field(..., description="Workspace-relative path (for logging)")
    line_start: int = Field(..., ge=1)
    line_end: int = Field(..., ge=1)
    language: str = Field(default="")


class ExplainResponse(BaseModel):
    """Response for POST /context/explain."""
    explanation: str
    model: str
    language: str
    file_path: str
    line_start: int
    line_end: int
