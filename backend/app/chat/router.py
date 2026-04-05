"""Chat router providing WebSocket and HTTP endpoints.

This module provides:
    - GET /chat: Guest chat page (HTML)
    - GET /chat/{room_id}/history: Paginated message history
    - WebSocket /ws/chat/{room_id}: Real-time chat messaging

The WebSocket protocol supports:
    - Message history delivery on connect
    - Message recovery on reconnect (via `since` query parameter)
    - User join/leave notifications
    - Real-time message broadcasting
    - Typing indicators
    - Read receipts
    - Host-initiated session termination
    - Message deduplication

Protocol Message Types:
    - join: User registration
    - message: Chat message
    - file: File upload notification
    - typing: Typing indicator (start/stop)
    - read: Mark message as read
    - end_session: Host ends session
"""

import html
import logging
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import get_config
from app.files.service import FileStorageService

from .manager import (
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
    ChatMessage,
    MessageType,
    UserRole,
    manager,
)
from .stack_trace_parser import parse_stack_trace

router = APIRouter()

# HTML template directory
TEMPLATES_DIR = Path(__file__).parent / "templates"


@router.get("/chat", response_class=HTMLResponse)
async def guest_chat_page(
    roomId: str = Query(..., description="Room ID to join"),
    role: str = Query("engineer", description="User role (host or engineer)"),
) -> HTMLResponse:
    """Serve the standalone guest chat page.

    This page provides a simple chat interface for users who want to
    participate in chat without the full invite page UI.

    Args:
        roomId: The room ID to join (from URL query).
        role: User role, either "host" or "engineer" (default: engineer).

    Returns:
        HTMLResponse with the rendered chat page.

    Example:
        GET /chat?roomId=abc123&role=engineer
    """
    # Validate role (default to engineer if invalid)
    if role not in ("host", "engineer"):
        role = "engineer"

    # Escape user input to prevent XSS attacks
    safe_room_id = html.escape(roomId)

    # Load and render template with simple substitution
    template_path = TEMPLATES_DIR / "guest_chat.html"
    template = template_path.read_text()

    content = template.replace("{{ room_id }}", safe_room_id)
    content = content.replace("{{ room_id[:8] }}", safe_room_id[:8] if len(safe_room_id) >= 8 else safe_room_id)
    # Role is validated above, so it's safe to use directly
    content = content.replace("{{ role }}", role)
    content = content.replace("{{ role | capitalize }}", role.capitalize())

    return HTMLResponse(content=content)


