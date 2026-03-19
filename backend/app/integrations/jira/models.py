"""Pydantic models for Jira integration."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel


class JiraTokenPair(BaseModel):
    """OAuth token pair stored after successful authorization."""
    access_token: str
    refresh_token: str
    expires_in: int = 3600
    scope: str = ""
    cloud_id: str = ""
    site_url: str = ""


class JiraCallbackRequest(BaseModel):
    """Request body for the OAuth callback."""
    code: str
    state: str = ""


class CreateIssueRequest(BaseModel):
    """Request to create a Jira issue."""
    project_key: str
    summary: str
    description: str = ""
    issue_type: str = "Task"
    priority: str = ""
    team: str = ""
    components: List[str] = []


class JiraIssue(BaseModel):
    """Response after creating an issue."""
    id: str
    key: str
    self_url: str = ""
    browse_url: str = ""


class JiraProject(BaseModel):
    """A Jira project."""
    id: str
    key: str
    name: str
    style: str = ""


class JiraIssueType(BaseModel):
    """A Jira issue type."""
    id: str
    name: str
    subtask: bool = False


class JiraFieldOption(BaseModel):
    """An option for a Jira field (priority, team, component, etc.)."""
    id: str
    name: str


class JiraCreateMeta(BaseModel):
    """Metadata for creating an issue — required fields and their options."""
    priorities: List[JiraFieldOption] = []
    components: List[JiraFieldOption] = []
    teams: List[JiraFieldOption] = []
    team_field_key: str = ""  # custom field ID for Team, e.g. "customfield_10001"
