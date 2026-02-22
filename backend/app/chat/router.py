"""Chat router providing WebSocket and HTTP endpoints.

This module provides:
    - GET /chat: Guest chat page (HTML)
    - GET /invite: Unified invite page with Live Share button and chat
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
import time
import uuid
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from .manager import (
    ChatMessage,
    ChatMessageInput,
    IdentitySource,
    MessageType,
    UserRole,
    manager,
    DEFAULT_PAGE_SIZE,
    MAX_PAGE_SIZE,
)
from .stack_trace_parser import parse_stack_trace
from app.files.service import FileStorageService

router = APIRouter()

# HTML template directory
TEMPLATES_DIR = Path(__file__).parent / "templates"


@router.get("/chat", response_class=HTMLResponse)
async def guest_chat_page(
    roomId: str = Query(..., description="Room ID to join"),
    role: str = Query("engineer", description="User role (host or engineer)")
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
    content = content.replace(
        "{{ room_id[:8] }}",
        safe_room_id[:8] if len(safe_room_id) >= 8 else safe_room_id
    )
    # Role is validated above, so it's safe to use directly
    content = content.replace("{{ role }}", role)
    content = content.replace("{{ role | capitalize }}", role.capitalize())

    return HTMLResponse(content=content)


@router.get("/invite", response_class=HTMLResponse)
async def invite_page(
    roomId: str = Query(..., description="Room ID to join"),
    liveShareUrl: str = Query(..., description="VS Code Live Share URL")
) -> HTMLResponse:
    """Serve the unified invite page with Live Share button and embedded chat.

    This page is the main entry point for guests. It displays:
    - Session info (room ID)
    - "Join Live Share in VS Code" button
    - Embedded chat iframe (2:1 layout ratio)

    Args:
        roomId: The room ID to join.
        liveShareUrl: The VS Code Live Share URL (URL-encoded).

    Returns:
        HTMLResponse with the rendered invite page.

    Example:
        GET /invite?roomId=abc123&liveShareUrl=https%3A%2F%2Fprod.liveshare...
    """
    template_path = TEMPLATES_DIR / "invite.html"
    template = template_path.read_text()

    # Escape user inputs to prevent XSS attacks
    safe_room_id = html.escape(roomId)
    safe_live_share_url = html.escape(liveShareUrl)

    # Simple template substitution (no Jinja2 dependency)
    content = template.replace("{{ room_id }}", safe_room_id)
    content = content.replace(
        "{{ room_id[:8] }}",
        safe_room_id[:8] if len(safe_room_id) >= 8 else safe_room_id
    )
    content = content.replace("{{ live_share_url }}", safe_live_share_url)

    return HTMLResponse(content=content)


@router.get("/chat/{room_id}/history")
async def get_message_history(
    room_id: str,
    before: Optional[float] = Query(None, description="Timestamp cursor (get messages before this time)"),
    limit: int = Query(DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Number of messages to return")
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

    return JSONResponse({
        "messages": [msg.model_dump() for msg in messages],
        "hasMore": has_more
    })


@router.post("/chat/{room_id}/ai-message")
async def post_ai_message(
    room_id: str,
    message_type: str = Query(..., description="Message type: ai_summary or ai_code_prompt"),
    model_name: str = Query(..., description="AI model name (e.g., claude_bedrock)"),
    content: str = Query(..., description="Message content (summary text or code prompt)"),
    ai_data: Optional[str] = Query(None, description="JSON string of AI-specific data")
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
    valid_types = ("ai_summary", "ai_code_prompt", "ai_explanation")
    if message_type not in valid_types:
        return JSONResponse(
            {"error": f"Invalid message type: {message_type}"},
            status_code=400
        )

    # Parse AI data if provided
    parsed_ai_data = None
    if ai_data:
        try:
            parsed_ai_data = json.loads(ai_data)
        except json.JSONDecodeError:
            return JSONResponse(
                {"error": "Invalid ai_data JSON"},
                status_code=400
            )

    # Create AI user ID
    ai_user_id = f"AI-{model_name}"
    ai_display_name = f"AI ({model_name})"

    # Create the message
    _type_map = {
        "ai_summary": MessageType.AI_SUMMARY,
        "ai_code_prompt": MessageType.AI_CODE_PROMPT,
        "ai_explanation": MessageType.AI_EXPLANATION,
    }
    msg_type = _type_map[message_type]
    message = ChatMessage(
        type=msg_type,
        roomId=room_id,
        userId=ai_user_id,
        displayName=ai_display_name,
        role=UserRole.AI,
        content=content,
        aiData=parsed_ai_data
    )

    # Store in history
    manager.add_message(room_id, message)

    # Broadcast to all clients in the room
    await manager.broadcast(
        {"type": message_type, **message.model_dump()},
        room_id
    )

    logger.info(f"[AI] Posted {message_type} to room {room_id} from {ai_user_id}")

    return JSONResponse(message.model_dump())


@router.websocket("/ws/chat/{room_id}")
async def websocket_chat_endpoint(
    websocket: WebSocket,
    room_id: str,
    since: Optional[float] = Query(None, description="Timestamp for message recovery on reconnect")
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
    logger.info(f"[WS] New connection to room: {room_id}, since={since}")

    # SECURITY: Backend assigns userId and role on connection
    assigned_user_id, assigned_role, history = await manager.connect(websocket, room_id)
    logger.info(
        f"[WS] Connection accepted. Assigned userId={assigned_user_id}, role={assigned_role}. "
        f"Room {room_id} now has {manager.get_room_size(room_id)} connections"
    )

    try:
        # SECURITY: Send backend-assigned credentials to client FIRST
        # Client MUST use these credentials for all subsequent operations
        await websocket.send_json({
            "type": "connected",
            "userId": assigned_user_id,
            "role": assigned_role,
            "leadId": manager.get_lead_id(room_id)
        })
        logger.info(f"[WS] Sent 'connected' with userId={assigned_user_id}, role={assigned_role}, leadId={manager.get_lead_id(room_id)}")

        # If reconnecting with `since`, only send messages newer than that timestamp
        if since is not None:
            history = manager.get_messages_since(room_id, since)
            logger.info(f"[WS] Reconnect recovery: sending {len(history)} messages since {since}")

        # Send message history and user list to the newly connected client
        history_data = [msg.model_dump() for msg in history]
        users_data = [u.model_dump() for u in manager.get_room_users(room_id)]

        await websocket.send_json({
            "type": "history",
            "messages": history_data,
            "users": users_data,
            "leadId": manager.get_lead_id(room_id),
            "isRecovery": since is not None  # Tell client this is a reconnection
        })

        # Main message loop
        while True:
            data = await websocket.receive_json()
            message_type = data.get("type")
            logger.debug("[WS] Room %s received: type=%s", room_id, data.get("type", "?"))

            # --- Handle JOIN message (user registration) ---
            # SECURITY: Use backend-assigned userId and role, ignore client-provided values
            if message_type == "join":
                logger.info(
                    f"[WS] JOIN from backend-assigned userId={assigned_user_id}, role={assigned_role}"
                )
                user = manager.register_user(
                    websocket=websocket,
                    room_id=room_id,
                    user_id=assigned_user_id,  # SECURITY: Use backend-assigned ID
                    display_name=data.get("displayName", ""),
                    role=assigned_role,  # SECURITY: Use backend-assigned role
                    identity_source=data.get("identitySource", "anonymous")
                )

                # Broadcast updated user list to all clients
                users_data = [u.model_dump() for u in manager.get_room_users(room_id)]
                logger.info(f"[WS] Broadcasting user_joined. Total users: {len(users_data)}")
                await manager.broadcast({
                    "type": "user_joined",
                    "user": user.model_dump(),
                    "users": users_data
                }, room_id)
                continue

            # --- Handle END_SESSION message (host only) ---
            # SECURITY: Validate using backend-tracked userId and role
            if message_type == "end_session":
                # SECURITY: Use backend-assigned userId, not client-provided
                if not manager.can_end_session(room_id, assigned_user_id):
                    logger.warning(
                        f"[WS] Unauthorized end_session attempt by userId={assigned_user_id}"
                    )
                    await websocket.send_json({
                        "type": "error",
                        "error": "Only the host can end the session"
                    })
                    continue

                logger.info(f"[WS] Host {assigned_user_id} ending session for room {room_id}")
                # Delete all files for this room
                # TODO: CLOUD_BACKUP - Consider backing up files before deletion
                try:
                    file_service = FileStorageService.get_instance()
                    deleted_count = file_service.delete_room_files(room_id)
                    logger.info(f"[WS] Deleted {deleted_count} files for room {room_id}")
                except Exception as e:
                    logger.error(f"[WS] Failed to delete files for room {room_id}: {e}")

                await manager.broadcast({
                    "type": "session_ended",
                    "message": "Host has ended the chat session"
                }, room_id)

                # Clear all room data
                manager.clear_room(room_id)
                continue

            # --- Handle TRANSFER_LEAD message (host or current lead only) ---
            # SECURITY: Only host or current lead can transfer lead
            if message_type == "transfer_lead":
                target_user_id = data.get("targetUserId")
                if not manager.can_configure(room_id, assigned_user_id):
                    logger.warning(
                        f"[WS] Unauthorized transfer_lead attempt by userId={assigned_user_id}"
                    )
                    await websocket.send_json({
                        "type": "error",
                        "error": "Only the host or current lead can transfer lead"
                    })
                    continue

                if not target_user_id or not manager.transfer_lead(room_id, target_user_id):
                    await websocket.send_json({
                        "type": "error",
                        "error": "Invalid target user for lead transfer"
                    })
                    continue

                logger.info(f"[WS] Lead transferred to {target_user_id} in room {room_id}")
                await manager.broadcast({
                    "type": "lead_changed",
                    "leadId": target_user_id
                }, room_id)
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
                            "isTyping": data.get("isTyping", True)
                        },
                        room_id,
                        exclude_websocket=websocket
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
                    await manager.broadcast({
                        "type": "read_receipt",
                        "messageId": message_id,
                        "readBy": list(read_by)
                    }, room_id)
                continue

            # --- Handle FILE message (broadcast file upload notification) ---
            # SECURITY: Use backend-assigned userId and role
            if message_type == "file":
                message_id = data.get("id", "")

                # Deduplication check
                if manager.is_duplicate_message(room_id, message_id):
                    logger.debug(f"[WS] Duplicate file message ignored: {message_id}")
                    continue

                user_info = manager.get_user(room_id, assigned_user_id)
                display_name = user_info.displayName if user_info else data.get("displayName", "")

                # Broadcast file message to all clients
                file_message = {
                    "type": "file",
                    "id": message_id,
                    "roomId": room_id,
                    "userId": assigned_user_id,  # SECURITY: Use backend-assigned ID
                    "displayName": display_name,
                    "role": assigned_role,  # SECURITY: Use backend-assigned role
                    "fileId": data.get("fileId", ""),
                    "originalFilename": data.get("originalFilename", ""),
                    "fileType": data.get("fileType", "other"),
                    "mimeType": data.get("mimeType", ""),
                    "sizeBytes": data.get("sizeBytes", 0),
                    "downloadUrl": data.get("downloadUrl", ""),
                    "caption": data.get("caption"),
                    "ts": data.get("ts", 0)
                }

                logger.info(f"[WS] Broadcasting file message: {file_message.get('originalFilename')}")
                await manager.broadcast(file_message, room_id)
                continue

            # --- Handle CODE SNIPPET message ---
            # SECURITY: Use backend-assigned userId and role
            if message_type == "code_snippet":
                message_id = str(uuid.uuid4())
                user_info = manager.get_user(room_id, assigned_user_id)
                display_name = user_info.displayName if user_info else data.get("displayName", "")
                code_snippet = data.get("codeSnippet", {})

                # Build code snippet message
                snippet_message = {
                    "type": "code_snippet",
                    "id": message_id,
                    "roomId": room_id,
                    "userId": assigned_user_id,  # SECURITY: Use backend-assigned ID
                    "displayName": display_name,
                    "role": assigned_role,  # SECURITY: Use backend-assigned role
                    "content": data.get("content", ""),  # Optional comment
                    "codeSnippet": {
                        "filename": code_snippet.get("filename", ""),
                        "relativePath": code_snippet.get("relativePath", ""),
                        "language": code_snippet.get("language", ""),
                        "startLine": code_snippet.get("startLine", 1),
                        "endLine": code_snippet.get("endLine", 1),
                        "code": code_snippet.get("code", "")
                    },
                    "ts": time.time()
                }

                logger.info(f"[WS] Broadcasting code snippet: {code_snippet.get('relativePath', 'unknown')} lines {code_snippet.get('startLine')}-{code_snippet.get('endLine')}")
                await manager.broadcast(snippet_message, room_id)
                continue

            # --- Handle STACK TRACE message ---
            # SECURITY: Use backend-assigned userId and role
            if message_type == "stack_trace":
                message_id = str(uuid.uuid4())
                user_info = manager.get_user(room_id, assigned_user_id)
                display_name = user_info.displayName if user_info else data.get("displayName", "")

                raw_text = data.get("rawText", "")
                # Parse on backend so server-stored messages are structured
                parsed = data.get("parsed") or parse_stack_trace(raw_text).to_dict()

                stack_trace_message = {
                    "type": "stack_trace",
                    "id": message_id,
                    "roomId": room_id,
                    "userId": assigned_user_id,
                    "displayName": display_name,
                    "role": assigned_role,
                    "content": data.get("content", ""),
                    "rawText": raw_text,
                    "stackTrace": parsed,
                    "ts": time.time(),
                }

                logger.info(
                    f"[WS] Broadcasting stack_trace from {assigned_user_id}: "
                    f"{parsed.get('errorType', 'unknown')} – {len(parsed.get('frames', []))} frames"
                )
                await manager.broadcast(stack_trace_message, room_id)
                continue

            # --- Handle TEST FAILURE message ---
            # SECURITY: Use backend-assigned userId and role
            if message_type == "test_failure":
                message_id = str(uuid.uuid4())
                user_info = manager.get_user(room_id, assigned_user_id)
                display_name = user_info.displayName if user_info else data.get("displayName", "")

                test_failure = data.get("testFailure", {})
                total_failed = test_failure.get("totalFailed", 0)

                test_failure_message = {
                    "type": "test_failure",
                    "id": message_id,
                    "roomId": room_id,
                    "userId": assigned_user_id,
                    "displayName": display_name,
                    "role": assigned_role,
                    "content": data.get("content", f"{total_failed} test(s) failed"),
                    "testFailure": test_failure,
                    "ts": time.time(),
                }

                logger.info(
                    f"[WS] Broadcasting test_failure from {assigned_user_id}: "
                    f"{total_failed} failures ({test_failure.get('framework', 'unknown')} framework)"
                )
                await manager.broadcast(test_failure_message, room_id)
                continue

            # --- Handle regular CHAT message ---
            # SECURITY: Use backend-assigned userId and role for all messages
            content = data.get("content", "")

            # Validate: content is required and cannot be empty for regular messages
            if not content or not content.strip():
                await websocket.send_json({
                    "type": "error",
                    "error": "Invalid message format: content is required"
                })
                continue

            logger.info(f"[WS] CHAT message from backend-assigned userId={assigned_user_id}: {content[:50]}")

            # Use registered display name if available
            user_info = manager.get_user(room_id, assigned_user_id)
            display_name = user_info.displayName if user_info else data.get("displayName", "")

            # Create and store the full message
            # SECURITY: Use backend-assigned userId and role, ignore client-provided values
            full_message = ChatMessage(
                roomId=room_id,
                userId=assigned_user_id,
                displayName=display_name,
                role=assigned_role,
                content=content
            )
            manager.add_message(room_id, full_message)

            # Broadcast to all clients in the room
            logger.info(f"[WS] Broadcasting message to {manager.get_room_size(room_id)} connections")
            await manager.broadcast(
                {"type": "message", **full_message.model_dump()},
                room_id
            )

    except WebSocketDisconnect:
        # Clean up and notify others
        disconnected_user, lead_reverted = manager.disconnect(websocket, room_id)

        if disconnected_user:
            users_data = [u.model_dump() for u in manager.get_room_users(room_id)]
            await manager.broadcast({
                "type": "user_left",
                "user": disconnected_user.model_dump(),
                "users": users_data
            }, room_id)

            # If lead reverted to host on disconnect, broadcast lead_changed
            if lead_reverted:
                new_lead_id = manager.get_lead_id(room_id)
                logger.info(f"[WS] Lead reverted to host {new_lead_id} in room {room_id}")
                await manager.broadcast({
                    "type": "lead_changed",
                    "leadId": new_lead_id
                }, room_id)

