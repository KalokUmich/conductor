"""Two-stage AI summary pipeline.

This module implements a two-stage pipeline for generating targeted summaries:
1. Classification: Classify the discussion type
2. Targeted Summary: Generate a specialized summary based on the classification

Usage:
    from app.ai_provider.pipeline import run_summary_pipeline

    result = run_summary_pipeline(messages, provider)
"""
import json
import logging
from dataclasses import dataclass
from typing import List

from .base import AIProvider, ChatMessage
from .prompts import (
    DiscussionType,
    get_classification_prompt,
    get_code_relevant_items_prompt,
    get_targeted_summary_prompt,
)

logger = logging.getLogger(__name__)


@dataclass
class ClassificationResult:
    """Result of discussion classification."""
    discussion_type: DiscussionType
    confidence: float


@dataclass
class CodeRelevantItem:
    """A discrete, actionable implementation task extracted from the summary."""
    id: str           # "item-1", "item-2", ...
    type: str         # api_design|code_change|product_flow|architecture|debugging
    title: str        # Short imperative title
    problem: str      # Specific problem this item addresses
    proposed_change: str  # What code change to make
    targets: List[str]    # File paths or component names
    risk_level: str       # low|medium|high


@dataclass
class PipelineSummary:
    """Result of the two-stage summary pipeline."""
    type: str = "decision_summary"
    topic: str = ""
    core_problem: str = ""
    proposed_solution: str = ""
    requires_code_change: bool = False
    impact_scope: str = "local"
    affected_components: List[str] = None
    risk_level: str = "low"
    next_steps: List[str] = None
    # Pipeline metadata
    discussion_type: str = "general"
    classification_confidence: float = 0.0
    # Code-relevant types for selective code prompt generation
    code_relevant_types: List[str] = None
    # Structured implementation items extracted from the summary
    code_relevant_items: List[CodeRelevantItem] = None

    def __post_init__(self):
        if self.affected_components is None:
            self.affected_components = []
        if self.next_steps is None:
            self.next_steps = []
        if self.code_relevant_types is None:
            self.code_relevant_types = []
        if self.code_relevant_items is None:
            self.code_relevant_items = []


