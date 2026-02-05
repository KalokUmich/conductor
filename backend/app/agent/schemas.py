"""Pydantic schemas for the code generation agent.

These schemas define the structure of ChangeSets - the core data structure
for representing code modifications in Conductor. A ChangeSet contains
one or more FileChanges, each describing either a new file creation or
a modification to an existing file.

Schema Compatibility:
    These schemas match the JSON schema defined in shared/changeset.schema.json
    and are used by both the backend API and the VS Code extension.

Example ChangeSet:
    {
        "changes": [
            {
                "id": "uuid-here",
                "file": "src/helper.py",
                "type": "create_file",
                "content": "# New helper module\\n..."
            },
            {
                "id": "uuid-here",
                "file": "src/main.py",
                "type": "replace_range",
                "range": {"start": 1, "end": 3},
                "content": "import helper\\n..."
            }
        ],
        "summary": "Added helper module and import statement"
    }
"""
import uuid
from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, model_validator


# =============================================================================
# Enums and Basic Types
# =============================================================================


class ChangeType(str, Enum):
    """Type of file change operation.

    Attributes:
        REPLACE_RANGE: Replace a range of lines in an existing file.
        CREATE_FILE: Create a new file with the specified content.
    """
    REPLACE_RANGE = "replace_range"
    CREATE_FILE = "create_file"


class Range(BaseModel):
    """A range of lines in a file (1-based, inclusive).

    Both start and end are inclusive. For example, Range(start=1, end=3)
    refers to lines 1, 2, and 3.

    Attributes:
        start: Starting line number (1-based, inclusive).
        end: Ending line number (1-based, inclusive).
    """
    start: int = Field(..., ge=1, description="Starting line number (1-based)")
    end: int = Field(..., ge=1, description="Ending line number (1-based)")


# =============================================================================
# Core Models
# =============================================================================


class FileChange(BaseModel):
    """A single file change operation.

    This represents one atomic change to a file. The required fields depend
    on the change type:

    - replace_range: Requires 'range' and 'content'
    - create_file: Requires 'content' only

    Attributes:
        id: Unique UUID for tracking this change through the review flow.
        file: Relative path to the file being changed.
        type: Type of change (replace_range or create_file).
        range: Line range to replace (required for replace_range).
        content: New content to insert.
        original_content: Original content being replaced (for reference/undo).
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique identifier (UUID)"
    )
    file: str = Field(
        ..., min_length=1,
        description="Relative path to the file"
    )
    type: ChangeType = Field(..., description="Type of change operation")
    range: Optional[Range] = Field(
        default=None,
        description="Line range to replace (required for replace_range)"
    )
    content: Optional[str] = Field(
        default=None,
        description="New content to insert"
    )
    original_content: Optional[str] = Field(
        default=None,
        description="Original content being replaced (for reference)"
    )

    @model_validator(mode='after')
    def validate_change_type_requirements(self) -> "FileChange":
        """Validate required fields based on change type.

        Raises:
            ValueError: If required fields are missing for the change type.
        """
        if self.type == ChangeType.REPLACE_RANGE:
            if self.range is None:
                raise ValueError("'range' is required for replace_range type")
            if self.content is None:
                raise ValueError("'content' is required for replace_range type")
        elif self.type == ChangeType.CREATE_FILE:
            if self.content is None:
                raise ValueError("'content' is required for create_file type")
        return self


class ChangeSet(BaseModel):
    """A collection of file changes to apply atomically.

    A ChangeSet represents a logical unit of work that should be reviewed
    and applied together. It contains one or more FileChanges and a
    human-readable summary.

    Attributes:
        changes: List of file changes (1-10 files).
        summary: Brief description of what the changes accomplish.
    """
    changes: List[FileChange] = Field(
        ...,
        min_length=1,
        max_length=10,
        description="List of file changes (1-10 files)"
    )
    summary: str = Field(
        default="",
        description="Brief summary of the changes"
    )


# =============================================================================
# API Request/Response Models
# =============================================================================


class GenerateChangesRequest(BaseModel):
    """Request body for the POST /generate-changes endpoint.

    Attributes:
        file_path: Target file to modify (optional for new files only).
        instruction: Natural language description of desired changes.
        file_content: Current content of the file (for context).
    """
    file_path: Optional[str] = Field(
        default=None,
        description="Path to modify (optional for new files only)"
    )
    instruction: str = Field(
        ...,
        description="Natural language instruction"
    )
    file_content: Optional[str] = Field(
        default=None,
        description="Current file content (for context)"
    )


class GenerateChangesResponse(BaseModel):
    """Response from the POST /generate-changes endpoint.

    Attributes:
        success: Whether generation succeeded.
        change_set: The generated ChangeSet.
        message: Additional details about the generation.
    """
    success: bool = Field(
        default=True,
        description="Whether generation succeeded"
    )
    change_set: ChangeSet = Field(
        ...,
        description="The generated ChangeSet"
    )
    message: str = Field(
        default="",
        description="Additional details or error message"
    )

