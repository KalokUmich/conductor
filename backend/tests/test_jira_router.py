"""Unit tests for Jira integration router endpoints.

Tests all nine endpoints in app.integrations.jira.router using
httpx.AsyncClient with a mocked JiraOAuthService on app.state.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, PropertyMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.integrations.jira.models import (
    CreateIssueRequest,
    JiraCreateMeta,
    JiraFieldOption,
    JiraIssue,
    JiraIssueType,
    JiraProject,
    JiraTokenPair,
)
from app.main import app


@pytest_asyncio.fixture
async def mock_service():
    """Create and install a mock JiraOAuthService on app.state."""
    svc = MagicMock()
    # Ensure async methods are AsyncMock
    svc.exchange_code = AsyncMock()
    svc.get_projects = AsyncMock()
    svc.get_issue_types = AsyncMock()
    svc.get_create_meta = AsyncMock()
    svc.create_issue = AsyncMock()
    # Sync methods stay as MagicMock
    svc.get_authorize_url = MagicMock()
    svc.get_status = MagicMock()
    svc.disconnect = MagicMock()
    svc._team_field_key = ""

    original = getattr(app.state, "jira_service", None)
    app.state.jira_service = svc
    yield svc
    app.state.jira_service = original


@pytest_asyncio.fixture
async def client(mock_service):
    """Provide an httpx.AsyncClient wired to the FastAPI app."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def client_no_service():
    """Provide a client with jira_service set to None (disabled)."""
    original = getattr(app.state, "jira_service", None)
    app.state.jira_service = None
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.state.jira_service = original


# ---------------------------------------------------------------
# Service disabled (jira_service is None) — should return 400
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_authorize_url_disabled(client_no_service):
    resp = await client_no_service.get("/api/integrations/jira/authorize-url")
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_status_disabled(client_no_service):
    resp = await client_no_service.get("/api/integrations/jira/status")
    assert resp.status_code == 400


# ---------------------------------------------------------------
# GET /authorize-url
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_authorize_url(client, mock_service):
    mock_service.get_authorize_url.return_value = {
        "authorize_url": "https://auth.atlassian.com/authorize?client_id=abc",
        "state": "random-state-value",
    }
    resp = await client.get("/api/integrations/jira/authorize-url")
    assert resp.status_code == 200
    body = resp.json()
    assert "authorize_url" in body
    assert body["state"] == "random-state-value"
    mock_service.get_authorize_url.assert_called_once()


# ---------------------------------------------------------------
# GET /callback (OAuth redirect — returns HTML)
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_callback_get_success(client, mock_service):
    token = JiraTokenPair(
        access_token="at", refresh_token="rt",
        cloud_id="cloud-1", site_url="https://mysite.atlassian.net",
    )
    mock_service.exchange_code.return_value = token

    resp = await client.get(
        "/api/integrations/jira/callback",
        params={"code": "auth-code-123", "state": "s"},
    )
    assert resp.status_code == 200
    assert "Jira Connected" in resp.text
    assert "mysite.atlassian.net" in resp.text
    mock_service.exchange_code.assert_awaited_once_with("auth-code-123", "s")


@pytest.mark.asyncio
async def test_oauth_callback_get_failure(client, mock_service):
    mock_service.exchange_code.side_effect = ValueError("Invalid state")

    resp = await client.get(
        "/api/integrations/jira/callback",
        params={"code": "bad-code", "state": "bad"},
    )
    assert resp.status_code == 400
    assert "Connection Failed" in resp.text
    assert "Invalid state" in resp.text


@pytest.mark.asyncio
async def test_oauth_callback_get_missing_code(client):
    """code is a required query param — FastAPI returns 422."""
    resp = await client.get("/api/integrations/jira/callback")
    assert resp.status_code == 422


