"""LiteLLM fallback provider implementation.

Provides an AIProvider implementation backed by LiteLLM, which supports
100+ LLM providers through a unified OpenAI-compatible interface.

This provider is designed as a **fallback** — when the native providers
(Bedrock, Anthropic Direct, OpenAI) fail their health checks, the
ProviderResolver can create a LiteLLMProvider as a backup.

LiteLLM model naming:
  - Bedrock:   "bedrock/{model_name}"   e.g. "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
  - Anthropic: "anthropic/{model_name}" e.g. "anthropic/claude-sonnet-4-20250514"
  - OpenAI:    "{model_name}"           e.g. "gpt-4o"
  - Google:    "gemini/{model_name}"    e.g. "gemini/gemini-2.0-flash"

Usage:
    provider = LiteLLMProvider(
        model="bedrock/anthropic.claude-3-haiku-20240307-v1:0",
        aws_access_key_id="...",
        aws_secret_access_key="...",
        aws_region_name="us-east-1",
    )
    if provider.health_check():
        response = provider.chat_with_tools(messages, tools)
"""
import json
import logging
import uuid
from typing import Any, Dict, List, Optional

from .base import AIProvider, ChatMessage, DecisionSummary, TokenUsage, ToolCall, ToolUseResponse
from .prompts import get_summary_prompt

logger = logging.getLogger(__name__)


def _check_litellm_available() -> bool:
    """Check if litellm is installed."""
    try:
        import litellm  # noqa: F401
        return True
    except ImportError:
        return False


# ---------------------------------------------------------------------------
# Bedrock Converse → OpenAI message format conversion
#
# The agent loop uses Bedrock Converse as the canonical internal format.
# LiteLLM expects OpenAI-compatible messages.  This converter handles:
#   - text blocks → plain string content
#   - toolUse blocks → assistant tool_calls
#   - toolResult blocks → role: "tool" messages
# ---------------------------------------------------------------------------

