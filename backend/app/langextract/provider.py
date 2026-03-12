"""LangExtract provider for Bedrock models (multi-vendor) and Anthropic Direct API.

Registers with langextract's provider router so that model IDs like
``claude-sonnet-4-20250514``, ``anthropic/...``, ``amazon.nova-...``,
``meta.llama3-...``, ``mistral.*``, ``bedrock/...``, etc. route through
this provider automatically.

Usage::

    import langextract as lx
    # After importing this module (which registers the provider), you can:
    result = lx.extract(
        "Some document text...",
        prompt_description="Extract the key entities.",
        model_id="claude-sonnet-4-20250514",
    )

Or construct the model directly::

    from app.langextract.provider import BedrockLanguageModel
    model = BedrockLanguageModel(model_id="anthropic.claude-sonnet-4-20250514-v1:0")
    result = lx.extract("...", model=model, prompt_description="...")
"""
from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Any

import boto3

from langextract.core.base_model import BaseLanguageModel
from langextract.core import types as core_types
from langextract.providers import router

if TYPE_CHECKING:
    from app.langextract.catalog import BedrockCatalog

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bedrock (boto3) inference
# ---------------------------------------------------------------------------

_BEDROCK_MODEL_MAP = {
    "claude-sonnet-4-20250514": "anthropic.claude-sonnet-4-20250514-v1:0",
    "claude-opus-4-0-20250514": "anthropic.claude-opus-4-0-20250514-v1:0",
    "claude-haiku-4-5-20251001": "anthropic.claude-haiku-4-5-20251001-v1:0",
}


def _call_bedrock(model_id: str, prompt: str, region: str | None = None, **kwargs) -> str:
    """Call a model via AWS Bedrock Converse API.

    Works with any Bedrock model (Claude, Amazon Nova, Llama, Mistral, etc.)
    as the Converse API provides a unified interface.
    """
    effective_region = region or os.environ.get("AWS_DEFAULT_REGION", "us-east-1")
    client = boto3.client("bedrock-runtime", region_name=effective_region)

    inference_config: dict[str, Any] = {}
    if "temperature" in kwargs:
        inference_config["temperature"] = kwargs["temperature"]
    max_tokens = kwargs.get("max_output_tokens", kwargs.get("max_tokens", 4096))
    inference_config["maxTokens"] = max_tokens

    response = client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig=inference_config,
    )

    output_msg = response.get("output", {}).get("message", {})
    content_blocks = output_msg.get("content", [])
    return "".join(b.get("text", "") for b in content_blocks)


# ---------------------------------------------------------------------------
# Direct Anthropic API inference
# ---------------------------------------------------------------------------


def _call_anthropic_direct(model_id: str, prompt: str, **kwargs) -> str:
    """Call Claude via the Anthropic Messages API."""
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    client = anthropic.Anthropic(api_key=api_key)

    params: dict[str, Any] = {
        "model": model_id,
        "max_tokens": kwargs.get("max_output_tokens", kwargs.get("max_tokens", 4096)),
        "messages": [{"role": "user", "content": prompt}],
    }
    if "temperature" in kwargs:
        params["temperature"] = kwargs["temperature"]

    response = client.messages.create(**params)
    return "".join(b.text for b in response.content if hasattr(b, "text"))


# ---------------------------------------------------------------------------
# LangExtract provider
# ---------------------------------------------------------------------------