def compute_code_relevant_types(
    discussion_type: str,
    requires_code_change: bool,
    proposed_solution: str = "",
) -> List[str]:
    """Compute which discussion types are code-relevant.

    Determines which types should be included in code prompt generation
    based on the discussion characteristics.

    Rules:
    - "code_change" is always included
    - "architecture" is included if implementation change is required
    - "api_design" is included if backend logic is affected
    - "product_flow" is included if backend state changes are required
    - "debugging" is included if a fix is needed
    - "innovation" is excluded unless it requires code
    - "general" is excluded unless it explicitly mentions implementation

    Args:
        discussion_type: The classified discussion type.
        requires_code_change: Whether code changes are required.
        proposed_solution: The proposed solution text for heuristics.

    Returns:
        List of code-relevant type strings.
    """
    code_relevant = []

    # code_change is always code-relevant
    if discussion_type == "code_change":
        code_relevant.append("code_change")

    # architecture is code-relevant if implementation is required
    elif discussion_type == "architecture":
        if requires_code_change:
            code_relevant.append("architecture")
            code_relevant.append("code_change")  # Implies code changes
        else:
            # Check for implementation keywords in solution
            impl_keywords = ["implement", "refactor", "migrate", "create", "modify", "change"]
            solution_lower = proposed_solution.lower()
            if any(kw in solution_lower for kw in impl_keywords):
                code_relevant.append("architecture")

    # api_design is code-relevant if backend logic is affected
    elif discussion_type == "api_design":
        if requires_code_change:
            code_relevant.append("api_design")
            code_relevant.append("code_change")
        else:
            # Check for backend/endpoint keywords
            backend_keywords = ["endpoint", "handler", "route", "controller", "service", "backend"]
            solution_lower = proposed_solution.lower()
            if any(kw in solution_lower for kw in backend_keywords):
                code_relevant.append("api_design")

    # product_flow is code-relevant if backend state changes required
    elif discussion_type == "product_flow":
        if requires_code_change:
            code_relevant.append("product_flow")
            code_relevant.append("code_change")
        else:
            # Check for state/backend keywords
            state_keywords = ["state", "database", "backend", "server", "api", "persist", "store"]
            solution_lower = proposed_solution.lower()
            if any(kw in solution_lower for kw in state_keywords):
                code_relevant.append("product_flow")

    # debugging is code-relevant if fix is needed
    elif discussion_type == "debugging":
        if requires_code_change:
            code_relevant.append("debugging")
            code_relevant.append("code_change")
        else:
            # Check for fix keywords
            fix_keywords = ["fix", "patch", "resolve", "correct", "repair", "update"]
            solution_lower = proposed_solution.lower()
            if any(kw in solution_lower for kw in fix_keywords):
                code_relevant.append("debugging")

    # innovation is excluded unless it requires code
    elif discussion_type == "innovation":
        if requires_code_change:
            code_relevant.append("innovation")
            code_relevant.append("code_change")
        # Otherwise excluded - no code relevance for pure ideation

    # general is excluded unless it explicitly mentions implementation
    elif discussion_type == "general":
        if requires_code_change:
            code_relevant.append("general")
            code_relevant.append("code_change")
        else:
            # Check for explicit implementation mentions
            impl_keywords = ["implement", "code", "develop", "build", "create file", "modify file"]
            solution_lower = proposed_solution.lower()
            if any(kw in solution_lower for kw in impl_keywords):
                code_relevant.append("general")

    # Ensure code_change is always present if any types are code-relevant
    if code_relevant and "code_change" not in code_relevant:
        code_relevant.insert(0, "code_change")

    # Remove duplicates while preserving order
    seen = set()
    unique_types = []
    for t in code_relevant:
        if t not in seen:
            seen.add(t)
            unique_types.append(t)

    return unique_types


def _strip_markdown_code_block(text: str) -> str:
    """Strip markdown code block wrappers from text.

    Args:
        text: Text that may be wrapped in ```json ... ``` blocks.

    Returns:
        Text with code block wrappers removed.
    """
    text = text.strip()
    if text.startswith("```"):
        # Find the end of the first line (e.g., ```json)
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        # Remove trailing ```
        if text.endswith("```"):
            text = text[:-3].strip()
    return text


def classify_discussion(
    messages: List[ChatMessage],
    provider: AIProvider
) -> ClassificationResult:
    """Classify the discussion type using the AI provider.

    Stage 1 of the pipeline: Determines what type of discussion this is
    to select the appropriate summary prompt.

    Args:
        messages: List of chat messages to classify.
        provider: The AI provider to use for classification.

    Returns:
        ClassificationResult with discussion_type and confidence.

    Raises:
        ValueError: If classification fails or JSON parsing fails.
    """
    if not messages:
        logger.warning("No messages to classify, defaulting to 'general'")
        return ClassificationResult(discussion_type="general", confidence=0.0)

    prompt = get_classification_prompt(messages)
    logger.info(f"Classifying discussion with {len(messages)} messages")

    # Call the provider
    response_text = provider.call_model(prompt)
    response_text = _strip_markdown_code_block(response_text)

    # Parse JSON response
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse classification JSON: {response_text}")
        raise ValueError(f"Invalid JSON response from classification: {e}")

    discussion_type = data.get("discussion_type", "general")
    confidence = float(data.get("confidence", 0.0))

    # Validate discussion type
    valid_types = [
        "api_design", "product_flow", "code_change",
        "architecture", "innovation", "debugging", "general"
    ]
    if discussion_type not in valid_types:
        logger.warning(f"Invalid discussion type '{discussion_type}', defaulting to 'general'")
        discussion_type = "general"

    logger.info(f"Classification result: {discussion_type} (confidence: {confidence:.2f})")
    return ClassificationResult(discussion_type=discussion_type, confidence=confidence)


