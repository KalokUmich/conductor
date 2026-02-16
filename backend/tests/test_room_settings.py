"""Tests for room settings endpoints and manager methods."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.chat.manager import ConnectionManager
from app.chat.settings_router import router


@pytest.fixture
def client():
    """Create a test client with the room settings router."""
    test_app = FastAPI()
    test_app.include_router(router)
    return TestClient(test_app)


class TestRoomSettingsManager:
    """Tests for ConnectionManager room_settings methods."""

    def test_get_room_settings_default(self):
        """get_room_settings should return defaults for unknown room."""
        mgr = ConnectionManager()
        settings = mgr.get_room_settings("unknown-room")
        assert settings == {"code_style": ""}

    def test_update_room_settings(self):
        """update_room_settings should store and return settings."""
        mgr = ConnectionManager()
        result = mgr.update_room_settings("room-1", {"code_style": "Use PEP 8"})
        assert result["code_style"] == "Use PEP 8"

    def test_get_room_settings_after_update(self):
        """get_room_settings should return previously stored settings."""
        mgr = ConnectionManager()
        mgr.update_room_settings("room-1", {"code_style": "Follow Google style"})
        settings = mgr.get_room_settings("room-1")
        assert settings["code_style"] == "Follow Google style"

    def test_update_room_settings_merge(self):
        """update_room_settings should merge with existing settings."""
        mgr = ConnectionManager()
        mgr.update_room_settings("room-1", {"code_style": "PEP 8"})
        mgr.update_room_settings("room-1", {"code_style": "Google Style"})
        settings = mgr.get_room_settings("room-1")
        assert settings["code_style"] == "Google Style"

    def test_clear_room_clears_settings(self):
        """clear_room should remove room settings."""
        mgr = ConnectionManager()
        mgr.update_room_settings("room-1", {"code_style": "PEP 8"})
        mgr.clear_room("room-1")
        settings = mgr.get_room_settings("room-1")
        assert settings == {"code_style": ""}

    def test_room_settings_isolated_per_room(self):
        """Each room should have independent settings."""
        mgr = ConnectionManager()
        mgr.update_room_settings("room-1", {"code_style": "PEP 8"})
        mgr.update_room_settings("room-2", {"code_style": "Google"})
        assert mgr.get_room_settings("room-1")["code_style"] == "PEP 8"
        assert mgr.get_room_settings("room-2")["code_style"] == "Google"


class TestRoomSettingsEndpoints:
    """Tests for room settings REST endpoints."""

    def test_get_settings_default(self, client):
        """GET /rooms/{room_id}/settings should return defaults."""
        response = client.get("/rooms/test-room/settings")
        assert response.status_code == 200
        data = response.json()
        assert data["code_style"] == ""

    def test_put_settings(self, client):
        """PUT /rooms/{room_id}/settings should update and return settings."""
        response = client.put(
            "/rooms/test-room/settings",
            json={"code_style": "Use 4-space indentation. Follow PEP 8."},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["code_style"] == "Use 4-space indentation. Follow PEP 8."

    def test_get_settings_after_put(self, client):
        """GET should reflect previously PUT settings."""
        client.put(
            "/rooms/room-persist/settings",
            json={"code_style": "Tabs not spaces"},
        )
        response = client.get("/rooms/room-persist/settings")
        assert response.status_code == 200
        assert response.json()["code_style"] == "Tabs not spaces"

    def test_put_settings_partial_update(self, client):
        """PUT with null code_style should not overwrite."""
        client.put(
            "/rooms/room-partial/settings",
            json={"code_style": "Original style"},
        )
        # PUT with no code_style field — should keep existing
        response = client.put(
            "/rooms/room-partial/settings",
            json={},
        )
        assert response.status_code == 200
        # Re-read to confirm
        response = client.get("/rooms/room-partial/settings")
        assert response.json()["code_style"] == "Original style"

    def test_put_empty_code_style(self, client):
        """PUT with empty string should clear code_style."""
        client.put(
            "/rooms/room-clear/settings",
            json={"code_style": "Some style"},
        )
        response = client.put(
            "/rooms/room-clear/settings",
            json={"code_style": ""},
        )
        assert response.status_code == 200
        assert response.json()["code_style"] == ""