def _converse_to_openai(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert Bedrock Converse messages to OpenAI Chat Completions format.

    Bedrock Converse uses:
      {"role": "user",      "content": [{"text": "..."}]}
      {"role": "assistant", "content": [{"text": "..."}, {"toolUse": {...}}]}
      {"role": "user",      "content": [{"toolResult": {...}}]}

    OpenAI Chat Completions uses:
      {"role": "user",      "content": "..."}
      {"role": "assistant", "content": "...", "tool_calls": [{...}]}
      {"role": "tool",      "tool_call_id": "...", "content": "..."}
    """
    converted: List[Dict[str, Any]] = []
    for msg in messages:
        role = msg["role"]
        content = msg.get("content", [])

        if isinstance(content, str):
            converted.append({"role": role, "content": content})
            continue

        has_tool_use = any("toolUse" in b for b in content)
        has_tool_result = any("toolResult" in b for b in content)

        if has_tool_use and role == "assistant":
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
            text_parts = [b["text"] for b in content if "text" in b]
            converted.append({
                "role": role,
                "content": "\n".join(text_parts) if text_parts else "",
            })

    return converted


def _tools_to_openai(tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert TOOL_DEFINITIONS (Anthropic input_schema) to OpenAI format."""
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
    return openai_tools


def to_litellm_model(provider_type: str, model_name: str) -> str:
    """Map our (provider_type, model_name) to a LiteLLM model string.

    Args:
        provider_type: One of "aws_bedrock", "anthropic", "openai".
        model_name: The model ID as configured in conductor.settings.yaml.

    Returns:
        LiteLLM-compatible model string.

    Examples:
        ("aws_bedrock", "anthropic.claude-3-haiku-20240307-v1:0")
            → "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
        ("aws_bedrock", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")
            → "bedrock/eu.anthropic.claude-sonnet-4-5-20250929-v1:0"
        ("anthropic", "claude-sonnet-4-20250514")
            → "anthropic/claude-sonnet-4-20250514"
        ("openai", "gpt-4o")
            → "gpt-4o"
    """
    if provider_type == "aws_bedrock":
        return f"bedrock/{model_name}"
    elif provider_type == "anthropic":
        return f"anthropic/{model_name}"
    elif provider_type == "openai":
        return model_name
    return model_name


class LiteLLMProvider(AIProvider):
    """AIProvider fallback implementation using LiteLLM.

    LiteLLM normalises 100+ LLM providers behind an OpenAI-compatible
    interface.  This provider translates between the Bedrock Converse
    internal message format used by the agent loop and the OpenAI format
    expected by LiteLLM.

    Credentials are passed as keyword arguments and forwarded directly
    to ``litellm.completion()``.

    Attributes:
        model: LiteLLM model string (e.g. "bedrock/anthropic.claude-3-haiku-20240307-v1:0").
    """

    def __init__(self, model: str, **credentials: Any) -> None:
        """Initialise the LiteLLM provider.

        Args:
            model: LiteLLM model string.
            **credentials: Provider-specific credentials forwarded to
                litellm.completion().  Common keys:
                - api_key: Anthropic / OpenAI API key
                - aws_access_key_id, aws_secret_access_key, aws_region_name: Bedrock
        """
        self.model = model
        self._credentials = credentials
        self._litellm = None

    def _get_litellm(self):
        """Lazy-import litellm to keep it an optional dependency."""
        if self._litellm is None:
            try:
                import litellm
                litellm.drop_params = True  # silently ignore unsupported params
                self._litellm = litellm
            except ImportError:
                raise ImportError(
                    "litellm package is required for LiteLLMProvider. "
                    "Install it with: pip install litellm"
                )
        return self._litellm

    def _call(self, messages: List[Dict], max_tokens: int = 2048,
              system: Optional[str] = None,
              tools: Optional[List[Dict]] = None) -> Any:
        """Core litellm.completion() call with credentials."""
        litellm = self._get_litellm()

        oai_messages = []
        if system:
            oai_messages.append({"role": "system", "content": system})
        oai_messages.extend(messages)

        kwargs: Dict[str, Any] = {
            "model": self.model,
            "messages": oai_messages,
            "max_tokens": max_tokens,
            **self._credentials,
        }
        if tools:
            kwargs["tools"] = tools

        return litellm.completion(**kwargs)

    # ----- AIProvider interface -----

    def health_check(self) -> bool:
        try:
            self._call(
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=1,
            )
            return True
        except Exception as e:
            logger.warning("LiteLLM health check failed for %s: %s", self.model, e)
            return False

    def summarize(self, messages: List[str]) -> str:
        if not messages:
            return ""
        combined = "\n".join(messages)
        response = self._call(
            messages=[{
                "role": "user",
                "content": f"Please provide a concise summary of the following messages:\n\n{combined}",
            }],
            max_tokens=1024,
        )
        return response.choices[0].message.content

    def summarize_structured(self, messages: List[ChatMessage]) -> DecisionSummary:
        if not messages:
            return DecisionSummary()

        prompt = get_summary_prompt(messages)
        response = self._call(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=2048,
        )
        response_text = response.choices[0].message.content.strip()

        try:
            data = json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.error("LiteLLM: failed to parse JSON response: %s", response_text[:200])
            raise ValueError(f"Invalid JSON response from AI: {e}")

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
        response = self._call(
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            system=system,
        )
        return response.choices[0].message.content.strip()

    def chat_with_tools(
        self,
        messages: List[Dict[str, Any]],
        tools: List[Dict[str, Any]],
        max_tokens: int = 4096,
        system: str | None = None,
    ) -> ToolUseResponse:
        """Send messages with tool definitions via LiteLLM.

        Converts from Bedrock Converse internal format to OpenAI format,
        calls litellm.completion(), and maps the response back to our
        ToolUseResponse.
        """
        oai_messages = _converse_to_openai(messages)
        oai_tools = _tools_to_openai(tools) if tools else None

        response = self._call(
            messages=oai_messages,
            max_tokens=max_tokens,
            system=system,
            tools=oai_tools,
        )

        choice = response.choices[0]
        msg = choice.message

        text = msg.content or ""
        tool_calls: List[ToolCall] = []

        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except (json.JSONDecodeError, AttributeError):
                    args = {}
                tool_calls.append(ToolCall(
                    id=tc.id or str(uuid.uuid4()),
                    name=tc.function.name,
                    input=args,
                ))

        # Map finish_reason to our stop_reason
        finish_reason = getattr(choice, "finish_reason", None) or "stop"
        stop_map = {
            "stop": "end_turn",
            "tool_calls": "tool_use",
            "length": "max_tokens",
        }
        stop_reason = stop_map.get(finish_reason, finish_reason)

        # Extract token usage
        usage = None
        raw_usage = getattr(response, "usage", None)
        if raw_usage:
            usage = TokenUsage(
                input_tokens=getattr(raw_usage, "prompt_tokens", 0) or 0,
                output_tokens=getattr(raw_usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(raw_usage, "total_tokens", 0) or 0,
            )

        return ToolUseResponse(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw=response,
            usage=usage,
        )
