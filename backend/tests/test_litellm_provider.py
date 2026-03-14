"""Tests for LiteLLMProvider — fallback AI provider backed by LiteLLM.

Tests cover:
  - Message format conversion (Bedrock Converse → OpenAI)
  - Tool definition conversion
  - LiteLLM model name mapping
  - Provider ABC methods (health_check, call_model, chat_with_tools)
  - Token usage extraction
  - Resolver fallback integration
"""
import json
import sys
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

# The litellm stub is created in conftest.py with a MagicMock completion.
# Grab a reference to it for configuring return values in tests.
_litellm_stub = sys.modules["litellm"]

from app.ai_provider.litellm_provider import (
    LiteLLMProvider,
    _converse_to_openai,
    _tools_to_openai,
    to_litellm_model,
    _check_litellm_available,
)
from app.ai_provider.base import TokenUsage, ToolCall, ToolUseResponse


# =========================================================================
# Fixtures & helpers
# =========================================================================

@dataclass
class _MockFunction:
    name: str
    arguments: str


@dataclass
class _MockToolCall:
    id: str
    function: _MockFunction


@dataclass
class _MockUsage:
    prompt_tokens: int = 100
    completion_tokens: int = 50
    total_tokens: int = 150


@dataclass
class _MockMessage:
    content: Optional[str] = "Hello"
    tool_calls: Optional[List[_MockToolCall]] = None


@dataclass
class _MockChoice:
    message: _MockMessage = None
    finish_reason: str = "stop"

    def __post_init__(self):
        if self.message is None:
            self.message = _MockMessage()


@dataclass
class _MockResponse:
    choices: List[_MockChoice] = None
    usage: Optional[_MockUsage] = None

    def __post_init__(self):
        if self.choices is None:
            self.choices = [_MockChoice()]


def _make_provider(**kwargs) -> LiteLLMProvider:
    return LiteLLMProvider(model="bedrock/anthropic.claude-3-haiku-20240307-v1:0", **kwargs)


# =========================================================================
# to_litellm_model mapping
# =========================================================================

