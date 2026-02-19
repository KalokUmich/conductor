"""Reusable wrapper for calling the active AI provider.

This module provides high-level functions for calling the AI provider
with proper error handling, timeout management, and logging.

Usage:
    from app.ai_provider.wrapper import call_summary, call_code_prompt

    # For summarization
    result = call_summary(chat_messages)

    # For code prompt generation (no AI call, just template)
    result = call_code_prompt(decision_summary, context_snippet)
"""
import logging
from dataclasses import dataclass
from typing import List, Optional

from fastapi import HTTPException

from .base import ChatMessage, DecisionSummary
from .pipeline import PipelineSummary, run_summary_pipeline
from .prompt_builder import PromptBuilder, build_selective_prompt
from .prompts import format_policy_constraints, get_code_prompt, get_selective_code_prompt
from .resolver import get_resolver

logger = logging.getLogger(__name__)

# Default timeout for AI provider calls (in seconds)
DEFAULT_TIMEOUT_SECONDS = 60


@dataclass
class ProviderCallResult:
    """Result of an AI provider call.

    Attributes:
        success: Whether the call succeeded.
        provider_name: Name of the provider used.
        data: The result data (DecisionSummary or str).
        error: Error message if call failed.
    """
    success: bool
    provider_name: Optional[str]
    data: Optional[object] = None
    error: Optional[str] = None


class AIProviderError(Exception):
    """Base exception for AI provider errors."""
    def __init__(self, message: str, status_code: int = 500):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class ProviderNotAvailableError(AIProviderError):
    """Raised when no AI provider is available."""
    def __init__(self, message: str = "No active AI provider available"):
        super().__init__(message, status_code=503)


class ProviderCallError(AIProviderError):
    """Raised when an AI provider call fails."""
    def __init__(self, message: str, provider_name: str):
        self.provider_name = provider_name
        super().__init__(f"Provider {provider_name} error: {message}", status_code=500)


class JSONParseError(AIProviderError):
    """Raised when AI response JSON parsing fails."""
    def __init__(self, message: str, provider_name: str):
        self.provider_name = provider_name
        super().__init__(
            f"Failed to parse AI response as JSON from {provider_name}: {message}",
            status_code=500
        )


def _get_active_provider():
    """Get the active AI provider with proper error handling.

    Returns:
        Tuple of (provider, provider_name, resolver).

    Raises:
        ProviderNotAvailableError: If no provider is available.
    """
    resolver = get_resolver()

    if resolver is None:
        logger.warning("AI provider call failed: resolver not initialized")
        raise ProviderNotAvailableError(
            "AI summarization service is not initialized. Please check server configuration."
        )

    if not resolver.summary_config.enabled:
        logger.info("AI provider call rejected: summary feature is disabled")
        raise ProviderNotAvailableError(
            "AI summarization is not enabled in configuration."
        )

    provider = resolver.get_active_provider()
    provider_name = resolver.active_provider_type

    if provider is None:
        # Log provider status for debugging
        status = resolver.get_status()
        provider_info = ", ".join(
            [f"{p.name}={'healthy' if p.healthy else 'unhealthy'}" for p in status.providers]
        ) or "no providers configured"
        logger.warning(f"AI provider call failed: no active provider. Status: {provider_info}")
        raise ProviderNotAvailableError(
            "No active AI provider available. Please check provider configuration and API keys."
        )

    return provider, provider_name, resolver


def call_summary(messages: List[ChatMessage]) -> DecisionSummary:
    """Call the active AI provider to generate a structured summary.

    This function handles:
    - Provider resolution from the global resolver
    - Error handling with appropriate HTTP status codes
    - Logging of requests and responses

    Args:
        messages: List of ChatMessage objects to summarize.

    Returns:
        DecisionSummary with structured summary data.

    Raises:
        ProviderNotAvailableError: If no provider is available (503).
        JSONParseError: If AI response parsing fails (500).
        ProviderCallError: If the provider call fails (500).
    """
    provider, provider_name, _ = _get_active_provider()

    logger.info(f"Calling summary with provider: {provider_name}, messages: {len(messages)}")

    try:
        summary = provider.summarize_structured(messages)
        logger.info(f"Successfully generated summary with provider: {provider_name}")
        return summary

    except ValueError as e:
        # ValueError is raised when JSON parsing fails in the provider
        error_msg = str(e)
        logger.error(f"JSON parsing error from provider {provider_name}: {error_msg}")
        raise JSONParseError(error_msg, provider_name)

    except Exception as e:
        # Catch-all for other provider errors (API errors, network issues, etc.)
        error_msg = str(e)
        logger.error(f"Provider {provider_name} error during summarization: {error_msg}")
        raise ProviderCallError(error_msg, provider_name)


