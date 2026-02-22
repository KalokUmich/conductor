"""OpenAI API provider implementation.

This module provides an AIProvider implementation that connects to
OpenAI's API using the official SDK.

Usage:
    provider = OpenAIProvider(api_key="sk-...")
    if provider.health_check():
        summary = provider.summarize_structured(messages)
"""
import json
import logging
from typing import List, Optional

from .base import AIProvider, ChatMessage, DecisionSummary
from .prompts import get_summary_prompt

logger = logging.getLogger(__name__)


class OpenAIProvider(AIProvider):
    """AIProvider implementation using OpenAI's API.

    This provider connects to OpenAI's API endpoint.
    It requires an OpenAI API key for authentication.

    Attributes:
        api_key: OpenAI API key for authentication.
        model: OpenAI model to use (default: gpt-4o).
        organization: Optional organization ID.
    """

    DEFAULT_MODEL = "gpt-4o"

    def __init__(
        self,
        api_key: str,
        model: Optional[str] = None,
        organization: Optional[str] = None,
    ) -> None:
        """Initialize the OpenAI provider.

        Args:
            api_key: OpenAI API key for authentication.
            model: OpenAI model to use. Defaults to gpt-4o.
            organization: Optional organization ID.
        """
        self.api_key = api_key
        self.model = model or self.DEFAULT_MODEL
        self.organization = organization
        self._client: Optional[object] = None

    def _get_client(self) -> object:
        """Get or create the OpenAI client.

        Returns:
            OpenAI client instance.

        Raises:
            ImportError: If openai package is not installed.
        """
        if self._client is None:
            try:
                import openai
                kwargs = {"api_key": self.api_key}
                if self.organization:
                    kwargs["organization"] = self.organization
                self._client = openai.OpenAI(**kwargs)
            except ImportError:
                raise ImportError(
                    "openai package is required for OpenAIProvider. "
                    "Install it with: pip install openai"
                )
        return self._client

    def health_check(self) -> bool:
        """Check if the OpenAI API is accessible.

        Attempts a minimal API call to verify connectivity.

        Returns:
            bool: True if the API is accessible, False otherwise.
        """
        try:
            client = self._get_client()
            # Perform a minimal request to verify API connectivity
            client.chat.completions.create(
                model=self.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True
        except Exception as e:
            logger.warning(f"OpenAI health check failed: {e}")
            return False

    def summarize(self, messages: List[str]) -> str:
        """Generate a summary of the provided messages using OpenAI.

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

        response = client.chat.completions.create(
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

        return response.choices[0].message.content

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

        response = client.chat.completions.create(
            model=self.model,
            max_tokens=2048,
            messages=[
                {
                    "role": "user",
                    "content": prompt,
                }
            ],
        )

        response_text = response.choices[0].message.content.strip()

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
        """Call the OpenAI model with a raw prompt.

        Args:
            prompt:     The user-turn prompt to send to the model.
            max_tokens: Maximum tokens in the response.
            system:     Optional system instruction prepended as a
                        ``role: "system"`` message.

        Returns:
            str: The model's response text.

        Raises:
            Exception: If the API call fails.
        """
        client = self._get_client()

        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        response = client.chat.completions.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=messages,
        )

        return response.choices[0].message.content.strip()

