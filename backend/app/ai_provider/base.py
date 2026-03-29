"""AIProvider abstract interface for LLM integrations.

This module defines the abstract base class for all AI provider implementations.
Each provider must implement health_check() and summarize_structured() methods.

Usage:
    from app.ai_provider import AIProvider, ClaudeDirectProvider

    provider = ClaudeDirectProvider(api_key="...")
    if provider.health_check():
        summary = provider.summarize_structured(messages)
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional


@dataclass
class ChatMessage:
    """A single chat message with role and content.

    Attributes:
        role: The role of the message sender (host or engineer).
        text: The message content.
        timestamp: Unix timestamp of the message.
    """
    role: Literal["host", "engineer"]
    text: str
    timestamp: float


@dataclass
class DecisionSummary:
    """Structured summary of a conversation decision.

    Attributes:
        type: Always "decision_summary".
        topic: Brief topic of the discussion.
        problem_statement: Description of the problem being discussed.
        proposed_solution: The proposed solution or approach.
        requires_code_change: Whether the solution requires code changes.
        affected_components: List of components/files that may be affected.
        risk_level: Risk assessment (low, medium, high).
        next_steps: List of action items or next steps.
    """
    type: Literal["decision_summary"] = "decision_summary"
    topic: str = ""
    problem_statement: str = ""
    proposed_solution: str = ""
    requires_code_change: bool = False
    affected_components: List[str] = None
    risk_level: Literal["low", "medium", "high"] = "low"
    next_steps: List[str] = None

    def __post_init__(self):
        if self.affected_components is None:
            self.affected_components = []
        if self.next_steps is None:
            self.next_steps = []

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {
            "type": self.type,
            "topic": self.topic,
            "problem_statement": self.problem_statement,
            "proposed_solution": self.proposed_solution,
            "requires_code_change": self.requires_code_change,
            "affected_components": self.affected_components,
            "risk_level": self.risk_level,
            "next_steps": self.next_steps,
        }


class AIProvider(ABC):
    """Abstract base class for AI provider implementations.

    All AI providers (Claude Direct, Claude Bedrock, etc.) must implement
    this interface to ensure consistent behavior across the application.

    Methods:
        health_check: Verify the provider is operational.
        summarize: Generate a simple summary from a list of messages.
        summarize_structured: Generate a structured decision summary.
    """

    @abstractmethod
    def health_check(self) -> bool:
        """Check if the AI provider is healthy and operational.

        This method should verify connectivity to the underlying AI service
        and return True if the service is available.

        Returns:
            bool: True if the provider is operational, False otherwise.
        """
        pass

    @abstractmethod
    def summarize(self, messages: List[str]) -> str:
        """Generate a summary from a list of messages.

        Args:
            messages: List of message strings to summarize.

        Returns:
            str: A concise summary of the provided messages.

        Raises:
            Exception: If the summarization request fails.
        """
        pass

    @abstractmethod
    def summarize_structured(self, messages: List[ChatMessage]) -> DecisionSummary:
        """Generate a structured decision summary from chat messages.

        Args:
            messages: List of ChatMessage objects to summarize.

        Returns:
            DecisionSummary: A structured summary with topic, problem,
                solution, and other decision-related fields.

        Raises:
            Exception: If the summarization request fails.
        """
        pass

    @abstractmethod
    def call_model(
        self,
        prompt: str,
        max_tokens: int = 2048,
        system: str | None = None,
        assistant_prefix: str | None = None,
    ) -> str:
        """Call the AI model with a raw prompt and return the response text.

        This is a low-level method for direct model interaction, used by
        the pipeline for classification, summarization, and code explanation.

        Args:
            prompt:           The user-turn prompt to send to the model.
            max_tokens:       Maximum tokens in the response (default: 2048).
            system:           Optional system-role instruction prepended before
                              the user message.  Supported by all three providers.
            assistant_prefix: Optional string to prepend as the start of the
                              assistant response (prefilling).  The model will
                              continue generating from this prefix.  Useful for
                              forcing structured output (e.g. ``"["`` for JSON
                              arrays).  The prefix is **not** included in the
                              returned text.

        Returns:
            str: The model's response text.

        Raises:
            Exception: If the API call fails.
        """
        pass

    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int = 4096,
        system: str | None = None,
        temperature: float | None = None,
    ) -> "ToolUseResponse":
        """Send messages with tool definitions and get back a response
        that may contain tool_use requests.

        Args:
            messages:   Conversation messages in provider-native format.
            tools:      Tool definitions (JSON Schema).
            max_tokens: Maximum tokens in the response.
            system:     Optional system instruction.

        Returns:
            ToolUseResponse with text content and/or tool calls.

        Raises:
            NotImplementedError: If the provider doesn't support tool use.
        """
        raise NotImplementedError(
            f"{type(self).__name__} does not support chat_with_tools"
        )


@dataclass
class ToolCall:
    """A single tool invocation requested by the model."""
    id: str
    name: str
    input: Dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    """Token counts from a single LLM call."""
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_write_input_tokens: int = 0


@dataclass
class ToolUseResponse:
    """Response from chat_with_tools — may contain text and/or tool calls."""
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    stop_reason: str = ""  # "end_turn", "tool_use", "max_tokens"
    raw: Any = None  # Provider-specific raw response
    usage: Optional[TokenUsage] = None  # Token counts (if provider returns them)

