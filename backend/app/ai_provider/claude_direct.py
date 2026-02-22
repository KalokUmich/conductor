"""Claude Direct API provider implementation.

This module provides an AIProvider implementation that connects directly
to Anthropic's Claude API using the official SDK.

Usage:
    provider = ClaudeDirectProvider(api_key="sk-ant-...")
    if provider.health_check():
        summary = provider.summarize_structured(messages)
"""
import json
import logging
from typing import List, Optional

from .base import AIProvider, ChatMessage, DecisionSummary
from .prompts import get_summary_prompt

logger = logging.getLogger(__name__)


class ClaudeDirectProvider(AIProvider):
    """AIProvider implementation using Anthropic's Claude API directly.

    This provider connects to Claude via Anthropic's official API endpoint.
    It requires an Anthropic API key for authentication.

    Attributes:
        api_key: Anthropic API key for authentication.
        model: Claude model to use (default: claude-3-sonnet-20240229).
        base_url: Anthropic API base URL.
    """

    DEFAULT_MODEL = "claude-3-sonnet-20240229"
    DEFAULT_BASE_URL = "https://api.anthropic.com"

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        base_url: Optional[str] = None,
    ) -> None:
        """Initialize the Claude Direct provider.

        Args:
            api_key: Anthropic API key for authentication.
            model: Claude model to use. Defaults to claude-3-sonnet-20240229.
            base_url: Optional custom API base URL.
        """
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.base_url = base_url or self.DEFAULT_BASE_URL
        self._client: Optional[object] = None

    def _get_client(self) -> object:
        """Get or create the Anthropic client.

        Returns:
            Anthropic client instance.

        Raises:
            ImportError: If anthropic package is not installed.
        """
        if self._client is None:
            try:
                import anthropic
                self._client = anthropic.Anthropic(
                    api_key=self.api_key,
                    base_url=self.base_url,
                )
            except ImportError:
                raise ImportError(
                    "anthropic package is required for ClaudeDirectProvider. "
                    "Install it with: pip install anthropic"
                )
        return self._client

    def health_check(self) -> bool:
        """Check if the Claude Direct API is accessible.

        Attempts a minimal API call to verify connectivity.

        Returns:
            bool: True if the API is accessible, False otherwise.
        """
        try:
            client = self._get_client()
            # Perform a minimal request to verify API connectivity
            client.messages.create(
                model=self.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True
        except Exception as e:
            logger.warning(f"Claude Direct health check failed: {e}")
            return False

    def summarize(self, messages: List[str]) -> str:
        """Generate a summary of the provided messages using Claude.

        Args:
            messages: List of message strings to summarize.

        Returns:
            str: A concise summary of the messages.

        Raises:
            Exception: If the API call fails.
        """
        if not messages:
            return ""

        client = self._get_client()
        combined_messages = "\n".join(messages)

        response = client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[
                {
                    "role": "user",
                    "content": (
                        "Please provide a concise summary of the following messages:\n\n"
                        f"{combined_messages}"
                    ),
                }
            ],
        )

        return response.content[0].text

    def summarize_structured(self, messages: List[ChatMessage]) -> DecisionSummary:
        """Generate a structured decision summary from chat messages.

        Args:
            messages: List of ChatMessage objects to summarize.

        Returns:
            DecisionSummary: A structured summary with topic, problem,
                solution, and other decision-related fields.

        Raises:
            Exception: If the API call fails or JSON parsing fails.
        """
        if not messages:
            return DecisionSummary()

        client = self._get_client()

        # Generate prompt using shared template
        prompt = get_summary_prompt(messages)

        response = client.messages.create(
            model=self.model,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        )

        response_text = response.content[0].text.strip()

        # Parse JSON response
        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {response_text}")
            raise ValueError(f"Invalid JSON response from AI: {e}")

        # Validate and extract fields with defaults
        return DecisionSummary(
            type="decision_summary",
            topic=data.get("topic", ""),
            problem_statement=data.get("problem_statement", ""),
            proposed_solution=data.get("proposed_solution", ""),
            requires_code_change=data.get("requires_code_change", False),
            affected_components=data.get("affected_components", []),
            risk_level=data.get("risk_level", "low"),
            next_steps=data.get("next_steps", []),
        )

    def call_model(
        self,
        prompt: str,
        max_tokens: int = 2048,
        system: str | None = None,
    ) -> str:
        """Call the Claude model with a raw prompt.

        Args:
            prompt:     The user-turn prompt to send to the model.
            max_tokens: Maximum tokens in the response.
            system:     Optional system instruction passed as the ``system``
                        parameter of the Messages API.

        Returns:
            str: The model's response text.

        Raises:
            Exception: If the API call fails.
        """
        client = self._get_client()

        kwargs: dict = {
            "model":      self.model,
            "max_tokens": max_tokens,
            "messages":   [{"role": "user", "content": prompt}],
        }
        if system:
            kwargs["system"] = system

        response = client.messages.create(**kwargs)
        return response.content[0].text.strip()