class TestToLiteLLMModel:
    def test_bedrock(self):
        assert to_litellm_model("aws_bedrock", "anthropic.claude-3-haiku-20240307-v1:0") == \
            "bedrock/anthropic.claude-3-haiku-20240307-v1:0"

    def test_bedrock_cross_region(self):
        assert to_litellm_model("aws_bedrock", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0") == \
            "bedrock/eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

    def test_anthropic(self):
        assert to_litellm_model("anthropic", "claude-sonnet-4-20250514") == \
            "anthropic/claude-sonnet-4-20250514"

    def test_openai(self):
        assert to_litellm_model("openai", "gpt-4o") == "gpt-4o"

    def test_unknown_provider(self):
        assert to_litellm_model("google", "gemini-2.0-flash") == "gemini-2.0-flash"


# =========================================================================
# _converse_to_openai — message format conversion
# =========================================================================

class TestConverseToOpenAI:
    def test_simple_text(self):
        msgs = [{"role": "user", "content": [{"text": "hello"}]}]
        result = _converse_to_openai(msgs)
        assert result == [{"role": "user", "content": "hello"}]

    def test_plain_string_passthrough(self):
        msgs = [{"role": "user", "content": "hi"}]
        result = _converse_to_openai(msgs)
        assert result == [{"role": "user", "content": "hi"}]

    def test_assistant_with_tool_use(self):
        msgs = [{
            "role": "assistant",
            "content": [
                {"text": "Let me search..."},
                {"toolUse": {
                    "toolUseId": "tc_1",
                    "name": "grep",
                    "input": {"pattern": "auth"},
                }},
            ],
        }]
        result = _converse_to_openai(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "assistant"
        assert result[0]["content"] == "Let me search..."
        assert len(result[0]["tool_calls"]) == 1
        tc = result[0]["tool_calls"][0]
        assert tc["id"] == "tc_1"
        assert tc["function"]["name"] == "grep"
        assert json.loads(tc["function"]["arguments"]) == {"pattern": "auth"}

    def test_tool_result(self):
        msgs = [{
            "role": "user",
            "content": [{
                "toolResult": {
                    "toolUseId": "tc_1",
                    "content": [{"text": "found 3 matches"}],
                },
            }],
        }]
        result = _converse_to_openai(msgs)
        assert len(result) == 1
        assert result[0]["role"] == "tool"
        assert result[0]["tool_call_id"] == "tc_1"
        assert result[0]["content"] == "found 3 matches"

    def test_multiple_tool_results(self):
        """Multiple toolResult blocks should produce separate tool messages."""
        msgs = [{
            "role": "user",
            "content": [
                {"toolResult": {"toolUseId": "tc_1", "content": [{"text": "result 1"}]}},
                {"toolResult": {"toolUseId": "tc_2", "content": [{"text": "result 2"}]}},
            ],
        }]
        result = _converse_to_openai(msgs)
        assert len(result) == 2
        assert result[0]["tool_call_id"] == "tc_1"
        assert result[1]["tool_call_id"] == "tc_2"

    def test_tool_result_string_content(self):
        """toolResult.content can be a plain string (non-list)."""
        msgs = [{
            "role": "user",
            "content": [{
                "toolResult": {"toolUseId": "tc_1", "content": "raw string"},
            }],
        }]
        result = _converse_to_openai(msgs)
        assert result[0]["content"] == "raw string"

    def test_assistant_tool_use_no_text(self):
        """Assistant message with toolUse but no text content."""
        msgs = [{
            "role": "assistant",
            "content": [
                {"toolUse": {"toolUseId": "tc_1", "name": "read_file", "input": {"path": "main.py"}}},
            ],
        }]
        result = _converse_to_openai(msgs)
        assert result[0]["content"] is None
        assert len(result[0]["tool_calls"]) == 1

    def test_full_conversation(self):
        """Verify a complete multi-turn conversation converts correctly."""
        msgs = [
            {"role": "user", "content": [{"text": "find auth"}]},
            {"role": "assistant", "content": [
                {"text": "Searching..."},
                {"toolUse": {"toolUseId": "tc_1", "name": "grep", "input": {"pattern": "auth"}}},
            ]},
            {"role": "user", "content": [
                {"toolResult": {"toolUseId": "tc_1", "content": [{"text": "auth.py:10 def authenticate"}]}},
            ]},
            {"role": "assistant", "content": [{"text": "Found authenticate() in auth.py."}]},
        ]
        result = _converse_to_openai(msgs)
        assert len(result) == 4
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"
        assert result[2]["role"] == "tool"
        assert result[3]["role"] == "assistant"


# =========================================================================
# _tools_to_openai — tool definition conversion
# =========================================================================

class TestToolsToOpenAI:
    def test_converts_input_schema(self):
        tools = [{
            "name": "grep",
            "description": "Search for pattern",
            "input_schema": {
                "type": "object",
                "properties": {"pattern": {"type": "string"}},
                "required": ["pattern"],
            },
        }]
        result = _tools_to_openai(tools)
        assert len(result) == 1
        assert result[0]["type"] == "function"
        assert result[0]["function"]["name"] == "grep"
        assert result[0]["function"]["parameters"]["type"] == "object"

    def test_empty_tools(self):
        assert _tools_to_openai([]) == []

    def test_missing_description(self):
        tools = [{"name": "test", "input_schema": {"type": "object"}}]
        result = _tools_to_openai(tools)
        assert result[0]["function"]["description"] == ""


# =========================================================================
# LiteLLMProvider — core methods
# =========================================================================

class TestLiteLLMProvider:
    def test_health_check_success(self):
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse()
        assert provider.health_check() is True

    def test_health_check_failure(self):
        provider = _make_provider()
        _litellm_stub.completion.side_effect = Exception("connection error")
        assert provider.health_check() is False
        _litellm_stub.completion.side_effect = None

    def test_call_model(self):
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse(
            choices=[_MockChoice(message=_MockMessage(content="  answer  "))]
        )
        result = provider.call_model("test prompt")
        assert result == "answer"

    def test_call_model_with_system(self):
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse()
        provider.call_model("prompt", system="you are helpful")

        call_kwargs = _litellm_stub.completion.call_args
        messages = call_kwargs.kwargs.get("messages") or call_kwargs[1].get("messages")
        assert messages[0]["role"] == "system"
        assert messages[0]["content"] == "you are helpful"

    def test_summarize_empty(self):
        provider = _make_provider()
        assert provider.summarize([]) == ""

    def test_summarize(self):
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse(
            choices=[_MockChoice(message=_MockMessage(content="Summary here"))]
        )
        result = provider.summarize(["msg1", "msg2"])
        assert result == "Summary here"

    def test_credentials_forwarded(self):
        provider = LiteLLMProvider(
            model="bedrock/test",
            aws_access_key_id="AK",
            aws_secret_access_key="SK",
            aws_region_name="us-east-1",
        )
        _litellm_stub.completion.return_value = _MockResponse()
        provider.call_model("hello")

        call_kwargs = _litellm_stub.completion.call_args.kwargs
        assert call_kwargs["aws_access_key_id"] == "AK"
        assert call_kwargs["aws_secret_access_key"] == "SK"
        assert call_kwargs["aws_region_name"] == "us-east-1"


# =========================================================================
# chat_with_tools
# =========================================================================

class TestChatWithTools:
    def test_text_only_response(self):
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse(
            choices=[_MockChoice(
                message=_MockMessage(content="The answer is 42"),
                finish_reason="stop",
            )],
            usage=_MockUsage(prompt_tokens=100, completion_tokens=20, total_tokens=120),
        )

        messages = [{"role": "user", "content": [{"text": "question"}]}]
        tools = [{"name": "grep", "description": "search", "input_schema": {"type": "object"}}]

        result = provider.chat_with_tools(messages, tools)
        assert isinstance(result, ToolUseResponse)
        assert result.text == "The answer is 42"
        assert result.tool_calls == []
        assert result.stop_reason == "end_turn"
        assert result.usage.input_tokens == 100
        assert result.usage.output_tokens == 20

    def test_tool_call_response(self):
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse(
            choices=[_MockChoice(
                message=_MockMessage(
                    content="Let me search",
                    tool_calls=[_MockToolCall(
                        id="call_1",
                        function=_MockFunction(
                            name="grep",
                            arguments='{"pattern": "auth"}',
                        ),
                    )],
                ),
                finish_reason="tool_calls",
            )],
        )

        result = provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "find auth"}]}],
            tools=[{"name": "grep", "description": "search", "input_schema": {}}],
        )
        assert result.stop_reason == "tool_use"
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].name == "grep"
        assert result.tool_calls[0].input == {"pattern": "auth"}

    def test_multiple_tool_calls(self):
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse(
            choices=[_MockChoice(
                message=_MockMessage(
                    content=None,
                    tool_calls=[
                        _MockToolCall(id="c1", function=_MockFunction(name="grep", arguments='{}')),
                        _MockToolCall(id="c2", function=_MockFunction(name="read_file", arguments='{"path":"a.py"}')),
                    ],
                ),
                finish_reason="tool_calls",
            )],
        )

        result = provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "test"}]}],
            tools=[],
        )
        assert len(result.tool_calls) == 2
        assert result.tool_calls[0].name == "grep"
        assert result.tool_calls[1].name == "read_file"

    def test_malformed_arguments_json(self):
        """Gracefully handle invalid JSON in tool call arguments."""
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse(
            choices=[_MockChoice(
                message=_MockMessage(
                    content="",
                    tool_calls=[_MockToolCall(
                        id="c1",
                        function=_MockFunction(name="grep", arguments="not json"),
                    )],
                ),
                finish_reason="tool_calls",
            )],
        )

        result = provider.chat_with_tools(
            messages=[{"role": "user", "content": [{"text": "test"}]}],
            tools=[],
        )
        assert len(result.tool_calls) == 1
        assert result.tool_calls[0].input == {}

    def test_no_usage_in_response(self):
        """Handle responses with no usage data."""
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse(
            choices=[_MockChoice()],
            usage=None,
        )
        result = provider.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
        )
        assert result.usage is None

    def test_max_tokens_stop_reason(self):
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse(
            choices=[_MockChoice(finish_reason="length")],
        )
        result = provider.chat_with_tools(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
        )
        assert result.stop_reason == "max_tokens"

    def test_converse_format_converted(self):
        """Verify that Bedrock Converse messages are converted before calling litellm."""
        provider = _make_provider()
        _litellm_stub.completion.return_value = _MockResponse()

        converse_msgs = [
            {"role": "user", "content": [{"text": "hello"}]},
            {"role": "assistant", "content": [
                {"text": "thinking"},
                {"toolUse": {"toolUseId": "tc1", "name": "grep", "input": {"pattern": "x"}}},
            ]},
            {"role": "user", "content": [
                {"toolResult": {"toolUseId": "tc1", "content": [{"text": "found"}]}},
            ]},
        ]

        provider.chat_with_tools(
            messages=converse_msgs,
            tools=[{"name": "grep", "description": "s", "input_schema": {}}],
        )

        call_kwargs = _litellm_stub.completion.call_args.kwargs
        oai_messages = call_kwargs["messages"]
        # user text, assistant+tool_calls, tool result
        assert oai_messages[0] == {"role": "user", "content": "hello"}
        assert oai_messages[1]["role"] == "assistant"
        assert "tool_calls" in oai_messages[1]
        assert oai_messages[2]["role"] == "tool"


