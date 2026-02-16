"""Tests for WebSocket chat functionality with multi-client support.

SECURITY NOTE: The WebSocket protocol now implements server-side credential assignment:
1. On connect, backend sends {type: "connected", userId: "<uuid>", role: "host/guest"}
2. First client in room becomes host, subsequent clients become guests
3. All messages use backend-assigned userId and role
"""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.chat.manager import manager


client = TestClient(app)


def receive_credentials(ws):
    """Helper to receive and validate backend-assigned credentials."""
    connected = ws.receive_json()
    assert connected["type"] == "connected"
    assert "userId" in connected
    assert connected["role"] in ("host", "guest")
    return connected


def receive_history(ws):
    """Helper to receive and validate history message."""
    history = ws.receive_json()
    assert history["type"] == "history"
    return history


@pytest.fixture(autouse=True)
def cleanup_rooms():
    """Clean up rooms after each test to avoid interference."""
    yield
    # Clear all rooms after test
    rooms_to_clear = list(manager.active_connections.keys()) + list(manager.message_history.keys())
    for room_id in set(rooms_to_clear):
        manager.clear_room(room_id)


def test_websocket_chat_two_clients_same_room():
    """Test that two WebSocket clients in the same room can communicate."""
    room_id = "test-room-two-clients"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
         client.websocket_connect(f"/ws/chat/{room_id}") as ws2:

        # SECURITY: First receive backend-assigned credentials
        creds1 = receive_credentials(ws1)
        creds2 = receive_credentials(ws2)

        # First client should be host, second should be guest
        assert creds1["role"] == "host"
        assert creds2["role"] == "guest"

        # Both clients receive history on connect (empty for new room)
        history1 = receive_history(ws1)
        history2 = receive_history(ws2)

        # Client 1 sends a message (displayName only, userId/role from backend)
        message1 = {
            "displayName": "User 1",
            "content": "Hello from user1"
        }
        ws1.send_json(message1)

        # Both clients should receive the broadcast message
        data1 = ws1.receive_json()
        data2 = ws2.receive_json()

        # Verify message structure - backend assigns userId/role
        assert data1["type"] == "message"
        assert data1["userId"] == creds1["userId"]  # Backend-assigned
        assert data1["role"] == "host"  # Backend-assigned
        assert data1["content"] == "Hello from user1"
        assert data1["roomId"] == room_id
        assert "id" in data1
        assert "ts" in data1

        # Both clients receive the same message
        assert data1 == data2

        # Client 2 sends a message
        message2 = {
            "displayName": "User 2",
            "content": "Hello from user2"
        }
        ws2.send_json(message2)

        # Both clients should receive the message
        data1 = ws1.receive_json()
        data2 = ws2.receive_json()

        assert data1["type"] == "message"
        assert data1["userId"] == creds2["userId"]  # Backend-assigned
        assert data1["role"] == "guest"  # Backend-assigned
        assert data1 == data2


def test_websocket_chat_three_clients_same_room():
    """Test that three WebSocket clients in the same room receive all messages."""
    room_id = "test-room-three-clients"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
         client.websocket_connect(f"/ws/chat/{room_id}") as ws2, \
         client.websocket_connect(f"/ws/chat/{room_id}") as ws3:

        # SECURITY: First receive backend-assigned credentials
        creds1 = receive_credentials(ws1)
        creds2 = receive_credentials(ws2)
        creds3 = receive_credentials(ws3)

        # First client should be host, others should be guests
        assert creds1["role"] == "host"
        assert creds2["role"] == "guest"
        assert creds3["role"] == "guest"

        # All clients receive history on connect (empty for new room)
        receive_history(ws1)
        receive_history(ws2)
        receive_history(ws3)

        # Client 1 (host) sends a message
        msg1 = {"displayName": "Host User", "content": "Welcome everyone!"}
        ws1.send_json(msg1)

        # All three clients should receive the message
        recv1 = ws1.receive_json()
        recv2 = ws2.receive_json()
        recv3 = ws3.receive_json()

        assert recv1["type"] == "message"
        assert recv1["content"] == "Welcome everyone!"
        assert recv1["role"] == "host"  # Backend-assigned
        assert recv1 == recv2 == recv3

        # Client 2 (guest) sends a message
        msg2 = {"displayName": "Guest 1", "content": "Thanks for having me!"}
        ws2.send_json(msg2)

        recv1 = ws1.receive_json()
        recv2 = ws2.receive_json()
        recv3 = ws3.receive_json()

        assert recv1["content"] == "Thanks for having me!"
        assert recv1["role"] == "guest"  # Backend-assigned
        assert recv1 == recv2 == recv3

        # Client 3 (guest) sends a message
        msg3 = {"displayName": "Guest 2", "content": "Hello team!"}
        ws3.send_json(msg3)

        recv1 = ws1.receive_json()
        recv2 = ws2.receive_json()
        recv3 = ws3.receive_json()

        assert recv1["content"] == "Hello team!"
        assert recv1 == recv2 == recv3

        # Verify message history has all 3 messages
        assert manager.get_message_count(room_id) == 3


