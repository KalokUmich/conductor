"""Unit tests for Jira integration tools (agent-facing tool functions).

Tests cover all four Jira tools:
- jira_search
- jira_get_issue
- jira_create_issue
- jira_list_projects

All HTTP calls are mocked at the httpx level.
"""
from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

import pytest

from app.integrations.jira.tools import (
    JIRA_TOOL_REGISTRY,
    init_jira_tools,
    jira_create_issue,
    jira_get_issue,
    jira_list_projects,
    jira_search,
    jira_update_issue,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_mock_service():
    """Create a mock JiraOAuthService with valid tokens."""
    svc = MagicMock()
    svc.client_id = "test-id"
    svc.client_secret = "test-secret"
    svc._tokens = MagicMock()
    svc._tokens.access_token = "test-token"
    svc._tokens.refresh_token = "test-refresh"
    svc._tokens.cloud_id = "cloud-123"
    svc._tokens.site_url = "https://test.atlassian.net"
    svc._tokens.expires_in = 3600
    svc._token_expires_at = time.time() + 3600
    svc._team_field_key = "customfield_10001"
    return svc


def _mock_response(status_code=200, json_data=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data or {}
    resp.text = text
    resp.content = b"content" if json_data else b""
    return resp


@pytest.fixture(autouse=True)
def setup_jira_tools():
    """Initialize Jira tools with a mock service for every test."""
    svc = _make_mock_service()
    init_jira_tools(svc, {"DEV", "FO", "HELP"})
    yield svc
    # Reset
    init_jira_tools(None, set())


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_all_tools_registered(self):
        assert "jira_search" in JIRA_TOOL_REGISTRY
        assert "jira_get_issue" in JIRA_TOOL_REGISTRY
        assert "jira_create_issue" in JIRA_TOOL_REGISTRY
        assert "jira_list_projects" in JIRA_TOOL_REGISTRY


# ---------------------------------------------------------------------------
# jira_search
# ---------------------------------------------------------------------------

class TestJiraSearch:
    @patch("app.integrations.jira.tools.httpx.post")
    def test_search_free_text(self, mock_post):
        mock_post.return_value = _mock_response(json_data={
            "total": 2,
            "issues": [
                {
                    "key": "DEV-100",
                    "fields": {
                        "summary": "Fix auth bug",
                        "status": {"name": "In Progress"},
                        "priority": {"name": "High"},
                        "issuetype": {"name": "Bug"},
                        "assignee": {"displayName": "Alice"},
                    },
                },
                {
                    "key": "DEV-101",
                    "fields": {
                        "summary": "Add retry logic",
                        "status": {"name": "To Do"},
                        "priority": None,
                        "issuetype": {"name": "Story"},
                        "assignee": None,
                    },
                },
            ],
        })

        result = jira_search(workspace="/tmp", query="auth bug")
        assert result.success is True
        assert len(result.data["issues"]) == 2
        assert result.data["issues"][0]["key"] == "DEV-100"
        assert result.data["issues"][1]["assignee"] == ""

        # Should use text search (not raw JQL) — now sent via POST body
        call_kwargs = mock_post.call_args.kwargs.get("json", {})
        assert 'text ~' in call_kwargs["jql"]

    @patch("app.integrations.jira.tools.httpx.post")
    def test_search_jql_passthrough(self, mock_post):
        mock_post.return_value = _mock_response(json_data={
            "total": 0,
            "issues": [],
        })

        result = jira_search(workspace="/tmp", query='project = DEV AND status = "In Progress"')
        assert result.success is True

        # Should pass JQL through directly
        call_kwargs = mock_post.call_args.kwargs.get("json", {})
        assert "project = DEV" in call_kwargs["jql"]

    @patch("app.integrations.jira.tools.httpx.post")
    def test_search_api_error(self, mock_post):
        mock_post.return_value = _mock_response(status_code=400, text="Bad JQL")

        result = jira_search(workspace="/tmp", query="bad query")
        assert result.success is False
        assert "400" in result.error

    def test_search_no_service(self):
        init_jira_tools(None)
        result = jira_search(workspace="/tmp", query="test")
        assert result.success is False
        assert "not configured" in result.error


# ---------------------------------------------------------------------------
# jira_get_issue
# ---------------------------------------------------------------------------

class TestJiraGetIssue:
    @patch("app.integrations.jira.tools.httpx.get")
    def test_get_issue_full(self, mock_get):
        mock_get.return_value = _mock_response(json_data={
            "key": "DEV-42",
            "fields": {
                "summary": "Implement retry logic",
                "description": {
                    "type": "doc", "version": 1,
                    "content": [{"type": "paragraph", "content": [
                        {"type": "text", "text": "Add retry to webhook"}
                    ]}],
                },
                "status": {"name": "In Progress"},
                "priority": {"name": "High"},
                "issuetype": {"name": "Story"},
                "assignee": {"displayName": "Bob"},
                "components": [{"name": "JBE"}, {"name": "Render API"}],
                "labels": ["backend"],
                "created": "2026-03-01T10:00:00Z",
                "updated": "2026-03-15T14:00:00Z",
                "parent": {"key": "DEV-40"},
                "subtasks": [
                    {"key": "DEV-43", "fields": {"summary": "Sub 1", "status": {"name": "Done"}}},
                ],
                "comment": {
                    "comments": [
                        {
                            "author": {"displayName": "Alice"},
                            "created": "2026-03-02T10:00:00Z",
                            "body": "Looks good",
                        },
                    ],
                },
            },
        })

        result = jira_get_issue(workspace="/tmp", issue_key="DEV-42")
        assert result.success is True
        assert result.data["key"] == "DEV-42"
        assert result.data["description"] == "Add retry to webhook"
        assert result.data["components"] == ["JBE", "Render API"]
        assert len(result.data["subtasks"]) == 1
        assert result.data["subtasks"][0]["key"] == "DEV-43"
        assert len(result.data["comments"]) == 1
        assert result.data["parent"] == "DEV-40"
        assert "test.atlassian.net/browse/DEV-42" in result.data["browse_url"]

    @patch("app.integrations.jira.tools.httpx.get")
    def test_get_issue_not_found(self, mock_get):
        mock_get.return_value = _mock_response(status_code=404, text="Issue not found")

        result = jira_get_issue(workspace="/tmp", issue_key="DEV-999")
        assert result.success is False
        assert "404" in result.error


# ---------------------------------------------------------------------------
# jira_create_issue
# ---------------------------------------------------------------------------

class TestJiraCreateIssue:
    @patch("app.integrations.jira.tools.httpx.post")
    def test_create_basic(self, mock_post):
        mock_post.return_value = _mock_response(json_data={
            "id": "12345",
            "key": "DEV-200",
        })

        result = jira_create_issue(
            workspace="/tmp",
            project_key="DEV",
            summary="New feature",
            description="Add the thing",
            issue_type="Story",
        )
        assert result.success is True
        assert result.data["key"] == "DEV-200"
        assert "test.atlassian.net/browse/DEV-200" in result.data["browse_url"]

        # Verify payload
        call_kwargs = mock_post.call_args.kwargs
        fields = call_kwargs["json"]["fields"]
        assert fields["project"] == {"key": "DEV"}
        assert fields["summary"] == "New feature"
        assert fields["issuetype"] == {"name": "Story"}

    @patch("app.integrations.jira.tools.httpx.post")
    def test_create_with_components_and_team(self, mock_post):
        mock_post.return_value = _mock_response(json_data={
            "id": "12346",
            "key": "DEV-201",
        })

        result = jira_create_issue(
            workspace="/tmp",
            project_key="DEV",
            summary="Fix bug",
            components=["JBE", "Render API"],
            team="Platform",
        )
        assert result.success is True

        fields = mock_post.call_args.kwargs["json"]["fields"]
        assert fields["components"] == [{"name": "JBE"}, {"name": "Render API"}]
        assert fields["customfield_10001"] == "Platform"

    @patch("app.integrations.jira.tools.httpx.post")
    def test_create_api_error(self, mock_post):
        mock_post.return_value = _mock_response(
            status_code=400, text='{"errors":{"summary":"required"}}'
        )

        result = jira_create_issue(
            workspace="/tmp",
            project_key="DEV",
            summary="",
        )
        assert result.success is False
        assert "400" in result.error


# ---------------------------------------------------------------------------
# jira_list_projects
# ---------------------------------------------------------------------------

class TestJiraListProjects:
    @patch("app.integrations.jira.tools.httpx.get")
    def test_list_filtered(self, mock_get):
        mock_get.return_value = _mock_response(json_data=[
            {"key": "DEV", "name": "Development", "id": "10040"},
            {"key": "FO", "name": "FinOps", "id": "10033"},
            {"key": "OLD", "name": "Legacy", "id": "10000"},
            {"key": "HELP", "name": "Helpdesk", "id": "10042"},
        ])

        result = jira_list_projects(workspace="/tmp")
        assert result.success is True
        keys = {p["key"] for p in result.data["projects"]}
        assert keys == {"DEV", "FO", "HELP"}
        assert "OLD" not in keys

    @patch("app.integrations.jira.tools.httpx.get")
    def test_list_no_filter(self, mock_get):
        init_jira_tools(_make_mock_service(), set())  # empty filter

        mock_get.return_value = _mock_response(json_data=[
            {"key": "DEV", "name": "Development", "id": "10040"},
            {"key": "OLD", "name": "Legacy", "id": "10000"},
        ])

        result = jira_list_projects(workspace="/tmp")
        assert result.success is True
        assert len(result.data["projects"]) == 2


# ---------------------------------------------------------------------------
# Token refresh
# ---------------------------------------------------------------------------

class TestTokenRefresh:
    @patch("app.integrations.jira.tools.httpx.post")
    def test_auto_refresh_on_expired_token(self, mock_post, setup_jira_tools):
        # Expire the token
        setup_jira_tools._token_expires_at = time.time() - 100

        # httpx.post is called twice: first for refresh, then for search/jql
        refresh_resp = _mock_response(json_data={
            "access_token": "new-token",
            "refresh_token": "new-refresh",
            "expires_in": 3600,
        })
        search_resp = _mock_response(json_data={
            "total": 0,
            "issues": [],
        })
        mock_post.side_effect = [refresh_resp, search_resp]

        result = jira_search(workspace="/tmp", query="test")
        assert result.success is True

        # Verify refresh was called (first call) + search (second call)
        assert mock_post.call_count == 2
        assert setup_jira_tools._tokens.access_token == "new-token"


# ---------------------------------------------------------------------------
# jira_update_issue
# ---------------------------------------------------------------------------

class TestJiraUpdateIssue:
    @patch("app.integrations.jira.tools.httpx.post")
    @patch("app.integrations.jira.tools.httpx.get")
    def test_transition_success(self, mock_get, mock_post):
        # GET transitions
        mock_get.return_value = _mock_response(json_data={
            "transitions": [
                {"id": "21", "name": "Start Progress", "to": {"name": "In Progress"}},
                {"id": "31", "name": "Done", "to": {"name": "Done"}},
            ],
        })
        # POST transition
        mock_post.return_value = _mock_response(json_data=None)
        mock_post.return_value.content = b""

        result = jira_update_issue(workspace="/tmp", issue_key="DEV-1", transition_to="In Progress")
        assert result.success is True
        assert "In Progress" in result.data["actions"][0]

    def test_transition_blocked_done(self):
        """Agent cannot transition to Done."""
        result = jira_update_issue(workspace="/tmp", issue_key="DEV-1", transition_to="Done")
        assert result.success is False
        assert "manual user action" in result.error

    def test_transition_blocked_closed(self):
        result = jira_update_issue(workspace="/tmp", issue_key="DEV-1", transition_to="Closed")
        assert result.success is False
        assert "manual user action" in result.error

    def test_transition_blocked_resolved(self):
        result = jira_update_issue(workspace="/tmp", issue_key="DEV-1", transition_to="Resolved")
        assert result.success is False
        assert "manual user action" in result.error

    @patch("app.integrations.jira.tools.httpx.post")
    def test_add_comment(self, mock_post):
        mock_post.return_value = _mock_response(json_data={"id": "999", "created": "2026-04-01"})

        result = jira_update_issue(workspace="/tmp", issue_key="DEV-1", comment="Found the root cause at auth.py:42")
        assert result.success is True
        assert "comment added" in result.data["actions"][0]

    @patch("app.integrations.jira.tools.httpx.put")
    def test_update_priority(self, mock_put):
        mock_put.return_value = _mock_response(json_data=None)
        mock_put.return_value.content = b""
        mock_put.return_value.status_code = 204

        result = jira_update_issue(workspace="/tmp", issue_key="DEV-1", priority="High")
        assert result.success is True
        assert "fields updated" in result.data["actions"][0]

    def test_no_actions_error(self):
        result = jira_update_issue(workspace="/tmp", issue_key="DEV-1")
        assert result.success is False
        assert "No update actions" in result.error

    def test_registered(self):
        assert "jira_update_issue" in JIRA_TOOL_REGISTRY