# =========================================================================
# Resolver fallback integration
# =========================================================================

class TestResolverFallback:
    """Test that ProviderResolver creates LiteLLM fallback when native fails."""

    def _make_config(self, litellm_fallback: bool = True):
        from app.config import (
            ConductorConfig, SummaryConfig, AIProvidersSecretsConfig,
            AIProviderSettingsConfig, AIModelConfig,
            AnthropicSecretsConfig, AWSBedrockSecretsConfig, OpenAISecretsConfig,
        )
        return ConductorConfig(
            summary=SummaryConfig(enabled=True, default_model="test-model"),
            ai_providers=AIProvidersSecretsConfig(
                anthropic=AnthropicSecretsConfig(api_key="sk-test"),
                aws_bedrock=AWSBedrockSecretsConfig(
                    access_key_id="AK", secret_access_key="SK", region="us-east-1",
                ),
                openai=OpenAISecretsConfig(api_key="sk-oai-test"),
            ),
            ai_provider_settings=AIProviderSettingsConfig(
                anthropic_enabled=True,
                aws_bedrock_enabled=True,
                openai_enabled=True,
                litellm_fallback=litellm_fallback,
            ),
            ai_models=[
                AIModelConfig(
                    id="test-model", provider="anthropic",
                    model_name="claude-sonnet-4-20250514",
                    display_name="Test", enabled=True,
                    litellm=True,
                ),
                AIModelConfig(
                    id="bedrock-model", provider="aws_bedrock",
                    model_name="anthropic.claude-3-haiku-20240307-v1:0",
                    display_name="Bedrock Test", enabled=True,
                    litellm=True,
                ),
                AIModelConfig(
                    id="nova-model", provider="aws_bedrock",
                    model_name="amazon.nova-pro-v1:0",
                    display_name="Nova Test", enabled=True,
                    litellm=False,  # not eligible for LiteLLM
                ),
                AIModelConfig(
                    id="openai-model", provider="openai",
                    model_name="gpt-4o",
                    display_name="GPT-4o", enabled=True,
                    litellm=True,
                ),
            ],
        )

    def test_fallback_creates_litellm_when_native_fails(self):
        """When native provider fails and litellm_fallback=True, LiteLLM is used."""
        from app.ai_provider.resolver import ProviderResolver

        config = self._make_config(litellm_fallback=True)
        resolver = ProviderResolver(config)

        # Native provider fails
        with patch.object(resolver, "_create_provider", return_value=None):
            with patch(
                "app.ai_provider.resolver._check_litellm_available", return_value=True,
            ):
                with patch.object(
                    LiteLLMProvider, "health_check", return_value=True,
                ):
                    result = resolver._check_provider_health("anthropic")
                    assert result is True
                    # The cached provider should be a LiteLLMProvider
                    cached = resolver._providers.get("anthropic")
                    assert isinstance(cached, LiteLLMProvider)

    def test_no_fallback_when_disabled(self):
        """When litellm_fallback=False, no LiteLLM fallback is attempted."""
        from app.ai_provider.resolver import ProviderResolver

        config = self._make_config(litellm_fallback=False)
        resolver = ProviderResolver(config)

        with patch.object(resolver, "_create_provider", return_value=None):
            result = resolver._check_provider_health("anthropic")
            assert result is False
            assert "anthropic" not in resolver._providers

    def test_fallback_passes_bedrock_credentials(self):
        """LiteLLM fallback for Bedrock includes AWS credentials."""
        from app.ai_provider.resolver import ProviderResolver

        config = self._make_config(litellm_fallback=True)
        resolver = ProviderResolver(config)

        fallback = resolver._create_litellm_fallback(
            "aws_bedrock", "anthropic.claude-3-haiku-20240307-v1:0",
        )
        assert fallback is not None
        assert fallback.model == "bedrock/anthropic.claude-3-haiku-20240307-v1:0"
        assert fallback._credentials["aws_access_key_id"] == "AK"
        assert fallback._credentials["aws_secret_access_key"] == "SK"
        assert fallback._credentials["aws_region_name"] == "us-east-1"

    def test_fallback_passes_anthropic_credentials(self):
        from app.ai_provider.resolver import ProviderResolver

        config = self._make_config(litellm_fallback=True)
        resolver = ProviderResolver(config)

        fallback = resolver._create_litellm_fallback(
            "anthropic", "claude-sonnet-4-20250514",
        )
        assert fallback.model == "anthropic/claude-sonnet-4-20250514"
        assert fallback._credentials["api_key"] == "sk-test"

    def test_fallback_passes_openai_credentials(self):
        from app.ai_provider.resolver import ProviderResolver

        config = self._make_config(litellm_fallback=True)
        resolver = ProviderResolver(config)

        fallback = resolver._create_litellm_fallback(
            "openai", "gpt-4o",
        )
        assert fallback.model == "gpt-4o"
        assert fallback._credentials["api_key"] == "sk-oai-test"

    def test_no_fallback_for_non_litellm_model(self):
        """Models with litellm=False should not get LiteLLM fallback."""
        from app.ai_provider.resolver import ProviderResolver

        config = self._make_config(litellm_fallback=True)
        resolver = ProviderResolver(config)

        fallback = resolver._create_litellm_fallback(
            "aws_bedrock", "amazon.nova-pro-v1:0",
        )
        assert fallback is None

    def test_model_status_includes_litellm_flag(self):
        """ModelStatus should expose the litellm flag for UI rendering."""
        from app.ai_provider.resolver import ProviderResolver

        config = self._make_config(litellm_fallback=True)
        resolver = ProviderResolver(config)
        resolver._provider_enabled = {
            "anthropic": True, "aws_bedrock": True, "openai": True,
        }
        resolver._provider_health = {
            "anthropic": True, "aws_bedrock": True, "openai": True,
        }

        status = resolver.get_status()
        model_map = {m.id: m for m in status.models}

        assert model_map["test-model"].litellm is True
        assert model_map["nova-model"].litellm is False
        assert model_map["bedrock-model"].litellm is True

    def test_get_provider_for_model_returns_litellm_when_fallback_active(self):
        """When fallback is active for a provider type, get_provider_for_model
        returns a LiteLLMProvider with the requested model."""
        from app.ai_provider.resolver import ProviderResolver

        config = self._make_config(litellm_fallback=True)
        resolver = ProviderResolver(config)

        # Simulate fallback being active: put a LiteLLMProvider in the cache
        resolver._providers["anthropic"] = LiteLLMProvider(
            model="anthropic/claude-sonnet-4-20250514",
            api_key="sk-test",
        )
        resolver._provider_health["anthropic"] = True

        provider = resolver.get_provider_for_model("test-model")
        assert isinstance(provider, LiteLLMProvider)
        assert provider.model == "anthropic/claude-sonnet-4-20250514"