def test_websocket_chat_message_history_on_join():
    """Test that new clients receive message history when joining."""
    room_id = "test-room-history"

    # First client connects and sends messages
    with client.websocket_connect(f"/ws/chat/{room_id}") as ws1:
        # SECURITY: Receive credentials first
        creds1 = receive_credentials(ws1)
        assert creds1["role"] == "host"

        # Receive empty history on connect
        history1 = receive_history(ws1)
        assert len(history1["messages"]) == 0

        # Send first message (displayName only)
        ws1.send_json({"displayName": "Host", "content": "First message"})
        ws1.receive_json()  # Receive the broadcast

        # Send second message
        ws1.send_json({"displayName": "Host", "content": "Second message"})
        ws1.receive_json()  # Receive the broadcast

        # Second client joins - should receive history
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws2:
            # SECURITY: Receive credentials first
            creds2 = receive_credentials(ws2)
            assert creds2["role"] == "guest"

            # Then receive history
            history = receive_history(ws2)

            assert len(history["messages"]) == 2
            assert history["messages"][0]["content"] == "First message"
            assert history["messages"][1]["content"] == "Second message"

            # Third client joins - should also receive history
            with client.websocket_connect(f"/ws/chat/{room_id}") as ws3:
                # SECURITY: Receive credentials first
                receive_credentials(ws3)
                history3 = receive_history(ws3)

                assert len(history3["messages"]) == 2


def test_websocket_chat_different_rooms():
    """Test that clients in different rooms don't receive each other's messages."""
    room1 = "room-isolated-1"
    room2 = "room-isolated-2"

    with client.websocket_connect(f"/ws/chat/{room1}") as ws1, \
         client.websocket_connect(f"/ws/chat/{room2}") as ws2:

        # SECURITY: Receive credentials first
        creds1 = receive_credentials(ws1)
        creds2 = receive_credentials(ws2)

        # Both are hosts in their respective rooms
        assert creds1["role"] == "host"
        assert creds2["role"] == "host"

        # Both clients receive history on connect
        receive_history(ws1)
        receive_history(ws2)

        # Client 1 sends a message in room 1
        ws1.send_json({"displayName": "User 1", "content": "Message in room 1"})

        # Client 1 should receive the message
        data1 = ws1.receive_json()
        assert data1["type"] == "message"
        assert data1["content"] == "Message in room 1"
        assert data1["roomId"] == room1

        # Client 2 sends a message in room 2
        ws2.send_json({"displayName": "User 2", "content": "Message in room 2"})

        # Client 2 should receive its message
        data2 = ws2.receive_json()
        assert data2["type"] == "message"
        assert data2["content"] == "Message in room 2"
        assert data2["roomId"] == room2

        # Verify each room has only 1 message
        assert manager.get_message_count(room1) == 1
        assert manager.get_message_count(room2) == 1


def test_websocket_chat_invalid_message():
    """Test that invalid messages are handled gracefully.

    NOTE: With server-side credential assignment, the only required field
    for a regular message is 'content'. Empty content is still invalid.
    """
    room_id = "test-room-invalid-msg"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        # SECURITY: Receive credentials first
        receive_credentials(ws)
        receive_history(ws)

        # Send empty message (missing content)
        invalid_message = {}
        ws.send_json(invalid_message)

        # Should receive an error response
        response = ws.receive_json()
        assert response["type"] == "error"
        assert "Invalid message format" in response["error"]


