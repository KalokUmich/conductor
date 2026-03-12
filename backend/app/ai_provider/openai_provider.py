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
from typing import Any, Dict, List, Optional

from .base import AIProvider, ChatMessage, DecisionSummary, TokenUsage, ToolCall, ToolUseResponse
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

    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int = 4096,
        system: str | None = None,
    ) -> ToolUseResponse:
        """Send messages with tool definitions via the OpenAI Chat Completions API.

        Messages arrive in Bedrock Converse format and are converted to
        OpenAI Chat Completions format before sending.
        """
        client = self._get_client()

        # Convert tool definitions to OpenAI format
        openai_tools = []
        for tool in tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })

        oai_messages = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        oai_messages.extend(_converse_to_openai(messages))

        create_kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "messages": oai_messages,
        }
        if openai_tools:
            create_kwargs["tools"] = openai_tools

        response = client.chat.completions.create(**create_kwargs)

        choice = response.choices[0]
        msg = choice.message

        text = msg.content or ""
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tool_calls.append(ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    input=args,
                ))

        # Map OpenAI finish_reason to our stop_reason
        stop_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        stop_reason = stop_map.get(choice.finish_reason, choice.finish_reason or "end_turn")

        # Extract token usage from OpenAI response
        usage = None
        if hasattr(response, "usage") and response.usage:
            u = response.usage
            usage = TokenUsage(
                input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                output_tokens=getattr(u, "completion_tokens", 0) or 0,
                total_tokens=getattr(u, "total_tokens", 0) or 0,
            )

        return ToolUseResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw=response,
            usage=usage,
        )


def _converse_to_openai(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Bedrock Converse message format to OpenAI Chat Completions format.

    Bedrock Converse uses:
      - {"role": "user",      "content": [{"text": "..."}]}
      - {"role": "assistant", "content": [{"text": "..."}, {"toolUse": {...}}]}
      - {"role": "user",      "content": [{"toolResult": {...}}]}

    OpenAI Chat Completions uses:
      - {"role": "user",      "content": "..."}
      - {"role": "assistant", "content": "...", "tool_calls": [{...}]}
      - {"role": "tool",      "tool_call_id": "...", "content": "..."}
    """
    converted = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", [])

        # Already a plain string — pass through
        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        has_tool_use = any("toolUse" in b for b in content)
        has_tool_result = any("toolResult" in b for b in content)

        if has_tool_use and role == "assistant":
            # Assistant message with tool calls
            text_parts = [b["text"] for b in content if "text" in b]
            oai_tool_calls = []
            for b in content:
                if "toolUse" in b:
                    tu = b["toolUse"]
                    oai_tool_calls.append({
                        "id": tu["toolUseId"],
                        "type": "function",
                        "function": {
                            "name": tu["name"],
                            "arguments": json.dumps(tu.get("input", {})),
                        },
                    })
            oai_msg: Dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(text_parts) if text_parts else None,
            }
            if oai_tool_calls:
                oai_msg["tool_calls"] = oai_tool_calls
            converted.append(oai_msg)

        elif has_tool_result:
            # Tool results → one "tool" message per result
            for b in content:
                if "toolResult" in b:
                    tr = b["toolResult"]
                    result_content = tr.get("content", [])
                    if isinstance(result_content, list):
                        text_parts = [c["text"] for c in result_content if "text" in c]
                        result_text = "\n".join(text_parts)
                    else:
                        result_text = str(result_content)
                    converted.append({
                        "role": "tool",
                        "tool_call_id": tr["toolUseId"],
                        "content": result_text,
                    })

        else:
            # Regular user/assistant message
            text_parts = [b["text"] for b in content if "text" in b]
            converted.append({
                "role": role,
                "content": "\n".join(text_parts) if text_parts else "",
            })

    return converted

