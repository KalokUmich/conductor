"""AI Provider API router.

This module provides the REST API endpoints for AI provider status and summarization.

Endpoints:
    GET /ai/status - Get current AI provider status
    POST /ai/summarize - Summarize messages using the active AI provider
    POST /ai/code-prompt - Generate a code prompt from a decision summary (supports multi-type)
"""
import logging
from typing import List, Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .base import ChatMessage
from .resolver import get_resolver
from .wrapper import (
    AIProviderError,
    call_code_prompt,
    call_selective_code_prompt,
    call_summary_pipeline,
    handle_provider_error,
    pipeline_summary_to_decision_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


class ProviderStatusResponse(BaseModel):
    """Response model for individual provider status."""
    name: str
    healthy: bool


class AIStatusResponse(BaseModel):
    """Response model for GET /ai/status endpoint."""
    summary_enabled: bool
    active_provider: Optional[str]
    providers: List[ProviderStatusResponse]


class MessageInput(BaseModel):
    """Input model for a single chat message."""
    role: Literal["host", "engineer"]
    text: str
    timestamp: float


class SummarizeRequest(BaseModel):
    """Request model for POST /ai/summarize endpoint."""
    messages: List[MessageInput]


class DecisionSummaryResponse(BaseModel):
    """Response model for POST /ai/summarize endpoint - structured decision summary."""
    type: Literal["decision_summary"] = "decision_summary"
    topic: str
    problem_statement: str
    proposed_solution: str
    requires_code_change: bool
    affected_components: List[str]
    risk_level: Literal["low", "medium", "high"]
    next_steps: List[str]
    # Pipeline metadata (optional, for observability)
    discussion_type: Optional[str] = None
    classification_confidence: Optional[float] = None
    # Code-relevant types for selective code prompt generation
    code_relevant_types: List[str] = Field(
        default_factory=list,
        description="Discussion types that are code-relevant for this summary"
    )


class DecisionSummaryInput(BaseModel):
    """Input model for decision summary in code-prompt request.

    Matches the structure of DecisionSummaryResponse from /ai/summarize.
    """
    type: Literal["decision_summary"] = "decision_summary"
    topic: str
    problem_statement: str
    proposed_solution: str
    requires_code_change: bool
    affected_components: List[str]
    risk_level: Literal["low", "medium", "high"]
    next_steps: List[str]


class CodePromptRequest(BaseModel):
    """Request model for POST /ai/code-prompt endpoint (legacy single summary)."""
    decision_summary: DecisionSummaryInput
    context_snippet: Optional[str] = None


class CodePromptResponse(BaseModel):
    """Response model for POST /ai/code-prompt endpoint."""
    code_prompt: str


# =============================================================================
# Multi-Type Summary Models for Selective Code Prompt Generation
# =============================================================================


class TypedSummaryInput(BaseModel):
    """Input model for a single typed summary within multi-type summary.

    Represents a summary for a specific discussion type with all relevant
    fields for code prompt generation.
    """
    discussion_type: str
    topic: str
    core_problem: str
    proposed_solution: str
    requires_code_change: bool
    impact_scope: str = "local"
    affected_components: List[str] = []
    risk_level: Literal["low", "medium", "high"] = "low"
    next_steps: List[str] = []


class MultiTypeSummaryInput(BaseModel):
    """Input model for multi-type summary in selective code-prompt request.

    Contains summaries from potentially multiple discussion types along with
    code_relevant_types to determine which summaries to include in code prompt.
    """
    primary_focus: str
    summaries: List[TypedSummaryInput]
    code_relevant_types: List[str]
    impact_scope: str = "local"


class SelectiveCodePromptRequest(BaseModel):
    """Request model for POST /ai/code-prompt with multi-type summary support."""
    multi_type_summary: MultiTypeSummaryInput
    context_snippet: Optional[str] = None


class FileLevelChange(BaseModel):
    """A single file-level change in the implementation plan."""
    file: str
    change_type: Literal["modify", "create", "delete"]
    description: str


class ImplementationPlan(BaseModel):
    """Structured implementation plan output."""
    affected_components: List[str]
    file_level_changes: List[FileLevelChange]
    tests_required: bool
    migration_required: bool
    risk_level: Literal["low", "medium", "high"]


class SelectiveCodePromptResponse(BaseModel):
    """Response model for selective code-prompt generation.

    Returns the generated prompt along with the implementation plan structure.
    """
    code_prompt: str
    implementation_plan: Optional[ImplementationPlan] = None
    code_relevant_types_used: List[str]


@router.get("/status", response_model=AIStatusResponse)
async def get_ai_status() -> AIStatusResponse:
    """Get the current AI provider status.

    Returns the summary enabled flag, active provider name,
    and health status of all configured providers.

    Returns:
        AIStatusResponse with:
            - summary_enabled: Whether AI summarization is enabled
            - active_provider: Name of the active provider (or null)
            - providers: List of provider statuses with name and healthy flag
    """
    resolver = get_resolver()

    if resolver is None:
        # Resolver not initialized (summary disabled or startup not complete)
        return AIStatusResponse(
            summary_enabled=False,
            active_provider=None,
            providers=[],
        )

    status = resolver.get_status()

    return AIStatusResponse(
        summary_enabled=status.summary_enabled,
        active_provider=status.active_provider,
        providers=[
            ProviderStatusResponse(name=p.name, healthy=p.healthy)
            for p in status.providers
        ],
    )


@router.post("/summarize", response_model=DecisionSummaryResponse)
async def summarize_messages(request: SummarizeRequest) -> DecisionSummaryResponse:
    """Summarize messages using the active AI provider.

    Uses the reusable wrapper to call the AI provider with proper
    error handling, timeout management, and logging.

    Args:
        request: SummarizeRequest with list of messages to summarize.

    Returns:
        DecisionSummaryResponse with structured summary including:
            - type: Always "decision_summary"
            - topic: Brief topic of the discussion
            - problem_statement: Description of the problem
            - proposed_solution: The proposed solution
            - requires_code_change: Whether code changes are needed
            - affected_components: List of affected components
            - risk_level: Risk assessment (low/medium/high)
            - next_steps: List of action items

    Raises:
        HTTPException 503: If summary is disabled or no active provider available.
        HTTPException 500: If the provider fails to generate summary or JSON parsing fails.
    """
    # Convert request messages to ChatMessage objects for the provider
    chat_messages = [
        ChatMessage(role=msg.role, text=msg.text, timestamp=msg.timestamp)
        for msg in request.messages
    ]

    try:
        # Use the two-stage pipeline for improved summarization
        pipeline_summary = call_summary_pipeline(chat_messages)

        # Convert to DecisionSummary for backward compatibility
        summary = pipeline_summary_to_decision_summary(pipeline_summary)

        return DecisionSummaryResponse(
            type=summary.type,
            topic=summary.topic,
            problem_statement=summary.problem_statement,
            proposed_solution=summary.proposed_solution,
            requires_code_change=summary.requires_code_change,
            affected_components=summary.affected_components,
            risk_level=summary.risk_level,
            next_steps=summary.next_steps,
            # Include pipeline metadata for observability
            discussion_type=pipeline_summary.discussion_type,
            classification_confidence=pipeline_summary.classification_confidence,
            # Include code-relevant types for selective code prompt generation
            code_relevant_types=pipeline_summary.code_relevant_types,
        )

    except AIProviderError as e:
        raise handle_provider_error(e)


@router.post("/code-prompt", response_model=CodePromptResponse)
async def generate_code_prompt(request: CodePromptRequest) -> CodePromptResponse:
    """Generate a code prompt from a decision summary.

    Takes a decision summary (typically from /ai/summarize) and constructs
    a prompt suitable for code generation models to produce unified diff output.

    Uses the reusable wrapper for consistent logging. This endpoint does not
    call an AI provider - it simply constructs the prompt using a template.
    The resulting prompt can be used with code generation tools like Codex SDK
    or other code agents.

    Args:
        request: CodePromptRequest with:
            - decision_summary: The structured decision summary from /ai/summarize
            - context_snippet: Optional code snippet for additional context

    Returns:
        CodePromptResponse with:
            - code_prompt: A formatted prompt string for code generation

    Example:
        Request:
        {
            "decision_summary": {
                "type": "decision_summary",
                "topic": "Add user authentication",
                "problem_statement": "Users cannot log in securely",
                "proposed_solution": "Implement JWT-based authentication",
                "requires_code_change": true,
                "affected_components": ["auth/login.py", "auth/middleware.py"],
                "risk_level": "medium",
                "next_steps": ["Implement login endpoint", "Add JWT validation"]
            },
            "context_snippet": "def login(username, password):\\n    pass"
        }

        Response:
        {
            "code_prompt": "You are a senior software engineer..."
        }
    """
    summary = request.decision_summary

    logger.info(f"Generating code prompt for topic: {summary.topic}")

    # Use the wrapper for consistent logging and potential future enhancements
    code_prompt_str = call_code_prompt(
        problem_statement=summary.problem_statement,
        proposed_solution=summary.proposed_solution,
        affected_components=summary.affected_components,
        risk_level=summary.risk_level,
        context_snippet=request.context_snippet,
    )

    return CodePromptResponse(code_prompt=code_prompt_str)


@router.post("/code-prompt/selective", response_model=SelectiveCodePromptResponse)
async def generate_selective_code_prompt(
    request: SelectiveCodePromptRequest
) -> SelectiveCodePromptResponse:
    """Generate a selective code prompt from multi-type summary.

    Takes a multi-type summary with code_relevant_types and generates a focused
    coding prompt that only includes code-relevant discussion summaries.

    This endpoint filters out non-code-relevant types (like innovation-only
    or product brainstorming sections) and builds a focused implementation prompt.

    Key behaviors:
    - If only one code-relevant type exists, generates a focused prompt for that type
    - If multiple code-relevant types exist, merges them logically
    - Non-code types (innovation without code, pure brainstorming) are excluded
    - Output is strictly JSON with structured implementation plan

    Args:
        request: SelectiveCodePromptRequest with:
            - multi_type_summary: Contains summaries and code_relevant_types
            - context_snippet: Optional code snippet for additional context

    Returns:
        SelectiveCodePromptResponse with:
            - code_prompt: A formatted prompt string for code generation
            - implementation_plan: Structured plan (populated by AI, null here)
            - code_relevant_types_used: Which types were actually included

    Example:
        Request:
        {
            "multi_type_summary": {
                "primary_focus": "Add user authentication feature",
                "impact_scope": "system",
                "code_relevant_types": ["code_change", "api_design"],
                "summaries": [
                    {
                        "discussion_type": "code_change",
                        "topic": "Implement JWT tokens",
                        "core_problem": "No secure auth mechanism",
                        "proposed_solution": "Add JWT middleware",
                        "requires_code_change": true,
                        "affected_components": ["auth/jwt.py"],
                        "risk_level": "medium",
                        "next_steps": ["Create JWT utility"]
                    },
                    {
                        "discussion_type": "api_design",
                        "topic": "Auth endpoints",
                        "core_problem": "Need login/logout endpoints",
                        "proposed_solution": "REST endpoints for auth",
                        "requires_code_change": true,
                        "affected_components": ["api/auth.py"],
                        "risk_level": "medium",
                        "next_steps": ["Design endpoint schema"]
                    },
                    {
                        "discussion_type": "innovation",
                        "topic": "Future biometric auth",
                        "core_problem": "Long-term auth improvements",
                        "proposed_solution": "Research biometric options",
                        "requires_code_change": false,
                        "affected_components": [],
                        "risk_level": "low",
                        "next_steps": ["Research phase"]
                    }
                ]
            }
        }

        Response:
        {
            "code_prompt": "You are a senior software engineer...",
            "implementation_plan": null,
            "code_relevant_types_used": ["code_change", "api_design"]
        }

    Note:
        The innovation summary is excluded because it's not in code_relevant_types.
    """
    multi_summary = request.multi_type_summary

    logger.info(
        f"Generating selective code prompt for focus: {multi_summary.primary_focus}, "
        f"code_relevant_types: {multi_summary.code_relevant_types}"
    )

    # Convert TypedSummaryInput models to dictionaries for the wrapper
    summaries_dicts = [
        {
            "discussion_type": s.discussion_type,
            "topic": s.topic,
            "core_problem": s.core_problem,
            "proposed_solution": s.proposed_solution,
            "requires_code_change": s.requires_code_change,
            "impact_scope": s.impact_scope,
            "affected_components": s.affected_components,
            "risk_level": s.risk_level,
            "next_steps": s.next_steps,
        }
        for s in multi_summary.summaries
    ]

    # Use the selective code prompt wrapper
    code_prompt_str, types_used = call_selective_code_prompt(
        primary_focus=multi_summary.primary_focus,
        impact_scope=multi_summary.impact_scope,
        summaries=summaries_dicts,
        code_relevant_types=multi_summary.code_relevant_types,
        context_snippet=request.context_snippet,
    )

    return SelectiveCodePromptResponse(
        code_prompt=code_prompt_str,
        implementation_plan=None,  # AI would populate this after processing the prompt
        code_relevant_types_used=types_used,
    )