"""Configuration loader for Conductor backend.

This module provides YAML-based configuration loading with Pydantic validation.
Configuration is split into two files:
    - conductor.secrets.yaml: Contains API keys and secrets (gitignored)
    - conductor.settings.yaml: Contains all other settings (can be committed)

Configuration file search order (for each file type):
    1. ./config/conductor.{secrets,settings}.yaml (current directory)
    2. ./conductor.{secrets,settings}.yaml (current directory)
    3. ../config/conductor.{secrets,settings}.yaml (parent directory)
    4. ~/.conductor/conductor.{secrets,settings}.yaml (user home)

Example:
    >>> from app.config import get_config
    >>> config = get_config()
    >>> print(config.server.port)  # 8000
"""
import logging
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel

logger = logging.getLogger(__name__)


# =============================================================================
# Configuration Models (Pydantic)
# =============================================================================


class ServerConfig(BaseModel):
    """Backend server configuration.

    Attributes:
        host: Server hostname (default: localhost).
        port: Server port (default: 8000).
        public_url: Public URL for external access (e.g., ngrok URL).
    """
    host: str = "localhost"
    port: int = 8000
    public_url: str = ""


class NgrokSettingsConfig(BaseModel):
    """Ngrok tunnel settings (non-secrets).

    Attributes:
        region: Ngrok region (us, eu, ap, au, sa, jp, in).
        enabled: Whether to enable ngrok tunnel on startup.
    """
    region: str = "us"
    enabled: bool = False


class NgrokSecretsConfig(BaseModel):
    """Ngrok secrets configuration.

    Attributes:
        authtoken: Ngrok authentication token (secret).
    """
    authtoken: str = ""


class AutoApplyConfig(BaseModel):
    """Auto-apply settings for automatic code change application.

    Attributes:
        enabled: Enable auto-apply by default.
        max_lines: Maximum lines for auto-apply (larger changes need review).
    """
    enabled: bool = False
    max_lines: int = 50


class ChangeLimitsConfig(BaseModel):
    """Limits for code change generation.

    Attributes:
        max_files_per_request: Maximum files per request.
        max_lines_per_file: Maximum lines changed per file.
        max_total_lines: Maximum total lines per request.
        auto_apply: Auto-apply configuration.
    """
    max_files_per_request: int = 10
    max_lines_per_file: int = 500
    max_total_lines: int = 2000
    auto_apply: AutoApplyConfig = AutoApplyConfig()


class SSOConfig(BaseModel):
    """AWS SSO (IAM Identity Center) configuration.

    Attributes:
        enabled: Whether SSO login is enabled.
        start_url: AWS SSO portal start URL (e.g. https://d-xxxxxxxxxx.awsapps.com/start).
        region: AWS region for SSO OIDC service.
    """
    enabled: bool = False
    start_url: str = ""
    region: str = "us-east-1"


class SessionConfig(BaseModel):
    """Chat session configuration.

    Attributes:
        timeout_minutes: Session timeout (0 = no timeout).
        max_participants: Maximum users per room.
    """
    timeout_minutes: int = 0
    max_participants: int = 10


class LoggingConfig(BaseModel):
    """Logging and audit configuration.

    Attributes:
        level: Log level (debug, info, warning, error).
        audit_enabled: Enable DuckDB audit logging.
        audit_path: Path to audit log database file.
    """
    level: str = "info"
    audit_enabled: bool = True
    audit_path: str = "audit_logs.duckdb"


# =============================================================================
# AI Provider Secrets Configuration (should be in gitignored secrets file)
# =============================================================================


class AnthropicSecretsConfig(BaseModel):
    """Anthropic API secrets configuration.

    Attributes:
        api_key: Anthropic API key (starts with sk-ant-...).
    """
    api_key: str = ""


class AWSBedrockSecretsConfig(BaseModel):
    """AWS Bedrock secrets configuration.

    Attributes:
        access_key_id: AWS access key ID.
        secret_access_key: AWS secret access key.
        session_token: Optional AWS session token for temporary credentials.
        region: AWS region for Bedrock service.
    """
    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: str = ""
    region: str = "us-east-1"


class OpenAISecretsConfig(BaseModel):
    """OpenAI API secrets configuration.

    Attributes:
        api_key: OpenAI API key (starts with sk-...).
        organization: Optional organization ID.
    """
    api_key: str = ""
    organization: str = ""


class AIProvidersSecretsConfig(BaseModel):
    """AI providers secrets configuration.

    This section contains API keys and should be in conductor.secrets.yaml.

    Attributes:
        anthropic: Anthropic API secrets.
        aws_bedrock: AWS Bedrock secrets.
        openai: OpenAI API secrets.
    """
    anthropic: AnthropicSecretsConfig = AnthropicSecretsConfig()
    aws_bedrock: AWSBedrockSecretsConfig = AWSBedrockSecretsConfig()
    openai: OpenAISecretsConfig = OpenAISecretsConfig()


