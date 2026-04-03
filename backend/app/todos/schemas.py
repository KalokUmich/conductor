"""Pydantic schemas for the TODO tracking module."""

import uuid
from datetime import datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

TodoStatus = Literal["open", "in_progress", "done"]
TodoPriority = Literal["high", "medium", "low"]
TodoSource = Literal["ai_summary", "manual", "stack_trace", "test_failure"]


class TodoCreate(BaseModel):
    """Request body for creating a new TODO."""

    title: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    type: str = Field(default="task")  # code_change, api_design, debugging …
    priority: TodoPriority = Field(default="medium")
    file_path: Optional[str] = Field(default=None)
    line_number: Optional[int] = Field(default=None, ge=1)
    created_by: str = Field(default="")
    assignee: Optional[str] = Field(default=None)
    source: TodoSource = Field(default="manual")
    source_id: Optional[str] = Field(default=None)


class TodoUpdate(BaseModel):
    """Request body for updating a TODO (all fields optional)."""

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    priority: Optional[TodoPriority] = None
    status: Optional[TodoStatus] = None
    file_path: Optional[str] = None
    line_number: Optional[int] = Field(default=None, ge=1)
    assignee: Optional[str] = None


class Todo(BaseModel):
    """Full TODO record returned by the API."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    room_id: str
    title: str
    description: Optional[str] = None
    type: str = "task"
    priority: TodoPriority = "medium"
    status: TodoStatus = "open"
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    created_by: str = ""
    assignee: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    source: TodoSource = "manual"
    source_id: Optional[str] = None
