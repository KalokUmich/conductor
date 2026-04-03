"""Provider resolver for AI provider selection.

This module provides a service that resolves which AI provider to use
based on configuration and health checks.

The new architecture supports:
- Multiple providers: Anthropic, AWS Bedrock, OpenAI
- Multiple models per provider
- Model selection for summarization

Usage:
    from app.ai_provider.resolver import ProviderResolver
    from app.config import get_config

    config = get_config()
    resolver = ProviderResolver(config)
    resolver.resolve()

    status = resolver.get_status()
"""
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional

from app.config import (
    AIModelConfig,
    AlibabaSecretsConfig,
    ConductorConfig,
    MoonshotSecretsConfig,
)

from .base import AIProvider
from .claude_bedrock import ClaudeBedrockProvider
from .claude_direct import ClaudeDirectProvider
from .openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)


class ProviderType(str, Enum):
    """Supported AI provider types."""
    ANTHROPIC = "anthropic"
    AWS_BEDROCK = "aws_bedrock"
    OPENAI = "openai"
    ALIBABA = "alibaba"
    MOONSHOT = "moonshot"


@dataclass
class ProviderStatus:
    """Status of a single provider."""
    name: str
    enabled: bool  # Enabled in settings
    configured: bool  # Has API key configured
    healthy: bool  # Health check passed


@dataclass
class ModelStatus:
    """Status of a single model."""
    id: str
    provider: str
    display_name: str
    available: bool  # Provider is healthy and model is enabled
    explorer: bool = False    # Can be used as a code-explorer sub-agent


@dataclass
class AIStatus:
    """Overall AI status response."""
    summary_enabled: bool
    active_provider: Optional[str]
    active_model: Optional[str]
    providers: List[ProviderStatus]
    models: List[ModelStatus]
    default_model: str


