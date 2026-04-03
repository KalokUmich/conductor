"""Jira integration API endpoints.

Endpoints:
    GET  /api/integrations/jira/authorize-url  — Start OAuth flow
    GET  /api/integrations/jira/callback        — OAuth redirect callback
    POST /api/integrations/jira/callback        — Exchange code for tokens (from extension)
    POST /api/integrations/jira/refresh         — Refresh token on behalf of extension
    GET  /api/integrations/jira/status          — Connection status
    POST /api/integrations/jira/disconnect      — Remove tokens
    GET  /api/integrations/jira/projects        — List projects
    GET  /api/integrations/jira/issue-types     — List issue types for a project
    GET  /api/integrations/jira/create-meta     — Field metadata for creating an issue
    POST /api/integrations/jira/issues          — Create an issue
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .models import CreateIssueRequest, RefreshTokenRequest
from .service import JiraOAuthService

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/integrations/jira", tags=["jira"])


def _get_service(request: Request) -> JiraOAuthService:
    """Get the JiraOAuthService from app state."""
    svc = getattr(request.app.state, "jira_service", None)
    if svc is None:
        raise HTTPException(status_code=400, detail="Jira integration is not enabled")
    return svc


@router.get("/authorize-url")
async def get_authorize_url(request: Request) -> dict:
    """Generate the Atlassian OAuth authorize URL."""
    svc = _get_service(request)
    return svc.get_authorize_url()


@router.get("/callback", response_class=HTMLResponse)
async def oauth_callback_get(
    request: Request,
    code: str = Query(...),
    state: str = Query(""),
) -> HTMLResponse:
    """Handle the OAuth redirect from Atlassian (browser callback).

    Exchanges the code for tokens and shows a success/error page.
    """
    svc = _get_service(request)
    try:
        token_pair = await svc.exchange_code(code, state)
        html = f"""<!DOCTYPE html>
<html><head><title>Jira Connected</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0;
       display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
.card {{ background: #16213e; border-radius: 12px; padding: 2rem; text-align: center;
         border: 1px solid rgba(139,92,246,0.3); max-width: 400px; }}
