"""Comprehensive unit tests for JiraOAuthService.

Tests cover all public methods and key internal helpers:
- get_authorize_url
- exchange_code
- _refresh_token
- get_valid_token
- get_status
- disconnect
- _api_request (authenticated requests, 401 auto-refresh, error parsing)
- get_projects
- get_issue_types
- create_issue
"""
from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from app.integrations.jira.models import (
    CreateIssueRequest,
    JiraFieldOption,
    JiraTokenPair,
)
from app.integrations.jira.service import JiraOAuthService


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_service(**kwargs) -> JiraOAuthService:
    """Create a JiraOAuthService with default test credentials."""
    defaults = {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "redirect_uri": "http://localhost:8000/callback",
    }
    defaults.update(kwargs)
    return JiraOAuthService(**defaults)


def _mock_response(status_code: int = 200, json_data=None, text: str = ""):
    """Build a mock httpx.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    resp.content = b"content" if json_data else b""
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "error", request=MagicMock(), response=resp,
        )
    return resp


def _inject_tokens(svc: JiraOAuthService, **overrides) -> None:
    """Inject a valid JiraTokenPair directly into the service."""
    defaults = {
        "access_token": "access-abc",
        "refresh_token": "refresh-xyz",
        "expires_in": 3600,
        "cloud_id": "cloud-123",
        "site_url": "https://mysite.atlassian.net",
    }
    defaults.update(overrides)
    svc._tokens = JiraTokenPair(**defaults)
    svc._token_expires_at = time.time() + defaults["expires_in"]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_httpx():
    """Provide a mock httpx.AsyncClient context manager."""
    with patch("app.integrations.jira.service.httpx.AsyncClient") as MockClient:
        mock_client = AsyncMock()
        MockClient.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        MockClient.return_value.__aexit__ = AsyncMock(return_value=False)
        yield mock_client


@pytest.fixture
def svc():
    """A default JiraOAuthService instance."""
    return _make_service()


# ===========================================================================
# get_authorize_url
# ===========================================================================

class TestGetAuthorizeUrl:
    def test_returns_authorize_url_and_state(self, svc: JiraOAuthService):
        result = svc.get_authorize_url()
        assert "authorize_url" in result
        assert "state" in result
        assert result["state"]  # non-empty

    def test_authorize_url_contains_expected_params(self, svc: JiraOAuthService):
        result = svc.get_authorize_url()
        url = result["authorize_url"]
        assert "auth.atlassian.com/authorize" in url
        assert "client_id=test-client-id" in url
        assert "response_type=code" in url
        assert "redirect_uri=" in url

    def test_state_registered_in_pending_states(self, svc: JiraOAuthService):
        result = svc.get_authorize_url()
        state = result["state"]
        assert state in svc._pending_states
        assert isinstance(svc._pending_states[state], float)

    def test_multiple_calls_produce_unique_states(self, svc: JiraOAuthService):
        r1 = svc.get_authorize_url()
        r2 = svc.get_authorize_url()
        assert r1["state"] != r2["state"]
        assert len(svc._pending_states) == 2


# ===========================================================================
# exchange_code
# ===========================================================================

class TestExchangeCode:
    @pytest.mark.asyncio
    async def test_exchange_code_stores_tokens(self, svc, mock_httpx):
        """Successful exchange stores tokens and cloudId."""
        token_resp = _mock_response(json_data={
            "access_token": "acc-123",
            "refresh_token": "ref-456",
            "expires_in": 7200,
            "scope": "read:jira-work",
        })
        resource_resp = _mock_response(json_data=[
            {"id": "cloud-abc", "url": "https://test.atlassian.net"},
        ])
        mock_httpx.post.return_value = token_resp
        mock_httpx.get.return_value = resource_resp

        # Register valid state
        svc._pending_states["valid-state"] = time.time()

        result = await svc.exchange_code("auth-code-1", "valid-state")

        assert result.access_token == "acc-123"
        assert result.refresh_token == "ref-456"
        assert result.cloud_id == "cloud-abc"
        assert result.site_url == "https://test.atlassian.net"
        assert svc._tokens is not None
        assert "valid-state" not in svc._pending_states

    @pytest.mark.asyncio
    async def test_exchange_code_prefers_fintern_resource(self, svc, mock_httpx):
        """When multiple resources exist, prefer the one with 'fintern' in URL."""
        token_resp = _mock_response(json_data={
            "access_token": "acc",
            "refresh_token": "ref",
        })
        resource_resp = _mock_response(json_data=[
            {"id": "cloud-other", "url": "https://other.atlassian.net"},
            {"id": "cloud-fintern", "url": "https://fintern.atlassian.net"},
        ])
        mock_httpx.post.return_value = token_resp
        mock_httpx.get.return_value = resource_resp

        svc._pending_states["s"] = time.time()
        result = await svc.exchange_code("code", "s")

        assert result.cloud_id == "cloud-fintern"
        assert result.site_url == "https://fintern.atlassian.net"

    @pytest.mark.asyncio
    async def test_exchange_code_invalid_state_raises(self, svc, mock_httpx):
        """A non-empty state that is not pending should raise ValueError."""
        with pytest.raises(ValueError, match="Invalid or expired OAuth state"):
            await svc.exchange_code("code", "unknown-state")

    @pytest.mark.asyncio
    async def test_exchange_code_empty_state_allowed(self, svc, mock_httpx):
        """An empty state string bypasses state validation."""
        token_resp = _mock_response(json_data={
            "access_token": "acc",
            "refresh_token": "ref",
        })
        resource_resp = _mock_response(json_data=[
            {"id": "cid", "url": "https://x.atlassian.net"},
        ])
        mock_httpx.post.return_value = token_resp
        mock_httpx.get.return_value = resource_resp

        result = await svc.exchange_code("code", "")
        assert result.access_token == "acc"

    @pytest.mark.asyncio
    async def test_exchange_code_no_resources(self, svc, mock_httpx):
        """When accessible-resources returns empty list, cloudId stays empty."""
        token_resp = _mock_response(json_data={
            "access_token": "acc",
            "refresh_token": "ref",
        })
        resource_resp = _mock_response(json_data=[])
        mock_httpx.post.return_value = token_resp
        mock_httpx.get.return_value = resource_resp

        svc._pending_states["s"] = time.time()
        result = await svc.exchange_code("code", "s")

        assert result.cloud_id == ""
        assert result.site_url == ""

    @pytest.mark.asyncio
    async def test_exchange_code_token_request_failure(self, svc, mock_httpx):
        """If the token exchange POST fails, the error propagates."""
        mock_httpx.post.return_value = _mock_response(status_code=400, json_data={"error": "invalid_grant"})

        svc._pending_states["s"] = time.time()
        with pytest.raises(httpx.HTTPStatusError):
            await svc.exchange_code("bad-code", "s")


# ===========================================================================
# _refresh_token
# ===========================================================================

class TestRefreshToken:
    @pytest.mark.asyncio
    async def test_refresh_updates_access_token(self, svc, mock_httpx):
        """Refreshing updates the access token and expiry."""
        _inject_tokens(svc)

        mock_httpx.post.return_value = _mock_response(json_data={
            "access_token": "new-access",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        })

        await svc._refresh_token()

        assert svc._tokens.access_token == "new-access"
        assert svc._tokens.refresh_token == "new-refresh"

    @pytest.mark.asyncio
    async def test_refresh_preserves_old_refresh_token_if_not_returned(self, svc, mock_httpx):
        """If the response omits refresh_token, keep the existing one."""
        _inject_tokens(svc, refresh_token="keep-me")

        mock_httpx.post.return_value = _mock_response(json_data={
            "access_token": "new-access",
        })

        await svc._refresh_token()

        assert svc._tokens.access_token == "new-access"
        assert svc._tokens.refresh_token == "keep-me"

    @pytest.mark.asyncio
    async def test_refresh_raises_when_no_tokens(self, svc, mock_httpx):
        """Refreshing without stored tokens raises RuntimeError."""
        with pytest.raises(RuntimeError, match="No refresh token available"):
            await svc._refresh_token()

    @pytest.mark.asyncio
    async def test_refresh_raises_when_no_refresh_token(self, svc, mock_httpx):
        """Refreshing when refresh_token is empty raises RuntimeError."""
        _inject_tokens(svc, refresh_token="")
        with pytest.raises(RuntimeError, match="No refresh token available"):
            await svc._refresh_token()


# ===========================================================================
# get_valid_token
# ===========================================================================

class TestGetValidToken:
    @pytest.mark.asyncio
    async def test_returns_token_when_not_expired(self, svc):
        """Returns the current access token if expiry is far in the future."""
        _inject_tokens(svc, expires_in=3600)
        token = await svc.get_valid_token()
        assert token == "access-abc"

    @pytest.mark.asyncio
    async def test_refreshes_when_near_expiry(self, svc, mock_httpx):
        """Triggers refresh when token expires within 60 seconds."""
        _inject_tokens(svc)
        svc._token_expires_at = time.time() + 30  # 30s left, under 60s threshold

        mock_httpx.post.return_value = _mock_response(json_data={
            "access_token": "refreshed-token",
            "refresh_token": "new-ref",
            "expires_in": 3600,
        })

        token = await svc.get_valid_token()
        assert token == "refreshed-token"

    @pytest.mark.asyncio
    async def test_raises_when_disconnected(self, svc):
        """Raises RuntimeError when no tokens are stored."""
        with pytest.raises(RuntimeError, match="Not connected to Jira"):
            await svc.get_valid_token()


# ===========================================================================
# get_status / disconnect
# ===========================================================================

class TestGetStatusAndDisconnect:
    def test_status_disconnected(self, svc):
        result = svc.get_status()
        assert result == {"connected": False}

    def test_status_connected(self, svc):
        _inject_tokens(svc)
        result = svc.get_status()
        assert result["connected"] is True
        assert result["cloud_id"] == "cloud-123"
        assert result["site_url"] == "https://mysite.atlassian.net"

    def test_disconnect_clears_tokens(self, svc):
        _inject_tokens(svc)
        assert svc._tokens is not None

        svc.disconnect()

        assert svc._tokens is None
        assert svc._token_expires_at == 0
        assert svc.get_status() == {"connected": False}


# ===========================================================================
# _api_request
# ===========================================================================

class TestApiRequest:
    @pytest.mark.asyncio
    async def test_successful_get_request(self, svc, mock_httpx):
        """A standard GET request returns parsed JSON."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={"result": "ok"})

        data = await svc._api_request("GET", "/test")
        assert data == {"result": "ok"}

        # Verify authorization header was set
        call_kwargs = mock_httpx.request.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers", {})
        assert "Bearer access-abc" in headers.get("Authorization", "")

    @pytest.mark.asyncio
    async def test_auto_refresh_on_401(self, svc, mock_httpx):
        """On 401, refresh the token and retry the request."""
        _inject_tokens(svc)

        # First request returns 401, retry returns 200
        first_resp = _mock_response(status_code=401, json_data={})
        # Override raise_for_status to NOT raise (the service checks status_code manually)
        first_resp.raise_for_status = MagicMock()
        second_resp = _mock_response(json_data={"ok": True})

        mock_httpx.request.side_effect = [first_resp, second_resp]
        mock_httpx.post.return_value = _mock_response(json_data={
            "access_token": "refreshed",
            "expires_in": 3600,
        })

        data = await svc._api_request("GET", "/resource")
        assert data == {"ok": True}
        assert mock_httpx.request.call_count == 2

    @pytest.mark.asyncio
    async def test_error_parsing_jira_error_body(self, svc, mock_httpx):
        """Jira error bodies with errorMessages and field errors are parsed."""
        _inject_tokens(svc)

        error_body = {
            "errorMessages": ["Issue does not exist"],
            "errors": {"summary": "Summary is required"},
        }
        mock_httpx.request.return_value = _mock_response(
            status_code=400, json_data=error_body,
        )

        with pytest.raises(RuntimeError, match="Issue does not exist.*summary: Summary is required"):
            await svc._api_request("GET", "/issue/FAKE-1")

    @pytest.mark.asyncio
    async def test_error_with_non_json_body(self, svc, mock_httpx):
        """When error response is not JSON, the text body is included."""
        _inject_tokens(svc)

        resp = _mock_response(status_code=500, text="Internal Server Error")
        resp.json.side_effect = Exception("not JSON")
        mock_httpx.request.return_value = resp

        with pytest.raises(RuntimeError, match="Jira API error \\(500\\)"):
            await svc._api_request("GET", "/broken")

    @pytest.mark.asyncio
    async def test_raises_when_not_connected(self, svc, mock_httpx):
        """_api_request raises if no tokens/cloudId are stored."""
        with pytest.raises(RuntimeError, match="Not connected to Jira"):
            await svc._api_request("GET", "/anything")

    @pytest.mark.asyncio
    async def test_returns_none_for_empty_content(self, svc, mock_httpx):
        """When response has no content, returns None."""
        _inject_tokens(svc)
        resp = _mock_response(json_data={"key": "val"})
        resp.content = b""
        mock_httpx.request.return_value = resp

        result = await svc._api_request("DELETE", "/issue/X-1")
        assert result is None