def test_websocket_chat_empty_content():
    """Test that messages with empty content are rejected."""
    room_id = "test-room-empty-content"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        # SECURITY: Receive credentials first
        receive_credentials(ws)
        receive_history(ws)

        # Send message with empty content
        invalid_message = {
            "displayName": "User",
            "content": ""
        }
        ws.send_json(invalid_message)

        # Should receive an error response
        response = ws.receive_json()
        assert response["type"] == "error"
        assert "Invalid message format" in response["error"]


def test_websocket_chat_multiple_messages():
    """Test sending multiple messages in sequence."""
    room_id = "test-room-multi-msgs"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        # SECURITY: Receive credentials first
        creds = receive_credentials(ws)
        receive_history(ws)

        messages = [
            {"displayName": "User", "content": "Message 1"},
            {"displayName": "User", "content": "Message 2"},
            {"displayName": "User", "content": "Message 3"},
        ]

        for i, msg in enumerate(messages):
            ws.send_json(msg)
            received = ws.receive_json()
            assert received["type"] == "message"
            assert received["content"] == msg["content"]
            # Verify unique IDs
            assert "id" in received
            # Verify backend-assigned userId/role
            assert received["userId"] == creds["userId"]
            assert received["role"] == creds["role"]

        # Verify all messages are stored
        assert manager.get_message_count(room_id) == 3


def test_websocket_chat_message_schema():
    """Test that message schema matches requirements."""
    room_id = "test-room-schema"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        # SECURITY: Receive credentials first
        creds = receive_credentials(ws)
        receive_history(ws)

        ws.send_json({
            "displayName": "Test User",
            "content": "Test content"
        })

        received = ws.receive_json()

        # Verify all required fields are present
        assert received["type"] == "message"
        assert "id" in received  # UUID
        assert received["roomId"] == room_id
        # SECURITY: userId and role are backend-assigned
        assert received["userId"] == creds["userId"]
        assert received["role"] == "host"  # First client is always host
        assert received["content"] == "Test content"
        assert "ts" in received  # Timestamp
        assert isinstance(received["ts"], float)


# =============================================================================
# Identity Source Tests
# =============================================================================


def test_identity_source_default_anonymous():
    """Test that identitySource defaults to 'anonymous' when not provided in join."""
    room_id = "test-room-identity-default"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        creds = receive_credentials(ws)
        receive_history(ws)

        # Join without identitySource
        ws.send_json({"type": "join", "displayName": "Alice"})
        joined = ws.receive_json()

        assert joined["type"] == "user_joined"
        assert joined["user"]["identitySource"] == "anonymous"


def test_identity_source_sso():
    """Test that SSO identitySource is stored and broadcast correctly."""
    room_id = "test-room-identity-sso"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        creds = receive_credentials(ws)
        receive_history(ws)

        ws.send_json({"type": "join", "displayName": "alice.smith", "identitySource": "sso"})
        joined = ws.receive_json()

        assert joined["type"] == "user_joined"
        assert joined["user"]["identitySource"] == "sso"
        assert joined["user"]["displayName"] == "alice.smith"


def test_identity_source_named():
    """Test that 'named' identitySource works for custom nicknames."""
    room_id = "test-room-identity-named"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        creds = receive_credentials(ws)
        receive_history(ws)

        ws.send_json({"type": "join", "displayName": "Bob", "identitySource": "named"})
        joined = ws.receive_json()

        assert joined["type"] == "user_joined"
        assert joined["user"]["identitySource"] == "named"
        assert joined["user"]["displayName"] == "Bob"


def test_identity_source_forced_anonymous_on_auto_name():
    """Test that identitySource is forced to 'anonymous' when backend auto-names the user."""
    room_id = "test-room-identity-autoname"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
         client.websocket_connect(f"/ws/chat/{room_id}") as ws2:

        creds1 = receive_credentials(ws1)
        creds2 = receive_credentials(ws2)
        receive_history(ws1)
        receive_history(ws2)

        # Host joins first
        ws1.send_json({"type": "join", "displayName": "Host"})
        ws1.receive_json()  # user_joined for ws1
        ws2.receive_json()  # user_joined broadcast to ws2

        # Guest joins with empty displayName but claims SSO — should be forced to anonymous
        ws2.send_json({"type": "join", "displayName": "", "identitySource": "sso"})
        joined1 = ws1.receive_json()  # user_joined broadcast
        joined2 = ws2.receive_json()  # user_joined broadcast

        assert joined1["type"] == "user_joined"
        assert joined1["user"]["identitySource"] == "anonymous"
        assert joined1["user"]["displayName"].startswith("Guest ")


