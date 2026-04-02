"""Jira integration tools for the agent loop.

These tools are executed by the agent tool executor (in a thread via
asyncio.to_thread), so they use synchronous httpx calls against the
Jira REST API.  Token management is delegated to the JiraOAuthService
instance stored in app.state.

Module-level ``_jira_service`` is set by ``init_jira_tools()`` during
app startup.  If not set, all tools return a clear error message.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

import httpx

from app.code_tools.schemas import ToolResult

logger = logging.getLogger(__name__)

# Set by init_jira_tools() during app startup
_jira_service: Any = None
_allowed_projects: set = set()

JIRA_API_BASE = "https://api.atlassian.com/ex/jira"


def init_jira_tools(jira_service: Any, allowed_projects: set | None = None) -> None:
    """Wire the Jira service into the tool module. Called once at startup."""
    global _jira_service, _allowed_projects
    _jira_service = jira_service
    _allowed_projects = allowed_projects or set()
    logger.info("Jira tools initialized (service=%s)", type(jira_service).__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_token() -> str:
    """Get a valid access token, refreshing synchronously if needed."""
    if not _jira_service:
        raise RuntimeError("Jira integration is not configured")
    if not _jira_service._tokens:
        raise RuntimeError("Not connected to Jira — please authenticate first")

    # Check if refresh is needed (60s margin, matching service.py)
    if time.time() > (_jira_service._token_expires_at - 60):
        # Sync refresh using httpx
        resp = httpx.post(
            "https://auth.atlassian.com/oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_id": _jira_service.client_id,
                "client_secret": _jira_service.client_secret,
                "refresh_token": _jira_service._tokens.refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        _jira_service._tokens.access_token = data["access_token"]
        _jira_service._tokens.refresh_token = data.get(
            "refresh_token", _jira_service._tokens.refresh_token
        )
        _jira_service._tokens.expires_in = data.get("expires_in", 3600)
        _jira_service._token_expires_at = time.time() + _jira_service._tokens.expires_in
        logger.info("Jira tools: token refreshed (sync)")

    return _jira_service._tokens.access_token


def _api_base() -> str:
    tokens = _jira_service._tokens
    if not tokens or not tokens.cloud_id:
        raise RuntimeError("Not connected to Jira")
    return f"{JIRA_API_BASE}/{tokens.cloud_id}/rest/api/3"


def _jira_get(path: str, params: Dict[str, str] | None = None) -> Any:
    """Sync GET request to Jira REST API."""
    token = _get_token()
    resp = httpx.get(
        f"{_api_base()}{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Jira API error ({resp.status_code}): {resp.text[:500]}")
    return resp.json() if resp.content else None


def _jira_post(path: str, json_body: Dict[str, Any]) -> Any:
    """Sync POST request to Jira REST API."""
    token = _get_token()
    resp = httpx.post(
        f"{_api_base()}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json=json_body,
        timeout=30.0,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"Jira API error ({resp.status_code}): {resp.text[:500]}")
    return resp.json() if resp.content else None


def _adf_to_text(adf: Any) -> str:
    """Convert Atlassian Document Format to plain text."""
    parts: List[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") == "text":
                parts.append(node.get("text", ""))
            for child in node.get("content", []):
                _walk(child)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(adf)
    return " ".join(parts).strip()


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def jira_search(workspace: str, query: str, max_results: int = 10) -> ToolResult:
    """Search Jira issues using JQL or free text."""
    try:
        # If query looks like JQL (contains operators), use it directly
        jql_keywords = {"=", "!=", "~", "IN", "AND", "OR", "ORDER BY", "NOT"}
        is_jql = any(kw in query.upper() for kw in jql_keywords)

        jql = query if is_jql else f'text ~ "{query}" ORDER BY updated DESC'

        data = _jira_post("/search/jql", {
            "jql": jql,
            "maxResults": max_results,
            "fields": ["summary", "status", "assignee", "priority", "issuetype"],
        })
        issues = data.get("issues", [])
        site_url = _jira_service._tokens.site_url if _jira_service._tokens else ""

        results = []
        for issue in issues:
            fields = issue.get("fields", {})
            results.append({
                "key": issue["key"],
                "summary": fields.get("summary", ""),
                "status": fields.get("status", {}).get("name", ""),
                "priority": fields.get("priority", {}).get("name", "") if fields.get("priority") else "",
                "issuetype": fields.get("issuetype", {}).get("name", "") if fields.get("issuetype") else "",
                "assignee": fields.get("assignee", {}).get("displayName", "") if fields.get("assignee") else "",
                "browse_url": f"{site_url}/browse/{issue['key']}" if site_url else "",
            })

        return ToolResult(tool_name="jira_search", success=True, data={
            "query": jql,
            "total": data.get("total", len(results)),
            "issues": results,
        })
    except Exception as exc:
        return ToolResult(tool_name="jira_search", success=False, error=str(exc))


def jira_get_issue(workspace: str, issue_key: str) -> ToolResult:
    """Get full details of a Jira issue."""
    try:
        data = _jira_get(f"/issue/{issue_key}", params={
            "fields": "summary,description,status,priority,assignee,issuetype,"
                      "components,labels,created,updated,comment,subtasks,parent",
        })
        fields = data.get("fields", {})

        # Parse description (ADF → plain text)
        desc_raw = fields.get("description")
        description = ""
        if isinstance(desc_raw, dict):
            description = _adf_to_text(desc_raw)
        elif isinstance(desc_raw, str):
            description = desc_raw

        # Parse comments (last 5)
        comment_data = fields.get("comment", {})
        comments = []
        for c in comment_data.get("comments", [])[-5:]:
            body = c.get("body", "")
            if isinstance(body, dict):
                body = _adf_to_text(body)
            comments.append({
                "author": c.get("author", {}).get("displayName", ""),
                "created": c.get("created", ""),
                "body": body[:500],  # Truncate long comments
            })

        # Parse subtasks
        subtasks = []
        for st in fields.get("subtasks", []):
            st_fields = st.get("fields", {})
            subtasks.append({
                "key": st["key"],
                "summary": st_fields.get("summary", ""),
                "status": st_fields.get("status", {}).get("name", ""),
            })

        site_url = _jira_service._tokens.site_url if _jira_service._tokens else ""

        return ToolResult(tool_name="jira_get_issue", success=True, data={
            "key": data["key"],
            "summary": fields.get("summary", ""),
            "description": description,
            "status": fields.get("status", {}).get("name", ""),
            "priority": fields.get("priority", {}).get("name", "") if fields.get("priority") else "",
            "issuetype": fields.get("issuetype", {}).get("name", "") if fields.get("issuetype") else "",
            "assignee": fields.get("assignee", {}).get("displayName", "") if fields.get("assignee") else "",
            "components": [c.get("name", "") for c in fields.get("components", [])],
            "labels": fields.get("labels", []),
            "created": fields.get("created", ""),
            "updated": fields.get("updated", ""),
            "parent": fields.get("parent", {}).get("key", "") if fields.get("parent") else "",
            "subtasks": subtasks,
            "comments": comments,
            "browse_url": f"{site_url}/browse/{issue_key}" if site_url else "",
        })
    except Exception as exc:
        return ToolResult(tool_name="jira_get_issue", success=False, error=str(exc))


def jira_create_issue(
    workspace: str,
    project_key: str,
    summary: str,
    description: str = "",
    issue_type: str = "Software Task",
    priority: str = "",
    components: Optional[List[str]] = None,
    team: str = "",
    parent_key: str = "",
) -> ToolResult:
    """Create a Jira issue. Supports hierarchy: Epic → sub-tasks via parent_key."""
    try:
        components = components or []

        # Build project reference
        project_ref = {"id": project_key} if project_key.isdigit() else {"key": project_key}
        issuetype_ref = {"id": issue_type} if issue_type.isdigit() else {"name": issue_type}

        fields: Dict[str, Any] = {
            "project": project_ref,
            "issuetype": issuetype_ref,
            "summary": summary,
        }

        # Description: support multi-paragraph with code blocks
        if description.strip():
            content_blocks = []
            for block in description.split("\n\n"):
                block = block.strip()
                if not block:
                    continue
                if block.startswith("```"):
                    # Code block → ADF codeBlock
                    code_text = block.strip("`").strip()
                    # Remove language hint on first line if present
                    lines = code_text.split("\n", 1)
                    if len(lines) > 1 and not lines[0].strip().startswith(("/", "#", "import", "from", "def", "class")):
                        code_text = lines[1]
                    content_blocks.append({
                        "type": "codeBlock",
                        "content": [{"type": "text", "text": code_text}],
                    })
                else:
                    content_blocks.append({
                        "type": "paragraph",
                        "content": [{"type": "text", "text": block}],
                    })

            if not content_blocks:
                content_blocks = [{"type": "paragraph", "content": [{"type": "text", "text": description}]}]

            fields["description"] = {
                "version": 1,
                "type": "doc",
                "content": content_blocks,
            }

        if priority:
            fields["priority"] = {"id": priority} if priority.isdigit() else {"name": priority}

        if components:
            fields["components"] = [
                {"id": c} if c.isdigit() else {"name": c} for c in components
            ]

        if team and _jira_service and _jira_service._team_field_key:
            fields[_jira_service._team_field_key] = int(team) if team.isdigit() else team

        # Parent link (for sub-tasks under an Epic)
        if parent_key:
            fields["parent"] = {"key": parent_key}

        data = _jira_post("/issue", {"fields": fields})
        site_url = _jira_service._tokens.site_url if _jira_service._tokens else ""
        browse_url = f"{site_url}/browse/{data['key']}" if site_url else ""

        return ToolResult(tool_name="jira_create_issue", success=True, data={
            "key": data["key"],
            "id": data["id"],
            "browse_url": browse_url,
            "summary": summary,
            "issue_type": issue_type,
            "parent_key": parent_key or None,
        })
    except Exception as exc:
        return ToolResult(tool_name="jira_create_issue", success=False, error=str(exc))


def jira_update_issue(
    workspace: str,
    issue_key: str,
    transition_to: str = "",
    comment: str = "",
    priority: str = "",
    labels_add: Optional[List[str]] = None,
) -> ToolResult:
    """Update a Jira issue: transition status, add comment, change fields."""
    try:
        actions_taken = []

        # 1. Status transition
        if transition_to:
            # Safety check: block Done/Closed/Resolved
            blocked = {"done", "closed", "resolved"}
            if transition_to.lower() in blocked:
                return ToolResult(
                    tool_name="jira_update_issue", success=False,
                    error=f"Cannot transition to '{transition_to}' — Done/Closed/Resolved require manual user action. "
                          f"Ask the user to close the ticket themselves.",
                )

            # Get available transitions and find matching one
            transitions = _jira_get(f"/issue/{issue_key}/transitions")
            available = transitions.get("transitions", [])
            target = None
            for t in available:
                to_name = t.get("to", {}).get("name", "")
                if to_name.lower() == transition_to.lower():
                    target = t
                    break

            if not target:
                avail_names = [t.get("to", {}).get("name", "") for t in available]
                return ToolResult(
                    tool_name="jira_update_issue", success=False,
                    error=f"Transition to '{transition_to}' not available for {issue_key}. "
                          f"Available: {avail_names}",
                )

            _jira_post(f"/issue/{issue_key}/transitions", {
                "transition": {"id": target["id"]},
            })
            actions_taken.append(f"status → {transition_to}")

        # 2. Add comment
        if comment:
            adf_body = {
                "version": 1, "type": "doc",
                "content": [{"type": "paragraph", "content": [{"type": "text", "text": comment}]}],
            }
            _jira_post(f"/issue/{issue_key}/comment", {"body": adf_body})
            actions_taken.append("comment added")

        # 3. Update fields
        fields: Dict[str, Any] = {}
        if priority:
            fields["priority"] = {"name": priority}
        if labels_add:
            # Jira field update for labels uses "add" operation
            pass  # labels require special edit format, handle via update endpoint

        if fields:
            token = _get_token()
            resp = httpx.put(
                f"{_api_base()}/issue/{issue_key}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=30.0,
            )
            if resp.status_code >= 400:
                return ToolResult(
                    tool_name="jira_update_issue", success=False,
                    error=f"Failed to update fields: {resp.text[:300]}",
                )
            actions_taken.append(f"fields updated: {list(fields.keys())}")

        if labels_add:
            token = _get_token()
            resp = httpx.put(
                f"{_api_base()}/issue/{issue_key}",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json={"update": {"labels": [{"add": label} for label in labels_add]}},
                timeout=30.0,
            )
            if resp.status_code >= 400:
                return ToolResult(
                    tool_name="jira_update_issue", success=False,
                    error=f"Failed to add labels: {resp.text[:300]}",
                )
            actions_taken.append(f"labels added: {labels_add}")

        if not actions_taken:
            return ToolResult(tool_name="jira_update_issue", success=False, error="No update actions specified")

        return ToolResult(tool_name="jira_update_issue", success=True, data={
            "issue_key": issue_key,
            "actions": actions_taken,
        })
    except Exception as exc:
        return ToolResult(tool_name="jira_update_issue", success=False, error=str(exc))


def jira_list_projects(workspace: str) -> ToolResult:
    """List available Jira projects with their issue types."""
    try:
        data = _jira_get("/project")
        projects = []
        for p in data:
            key = p.get("key", "")
            if _allowed_projects and key.upper() not in _allowed_projects:
                continue
            projects.append({
                "key": key,
                "name": p.get("name", ""),
                "id": p.get("id", ""),
            })

        return ToolResult(tool_name="jira_list_projects", success=True, data={
            "projects": projects,
        })
    except Exception as exc:
        return ToolResult(tool_name="jira_list_projects", success=False, error=str(exc))


# ---------------------------------------------------------------------------
# Tool registry (merged into TOOL_REGISTRY by code_tools/tools.py)
# ---------------------------------------------------------------------------

JIRA_TOOL_REGISTRY = {
    "jira_search": jira_search,
    "jira_get_issue": jira_get_issue,
    "jira_create_issue": jira_create_issue,
    "jira_update_issue": jira_update_issue,
    "jira_list_projects": jira_list_projects,
}
