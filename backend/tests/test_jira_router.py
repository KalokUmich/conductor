"""Unit tests for Jira integration router endpoints.

Tests all nine endpoints in app.integrations.jira.router using
httpx.AsyncClient with a mocked JiraOAuthService on app.state.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.integrations.jira.models import (
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
        access_token="at",
        refresh_token="rt",
        cloud_id="cloud-1",
        site_url="https://mysite.atlassian.net",
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
        access_token="at",
        refresh_token="rt",
        cloud_id="cloud-2",
        site_url="https://site2.atlassian.net",
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
async def test_list_projects_filtered(client, mock_service):
    """When allowed_projects is set, only matching projects are returned."""
    mock_service.get_projects.return_value = [
        JiraProject(id="10040", key="DEV", name="Development", style="classic"),
        JiraProject(id="10033", key="FO", name="FinOps", style="next-gen"),
        JiraProject(id="10000", key="OLD", name="Legacy Project", style="classic"),
        JiraProject(id="10042", key="HELP", name="Helpdesk", style="classic"),
    ]
    # Set allowed filter
    app.state.jira_allowed_projects = {"DEV", "FO", "HELP"}
    try:
        resp = await client.get("/api/integrations/jira/projects")
        assert resp.status_code == 200
        projects = resp.json()
        assert len(projects) == 3
        keys = {p["key"] for p in projects}
        assert keys == {"DEV", "FO", "HELP"}
    finally:
        app.state.jira_allowed_projects = set()


@pytest.mark.asyncio
async def test_list_projects_no_filter(client, mock_service):
    """When allowed_projects is empty, all projects are returned."""
    mock_service.get_projects.return_value = [
        JiraProject(id="10040", key="DEV", name="Development"),
        JiraProject(id="10000", key="OLD", name="Legacy"),
    ]
    app.state.jira_allowed_projects = set()
    resp = await client.get("/api/integrations/jira/projects")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


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
        id="12345",
        key="PROJ-42",
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
        id="99",
        key="ENG-7",
        self_url="",
        browse_url="",
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
        id="50",
        key="PROJ-50",
        self_url="",
        browse_url="",
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


# ---------------------------------------------------------------
# POST /refresh (token refresh on behalf of extension)
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_token_success(client, mock_service):
    mock_service.refresh_token_for_client = AsyncMock(
        return_value={
            "access_token": "new-acc",
            "refresh_token": "new-ref",
            "expires_in": 3600,
        }
    )
    resp = await client.post(
        "/api/integrations/jira/refresh",
        json={"refresh_token": "old-ref"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "new-acc"
    assert body["refresh_token"] == "new-ref"
    assert body["expires_in"] == 3600
    mock_service.refresh_token_for_client.assert_awaited_once_with("old-ref")


@pytest.mark.asyncio
async def test_refresh_token_expired(client, mock_service):
    mock_service.refresh_token_for_client = AsyncMock(side_effect=RuntimeError("Token refresh failed: invalid_grant"))
    resp = await client.post(
        "/api/integrations/jira/refresh",
        json={"refresh_token": "expired-ref"},
    )
    assert resp.status_code == 401
    assert "Token refresh failed" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_token_server_error(client, mock_service):
    mock_service.refresh_token_for_client = AsyncMock(side_effect=Exception("Unexpected error"))
    resp = await client.post(
        "/api/integrations/jira/refresh",
        json={"refresh_token": "some-ref"},
    )
    assert resp.status_code == 500


@pytest.mark.asyncio
async def test_refresh_token_disabled(client_no_service):
    resp = await client_no_service.post(
        "/api/integrations/jira/refresh",
        json={"refresh_token": "ref"},
    )
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_refresh_token_missing_body(client, mock_service):
    """refresh_token is required — should return 422."""
    resp = await client.post(
        "/api/integrations/jira/refresh",
        json={},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------
# GET /tokens (retrieve current tokens for extension persistence)
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_tokens_connected(client, mock_service):
    mock_service.get_status.return_value = {"connected": True}
    mock_service._tokens = JiraTokenPair(
        access_token="acc-1",
        refresh_token="ref-1",
        expires_in=3600,
        cloud_id="cloud-t",
        site_url="https://t.atlassian.net",
    )
    resp = await client.get("/api/integrations/jira/tokens")
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "acc-1"
    assert body["refresh_token"] == "ref-1"
    assert body["cloud_id"] == "cloud-t"
    assert body["site_url"] == "https://t.atlassian.net"


@pytest.mark.asyncio
async def test_get_tokens_not_connected(client, mock_service):
    mock_service.get_status.return_value = {"connected": False}
    resp = await client.get("/api/integrations/jira/tokens")
    assert resp.status_code == 401
    assert "Not connected" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_get_tokens_disabled(client_no_service):
    resp = await client_no_service.get("/api/integrations/jira/tokens")
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


# ---------------------------------------------------------------
# POST /callback now includes tokens in response
# ---------------------------------------------------------------


# ---------------------------------------------------------------
# GET /undone (list user's undone tickets)
# ---------------------------------------------------------------


# ---------------------------------------------------------------
# GET /issue/{key}/transitions
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_transitions(client, mock_service):
    mock_service.get_transitions = AsyncMock(
        return_value=[
            {"id": "21", "name": "Start", "to_status": "In Progress", "blocked": False},
            {"id": "31", "name": "Done", "to_status": "Done", "blocked": True},
        ]
    )
    resp = await client.get("/api/integrations/jira/issue/DEV-1/transitions")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    assert data[1]["blocked"] is True


# ---------------------------------------------------------------
# POST /issue/{key}/transition
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_transition_issue_success(client, mock_service):
    mock_service.transition_issue = AsyncMock()
    resp = await client.post(
        "/api/integrations/jira/issue/DEV-1/transition",
        json={"transition_id": "21"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "transitioned"


@pytest.mark.asyncio
async def test_transition_issue_blocked(client, mock_service):
    mock_service.transition_issue = AsyncMock(
        side_effect=RuntimeError("Cannot transition DEV-1 to 'Done' — require manual user action")
    )
    resp = await client.post(
        "/api/integrations/jira/issue/DEV-1/transition",
        json={"transition_id": "31"},
    )
    assert resp.status_code == 403
    assert "manual" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_transition_issue_missing_id(client, mock_service):
    resp = await client.post(
        "/api/integrations/jira/issue/DEV-1/transition",
        json={},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------
# POST /issue/{key}/comment
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_comment(client, mock_service):
    mock_service.add_comment = AsyncMock(return_value={"id": "999", "created": "2026-04-01"})
    resp = await client.post(
        "/api/integrations/jira/issue/DEV-1/comment",
        json={"body": "Found the root cause"},
    )
    assert resp.status_code == 200
    assert resp.json()["id"] == "999"


@pytest.mark.asyncio
async def test_add_comment_empty_body(client, mock_service):
    resp = await client.post(
        "/api/integrations/jira/issue/DEV-1/comment",
        json={"body": ""},
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------
# GET /undone
# ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_undone_tickets(client, mock_service):
    mock_service.list_undone_tickets = AsyncMock(
        return_value={
            "tickets": [
                {
                    "key": "DEV-1",
                    "summary": "Fix bug",
                    "status": "In Progress",
                    "priority": "High",
                    "issuetype": "Bug",
                    "assignee": "Alice",
                    "components": ["JBE"],
                    "epic_key": "EPIC-1",
                    "browse_url": "https://x/DEV-1",
                },
                {
                    "key": "DEV-2",
                    "summary": "Add feature",
                    "status": "To Do",
                    "priority": "Medium",
                    "issuetype": "Story",
                    "assignee": "Alice",
                    "components": [],
                    "epic_key": "",
                    "browse_url": "https://x/DEV-2",
                },
            ],
            "epics": {
                "EPIC-1": {
                    "key": "EPIC-1",
                    "summary": "Epic One",
                    "status": "In Progress",
                    "priority": "High",
                    "assignee": "Alice",
                    "browse_url": "https://x/EPIC-1",
                },
            },
            "unassigned_tickets": [],
        }
    )
    resp = await client.get("/api/integrations/jira/undone")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["tickets"]) == 2
    assert data["tickets"][0]["key"] == "DEV-1"
    assert data["tickets"][0]["epic_key"] == "EPIC-1"
    assert "EPIC-1" in data["epics"]
    assert data["epics"]["EPIC-1"]["summary"] == "Epic One"
    mock_service.list_undone_tickets.assert_awaited_once()


@pytest.mark.asyncio
async def test_list_undone_tickets_unauthorized(client, mock_service):
    mock_service.list_undone_tickets = AsyncMock(side_effect=RuntimeError("Not connected"))
    resp = await client.get("/api/integrations/jira/undone")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_list_undone_tickets_disabled(client_no_service):
    resp = await client_no_service.get("/api/integrations/jira/undone")
    assert resp.status_code == 400
    assert "not enabled" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_oauth_callback_post_returns_tokens(client, mock_service):
    """POST /callback should return access_token, refresh_token, expires_in."""
    token = JiraTokenPair(
        access_token="at-new",
        refresh_token="rt-new",
        expires_in=7200,
        cloud_id="cloud-new",
        site_url="https://new.atlassian.net",
    )
    mock_service.exchange_code.return_value = token

    resp = await client.post(
        "/api/integrations/jira/callback",
        json={"code": "code-x", "state": "s"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"] == "at-new"
    assert body["refresh_token"] == "rt-new"
    assert body["expires_in"] == 7200
