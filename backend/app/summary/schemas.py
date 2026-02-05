"""Pydantic schemas for chat summary extraction.

This module defines the request/response models for the summary endpoint.
The summary endpoint extracts structured information from chat history
to help teams track goals, decisions, and open questions.

Note:
    Field names use camelCase (e.g., userId, roomId) to match the
    TypeScript/JavaScript convention used in the extension frontend.
"""
from typing import List

from pydantic import BaseModel, Field


class ChatMessage(BaseModel):
    """A single chat message from the session history.

    Attributes:
        userId: Unique identifier of the message sender.
        content: The message text content.
        ts: Unix timestamp (seconds since epoch) when message was sent.
    """
    userId: str = Field(..., description="Sender's user ID")
    content: str = Field(..., description="Message text")
    ts: float = Field(..., description="Unix timestamp (seconds)")


class SummaryRequest(BaseModel):
    """Request body for the POST /summary endpoint.

    Contains the chat history to analyze for summary extraction.

    Attributes:
        roomId: The collaboration session identifier.
        messages: List of chat messages to analyze.
    """
    roomId: str = Field(..., description="Session identifier")
    messages: List[ChatMessage] = Field(..., description="Chat history")


class SummaryResponse(BaseModel):
    """Structured summary extracted from chat messages.

    Contains categorized information extracted from the conversation
    using keyword-based pattern matching.

    Attributes:
        goal: The main objective or goal of the session.
        constraints: Limitations or restrictions mentioned.
        decisions: Decisions that were made during the chat.
        open_questions: Questions that remain unresolved.
        non_goals: Items explicitly marked as out of scope.
    """
    goal: str = Field(
        default="",
        description="Main objective"
    )
    constraints: List[str] = Field(
        default_factory=list,
        description="Limitations mentioned"
    )
    decisions: List[str] = Field(
        default_factory=list,
        description="Decisions made"
    )
    open_questions: List[str] = Field(
        default_factory=list,
        description="Unresolved questions"
    )
    non_goals: List[str] = Field(
        default_factory=list,
        description="Out of scope items"
    )

