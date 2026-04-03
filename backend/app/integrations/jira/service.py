"""Jira OAuth 2.0 (3LO) service.

Implements Atlassian OAuth 2.0 authorization code flow:
1. Generate authorize URL → user grants access in browser
2. Exchange authorization code for tokens via callback
3. Fetch accessible resources to get cloudId
4. Use tokens for Jira REST API calls with auto-refresh
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Any, Dict, List, Optional

import httpx

from .models import (
    CreateIssueRequest,
    JiraCreateMeta,
    JiraFieldOption,
    JiraIssue,
    JiraIssueType,
    JiraProject,
    JiraTokenPair,
)

logger = logging.getLogger(__name__)

ATLASSIAN_AUTH_URL = "https://auth.atlassian.com/authorize"
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"
ATLASSIAN_RESOURCES_URL = "https://api.atlassian.com/oauth/token/accessible-resources"
JIRA_API_BASE = "https://api.atlassian.com/ex/jira"

SCOPES = "read:jira-work write:jira-work read:jira-user manage:jira-configuration offline_access"


class JiraOAuthService:
    """Handles Jira OAuth 2.0 (3LO) flow and API calls."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        static_teams: Optional[List[JiraFieldOption]] = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri

        # In-memory token store keyed by state (simple for now)
        self._pending_states: Dict[str, float] = {}  # state -> created_at
        self._tokens: Optional[JiraTokenPair] = None
        self._token_expires_at: float = 0
        # Static teams from config (customfield_10001 UUIDs); API-based discovery as fallback
        self._team_cache: List[JiraFieldOption] = static_teams or []
        self._team_field_key: str = "customfield_10001" if static_teams else ""
        self._static_teams: bool = bool(static_teams)  # if True, never overwrite from API
        # Classic Jira epic link custom field (discovered once via /field API)
        self._epic_link_field: Optional[str] = None
        self._epic_link_field_checked: bool = False

    def get_authorize_url(self) -> dict:
        """Generate the Atlassian OAuth authorize URL.

        Returns dict with authorize_url and state for CSRF verification.
        """
        state = secrets.token_urlsafe(32)
        self._pending_states[state] = time.time()

        from urllib.parse import urlencode

        params = {
            "audience": "api.atlassian.com",
            "client_id": self.client_id,
            "scope": SCOPES,
            "redirect_uri": self.redirect_uri,
            "state": state,
            "response_type": "code",
            "prompt": "consent",
        }
        url = f"{ATLASSIAN_AUTH_URL}?{urlencode(params)}"

        return {"authorize_url": url, "state": state}

    async def exchange_code(self, code: str, state: str) -> JiraTokenPair:
        """Exchange authorization code for access + refresh tokens.

        Also fetches the cloudId from accessible-resources.
        """
        # Validate state
        if state and state not in self._pending_states:
            raise ValueError("Invalid or expired OAuth state")
        self._pending_states.pop(state, None)

        async with httpx.AsyncClient() as client:
            # Exchange code for tokens
            resp = await client.post(
                ATLASSIAN_TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            token_pair = JiraTokenPair(
                access_token=data["access_token"],
                refresh_token=data.get("refresh_token", ""),
                expires_in=data.get("expires_in", 3600),
                scope=data.get("scope", ""),
            )

            # Fetch accessible resources to get cloudId
            res_resp = await client.get(
                ATLASSIAN_RESOURCES_URL,
                headers={"Authorization": f"Bearer {token_pair.access_token}"},
            )
            res_resp.raise_for_status()
            resources = res_resp.json()

            if resources:
                # Use first resource (or match fintern.atlassian.net)
                resource = resources[0]
                for r in resources:
                    if "fintern" in r.get("url", ""):
                        resource = r
                        break
                token_pair.cloud_id = resource["id"]
                token_pair.site_url = resource.get("url", "")
                logger.info(
                    "Jira OAuth: connected to %s (cloudId=%s)",
                    token_pair.site_url,
                    token_pair.cloud_id,
                )

            # Store tokens
            self._tokens = token_pair
            self._token_expires_at = time.time() + token_pair.expires_in

            return token_pair

    async def _refresh_token(self) -> None:
        """Refresh the access token using the rotating refresh token."""
        if not self._tokens or not self._tokens.refresh_token:
            raise RuntimeError("No refresh token available")

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                ATLASSIAN_TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self._tokens.refresh_token,
                },
            )
            resp.raise_for_status()
            data = resp.json()

            self._tokens.access_token = data["access_token"]
            self._tokens.refresh_token = data.get("refresh_token", self._tokens.refresh_token)
            self._tokens.expires_in = data.get("expires_in", 3600)
            self._token_expires_at = time.time() + self._tokens.expires_in
            logger.info("Jira OAuth: token refreshed")

    async def get_valid_token(self) -> str:
        """Get a valid access token, refreshing if expired."""
        if not self._tokens:
            raise RuntimeError("Not connected to Jira")

        # Refresh 60s before expiry
        if time.time() > (self._token_expires_at - 60):
            await self._refresh_token()

        return self._tokens.access_token

    def get_status(self) -> dict:
        """Return current Jira connection status."""
        if not self._tokens:
            return {"connected": False}
        return {
            "connected": True,
            "cloud_id": self._tokens.cloud_id,
            "site_url": self._tokens.site_url,
        }

    async def refresh_token_for_client(self, refresh_token: str) -> dict:
        """Refresh a token on behalf of a client (extension).

        Uses server-side client_id/client_secret with the provided refresh_token.
        Returns the new token pair for the client to store locally.
        Does NOT update server-side in-memory state.
        """
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                ATLASSIAN_TOKEN_URL,
                json={
                    "grant_type": "refresh_token",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": refresh_token,
                },
            )
            if resp.status_code >= 400:
                try:
                    error_body = resp.json()
                except Exception:
                    error_body = resp.text
                raise RuntimeError(f"Token refresh failed: {error_body}")

            data = resp.json()
            return {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", refresh_token),
                "expires_in": data.get("expires_in", 3600),
            }

    def disconnect(self) -> None:
        """Clear stored tokens."""
        self._tokens = None
        self._token_expires_at = 0
        logger.info("Jira OAuth: disconnected")

    # ------------------------------------------------------------------
    # Jira REST API calls
    # ------------------------------------------------------------------

    def _api_base(self) -> str:
        if not self._tokens or not self._tokens.cloud_id:
            raise RuntimeError("Not connected to Jira")
        return f"{JIRA_API_BASE}/{self._tokens.cloud_id}/rest/api/3"

    async def _api_request(
        self,
        method: str,
        path: str,
        json: Any = None,
        params: Optional[Dict[str, str]] = None,
    ) -> Any:
        """Make an authenticated Jira API request with auto-refresh on 401."""
        token = await self.get_valid_token()
        url = f"{self._api_base()}{path}"

        async with httpx.AsyncClient() as client:
            resp = await client.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=json,
                params=params,
            )

            # Auto-refresh on 401 and retry once
            if resp.status_code == 401:
                await self._refresh_token()
                token = self._tokens.access_token  # type: ignore
                resp = await client.request(
                    method,
                    url,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    json=json,
                    params=params,
                )

            if resp.status_code >= 400:
                # Capture Jira's detailed error response
                try:
                    error_body = resp.json()
                except Exception:
                    error_body = resp.text
                logger.error("Jira API %s %s → %s: %s", method, path, resp.status_code, error_body)
                # Build a human-readable message from Jira's error format
                messages = []
                if isinstance(error_body, dict):
                    for msg in error_body.get("errorMessages", []):
                        messages.append(msg)
                    for field, msg in error_body.get("errors", {}).items():
                        messages.append(f"{field}: {msg}")
                detail = "; ".join(messages) if messages else str(error_body)
                raise RuntimeError(f"Jira API error ({resp.status_code}): {detail}")

            return resp.json() if resp.content else None

    async def get_projects(self) -> List[JiraProject]:
        """List accessible Jira projects."""
        data = await self._api_request("GET", "/project")
        return [
            JiraProject(
                id=p["id"],
                key=p["key"],
                name=p["name"],
                style=p.get("style", ""),
            )
            for p in data
        ]

    async def get_issue_types(self, project_key: str) -> List[JiraIssueType]:
        """List issue types for a project."""
        data = await self._api_request("GET", f"/project/{project_key}/statuses")
        # The /project/{key}/statuses endpoint returns issue types with statuses.
        # Each item has id, name, subtask.
        seen = set()
        types = []
        for item in data:
            tid = item["id"]
            if tid not in seen:
                seen.add(tid)
                types.append(
                    JiraIssueType(
                        id=tid,
                        name=item["name"],
                        subtask=item.get("subtask", False),
                    )
                )
        return types

    async def get_teams(self, project_key: str = "") -> List[JiraFieldOption]:
        """Fetch teams from the Jira Teams REST API (/rest/teams/1.0/teams/find).

        This is the correct source for customfield_10001 (Team) IDs.
        Falls back to Tempo Team options from createmeta if the endpoint is unavailable.
        """
        try:
            params: Dict[str, str] = {"maxResults": "50"}
            if project_key:
                params["query"] = ""
            data = await self._api_request("GET", "/rest/teams/1.0/teams/find", params=params)
            teams = []
            for t in data if isinstance(data, list) else data.get("teams", data.get("results", [])):
                tid = str(t.get("id", ""))
                name = t.get("title", t.get("name", ""))
                if tid and name:
                    teams.append(JiraFieldOption(id=tid, name=name))
            if teams:
                self._team_cache = teams
                self._team_field_key = "customfield_10001"
                logger.info("Fetched %d teams from Teams API", len(teams))
            return teams
        except Exception as e:
            logger.warning("Teams API unavailable (%s), falling back to createmeta", e)
            return []

    async def _seed_teams_from_simple_task(
        self, project_key: str, skip_issue_type_id: str
    ) -> tuple[List[JiraFieldOption], str]:
        """Fetch team options from Simple Task, which reliably exposes Tempo Team allowedValues.

        Returns (teams, team_field_key). Updates internal cache on success.
        """
        try:
            all_types = await self.get_issue_types(project_key)
            seed_id = next(
                (t.id for t in all_types if t.name.lower() == "simple task" and t.id != skip_issue_type_id),
                None,
            )
            if not seed_id:
                logger.info("No Simple Task issue type found in project %s", project_key)
                return [], ""

            logger.info("Seeding team options from Simple Task (%s) in project %s", seed_id, project_key)
            data = await self._api_request("GET", f"/issue/createmeta/{project_key}/issuetypes/{seed_id}")
            fields_raw = data.get("fields", data.get("values", []))
            if isinstance(fields_raw, dict):
                fields_list = list(fields_raw.items())
            elif isinstance(fields_raw, list):
                fields_list = [(f.get("fieldId", f.get("key", "")), f) for f in fields_raw]
            else:
                return [], ""

            teams: List[JiraFieldOption] = []
            team_field_key = ""
            for fk, fi in fields_list:
                name_lower = fi.get("name", "").lower()
                allowed = fi.get("allowedValues", [])
                if "team" in name_lower and allowed and not teams:
                    team_field_key = fk
                    teams = [
                        JiraFieldOption(
                            id=str(opt.get("id", "")),
                            name=opt.get("name", opt.get("value", "")),
                        )
                        for opt in allowed
                    ]

            # team_field_key already points to the field that owns these options — don't override
            if teams:
                self._team_cache = teams
                self._team_field_key = team_field_key
                logger.info("Seeded %d team options from Simple Task (field=%s)", len(teams), team_field_key)
            return teams, team_field_key
        except Exception as e:
            logger.warning("Failed to seed teams from Simple Task: %s", e)
            return [], ""

    async def get_create_meta(self, project_key: str, issue_type_id: str) -> JiraCreateMeta:
        """Fetch field metadata for creating an issue.

        Uses the createmeta endpoint to discover required fields and their
        allowed values (priorities, components, teams).
        """
        data = await self._api_request(
            "GET",
            f"/issue/createmeta/{project_key}/issuetypes/{issue_type_id}",
        )

        priorities: List[JiraFieldOption] = []
        components: List[JiraFieldOption] = []
        teams: List[JiraFieldOption] = []
        team_field_key = ""

        # The response can have "fields" as a dict or "values" as a list
        fields_raw = data.get("fields", data.get("values", []))

        # Normalize to list of (field_key, field_info) pairs
        if isinstance(fields_raw, dict):
            fields_list = list(fields_raw.items())
        elif isinstance(fields_raw, list):
            fields_list = [(f.get("fieldId", f.get("key", "")), f) for f in fields_raw]
        else:
            fields_list = []

        # Log all field keys and names for debugging
        logger.info(
            "Jira create_meta raw fields for %s/%s: %s",
            project_key,
            issue_type_id,
            [(fk, fi.get("name", "?")) for fk, fi in fields_list],
        )

        # Track team candidates: exact "Team" vs other team-like fields
        exact_team_key = ""
        exact_team_options: List[JiraFieldOption] = []
        fallback_team_key = ""
        fallback_team_options: List[JiraFieldOption] = []

        for field_key, field_info in fields_list:
            name_lower = field_info.get("name", "").lower()
            allowed = field_info.get("allowedValues", [])

            if field_key == "priority":
                for opt in allowed:
                    priorities.append(JiraFieldOption(id=opt["id"], name=opt["name"]))

            elif field_key == "components":
                for opt in allowed:
                    components.append(JiraFieldOption(id=opt["id"], name=opt["name"]))

            elif name_lower == "team":
                # Exact match — this is the real Team field (e.g. customfield_10001)
                exact_team_key = field_key
                for opt in allowed:
                    exact_team_options.append(
                        JiraFieldOption(
                            id=str(opt.get("id", "")),
                            name=opt.get("name", opt.get("value", "")),
                        )
                    )

            elif "team" in name_lower:
                # Fallback: "Tempo Team" etc. — collect options regardless of exact match
                # (exact Team field often has no allowedValues but Tempo Team does)
                if not fallback_team_key:
                    fallback_team_key = field_key
                for opt in allowed:
                    fallback_team_options.append(
                        JiraFieldOption(
                            id=str(opt.get("id", "")),
                            name=opt.get("name", opt.get("value", "")),
                        )
                    )

        # Use the field key that matches the source of the options — IDs are not portable
        # between fields. customfield_10001 ("Team") never has allowedValues, so we always
        # end up using customfield_10124 ("Tempo Team") for both options and creation.
        if exact_team_options:
            team_field_key = exact_team_key
            teams = exact_team_options
        elif fallback_team_options:
            team_field_key = fallback_team_key
            teams = fallback_team_options

        # Update persistent cache when we get fresh options; fall back to cache otherwise.
        # Never overwrite static teams configured in settings (they have the correct UUIDs
        # for customfield_10001; discovered options are Tempo Team IDs for customfield_10124).
        if teams and not self._static_teams:
            self._team_cache = teams
            self._team_field_key = team_field_key
            logger.info("Team cache updated: %d options (field=%s)", len(teams), team_field_key)
        elif self._team_cache:
            teams = self._team_cache
            team_field_key = self._team_field_key or team_field_key
            logger.info("Using %d cached team options (field=%s)", len(teams), team_field_key)
        elif not self._static_teams:
            # Try the Jira Teams API first (correct IDs for customfield_10001),
            # then fall back to seeding from Simple Task (Tempo Team options).
            teams = await self.get_teams(project_key)
            team_field_key = self._team_field_key if teams else ""
            if not teams:
                teams, team_field_key = await self._seed_teams_from_simple_task(project_key, issue_type_id)

        logger.info(
            "Jira create_meta result: %d priorities, %d components, %d teams (team_field=%s)",
            len(priorities),
            len(components),
            len(teams),
            team_field_key,
        )
        return JiraCreateMeta(
            priorities=priorities,
            components=components,
            teams=teams,
            team_field_key=team_field_key,
        )

    async def create_issue(self, req: CreateIssueRequest, team_field_key: str = "") -> JiraIssue:
        """Create a Jira issue."""
        # Use key for project, id for issuetype
        project_ref: dict = {"id": req.project_key} if req.project_key.isdigit() else {"key": req.project_key}
        issuetype_ref: dict = {"id": req.issue_type} if req.issue_type.isdigit() else {"name": req.issue_type}

        fields: dict = {
            "project": project_ref,
            "issuetype": issuetype_ref,
            "summary": req.summary,
        }

        # Only include description if non-empty (ADF format)
        if req.description.strip():
            fields["description"] = {
                "version": 1,
                "type": "doc",
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": req.description}],
                    }
                ],
            }

        if req.priority:
            fields["priority"] = {"id": req.priority} if req.priority.isdigit() else {"name": req.priority}

        if req.components:
            fields["components"] = [{"id": c} if c.isdigit() else {"name": c} for c in req.components]

        if req.team and team_field_key:
            # Tempo Team (customfield_10124) expects a bare Long integer, not an object
            fields[team_field_key] = int(req.team) if req.team.isdigit() else req.team

        payload = {"fields": fields}
        logger.info("Jira create_issue payload: %s", payload)

        data = await self._api_request("POST", "/issue", json=payload)
        browse_url = ""
        if self._tokens and self._tokens.site_url:
            browse_url = f"{self._tokens.site_url}/browse/{data['key']}"

        return JiraIssue(
            id=data["id"],
            key=data["key"],
            self_url=data.get("self", ""),
            browse_url=browse_url,
        )

    async def get_issue(self, issue_key: str) -> dict:
        """Get full details of a Jira issue."""
        data = await self._api_request(
            "GET",
            f"/issue/{issue_key}",
            params={
                "fields": "summary,description,status,priority,assignee,issuetype,"
                "components,labels,created,updated,comment,subtasks,parent",
            },
        )
        fields = data.get("fields", {})

        # Parse description (ADF → plain text)
        desc_raw = fields.get("description")
        description = ""
        if isinstance(desc_raw, dict):
            description = self._adf_to_text(desc_raw)
        elif isinstance(desc_raw, str):
            description = desc_raw

        # Parse comments
        comment_data = fields.get("comment", {})
        comments = []
        for c in comment_data.get("comments", []):
            body = c.get("body", "")
            if isinstance(body, dict):
                body = self._adf_to_text(body)
            comments.append(
                {
                    "author": c.get("author", {}).get("displayName", ""),
                    "created": c.get("created", ""),
                    "body": body,
                }
            )

        # Parse subtasks
        subtasks = []
        for st in fields.get("subtasks", []):
            st_fields = st.get("fields", {})
            subtasks.append(
                {
                    "key": st["key"],
                    "summary": st_fields.get("summary", ""),
                    "status": st_fields.get("status", {}).get("name", ""),
                }
            )

        browse_url = ""
        if self._tokens and self._tokens.site_url:
            browse_url = f"{self._tokens.site_url}/browse/{issue_key}"

        return {
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
            "browse_url": browse_url,
        }

    @staticmethod
    def _adf_to_text(adf: dict) -> str:
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

    async def _discover_epic_link_field(self) -> Optional[str]:
        """Discover the classic Jira epic link custom field ID.

        Calls GET /field once and caches the result. Returns the field ID
        (e.g. 'customfield_10014') or None if not found / not classic project.
        """
        if self._epic_link_field_checked:
            return self._epic_link_field
        self._epic_link_field_checked = True
        try:
            fields = await self._api_request("GET", "/field")
            for f in fields:
                schema = f.get("schema", {})
                if schema.get("custom") == "com.pyxis.greenhopper.jira:gh-epic-link":
                    self._epic_link_field = f["id"]
                    logger.info("Discovered classic epic link field: %s", self._epic_link_field)
                    return self._epic_link_field
        except Exception as e:
            logger.debug("Epic link field discovery failed (non-critical): %s", e)
        return None

    async def _fetch_epic_details(self, epic_keys: List[str]) -> Dict[str, dict]:
        """Batch-fetch details for a list of Epic issue keys."""
        if not epic_keys:
            return {}
        keys_str = ", ".join(epic_keys)
        data = await self._api_request(
            "POST",
            "/search/jql",
            json={
                "jql": f"key IN ({keys_str})",
                "maxResults": len(epic_keys),
                "fields": ["summary", "status", "priority", "issuetype", "assignee"],
            },
        )
        result = {}
        for issue in data.get("issues", []):
            fields = issue.get("fields", {})
            result[issue["key"]] = {
                "key": issue["key"],
                "summary": fields.get("summary", ""),
                "status": fields.get("status", {}).get("name", ""),
                "priority": fields.get("priority", {}).get("name", "") if fields.get("priority") else "",
                "issuetype": fields.get("issuetype", {}).get("name", "") if fields.get("issuetype") else "",
                "assignee": fields.get("assignee", {}).get("displayName", "") if fields.get("assignee") else "",
                "browse_url": (
                    f"{self._tokens.site_url}/browse/{issue['key']}" if self._tokens and self._tokens.site_url else ""
                ),
            }
        return result

    def _extract_epic_key(self, fields: dict) -> str:
        """Extract the epic key from issue fields (handles both next-gen and classic)."""
        # Next-gen: parent with issuetype == "Epic"
        parent = fields.get("parent")
        if parent:
            parent_type = parent.get("fields", {}).get("issuetype", {}).get("name", "")
            if parent_type.lower() == "epic":
                return parent.get("key", "")
        # Classic: custom epic link field
        if self._epic_link_field:
            epic_val = fields.get(self._epic_link_field)
            if isinstance(epic_val, str) and epic_val:
                return epic_val
            if isinstance(epic_val, dict):
                return epic_val.get("key", "")
        return ""

    def _parse_ticket(self, issue: dict) -> dict:
        """Parse a Jira issue into a ticket dict with epic_key."""
        fields = issue.get("fields", {})
        return {
            "key": issue["key"],
            "summary": fields.get("summary", ""),
            "status": fields.get("status", {}).get("name", ""),
            "priority": fields.get("priority", {}).get("name", "") if fields.get("priority") else "",
            "issuetype": fields.get("issuetype", {}).get("name", "") if fields.get("issuetype") else "",
            "assignee": fields.get("assignee", {}).get("displayName", "") if fields.get("assignee") else "",
            "components": [c.get("name", "") for c in fields.get("components", [])],
            "epic_key": self._extract_epic_key(fields),
            "browse_url": (
                f"{self._tokens.site_url}/browse/{issue['key']}" if self._tokens and self._tokens.site_url else ""
            ),
        }

    async def list_undone_tickets(self, max_results: int = 30) -> dict:
        """List the current user's undone tickets with Epic grouping.

        Returns dict with:
        - tickets: user's own undone tickets (each with epic_key)
        - epics: details for referenced Epics
        - unassigned_tickets: unassigned tickets under the same Epics
        """
        # Discover classic epic link field (cached after first call)
        epic_field = await self._discover_epic_link_field()

        # Build fields list
        fields_list = ["summary", "status", "assignee", "priority", "issuetype", "components", "parent"]
        if epic_field:
            fields_list.append(epic_field)

        # Fetch user's tickets
        jql = (
            "assignee = currentUser() "
            "AND status NOT IN (Done, Closed, Merged, Resolved) "
            "ORDER BY priority ASC, updated DESC"
        )
        data = await self._api_request(
            "POST",
            "/search/jql",
            json={
                "jql": jql,
                "maxResults": max_results,
                "fields": fields_list,
            },
        )
        tickets = [self._parse_ticket(issue) for issue in data.get("issues", [])]

        # Collect unique epic keys
        epic_keys = list({t["epic_key"] for t in tickets if t["epic_key"]})

        # Batch-fetch epic details
        epics = await self._fetch_epic_details(epic_keys) if epic_keys else {}

        # Fetch unassigned tickets under the same epics
        unassigned_tickets: List[dict] = []
        if epic_keys:
            my_keys = {t["key"] for t in tickets}
            # Build JQL for unassigned under these epics
            keys_str = ", ".join(epic_keys)
            unassigned_jql = (
                f"assignee IS EMPTY "
                f"AND parent IN ({keys_str}) "
                f"AND status NOT IN (Done, Closed, Merged, Resolved) "
                f"ORDER BY priority ASC, updated DESC"
            )
            try:
                ua_data = await self._api_request(
                    "POST",
                    "/search/jql",
                    json={
                        "jql": unassigned_jql,
                        "maxResults": 50,
                        "fields": fields_list,
                    },
                )
                for issue in ua_data.get("issues", []):
                    parsed = self._parse_ticket(issue)
                    if parsed["key"] not in my_keys:
                        unassigned_tickets.append(parsed)
            except Exception as e:
                logger.debug("Unassigned tickets fetch failed (non-critical): %s", e)

        return {
            "tickets": tickets,
            "epics": epics,
            "unassigned_tickets": unassigned_tickets,
        }

    async def search_issues(self, query: str, max_results: int = 10) -> List[dict]:
        """Search Jira issues using JQL text search."""
        jql = f'text ~ "{query}" ORDER BY updated DESC'
        data = await self._api_request(
            "POST",
            "/search/jql",
            json={
                "jql": jql,
                "maxResults": max_results,
                "fields": ["summary", "status", "assignee", "priority", "issuetype"],
            },
        )
        issues = data.get("issues", [])
        result = []
        for issue in issues:
            fields = issue.get("fields", {})
            result.append(
                {
                    "key": issue["key"],
                    "summary": fields.get("summary", ""),
                    "status": fields.get("status", {}).get("name", ""),
                    "priority": fields.get("priority", {}).get("name", "") if fields.get("priority") else "",
                    "issuetype": fields.get("issuetype", {}).get("name", "") if fields.get("issuetype") else "",
                    "assignee": fields.get("assignee", {}).get("displayName", "") if fields.get("assignee") else "",
                    "browse_url": (
                        f"{self._tokens.site_url}/browse/{issue['key']}"
                        if self._tokens and self._tokens.site_url
                        else ""
                    ),
                }
            )
        return result

    # ------------------------------------------------------------------
    # Issue update operations
    # ------------------------------------------------------------------

    # Statuses that agents CANNOT transition to — user-only operations
    BLOCKED_STATUSES = frozenset({"done", "closed", "resolved"})

    async def get_transitions(self, issue_key: str) -> List[dict]:
        """Get available status transitions for an issue."""
        data = await self._api_request("GET", f"/issue/{issue_key}/transitions")
        transitions = []
        for t in data.get("transitions", []):
            target_name = t.get("to", {}).get("name", "")
            transitions.append(
                {
                    "id": t["id"],
                    "name": t["name"],
                    "to_status": target_name,
                    "blocked": target_name.lower() in self.BLOCKED_STATUSES,
                }
            )
        return transitions

    async def transition_issue(self, issue_key: str, transition_id: str) -> None:
        """Transition an issue to a new status.

        Raises RuntimeError if the target status is in BLOCKED_STATUSES.
        """
        transitions = await self.get_transitions(issue_key)
        target = next((t for t in transitions if t["id"] == transition_id), None)
        if not target:
            raise RuntimeError(f"Transition {transition_id} not available for {issue_key}")
        if target["blocked"]:
            raise RuntimeError(
                f"Cannot transition {issue_key} to '{target['to_status']}' — "
                f"Done/Closed/Resolved transitions require manual user action"
            )

        await self._api_request(
            "POST",
            f"/issue/{issue_key}/transitions",
            json={
                "transition": {"id": transition_id},
            },
        )
        logger.info("Jira: transitioned %s via %s → %s", issue_key, transition_id, target["to_status"])

    async def add_comment(self, issue_key: str, body: str) -> dict:
        """Add a comment to an issue."""
        adf_body = {
            "version": 1,
            "type": "doc",
            "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": body}]},
            ],
        }
        data = await self._api_request(
            "POST",
            f"/issue/{issue_key}/comment",
            json={
                "body": adf_body,
            },
        )
        return {
            "id": data.get("id", ""),
            "created": data.get("created", ""),
        }

    async def update_fields(self, issue_key: str, fields: Dict[str, Any]) -> None:
        """Update arbitrary fields on an issue (priority, labels, components, etc.)."""
        await self._api_request("PUT", f"/issue/{issue_key}", json={"fields": fields})
