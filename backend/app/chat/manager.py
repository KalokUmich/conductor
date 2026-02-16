"""WebSocket connection manager for real-time chat rooms.

This module manages WebSocket connections, user sessions, and message history
for Conductor's real-time chat functionality. It supports multiple rooms with
independent message histories and user lists.

Key features:
    - Multiple chat rooms with isolated state
    - Automatic guest numbering (Guest 1, Guest 2, etc.)
    - Message history persistence (in-memory, per room)
    - Avatar color assignment
    - Broadcast messaging to all room participants
    - Concurrent message broadcasting with asyncio.gather()
    - Automatic dead connection cleanup
    - Message deduplication with LRU cache
    - Paginated message history
    - Read receipts tracking

Thread Safety:
    This implementation is designed for async/await usage with a single event loop.
    It is NOT thread-safe for concurrent access from multiple threads.

Performance Notes:
    - Broadcasting uses asyncio.gather() for concurrent message delivery
    - Failed connections are automatically removed during broadcast
    - Uvicorn handles ping/pong at the protocol level (default 20s interval)
    - Message deduplication uses OrderedDict as LRU cache (O(1) lookup)
"""
import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from enum import Enum
from typing import Dict, List, Optional, Set, Tuple

from fastapi import WebSocket
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# =============================================================================
# Constants
# =============================================================================

# Maximum number of message IDs to track for deduplication
MESSAGE_DEDUP_CACHE_SIZE = 10000

# Default page size for message history pagination
DEFAULT_PAGE_SIZE = 50

# Maximum page size to prevent abuse
MAX_PAGE_SIZE = 100


# =============================================================================
# Data Models
# =============================================================================


class UserRole(str, Enum):
    """User role in a chat room.

    Attributes:
        HOST: The lead user who can end sessions and use AI features.
        GUEST: A participant who joined an existing room (limited permissions).
        ENGINEER: Legacy alias for guest (kept for backwards compatibility).
        AI: AI assistant that generates summaries and code prompts.
    """
    HOST = "host"
    GUEST = "guest"
    ENGINEER = "engineer"  # Legacy alias
    AI = "ai"  # AI assistant


class IdentitySource(str, Enum):
    """How a user's identity was established.

    Attributes:
        SSO: Verified via SSO (e.g., Azure AD, Okta).
        NAMED: User provided a custom display name.
        ANONYMOUS: Backend auto-generated name (Guest N).
    """
    SSO = "sso"
    NAMED = "named"
    ANONYMOUS = "anonymous"


class MessageType(str, Enum):
    """Type of chat message.

    Attributes:
        MESSAGE: Regular text message.
        CODE_SNIPPET: Code snippet with file info.
        FILE: File attachment.
        AI_SUMMARY: AI-generated decision summary.
        AI_CODE_PROMPT: AI-generated code prompt for code agents.
    """
    MESSAGE = "message"
    CODE_SNIPPET = "code_snippet"
    FILE = "file"
    AI_SUMMARY = "ai_summary"
    AI_CODE_PROMPT = "ai_code_prompt"


class RoomUser(BaseModel):
    """User information stored in a chat room.

    Attributes:
        userId: Unique identifier for this user (UUID from extension).
        displayName: Human-readable name shown in the chat UI.
        role: User's role (host or engineer).
        avatarColor: CSS color name for the user's avatar background.
    """
    userId: str = Field(..., description="Unique user ID")
    displayName: str = Field(..., description="Display name shown in UI")
    role: UserRole = Field(..., description="User role (host or engineer)")
    avatarColor: str = Field(default="purple", description="Avatar background color")
    identitySource: IdentitySource = Field(
        default=IdentitySource.ANONYMOUS,
        description="How identity was established (sso, named, anonymous)"
    )