def call_code_prompt(
    problem_statement: str,
    proposed_solution: str,
    affected_components: List[str],
    risk_level: str,
    context_snippet: Optional[str] = None,
    room_code_style: Optional[str] = None,
    detected_languages: Optional[List[str]] = None,
    room_output_mode: Optional[str] = None,
) -> str:
    """Generate a code prompt from decision summary components.

    This function does not call an AI provider - it constructs a prompt
    using PromptBuilder that can be used with code generation tools.

    Args:
        problem_statement: Description of the problem to solve.
        proposed_solution: The proposed solution approach.
        affected_components: List of components/files affected.
        risk_level: Risk assessment (low/medium/high).
        context_snippet: Optional code snippet for context.
        room_code_style: Optional room-level code style guidelines.
        detected_languages: Optional list of workspace-detected languages.
        room_output_mode: Optional room-level output mode override.

    Returns:
        str: Formatted code prompt for code generation.
    """
    logger.info(f"Generating code prompt for {len(affected_components)} components")

    policy_str = _load_policy_constraints()
    output_mode = room_output_mode or _get_output_mode()

    code_prompt = (
        PromptBuilder(problem_statement, proposed_solution, affected_components, risk_level)
        .with_context_snippet(context_snippet)
        .with_policy_constraints(policy_str)
        .with_room_code_style(room_code_style)
        .with_detected_languages(detected_languages)
        .with_output_mode(output_mode)
        .build()
    )

    logger.debug(f"Generated code prompt with {len(code_prompt)} characters")
    return code_prompt


def call_code_prompt_from_items(
    items: List[dict],
    topic: str = "",
    context_snippet: Optional[str] = None,
    context_snippets: Optional[List[dict]] = None,
    room_code_style: Optional[str] = None,
    detected_languages: Optional[List[str]] = None,
    room_output_mode: Optional[str] = None,
) -> str:
    """Generate a code prompt from selected code-relevant items.

    Merges multiple items into a single prompt by combining their fields:
    - problem_statement: joined title+problem lines
    - proposed_solution: joined title+proposed_change lines
    - affected_components: deduplicated union of all targets
    - risk_level: highest among items

    Args:
        items: List of item dicts with type, title, problem, proposed_change,
               targets, and risk_level.
        topic: Optional topic string for context.
        context_snippet: Optional single code snippet for context (legacy).
        context_snippets: Optional list of file-targeted snippets, each with
            "file_path" and "snippet" keys.
        room_code_style: Optional room-level code style guidelines.
        detected_languages: Optional list of workspace-detected languages.
        room_output_mode: Optional room-level output mode override.

    Returns:
        str: Formatted code prompt for code generation.
    """
    logger.info(f"Generating code prompt from {len(items)} selected items")

    risk_order = {"low": 0, "medium": 1, "high": 2}
    risk_reverse = {0: "low", 1: "medium", 2: "high"}

    # Merge items
    problem_parts = []
    solution_parts = []
    all_targets = []
    max_risk = 0

    for item in items:
        title = item.get("title", "")
        problem = item.get("problem", "")
        proposed_change = item.get("proposed_change", "")
        targets = item.get("targets", [])
        risk = item.get("risk_level", "low")

        problem_parts.append(f"- {title}: {problem}" if problem else f"- {title}")
        solution_parts.append(f"- {title}: {proposed_change}" if proposed_change else f"- {title}")
        all_targets.extend(targets)
        max_risk = max(max_risk, risk_order.get(risk, 0))

    # Deduplicate targets preserving order
    seen = set()
    unique_targets = []
    for t in all_targets:
        if t not in seen:
            seen.add(t)
            unique_targets.append(t)

    problem_statement = "\n".join(problem_parts)
    if topic:
        problem_statement = f"{topic}\n\n{problem_statement}"

    proposed_solution = "\n".join(solution_parts)
    highest_risk = risk_reverse.get(max_risk, "low")

    policy_str = _load_policy_constraints()
    output_mode = room_output_mode or _get_output_mode()

    builder = (
        PromptBuilder(problem_statement, proposed_solution, unique_targets, highest_risk)
        .with_context_snippet(context_snippet)
        .with_context_snippets(context_snippets)
        .with_policy_constraints(policy_str)
        .with_room_code_style(room_code_style)
        .with_detected_languages(detected_languages)
        .with_output_mode(output_mode)
    )

    code_prompt = builder.build()

    logger.debug(f"Generated item-based code prompt with {len(code_prompt)} characters")
    return code_prompt


def handle_provider_error(error: AIProviderError) -> HTTPException:
    """Convert an AIProviderError to an HTTPException.

    Args:
        error: The AIProviderError to convert.

    Returns:
        HTTPException with appropriate status code and detail.
    """
    return HTTPException(
        status_code=error.status_code,
        detail=error.message,
    )