# ===========================================================================
# get_projects
# ===========================================================================

class TestGetProjects:
    @pytest.mark.asyncio
    async def test_returns_list_of_projects(self, svc, mock_httpx):
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data=[
            {"id": "10001", "key": "PROJ", "name": "Project One", "style": "classic"},
            {"id": "10002", "key": "WEB", "name": "Web App"},
        ])

        projects = await svc.get_projects()

        assert len(projects) == 2
        assert projects[0].key == "PROJ"
        assert projects[0].name == "Project One"
        assert projects[0].style == "classic"
        assert projects[1].style == ""  # missing style defaults to ""


# ===========================================================================
# get_issue_types
# ===========================================================================

class TestGetIssueTypes:
    @pytest.mark.asyncio
    async def test_returns_deduplicated_issue_types(self, svc, mock_httpx):
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data=[
            {"id": "1", "name": "Bug", "subtask": False},
            {"id": "2", "name": "Story", "subtask": False},
            {"id": "1", "name": "Bug", "subtask": False},  # duplicate
        ])

        types = await svc.get_issue_types("PROJ")

        assert len(types) == 2
        assert types[0].name == "Bug"
        assert types[1].name == "Story"

    @pytest.mark.asyncio
    async def test_subtask_flag_preserved(self, svc, mock_httpx):
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data=[
            {"id": "3", "name": "Sub-task", "subtask": True},
        ])

        types = await svc.get_issue_types("PROJ")
        assert types[0].subtask is True


