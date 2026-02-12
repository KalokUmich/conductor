"""AI Provider module for LLM integrations.

This module provides a unified interface for AI providers with three
implementations: ClaudeDirectProvider, ClaudeBedrockProvider, and OpenAIProvider.

Usage:
    from app.ai_provider import (
        AIProvider, ClaudeDirectProvider, ClaudeBedrockProvider, OpenAIProvider,
        ChatMessage, DecisionSummary
    )

    # Using Claude Direct API (Anthropic)
    direct_provider = ClaudeDirectProvider(api_key="sk-ant-...")

    # Using Claude via AWS Bedrock
    bedrock_provider = ClaudeBedrockProvider(
        aws_access_key_id="...",
        aws_secret_access_key="..."
    )

    # Using OpenAI API
    openai_provider = OpenAIProvider(api_key="sk-...")

    # All implement the same interface
    if provider.health_check():
        messages = [ChatMessage(role="host", text="Hello", timestamp=1234567890)]
        summary = provider.summarize_structured(messages)
"""
from .base import AIProvider, ChatMessage, DecisionSummary
from .claude_bedrock import ClaudeBedrockProvider
from .claude_direct import ClaudeDirectProvider
from .openai_provider import OpenAIProvider
from .prompts import STRUCTURED_SUMMARY_PROMPT, format_conversation, get_summary_prompt
from .wrapper import (
    AIProviderError,
    JSONParseError,
    ProviderCallError,
    ProviderNotAvailableError,
    call_code_prompt,
    call_summary,
    call_summary_http,
    handle_provider_error,
)

__all__ = [
    "AIProvider",
    "ChatMessage",
    "DecisionSummary",
    "ClaudeDirectProvider",
    "ClaudeBedrockProvider",
    "OpenAIProvider",
    "STRUCTURED_SUMMARY_PROMPT",
    "format_conversation",
    "get_summary_prompt",
    # Wrapper functions and exceptions
    "call_summary",
    "call_summary_http",
    "call_code_prompt",
    "handle_provider_error",
    "AIProviderError",
    "ProviderNotAvailableError",
    "ProviderCallError",
    "JSONParseError",
]

