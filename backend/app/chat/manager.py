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

Thread Safety:
    This implementation is designed for async/await usage with a single event loop.
    It is NOT thread-safe for concurrent access from multiple threads.
"""
import time
import uuid
from enum import Enum
from typing import Dict, List, Optional, Tuple

from fastapi import WebSocket
from pydantic import BaseModel, Field


# =============================================================================
# Data Models
# =============================================================================


class UserRole(str, Enum):
    """User role in a chat room.

    Attributes:
        HOST: The lead user who can end sessions and use AI features.
        ENGINEER: A participant who can chat but has limited permissions.
    """
    HOST = "host"
    ENGINEER = "engineer"


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


class ChatMessage(BaseModel):
    """Complete chat message with all metadata.

    This is the full message structure stored in history and broadcast to clients.

    Attributes:
        id: Unique message identifier (auto-generated UUID).
        roomId: Room this message belongs to.
        userId: Sender's user ID.
        displayName: Sender's display name.
        role: Sender's role.
        content: Message text content.
        ts: Unix timestamp (seconds since epoch).
    """
    id: str = Field(
        default_factory=lambda: str(uuid.uuid4()),
        description="Unique message ID"
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

    async def connect(self, websocket: WebSocket, room_id: str) -> List[ChatMessage]:
        """Accept a WebSocket connection and add it to a room.

        This method accepts the WebSocket handshake and initializes
        room data structures if this is the first connection to the room.

        Args:
            websocket: The WebSocket connection to accept.
            room_id: The room ID to join.

        Returns:
            List of existing messages in the room (may be empty).
        """
        await websocket.accept()

        # Initialize room data structures if needed (first connection to room)
        if room_id not in self.active_connections:
            self.active_connections[room_id] = []
        if room_id not in self.message_history:
            self.message_history[room_id] = []
        if room_id not in self.room_users:
            self.room_users[room_id] = {}
        if room_id not in self.guest_counters:
            self.guest_counters[room_id] = 0

        self.active_connections[room_id].append(websocket)

        return self.message_history[room_id]

    def register_user(
        self,
        websocket: WebSocket,
        room_id: str,
        user_id: str,
        display_name: str,
        role: UserRole
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

        Returns:
            RoomUser object with assigned display name and avatar color.
        """
        # Auto-generate display name for guests without a proper name
        if not display_name or display_name.startswith("guest-"):
            if role == UserRole.HOST:
                display_name = "Host"
            else:
                self.guest_counters[room_id] += 1
                display_name = f"Guest {self.guest_counters[room_id]}"

        # Assign avatar color (Host gets amber, guests get rotating colors)
        color_index = len(self.room_users.get(room_id, {})) % len(AVATAR_COLORS)
        avatar_color = "amber" if role == UserRole.HOST else AVATAR_COLORS[color_index]

        user = RoomUser(
            userId=user_id,
            displayName=display_name,
            role=role,
            avatarColor=avatar_color
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

    def disconnect(self, websocket: WebSocket, room_id: str) -> Optional[RoomUser]:
        """Remove a WebSocket connection and its user from a room.

        Args:
            websocket: The disconnecting WebSocket.
            room_id: The room to disconnect from.

        Returns:
            The disconnected RoomUser if registered, None otherwise.
        """
        disconnected_user = None

        # Remove from active connections
        if room_id in self.active_connections:
            if websocket in self.active_connections[room_id]:
                self.active_connections[room_id].remove(websocket)

        # Remove user registration
        if websocket in self.websocket_to_user:
            ws_room_id, user_id = self.websocket_to_user[websocket]
            if ws_room_id == room_id and room_id in self.room_users:
                disconnected_user = self.room_users[room_id].pop(user_id, None)
            del self.websocket_to_user[websocket]

        return disconnected_user

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
        """Broadcast a message to all connections in a room.

        This method safely handles disconnected clients by removing them
        from the connection list if sending fails.

        Args:
            message: JSON-serializable message to broadcast.
            room_id: Room to broadcast to.
        """
        if room_id not in self.active_connections:
            return

        # Copy list to avoid modification during iteration
        connections = self.active_connections[room_id].copy()
        for connection in connections:
            try:
                await connection.send_json(message)
            except Exception:
                # Connection closed or errored, remove it
                if connection in self.active_connections[room_id]:
                    self.active_connections[room_id].remove(connection)

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
        and guest counters for the specified room.

        Args:
            room_id: Room to clear.
        """
        # Remove all room data
        self.active_connections.pop(room_id, None)
        self.message_history.pop(room_id, None)
        self.room_users.pop(room_id, None)
        self.guest_counters.pop(room_id, None)

        # Clean up websocket-to-user mappings for this room
        to_remove = [
            ws for ws, (rid, _) in self.websocket_to_user.items()
            if rid == room_id
        ]
        for ws in to_remove:
            del self.websocket_to_user[ws]


# Global singleton instance used by all WebSocket handlers
manager = ConnectionManager()