h2 {{ color: #8b5cf6; margin-bottom: 0.5rem; }}
p {{ color: #9ca3af; font-size: 0.9rem; }}
.site {{ color: #60a5fa; }}
</style></head>
<body><div class="card">
<h2>&#10003; Jira Connected</h2>
<p>Connected to <span class="site">{token_pair.site_url}</span></p>
<p style="margin-top:1rem;"><a href="vscode://ai-collab/jira/callback?connected=true"
   style="color:#8b5cf6; text-decoration:underline;">Return to VS Code</a></p>
<p style="font-size:0.8rem; margin-top:0.5rem; color:#6b7280;">Or close this tab manually.</p>
<script>setTimeout(function(){{ window.location.href='vscode://ai-collab/jira/callback?connected=true'; }}, 2000);</script>
</div></body></html>"""
        return HTMLResponse(content=html)

    except Exception as e:
        logger.error("Jira OAuth callback failed: %s", e)
        html = f"""<!DOCTYPE html>
<html><head><title>Jira Connection Failed</title>
<style>
body {{ font-family: system-ui, sans-serif; background: #1a1a2e; color: #e0e0e0;
       display: flex; align-items: center; justify-content: center; height: 100vh; margin: 0; }}
.card {{ background: #16213e; border-radius: 12px; padding: 2rem; text-align: center;
         border: 1px solid rgba(239,68,68,0.3); max-width: 400px; }}
h2 {{ color: #ef4444; margin-bottom: 0.5rem; }}
p {{ color: #9ca3af; font-size: 0.9rem; }}
</style></head>
<body><div class="card">
<h2>&#10007; Connection Failed</h2>
<p>{e!s}</p>
</div></body></html>"""
        return HTMLResponse(content=html, status_code=400)


@router.post("/callback")
async def oauth_callback_post(request: Request) -> dict:
    """Exchange authorization code for tokens (called from extension)."""
    svc = _get_service(request)
    body = await request.json()
    code = body.get("code", "")
    state = body.get("state", "")
    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    try:
        token_pair = await svc.exchange_code(code, state)
        return {
            "status": "connected",
            "cloud_id": token_pair.cloud_id,
            "site_url": token_pair.site_url,
            "access_token": token_pair.access_token,
            "refresh_token": token_pair.refresh_token,
            "expires_in": token_pair.expires_in,
        }
    except Exception as e:
        logger.error("Jira token exchange failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/refresh")
async def refresh_token(request: Request, req: RefreshTokenRequest) -> dict:
    """Refresh a Jira token on behalf of the extension.

    The extension sends its locally-stored refresh_token; the backend
    combines it with server-side client_id/client_secret to get a new
    token pair from Atlassian.
    """
    svc = _get_service(request)
    try:
        return await svc.refresh_token_for_client(req.refresh_token)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Jira token refresh failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/status")
async def jira_status(request: Request) -> dict:
    """Return current Jira connection status."""
    svc = _get_service(request)
    return svc.get_status()


@router.get("/tokens")
async def get_tokens(request: Request) -> dict:
    """Return the current in-memory token pair for extension persistence.

    Called once after browser OAuth flow so the extension can store tokens
    locally in SecretStorage.  Returns 401 if not connected.
    """
    svc = _get_service(request)
    status = svc.get_status()
    if not status.get("connected"):
        raise HTTPException(status_code=401, detail="Not connected to Jira")
    tokens = svc._tokens
    return {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "expires_in": tokens.expires_in,
        "cloud_id": tokens.cloud_id,
        "site_url": tokens.site_url,
    }


@router.post("/disconnect")
async def jira_disconnect(request: Request) -> dict:
    """Disconnect Jira (remove stored tokens)."""
    svc = _get_service(request)
    svc.disconnect()
    return {"status": "disconnected"}


@router.get("/projects")
async def list_projects(request: Request) -> list:
    """List accessible Jira projects, filtered by allowed_projects config."""
    svc = _get_service(request)
    try:
        projects = await svc.get_projects()
        allowed: set = getattr(request.app.state, "jira_allowed_projects", set())
        if allowed:
            projects = [p for p in projects if p.key.upper() in allowed]
        return [p.model_dump() for p in projects]
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to list Jira projects: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/issue-types")
async def list_issue_types(
    request: Request,
    project_key: str = Query(..., alias="projectKey"),
) -> list:
    """List issue types for a given project."""
    svc = _get_service(request)
    try:
        types = await svc.get_issue_types(project_key)
        return [t.model_dump() for t in types]
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to list issue types: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/create-meta")
async def get_create_meta(
    request: Request,
    project_key: str = Query(..., alias="projectKey"),
    issue_type_id: str = Query(..., alias="issueTypeId"),
) -> dict:
    """Get field metadata (priorities, components, teams) for creating an issue."""
    svc = _get_service(request)
    try:
        meta = await svc.get_create_meta(project_key, issue_type_id)
        return meta.model_dump()
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to get create meta: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/issue/{issue_key}")
async def get_issue(request: Request, issue_key: str) -> dict:
    """Get full details of a Jira issue."""
    svc = _get_service(request)
    try:
        return await svc.get_issue(issue_key)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to get Jira issue %s: %s", issue_key, e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/issue/{issue_key}/transitions")
async def get_transitions(request: Request, issue_key: str) -> list:
    """List available status transitions for an issue."""
    svc = _get_service(request)
    try:
        return await svc.get_transitions(issue_key)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to get transitions for %s: %s", issue_key, e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/issue/{issue_key}/transition")
async def transition_issue(request: Request, issue_key: str) -> dict:
    """Transition an issue to a new status. Blocks Done/Closed/Resolved."""
    svc = _get_service(request)
    body = await request.json()
    transition_id = body.get("transition_id", "")
    if not transition_id:
        raise HTTPException(status_code=400, detail="Missing transition_id")
    try:
        await svc.transition_issue(issue_key, transition_id)
        return {"status": "transitioned", "issue_key": issue_key}
    except RuntimeError as e:
        status = 403 if "require manual" in str(e) else 401
        raise HTTPException(status_code=status, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to transition %s: %s", issue_key, e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/issue/{issue_key}/comment")
async def add_comment(request: Request, issue_key: str) -> dict:
    """Add a comment to an issue."""
    svc = _get_service(request)
    body = await request.json()
    comment_body = body.get("body", "")
    if not comment_body:
        raise HTTPException(status_code=400, detail="Missing comment body")
    try:
        return await svc.add_comment(issue_key, comment_body)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to add comment to %s: %s", issue_key, e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/search")
async def search_issues(
    request: Request,
    q: str = Query(..., description="Search query text"),
    max_results: int = Query(10, alias="maxResults", le=50),
) -> list:
    """Search Jira issues using JQL text search."""
    svc = _get_service(request)
    try:
        results = await svc.search_issues(q, max_results=max_results)
        return results
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to search Jira issues: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/undone")
async def list_undone_tickets(
    request: Request,
    max_results: int = Query(30, alias="maxResults", le=50),
) -> dict:
    """List the current user's undone tickets with Epic grouping.

    Returns { tickets, epics, unassigned_tickets }.
    """
    svc = _get_service(request)
    try:
        return await svc.list_undone_tickets(max_results=max_results)
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to list undone tickets: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/issues")
async def create_issue(request: Request, req: CreateIssueRequest) -> dict:
    """Create a Jira issue."""
    svc = _get_service(request)
    try:
        # If team is set, resolve the custom team field key.
        # Use cached key first; fall back to fetching create-meta.
        team_field_key = ""
        if req.team:
            team_field_key = svc._team_field_key  # populated after any prior create-meta call
            if not team_field_key:
                # issue_type may be an ID or a name — match either
                types = await svc.get_issue_types(req.project_key)
                type_id = next(
                    (t.id for t in types if t.id == req.issue_type or t.name == req.issue_type),
                    req.issue_type if req.issue_type.isdigit() else "",
                )
                if type_id:
                    meta = await svc.get_create_meta(req.project_key, type_id)
                    team_field_key = meta.team_field_key

        issue = await svc.create_issue(req, team_field_key=team_field_key)
        return issue.model_dump()
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e)) from e
    except Exception as e:
        logger.error("Failed to create Jira issue: %s", e)
        raise HTTPException(status_code=500, detail=str(e)) from e