class ChatMessage(BaseModel):
    """Complete chat message with all metadata.

    This is the full message structure stored in history and broadcast to clients.

    Attributes:
        id: Unique message identifier (auto-generated UUID).
        type: Message type (message, code_snippet, file, ai_summary, ai_code_prompt).
        roomId: Room this message belongs to.
        userId: Sender's user ID.
        displayName: Sender's display name.
        role: Sender's role.
        content: Message text content.
        ts: Unix timestamp (seconds since epoch).
        aiData: Optional AI-specific data (for ai_summary and ai_code_prompt types).
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique message ID"
    )
    type: MessageType = Field(
        default=MessageType.MESSAGE,
        description="Message type"
    )
    roomId: str = Field(..., description="Room ID this message belongs to")
    userId: str = Field(..., description="User ID of the sender")
    displayName: str = Field(default="", description="Display name of the sender")
    role: UserRole = Field(..., description="Role of the sender (host or engineer)")
    content: str = Field(..., description="Message content")
    ts: float = Field(
        default_factory=time.time,
        description="Timestamp in seconds since epoch"
    )
    # AI-specific data (only for ai_summary and ai_code_prompt types)
    aiData: Optional[dict] = Field(
        default=None,
        description="AI-specific data (summary details or code prompt)"
    )


class ChatMessageInput(BaseModel):
    """Input schema for sending a chat message from client.

    Clients send this lightweight structure. The server adds id, roomId, and ts.

    Attributes:
        userId: Sender's user ID.
        displayName: Sender's display name (optional if already registered).
        role: Sender's role.
        content: Message text content.
    """
    userId: str = Field(..., description="User ID of the sender")
    displayName: str = Field(default="", description="Display name of the sender")
    role: UserRole = Field(..., description="Role of the sender (host or engineer)")
    content: str = Field(..., description="Message content")


# Avatar color palette for guests (Host always gets "amber")
AVATAR_COLORS = [
    "purple", "blue", "green", "orange", "pink", "cyan", "yellow", "red"
]


# =============================================================================
# Connection Manager
# =============================================================================


class ConnectionManager:
    """Manages WebSocket connections and message history for multiple chat rooms.

    This class maintains the state for all active chat rooms, including:
    - Active WebSocket connections per room
    - Message history per room (in-memory, append-only)
    - User registrations per room
    - Guest numbering counters
    - Room host tracking (for permission validation)

    Security Model:
        - Backend assigns userId on WebSocket connection (not client-provided)
        - Backend determines role: first user in room = host, others = guest
        - Backend validates permissions for sensitive operations (end_session, etc.)

    Note:
        This is a singleton-style global instance. All WebSocket handlers
        share the same ConnectionManager to maintain consistent state.
    """

    def __init__(self) -> None:
        """Initialize empty connection manager."""
        # room_id -> list of active WebSocket connections
        self.active_connections: Dict[str, List[WebSocket]] = {}

        # room_id -> list of messages (append-only history)
        self.message_history: Dict[str, List[ChatMessage]] = {}

        # room_id -> {userId -> RoomUser}
        self.room_users: Dict[str, Dict[str, RoomUser]] = {}

        # room_id -> guest counter (for "Guest 1", "Guest 2" naming)
        self.guest_counters: Dict[str, int] = {}

        # websocket -> (room_id, userId) for disconnect handling
        self.websocket_to_user: Dict[WebSocket, Tuple[str, str]] = {}

        # Message deduplication: room_id -> OrderedDict of message IDs (LRU cache)
        self.seen_message_ids: Dict[str, OrderedDict] = {}

        # Read receipts: room_id -> {message_id -> set of user_ids who read it}
        self.message_read_by: Dict[str, Dict[str, Set[str]]] = {}

        # SECURITY: room_id -> host_user_id (first user to join becomes host)
        self.room_hosts: Dict[str, str] = {}

        # Lead tracking: room_id -> lead_user_id (initially the host)
        self.room_leads: Dict[str, str] = {}

        # Room settings: room_id -> settings dict (e.g., {"code_style": "..."})
        self.room_settings: Dict[str, dict] = {}

    async def connect(
        self, websocket: WebSocket, room_id: str
    ) -> Tuple[str, str, List[ChatMessage]]:
        """Accept a WebSocket connection, assign userId/role, and add to room.

        SECURITY: This method is responsible for:
        1. Generating a unique userId for the connection (not client-provided)
        2. Determining role: first user in room = host, others = guest
        3. Storing the host userId for permission validation

        Args:
            websocket: The WebSocket connection to accept.
            room_id: The room ID to join.

        Returns:
            Tuple of (userId, role, message_history):
            - userId: Backend-generated unique identifier
            - role: "host" for first user, "guest" for others
            - message_history: List of existing messages in the room
        """
        await websocket.accept()

        # SECURITY: Generate userId on backend (never trust client-provided IDs)
        user_id = str(uuid.uuid4())

        # Initialize room data structures if needed (first connection to room)
        if room_id not in self.active_connections:
            self.active_connections[room_id] = []
        if room_id not in self.message_history:
            self.message_history[room_id] = []
        if room_id not in self.room_users:
            self.room_users[room_id] = {}
        if room_id not in self.guest_counters:
            self.guest_counters[room_id] = 0

        # SECURITY: First user to connect becomes host and initial lead
        if room_id not in self.room_hosts:
            self.room_hosts[room_id] = user_id
            self.room_leads[room_id] = user_id
            role = "host"
            logger.info(f"[Manager] User {user_id} is HOST and LEAD of room {room_id}")
        else:
            role = "guest"
            logger.info(f"[Manager] User {user_id} is GUEST in room {room_id}")

        self.active_connections[room_id].append(websocket)

        return (user_id, role, self.message_history[room_id])

    def register_user(
        self,
        websocket: WebSocket,
        room_id: str,
        user_id: str,
        display_name: str,
        role: UserRole,
        identity_source: str = "anonymous"
    ) -> RoomUser:
        """Register a user in the room after connection.

        This method is called when a client sends a "join" message.
        It assigns display names to guests and avatar colors.

        Args:
            websocket: The user's WebSocket connection.
            room_id: The room to register in.
            user_id: Unique user identifier.
            display_name: Preferred display name (may be auto-generated).
            role: User role (host or engineer).
            identity_source: How identity was established (sso, named, anonymous).

        Returns:
            RoomUser object with assigned display name and avatar color.
        """
        # Track whether backend will auto-generate the display name
        auto_named = False

        # Auto-generate display name for guests without a proper name
        if not display_name or display_name.startswith("guest-"):
            auto_named = True
            if role == UserRole.HOST:
                display_name = "Host"
            else:
                self.guest_counters[room_id] += 1
                display_name = f"Guest {self.guest_counters[room_id]}"

        # Resolve identity source enum from string (fallback to ANONYMOUS)
        try:
            resolved_source = IdentitySource(identity_source)
        except ValueError:
            resolved_source = IdentitySource.ANONYMOUS

        # Force ANONYMOUS if backend auto-named the user
        if auto_named:
            resolved_source = IdentitySource.ANONYMOUS

        # Assign avatar color (Host gets amber, guests get rotating colors)
        color_index = len(self.room_users.get(room_id, {})) % len(AVATAR_COLORS)
        avatar_color = "amber" if role == UserRole.HOST else AVATAR_COLORS[color_index]

        user = RoomUser(
            userId=user_id,
            displayName=display_name,
            role=role,
            avatarColor=avatar_color,
            identitySource=resolved_source
        )

        # Store user in room and create websocket mapping
        if room_id not in self.room_users:
            self.room_users[room_id] = {}
        self.room_users[room_id][user_id] = user
        self.websocket_to_user[websocket] = (room_id, user_id)

        return user

    def get_user(self, room_id: str, user_id: str) -> Optional[RoomUser]:
        """Get a user's info from the room.

        Args:
            room_id: Room to look in.
            user_id: User ID to find.

        Returns:
            RoomUser if found, None otherwise.
        """
        return self.room_users.get(room_id, {}).get(user_id)

    def get_room_users(self, room_id: str) -> List[RoomUser]:
        """Get all registered users in a room.

        Args:
            room_id: Room ID to query.

        Returns:
            List of all RoomUser objects in the room.
        """
        return list(self.room_users.get(room_id, {}).values())

    def disconnect(self, websocket: WebSocket, room_id: str) -> Tuple[Optional[RoomUser], bool]:
        """Remove a WebSocket connection and its user from a room.

        If the disconnecting user is the lead (but not the host), lead reverts
        to the host automatically.

        Args:
            websocket: The disconnecting WebSocket.
            room_id: The room to disconnect from.

        Returns:
            Tuple of (disconnected_user, lead_reverted):
            - disconnected_user: The disconnected RoomUser if registered, None otherwise.
            - lead_reverted: True if lead was reverted to host due to this disconnect.
        """
        disconnected_user = None
        lead_reverted = False

        # Remove from active connections
        if room_id in self.active_connections:
            if websocket in self.active_connections[room_id]:
                self.active_connections[room_id].remove(websocket)

        # Remove user registration
        if websocket in self.websocket_to_user:
            ws_room_id, user_id = self.websocket_to_user[websocket]
            if ws_room_id == room_id and room_id in self.room_users:
                disconnected_user = self.room_users[room_id].pop(user_id, None)

            # If the disconnecting user was the lead, revert to host
            if disconnected_user and self.room_leads.get(room_id) == user_id:
                host_id = self.room_hosts.get(room_id)
                if host_id and host_id != user_id and host_id in self.room_users.get(room_id, {}):
                    self.room_leads[room_id] = host_id
                    lead_reverted = True
                    logger.info(f"[Manager] Lead reverted to host {host_id} in room {room_id}")

            del self.websocket_to_user[websocket]

        return (disconnected_user, lead_reverted)

    def add_message(self, room_id: str, message: ChatMessage) -> ChatMessage:
        """Add a message to the room's history.

        Args:
            room_id: Room to add message to.
            message: The ChatMessage to store.

        Returns:
            The same message (for chaining).
        """
        if room_id not in self.message_history:
            self.message_history[room_id] = []
        self.message_history[room_id].append(message)
        return message

    async def broadcast(self, message: dict, room_id: str) -> None:
        """Broadcast a message to all connections in a room concurrently.

        Uses asyncio.gather() for concurrent message delivery, which is
        significantly faster than sequential iteration for rooms with
        many connections.

        This method safely handles disconnected clients by removing them
        from the connection list if sending fails.

        Args:
            message: JSON-serializable message to broadcast.
            room_id: Room to broadcast to.
        """
        if room_id not in self.active_connections:
            return

        connections = self.active_connections[room_id].copy()
        if not connections:
            return

        # Send to all connections concurrently
        results = await asyncio.gather(
            *[self._safe_send(conn, message) for conn in connections],
            return_exceptions=True
        )

        # Remove failed connections
        failed_connections = [
            conn for conn, success in zip(connections, results)
            if success is False
        ]
        self._cleanup_connections(room_id, failed_connections)

    async def broadcast_except(
        self, message: dict, room_id: str, exclude_websocket: WebSocket
    ) -> None:
        """Broadcast a message to all connections except one concurrently.

        Useful for typing indicators where sender shouldn't see their own.
        Uses asyncio.gather() for concurrent message delivery.

        Args:
            message: JSON-serializable message to broadcast.
            room_id: Room to broadcast to.
            exclude_websocket: WebSocket connection to exclude from broadcast.
        """
        if room_id not in self.active_connections:
            return

        connections = [
            conn for conn in self.active_connections[room_id]
            if conn != exclude_websocket
        ]
        if not connections:
            return

        # Send to all connections concurrently (except excluded)
        results = await asyncio.gather(
            *[self._safe_send(conn, message) for conn in connections],
            return_exceptions=True
        )

        # Remove failed connections
        failed_connections = [
            conn for conn, success in zip(connections, results)
            if success is False
        ]
        self._cleanup_connections(room_id, failed_connections)

    async def _safe_send(self, connection: WebSocket, message: dict) -> bool:
        """Send a message to a WebSocket connection with error handling.

        Args:
            connection: The WebSocket to send to.
            message: JSON-serializable message to send.

        Returns:
            True if successful, False if connection failed.
        """
        try:
            await connection.send_json(message)
            return True
        except Exception as e:
            logger.debug(f"Failed to send to connection: {e}")
            return False

    def _cleanup_connections(
        self, room_id: str, failed_connections: List[WebSocket]
    ) -> None:
        """Remove failed connections from a room.

        Args:
            room_id: Room to clean up.
            failed_connections: List of WebSocket connections to remove.
        """
        if not failed_connections or room_id not in self.active_connections:
            return

        for conn in failed_connections:
            if conn in self.active_connections[room_id]:
                self.active_connections[room_id].remove(conn)
                logger.debug(f"Removed dead connection from room {room_id}")

    def get_room_size(self, room_id: str) -> int:
        """Get the number of active connections in a room."""
        return len(self.active_connections.get(room_id, []))

    def get_message_count(self, room_id: str) -> int:
        """Get the number of messages in a room's history."""
        return len(self.message_history.get(room_id, []))

    def get_history(self, room_id: str) -> List[ChatMessage]:
        """Get the message history for a room."""
        return self.message_history.get(room_id, [])

    def clear_room(self, room_id: str) -> None:
        """Clear all data for a room (used when host ends session).

        This removes all connections, message history, user registrations,
        guest counters, and host tracking for the specified room.

        Args:
            room_id: Room to clear.
        """
        # Remove all room data
        self.active_connections.pop(room_id, None)
        self.message_history.pop(room_id, None)
        self.room_users.pop(room_id, None)
        self.guest_counters.pop(room_id, None)
        self.seen_message_ids.pop(room_id, None)
        self.message_read_by.pop(room_id, None)
        self.room_hosts.pop(room_id, None)  # SECURITY: Clear host tracking
        self.room_leads.pop(room_id, None)  # Clear lead tracking
        self.room_settings.pop(room_id, None)

        # Clean up websocket-to-user mappings for this room
        to_remove = [
            ws for ws, (rid, _) in self.websocket_to_user.items()
            if rid == room_id
        ]
        for ws in to_remove:
            del self.websocket_to_user[ws]

    # =========================================================================
    # Permission Validation (Security)
    # =========================================================================

    def is_host(self, room_id: str, user_id: str) -> bool:
        """Check if a user is the host of a room.

        SECURITY: Used for permission validation before sensitive operations.

        Args:
            room_id: The room ID.
            user_id: The user ID to check.

        Returns:
            True if user is the host, False otherwise.
        """
        return self.room_hosts.get(room_id) == user_id

    def get_host_id(self, room_id: str) -> Optional[str]:
        """Get the host user ID for a room.

        Args:
            room_id: The room ID.

        Returns:
            The host's user ID, or None if room doesn't exist.
        """
        return self.room_hosts.get(room_id)

    # =========================================================================
    # Lead Management
    # =========================================================================

    def get_lead_id(self, room_id: str) -> Optional[str]:
        """Get the current lead user ID for a room.

        Args:
            room_id: The room ID.

        Returns:
            The lead's user ID, or None if room doesn't exist.
        """
        return self.room_leads.get(room_id)

    def is_lead(self, room_id: str, user_id: str) -> bool:
        """Check if a user is the lead of a room.

        Args:
            room_id: The room ID.
            user_id: The user ID to check.

        Returns:
            True if user is the lead, False otherwise.
        """
        return self.room_leads.get(room_id) == user_id

    def transfer_lead(self, room_id: str, new_lead_id: str) -> bool:
        """Transfer lead role to another user in the room.

        Args:
            room_id: The room ID.
            new_lead_id: The user ID to become the new lead.

        Returns:
            True if transfer succeeded, False if target user is not in the room.
        """
        if new_lead_id not in self.room_users.get(room_id, {}):
            return False
        self.room_leads[room_id] = new_lead_id
        logger.info(f"[Manager] Lead transferred to {new_lead_id} in room {room_id}")
        return True

    def can_use_ai(self, room_id: str, user_id: str) -> bool:
        """Check if a user can use AI features (summary, code changes).

        Only the lead can use AI features.

        Args:
            room_id: The room ID.
            user_id: The user ID to check.

        Returns:
            True if user is the lead, False otherwise.
        """
        return self.is_lead(room_id, user_id)

    def can_configure(self, room_id: str, user_id: str) -> bool:
        """Check if a user can configure AI settings and room settings.

        The host always retains configuration access. The lead also gets it.

        Args:
            room_id: The room ID.
            user_id: The user ID to check.

        Returns:
            True if user is host or lead, False otherwise.
        """
        return self.is_host(room_id, user_id) or self.is_lead(room_id, user_id)

    def can_end_session(self, room_id: str, user_id: str) -> bool:
        """Check if a user has permission to end a session.

        SECURITY: Only the host can end sessions.

        Args:
            room_id: The room ID.
            user_id: The user ID attempting the operation.

        Returns:
            True if user can end session, False otherwise.
        """
        return self.is_host(room_id, user_id)

    # =========================================================================
    # Room Settings
    # =========================================================================

    def get_room_settings(self, room_id: str) -> dict:
        """Get settings for a room.

        Args:
            room_id: The room ID.

        Returns:
            Settings dict. Returns default settings if none stored.
        """
        return self.room_settings.get(room_id, {"code_style": ""})

    def update_room_settings(self, room_id: str, settings: dict) -> dict:
        """Update settings for a room.

        Args:
            room_id: The room ID.
            settings: Dict of settings to merge into existing settings.

        Returns:
            Updated settings dict.
        """
        current = self.room_settings.get(room_id, {"code_style": ""})
        current.update(settings)
        self.room_settings[room_id] = current
        return current

    # =========================================================================
    # Message Deduplication
    # =========================================================================

    def is_duplicate_message(self, room_id: str, message_id: str) -> bool:
        """Check if a message ID has been seen before (for deduplication).

        Uses an LRU cache to track seen message IDs. If the message is new,
        it is added to the cache.

        Args:
            room_id: The room ID.
            message_id: The message ID to check.

        Returns:
            True if the message has been seen before, False if it's new.
        """
        if not message_id:
            return False  # No ID means we can't dedupe

        if room_id not in self.seen_message_ids:
            self.seen_message_ids[room_id] = OrderedDict()

        cache = self.seen_message_ids[room_id]

        if message_id in cache:
            # Move to end (most recently used)
            cache.move_to_end(message_id)
            return True

        # Add new message ID
        cache[message_id] = True

        # Evict oldest if cache is full
        while len(cache) > MESSAGE_DEDUP_CACHE_SIZE:
            cache.popitem(last=False)

        return False

    # =========================================================================
    # Message Pagination
    # =========================================================================

    def get_messages_since(
        self, room_id: str, since_ts: float
    ) -> List[ChatMessage]:
        """Get messages newer than the given timestamp (for reconnection).

        Args:
            room_id: The room ID.
            since_ts: Unix timestamp (seconds). Returns messages with ts > since_ts.

        Returns:
            List of messages newer than since_ts.
        """
        messages = self.message_history.get(room_id, [])
        return [msg for msg in messages if msg.ts > since_ts]

    def get_paginated_history(
        self,
        room_id: str,
        before_ts: Optional[float] = None,
        limit: int = DEFAULT_PAGE_SIZE
    ) -> List[ChatMessage]:
        """Get paginated message history (for lazy loading).

        Returns messages older than the cursor, limited to `limit` messages.
        Messages are returned in chronological order (oldest first).

        Args:
            room_id: The room ID.
            before_ts: Unix timestamp cursor. Returns messages with ts < before_ts.
                       If None, returns the most recent messages.
            limit: Maximum number of messages to return.

        Returns:
            List of messages, oldest first.
        """
        limit = min(limit, MAX_PAGE_SIZE)  # Prevent abuse
        messages = self.message_history.get(room_id, [])

        if before_ts is not None:
            # Filter messages before the cursor
            messages = [msg for msg in messages if msg.ts < before_ts]

        # Return last N messages (most recent before cursor)
        return messages[-limit:] if messages else []

    # =========================================================================
    # Read Receipts
    # =========================================================================

    def mark_message_read(
        self, room_id: str, message_id: str, user_id: str
    ) -> Set[str]:
        """Mark a message as read by a user.

        Args:
            room_id: The room ID.
            message_id: The message ID that was read.
            user_id: The user ID who read the message.

        Returns:
            Set of all user IDs who have read this message.
        """
        if room_id not in self.message_read_by:
            self.message_read_by[room_id] = {}

        if message_id not in self.message_read_by[room_id]:
            self.message_read_by[room_id][message_id] = set()

        self.message_read_by[room_id][message_id].add(user_id)
        return self.message_read_by[room_id][message_id]

    def get_read_by(self, room_id: str, message_id: str) -> Set[str]:
        """Get the set of user IDs who have read a message.

        Args:
            room_id: The room ID.
            message_id: The message ID.

        Returns:
            Set of user IDs who have read the message.
        """
        return self.message_read_by.get(room_id, {}).get(message_id, set())


# Global singleton instance used by all WebSocket handlers
manager = ConnectionManager()