def call_summary_http(messages: List[ChatMessage]) -> DecisionSummary:
    """Call summary with automatic HTTP exception conversion.

    Convenience wrapper that catches AIProviderError and converts
    to HTTPException for use in FastAPI endpoints.

    Args:
        messages: List of ChatMessage objects to summarize.

    Returns:
        DecisionSummary with structured summary data.

    Raises:
        HTTPException: On any provider error.
    """
    try:
        return call_summary(messages)
    except AIProviderError as e:
        raise handle_provider_error(e)


def call_summary_pipeline(messages: List[ChatMessage]) -> PipelineSummary:
    """Call the two-stage AI summary pipeline.

    This function runs the complete pipeline:
    1. Classification: Determine the discussion type
    2. Targeted Summary: Generate a specialized summary

    Args:
        messages: List of ChatMessage objects to process.

    Returns:
        PipelineSummary with structured summary data and classification metadata.

    Raises:
        ProviderNotAvailableError: If no provider is available (503).
        JSONParseError: If AI response parsing fails (500).
        ProviderCallError: If the provider call fails (500).
    """
    provider, provider_name, _ = _get_active_provider()

    logger.info(
        f"Starting summary pipeline with provider: {provider_name}, "
        f"messages: {len(messages)}"
    )

    try:
        summary = run_summary_pipeline(messages, provider)
        logger.info(
            f"Pipeline complete with provider: {provider_name}, "
            f"type={summary.discussion_type}, confidence={summary.classification_confidence:.2f}"
        )
        return summary

    except ValueError as e:
        # ValueError is raised when JSON parsing fails in the pipeline
        error_msg = str(e)
        logger.error(f"JSON parsing error in pipeline from {provider_name}: {error_msg}")
        raise JSONParseError(error_msg, provider_name)

    except Exception as e:
        # Catch-all for other errors
        error_msg = str(e)
        logger.error(f"Provider {provider_name} error during pipeline: {error_msg}")
        raise ProviderCallError(error_msg, provider_name)


def call_summary_pipeline_http(messages: List[ChatMessage]) -> PipelineSummary:
    """Call summary pipeline with automatic HTTP exception conversion.

    Convenience wrapper that catches AIProviderError and converts
    to HTTPException for use in FastAPI endpoints.

    Args:
        messages: List of ChatMessage objects to process.

    Returns:
        PipelineSummary with structured summary data.

    Raises:
        HTTPException: On any provider error.
    """
    try:
        return call_summary_pipeline(messages)
    except AIProviderError as e:
        raise handle_provider_error(e)


def pipeline_summary_to_decision_summary(pipeline_summary: PipelineSummary) -> DecisionSummary:
    """Convert a PipelineSummary to a DecisionSummary for backward compatibility.

    Maps the new pipeline fields to the legacy DecisionSummary format.

    Args:
        pipeline_summary: The PipelineSummary to convert.

    Returns:
        DecisionSummary with mapped fields.
    """
    return DecisionSummary(
        type="decision_summary",
        topic=pipeline_summary.topic,
        problem_statement=pipeline_summary.core_problem,  # Map core_problem -> problem_statement
        proposed_solution=pipeline_summary.proposed_solution,
        requires_code_change=pipeline_summary.requires_code_change,
        affected_components=pipeline_summary.affected_components,
        risk_level=pipeline_summary.risk_level,
        next_steps=pipeline_summary.next_steps,
    )


def filter_code_relevant_summaries(
    summaries: List[dict],
    code_relevant_types: List[str],
) -> List[dict]:
    """Filter summaries to only include code-relevant types.

    This ensures only code-relevant discussion summaries are passed
    to the AI for code prompt generation, excluding innovation-only
    or product brainstorming sections.

    Args:
        summaries: List of summary dictionaries with discussion_type.
        code_relevant_types: List of discussion types considered code-relevant.

    Returns:
        Filtered list containing only summaries with code-relevant types.
    """
    if not code_relevant_types:
        logger.warning("No code_relevant_types specified, returning empty list")
        return []

    filtered = []
    for summary in summaries:
        # Handle both dict and object-like summaries
        if hasattr(summary, "discussion_type"):
            disc_type = summary.discussion_type
        else:
            disc_type = summary.get("discussion_type", "")

        if disc_type in code_relevant_types:
            filtered.append(summary)
            logger.debug(f"Including summary of type '{disc_type}' in code prompt")
        else:
            logger.debug(f"Excluding summary of type '{disc_type}' - not in code_relevant_types")

    logger.info(
        f"Filtered {len(summaries)} summaries to {len(filtered)} code-relevant summaries"
    )
    return filtered


