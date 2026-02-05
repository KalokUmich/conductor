"""Configuration loader for Conductor backend.

This module provides YAML-based configuration loading with Pydantic validation.
It searches for configuration files in standard locations and provides
sensible defaults for all settings.

Configuration file search order:
    1. ./config/conductor.yaml (current directory)
    2. ./conductor.yaml (current directory)
    3. ../config/conductor.yaml (parent directory)
    4. ~/.conductor/conductor.yaml (user home)

Example:
    >>> from app.config import get_config
    >>> config = get_config()
    >>> print(config.server.port)  # 8000
"""
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel


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


class NgrokConfig(BaseModel):
    """Ngrok tunnel configuration for external access.

    Attributes:
        authtoken: Ngrok authentication token.
        region: Ngrok region (us, eu, ap, au, sa, jp, in).
        enabled: Whether to enable ngrok tunnel on startup.
    """
    authtoken: str = ""
    region: str = "us"
    enabled: bool = False


class LLMConfig(BaseModel):
    """LLM (Large Language Model) configuration for AI code generation.

    Note: Currently using MockAgent. This config is for future LLM integration.

    Attributes:
        provider: LLM provider (openai, anthropic, azure, local).
        model: Model name (e.g., gpt-4, claude-3).
        api_key: API key (can also use environment variable).
        temperature: Generation temperature (0.0-1.0).
        max_tokens: Maximum tokens per request.
    """
    provider: str = "openai"
    model: str = "gpt-4"
    api_key: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096


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


class ConductorConfig(BaseModel):
    """Main configuration container for all Conductor settings.

    Attributes:
        server: Backend server settings.
        ngrok: Ngrok tunnel settings.
        llm: LLM/AI settings.
        change_limits: Code change limits.
        session: Chat session settings.
        logging: Logging and audit settings.
    """
    server: ServerConfig = ServerConfig()
    ngrok: NgrokConfig = NgrokConfig()
    llm: LLMConfig = LLMConfig()
    change_limits: ChangeLimitsConfig = ChangeLimitsConfig()
    session: SessionConfig = SessionConfig()
    logging: LoggingConfig = LoggingConfig()


# =============================================================================
# Configuration Loading Functions
# =============================================================================


def find_config_file() -> Path | None:
    """Find the configuration file in standard locations."""
    # Check locations in order of priority
    locations = [
        Path.cwd() / "config" / "conductor.yaml",
        Path.cwd() / "conductor.yaml",
        Path.cwd().parent / "config" / "conductor.yaml",
        Path.home() / ".conductor" / "conductor.yaml",
    ]
    
    for path in locations:
        if path.exists():
            return path
    
    return None


def load_config(config_path: Path | str | None = None) -> ConductorConfig:
    """Load configuration from YAML file.
    
    Args:
        config_path: Optional path to config file. If not provided,
                     searches standard locations.
    
    Returns:
        ConductorConfig instance with loaded settings.
    """
    if config_path is None:
        config_path = find_config_file()
    
    if config_path is None:
        # Return default config if no file found
        return ConductorConfig()
    
    config_path = Path(config_path)
    
    if not config_path.exists():
        return ConductorConfig()
    
    with open(config_path) as f:
        data = yaml.safe_load(f) or {}
    
    return ConductorConfig(**data)


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