# =============================================================================
# Secrets Config (conductor.secrets.yaml)
# =============================================================================


class SecretsConfig(BaseModel):
    """All secrets configuration (conductor.secrets.yaml).

    This file should be gitignored and contains all sensitive data.

    Attributes:
        ai_providers: AI provider API keys and credentials.
        ngrok: Ngrok authentication token.
    """
    ai_providers: AIProvidersSecretsConfig = AIProvidersSecretsConfig()
    ngrok: NgrokSecretsConfig = NgrokSecretsConfig()


# =============================================================================
# AI Settings Configuration (Non-secrets - can be committed)
# =============================================================================


class AIProviderSettingsConfig(BaseModel):
    """Settings for enabling/disabling AI providers.

    These are settings (not secrets) that control which providers are enabled.
    Even if enabled here, providers will only work if properly configured with secrets.

    Attributes:
        anthropic_enabled: Whether Anthropic provider is enabled.
        aws_bedrock_enabled: Whether AWS Bedrock provider is enabled.
        openai_enabled: Whether OpenAI provider is enabled.
    """
    anthropic_enabled: bool = True
    aws_bedrock_enabled: bool = True
    openai_enabled: bool = True


class AIModelConfig(BaseModel):
    """Configuration for a single AI model.

    Attributes:
        id: Unique identifier for the model (used in API).
        provider: Provider type (anthropic, aws_bedrock, openai).
        model_name: Actual model name/ID used by the provider.
        display_name: Human-readable name for UI display.
        enabled: Whether this model is enabled.
    """
    id: str
    provider: str  # anthropic, aws_bedrock, openai
    model_name: str
    display_name: str
    enabled: bool = True


# Default models configuration
# Note: For AWS Bedrock, use models that support single-region inference
# Cross-region inference profiles (us.anthropic.*) require Converse API
DEFAULT_AI_MODELS = [
    AIModelConfig(
        id="claude-sonnet-4-anthropic",
        provider="anthropic",
        model_name="claude-sonnet-4-20250514",
        display_name="Claude Sonnet 4 (Anthropic)",
    ),
    AIModelConfig(
        id="claude-3-haiku-bedrock",
        provider="aws_bedrock",
        model_name="anthropic.claude-3-haiku-20240307-v1:0",
        display_name="Claude 3 Haiku (Bedrock)",
    ),
    AIModelConfig(
        id="gpt-4o",
        provider="openai",
        model_name="gpt-4o",
        display_name="GPT-4o",
    ),
    AIModelConfig(
        id="gpt-4o-mini",
        provider="openai",
        model_name="gpt-4o-mini",
        display_name="GPT-4o Mini",
    ),
]


class SummaryConfig(BaseModel):
    """Summary/AI configuration.

    Attributes:
        enabled: Whether AI-powered summarization is enabled.
        default_model: Default model ID to use for summarization.
    """
    enabled: bool = False
    default_model: str = "claude-sonnet-4-anthropic"


# =============================================================================
# Settings Config (conductor.settings.yaml)
# =============================================================================


class SettingsConfig(BaseModel):
    """All settings configuration (conductor.settings.yaml).

    This file can be committed to git and contains all non-sensitive settings.

    Attributes:
        server: Backend server settings.
        ngrok: Ngrok settings (region, enabled - NOT authtoken).
        change_limits: Code change limits.
        session: Chat session settings.
        logging: Logging and audit settings.
        summary: Summary/AI settings.
        ai_provider_settings: AI provider enable flags.
        ai_models: AI model configurations.
    """
    server: ServerConfig = ServerConfig()
    ngrok: NgrokSettingsConfig = NgrokSettingsConfig()
    change_limits: ChangeLimitsConfig = ChangeLimitsConfig()
    session: SessionConfig = SessionConfig()
    logging: LoggingConfig = LoggingConfig()
    sso: SSOConfig = SSOConfig()
    summary: SummaryConfig = SummaryConfig()
    ai_provider_settings: AIProviderSettingsConfig = AIProviderSettingsConfig()
    ai_models: list[AIModelConfig] = DEFAULT_AI_MODELS.copy()


# =============================================================================
# Combined Config (merged from secrets + settings)
# =============================================================================


