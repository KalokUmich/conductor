"""Tests for the summary endpoint.

Note: The legacy summary router is no longer registered in app.main.
These tests use a local test app with the summary router included.
"""
import time
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from app.summary.router import router as summary_router
from app.summary.schemas import SummaryRequest, SummaryResponse, ChatMessage


_test_app = FastAPI()
_test_app.include_router(summary_router)
client = TestClient(_test_app)


class TestPydanticSchemas:
    """Test Pydantic schema validation."""

    def test_chat_message_valid(self):
        """Test valid ChatMessage creation."""
        msg = ChatMessage(userId="user1", content="Hello", ts=time.time())
        assert msg.userId == "user1"
        assert msg.content == "Hello"
        assert msg.ts > 0

    def test_chat_message_missing_fields(self):
        """Test ChatMessage with missing required fields."""
        with pytest.raises(ValidationError):
            ChatMessage(userId="user1")  # missing content and ts

    def test_summary_request_valid(self):
        """Test valid SummaryRequest creation."""
        request = SummaryRequest(
            roomId="room1",
            messages=[
                ChatMessage(userId="user1", content="Hello", ts=time.time())
            ]
        )
        assert request.roomId == "room1"
        assert len(request.messages) == 1

    def test_summary_request_empty_messages(self):
        """Test SummaryRequest with empty messages list."""
        request = SummaryRequest(roomId="room1", messages=[])
        assert request.roomId == "room1"
        assert len(request.messages) == 0

    def test_summary_request_missing_room_id(self):
        """Test SummaryRequest with missing roomId."""
        with pytest.raises(ValidationError):
            SummaryRequest(messages=[])

    def test_summary_response_defaults(self):
        """Test SummaryResponse with default values."""
        response = SummaryResponse()
        assert response.goal == ""
        assert response.constraints == []
        assert response.decisions == []
        assert response.open_questions == []
        assert response.non_goals == []

    def test_summary_response_with_values(self):
        """Test SummaryResponse with provided values."""
        response = SummaryResponse(
            goal="Build a chat app",
            constraints=["No database"],
            decisions=["Use WebSocket"],
            open_questions=["Which framework?"],
            non_goals=["Mobile app"]
        )
        assert response.goal == "Build a chat app"
        assert len(response.constraints) == 1
        assert len(response.decisions) == 1
        assert len(response.open_questions) == 1
        assert len(response.non_goals) == 1


class TestSummaryEndpoint:
    """Test the POST /summary endpoint."""

    def test_summary_endpoint_empty_messages(self):
        """Test summary endpoint with empty messages."""
        response = client.post("/summary", json={
            "roomId": "room1",
            "messages": []
        })
        assert response.status_code == 200
        data = response.json()
        assert data["goal"] == ""
        assert data["constraints"] == []
        assert data["decisions"] == []
        assert data["open_questions"] == []
        assert data["non_goals"] == []

    def test_summary_endpoint_extracts_goal(self):
        """Test that summary endpoint extracts goal from messages."""
        response = client.post("/summary", json={
            "roomId": "room1",
            "messages": [
                {"userId": "user1", "content": "Goal: Build a real-time chat application", "ts": time.time()}
            ]
        })
        assert response.status_code == 200
        data = response.json()
        assert "chat application" in data["goal"].lower()

    def test_summary_endpoint_extracts_decisions(self):
        """Test that summary endpoint extracts decisions from messages."""
        response = client.post("/summary", json={
            "roomId": "room1",
            "messages": [
                {"userId": "user1", "content": "Decided: We will use FastAPI for the backend", "ts": time.time()}
            ]
        })
        assert response.status_code == 200
        data = response.json()
        assert len(data["decisions"]) == 1
        assert "FastAPI" in data["decisions"][0]

    def test_summary_endpoint_extracts_questions(self):
        """Test that summary endpoint extracts open questions."""
        response = client.post("/summary", json={
            "roomId": "room1",
            "messages": [
                {"userId": "user1", "content": "Should we use PostgreSQL or MongoDB?", "ts": time.time()}
            ]
        })
        assert response.status_code == 200
        data = response.json()
        assert len(data["open_questions"]) == 1

    def test_summary_endpoint_invalid_request(self):
        """Test summary endpoint with invalid request body."""
        response = client.post("/summary", json={
            "messages": []  # missing roomId
        })
        assert response.status_code == 422  # Validation error

    def test_summary_response_schema(self):
        """Test that response matches SummaryResponse schema."""
        response = client.post("/summary", json={
            "roomId": "room1",
            "messages": []
        })
        assert response.status_code == 200
        # Validate response can be parsed as SummaryResponse
        data = response.json()
        summary = SummaryResponse(**data)
        assert isinstance(summary.goal, str)
        assert isinstance(summary.constraints, list)
        assert isinstance(summary.decisions, list)
        assert isinstance(summary.open_questions, list)
        assert isinstance(summary.non_goals, list)