@router.get("/chat/{room_id}/history")
async def get_message_history(
    room_id: str,
    before: Optional[float] = Query(None, description="Timestamp cursor (get messages before this time)"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Number of messages to return"),
) -> JSONResponse:
    """Get paginated message history for a room.

    This endpoint supports lazy loading of chat history. Clients can fetch
    older messages by passing the `before` timestamp of the oldest message
    they currently have.

    Args:
        room_id: The room ID.
        before: Unix timestamp cursor. Returns messages older than this.
                If not provided, returns the most recent messages.
        limit: Maximum number of messages to return (1-100, default 50).

    Returns:
        JSON with messages array and hasMore boolean.

    Example:
        GET /chat/abc123/history?limit=50
        GET /chat/abc123/history?before=1707321600.123&limit=50
    """
    messages = manager.get_paginated_history(room_id, before, limit)

    # Check if there are more messages before the oldest returned
    has_more = False
    if messages:
        oldest_ts = messages[0].ts
        older_messages = manager.get_paginated_history(room_id, oldest_ts, 1)
        has_more = len(older_messages) > 0

    history_msgs = []
    for msg in messages:
        d = msg.model_dump()
        if d.get("type") == "code_snippet" and d.get("metadata") and not d.get("codeSnippet"):
            d["codeSnippet"] = d["metadata"]
        history_msgs.append(d)

    return JSONResponse({"messages": history_msgs, "hasMore": has_more})


@router.post("/chat/{room_id}/ai-message")
async def post_ai_message(
    room_id: str,
    message_type: str = Query(..., description="Message type: ai_summary or ai_code_prompt"),
    model_name: str = Query(..., description="AI model name (e.g., claude_bedrock)"),
    content: str = Query(..., description="Message content (summary text or code prompt)"),
    ai_data: Optional[str] = Query(None, description="JSON string of AI-specific data"),
    parent_message_id: Optional[str] = Query(None, description="ID of the parent message this replies to"),
) -> JSONResponse:
    """Post an AI-generated message to a chat room.

    This endpoint allows the extension to post AI-generated summaries and code prompts
    as chat messages. The message is stored in history and broadcast to all clients.

    Args:
        room_id: The room ID to post to.
        message_type: Either 'ai_summary' or 'ai_code_prompt'.
        model_name: The AI model name (used in userId as AI-{model_name}).
        content: The message content.
        ai_data: Optional JSON string with AI-specific data.

    Returns:
        JSONResponse with the created message.
    """
    import json

    # Validate message type
    valid_types = ("ai_summary", "ai_code_prompt", "ai_explanation", "ai_answer")
    if message_type not in valid_types:
        return JSONResponse({"error": f"Invalid message type: {message_type}"}, status_code=400)

    # Parse AI data if provided
    parsed_ai_data = None
    if ai_data:
        try:
            parsed_ai_data = json.loads(ai_data)
        except json.JSONDecodeError:
            return JSONResponse({"error": "Invalid ai_data JSON"}, status_code=400)

    # Create AI user ID
    ai_user_id = f"AI-{model_name}"
    ai_display_name = f"AI ({model_name})"

    # Create the message
    _type_map = {
        "ai_summary": MessageType.AI_SUMMARY,
        "ai_code_prompt": MessageType.AI_CODE_PROMPT,
        "ai_explanation": MessageType.AI_EXPLANATION,
        "ai_answer": MessageType.AI_ANSWER,
    }
    msg_type = _type_map[message_type]
    message = ChatMessage(
        type=msg_type,
        roomId=room_id,
        userId=ai_user_id,
        displayName=ai_display_name,
        role=UserRole.AI,
        content=content,
        identitySource="ai",
        parentMessageId=parent_message_id,
        aiData=parsed_ai_data,
    )

    # Store in history
    await manager.add_message(room_id, message)

    # Broadcast to all clients in the room
    await manager.broadcast({"type": message_type, **message.model_dump()}, room_id)

    logger.info(f"[AI] Posted {message_type} to room {room_id} from {ai_user_id}")

    return JSONResponse(message.model_dump())


# ---------------------------------------------------------------------------
# Room discovery & incremental sync endpoints
# ---------------------------------------------------------------------------


@router.get("/chat/rooms")
async def list_user_rooms(
    email: str = Query(..., description="SSO email to find rooms for"),
) -> JSONResponse:
    """List active/ended rooms owned by an SSO-authenticated user."""
    from app.main import app

    persistence = getattr(app.state, "chat_persistence", None)
    if not persistence:
        return JSONResponse({"rooms": [], "error": "persistence disabled"})
    rooms = await persistence.get_rooms_for_user(email)
    return JSONResponse({"rooms": rooms})


@router.delete("/chat/{room_id}")
async def delete_room(room_id: str) -> JSONResponse:
    """Delete a room and all its data (messages, files, audit logs)."""
    from app.main import app

    # Clear in-memory history
    await manager.clear_message_history(room_id)

    # Delete from Postgres
    persistence = getattr(app.state, "chat_persistence", None)
    if persistence:
        await persistence.delete_room(room_id)

    # Delete files
    try:
        file_service = FileStorageService.get_instance()
        await file_service.delete_room_files(room_id)
    except Exception:
        pass

    # Delete audit logs
    try:
        from app.audit.service import AuditLogService

        await AuditLogService.get_instance().delete_room_logs(room_id)
    except Exception:
        pass

    return JSONResponse({"ok": True})


@router.get("/chat/{room_id}/status")
async def room_status(room_id: str) -> JSONResponse:
    """Check if a room exists, its status, and whether it has in-memory state."""
    has_history = len(manager.message_history.get(room_id, [])) > 0

    # Check Postgres if no in-memory state
    pg_status = None
    from app.main import app

    persistence = getattr(app.state, "chat_persistence", None)
    if persistence and not has_history:
        from app.db.models import ChatRoom

        try:
            from sqlalchemy import select as sa_select

            async with persistence._session_factory() as session:
                row = (await session.execute(sa_select(ChatRoom).where(ChatRoom.id == room_id))).scalar_one_or_none()
                if row:
                    pg_status = row.status
        except Exception:
            pass

    return JSONResponse(
        {
            "room_id": room_id,
            "active_connections": manager.get_room_size(room_id),
            "has_history": has_history,
            "pg_status": pg_status,
        }
    )


@router.get("/chat/{room_id}/messages/after")
async def get_messages_after(
    room_id: str,
    last_id: str = Query(..., description="UUID of the last known message — return everything after it"),
    limit: int = Query(500, ge=1, le=1000),
) -> JSONResponse:
    """Incremental sync by message UUID.

    Looks up the timestamp of *last_id*, then returns all messages with ts > that value.
    More robust than timestamp-based sync (avoids clock skew issues).
    """
    # Try in-memory first
    history = manager.message_history.get(room_id, [])
    pivot_ts = None
    for msg in history:
        if msg.id == last_id:
            pivot_ts = msg.ts
            break

    if pivot_ts is not None:
        newer = [m.model_dump() for m in history if m.ts > pivot_ts]
        return JSONResponse({"messages": newer[:limit], "source": "memory"})

    # Fall back to Postgres
    from app.main import app

    persistence = getattr(app.state, "chat_persistence", None)
    if persistence:
        # Look up the ts of last_id in Postgres
        try:
            from sqlalchemy import select as sa_select

            from app.db.models import ChatMessageRecord

            async with persistence._session_factory() as session:
                row = (
                    await session.execute(sa_select(ChatMessageRecord.ts).where(ChatMessageRecord.id == last_id))
                ).scalar_one_or_none()
                if row is not None:
                    pivot_ts = row
        except Exception:
            pass

        if pivot_ts is not None:
            msgs = await persistence.get_messages_since(room_id, pivot_ts, limit=limit)
            return JSONResponse({"messages": msgs, "source": "postgres"})

    # last_id not found anywhere — return full history
    if persistence:
        msgs = await persistence.load_messages_from_postgres(room_id, limit=limit)
        return JSONResponse({"messages": msgs, "source": "postgres_full"})

    return JSONResponse({"messages": [], "source": "none"})


@router.get("/chat/{room_id}/messages/since")
async def get_messages_since(
    room_id: str,
    since: float = Query(..., description="Unix timestamp — return messages newer than this"),
    limit: int = Query(500, ge=1, le=1000),
) -> JSONResponse:
    """Incremental sync: get messages newer than *since* timestamp.

    Checks in-memory first, then Postgres.
    """
    # Try in-memory
    history = manager.message_history.get(room_id, [])
    newer = [m.model_dump() for m in history if m.ts > since]
    if newer:
        return JSONResponse({"messages": newer[:limit], "source": "memory"})

    # Fall back to Postgres
    from app.main import app

    persistence = getattr(app.state, "chat_persistence", None)
    if persistence:
        msgs = await persistence.get_messages_since(room_id, since, limit=limit)
        return JSONResponse({"messages": msgs, "source": "postgres"})

    return JSONResponse({"messages": [], "source": "none"})


@router.websocket("/ws/chat/{room_id}")
async def websocket_chat_endpoint(
    websocket: WebSocket,
    room_id: str,
    since: Optional[float] = Query(None, description="Timestamp for message recovery on reconnect"),
) -> None:
    """WebSocket endpoint for real-time chat in a room.

    This endpoint handles the complete chat lifecycle for a single client.

    SECURITY MODEL:
        - Backend assigns userId on connection (never trust client-provided IDs)
        - Backend determines role: first user = host, others = guest
        - Backend validates permissions for sensitive operations

    Protocol Flow:
        1. Client connects → Server assigns userId and role
           → Server sends: {type: "connected", userId: "xxx", role: "host/guest"}
           → Server sends: {type: "history", messages: [], users: []}
        2. Client sends: {type: "join", displayName} (uses backend-assigned userId/role)
           → Server broadcasts: {type: "user_joined", user: {}, users: []}
        3. Client sends: {content} (messages use backend-tracked userId/role)
           → Server broadcasts: {type: "message", ...fullMessage}
        4. Client sends: {type: "read", messageId}
           → Server broadcasts: {type: "read_receipt", messageId, readBy: [...]}
        5. Client sends: {type: "end_session"} (host only, validated by backend)
           → Server broadcasts: {type: "session_ended", message: "..."}
        6. On disconnect → Server broadcasts: {type: "user_left", user: {}, users: []}

    Args:
        websocket: The WebSocket connection.
        room_id: The room ID to join.
        since: Optional timestamp for message recovery on reconnect.
    """
    client_host = websocket.client.host if websocket.client else "unknown"
    logger.info(f"[WS] New connection to room: {room_id}, since={since}, client={client_host}")

    # Enforce max_participants from config (0 = no limit)
    max_participants = get_config().session.max_participants
    if max_participants > 0 and manager.get_room_size(room_id) >= max_participants:
        logger.warning(f"[WS] Room {room_id} is full ({max_participants} participants). Rejecting new connection.")
        await websocket.close(code=1008)  # 1008 = Policy Violation
        return

    # SECURITY: Backend assigns userId and role on connection
    try:
        assigned_user_id, assigned_role, history = await manager.connect(websocket, room_id)
    except Exception as exc:
        logger.exception(f"[WS] manager.connect() raised an exception for room {room_id}: {exc}")
        raise
    logger.info(
        f"[WS] Connection accepted. Assigned userId={assigned_user_id}, role={assigned_role}. "
        f"Room {room_id} now has {manager.get_room_size(room_id)} connections"
    )

    try:
        # SECURITY: Send backend-assigned credentials to client FIRST
        # Client MUST use these credentials for all subsequent operations
        await websocket.send_json(
            {
                "type": "connected",
                "userId": assigned_user_id,
                "role": assigned_role,
                "leadId": manager.get_lead_id(room_id),
            }
        )
        logger.info(
            f"[WS] Sent 'connected' with userId={assigned_user_id}, role={assigned_role}, leadId={manager.get_lead_id(room_id)}"
        )

        # Hydrate from Postgres if no in-memory history (room was idle / restarted)
        if not history:
            _persistence = getattr(manager, "_persistence", None)
            if _persistence:
                try:
                    pg_msgs = await _persistence.hydrate_room(
                        room_id,
                        redis_store=manager._redis_store,
                    )
                    if pg_msgs:
                        for m in pg_msgs:
                            cm = ChatMessage(**m)
                            if room_id not in manager.message_history:
                                manager.message_history[room_id] = []
                            manager.message_history[room_id].append(cm)
                        history = manager.message_history.get(room_id, [])
                        logger.info(f"[WS] Hydrated {len(pg_msgs)} messages from Postgres for room {room_id}")
                except Exception as exc:
                    logger.warning(f"[WS] Room hydration failed for {room_id}: {exc}")

        # If reconnecting with `since`, only send messages newer than that timestamp
        if since is not None:
            history = manager.get_messages_since(room_id, since)
            logger.info(f"[WS] Reconnect recovery: sending {len(history)} messages since {since}")

        # Ensure room exists in Postgres (upsert) — skip for local mode
        # Local mode stores everything client-side; no Postgres dependency needed.
        if not manager._is_local_room(room_id):
            _persistence = getattr(manager, "_persistence", None)
            if _persistence:
                sso_info = manager.room_sso_hosts.get(room_id, {})
                try:
                    await _persistence.ensure_room(
                        room_id,
                        owner_email=sso_info.get("email"),
                        owner_provider=sso_info.get("provider"),
                    )
                except Exception as exc:
                    logger.warning(f"[WS] ensure_room failed for {room_id}: {exc}")

        # Send message history and user list to the newly connected client
        # For code_snippet messages, copy metadata → codeSnippet so the
        # frontend renderer finds data in the same field as live broadcasts.
        history_data = []
        for msg in history:
            d = msg.model_dump()
            if d.get("type") == "code_snippet" and d.get("metadata") and not d.get("codeSnippet"):
                d["codeSnippet"] = d["metadata"]
            history_data.append(d)
        users_data = [u.model_dump() for u in manager.get_room_users(room_id)]

        await websocket.send_json(
            {
                "type": "history",
                "messages": history_data,
                "users": users_data,
                "leadId": manager.get_lead_id(room_id),
                "isRecovery": since is not None,  # Tell client this is a reconnection
            }
        )

        # Main message loop
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")
            logger.debug("[WS] Room %s received: type=%s", room_id, data.get("type", "?"))

            # --- Handle JOIN message (user registration) ---
            # SECURITY: Use backend-assigned userId and role, ignore client-provided values
            if message_type == "join":
                logger.info(f"[WS] JOIN from backend-assigned userId={assigned_user_id}, role={assigned_role}")
                sso_email = data.get("ssoEmail")
                sso_provider = data.get("ssoProvider")
                user_uuid = data.get("userUuid")  # Stable UUID from SSO/Postgres
                display_name = data.get("displayName", "")
                identity_source = data.get("identitySource", "anonymous")

                # Stable identity: if SSO provides a userUuid (from Postgres users table),
                # use it instead of the random UUID from connect(). This ensures messages
                # always carry the same userId across sessions and reconnections.
                if user_uuid and identity_source == "sso":
                    old_id = assigned_user_id
                    assigned_user_id = user_uuid
                    # Migrate room host/lead references from temp ID to stable UUID
                    if manager.room_hosts.get(room_id) == old_id:
                        manager.room_hosts[room_id] = assigned_user_id
                    if manager.room_leads.get(room_id) == old_id:
                        manager.room_leads[room_id] = assigned_user_id
                    # Send corrected identity to client
                    await websocket.send_json(
                        {
                            "type": "connected",
                            "userId": assigned_user_id,
                            "role": assigned_role,
                            "leadId": manager.get_lead_id(room_id),
                        }
                    )
                    logger.info(f"[WS] Using stable userUuid={assigned_user_id} (was temp={old_id}) in room {room_id}")

                # Legacy identity reconciliation: if this SSO user was in the room
                # before (no userUuid), reuse their original user_id to avoid duplicates.
                elif sso_email:
                    reclaimed_id = manager.reclaim_user_by_sso(
                        room_id,
                        assigned_user_id,
                        sso_email,
                    )
                    if reclaimed_id:
                        assigned_user_id = reclaimed_id
                        # Restore role from room state
                        if manager.room_hosts.get(room_id) == assigned_user_id:
                            assigned_role = "host"
                        # Send corrected identity to client
                        await websocket.send_json(
                            {
                                "type": "connected",
                                "userId": assigned_user_id,
                                "role": assigned_role,
                                "leadId": manager.get_lead_id(room_id),
                            }
                        )
                        logger.info(f"[WS] Identity reclaimed via SSO for user {assigned_user_id} in room {room_id}")

                user = manager.register_user(
                    websocket=websocket,
                    room_id=room_id,
                    user_id=assigned_user_id,  # SECURITY: Use backend-assigned ID
                    display_name=display_name,
                    role=assigned_role,  # SECURITY: Use backend-assigned role
                    identity_source=identity_source,
                    sso_email=sso_email,
                    sso_provider=sso_provider,
                )

                # SSO reconnect: elevate role if credentials match stored host identity.
                role_restored = manager.try_restore_host_by_sso(room_id, assigned_user_id, sso_email, sso_provider)
                if role_restored:
                    assigned_role = "host"
                    logger.info(f"[WS] Host role restored via SSO for user {assigned_user_id} in room {room_id}")
                    await websocket.send_json(
                        {
                            "type": "role_restored",
                            "role": "host",
                            "leadId": manager.get_lead_id(room_id),
                        }
                    )

                # Broadcast updated user list to all clients
                users_data = [u.model_dump() for u in manager.get_room_users(room_id)]
                logger.info(f"[WS] Broadcasting user_joined. Total users: {len(users_data)}")
                await manager.broadcast(
                    {"type": "user_joined", "user": user.model_dump(), "users": users_data}, room_id
                )

                # Track participant in Postgres
                _persistence = getattr(manager, "_persistence", None)
                if _persistence:
                    try:
                        await _persistence.upsert_participant(
                            room_id=room_id,
                            user_id=assigned_user_id,
                            display_name=display_name,
                            role=assigned_role,
                            identity_source=identity_source,
                            email=sso_email,
                            provider=sso_provider,
                        )
                    except Exception as exc:
                        logger.warning(f"[WS] upsert_participant failed: {exc}")
                continue

            # --- Handle END_SESSION message (host only) ---
            # SECURITY: Validate using backend-tracked userId and role
            if message_type == "end_session":
                # SECURITY: Use backend-assigned userId, not client-provided
                if not manager.can_end_session(room_id, assigned_user_id):
                    logger.warning(f"[WS] Unauthorized end_session attempt by userId={assigned_user_id}")
                    await websocket.send_json({"type": "error", "error": "Only the host can end the session"})
                    continue

                # Check blockers before proceeding
                from .manager import check_end_chat_blockers

                blockers = check_end_chat_blockers(room_id)
                if blockers:
                    logger.info(f"[WS] end_session blocked for room {room_id}: {blockers}")
                    await websocket.send_json(
                        {
                            "type": "end_session_blocked",
                            "blockers": blockers,
                            "message": f"Cannot end session: {', '.join(blockers)}",
                        }
                    )
                    continue

                logger.info(f"[WS] Host {assigned_user_id} ending session for room {room_id}")

                # Mark room as ended in Postgres (flush micro-batch buffer)
                _persistence = getattr(manager, "_persistence", None)
                if _persistence:
                    try:
                        await _persistence.end_room(room_id)
                    except Exception as exc:
                        logger.error(f"[WS] end_room persistence failed for {room_id}: {exc}")

                # Delete all files for this room
                try:
                    file_service = FileStorageService.get_instance()
                    deleted_count = await file_service.delete_room_files(room_id)
                    logger.info(f"[WS] Deleted {deleted_count} files for room {room_id}")
                except Exception as e:
                    logger.error(f"[WS] Failed to delete files for room {room_id}: {e}")

                await manager.broadcast(
                    {"type": "session_ended", "message": "Host has ended the chat session"}, room_id
                )

                # Clear all room data (in-memory + Redis)
                await manager.clear_room(room_id)
                continue

            # --- Handle QUIT_CHAT message (any user) ---
            # Leave the room but preserve all data for later rejoin.
            if message_type == "quit_chat":
                logger.info(f"[WS] User {assigned_user_id} quitting room {room_id} (data preserved)")

                # Drain micro-batch buffer to Postgres
                _persistence = getattr(manager, "_persistence", None)
                if _persistence:
                    try:
                        await _persistence._flush_buffer(room_id)
                    except Exception as exc:
                        logger.warning(f"[WS] quit_chat flush failed for {room_id}: {exc}")

                # Send confirmation before disconnecting
                await websocket.send_json(
                    {
                        "type": "quit_confirmed",
                        "room_id": room_id,
                        "message": "Left room. Data preserved.",
                    }
                )

                # Disconnect user (reuse existing logic)
                disconnected_user, lead_reverted = manager.disconnect(websocket, room_id)
                if disconnected_user:
                    users_data = [u.model_dump() for u in manager.get_room_users(room_id)]
                    await manager.broadcast(
                        {
                            "type": "user_left",
                            "user": disconnected_user.model_dump(),
                            "users": users_data,
                        },
                        room_id,
                    )
                    if lead_reverted:
                        new_lead_id = manager.get_lead_id(room_id)
                        await manager.broadcast(
                            {
                                "type": "lead_changed",
                                "leadId": new_lead_id,
                            },
                            room_id,
                        )
                break  # Exit the WebSocket message loop

            # --- Handle TRANSFER_LEAD message (host or current lead only) ---
            # SECURITY: Only host or current lead can transfer lead
            if message_type == "transfer_lead":
                target_user_id = data.get("targetUserId")
                if not manager.can_configure(room_id, assigned_user_id):
                    logger.warning(f"[WS] Unauthorized transfer_lead attempt by userId={assigned_user_id}")
                    await websocket.send_json(
                        {"type": "error", "error": "Only the host or current lead can transfer lead"}
                    )
                    continue

                if not target_user_id or not manager.transfer_lead(room_id, target_user_id):
                    await websocket.send_json({"type": "error", "error": "Invalid target user for lead transfer"})
                    continue

                logger.info(f"[WS] Lead transferred to {target_user_id} in room {room_id}")
                await manager.broadcast({"type": "lead_changed", "leadId": target_user_id}, room_id)
                continue

            # --- Handle TYPING indicator message ---
            # SECURITY: Use backend-assigned userId
            if message_type == "typing":
                user_info = manager.get_user(room_id, assigned_user_id)
                if user_info:
                    await manager.broadcast_except(
                        {
                            "type": "typing",
                            "userId": assigned_user_id,
                            "displayName": user_info.displayName,
                            "isTyping": data.get("isTyping", True),
                        },
                        room_id,
                        exclude_websocket=websocket,
                    )
                continue

            # --- Handle READ receipt message ---
            # SECURITY: Use backend-assigned userId
            if message_type == "read":
                message_id = data.get("messageId", "")
                if message_id:
                    # Mark message as read and get all readers
                    read_by = manager.mark_message_read(room_id, message_id, assigned_user_id)
                    # Broadcast read receipt to all clients
                    await manager.broadcast(
                        {"type": "read_receipt", "messageId": message_id, "readBy": list(read_by)}, room_id
                    )
                continue

            # --- Handle FILE message (broadcast file upload notification) ---
            # SECURITY: Use backend-assigned userId and role
            if message_type == "file":
                message_id = data.get("id", "") or str(uuid.uuid4())

                # Deduplication check
                if manager.is_duplicate_message(room_id, message_id):
                    logger.debug(f"[WS] Duplicate file message ignored: {message_id}")
                    continue

                user_info = manager.get_user(room_id, assigned_user_id)
                display_name = user_info.displayName if user_info else data.get("displayName", "")
                identity_src = user_info.identitySource if user_info else "anonymous"

                file_meta = {
                    "fileId": data.get("fileId", ""),
                    "originalFilename": data.get("originalFilename", ""),
                    "fileType": data.get("fileType", "other"),
                    "mimeType": data.get("mimeType", ""),
                    "sizeBytes": data.get("sizeBytes", 0),
                    "downloadUrl": data.get("downloadUrl", ""),
                    "caption": data.get("caption"),
                }

                file_msg = ChatMessage(
                    id=message_id,
                    type=MessageType.FILE,
                    roomId=room_id,
                    userId=assigned_user_id,
                    displayName=display_name,
                    role=assigned_role,
                    content=data.get("caption") or data.get("originalFilename", ""),
                    identitySource=identity_src,
                    metadata=file_meta,
                )
                await manager.add_message(room_id, file_msg)

                broadcast_data = file_msg.model_dump()
                broadcast_data.update(file_meta)
                logger.info(f"[WS] Broadcasting file message: {file_meta.get('originalFilename')}")
                await manager.broadcast(broadcast_data, room_id)
                continue

            # --- Handle CODE SNIPPET message ---
            # SECURITY: Use backend-assigned userId and role
            if message_type == "code_snippet":
                user_info = manager.get_user(room_id, assigned_user_id)
                display_name = user_info.displayName if user_info else data.get("displayName", "")
                identity_src = user_info.identitySource if user_info else "anonymous"
                cs = data.get("codeSnippet", {})

                snippet_meta = {
                    "filename": cs.get("filename", ""),
                    "relativePath": cs.get("relativePath", ""),
                    "language": cs.get("language", ""),
                    "startLine": cs.get("startLine", 1),
                    "endLine": cs.get("endLine", 1),
                    "code": cs.get("code", ""),
                }

                snippet_msg = ChatMessage(
                    type=MessageType.CODE_SNIPPET,
                    roomId=room_id,
                    userId=assigned_user_id,
                    displayName=display_name,
                    role=assigned_role,
                    content=data.get("content", ""),
                    identitySource=identity_src,
                    metadata=snippet_meta,
                )
                await manager.add_message(room_id, snippet_msg)

                broadcast_data = snippet_msg.model_dump()
                broadcast_data["codeSnippet"] = snippet_meta
                logger.info(
                    f"[WS] Broadcasting code snippet: {snippet_meta.get('relativePath', 'unknown')} lines {snippet_meta.get('startLine')}-{snippet_meta.get('endLine')}"
                )
                await manager.broadcast(broadcast_data, room_id)
                continue

            # --- Handle STACK TRACE message ---
            # SECURITY: Use backend-assigned userId and role
            if message_type == "stack_trace":
                user_info = manager.get_user(room_id, assigned_user_id)
                display_name = user_info.displayName if user_info else data.get("displayName", "")
                identity_src = user_info.identitySource if user_info else "anonymous"

                raw_text = data.get("rawText", "")
                parsed = data.get("parsed") or parse_stack_trace(raw_text).to_dict()

                trace_msg = ChatMessage(
                    type="stack_trace",
                    roomId=room_id,
                    userId=assigned_user_id,
                    displayName=display_name,
                    role=assigned_role,
                    content=raw_text or data.get("content", ""),
                    identitySource=identity_src,
                    metadata={"stackTrace": parsed},
                )
                await manager.add_message(room_id, trace_msg)

                broadcast_data = trace_msg.model_dump()
                broadcast_data["stackTrace"] = parsed
                logger.info(
                    f"[WS] Broadcasting stack_trace from {assigned_user_id}: "
                    f"{parsed.get('errorType', 'unknown')} – {len(parsed.get('frames', []))} frames"
                )
                await manager.broadcast(broadcast_data, room_id)
                continue

            # --- Handle TEST FAILURE message ---
            # SECURITY: Use backend-assigned userId and role
            if message_type == "test_failure":
                user_info = manager.get_user(room_id, assigned_user_id)
                display_name = user_info.displayName if user_info else data.get("displayName", "")
                identity_src = user_info.identitySource if user_info else "anonymous"

                test_failure = data.get("testFailure", {})
                total_failed = test_failure.get("totalFailed", 0)

                fail_msg = ChatMessage(
                    type="test_failure",
                    roomId=room_id,
                    userId=assigned_user_id,
                    displayName=display_name,
                    role=assigned_role,
                    content=data.get("content", f"{total_failed} test(s) failed"),
                    identitySource=identity_src,
                    metadata={"testFailure": test_failure},
                )
                await manager.add_message(room_id, fail_msg)

                broadcast_data = fail_msg.model_dump()
                broadcast_data["testFailure"] = test_failure
                logger.info(
                    f"[WS] Broadcasting test_failure from {assigned_user_id}: "
                    f"{total_failed} failures ({test_failure.get('framework', 'unknown')} framework)"
                )
                await manager.broadcast(broadcast_data, room_id)
                continue

            # --- Handle tool_response (from extension's local tool execution) ---
            if message_type == "tool_response":
                from app.code_tools.proxy import tool_proxy

                tool_proxy.handle_response(data)
                continue

            # --- Handle regular CHAT message ---
            # SECURITY: Use backend-assigned userId and role for all messages
            content = data.get("content", "")

            # Validate: content is required and cannot be empty for regular messages
            if not content or not content.strip():
                await websocket.send_json({"type": "error", "error": "Invalid message format: content is required"})
                continue

            logger.info(f"[WS] CHAT message from backend-assigned userId={assigned_user_id}: {content[:50]}")

            # Use registered display name if available
            user_info = manager.get_user(room_id, assigned_user_id)
            display_name = user_info.displayName if user_info else data.get("displayName", "")

            # Create and store the full message
            # SECURITY: Use backend-assigned userId and role, ignore client-provided values
            identity_src = user_info.identitySource if user_info else "anonymous"
            full_message = ChatMessage(
                roomId=room_id,
                userId=assigned_user_id,
                displayName=display_name,
                role=assigned_role,
                content=content,
                identitySource=identity_src,
            )
            await manager.add_message(room_id, full_message)

            # Broadcast to all clients in the room
            logger.info(f"[WS] Broadcasting message to {manager.get_room_size(room_id)} connections")
            await manager.broadcast({"type": "message", **full_message.model_dump()}, room_id)

    except WebSocketDisconnect:
        # Capture identity BEFORE disconnect() removes the websocket mapping.
        pre_info = manager.websocket_to_user.get(websocket)
        pre_user_id = pre_info[1] if pre_info else None
        is_host_disconnect = pre_user_id is not None and manager.is_host(room_id, pre_user_id)
        host_had_sso = room_id in manager.room_sso_hosts

        disconnected_user, lead_reverted = manager.disconnect(websocket, room_id)

        if disconnected_user:
            # Track participant leaving in Postgres
            _persistence = getattr(manager, "_persistence", None)
            if _persistence and pre_user_id:
                try:
                    await _persistence.mark_participant_left(room_id, pre_user_id)
                except Exception as exc:
                    logger.warning(f"[WS] mark_participant_left failed: {exc}")

            users_data = [u.model_dump() for u in manager.get_room_users(room_id)]
            await manager.broadcast(
                {"type": "user_left", "user": disconnected_user.model_dump(), "users": users_data}, room_id
            )

            # If lead reverted to host on disconnect, broadcast lead_changed
            if lead_reverted:
                new_lead_id = manager.get_lead_id(room_id)
                logger.info(f"[WS] Lead reverted to host {new_lead_id} in room {room_id}")
                await manager.broadcast({"type": "lead_changed", "leadId": new_lead_id}, room_id)

        # Non-SSO host disconnect: purge history and audit logs.
        if is_host_disconnect and not host_had_sso:
            logger.info(f"[WS] Non-SSO host {pre_user_id} left room {room_id} — clearing history and audit logs")
            await manager.clear_message_history(room_id)
            try:
                from app.audit.service import AuditLogService

                await AuditLogService.get_instance().delete_room_logs(room_id)
            except Exception as exc:
                logger.error(f"[WS] Could not delete audit logs for room {room_id}: {exc}")
            await manager.broadcast(
                {
                    "type": "history_cleared",
                    "reason": "host_session_ended",
                },
                room_id,
            )

    except Exception as exc:
        logger.exception(
            f"[WS] Unhandled exception in websocket_chat_endpoint for room {room_id}, "
            f"userId={assigned_user_id if 'assigned_user_id' in dir() else 'unassigned'}: {exc}"
        )
        manager.disconnect(websocket, room_id)
        raise