def generate_targeted_summary(
    messages: List[ChatMessage],
    provider: AIProvider,
    discussion_type: DiscussionType
) -> PipelineSummary:
    """Generate a targeted summary based on discussion type.

    Stage 2 of the pipeline: Uses specialized prompts based on the
    classified discussion type.

    Args:
        messages: List of chat messages to summarize.
        provider: The AI provider to use for summarization.
        discussion_type: The classified discussion type.

    Returns:
        PipelineSummary with structured summary data.

    Raises:
        ValueError: If summarization fails or JSON parsing fails.
    """
    if not messages:
        logger.warning("No messages to summarize")
        return PipelineSummary(discussion_type=discussion_type)

    prompt = get_targeted_summary_prompt(messages, discussion_type)
    logger.info(f"Generating targeted summary for type: {discussion_type}")

    # Call the provider
    response_text = provider.call_model(prompt)
    response_text = _strip_markdown_code_block(response_text)

    # Parse JSON response
    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse summary JSON: {response_text}")
        raise ValueError(f"Invalid JSON response from summary: {e}")

    # Build PipelineSummary with validated fields
    summary = PipelineSummary(
        type="decision_summary",
        topic=data.get("topic", ""),
        core_problem=data.get("core_problem", ""),
        proposed_solution=data.get("proposed_solution", ""),
        requires_code_change=_infer_requires_code_change(data, discussion_type),
        impact_scope=data.get("impact_scope", "local"),
        affected_components=data.get("affected_components", []),
        risk_level=data.get("risk_level", "low"),
        next_steps=data.get("next_steps", []),
        discussion_type=discussion_type,
    )

    logger.info(f"Generated summary: topic='{summary.topic}', requires_code_change={summary.requires_code_change}")
    return summary


def _infer_requires_code_change(data: dict, discussion_type: DiscussionType) -> bool:
    """Infer requires_code_change based on data and discussion type.

    Uses the AI's response but applies heuristics based on discussion type
    to improve accuracy.

    Args:
        data: Parsed JSON data from AI response.
        discussion_type: The classified discussion type.

    Returns:
        Boolean indicating if code change is required.
    """
    # Get AI's assessment
    ai_assessment = data.get("requires_code_change", False)

    # Apply type-specific heuristics
    if discussion_type == "code_change":
        # Code change discussions almost always require code changes
        return True
    elif discussion_type == "debugging":
        # Debugging usually requires a fix
        # Check if a solution was proposed
        if data.get("proposed_solution") and len(data.get("proposed_solution", "")) > 20:
            return True
        return ai_assessment
    elif discussion_type == "architecture":
        # Architecture discussions often lead to code changes
        # But not always (could be planning phase)
        return ai_assessment
    elif discussion_type == "api_design":
        # API design usually requires implementation
        if data.get("affected_components") and len(data.get("affected_components", [])) > 0:
            return True
        return ai_assessment

    return ai_assessment


def _fallback_item_from_summary(summary: PipelineSummary) -> CodeRelevantItem:
    """Create a single fallback item from the summary's existing fields.

    Used when stage 4 extraction fails but requires_code_change is True,
    so we still provide at least one item.

    Args:
        summary: The PipelineSummary to derive the item from.

    Returns:
        A single CodeRelevantItem.
    """
    return CodeRelevantItem(
        id="item-1",
        type=summary.discussion_type if summary.discussion_type != "general" else "code_change",
        title=summary.topic or "Implement proposed changes",
        problem=summary.core_problem or "See summary for details",
        proposed_change=summary.proposed_solution or "See summary for details",
        targets=summary.affected_components or [],
        risk_level=summary.risk_level or "low",
    )


