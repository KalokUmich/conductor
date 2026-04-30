"""Jira read-only client using service-account API token + HTTP Basic auth.

Separate from the 3LO OAuth flow in ``service.py`` — this path does NOT require
per-user consent and is intended for server-side automation (PR review
context enrichment, Azure DevOps webhook, summarizer).

Atlassian API token path:
  ``{site_url}/rest/api/3/issue/{key}``
(note: differs from the OAuth path, which goes via
 ``api.atlassian.com/ex/jira/{cloudId}/...``)
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


class JiraReadonlyClient:
    def __init__(self, site_url: str, email: str, api_token: str, timeout: float = 15.0):
        self._site_url = site_url.rstrip("/")
        self._auth = httpx.BasicAuth(email, api_token)
        self._timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self._site_url and self._auth)

    async def get_issue(
        self,
        key: str,
        fields: str = "summary,description,issuetype,priority,status,labels,assignee,reporter",
    ) -> dict[str, Any]:
        url = f"{self._site_url}/rest/api/3/issue/{key}"
        async with httpx.AsyncClient(auth=self._auth, timeout=self._timeout) as c:
            r = await c.get(url, params={"fields": fields})
            r.raise_for_status()
            return r.json()

    async def myself(self) -> dict[str, Any]:
        """Verify credentials — returns the service account's profile."""
        url = f"{self._site_url}/rest/api/3/myself"
        async with httpx.AsyncClient(auth=self._auth, timeout=self._timeout) as c:
            r = await c.get(url)
            r.raise_for_status()
            return r.json()

    async def add_comment(self, key: str, body: str) -> dict[str, Any]:
        """Post a comment on an issue using the service account.

        ``body`` is plain text; it will be wrapped in a minimal ADF
        document since Jira Cloud v3 expects ADF (not the legacy
        wiki-markup string format that v2 accepted).
        """
        url = f"{self._site_url}/rest/api/3/issue/{key}/comment"
        adf = {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [{"type": "text", "text": body}],
                    }
                ],
            }
        }
        async with httpx.AsyncClient(auth=self._auth, timeout=self._timeout) as c:
            r = await c.post(url, json=adf)
            r.raise_for_status()
            return r.json()