# ===========================================================================
# create_issue
# ===========================================================================

class TestCreateIssue:
    @pytest.mark.asyncio
    async def test_create_basic_issue(self, svc, mock_httpx):
        """Creates an issue with summary and project key."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "12345",
            "key": "PROJ-42",
            "self": "https://api.atlassian.com/rest/api/3/issue/12345",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="Test issue",
            issue_type="Task",
        )
        issue = await svc.create_issue(req)

        assert issue.id == "12345"
        assert issue.key == "PROJ-42"
        assert "PROJ-42" in issue.browse_url

    @pytest.mark.asyncio
    async def test_create_issue_numeric_project_key(self, svc, mock_httpx):
        """Numeric project_key is sent as {id: ...} instead of {key: ...}."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "100", "key": "NUM-1",
        })

        req = CreateIssueRequest(
            project_key="10001",
            summary="Numeric project",
        )
        await svc.create_issue(req)

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert payload["fields"]["project"] == {"id": "10001"}

    @pytest.mark.asyncio
    async def test_create_issue_string_project_key(self, svc, mock_httpx):
        """String project_key is sent as {key: ...}."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "100", "key": "PROJ-1",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="String project",
        )
        await svc.create_issue(req)

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert payload["fields"]["project"] == {"key": "PROJ"}

    @pytest.mark.asyncio
    async def test_create_issue_with_description_adf(self, svc, mock_httpx):
        """Description is formatted as ADF (Atlassian Document Format)."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "200", "key": "PROJ-2",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="With desc",
            description="This is a test description",
        )
        await svc.create_issue(req)

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        desc = payload["fields"]["description"]
        assert desc["type"] == "doc"
        assert desc["version"] == 1
        assert desc["content"][0]["content"][0]["text"] == "This is a test description"

    @pytest.mark.asyncio
    async def test_create_issue_empty_description_not_included(self, svc, mock_httpx):
        """Empty description is not sent in the payload."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "300", "key": "PROJ-3",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="No desc",
            description="",
        )
        await svc.create_issue(req)

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert "description" not in payload["fields"]

    @pytest.mark.asyncio
    async def test_create_issue_with_priority(self, svc, mock_httpx):
        """Priority is set correctly (name-based for non-numeric)."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "400", "key": "PROJ-4",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="High priority",
            priority="High",
        )
        await svc.create_issue(req)

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert payload["fields"]["priority"] == {"name": "High"}

    @pytest.mark.asyncio
    async def test_create_issue_with_numeric_priority(self, svc, mock_httpx):
        """Numeric priority is sent as {id: ...}."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "401", "key": "PROJ-5",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="Priority by id",
            priority="3",
        )
        await svc.create_issue(req)

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert payload["fields"]["priority"] == {"id": "3"}

    @pytest.mark.asyncio
    async def test_create_issue_with_team(self, svc, mock_httpx):
        """Team is set on the custom field with numeric conversion for digits."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "500", "key": "PROJ-6",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="With team",
            team="42",
        )
        await svc.create_issue(req, team_field_key="customfield_10124")

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        # Numeric team ID should be converted to int
        assert payload["fields"]["customfield_10124"] == 42

    @pytest.mark.asyncio
    async def test_create_issue_with_string_team(self, svc, mock_httpx):
        """Non-numeric team value is kept as string."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "501", "key": "PROJ-7",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="String team",
            team="team-uuid-abc",
        )
        await svc.create_issue(req, team_field_key="customfield_10001")

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert payload["fields"]["customfield_10001"] == "team-uuid-abc"

    @pytest.mark.asyncio
    async def test_create_issue_team_ignored_without_field_key(self, svc, mock_httpx):
        """Team is not included when team_field_key is empty."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "502", "key": "PROJ-8",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="No team field key",
            team="42",
        )
        await svc.create_issue(req, team_field_key="")

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        # No customfield should be set
        field_keys = payload["fields"].keys()
        assert all(not k.startswith("customfield") for k in field_keys)

    @pytest.mark.asyncio
    async def test_create_issue_with_components(self, svc, mock_httpx):
        """Components are included as list of name/id refs."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "600", "key": "PROJ-9",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="With components",
            components=["Backend", "10050"],
        )
        await svc.create_issue(req)

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        components = payload["fields"]["components"]
        assert components == [{"name": "Backend"}, {"id": "10050"}]

    @pytest.mark.asyncio
    async def test_create_issue_numeric_issue_type(self, svc, mock_httpx):
        """Numeric issue_type is sent as {id: ...} instead of {name: ...}."""
        _inject_tokens(svc)
        mock_httpx.request.return_value = _mock_response(json_data={
            "id": "700", "key": "PROJ-10",
        })

        req = CreateIssueRequest(
            project_key="PROJ",
            summary="By type id",
            issue_type="10001",
        )
        await svc.create_issue(req)

        call_kwargs = mock_httpx.request.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json", {})
        assert payload["fields"]["issuetype"] == {"id": "10001"}


# ===========================================================================
# Constructor / static_teams
# ===========================================================================

class TestConstructor:
    def test_default_no_static_teams(self):
        svc = _make_service()
        assert svc._team_cache == []
        assert svc._team_field_key == ""
        assert svc._static_teams is False

    def test_with_static_teams(self):
        teams = [
            JiraFieldOption(id="uuid-1", name="Alpha"),
            JiraFieldOption(id="uuid-2", name="Beta"),
        ]
        svc = _make_service(static_teams=teams)
        assert len(svc._team_cache) == 2
        assert svc._team_field_key == "customfield_10001"
        assert svc._static_teams is True


# ===========================================================================
# refresh_token_for_client
# ===========================================================================

class TestRefreshTokenForClient:
    @pytest.mark.asyncio
    async def test_successful_refresh(self, svc, mock_httpx):
        """Returns new token pair on successful refresh."""
        mock_httpx.post.return_value = _mock_response(json_data={
            "access_token": "new-acc",
            "refresh_token": "new-ref",
            "expires_in": 3600,
        })

        result = await svc.refresh_token_for_client("old-refresh-token")

        assert result["access_token"] == "new-acc"
        assert result["refresh_token"] == "new-ref"
        assert result["expires_in"] == 3600

        # Verify it sent the right payload
        call_kwargs = mock_httpx.post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["grant_type"] == "refresh_token"
        assert payload["client_id"] == "test-client-id"
        assert payload["client_secret"] == "test-client-secret"
        assert payload["refresh_token"] == "old-refresh-token"

    @pytest.mark.asyncio
    async def test_refresh_preserves_old_refresh_token_when_not_rotated(self, svc, mock_httpx):
        """If Atlassian doesn't return a new refresh_token, keep the old one."""
        mock_httpx.post.return_value = _mock_response(json_data={
            "access_token": "new-acc",
            "expires_in": 3600,
        })

        result = await svc.refresh_token_for_client("keep-this-token")

        assert result["refresh_token"] == "keep-this-token"

    @pytest.mark.asyncio
    async def test_refresh_does_not_modify_server_state(self, svc, mock_httpx):
        """Server-side in-memory tokens should NOT be updated."""
        _inject_tokens(svc, access_token="server-acc", refresh_token="server-ref")

        mock_httpx.post.return_value = _mock_response(json_data={
            "access_token": "client-new-acc",
            "refresh_token": "client-new-ref",
            "expires_in": 3600,
        })

        await svc.refresh_token_for_client("client-ref")

        # Server state unchanged
        assert svc._tokens.access_token == "server-acc"
        assert svc._tokens.refresh_token == "server-ref"

    @pytest.mark.asyncio
    async def test_refresh_failure_raises_runtime_error(self, svc, mock_httpx):
        """Atlassian returns an error (e.g. expired refresh token)."""
        error_resp = MagicMock()
        error_resp.status_code = 400
        error_resp.json.return_value = {"error": "invalid_grant", "error_description": "Token has been revoked"}
        error_resp.text = '{"error": "invalid_grant"}'
        mock_httpx.post.return_value = error_resp

        with pytest.raises(RuntimeError, match="Token refresh failed"):
            await svc.refresh_token_for_client("expired-token")

    @pytest.mark.asyncio
    async def test_refresh_failure_with_non_json_response(self, svc, mock_httpx):
        """Atlassian returns a non-JSON error body."""
        error_resp = MagicMock()
        error_resp.status_code = 500
        error_resp.json.side_effect = ValueError("Not JSON")
        error_resp.text = "Internal Server Error"
        mock_httpx.post.return_value = error_resp

        with pytest.raises(RuntimeError, match="Token refresh failed.*Internal Server Error"):
            await svc.refresh_token_for_client("some-token")