class ProviderResolver:
    """Resolves and manages AI providers based on configuration.

    This service reads API keys from config, creates providers for those
    with non-empty keys, performs health checks, and manages model selection.

    Provider types: anthropic, aws_bedrock, openai

    Attributes:
        config: Full conductor configuration.
        summary_config: Summary configuration with enabled flag.
        providers_config: AI providers configuration with API keys.
        models_config: List of AI model configurations.
        active_model_id: Currently selected model ID.
    """

    # All provider types to check during resolution
    PROVIDER_TYPES = list(ProviderType)

    def __init__(self, config: ConductorConfig) -> None:
        """Initialize the provider resolver.

        Args:
            config: Full conductor configuration.
        """
        self.config = config
        self.summary_config = config.summary
        self.providers_config = config.ai_providers
        self.provider_settings = config.ai_provider_settings
        self.models_config = config.ai_models

        # Provider instances keyed by type (anthropic, aws_bedrock, openai)
        self._providers: Dict[str, AIProvider] = {}
        # Provider health status keyed by type
        self._provider_health: Dict[str, bool] = {}
        # Whether provider is configured (has API key)
        self._provider_configured: Dict[str, bool] = {}
        # Whether provider is enabled in settings
        self._provider_enabled: Dict[str, bool] = {}

        # Active model and provider
        self.active_model_id: Optional[str] = None
        self.active_provider_type: Optional[str] = None

        # Model-level provider cache: model_id → AIProvider instance.
        # Avoids recreating clients on every request when per-room model
        # selection resolves the same model_id repeatedly.
        self._model_cache: Dict[str, AIProvider] = {}

    def _is_provider_enabled(self, provider_type: ProviderType) -> bool:
        """Check if a provider is enabled in settings."""
        enabled_map = {
            ProviderType.ANTHROPIC: self.provider_settings.anthropic_enabled,
            ProviderType.AWS_BEDROCK: self.provider_settings.aws_bedrock_enabled,
            ProviderType.OPENAI: self.provider_settings.openai_enabled,
            ProviderType.ALIBABA: self.provider_settings.alibaba_enabled,
            ProviderType.MOONSHOT: self.provider_settings.moonshot_enabled,
        }
        return enabled_map.get(provider_type, False)

    def _is_provider_configured(self, provider_type: ProviderType) -> bool:
        """Check if a provider has API keys configured."""
        if provider_type == ProviderType.ANTHROPIC:
            return bool(self.providers_config.anthropic.api_key)
        elif provider_type == ProviderType.AWS_BEDROCK:
            return bool(self.providers_config.aws_bedrock.access_key_id and
                       self.providers_config.aws_bedrock.secret_access_key)
        elif provider_type == ProviderType.OPENAI:
            return bool(self.providers_config.openai.api_key)
        elif provider_type == ProviderType.ALIBABA:
            return bool(self.providers_config.alibaba.api_key)
        elif provider_type == ProviderType.MOONSHOT:
            return bool(self.providers_config.moonshot.api_key)
        return False

    def _create_provider(
        self,
        provider_type: ProviderType,
        model_name: str,
        *,
        enable_thinking: Optional[bool] = None,
    ) -> Optional[AIProvider]:
        """Create a provider instance for the given type and model.

        Args:
            provider_type: Which provider backend to use.
            model_name: The model ID / name to pass to the provider.
            enable_thinking: Override for Alibaba ``enable_thinking``.
                - ``None`` (default): use the provider default (True for Alibaba).
                - ``True`` / ``False``: explicitly enable / disable thinking.
        """
        try:
            if provider_type == ProviderType.ANTHROPIC:
                return ClaudeDirectProvider(
                    api_key=self.providers_config.anthropic.api_key,
                    model=model_name,
                )
            elif provider_type == ProviderType.AWS_BEDROCK:
                cfg = self.providers_config.aws_bedrock
                return ClaudeBedrockProvider(
                    aws_access_key_id=cfg.access_key_id,
                    aws_secret_access_key=cfg.secret_access_key,
                    aws_session_token=cfg.session_token or None,
                    region_name=cfg.region,
                    model_id=model_name,
                )
            elif provider_type == ProviderType.OPENAI:
                cfg = self.providers_config.openai
                return OpenAIProvider(
                    api_key=cfg.api_key,
                    model=model_name,
                    organization=cfg.organization or None,
                )
            elif provider_type == ProviderType.ALIBABA:
                cfg = self.providers_config.alibaba
                # Default: thinking enabled for Alibaba (classifier / main / explorer).
                # Pass enable_thinking=False explicitly to disable if needed.
                thinking = enable_thinking if enable_thinking is not None else True
                extra_body = {"enable_thinking": thinking}
                return OpenAIProvider(
                    api_key=cfg.api_key,
                    model=model_name,
                    base_url=cfg.base_url,
                    extra_body=extra_body,
                )
            elif provider_type == ProviderType.MOONSHOT:
                cfg = self.providers_config.moonshot
                return OpenAIProvider(
                    api_key=cfg.api_key,
                    model=model_name,
                    base_url=cfg.base_url,
                )
            else:
                logger.warning(f"Unknown provider type: {provider_type}")
                return None
        except Exception as e:
            logger.error(f"Failed to create provider {provider_type}: {e}")
            return None

    def _check_provider_health(self, provider_type: ProviderType) -> bool:
        """Check health of a provider type using a default model."""
        if not self._is_provider_configured(provider_type):
            return False

        # Find a model for this provider to test with
        test_model = None
        for model in self.models_config:
            if model.provider == provider_type and model.enabled:
                test_model = model
                break

        if not test_model:
            default_models = {
                ProviderType.ANTHROPIC: "claude-sonnet-4-20250514",
                ProviderType.AWS_BEDROCK: "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                ProviderType.OPENAI: "gpt-4o",
                ProviderType.ALIBABA: "qwen-plus",
                ProviderType.MOONSHOT: "kimi-k2.5",
            }
            model_name = default_models.get(provider_type, "")
        else:
            model_name = test_model.model_name

        provider = self._create_provider(provider_type, model_name)
        if provider:
            try:
                if provider.health_check():
                    self._providers[provider_type] = provider
                    return True
            except Exception as e:
                logger.warning("Provider %s health check error: %s", provider_type, e)

        return False

    def resolve(self) -> Optional[AIProvider]:
        """Resolve providers and set the active model based on config.

        Checks all enabled and configured providers, performs health checks,
        and sets the default model as active if its provider is healthy.

        Returns:
            The active AIProvider or None if no healthy provider found.
        """
        logger.debug("Resolving AI providers...")

        # Check all provider types
        for provider_type in self.PROVIDER_TYPES:
            enabled = self._is_provider_enabled(provider_type)
            self._provider_enabled[provider_type] = enabled

            if not enabled:
                logger.debug("Provider %s skipped (disabled)", provider_type)
                self._provider_configured[provider_type] = False
                self._provider_health[provider_type] = False
                continue

            configured = self._is_provider_configured(provider_type)
            self._provider_configured[provider_type] = configured

            if configured:
                logger.debug("Checking provider %s...", provider_type)
                healthy = self._check_provider_health(provider_type)
                self._provider_health[provider_type] = healthy
                if healthy:
                    logger.debug("Provider %s is healthy", provider_type)
                else:
                    logger.warning("Provider %s health check failed", provider_type)
            else:
                logger.debug("Provider %s skipped (not configured)", provider_type)
                self._provider_health[provider_type] = False

        # Set active model based on default_model config
        default_model_id = self.summary_config.default_model
        default_model = self._find_model(default_model_id)

        if default_model and self._provider_health.get(default_model.provider, False):
            self.active_model_id = default_model_id
            self.active_provider_type = default_model.provider
            logger.info("Active model: %s (%s)", default_model_id, default_model.provider)
            return self._providers.get(default_model.provider)

        # Fallback: find first available model
        for model in self.models_config:
            if model.enabled and self._provider_health.get(model.provider, False):
                self.active_model_id = model.id
                self.active_provider_type = model.provider
                logger.info("Fallback active model: %s (%s)", model.id, model.provider)
                return self._providers.get(model.provider)

        logger.warning("No healthy AI provider found")
        return None

    def _find_model(self, model_id: str) -> Optional[AIModelConfig]:
        """Find a model configuration by ID.

        Args:
            model_id: Model ID to find.

        Returns:
            AIModelConfig or None if not found.
        """
        for model in self.models_config:
            if model.id == model_id:
                return model
        return None

    def get_provider_for_model(self, model_id: str) -> Optional[AIProvider]:
        """Get a provider instance for a specific model.

        Args:
            model_id: Model ID to get provider for.

        Returns:
            AIProvider instance or None if not available.
        """
        model = self._find_model(model_id)
        if not model:
            logger.warning(f"Model not found: {model_id}")
            return None

        if not model.enabled:
            logger.warning(f"Model is disabled: {model_id}")
            return None

        if not self._provider_health.get(model.provider, False):
            logger.warning(f"Provider not healthy for model {model_id}: {model.provider}")
            return None

        return self._create_provider(model.provider, model.model_name)

    def get_or_create_provider(self, model_id: str) -> Optional[AIProvider]:
        """Get a cached provider for a model, creating it on first access.

        Unlike ``get_provider_for_model`` (which creates a new instance every
        call), this method caches by *model_id* so the same httpx client is
        reused across requests — important when per-room model selection
        resolves the same model repeatedly.

        Returns None if the model is not found, disabled, or its provider is
        unhealthy.
        """
        cached = self._model_cache.get(model_id)
        if cached is not None:
            return cached

        model = self._find_model(model_id)
        if not model or not model.enabled:
            return None
        if not self._provider_health.get(model.provider, False):
            return None

        provider = self._create_provider(model.provider, model.model_name)
        if provider:
            self._model_cache[model_id] = provider
        return provider

    def get_status(self) -> AIStatus:
        """Get the current AI status.

        Returns:
            AIStatus with providers, models, and active selections.
        """
        providers = [
            ProviderStatus(
                name=provider_type,
                enabled=self._provider_enabled.get(provider_type, False),
                configured=self._provider_configured.get(provider_type, False),
                healthy=self._provider_health.get(provider_type, False),
            )
            for provider_type in self.PROVIDER_TYPES
        ]

        # Only show models for enabled providers
        models = [
            ModelStatus(
                id=model.id,
                provider=model.provider,
                display_name=model.display_name,
                available=(
                    model.enabled and
                    self._provider_enabled.get(model.provider, False) and
                    self._provider_health.get(model.provider, False)
                ),
                explorer=model.explorer,
            )
            for model in self.models_config
            if self._provider_enabled.get(model.provider, False)
        ]

        return AIStatus(
            summary_enabled=self.summary_config.enabled,
            active_provider=self.active_provider_type,
            active_model=self.active_model_id,
            providers=providers,
            models=models,
            default_model=self.summary_config.default_model,
        )

    def get_active_provider(self) -> Optional[AIProvider]:
        """Get the currently active provider.

        Returns:
            The active AIProvider or None.
        """
        if self.active_provider_type:
            return self._providers.get(self.active_provider_type)
        return None

    def get_explorer_provider(self) -> Optional[AIProvider]:
        """Get a provider for code-explorer sub-agents.

        Returns the first enabled model with ``explorer: true`` whose
        provider is healthy.  For Alibaba models, thinking is explicitly
        **enabled** so the model can reason deeply about code structure
        before emitting tool calls — this significantly improves the
        quality of provability judgements in code review findings.

        Returns:
            An AIProvider suitable for sub-agent / explorer work, or None.
        """
        for model in self.models_config:
            if (
                model.explorer
                and model.enabled
                and self._provider_health.get(model.provider, False)
            ):
                # Explorer sub-agents: enable thinking for Alibaba models
                # so the model reasons about code structure before acting.
                # This improves finding quality (e.g. provability of defects).
                provider = self._create_provider(
                    model.provider,
                    model.model_name,
                    enable_thinking=True,
                )
                if provider:
                    logger.info("Explorer provider: %s (%s)", model.id, model.provider)
                    return provider
        return None

    def get_explorer_provider_for_model(self, model_id: str) -> Optional[AIProvider]:
        """Get an explorer provider for a specific model ID.

        Returns None if the model is not found, not enabled, not marked as
        explorer, or its provider is not healthy.
        """
        model = self._find_model(model_id)
        if not model:
            return None
        if not model.explorer or not model.enabled:
            return None
        if not self._provider_health.get(model.provider, False):
            return None
        provider = self._create_provider(
            model.provider,
            model.model_name,
            enable_thinking=False,
        )
        if provider:
            logger.info("Explorer provider set to: %s (%s)", model.id, model.provider)
        return provider

    def set_active_model(self, model_id: str) -> bool:
        """Set the active model for summarization.

        Args:
            model_id: Model ID to set as active.

        Returns:
            True if successful, False if model not available.
        """
        model = self._find_model(model_id)
        if not model:
            logger.warning(f"Cannot set active model, not found: {model_id}")
            return False

        if not model.enabled:
            logger.warning(f"Cannot set active model, disabled: {model_id}")
            return False

        if not self._provider_health.get(model.provider, False):
            logger.warning(f"Cannot set active model, provider not healthy: {model.provider}")
            return False

        self.active_model_id = model_id
        self.active_provider_type = model.provider

        # Ensure provider is created with correct model
        provider = self._create_provider(model.provider, model.model_name)
        if provider:
            self._providers[model.provider] = provider
            self._model_cache[model_id] = provider

        logger.info(f"Active model changed to: {model_id}")
        return True


# Global resolver instance (initialized on startup)
_resolver: Optional[ProviderResolver] = None


def get_resolver() -> Optional[ProviderResolver]:
    """Get the global provider resolver instance."""
    return _resolver


def set_resolver(resolver: ProviderResolver) -> None:
    """Set the global provider resolver instance."""
    global _resolver
    _resolver = resolver