class BedrockLanguageModel(BaseLanguageModel):
    """LangExtract provider for Bedrock models (all vendors) and Anthropic Direct.

    Supports two backends:
    - **Bedrock**: model IDs starting with ``bedrock/``, mapped Claude names,
      or any model in the catalog (Amazon Nova, Llama, Mistral, DeepSeek, etc.)
    - **Direct**: Anthropic Claude models when ``use_bedrock=False``
      (uses ``ANTHROPIC_API_KEY``)
    """

    def __init__(
        self,
        model_id: str = "claude-sonnet-4-20250514",
        use_bedrock: bool | None = None,
        region: str | None = None,
        catalog: BedrockCatalog | None = None,
        **kwargs: Any,
    ):
        super().__init__(**kwargs)
        self.model_id = model_id
        self.region = region
        self._catalog = catalog

        # Auto-detect backend
        if use_bedrock is not None:
            self._use_bedrock = use_bedrock
        else:
            self._use_bedrock = (
                model_id.startswith("bedrock/")
                or model_id in _BEDROCK_MODEL_MAP
                or (catalog is not None and catalog.has_model(model_id))
                or bool(os.environ.get("AWS_ACCESS_KEY_ID"))
            )
        self.temperature: float | None = kwargs.get("temperature")

    def _resolve_model_id(self, model_id: str) -> str:
        """Resolve model ID through catalog (inference profiles) and static map."""
        # Strip bedrock/ prefix
        resolved = model_id.removeprefix("bedrock/")

        # Check static map for short Claude names
        if resolved in _BEDROCK_MODEL_MAP:
            resolved = _BEDROCK_MODEL_MAP[resolved]

        # Check catalog for inference profile resolution
        if self._catalog:
            resolved = self._catalog.get_effective_model_id(resolved)

        return resolved

    def infer(
        self, batch_prompts: Sequence[str], **kwargs: Any
    ) -> Iterator[Sequence[core_types.ScoredOutput]]:
        """Run inference on a batch of prompts."""
        merged = self.merge_kwargs(kwargs)
        call_kwargs: dict[str, Any] = {}

        temp = merged.get("temperature", self.temperature)
        if temp is not None:
            call_kwargs["temperature"] = temp
        if "max_output_tokens" in merged:
            call_kwargs["max_output_tokens"] = merged["max_output_tokens"]

        if self._use_bedrock:
            resolved = self._resolve_model_id(self.model_id)
            for prompt in batch_prompts:
                try:
                    text = _call_bedrock(resolved, prompt, region=self.region, **call_kwargs)
                    yield [core_types.ScoredOutput(score=1.0, output=text)]
                except Exception as exc:
                    logger.error("Bedrock inference failed (%s): %s", resolved, exc)
                    yield [core_types.ScoredOutput(score=0.0, output=f"Error: {exc}")]
        else:
            model = self.model_id.removeprefix("bedrock/")
            for prompt in batch_prompts:
                try:
                    text = _call_anthropic_direct(model, prompt, **call_kwargs)
                    yield [core_types.ScoredOutput(score=1.0, output=text)]
                except Exception as exc:
                    logger.error("Anthropic direct inference failed: %s", exc)
                    yield [core_types.ScoredOutput(score=0.0, output=f"Error: {exc}")]


# Backwards-compatible alias
ClaudeLanguageModel = BedrockLanguageModel


# ---------------------------------------------------------------------------
# Register with langextract's provider router
# ---------------------------------------------------------------------------

@router.register(
    # Claude / Anthropic
    r"^claude",
    r"^anthropic",
    r"^bedrock/anthropic",
    # Amazon
    r"^amazon\.",
    r"^bedrock/amazon",
    # Meta
    r"^meta\.",
    r"^bedrock/meta",
    # Mistral
    r"^mistral\.",
    r"^bedrock/mistral",
    # DeepSeek
    r"^deepseek",
    r"^bedrock/deepseek",
    # Qwen
    r"^qwen",
    r"^bedrock/qwen",
    # Google (Gemma)
    r"^google",
    r"^bedrock/google",
    # Catch-all for other bedrock/ prefixed models
    r"^bedrock/",
    priority=10,
)
class _RegisteredBedrockProvider(BedrockLanguageModel):
    """Auto-registered wrapper so ``lx.extract(model_id="...")`` works."""

    def __init__(self, model_id: str = "claude-sonnet-4-20250514", **kwargs: Any):
        super().__init__(model_id=model_id, **kwargs)
