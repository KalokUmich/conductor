"""Backwards-compatible alias module.

All functionality has moved to :mod:`app.langextract.provider`.
This module re-exports the public names so existing imports continue to work.
"""

from app.langextract.provider import (  # noqa: F401
    _BEDROCK_MODEL_MAP,
    BedrockLanguageModel,
    ClaudeLanguageModel,
    _call_anthropic_direct,
    _call_bedrock,
)
