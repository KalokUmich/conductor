"""Tests for WebSocket chat functionality with multi-client support."""
import pytest
from fastapi.testclient import TestClient
from app.main import app
from app.chat.manager import manager


client = TestClient(app)


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

        # Both clients receive history on connect (empty for new room)
        history1 = ws1.receive_json()
        history2 = ws2.receive_json()
        assert history1["type"] == "history"
        assert history2["type"] == "history"

        # Client 1 sends a message
        message1 = {
            "userId": "user1",
            "role": "host",
            "content": "Hello from user1"
        }
        ws1.send_json(message1)

        # Both clients should receive the broadcast message
        data1 = ws1.receive_json()
        data2 = ws2.receive_json()

        # Verify message structure
        assert data1["type"] == "message"
        assert data1["userId"] == "user1"
        assert data1["role"] == "host"
        assert data1["content"] == "Hello from user1"
        assert data1["roomId"] == room_id
        assert "id" in data1
        assert "ts" in data1

        # Both clients receive the same message
        assert data1 == data2

        # Client 2 sends a message
        message2 = {
            "userId": "user2",
            "role": "engineer",
            "content": "Hello from user2"
        }
        ws2.send_json(message2)

        # Both clients should receive the message
        data1 = ws1.receive_json()
        data2 = ws2.receive_json()

        assert data1["type"] == "message"
        assert data1["userId"] == "user2"
        assert data1["role"] == "engineer"
        assert data1 == data2


def test_websocket_chat_three_clients_same_room():
    """Test that three WebSocket clients in the same room receive all messages."""
    room_id = "test-room-three-clients"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws1, \
         client.websocket_connect(f"/ws/chat/{room_id}") as ws2, \
         client.websocket_connect(f"/ws/chat/{room_id}") as ws3:

        # All clients receive history on connect (empty for new room)
        assert ws1.receive_json()["type"] == "history"
        assert ws2.receive_json()["type"] == "history"
        assert ws3.receive_json()["type"] == "history"

        # Client 1 (host) sends a message
        msg1 = {"userId": "host-user", "role": "host", "content": "Welcome everyone!"}
        ws1.send_json(msg1)

        # All three clients should receive the message
        recv1 = ws1.receive_json()
        recv2 = ws2.receive_json()
        recv3 = ws3.receive_json()

        assert recv1["type"] == "message"
        assert recv1["content"] == "Welcome everyone!"
        assert recv1["role"] == "host"
        assert recv1 == recv2 == recv3

        # Client 2 (engineer) sends a message
        msg2 = {"userId": "engineer-1", "role": "engineer", "content": "Thanks for having me!"}
        ws2.send_json(msg2)

        recv1 = ws1.receive_json()
        recv2 = ws2.receive_json()
        recv3 = ws3.receive_json()

        assert recv1["content"] == "Thanks for having me!"
        assert recv1["role"] == "engineer"
        assert recv1 == recv2 == recv3

        # Client 3 (engineer) sends a message
        msg3 = {"userId": "engineer-2", "role": "engineer", "content": "Hello team!"}
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
        # Receive empty history on connect
        history1 = ws1.receive_json()
        assert history1["type"] == "history"
        assert len(history1["messages"]) == 0

        # Send first message
        ws1.send_json({"userId": "user1", "role": "host", "content": "First message"})
        ws1.receive_json()  # Receive the broadcast

        # Send second message
        ws1.send_json({"userId": "user1", "role": "host", "content": "Second message"})
        ws1.receive_json()  # Receive the broadcast

        # Second client joins - should receive history
        with client.websocket_connect(f"/ws/chat/{room_id}") as ws2:
            # First message should be history
            history = ws2.receive_json()

            assert history["type"] == "history"
            assert len(history["messages"]) == 2
            assert history["messages"][0]["content"] == "First message"
            assert history["messages"][1]["content"] == "Second message"

            # Third client joins - should also receive history
            with client.websocket_connect(f"/ws/chat/{room_id}") as ws3:
                history3 = ws3.receive_json()

                assert history3["type"] == "history"
                assert len(history3["messages"]) == 2


def test_websocket_chat_different_rooms():
    """Test that clients in different rooms don't receive each other's messages."""
    room1 = "room-isolated-1"
    room2 = "room-isolated-2"

    with client.websocket_connect(f"/ws/chat/{room1}") as ws1, \
         client.websocket_connect(f"/ws/chat/{room2}") as ws2:

        # Both clients receive history on connect
        assert ws1.receive_json()["type"] == "history"
        assert ws2.receive_json()["type"] == "history"

        # Client 1 sends a message in room 1
        ws1.send_json({"userId": "user1", "role": "host", "content": "Message in room 1"})

        # Client 1 should receive the message
        data1 = ws1.receive_json()
        assert data1["type"] == "message"
        assert data1["content"] == "Message in room 1"
        assert data1["roomId"] == room1

        # Client 2 sends a message in room 2
        ws2.send_json({"userId": "user2", "role": "engineer", "content": "Message in room 2"})

        # Client 2 should receive its message
        data2 = ws2.receive_json()
        assert data2["type"] == "message"
        assert data2["content"] == "Message in room 2"
        assert data2["roomId"] == room2

        # Verify each room has only 1 message
        assert manager.get_message_count(room1) == 1
        assert manager.get_message_count(room2) == 1


def test_websocket_chat_invalid_message():
    """Test that invalid messages are handled gracefully."""
    room_id = "test-room-invalid-msg"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        # Receive history on connect
        assert ws.receive_json()["type"] == "history"

        # Send invalid message (missing required fields)
        invalid_message = {"content": "Missing userId and role"}
        ws.send_json(invalid_message)

        # Should receive an error response
        response = ws.receive_json()
        assert response["type"] == "error"
        assert "Invalid message format" in response["error"]


def test_websocket_chat_invalid_role():
    """Test that invalid role values are rejected."""
    room_id = "test-room-invalid-role"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        # Receive history on connect
        assert ws.receive_json()["type"] == "history"

        # Send message with invalid role
        invalid_message = {
            "userId": "user1",
            "role": "admin",  # Invalid role
            "content": "Test"
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
        # Receive history on connect
        assert ws.receive_json()["type"] == "history"

        messages = [
            {"userId": "user1", "role": "host", "content": "Message 1"},
            {"userId": "user1", "role": "host", "content": "Message 2"},
            {"userId": "user1", "role": "host", "content": "Message 3"},
        ]

        for i, msg in enumerate(messages):
            ws.send_json(msg)
            received = ws.receive_json()
            assert received["type"] == "message"
            assert received["content"] == msg["content"]
            # Verify unique IDs
            assert "id" in received

        # Verify all messages are stored
        assert manager.get_message_count(room_id) == 3


def test_websocket_chat_message_schema():
    """Test that message schema matches requirements."""
    room_id = "test-room-schema"

    with client.websocket_connect(f"/ws/chat/{room_id}") as ws:
        # Receive history on connect
        assert ws.receive_json()["type"] == "history"

        ws.send_json({
            "userId": "test-user",
            "role": "engineer",
            "content": "Test content"
        })

        received = ws.receive_json()

        # Verify all required fields are present
        assert received["type"] == "message"
        assert "id" in received  # UUID
        assert received["roomId"] == room_id
        assert received["userId"] == "test-user"
        assert received["role"] == "engineer"
        assert received["content"] == "Test content"
        assert "ts" in received  # Timestamp
        assert isinstance(received["ts"], float)

