"""Jira integration API endpoints.

Endpoints:
    GET  /api/integrations/jira/authorize-url  — Start OAuth flow
    GET  /api/integrations/jira/callback        — OAuth redirect callback
    POST /api/integrations/jira/callback        — Exchange code for tokens (from extension)
    GET  /api/integrations/jira/status          — Connection status
    POST /api/integrations/jira/disconnect      — Remove tokens
    GET  /api/integrations/jira/projects        — List projects
    GET  /api/integrations/jira/issue-types     — List issue types for a project
    GET  /api/integrations/jira/create-meta     — Field metadata for creating an issue
    POST /api/integrations/jira/issues          — Create an issue
"""
from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse

from .models import CreateIssueRequest
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
<p style="font-size:0.8rem; margin-top:1rem; color:#6b7280;">You can close this tab and return to VS Code.</p>
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
<p>{str(e)}</p>
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
        }
    except Exception as e:
        logger.error("Jira token exchange failed: %s", e)
        raise HTTPException(status_code=400, detail=str(e))


@router.get("/status")
async def jira_status(request: Request) -> dict:
    """Return current Jira connection status."""
    svc = _get_service(request)
    return svc.get_status()


@router.post("/disconnect")
async def jira_disconnect(request: Request) -> dict:
    """Disconnect Jira (remove stored tokens)."""
    svc = _get_service(request)
    svc.disconnect()
    return {"status": "disconnected"}


@router.get("/projects")
async def list_projects(request: Request) -> list:
    """List accessible Jira projects."""
    svc = _get_service(request)
    try:
        projects = await svc.get_projects()
        return [p.model_dump() for p in projects]
    except RuntimeError as e:
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error("Failed to list Jira projects: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error("Failed to list issue types: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error("Failed to get create meta: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=401, detail=str(e))
    except Exception as e:
        logger.error("Failed to create Jira issue: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