# ---------------------------------------------------------------
# POST /callback (exchange code from extension)
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_oauth_callback_post_success(client, mock_service):
    token = JiraTokenPair(
        access_token="at", refresh_token="rt",
        cloud_id="cloud-2", site_url="https://site2.atlassian.net",
    )
    mock_service.exchange_code.return_value = token

    resp = await client.post(
        "/api/integrations/jira/callback",
        json={"code": "ext-code", "state": "ext-state"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "connected"
    assert body["cloud_id"] == "cloud-2"
    assert body["site_url"] == "https://site2.atlassian.net"
    mock_service.exchange_code.assert_awaited_once_with("ext-code", "ext-state")


@pytest.mark.asyncio
async def test_oauth_callback_post_missing_code(client, mock_service):
    resp = await client.post(
        "/api/integrations/jira/callback",
        json={"code": "", "state": "s"},
    )
    assert resp.status_code == 400
    assert "Missing authorization code" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_oauth_callback_post_exchange_error(client, mock_service):
    mock_service.exchange_code.side_effect = RuntimeError("Token exchange failed")

    resp = await client.post(
        "/api/integrations/jira/callback",
        json={"code": "code", "state": "s"},
    )
    assert resp.status_code == 400
    assert "Token exchange failed" in resp.json()["detail"]


# ---------------------------------------------------------------
# GET /status
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_connected(client, mock_service):
    mock_service.get_status.return_value = {
        "connected": True,
        "cloud_id": "cloud-x",
        "site_url": "https://x.atlassian.net",
    }
    resp = await client.get("/api/integrations/jira/status")
    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is True
    assert body["cloud_id"] == "cloud-x"


@pytest.mark.asyncio
async def test_status_disconnected(client, mock_service):
    mock_service.get_status.return_value = {"connected": False}
    resp = await client.get("/api/integrations/jira/status")
    assert resp.status_code == 200
    assert resp.json()["connected"] is False


# ---------------------------------------------------------------
# POST /disconnect
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect(client, mock_service):
    resp = await client.post("/api/integrations/jira/disconnect")
    assert resp.status_code == 200
    assert resp.json()["status"] == "disconnected"
    mock_service.disconnect.assert_called_once()


# ---------------------------------------------------------------
# GET /projects
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_projects(client, mock_service):
    mock_service.get_projects.return_value = [
        JiraProject(id="10000", key="PROJ", name="My Project", style="classic"),
        JiraProject(id="10001", key="ENG", name="Engineering"),
    ]
    resp = await client.get("/api/integrations/jira/projects")
    assert resp.status_code == 200
    projects = resp.json()
    assert len(projects) == 2
    assert projects[0]["key"] == "PROJ"
    assert projects[1]["name"] == "Engineering"


@pytest.mark.asyncio
async def test_list_projects_unauthorized(client, mock_service):
    mock_service.get_projects.side_effect = RuntimeError("Not connected")
    resp = await client.get("/api/integrations/jira/projects")
    assert resp.status_code == 401
    assert "Not connected" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_list_projects_server_error(client, mock_service):
    mock_service.get_projects.side_effect = Exception("Unexpected error")
    resp = await client.get("/api/integrations/jira/projects")
    assert resp.status_code == 500


# ---------------------------------------------------------------
# GET /issue-types
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_issue_types(client, mock_service):
    mock_service.get_issue_types.return_value = [
        JiraIssueType(id="1", name="Bug", subtask=False),
        JiraIssueType(id="2", name="Story", subtask=False),
        JiraIssueType(id="3", name="Sub-task", subtask=True),
    ]
    resp = await client.get(
        "/api/integrations/jira/issue-types",
        params={"projectKey": "PROJ"},
    )
    assert resp.status_code == 200
    types = resp.json()
    assert len(types) == 3
    assert types[0]["name"] == "Bug"
    assert types[2]["subtask"] is True
    mock_service.get_issue_types.assert_awaited_once_with("PROJ")


@pytest.mark.asyncio
async def test_list_issue_types_unauthorized(client, mock_service):
    mock_service.get_issue_types.side_effect = RuntimeError("Not connected")
    resp = await client.get(
        "/api/integrations/jira/issue-types",
        params={"projectKey": "PROJ"},
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_issue_types_missing_project_key(client):
    """projectKey is required — should return 422."""
    resp = await client.get("/api/integrations/jira/issue-types")
    assert resp.status_code == 422


# ---------------------------------------------------------------
# GET /create-meta
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_create_meta(client, mock_service):
    meta = JiraCreateMeta(
        priorities=[JiraFieldOption(id="1", name="High")],
        components=[JiraFieldOption(id="10", name="Backend")],
        teams=[JiraFieldOption(id="100", name="Alpha Team")],
        team_field_key="customfield_10001",
    )
    mock_service.get_create_meta.return_value = meta

    resp = await client.get(
        "/api/integrations/jira/create-meta",
        params={"projectKey": "PROJ", "issueTypeId": "1"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["priorities"]) == 1
    assert body["priorities"][0]["name"] == "High"
    assert body["team_field_key"] == "customfield_10001"
    mock_service.get_create_meta.assert_awaited_once_with("PROJ", "1")


@pytest.mark.asyncio
async def test_get_create_meta_unauthorized(client, mock_service):
    mock_service.get_create_meta.side_effect = RuntimeError("Not connected")
    resp = await client.get(
        "/api/integrations/jira/create-meta",
        params={"projectKey": "PROJ", "issueTypeId": "1"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------
# POST /issues
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_issue_simple(client, mock_service):
    mock_service.create_issue.return_value = JiraIssue(
        id="12345", key="PROJ-42",
        self_url="https://api.atlassian.com/ex/jira/cloud/rest/api/3/issue/12345",
        browse_url="https://mysite.atlassian.net/browse/PROJ-42",
    )
    resp = await client.post(
        "/api/integrations/jira/issues",
        json={"project_key": "PROJ", "summary": "Fix login bug"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["key"] == "PROJ-42"
    assert body["browse_url"].endswith("PROJ-42")
    mock_service.create_issue.assert_awaited_once()
    # team was empty, so team_field_key should be ""
    call_kwargs = mock_service.create_issue.call_args
    assert call_kwargs.kwargs.get("team_field_key", "") == ""


@pytest.mark.asyncio
async def test_create_issue_with_team_cached(client, mock_service):
    """When team is specified and _team_field_key is already cached."""
    mock_service._team_field_key = "customfield_10001"
    mock_service.create_issue.return_value = JiraIssue(
        id="99", key="ENG-7", self_url="", browse_url="",
    )
    resp = await client.post(
        "/api/integrations/jira/issues",
        json={
            "project_key": "ENG",
            "summary": "Add feature",
            "team": "team-uuid",
            "issue_type": "Task",
        },
    )
    assert resp.status_code == 200
    call_kwargs = mock_service.create_issue.call_args
    assert call_kwargs.kwargs["team_field_key"] == "customfield_10001"


@pytest.mark.asyncio
async def test_create_issue_with_team_needs_lookup(client, mock_service):
    """When team is specified but _team_field_key is empty — triggers issue type + create-meta lookup."""
    mock_service._team_field_key = ""
    mock_service.get_issue_types.return_value = [
        JiraIssueType(id="10100", name="Task"),
        JiraIssueType(id="10101", name="Bug"),
    ]
    mock_service.get_create_meta.return_value = JiraCreateMeta(
        team_field_key="customfield_10124",
        teams=[JiraFieldOption(id="200", name="Beta")],
    )
    mock_service.create_issue.return_value = JiraIssue(
        id="50", key="PROJ-50", self_url="", browse_url="",
    )
    resp = await client.post(
        "/api/integrations/jira/issues",
        json={
            "project_key": "PROJ",
            "summary": "Team issue",
            "team": "200",
            "issue_type": "Task",
        },
    )
    assert resp.status_code == 200
    mock_service.get_issue_types.assert_awaited_once_with("PROJ")
    mock_service.get_create_meta.assert_awaited_once_with("PROJ", "10100")
    call_kwargs = mock_service.create_issue.call_args
    assert call_kwargs.kwargs["team_field_key"] == "customfield_10124"


@pytest.mark.asyncio
async def test_create_issue_unauthorized(client, mock_service):
    mock_service.create_issue.side_effect = RuntimeError("Not connected")
    resp = await client.post(
        "/api/integrations/jira/issues",
        json={"project_key": "PROJ", "summary": "Test"},
    )
    assert resp.status_code == 401
    assert "Not connected" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_create_issue_server_error(client, mock_service):
    mock_service.create_issue.side_effect = Exception("Jira API error (400): field required")
    resp = await client.post(
        "/api/integrations/jira/issues",
        json={"project_key": "PROJ", "summary": "Test"},
    )
    assert resp.status_code == 500