def call_selective_code_prompt(
    primary_focus: str,
    impact_scope: str,
    summaries: List[dict],
    code_relevant_types: List[str],
    context_snippet: Optional[str] = None,
    room_code_style: Optional[str] = None,
    detected_languages: Optional[List[str]] = None,
) -> tuple[str, List[str]]:
    """Generate a selective code prompt from multi-type summaries.

    This function:
    1. Filters summaries to only include code-relevant types
    2. Merges information from multiple summaries if needed
    3. Generates a focused coding task prompt

    Important: Only code-relevant summaries are included. Innovation-only
    or pure product brainstorming sections are excluded.

    Args:
        primary_focus: The primary focus area of the implementation.
        impact_scope: The scope of impact (local, module, system, cross-system).
        summaries: List of all summary dictionaries.
        code_relevant_types: List of types to include in code prompt.
        context_snippet: Optional code snippet for context.
        room_code_style: Optional room-level code style guidelines.

    Returns:
        Tuple of (code_prompt_str, filtered_types_used)
    """
    logger.info(
        f"Generating selective code prompt with {len(summaries)} summaries, "
        f"code_relevant_types={code_relevant_types}"
    )

    # Filter to only code-relevant summaries
    filtered_summaries = filter_code_relevant_summaries(summaries, code_relevant_types)

    if not filtered_summaries:
        logger.warning("No code-relevant summaries after filtering")
        return (
            "No code-relevant discussion summaries available for code generation.",
            []
        )

    # Track which types were actually used
    types_used = list(set(
        s.discussion_type if hasattr(s, "discussion_type") else s.get("discussion_type", "")
        for s in filtered_summaries
    ))

    output_mode = _get_output_mode()

    # Generate the selective code prompt via PromptBuilder
    code_prompt = build_selective_prompt(
        primary_focus=primary_focus,
        impact_scope=impact_scope,
        summaries=filtered_summaries,
        context_snippet=context_snippet,
        room_code_style=room_code_style,
        detected_languages=detected_languages,
        output_mode=output_mode,
    )

    logger.info(
        f"Generated selective code prompt with {len(code_prompt)} characters, "
        f"using types: {types_used}"
    )

    return code_prompt, types_used


def _get_output_mode() -> str:
    """Read the configured output mode for code prompts.

    Returns:
        Output mode string, defaults to "unified_diff".
    """
    try:
        from app.config import get_config
        return get_config().prompt.output_mode
    except Exception:
        return "unified_diff"


def _load_policy_constraints() -> Optional[str]:
    """Load policy constraints from config and auto-apply defaults.

    Returns:
        Formatted policy constraints string, or None if unavailable.
    """
    try:
        from app.config import get_config
        from app.policy.auto_apply import FORBIDDEN_PATHS

        config = get_config()
        limits = config.change_limits
        return format_policy_constraints(
            max_files=limits.max_files_per_request,
            max_lines_changed=limits.max_total_lines,
            forbidden_paths=FORBIDDEN_PATHS,
        )
    except Exception as e:
        logger.debug(f"Could not load policy constraints: {e}")
        return None


def _load_style_guidelines(
    room_code_style: Optional[str] = None,
    detected_languages: Optional[List[str]] = None,
) -> Optional[str]:
    """Load code style guidelines with room-level override.

    Priority:
    1. Room-level code style takes precedence (returned as-is).
    2. If detected_languages is non-empty, combine universal + language-specific styles.
    3. Otherwise fall back to CodeStyleLoader (file-based .ai/code-style.md).

    Args:
        room_code_style: Optional room-level code style string.
        detected_languages: Optional list of language strings from the extension
            (e.g., ["python", "javascript"]).

    Returns:
        Style guidelines string, or None if unavailable.
    """
    if room_code_style:
        return room_code_style

    if detected_languages:
        try:
            from app.agent.style_loader import Language, _read_builtin_style, _read_universal_style

            parts = [_read_universal_style()]
            loaded_languages = []

            for lang_str in detected_languages:
                try:
                    lang_enum = Language(lang_str)
                    parts.append(_read_builtin_style(lang_enum))
                    loaded_languages.append(lang_str)
                except (ValueError, FileNotFoundError):
                    logger.debug(f"Skipping unknown/missing language style: {lang_str}")

            logger.info(
                f"Loaded style guidelines for detected languages: {loaded_languages} "
                f"(requested: {detected_languages})"
            )

            if len(parts) > 1:
                return "\n\n---\n\n".join(parts)
            # Only universal was loaded (all languages were invalid)
            return parts[0]
        except Exception as e:
            logger.debug(f"Could not load detected language styles: {e}")

    try:
        from app.agent.style_loader import CodeStyleLoader

        loader = CodeStyleLoader()
        style, _source = loader.get_style()
        if style:
            return style
    except Exception as e:
        logger.debug(f"Could not load style guidelines: {e}")

    return None