def extract_code_relevant_items(
    summary: PipelineSummary,
    provider: AIProvider,
) -> List[CodeRelevantItem]:
    """Extract discrete implementation items from a pipeline summary.

    Stage 4 of the pipeline: Uses the AI provider to decompose the summary
    into actionable implementation tasks.

    Args:
        summary: The PipelineSummary from stages 1-3.
        provider: The AI provider to use for extraction.

    Returns:
        List of CodeRelevantItem objects.

    Raises:
        ValueError: If extraction fails or JSON parsing fails.
    """
    prompt = get_code_relevant_items_prompt(summary)
    logger.info(f"Extracting code-relevant items for topic: {summary.topic}")

    response_text = provider.call_model(prompt)
    response_text = _strip_markdown_code_block(response_text)

    try:
        data = json.loads(response_text)
    except json.JSONDecodeError as e:
        logger.error(f"Failed to parse code-relevant items JSON: {response_text}")
        raise ValueError(f"Invalid JSON response from code-relevant items extraction: {e}")

    if not isinstance(data, list):
        logger.error(f"Expected JSON array, got: {type(data)}")
        raise ValueError("Expected JSON array for code-relevant items")

    valid_types = {"api_design", "code_change", "product_flow", "architecture", "debugging"}
    valid_risks = {"low", "medium", "high"}

    items = []
    for i, item_data in enumerate(data):
        item_type = item_data.get("type", "code_change")
        if item_type not in valid_types:
            item_type = "code_change"

        risk = item_data.get("risk_level", "low")
        if risk not in valid_risks:
            risk = "low"

        # Assign sequential ID if omitted
        item_id = item_data.get("id", f"item-{i + 1}")

        items.append(CodeRelevantItem(
            id=item_id,
            type=item_type,
            title=item_data.get("title", ""),
            problem=item_data.get("problem", ""),
            proposed_change=item_data.get("proposed_change", ""),
            targets=item_data.get("targets", []),
            risk_level=risk,
        ))

    # Ensure sequential IDs
    for i, item in enumerate(items):
        item.id = f"item-{i + 1}"

    logger.info(f"Extracted {len(items)} code-relevant items")
    return items


def run_summary_pipeline(
    messages: List[ChatMessage],
    provider: AIProvider
) -> PipelineSummary:
    """Run the complete two-stage summary pipeline.

    This is the main entry point for the pipeline. It:
    1. Classifies the discussion type
    2. Generates a targeted summary based on the classification
    3. Computes code-relevant types for selective code prompt generation

    Args:
        messages: List of chat messages to process.
        provider: The AI provider to use.

    Returns:
        PipelineSummary with complete summary data including classification metadata
        and code_relevant_types.

    Raises:
        ValueError: If any stage fails.
    """
    logger.info(f"Starting summary pipeline with {len(messages)} messages")

    # Stage 1: Classification
    classification = classify_discussion(messages, provider)

    # Stage 2: Targeted Summary
    summary = generate_targeted_summary(messages, provider, classification.discussion_type)

    # Add classification metadata
    summary.classification_confidence = classification.confidence

    # Stage 3: Compute code-relevant types
    summary.code_relevant_types = compute_code_relevant_types(
        discussion_type=summary.discussion_type,
        requires_code_change=summary.requires_code_change,
        proposed_solution=summary.proposed_solution,
    )

    # Stage 4: Extract code-relevant items (only when code changes are indicated)
    if summary.requires_code_change or summary.code_relevant_types:
        try:
            summary.code_relevant_items = extract_code_relevant_items(summary, provider)
        except Exception as e:
            logger.warning(f"Stage 4 (item extraction) failed: {e}")
            if summary.requires_code_change:
                summary.code_relevant_items = [_fallback_item_from_summary(summary)]
            # Otherwise leave as empty list

    logger.info(
        f"Pipeline complete: type={summary.discussion_type}, "
        f"confidence={summary.classification_confidence:.2f}, "
        f"requires_code_change={summary.requires_code_change}, "
        f"code_relevant_types={summary.code_relevant_types}, "
        f"code_relevant_items={len(summary.code_relevant_items)}"
    )

    return summary

