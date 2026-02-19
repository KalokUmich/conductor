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
    call_code_prompt_from_items,
    call_summary_pipeline,
    handle_provider_error,
    pipeline_summary_to_decision_summary,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


class ProviderStatusResponse(BaseModel):
    """Response model for individual provider status."""
    name: str
    enabled: bool
    configured: bool
    healthy: bool


class ModelStatusResponse(BaseModel):
    """Response model for individual model status."""
    id: str
    provider: str
    display_name: str
    available: bool


class AIStatusResponse(BaseModel):
    """Response model for GET /ai/status endpoint."""
    summary_enabled: bool
    active_provider: Optional[str]
    active_model: Optional[str]
    providers: List[ProviderStatusResponse]
    models: List[ModelStatusResponse]
    default_model: str


class MessageInput(BaseModel):
    """Input model for a single chat message."""
    role: Literal["host", "engineer"]
    text: str
    timestamp: float


class SummarizeRequest(BaseModel):
    """Request model for POST /ai/summarize endpoint."""
    messages: List[MessageInput]


class CodeRelevantItemResponse(BaseModel):
    """Response model for a single code-relevant implementation item."""
    id: str
    type: Literal["api_design", "code_change", "product_flow", "architecture", "debugging"]
    title: str
    problem: str
    proposed_change: str
    targets: List[str]
    risk_level: Literal["low", "medium", "high"]


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
    # Structured implementation items extracted from the summary
    code_relevant_items: List[CodeRelevantItemResponse] = Field(
        default_factory=list,
        description="Discrete implementation tasks extracted by AI"
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
    room_id: Optional[str] = None
    detected_languages: Optional[List[str]] = None


class CodePromptResponse(BaseModel):
    """Response model for POST /ai/code-prompt endpoint."""
    code_prompt: str


# =============================================================================
# Selective Item-Based Code Prompt Generation
# =============================================================================


class SummaryWithItemsInput(BaseModel):
    """Input model for a summary that includes code_relevant_items.

    Matches the structure of DecisionSummaryResponse from /ai/summarize,
    carrying the code_relevant_items needed for server-side filtering.
    """
    topic: str = ""
    problem_statement: str = ""
    proposed_solution: str = ""
    requires_code_change: bool = False
    affected_components: List[str] = []
    risk_level: Literal["low", "medium", "high"] = "low"
    next_steps: List[str] = []
    code_relevant_items: List[CodeRelevantItemResponse] = []


class ContextSnippetInput(BaseModel):
    """A code snippet from a specific file, used for context injection."""
    file_path: str
    snippet: str


class SelectiveItemsCodePromptRequest(BaseModel):
    """Request model for POST /ai/code-prompt/selective.

    Accepts a full summary (with code_relevant_items) and a list of
    selected_item_ids. The server filters items by the selected IDs
    before generating a focused prompt.
    """
    summary: SummaryWithItemsInput
    selected_item_ids: List[str]
    context_snippet: Optional[str] = None
    context_snippets: Optional[List[ContextSnippetInput]] = None
    room_id: Optional[str] = None
    detected_languages: Optional[List[str]] = None


@router.get("/status", response_model=AIStatusResponse)
async def get_ai_status() -> AIStatusResponse:
    """Get the current AI provider status.

    Returns the summary enabled flag, active provider/model,
    and health status of all configured providers and models.

    Returns:
        AIStatusResponse with:
            - summary_enabled: Whether AI summarization is enabled
            - active_provider: Name of the active provider type (or null)
            - active_model: ID of the active model (or null)
            - providers: List of provider statuses with name, configured, and healthy flags
            - models: List of model statuses with id, provider, display_name, and available flag
            - default_model: The configured default model ID
    """
    resolver = get_resolver()

    if resolver is None:
        # Resolver not initialized (summary disabled or startup not complete)
        return AIStatusResponse(
            summary_enabled=False,
            active_provider=None,
            active_model=None,
            providers=[],
            models=[],
            default_model="",
        )

    status = resolver.get_status()

    return AIStatusResponse(
        summary_enabled=status.summary_enabled,
        active_provider=status.active_provider,
        active_model=status.active_model,
        providers=[
            ProviderStatusResponse(
                name=p.name,
                enabled=p.enabled,
                configured=p.configured,
                healthy=p.healthy,
            )
            for p in status.providers
        ],
        models=[
            ModelStatusResponse(
                id=m.id,
                provider=m.provider,
                display_name=m.display_name,
                available=m.available,
            )
            for m in status.models
        ],
        default_model=status.default_model,
    )


class SetModelRequest(BaseModel):
    """Request model for POST /ai/model endpoint."""
    model_id: str


class SetModelResponse(BaseModel):
    """Response model for POST /ai/model endpoint."""
    success: bool
    active_model: Optional[str]
    message: str


@router.post("/model", response_model=SetModelResponse)
async def set_active_model(request: SetModelRequest) -> SetModelResponse:
    """Set the active AI model for summarization.

    Changes the model used for AI summarization. The model must be
    enabled and its provider must be healthy.

    Args:
        request: SetModelRequest with model_id to set as active.

    Returns:
        SetModelResponse with success status and message.

    Raises:
        HTTPException 503: If summary is disabled or resolver not initialized.
        HTTPException 400: If the model is not available.
    """
    resolver = get_resolver()

    if resolver is None:
        raise HTTPException(
            status_code=503,
            detail="AI summary is disabled or not initialized",
        )

    success = resolver.set_active_model(request.model_id)

    if success:
        return SetModelResponse(
            success=True,
            active_model=request.model_id,
            message=f"Active model set to: {request.model_id}",
        )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot set model '{request.model_id}': model not found, disabled, or provider not healthy",
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

        # Convert code_relevant_items from dataclasses to response models
        items_response = [
            CodeRelevantItemResponse(
                id=item.id,
                type=item.type,
                title=item.title,
                problem=item.problem,
                proposed_change=item.proposed_change,
                targets=item.targets,
                risk_level=item.risk_level,
            )
            for item in (pipeline_summary.code_relevant_items or [])
        ]

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
            # Include structured implementation items
            code_relevant_items=items_response,
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

    # Look up room code style and output mode if room_id provided
    room_code_style = _get_room_code_style(request.room_id)
    room_output_mode = _get_room_output_mode(request.room_id)

    # Use the wrapper for consistent logging and potential future enhancements
    code_prompt_str = call_code_prompt(
        problem_statement=summary.problem_statement,
        proposed_solution=summary.proposed_solution,
        affected_components=summary.affected_components,
        risk_level=summary.risk_level,
        context_snippet=request.context_snippet,
        room_code_style=room_code_style,
        detected_languages=request.detected_languages,
        room_output_mode=room_output_mode,
    )

    return CodePromptResponse(code_prompt=code_prompt_str)


@router.post("/code-prompt/selective", response_model=CodePromptResponse)
async def generate_selective_code_prompt(
    request: SelectiveItemsCodePromptRequest,
) -> CodePromptResponse:
    """Generate a code prompt from selected code_relevant_items within a summary.

    Takes a full summary (with code_relevant_items) and a list of
    selected_item_ids. Filters items to only the selected ones, then
    generates a focused coding prompt containing only those items'
    problem/solution/targets/risk.

    Key behaviors:
    - Only selected items appear in the prompt — unrelated items are excluded
    - Language-specific style guidelines are inferred from selected items'
      targets only (e.g., Python targets won't include JS guidelines)
    - Room code style and policy constraints are included when available
    - Output mode is configurable via server settings

    Args:
        request: SelectiveItemsCodePromptRequest with:
            - summary: Full summary including code_relevant_items
            - selected_item_ids: IDs of items to include in the prompt
            - context_snippet: Optional code snippet for context
            - room_id: Optional room ID for code style lookup
            - detected_languages: Optional workspace languages (fallback)

    Returns:
        CodePromptResponse with the generated code prompt.

    Raises:
        HTTPException 400: If selected_item_ids is empty or no items match.
    """
    if not request.selected_item_ids:
        raise HTTPException(
            status_code=400,
            detail="At least one selected_item_id is required",
        )

    # Filter items by selected IDs
    selected_ids_set = set(request.selected_item_ids)
    filtered_items = [
        item for item in request.summary.code_relevant_items
        if item.id in selected_ids_set
    ]

    if not filtered_items:
        raise HTTPException(
            status_code=400,
            detail=f"No items matched the selected IDs: {request.selected_item_ids}",
        )

    logger.info(
        f"Selective code prompt: {len(filtered_items)} of "
        f"{len(request.summary.code_relevant_items)} items selected "
        f"(IDs: {request.selected_item_ids})"
    )

    room_code_style = _get_room_code_style(request.room_id)
    room_output_mode = _get_room_output_mode(request.room_id)

    items_dicts = [
        {
            "id": item.id,
            "type": item.type,
            "title": item.title,
            "problem": item.problem,
            "proposed_change": item.proposed_change,
            "targets": item.targets,
            "risk_level": item.risk_level,
        }
        for item in filtered_items
    ]

    snippets_dicts = None
    if request.context_snippets:
        snippets_dicts = [
            {"file_path": s.file_path, "snippet": s.snippet}
            for s in request.context_snippets
        ]

    code_prompt_str = call_code_prompt_from_items(
        items=items_dicts,
        topic=request.summary.topic,
        context_snippet=request.context_snippet,
        context_snippets=snippets_dicts,
        room_code_style=room_code_style,
        detected_languages=request.detected_languages,
        room_output_mode=room_output_mode,
    )

    return CodePromptResponse(code_prompt=code_prompt_str)


class CodeRelevantItemInput(BaseModel):
    """Input model for a single code-relevant item in items code-prompt request."""
    id: str
    type: Literal["api_design", "code_change", "product_flow", "architecture", "debugging"]
    title: str
    problem: str
    proposed_change: str
    targets: List[str] = []
    risk_level: Literal["low", "medium", "high"] = "low"


class ItemsCodePromptRequest(BaseModel):
    """Request model for POST /ai/code-prompt/items endpoint."""
    items: List[CodeRelevantItemInput]
    topic: str = ""
    context_snippet: Optional[str] = None
    context_snippets: Optional[List[ContextSnippetInput]] = None
    room_id: Optional[str] = None
    detected_languages: Optional[List[str]] = None


@router.post("/code-prompt/items", response_model=CodePromptResponse)
async def generate_code_prompt_from_items(
    request: ItemsCodePromptRequest,
) -> CodePromptResponse:
    """Generate a code prompt from selected implementation items.

    Takes a list of code-relevant items (typically selected by the lead from
    the checklist UI) and generates a focused coding prompt.

    Args:
        request: ItemsCodePromptRequest with selected items and metadata.

    Returns:
        CodePromptResponse with the generated code prompt.

    Raises:
        HTTPException 400: If no items are provided.
    """
    if not request.items:
        raise HTTPException(status_code=400, detail="At least one item is required")

    logger.info(f"Generating code prompt from {len(request.items)} selected items")

    room_code_style = _get_room_code_style(request.room_id)
    room_output_mode = _get_room_output_mode(request.room_id)

    items_dicts = [
        {
            "id": item.id,
            "type": item.type,
            "title": item.title,
            "problem": item.problem,
            "proposed_change": item.proposed_change,
            "targets": item.targets,
            "risk_level": item.risk_level,
        }
        for item in request.items
    ]

    snippets_dicts = None
    if request.context_snippets:
        snippets_dicts = [
            {"file_path": s.file_path, "snippet": s.snippet}
            for s in request.context_snippets
        ]

    code_prompt_str = call_code_prompt_from_items(
        items=items_dicts,
        topic=request.topic,
        context_snippet=request.context_snippet,
        context_snippets=snippets_dicts,
        room_code_style=room_code_style,
        detected_languages=request.detected_languages,
        room_output_mode=room_output_mode,
    )

    return CodePromptResponse(code_prompt=code_prompt_str)


class StyleTemplateItem(BaseModel):
    """A single style template."""
    name: str
    filename: str
    content: str


class StyleTemplatesResponse(BaseModel):
    """Response model for GET /ai/style-templates endpoint."""
    templates: List[StyleTemplateItem]


@router.get("/style-templates", response_model=StyleTemplatesResponse)
async def get_style_templates() -> StyleTemplatesResponse:
    """Get all available built-in code style templates.

    Returns a list of style template markdown files that can be used
    as starting points for room-level code style configuration.

    Returns:
        StyleTemplatesResponse with list of templates (name, filename, content).
    """
    from app.agent.style_loader import CodeStyleLoader

    templates = CodeStyleLoader.list_templates()
    return StyleTemplatesResponse(
        templates=[StyleTemplateItem(**t) for t in templates]
    )


def _get_room_output_mode(room_id: Optional[str]) -> Optional[str]:
    """Look up output_mode for a room from the chat manager.

    Args:
        room_id: Optional room ID to look up.

    Returns:
        Output mode string if set, None otherwise.
    """
    if not room_id:
        return None

    try:
        from app.chat.manager import manager

        settings = manager.get_room_settings(room_id)
        output_mode = settings.get("output_mode", "")
        return output_mode if output_mode else None
    except Exception as e:
        logger.debug(f"Could not load room output_mode for {room_id}: {e}")
        return None


def _get_room_code_style(room_id: Optional[str]) -> Optional[str]:
    """Look up code style for a room from the chat manager.

    Args:
        room_id: Optional room ID to look up.

    Returns:
        Code style string if found, None otherwise.
    """
    if not room_id:
        return None

    try:
        from app.chat.manager import manager

        settings = manager.get_room_settings(room_id)
        code_style = settings.get("code_style", "")
        return code_style if code_style else None
    except Exception as e:
        logger.debug(f"Could not load room code style for {room_id}: {e}")
        return None