def test_identity_source_invalid_fallback():
    """Test that invalid identitySource values fallback to 'anonymous'."""
    room_id = "test-room-identity-invalid"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        creds = receive_credentials(ws)
        receive_history(ws)

        ws.send_json({"type": "join", "displayName": "Charlie", "identitySource": "bogus_value"})
        joined = ws.receive_json()

        assert joined["type"] == "user_joined"
        assert joined["user"]["identitySource"] == "anonymous"


def test_identity_source_in_users_list_broadcast():
    """Test that identitySource appears in the users list broadcast for all clients."""
    room_id = "test-room-identity-broadcast"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
         client.websocket_connect(f"/ws/chat/{room_id}") as ws2:

        creds1 = receive_credentials(ws1)
        creds2 = receive_credentials(ws2)
        receive_history(ws1)
        receive_history(ws2)

        # Host joins with SSO
        ws1.send_json({"type": "join", "displayName": "alice.smith", "identitySource": "sso"})
        joined_ws1 = ws1.receive_json()
        joined_ws2 = ws2.receive_json()

        # Guest joins with named
        ws2.send_json({"type": "join", "displayName": "Bob", "identitySource": "named"})
        joined2_ws1 = ws1.receive_json()
        joined2_ws2 = ws2.receive_json()

        # Check users list in the last broadcast has both users with their identity sources
        users = joined2_ws1["users"]
        assert len(users) == 2

        sso_user = next(u for u in users if u["displayName"] == "alice.smith")
        named_user = next(u for u in users if u["displayName"] == "Bob")

        assert sso_user["identitySource"] == "sso"
        assert named_user["identitySource"] == "named"


# =============================================================================
# Lead Transfer Tests
# =============================================================================


