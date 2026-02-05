"""Chat summary extraction endpoint.

This module provides keyword-based extraction of structured summaries
from chat message history. It identifies goals, constraints, decisions,
open questions, and non-goals based on message content.

Current Implementation:
    Uses simple keyword matching (no LLM). This is fast and deterministic
    but may miss nuanced discussions.

Keyword Patterns:
    - Goals: "goal:", "objective:", "aim:", "target:"
    - Constraints: "constraint:", "limitation:", "must not", "cannot"
    - Decisions: "decided:", "decision:", "we will", "agreed:"
    - Questions: "?", "question:", "unclear:", "tbd"
    - Non-goals: "non-goal:", "out of scope:", "won't"

Future LLM Integration:
    Replace keyword matching with LLM-based summarization for:
    - Better understanding of implicit goals
    - Contextual decision extraction
    - Multi-message reasoning
"""
from typing import List

from fastapi import APIRouter

from .schemas import ChatMessage, SummaryRequest, SummaryResponse

router = APIRouter(prefix="/summary", tags=["summary"])


# =============================================================================
# Keyword Patterns for Extraction
# =============================================================================

# Keywords that indicate a goal statement
GOAL_KEYWORDS = ("goal:", "objective:", "aim:", "target:")

# Keywords that indicate a constraint or limitation
CONSTRAINT_KEYWORDS = (
    "constraint:", "limitation:", "must not", "cannot", "restricted"
)

# Keywords that indicate a decision was made
DECISION_KEYWORDS = (
    "decided:", "decision:", "we will", "agreed:", "let's go with"
)

# Keywords that indicate an open question
QUESTION_KEYWORDS = ("question:", "unclear:", "need to discuss", "tbd", "to be determined")

# Keywords that indicate something is explicitly out of scope
NON_GOAL_KEYWORDS = ("non-goal:", "out of scope:", "not going to", "won't", "will not")


# =============================================================================
# Extraction Logic
# =============================================================================


def extract_summary_from_messages(messages: List[ChatMessage]) -> SummaryResponse:
    """Extract a structured summary from chat messages.

    Scans messages for keyword patterns and extracts relevant content.
    Does NOT invent information - only extracts what's explicitly stated.

    Args:
        messages: List of ChatMessage objects from the session.

    Returns:
        SummaryResponse with extracted goal, constraints, decisions,
        open_questions, and non_goals.
    """
    goal = ""
    constraints: List[str] = []
    decisions: List[str] = []
    open_questions: List[str] = []
    non_goals: List[str] = []

    for msg in messages:
        content_lower = msg.content.lower()
        original_content = msg.content

        # Extract goal (first matching keyword wins)
        if any(kw in content_lower for kw in GOAL_KEYWORDS):
            for keyword in GOAL_KEYWORDS:
                if keyword in content_lower:
                    idx = content_lower.find(keyword)
                    goal = original_content[idx + len(keyword):].strip()
                    # Take only the first line
                    if "\n" in goal:
                        goal = goal.split("\n")[0].strip()
                    break

        # Extract constraints
        if any(kw in content_lower for kw in CONSTRAINT_KEYWORDS):
            constraints.append(original_content.strip())

        # Extract decisions
        if any(kw in content_lower for kw in DECISION_KEYWORDS):
            decisions.append(original_content.strip())

        # Extract open questions (messages with "?")
        if "?" in content_lower or any(kw in content_lower for kw in QUESTION_KEYWORDS):
            if "?" in content_lower:
                open_questions.append(original_content.strip())

        # Extract non-goals
        if any(kw in content_lower for kw in NON_GOAL_KEYWORDS):
            non_goals.append(original_content.strip())

    return SummaryResponse(
        goal=goal,
        constraints=constraints,
        decisions=decisions,
        open_questions=open_questions,
        non_goals=non_goals
    )


# =============================================================================
# Endpoints
# =============================================================================


@router.post("", response_model=SummaryResponse)
async def create_summary(request: SummaryRequest) -> SummaryResponse:
    """Generate a structured summary from chat messages.

    Extracts key information using keyword pattern matching:
    - goal: The main objective of the session
    - constraints: Limitations or restrictions mentioned
    - decisions: Decisions that were made
    - open_questions: Unresolved questions
    - non_goals: Things explicitly out of scope

    Args:
        request: SummaryRequest containing chat message history.

    Returns:
        SummaryResponse with structured summary data.

    Note:
        This uses keyword-based extraction (no LLM). It's fast but
        may miss nuanced or implicit information.
    """
    return extract_summary_from_messages(request.messages)