# =========================================================================
# API endpoint tests — /ai/status litellm fields + /ai/litellm-fallback
# =========================================================================

class TestLiteLLMAPIEndpoints:
    """Test the REST API endpoints for LiteLLM-related fields."""

    def test_status_includes_litellm_fallback_flag(self, api_client):
        """GET /ai/status response includes litellm_fallback field."""
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.config import get_config

        config = get_config()
        resolver = ProviderResolver(config)
        resolver._provider_enabled = {}
        resolver._provider_health = {}
        resolver._provider_configured = {}
        set_resolver(resolver)

        response = api_client.get("/ai/status")
        assert response.status_code == 200
        data = response.json()
        assert "litellm_fallback" in data
        assert isinstance(data["litellm_fallback"], bool)

    def test_status_models_include_litellm_field(self, api_client):
        """GET /ai/status model entries include litellm boolean field."""
        from app.ai_provider.resolver import ProviderResolver, set_resolver
        from app.config import (
            ConductorConfig, SummaryConfig, AIProvidersSecretsConfig,
            AIProviderSettingsConfig, AIModelConfig,
            AnthropicSecretsConfig, AWSBedrockSecretsConfig, OpenAISecretsConfig,
        )

        config = ConductorConfig(
            summary=SummaryConfig(enabled=True, default_model="m1"),
            ai_providers=AIProvidersSecretsConfig(
                anthropic=AnthropicSecretsConfig(api_key="sk-test"),
            ),
            ai_provider_settings=AIProviderSettingsConfig(anthropic_enabled=True),
            ai_models=[
                AIModelConfig(
                    id="m1", provider="anthropic",
                    model_name="claude-sonnet-4-20250514",
                    display_name="Test Model", enabled=True, litellm=True,
                ),
                AIModelConfig(
                    id="m2", provider="anthropic",
                    model_name="claude-haiku",
                    display_name="Haiku", enabled=True, litellm=False,
                ),
            ],
        )
        resolver = ProviderResolver(config)
        resolver._provider_enabled = {"anthropic": True}
        resolver._provider_health = {"anthropic": True}
        resolver._provider_configured = {"anthropic": True}
        set_resolver(resolver)

        # Patch get_config to return our test config
        with patch("app.config.get_config", return_value=config):
            response = api_client.get("/ai/status")
        assert response.status_code == 200
        models = {m["id"]: m for m in response.json()["models"]}
        assert models["m1"]["litellm"] is True
        assert models["m2"]["litellm"] is False

    def test_litellm_fallback_enable(self, api_client):
        """POST /ai/litellm-fallback enables the fallback."""
        response = api_client.post(
            "/ai/litellm-fallback",
            json={"enabled": True},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["litellm_fallback"] is True
        assert "enabled" in data["message"].lower()

    def test_litellm_fallback_disable(self, api_client):
        """POST /ai/litellm-fallback disables the fallback."""
        response = api_client.post(
            "/ai/litellm-fallback",
            json={"enabled": False},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["litellm_fallback"] is False
        assert "disabled" in data["message"].lower()