class ConductorConfig(BaseModel):
    """Main configuration container for all Conductor settings.

    This is the merged configuration from both secrets and settings files.

    Attributes:
        server: Backend server settings.
        ngrok_settings: Ngrok settings (region, enabled).
        ngrok_secrets: Ngrok secrets (authtoken).
        change_limits: Code change limits.
        session: Chat session settings.
        logging: Logging and audit settings.
        summary: Summary/AI settings.
        ai_provider_settings: AI provider enable flags.
        ai_providers: AI provider credentials (secrets).
        ai_models: AI model configurations.
    """
    server: ServerConfig = ServerConfig()
    ngrok_settings: NgrokSettingsConfig = NgrokSettingsConfig()
    ngrok_secrets: NgrokSecretsConfig = NgrokSecretsConfig()
    change_limits: ChangeLimitsConfig = ChangeLimitsConfig()
    session: SessionConfig = SessionConfig()
    logging: LoggingConfig = LoggingConfig()
    sso: SSOConfig = SSOConfig()
    summary: SummaryConfig = SummaryConfig()
    ai_provider_settings: AIProviderSettingsConfig = AIProviderSettingsConfig()
    ai_providers: AIProvidersSecretsConfig = AIProvidersSecretsConfig()
    ai_models: list[AIModelConfig] = DEFAULT_AI_MODELS.copy()


# =============================================================================
# Configuration Loading Functions
# =============================================================================


def _find_config_file(filename: str) -> Path | None:
    """Find a configuration file in standard locations.

    Args:
        filename: Name of the file to find (e.g., 'conductor.secrets.yaml').

    Returns:
        Path to the file if found, None otherwise.
    """
    # Check locations in order of priority
    locations = [
        Path.cwd() / "config" / filename,
        Path.cwd() / filename,
        Path.cwd().parent / "config" / filename,
        Path.home() / ".conductor" / filename,
    ]

    for path in locations:
        if path.exists():
            return path

    return None


def find_secrets_file() -> Path | None:
    """Find the secrets configuration file."""
    return _find_config_file("conductor.secrets.yaml")


def find_settings_file() -> Path | None:
    """Find the settings configuration file."""
    return _find_config_file("conductor.settings.yaml")


def _load_yaml_file(path: Path | None) -> dict:
    """Load a YAML file and return its contents as a dict.

    Args:
        path: Path to the YAML file, or None.

    Returns:
        Dictionary with file contents, or empty dict if file not found.
    """
    if path is None or not path.exists():
        return {}

    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Failed to load config file {path}: {e}")
        return {}


def load_config(
    secrets_path: Path | str | None = None,
    settings_path: Path | str | None = None,
) -> ConductorConfig:
    """Load configuration from secrets and settings YAML files.

    Args:
        secrets_path: Optional path to secrets file. If not provided,
                      searches standard locations.
        settings_path: Optional path to settings file. If not provided,
                       searches standard locations.

    Returns:
        ConductorConfig instance with merged settings.
    """
    # Find files if not provided
    if secrets_path is None:
        secrets_path = find_secrets_file()
    elif isinstance(secrets_path, str):
        secrets_path = Path(secrets_path)

    if settings_path is None:
        settings_path = find_settings_file()
    elif isinstance(settings_path, str):
        settings_path = Path(settings_path)

    # Load both files
    secrets_data = _load_yaml_file(secrets_path)
    settings_data = _load_yaml_file(settings_path)

    # Log what files were found
    if secrets_path and secrets_path.exists():
        logger.info(f"Loaded secrets from: {secrets_path}")
    else:
        logger.info("No secrets file found, using defaults")

    if settings_path and settings_path.exists():
        logger.info(f"Loaded settings from: {settings_path}")
    else:
        logger.info("No settings file found, using defaults")

    # Build the merged config
    # Settings file structure matches SettingsConfig
    # Secrets file structure matches SecretsConfig
    config_data = {}

    # Map settings YAML keys to ConductorConfig field names
    settings_key_map = {
        "server": "server",
        "ngrok": "ngrok_settings",
        "change_limits": "change_limits",
        "session": "session",
        "logging": "logging",
        "summary": "summary",
        "ai_provider_settings": "ai_provider_settings",
        "ai_models": "ai_models",
        "sso": "sso",
    }
    for yaml_key, config_key in settings_key_map.items():
        if yaml_key in settings_data:
            config_data[config_key] = settings_data[yaml_key]

    # Map secrets YAML keys to ConductorConfig field names
    secrets_key_map = {
        "ai_providers": "ai_providers",
        "ngrok": "ngrok_secrets",
    }
    for yaml_key, config_key in secrets_key_map.items():
        if yaml_key in secrets_data:
            config_data[config_key] = secrets_data[yaml_key]

    return ConductorConfig(**config_data)


def get_public_url(config: ConductorConfig) -> str:
    """Get the public URL for the backend.

    Returns ngrok URL if configured, otherwise localhost.
    """
    if config.server.public_url:
        return config.server.public_url

    return f"http://{config.server.host}:{config.server.port}"


# Global config instance (lazy loaded)
_config: ConductorConfig | None = None


def get_config() -> ConductorConfig:
    """Get the global configuration instance."""
    global _config
    if _config is None:
        _config = load_config()
    return _config

