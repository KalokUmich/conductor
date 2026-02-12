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
from typing import Dict, List, Optional

from app.config import (
    AIModelConfig,
    ConductorConfig,
)

from .base import AIProvider
from .claude_bedrock import ClaudeBedrockProvider
from .claude_direct import ClaudeDirectProvider
from .openai_provider import OpenAIProvider

logger = logging.getLogger(__name__)


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

    # Provider type names
    PROVIDER_TYPES = ["anthropic", "aws_bedrock", "openai"]

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

    def _is_provider_enabled(self, provider_type: str) -> bool:
        """Check if a provider is enabled in settings.

        Args:
            provider_type: Provider type (anthropic, aws_bedrock, openai).

        Returns:
            True if the provider is enabled in settings.
        """
        if provider_type == "anthropic":
            return self.provider_settings.anthropic_enabled
        elif provider_type == "aws_bedrock":
            return self.provider_settings.aws_bedrock_enabled
        elif provider_type == "openai":
            return self.provider_settings.openai_enabled
        return False

    def _is_provider_configured(self, provider_type: str) -> bool:
        """Check if a provider has API keys configured.

        Args:
            provider_type: Provider type (anthropic, aws_bedrock, openai).

        Returns:
            True if the provider has API keys configured.
        """
        if provider_type == "anthropic":
            return bool(self.providers_config.anthropic.api_key)
        elif provider_type == "aws_bedrock":
            return bool(self.providers_config.aws_bedrock.access_key_id and
                       self.providers_config.aws_bedrock.secret_access_key)
        elif provider_type == "openai":
            return bool(self.providers_config.openai.api_key)
        return False

    def _create_provider(self, provider_type: str, model_name: str) -> Optional[AIProvider]:
        """Create a provider instance for the given type and model.

        Args:
            provider_type: Provider type (anthropic, aws_bedrock, openai).
            model_name: Model name to use with the provider.

        Returns:
            AIProvider instance or None if creation fails.
        """
        try:
            if provider_type == "anthropic":
                return ClaudeDirectProvider(
                    api_key=self.providers_config.anthropic.api_key,
                    model=model_name,
                )
            elif provider_type == "aws_bedrock":
                cfg = self.providers_config.aws_bedrock
                return ClaudeBedrockProvider(
                    aws_access_key_id=cfg.access_key_id,
                    aws_secret_access_key=cfg.secret_access_key,
                    aws_session_token=cfg.session_token or None,
                    region_name=cfg.region,
                    model_id=model_name,
                )
            elif provider_type == "openai":
                cfg = self.providers_config.openai
                return OpenAIProvider(
                    api_key=cfg.api_key,
                    model=model_name,
                    organization=cfg.organization or None,
                )
            else:
                logger.warning(f"Unknown provider type: {provider_type}")
                return None
        except Exception as e:
            logger.error(f"Failed to create provider {provider_type}: {e}")
            return None

    def _check_provider_health(self, provider_type: str) -> bool:
        """Check health of a provider type using a default model.

        Args:
            provider_type: Provider type to check.

        Returns:
            True if the provider is healthy.
        """
        if not self._is_provider_configured(provider_type):
            return False

        # Find a model for this provider to test with
        test_model = None
        for model in self.models_config:
            if model.provider == provider_type and model.enabled:
                test_model = model
                break

        if not test_model:
            # No enabled model for this provider, use a default
            default_models = {
                "anthropic": "claude-sonnet-4-20250514",
                "aws_bedrock": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
                "openai": "gpt-4o",
            }
            model_name = default_models.get(provider_type, "")
        else:
            model_name = test_model.model_name

        provider = self._create_provider(provider_type, model_name)
        if not provider:
            return False

        try:
            healthy = provider.health_check()
            if healthy:
                # Cache the provider for reuse
                self._providers[provider_type] = provider
            return healthy
        except Exception as e:
            logger.error(f"Provider {provider_type} health check error: {e}")
            return False

    def resolve(self) -> Optional[AIProvider]:
        """Resolve providers and set the active model based on config.

        Checks all enabled and configured providers, performs health checks,
        and sets the default model as active if its provider is healthy.

        Returns:
            The active AIProvider or None if no healthy provider found.
        """
        if not self.summary_config.enabled:
            logger.info("Summary is disabled, skipping provider resolution")
            return None

        logger.info("Resolving AI providers...")

        # Check all provider types
        for provider_type in self.PROVIDER_TYPES:
            enabled = self._is_provider_enabled(provider_type)
            self._provider_enabled[provider_type] = enabled

            if not enabled:
                logger.info(f"⏭️ Provider {provider_type} skipped (disabled in settings)")
                self._provider_configured[provider_type] = False
                self._provider_health[provider_type] = False
                continue

            configured = self._is_provider_configured(provider_type)
            self._provider_configured[provider_type] = configured

            if configured:
                logger.info(f"🔍 Checking provider {provider_type}...")
                healthy = self._check_provider_health(provider_type)
                self._provider_health[provider_type] = healthy
                if healthy:
                    logger.info(f"✅ Provider {provider_type} is healthy")
                else:
                    logger.warning(f"⚠️ Provider {provider_type} health check failed")
            else:
                logger.info(f"⏭️ Provider {provider_type} skipped (not configured)")
                self._provider_health[provider_type] = False

        # Set active model based on default_model config
        default_model_id = self.summary_config.default_model
        default_model = self._find_model(default_model_id)

        if default_model and self._provider_health.get(default_model.provider, False):
            self.active_model_id = default_model_id
            self.active_provider_type = default_model.provider
            logger.info(f"✅ Active model set to: {default_model_id} ({default_model.provider})")
            return self._providers.get(default_model.provider)

        # Fallback: find first available model
        for model in self.models_config:
            if model.enabled and self._provider_health.get(model.provider, False):
                self.active_model_id = model.id
                self.active_provider_type = model.provider
                logger.info(f"✅ Fallback active model: {model.id} ({model.provider})")
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

        # Create provider with the specific model
        return self._create_provider(model.provider, model.model_name)

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

