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
from typing import Any, Dict, List, Optional

from .base import AIProvider, ChatMessage, DecisionSummary, TokenUsage, ToolCall, ToolUseResponse
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
        temperature: float | None = None,
        assistant_prefix: str | None = None,
    ) -> str:
        """Call the Claude model with a raw prompt.

        Args:
            prompt:           The user-turn prompt to send to the model.
            max_tokens:       Maximum tokens in the response.
            system:           Optional system instruction passed as the ``system``
                              parameter of the Messages API.
            assistant_prefix: Optional string to prefill the assistant response.

        Returns:
            str: The model's response text.

        Raises:
            Exception: If the API call fails.
        """
        client = self._get_client()

        messages: list = [{"role": "user", "content": prompt}]
        if assistant_prefix:
            messages.append({"role": "assistant", "content": assistant_prefix})

        kwargs: dict = {
            "model":      self.model,
            "max_tokens": max_tokens,
            "messages":   messages,
        }
        if system:
            kwargs["system"] = system

        response = client.messages.create(**kwargs)
        text = response.content[0].text.strip()
        if assistant_prefix:
            return assistant_prefix + text
        return text

    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int = 4096,
        system: str | None = None,
        temperature: float | None = None,
    ) -> ToolUseResponse:
        """Send messages with tool definitions via the Anthropic Messages API.

        Uses the native tool_use support in the Anthropic API.
        Messages arrive in Bedrock Converse format and are converted to
        Anthropic Messages API format before sending.
        """
        client = self._get_client()

        # Convert tool definitions to Anthropic format
        anthropic_tools = []
        for tool in tools:
            anthropic_tools.append({
                "name": tool["name"],
                "description": tool.get("description", ""),
                "input_schema": tool.get("input_schema", {}),
            })

        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": _converse_to_anthropic(messages),
        }
        if anthropic_tools:
            kwargs["tools"] = anthropic_tools
        if system:
            kwargs["system"] = system

        response = client.messages.create(**kwargs)

        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id,
                    name=block.name,
                    input=block.input,
                ))

        # Extract token usage from Anthropic Messages API response
        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = TokenUsage(
                input_tokens=getattr(u, "input_tokens", 0),
                output_tokens=getattr(u, "output_tokens", 0),
                total_tokens=getattr(u, "input_tokens", 0) + getattr(u, "output_tokens", 0),
                cache_read_input_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
                cache_write_input_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
            )

        return ToolUseResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=response.stop_reason,
            raw=response,
            usage=usage,
        )


def _converse_to_anthropic(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Bedrock Converse message format to Anthropic Messages API format.

    Bedrock Converse uses:
      - {"text": "..."}
      - {"toolUse": {"toolUseId": ..., "name": ..., "input": ...}}
      - {"toolResult": {"toolUseId": ..., "content": [{"text": "..."}]}}

    Anthropic Messages API uses:
      - {"type": "text", "text": "..."}
      - {"type": "tool_use", "id": ..., "name": ..., "input": ...}
      - {"type": "tool_result", "tool_use_id": ..., "content": "..."}
    """
    converted = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", [])

        # Already a plain string — pass through
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        anthropic_blocks = []
        for block in content:
            if "text" in block and "toolUse" not in block and "toolResult" not in block:
                anthropic_blocks.append({"type": "text", "text": block["text"]})
            elif "toolUse" in block:
                tu = block["toolUse"]
                anthropic_blocks.append({
                    "type": "tool_use",
                    "id": tu["toolUseId"],
                    "name": tu["name"],
                    "input": tu.get("input", {}),
                })
            elif "toolResult" in block:
                tr = block["toolResult"]
                result_content = tr.get("content", [])
                if isinstance(result_content, list):
                    text_parts = [c["text"] for c in result_content if "text" in c]
                    result_text = "\n".join(text_parts)
                else:
                    result_text = str(result_content)
                anthropic_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tr["toolUseId"],
                    "content": result_text,
                })

        converted.append({"role": role, "content": anthropic_blocks})
    return converted
