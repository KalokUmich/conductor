"""Tests for AzureDevOpsClient.vote() — the PAT-identity vote fix.

A PR vote can only be cast for the *authenticated* identity. The old code
grabbed the PR's first reviewer GUID and PUT a vote to it, which ADO rejects
with 400 (you can't vote as someone else). The fix resolves the PAT owner's
own identity via the connectionData endpoint and votes as that GUID (which
also self-adds the bot as a reviewer).
"""

from __future__ import annotations

import json as _json

import httpx
import pytest

from app.integrations.azure_devops import mcp_client as adoc


def _patch_transport(monkeypatch, handler):
    """Route every AsyncClient created inside mcp_client through a MockTransport."""
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real(*args, **kwargs)

    monkeypatch.setattr(adoc.httpx, "AsyncClient", factory)


@pytest.mark.asyncio
async def test_vote_uses_authenticated_user_guid(monkeypatch):
    """vote() must PUT to reviewers/<our own GUID>, resolved via connectionData."""
    calls = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/_apis/connectionData"):
            return httpx.Response(200, json={"authenticatedUser": {"id": "BOT-GUID-123"}})
        if "/reviewers/" in request.url.path:
            calls["path"] = request.url.path
            calls["method"] = request.method
            calls["body"] = _json.loads(request.content.decode())
            return httpx.Response(200, json={"vote": 10, "id": "BOT-GUID-123"})
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    client = adoc.AzureDevOpsClient("https://dev.azure.com/Org", "pat")
    res = await client.vote("Proj", "repo", 14453, 10)

    assert res["vote"] == 10
    assert calls["method"] == "PUT"
    # the GUID must be our own identity, NOT an arbitrary PR reviewer
    assert calls["path"].endswith("/reviewers/BOT-GUID-123")
    assert calls["body"] == {"vote": 10}


@pytest.mark.asyncio
async def test_vote_skips_when_identity_unresolved(monkeypatch):
    """If connectionData fails, vote() degrades gracefully and never PUTs a vote."""
    put_made = {"v": False}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/_apis/connectionData"):
            return httpx.Response(500, json={"message": "boom"})
        if "/reviewers/" in request.url.path:
            put_made["v"] = True
            return httpx.Response(200, json={})
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    client = adoc.AzureDevOpsClient("https://dev.azure.com/Org", "pat")
    res = await client.vote("Proj", "repo", 14453, 10)

    assert res["status"] == "skipped"
    assert res["reason"] == "no_authenticated_user_id"
    assert put_made["v"] is False  # never PUT a wrong-identity vote


@pytest.mark.asyncio
async def test_authenticated_user_id_is_cached(monkeypatch):
    """connectionData is fetched once per client, then cached across votes."""
    hits = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/_apis/connectionData"):
            hits["n"] += 1
            return httpx.Response(200, json={"authenticatedUser": {"id": "G"}})
        if "/reviewers/" in request.url.path:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404)

    _patch_transport(monkeypatch, handler)
    client = adoc.AzureDevOpsClient("https://dev.azure.com/Org", "pat")
    await client.vote("P", "r", 1, 10)
    await client.vote("P", "r", 2, 10)

    assert hits["n"] == 1  # fetched once, then served from cache
