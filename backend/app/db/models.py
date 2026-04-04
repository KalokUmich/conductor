"""SQLAlchemy ORM models for all Conductor OLTP data.

Tables:
  * repo_tokens — PAT cache for git workspace
  * session_traces — agent loop session metrics
  * audit_logs — changeset apply audit trail
  * file_metadata — uploaded file metadata
  * todos — room-scoped task tracking
  * integration_tokens — OAuth tokens for external integrations (Jira, etc.)
  * chat_rooms — chat room lifecycle metadata
  * chat_messages — durable message archive
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Shared base for all ORM models."""

    pass


class User(Base):
    """Persistent user profile — stable identity across sessions."""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String, primary_key=True)  # UUID
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    auth_provider: Mapped[str] = mapped_column(String(50), nullable=False)
    avatar_color: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class RepoToken(Base):
    """Cached PAT for a git repository URL."""

    __tablename__ = "repo_tokens"

    repo_url: Mapped[str] = mapped_column(String, primary_key=True)
    token: Mapped[str] = mapped_column(Text, nullable=False)
    username: Mapped[str | None] = mapped_column(String, nullable=True)
    cached_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SessionTraceRecord(Base):
    """Persisted session trace for offline analysis."""

    __tablename__ = "session_traces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    query: Mapped[str | None] = mapped_column(Text, nullable=True)
    workspace_path: Mapped[str | None] = mapped_column(String, nullable=True)
    duration_ms: Mapped[float | None] = mapped_column(nullable=True)
    total_input_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_output_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_tool_calls: Mapped[int | None] = mapped_column(Integer, nullable=True)
    iterations_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_answer_chars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    trace_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class AuditLog(Base):
    """Audit trail for changeset apply operations."""

    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    summary_id: Mapped[str | None] = mapped_column(String, nullable=True)
    changeset_hash: Mapped[str] = mapped_column(String, nullable=False)
    applied_by: Mapped[str] = mapped_column(String, nullable=False)
    mode: Mapped[str] = mapped_column(String, nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class FileMetadataRecord(Base):
    """Metadata for uploaded files."""

    __tablename__ = "file_metadata"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False)
    original_filename: Mapped[str] = mapped_column(String, nullable=False)
    stored_filename: Mapped[str] = mapped_column(String, nullable=False)
    file_type: Mapped[str] = mapped_column(String, nullable=False)
    mime_type: Mapped[str] = mapped_column(String, nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class Todo(Base):
    """Room-scoped task/TODO item."""

    __tablename__ = "todos"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    type: Mapped[str] = mapped_column(String, nullable=False, default="task")
    priority: Mapped[str] = mapped_column(String, nullable=False, default="medium")
    status: Mapped[str] = mapped_column(String, nullable=False, default="open")
    file_path: Mapped[str | None] = mapped_column(String, nullable=True)
    line_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_by: Mapped[str] = mapped_column(String, nullable=False, default="")
    assignee: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    source: Mapped[str] = mapped_column(String, nullable=False, default="manual")
    source_id: Mapped[str | None] = mapped_column(String, nullable=True)


class IntegrationToken(Base):
    """OAuth tokens for external integrations (Jira, Teams, Slack, etc.)."""

    __tablename__ = "integration_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_email: Mapped[str] = mapped_column(String, nullable=False)
    provider: Mapped[str] = mapped_column(String, nullable=False)
    access_token: Mapped[str] = mapped_column(Text, nullable=False)
    refresh_token: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cloud_id: Mapped[str] = mapped_column(String, nullable=False, default="")
    site_url: Mapped[str] = mapped_column(String, nullable=False, default="")
    scope: Mapped[str] = mapped_column(String, nullable=False, default="")
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (UniqueConstraint("user_email", "provider", name="uq_integration_user_provider"),)


class ChatRoom(Base):
    """Chat room lifecycle and identity."""

    __tablename__ = "chat_rooms"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str | None] = mapped_column(String, nullable=True)
    owner_email: Mapped[str | None] = mapped_column(String, nullable=True, index=True)
    owner_provider: Mapped[str | None] = mapped_column(String, nullable=True)
    display_name: Mapped[str | None] = mapped_column(String, nullable=True)
    mode: Mapped[str] = mapped_column(String, nullable=False, default="local")
    workspace_path: Mapped[str | None] = mapped_column(String, nullable=True)
    repo_url: Mapped[str | None] = mapped_column(String, nullable=True)
    branch: Mapped[str | None] = mapped_column(String, nullable=True)
    status: Mapped[str] = mapped_column(String, nullable=False, default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    last_active_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChatRoomParticipant(Base):
    """Tracks who participated in a room."""

    __tablename__ = "chat_room_participants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    room_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("chat_rooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    role: Mapped[str] = mapped_column(String, nullable=False, default="guest")
    identity_source: Mapped[str] = mapped_column(String, nullable=False, default="anonymous")
    email: Mapped[str | None] = mapped_column(String, nullable=True)
    provider: Mapped[str | None] = mapped_column(String, nullable=True)
    joined_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("room_id", "user_id", name="uq_participant_room_user"),
        Index("ix_chat_participants_room", "room_id"),
        Index("ix_chat_participants_email", "email"),
    )


class ChatMessageRecord(Base):
    """Durable chat message archive."""

    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    room_id: Mapped[str] = mapped_column(
        String,
        ForeignKey("chat_rooms.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id: Mapped[str] = mapped_column(String, nullable=False)
    display_name: Mapped[str] = mapped_column(String, nullable=False, default="")
    role: Mapped[str] = mapped_column(String, nullable=False)
    type: Mapped[str] = mapped_column(String, nullable=False, default="message")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    identity_source: Mapped[str] = mapped_column(String, nullable=False, default="anonymous")
    parent_message_id: Mapped[str | None] = mapped_column(String, nullable=True)
    ai_data: Mapped[str | None] = mapped_column(Text, nullable=True)
    extra_data: Mapped[str | None] = mapped_column("metadata", Text, nullable=True)
    ts: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (Index("ix_chat_msg_room_ts", "room_id", "ts"),)