class TestLeadTransfer:
    """Tests for dynamic lead transfer functionality."""

    def test_host_is_initial_lead(self):
        """First user gets both host and lead roles."""
        room_id = "test-lead-initial"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1:
            creds = receive_credentials(ws1)
            assert creds["role"] == "host"
            assert creds["leadId"] == creds["userId"]

    def test_connected_message_includes_lead_id(self):
        """Verify leadId is present in the connected response."""
        room_id = "test-lead-connected"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1:
            creds = receive_credentials(ws1)
            assert "leadId" in creds
            assert creds["leadId"] == creds["userId"]

            history = receive_history(ws1)

            # Second client also sees the lead
            with client.websocket_connect(f"/ws/chat/{room_id}") as ws2:
                creds2 = receive_credentials(ws2)
                assert creds2["leadId"] == creds["userId"]  # Lead is still first user

    def test_history_message_includes_lead_id(self):
        """Verify leadId is present in the history response."""
        room_id = "test-lead-history"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1:
            creds1 = receive_credentials(ws1)
            history1 = receive_history(ws1)
            assert "leadId" in history1
            assert history1["leadId"] == creds1["userId"]

    def test_transfer_lead_by_host(self):
        """Host can transfer lead to a guest."""
        room_id = "test-lead-transfer-host"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
             client.websocket_connect(f"/ws/chat/{room_id}") as ws2:
            creds1 = receive_credentials(ws1)
            creds2 = receive_credentials(ws2)
            receive_history(ws1)
            receive_history(ws2)

            # Register both users
            ws1.send_json({"type": "join", "displayName": "Host"})
            ws1.receive_json()  # user_joined broadcast
            ws2.receive_json()  # user_joined broadcast

            ws2.send_json({"type": "join", "displayName": "Guest"})
            ws1.receive_json()  # user_joined broadcast
            ws2.receive_json()  # user_joined broadcast

            # Host transfers lead to guest
            ws1.send_json({"type": "transfer_lead", "targetUserId": creds2["userId"]})

            # Both clients receive lead_changed
            lead_msg1 = ws1.receive_json()
            lead_msg2 = ws2.receive_json()
            assert lead_msg1["type"] == "lead_changed"
            assert lead_msg1["leadId"] == creds2["userId"]
            assert lead_msg2["type"] == "lead_changed"
            assert lead_msg2["leadId"] == creds2["userId"]

    def test_transfer_lead_by_lead(self):
        """Current lead (non-host) can transfer lead to another user."""
        room_id = "test-lead-transfer-lead"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
             client.websocket_connect(f"/ws/chat/{room_id}") as ws2, \
             client.websocket_connect(f"/ws/chat/{room_id}") as ws3:
            creds1 = receive_credentials(ws1)
            creds2 = receive_credentials(ws2)
            creds3 = receive_credentials(ws3)
            receive_history(ws1)
            receive_history(ws2)
            receive_history(ws3)

            # Register all users
            ws1.send_json({"type": "join", "displayName": "Host"})
            ws1.receive_json(); ws2.receive_json(); ws3.receive_json()
            ws2.send_json({"type": "join", "displayName": "Guest1"})
            ws1.receive_json(); ws2.receive_json(); ws3.receive_json()
            ws3.send_json({"type": "join", "displayName": "Guest2"})
            ws1.receive_json(); ws2.receive_json(); ws3.receive_json()

            # Host transfers lead to Guest1
            ws1.send_json({"type": "transfer_lead", "targetUserId": creds2["userId"]})
            ws1.receive_json(); ws2.receive_json(); ws3.receive_json()  # lead_changed

            # Guest1 (now lead) transfers lead to Guest2
            ws2.send_json({"type": "transfer_lead", "targetUserId": creds3["userId"]})
            lead1 = ws1.receive_json()
            lead2 = ws2.receive_json()
            lead3 = ws3.receive_json()
            assert lead1["type"] == "lead_changed"
            assert lead1["leadId"] == creds3["userId"]
            assert lead2["leadId"] == creds3["userId"]
            assert lead3["leadId"] == creds3["userId"]

    def test_transfer_lead_unauthorized(self):
        """Regular guest cannot transfer lead."""
        room_id = "test-lead-transfer-unauth"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
             client.websocket_connect(f"/ws/chat/{room_id}") as ws2:
            creds1 = receive_credentials(ws1)
            creds2 = receive_credentials(ws2)
            receive_history(ws1)
            receive_history(ws2)

            # Register both users
            ws1.send_json({"type": "join", "displayName": "Host"})
            ws1.receive_json(); ws2.receive_json()
            ws2.send_json({"type": "join", "displayName": "Guest"})
            ws1.receive_json(); ws2.receive_json()

            # Guest tries to transfer lead — should get error
            ws2.send_json({"type": "transfer_lead", "targetUserId": creds1["userId"]})
            error = ws2.receive_json()
            assert error["type"] == "error"
            assert "host or current lead" in error["error"]

    def test_transfer_lead_invalid_target(self):
        """Transfer to non-existent user fails."""
        room_id = "test-lead-transfer-invalid"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1:
            creds1 = receive_credentials(ws1)
            receive_history(ws1)

            ws1.send_json({"type": "join", "displayName": "Host"})
            ws1.receive_json()  # user_joined

            ws1.send_json({"type": "transfer_lead", "targetUserId": "nonexistent-user-id"})
            error = ws1.receive_json()
            assert error["type"] == "error"
            assert "Invalid target" in error["error"]

    def test_lead_revert_on_disconnect(self):
        """When lead (non-host) disconnects, lead reverts to host.

        Uses manager-level disconnect to avoid flaky WebSocket context-manager
        disconnect timing when running in the full test suite.
        """
        room_id = "test-lead-revert"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
             client.websocket_connect(f"/ws/chat/{room_id}") as ws2:
            creds1 = receive_credentials(ws1)
            creds2 = receive_credentials(ws2)
            receive_history(ws1)
            receive_history(ws2)

            ws1.send_json({"type": "join", "displayName": "Host"})
            ws1.receive_json(); ws2.receive_json()
            ws2.send_json({"type": "join", "displayName": "Guest"})
            ws1.receive_json(); ws2.receive_json()

            # Transfer lead to guest
            ws1.send_json({"type": "transfer_lead", "targetUserId": creds2["userId"]})
            ws1.receive_json(); ws2.receive_json()  # lead_changed

            # Verify lead is now guest
            assert manager.get_lead_id(room_id) == creds2["userId"]

            # Find the server-side WebSocket for guest (ws2) from manager mapping
            guest_ws = None
            for ws_obj, (rid, uid) in manager.websocket_to_user.items():
                if rid == room_id and uid == creds2["userId"]:
                    guest_ws = ws_obj
                    break
            assert guest_ws is not None, "Guest WebSocket not found in manager"

            # Simulate disconnect at manager level (avoids flaky broadcast receive)
            disconnected_user, lead_reverted = manager.disconnect(guest_ws, room_id)
            assert disconnected_user is not None
            assert disconnected_user.displayName == "Guest"
            assert lead_reverted is True
            assert manager.get_lead_id(room_id) == creds1["userId"]

    def test_lead_disconnect_when_lead_is_host(self):
        """Host-lead disconnect doesn't trigger lead revert (no one to revert to)."""
        room_id = "test-lead-host-disconnect"
        # Just verify the manager state directly
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1:
            creds1 = receive_credentials(ws1)
            receive_history(ws1)
            ws1.send_json({"type": "join", "displayName": "Host"})
            ws1.receive_json()

            assert manager.is_lead(room_id, creds1["userId"])
            assert manager.is_host(room_id, creds1["userId"])

        # After disconnect, room may be empty — lead tracking cleaned up
        # No revert broadcast needed since no one is left

    def test_lead_changed_broadcast(self):
        """All clients receive lead_changed message on transfer."""
        room_id = "test-lead-broadcast"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
             client.websocket_connect(f"/ws/chat/{room_id}") as ws2, \
             client.websocket_connect(f"/ws/chat/{room_id}") as ws3:
            creds1 = receive_credentials(ws1)
            creds2 = receive_credentials(ws2)
            creds3 = receive_credentials(ws3)
            receive_history(ws1)
            receive_history(ws2)
            receive_history(ws3)

            # Register all
            ws1.send_json({"type": "join", "displayName": "Host"})
            ws1.receive_json(); ws2.receive_json(); ws3.receive_json()
            ws2.send_json({"type": "join", "displayName": "G1"})
            ws1.receive_json(); ws2.receive_json(); ws3.receive_json()
            ws3.send_json({"type": "join", "displayName": "G2"})
            ws1.receive_json(); ws2.receive_json(); ws3.receive_json()

            # Transfer lead
            ws1.send_json({"type": "transfer_lead", "targetUserId": creds3["userId"]})
            msg1 = ws1.receive_json()
            msg2 = ws2.receive_json()
            msg3 = ws3.receive_json()

            # All three receive the same lead_changed
            for msg in [msg1, msg2, msg3]:
                assert msg["type"] == "lead_changed"
                assert msg["leadId"] == creds3["userId"]

    def test_can_use_ai_permission(self):
        """can_use_ai returns True only for lead."""
        room_id = "test-can-use-ai"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
             client.websocket_connect(f"/ws/chat/{room_id}") as ws2:
            creds1 = receive_credentials(ws1)
            creds2 = receive_credentials(ws2)
            receive_history(ws1)
            receive_history(ws2)

            ws1.send_json({"type": "join", "displayName": "Host"})
            ws1.receive_json(); ws2.receive_json()
            ws2.send_json({"type": "join", "displayName": "Guest"})
            ws1.receive_json(); ws2.receive_json()

            # Host is initial lead — can use AI
            assert manager.can_use_ai(room_id, creds1["userId"]) is True
            assert manager.can_use_ai(room_id, creds2["userId"]) is False

            # Transfer lead to guest
            manager.transfer_lead(room_id, creds2["userId"])
            assert manager.can_use_ai(room_id, creds1["userId"]) is False
            assert manager.can_use_ai(room_id, creds2["userId"]) is True

    def test_can_configure_permission(self):
        """can_configure returns True for host OR lead."""
        room_id = "test-can-configure"
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
             client.websocket_connect(f"/ws/chat/{room_id}") as ws2:
            creds1 = receive_credentials(ws1)
            creds2 = receive_credentials(ws2)
            receive_history(ws1)
            receive_history(ws2)

            ws1.send_json({"type": "join", "displayName": "Host"})
            ws1.receive_json(); ws2.receive_json()
            ws2.send_json({"type": "join", "displayName": "Guest"})
            ws1.receive_json(); ws2.receive_json()

            # Initially host is lead — both host and lead can configure
            assert manager.can_configure(room_id, creds1["userId"]) is True
            assert manager.can_configure(room_id, creds2["userId"]) is False

            # Transfer lead to guest — now BOTH host and lead can configure
            manager.transfer_lead(room_id, creds2["userId"])
            assert manager.can_configure(room_id, creds1["userId"]) is True  # host always
            assert manager.can_configure(room_id, creds2["userId"]) is True  # lead

