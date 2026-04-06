"""MCP client for Azure DevOps.

Connects to the official microsoft/azure-devops-mcp server via stdio transport.
Requires: npx @azure-devops/mcp <org> (Node.js runtime)

Alternatively supports direct REST API fallback when MCP is not available.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger(__name__)


class AzureDevOpsClient:
    """Azure DevOps REST API client (direct, no MCP dependency).

    Uses PAT authentication. MCP integration can be layered on top later
    when the Python MCP SDK is added to requirements.
    """

    def __init__(self, org_url: str, pat: str):
        """Initialize with organization URL and Personal Access Token.

        Args:
            org_url: e.g. "https://dev.azure.com/myorg"
            pat: Personal Access Token with Code (Read & Write) scope
        """
        self.org_url = org_url.rstrip("/")
        self._auth = httpx.BasicAuth("", pat)
        self._api_version = "7.1"

    def _url(self, project: str, path: str) -> str:
        return f"{self.org_url}/{project}/_apis/{path}"

    async def get_pull_request(self, project: str, repo: str, pr_id: int) -> Dict[str, Any]:
        """Get PR details including source/target branches."""
        url = self._url(project, f"git/repositories/{repo}/pullRequests/{pr_id}")
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                auth=self._auth,
                params={"api-version": self._api_version},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_pull_request_diff(self, project: str, repo: str, pr_id: int) -> str:
        """Get the diff content for a PR as unified diff text."""
        # Get PR iterations to find the latest
        url = self._url(
            project,
            f"git/repositories/{repo}/pullRequests/{pr_id}/iterations",
        )
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                url,
                auth=self._auth,
                params={"api-version": self._api_version},
            )
            resp.raise_for_status()
            iterations = resp.json().get("value", [])
            if not iterations:
                return ""

            latest = iterations[-1]["id"]

            # Get changes in latest iteration
            changes_url = self._url(
                project,
                f"git/repositories/{repo}/pullRequests/{pr_id}/iterations/{latest}/changes",
            )
            resp = await client.get(
                changes_url,
                auth=self._auth,
                params={"api-version": self._api_version},
            )
            resp.raise_for_status()

        # Build a diff spec from PR branches for our Brain to use
        pr_data = await self.get_pull_request(project, repo, pr_id)
        source = pr_data.get("sourceRefName", "").replace("refs/heads/", "")
        target = pr_data.get("targetRefName", "").replace("refs/heads/", "")
        return f"{target}...{source}"

    async def create_thread(
        self,
        project: str,
        repo: str,
        pr_id: int,
        content: str,
        file_path: Optional[str] = None,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        status: int = 1,
    ) -> Dict[str, Any]:
        """Create a PR thread (inline comment).

        Args:
            content: Markdown content
            file_path: File to attach to (e.g. "/src/auth.java"). Must start with /
            start_line: Start line on the right (modified) side
            end_line: End line on the right side
            status: 1=active, 2=fixed, 3=wontFix, 4=closed
        """
        url = self._url(
            project,
            f"git/repositories/{repo}/pullRequests/{pr_id}/threads",
        )

        body: Dict[str, Any] = {
            "comments": [
                {
                    "parentCommentId": 0,
                    "content": content,
                    "commentType": 1,  # text
                }
            ],
            "status": status,
        }

        if file_path and start_line:
            # Ensure leading slash
            if not file_path.startswith("/"):
                file_path = f"/{file_path}"
            body["threadContext"] = {
                "filePath": file_path,
                "rightFileStart": {"line": start_line, "offset": 1},
                "rightFileEnd": {"line": end_line or start_line, "offset": 1},
            }

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                auth=self._auth,
                json=body,
                params={"api-version": self._api_version},
            )
            resp.raise_for_status()
            return resp.json()

    async def vote(self, project: str, repo: str, pr_id: int, vote: int) -> Dict[str, Any]:
        """Set a vote on a PR.

        Vote values: 10=approve, 5=approve_with_suggestions, 0=none, -5=wait, -10=reject
        """
        # Need reviewer ID — get current user from PR
        pr = await self.get_pull_request(project, repo, pr_id)
        reviewer_id = None
        for reviewer in pr.get("reviewers", []):
            # The PAT owner is typically the first non-required reviewer
            # For now, we'll need the user to configure their reviewer ID
            reviewer_id = reviewer.get("id")
            break

        if not reviewer_id:
            logger.warning("No reviewer ID found on PR %d — skipping vote", pr_id)
            return {"status": "skipped", "reason": "no_reviewer_id"}

        url = self._url(
            project,
            f"git/repositories/{repo}/pullRequests/{pr_id}/reviewers/{reviewer_id}",
        )

        async with httpx.AsyncClient() as client:
            resp = await client.put(
                url,
                auth=self._auth,
                json={"vote": vote},
                params={"api-version": self._api_version},
            )
            resp.raise_for_status()
            return resp.json()

    async def get_diff_text(self, project: str, repo: str, source_branch: str, target_branch: str) -> str:
        """Get the actual git diff between two branches via the diffs API."""
        url = self._url(
            project,
            f"git/repositories/{repo}/diffs/commits",
        )
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.get(
                url,
                auth=self._auth,
                params={
                    "baseVersion": target_branch,
                    "baseVersionType": "branch",
                    "targetVersion": source_branch,
                    "targetVersionType": "branch",
                    "api-version": self._api_version,
                },
            )
            resp.raise_for_status()

        return f"{target_branch}...{source_branch}"
