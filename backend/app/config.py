"""Conductor application configuration.

Loads settings from two YAML files:
  * conductor.settings.yaml  — non-secret configuration
  * conductor.secrets.yaml   — secrets (never committed)

Environment variable overrides:
  Any secret can be overridden by an environment variable.  This allows
  Docker images to ship with dev defaults in conductor.secrets.yaml while
  ECS/K8s task definitions inject production values via env vars.

  Naming convention: ``CONDUCTOR_<SECTION>_<KEY>`` (uppercase, underscores).
  Examples:
    CONDUCTOR_AWS_ACCESS_KEY_ID      → ai_providers.aws_bedrock.access_key_id
    CONDUCTOR_AWS_SECRET_ACCESS_KEY  → ai_providers.aws_bedrock.secret_access_key
    CONDUCTOR_AWS_SESSION_TOKEN      → ai_providers.aws_bedrock.session_token
    CONDUCTOR_AWS_REGION             → ai_providers.aws_bedrock.region
    CONDUCTOR_ANTHROPIC_API_KEY      → ai_providers.anthropic.api_key
    CONDUCTOR_OPENAI_API_KEY         → ai_providers.openai.api_key
    CONDUCTOR_ALIBABA_API_KEY        → ai_providers.alibaba.api_key
    CONDUCTOR_ALIBABA_BASE_URL       → ai_providers.alibaba.base_url
    CONDUCTOR_MOONSHOT_API_KEY       → ai_providers.moonshot.api_key
    CONDUCTOR_POSTGRES_USER          → postgres.user
    CONDUCTOR_POSTGRES_PASSWORD      → postgres.password
    CONDUCTOR_JIRA_CLIENT_ID         → jira.client_id
    CONDUCTOR_JIRA_CLIENT_SECRET     → jira.client_secret
    CONDUCTOR_GOOGLE_CLIENT_ID       → google_sso.client_id
    CONDUCTOR_GOOGLE_CLIENT_SECRET   → google_sso.client_secret
    CONDUCTOR_NGROK_AUTHTOKEN        → ngrok.authtoken
    LANGFUSE_PUBLIC_KEY              → langfuse.public_key  (Langfuse SDK convention)
    LANGFUSE_SECRET_KEY              → langfuse.secret_key
    LANGFUSE_HOST                    → langfuse.host
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field


def _env(name: str, default: str = "") -> str:
    """Return env var *name* if set and non-empty, else *default*."""
    val = os.environ.get(name, "")
    return val if val else default


# Mapping: env var name → path into the secrets_data dict.
_ENV_SECRETS_MAP = {
    "CONDUCTOR_POSTGRES_USER": ("postgres", "user"),
    "CONDUCTOR_POSTGRES_PASSWORD": ("postgres", "password"),
    "CONDUCTOR_ANTHROPIC_API_KEY": ("ai_providers", "anthropic", "api_key"),
    "CONDUCTOR_AWS_ACCESS_KEY_ID": ("ai_providers", "aws_bedrock", "access_key_id"),
    "CONDUCTOR_AWS_SECRET_ACCESS_KEY": ("ai_providers", "aws_bedrock", "secret_access_key"),
    "CONDUCTOR_AWS_SESSION_TOKEN": ("ai_providers", "aws_bedrock", "session_token"),
    "CONDUCTOR_AWS_REGION": ("ai_providers", "aws_bedrock", "region"),
    "CONDUCTOR_OPENAI_API_KEY": ("ai_providers", "openai", "api_key"),
    "CONDUCTOR_ALIBABA_API_KEY": ("ai_providers", "alibaba", "api_key"),
    "CONDUCTOR_ALIBABA_BASE_URL": ("ai_providers", "alibaba", "base_url"),
    "CONDUCTOR_MOONSHOT_API_KEY": ("ai_providers", "moonshot", "api_key"),
    "CONDUCTOR_MOONSHOT_BASE_URL": ("ai_providers", "moonshot", "base_url"),
    "CONDUCTOR_JIRA_CLIENT_ID": ("jira", "client_id"),
    "CONDUCTOR_JIRA_CLIENT_SECRET": ("jira", "client_secret"),
    "CONDUCTOR_GOOGLE_CLIENT_ID": ("google_sso", "client_id"),
    "CONDUCTOR_GOOGLE_CLIENT_SECRET": ("google_sso", "client_secret"),
    "CONDUCTOR_NGROK_AUTHTOKEN": ("ngrok", "authtoken"),
    "LANGFUSE_PUBLIC_KEY": ("langfuse", "public_key"),
    "LANGFUSE_SECRET_KEY": ("langfuse", "secret_key"),
}


def _apply_env_overrides(secrets_data: dict) -> None:
    """Override secrets_data values with environment variables when set.

    Mutates *secrets_data* in place. Only overwrites if the env var exists
    and is non-empty, so Docker-baked YAML defaults still work when no
    env var is set.
    """
    for env_name, path in _ENV_SECRETS_MAP.items():
        val = os.environ.get(env_name, "")
        if not val:
            continue
        # Navigate to the parent dict, creating intermediaries if needed
        d = secrets_data
        for key in path[:-1]:
            if key not in d or not isinstance(d[key], dict):
                d[key] = {}
            d = d[key]
        d[path[-1]] = val


logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers — config file discovery
# ---------------------------------------------------------------------------


def _find_config_file(filename: str) -> Optional[Path]:
    """Find a configuration file in standard locations.

    Search order (first match wins):
      1. ./config/{filename}
      2. ./{filename}
      3. ../config/{filename}
      4. ~/.conductor/{filename}
    """
    locations = [
        Path.cwd() / "config" / filename,
        Path.cwd() / filename,
        Path.cwd().parent / "config" / filename,
        Path.home() / ".conductor" / filename,
    ]
    for path in locations:
        if path.exists():
            logger.debug("Found config file: %s", path)
            return path
    return None


def _load_yaml(path: Optional[Path]) -> Dict[str, Any]:
    if path is None or not path.exists():
        logger.warning("Config file not found: %s", path)
        return {}
    with path.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Secrets models
# ---------------------------------------------------------------------------


class PostgresSecrets(BaseModel):
    """Postgres credentials (from conductor.secrets.yaml)."""

    user: str = "conductor"
    password: str = "conductor"


class RedisSecrets(BaseModel):
    """Redis credentials (from conductor.secrets.yaml)."""

    password: str = ""


class DatabaseSecrets(BaseModel):
    url: Optional[str] = None


class JWTSecrets(BaseModel):
    secret_key: str = "change-me-in-production"
    algorithm: str = "HS256"


class LangfuseSecrets(BaseModel):
    """Langfuse API keys (from conductor.secrets.yaml)."""

    public_key: str = ""
    secret_key: str = ""


class Secrets(BaseModel):
    database: DatabaseSecrets = Field(default_factory=DatabaseSecrets)
    postgres: PostgresSecrets = Field(default_factory=PostgresSecrets)
    redis: RedisSecrets = Field(default_factory=RedisSecrets)
    jwt: JWTSecrets = Field(default_factory=JWTSecrets)
    langfuse: LangfuseSecrets = Field(default_factory=LangfuseSecrets)


# ---------------------------------------------------------------------------
# Settings models
# ---------------------------------------------------------------------------


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    reload: bool = False
    log_level: str = "info"
    allowed_origins: List[str] = Field(default_factory=lambda: ["*"])
    # Public URL for external access (ngrok, cloudflare tunnel, etc.).
    # When set, this is returned by GET /public-url and used by the extension
    # to build invite links instead of http://localhost:8000.
    public_url: str = ""


class DatabaseSettings(BaseModel):
    pool_size: int = 10
    max_overflow: int = 20
    pool_timeout: int = 30
    echo_sql: bool = False


class PostgresSettings(BaseModel):
    """PostgreSQL connection settings (async via asyncpg).

    Credentials come from ``secrets.postgres`` in conductor.secrets.yaml.
    The full URL is built at runtime by ``build_postgres_url()``.
    Env var ``DATABASE_URL`` always takes priority over config.
    """

    host: str = "localhost"
    port: int = 5432
    database: str = "conductor"
    pool_size: int = 10
    max_overflow: int = 20


class RedisSettings(BaseModel):
    """Redis connection settings.

    Password comes from ``secrets.redis`` in conductor.secrets.yaml.
    The full URL is built at runtime by ``build_redis_url()``.
    Env var ``REDIS_URL`` always takes priority over config.
    """

    host: str = "localhost"
    port: int = 6379
    db: int = 0
    prefix: str = "conductor:"


class AuthSettings(BaseModel):
    token_expire_minutes: int = 60
    refresh_expire_days: int = 7
    require_email_verify: bool = False


class RoomSettings(BaseModel):
    max_participants: int = 50
    max_rooms_per_user: int = 10
    session_timeout_minutes: int = 120
    enable_persistence: bool = True


class GitWorkspaceSettings(BaseModel):
    """Configuration for the Git Workspace module."""

    enabled: bool = True
    workspaces_dir: str = "./workspaces"
    git_auth_mode: Literal["token", "delegate"] = "token"
    credential_ttl_seconds: int = 3600
    max_worktrees_per_repo: int = 20
    cleanup_on_room_close: bool = True


class TraceSettings(BaseModel):
    """Configuration for agent loop session tracing.

    Traces record per-iteration metrics (tokens, latencies, tool calls)
    for offline analysis and prompt optimization.

    Storage backends:
      * ``local``    — JSON files in ``local_path`` (default, gitignored)
      * ``database`` — SQLite / PostgreSQL via ``database_url``
    """

    enabled: bool = True
    backend: Literal["local", "database"] = "local"
    local_path: str = ".conductor/session_traces"
    database_url: str = ""  # e.g. "sqlite:///traces.db" or "postgresql://..."


class LangfuseSettings(BaseModel):
    """Configuration for Langfuse observability integration.

    Self-hosted via docker/docker-compose.langfuse.yaml.
    When disabled, @observe decorators are no-ops (zero overhead).
    """

    enabled: bool = False
    host: str = "http://localhost:3001"


class CodeSearchSettings(BaseModel):
    """Configuration for code search and repo graph features."""

    # -- RepoMap (graph-based context) --
    repo_map_enabled: bool = True
    repo_map_top_n: int = 10  # Top N files by PageRank to include in map


class AppSettings(BaseModel):
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    redis: RedisSettings = Field(default_factory=RedisSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    rooms: RoomSettings = Field(default_factory=RoomSettings)
    git_workspace: GitWorkspaceSettings = Field(default_factory=GitWorkspaceSettings)
    code_search: CodeSearchSettings = Field(default_factory=CodeSearchSettings)
    trace: TraceSettings = Field(default_factory=TraceSettings)
    langfuse: LangfuseSettings = Field(default_factory=LangfuseSettings)
    secrets: Secrets = Field(default_factory=Secrets)

    def build_postgres_url(self) -> str:
        """Build the full async Postgres URL from settings + secrets."""
        pg = self.postgres
        sec = self.secrets.postgres
        return f"postgresql+asyncpg://{sec.user}:{sec.password}@{pg.host}:{pg.port}/{pg.database}"

    def build_redis_url(self) -> str:
        """Build the full Redis URL from settings + secrets."""
        r = self.redis
        sec = self.secrets.redis
        if sec.password:
            return f"redis://:{sec.password}@{r.host}:{r.port}/{r.db}"
        return f"redis://{r.host}:{r.port}/{r.db}"


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_settings() -> AppSettings:
    """Load and merge settings + secrets into a single *AppSettings* object.

    Searches for config files in standard locations
    (see :func:`_find_config_file` for the search order).
    """
    settings_path = _find_config_file("conductor.settings.yaml")
    secrets_path = _find_config_file("conductor.secrets.yaml")

    settings_data = _load_yaml(settings_path)
    secrets_data = _load_yaml(secrets_path)

    # Apply environment variable overrides to secrets
    _apply_env_overrides(secrets_data)

    # Merge: secrets live under the "secrets" key in AppSettings
    settings_data["secrets"] = secrets_data

    app_settings = AppSettings(**settings_data)
    logger.info(
        "Settings loaded (server=%s:%s, git_workspace.enabled=%s, repo_map.enabled=%s)",
        app_settings.server.host,
        app_settings.server.port,
        app_settings.git_workspace.enabled,
        app_settings.code_search.repo_map_enabled,
    )
    return app_settings


# ---------------------------------------------------------------------------
# AI Provider config models
# ---------------------------------------------------------------------------


class SummaryConfig(BaseModel):
    """Configuration for AI summarization feature."""

    enabled: bool = False
    default_model: str = "claude-3-haiku-bedrock"


class AnthropicSecretsConfig(BaseModel):
    """Anthropic API credentials."""

    api_key: str = ""


class AWSBedrockSecretsConfig(BaseModel):
    """AWS Bedrock credentials."""

    access_key_id: str = ""
    secret_access_key: str = ""
    session_token: Optional[str] = None
    region: str = "us-east-1"


class OpenAISecretsConfig(BaseModel):
    """OpenAI API credentials."""

    api_key: str = ""
    organization: Optional[str] = None


class AlibabaSecretsConfig(BaseModel):
    """Alibaba Cloud DashScope API credentials.

    Uses an OpenAI-compatible endpoint at DashScope.
    Get your API key from: https://dashscope.console.aliyun.com/
    """

    api_key: str = ""
    base_url: str = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


class MoonshotSecretsConfig(BaseModel):
    """Moonshot AI (Kimi) API credentials.

    Uses an OpenAI-compatible endpoint.
    Get your API key from: https://platform.moonshot.ai/
    """

    api_key: str = ""
    base_url: str = "https://api.moonshot.ai/v1"


class AIProvidersSecretsConfig(BaseModel):
    """Credentials for all AI providers."""

    anthropic: AnthropicSecretsConfig = Field(default_factory=AnthropicSecretsConfig)
    aws_bedrock: AWSBedrockSecretsConfig = Field(default_factory=AWSBedrockSecretsConfig)
    openai: OpenAISecretsConfig = Field(default_factory=OpenAISecretsConfig)
    alibaba: AlibabaSecretsConfig = Field(default_factory=AlibabaSecretsConfig)
    moonshot: MoonshotSecretsConfig = Field(default_factory=MoonshotSecretsConfig)


class AIProviderSettingsConfig(BaseModel):
    """Enable/disable flags for each AI provider."""

    anthropic_enabled: bool = False
    aws_bedrock_enabled: bool = False
    openai_enabled: bool = False
    alibaba_enabled: bool = False
    moonshot_enabled: bool = False


class AIModelConfig(BaseModel):
    """Configuration for a single AI model.

    Flags:
      - ``explorer``: If True, this model can be used as a code explorer
        (sub-agent) for iterative tool-calling loops.  Explorer models
        have thinking/reasoning disabled to maximise content output and
        reduce token waste.  (Typically a fast/cheap model like Flash or Haiku.)
    """

    id: str = ""
    provider: str = ""
    model_name: str = ""
    display_name: str = ""
    enabled: bool = True
    explorer: bool = False


# ---------------------------------------------------------------------------
# SSO config models
# ---------------------------------------------------------------------------


class SSOConfig(BaseModel):
    """AWS SSO configuration."""

    enabled: bool = False
    start_url: str = ""
    region: str = "us-east-1"


class GoogleSSOConfig(BaseModel):
    """Google OAuth SSO configuration."""

    enabled: bool = False


class GoogleSSOSecretsConfig(BaseModel):
    """Google OAuth credentials."""

    client_id: str = ""
    client_secret: str = ""


class JiraTeamEntry(BaseModel):
    """A statically configured Atlassian team (UUID + display name)."""

    id: str
    name: str


class JiraBranchFormats(BaseModel):
    """Branch naming templates for ticket-linked branches."""

    feature: str = "feature/{ticket}-{content}"
    bugfix: str = "bugfix/{ticket}-{content}"


class JiraSettings(BaseModel):
    """Jira integration configuration."""

    enabled: bool = False
    branch_formats: JiraBranchFormats = JiraBranchFormats()
    allowed_projects: List[str] = []  # filter by project key; empty = show all
    teams: List[JiraTeamEntry] = []


class JiraSecretsConfig(BaseModel):
    """Jira OAuth credentials (from conductor.secrets.yaml)."""

    client_id: str = ""
    client_secret: str = ""


# ---------------------------------------------------------------------------
# Logging config model
# ---------------------------------------------------------------------------


class LoggingConfig(BaseModel):
    """Logging and audit configuration."""

    audit_enabled: bool = False
    audit_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Prompt / change-limits config models
# ---------------------------------------------------------------------------


class PromptConfig(BaseModel):
    """Prompt rendering options."""

    output_mode: str = "unified_diff"


class AutoApplyLimitsConfig(BaseModel):
    """Limits applied specifically to auto-apply mode."""

    max_lines: int = 50


class ChangeLimitsConfig(BaseModel):
    """Limits on the size of AI-generated changesets."""

    max_files_per_request: int = 2
    max_total_lines: int = 50
    auto_apply: AutoApplyLimitsConfig = Field(default_factory=AutoApplyLimitsConfig)


# ---------------------------------------------------------------------------
# Top-level ConductorConfig (used by AI provider, audit, auth, policy modules)
# ---------------------------------------------------------------------------


class SessionConfig(BaseModel):
    """Session / room participation limits."""

    max_participants: int = 50


class ConductorConfig(BaseModel):
    """Unified top-level configuration used by newer modules."""

    summary: SummaryConfig = Field(default_factory=SummaryConfig)
    ai_providers: AIProvidersSecretsConfig = Field(default_factory=AIProvidersSecretsConfig)
    ai_provider_settings: AIProviderSettingsConfig = Field(default_factory=AIProviderSettingsConfig)
    ai_models: List[AIModelConfig] = Field(default_factory=list)
    sso: SSOConfig = Field(default_factory=SSOConfig)
    google_sso: GoogleSSOConfig = Field(default_factory=GoogleSSOConfig)
    google_sso_secrets: GoogleSSOSecretsConfig = Field(default_factory=GoogleSSOSecretsConfig)
    jira: JiraSettings = Field(default_factory=JiraSettings)
    jira_secrets: JiraSecretsConfig = Field(default_factory=JiraSecretsConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    prompt: PromptConfig = Field(default_factory=PromptConfig)
    change_limits: ChangeLimitsConfig = Field(default_factory=ChangeLimitsConfig)
    session: SessionConfig = Field(default_factory=SessionConfig)


# ---------------------------------------------------------------------------
# ConductorConfig loader with audit_path resolution
# ---------------------------------------------------------------------------


def _resolve_audit_path(raw_path: Optional[str], settings_path: Optional[Path]) -> Optional[str]:
    """Resolve a (possibly relative) audit_path to an absolute path.

    Resolution rules:
      1. Absolute paths are returned unchanged.
      2. If settings file lives inside a ``config/`` directory
         (e.g. ``<root>/config/conductor.settings.yaml``), relative paths
         are resolved from ``<root>`` (the parent of ``config/``).
      3. Otherwise relative paths are resolved from the directory that
         contains the settings file.
    """
    if raw_path is None:
        return None
    p = Path(raw_path)
    if p.is_absolute():
        return str(p)
    if settings_path is None:
        return str(p)
    settings_dir = settings_path.parent
    if settings_dir.name == "config":
        base = settings_dir.parent
    else:
        base = settings_dir
    return str(base / p)


def load_config(
    settings_path: Optional[Path] = None,
    secrets_path: Optional[Path] = None,
) -> ConductorConfig:
    """Load a :class:`ConductorConfig` from YAML files.

    Args:
        settings_path: Explicit path to ``conductor.settings.yaml``.
            Falls back to automatic discovery if *None*.
        secrets_path: Explicit path to ``conductor.secrets.yaml``.
            Falls back to automatic discovery if *None*.

    Returns:
        A fully populated :class:`ConductorConfig`.
    """
    if settings_path is None:
        settings_path = _find_config_file("conductor.settings.yaml")
    if secrets_path is None:
        secrets_path = _find_config_file("conductor.secrets.yaml")

    raw = _load_yaml(settings_path)

    # Build nested sub-configs from raw YAML sections
    logging_data = raw.get("logging", {})
    raw_audit_path = logging_data.get("audit_path")
    resolved_audit_path = _resolve_audit_path(raw_audit_path, settings_path)
    logging_cfg = LoggingConfig(
        audit_enabled=logging_data.get("audit_enabled", False),
        audit_path=resolved_audit_path,
    )

    prompt_data = raw.get("prompt", {})
    prompt_cfg = PromptConfig(output_mode=prompt_data.get("output_mode", "unified_diff"))

    cl_data = raw.get("change_limits", {})
    aa_data = cl_data.get("auto_apply", {})
    change_limits_cfg = ChangeLimitsConfig(
        max_files_per_request=cl_data.get("max_files_per_request", 2),
        max_total_lines=cl_data.get("max_total_lines", 50),
        auto_apply=AutoApplyLimitsConfig(max_lines=aa_data.get("max_lines", 50)),
    )

    sso_data = raw.get("sso", {})
    sso_cfg = SSOConfig(
        enabled=sso_data.get("enabled", False),
        start_url=sso_data.get("start_url", ""),
        region=sso_data.get("region", "us-east-1"),
    )

    gsso_data = raw.get("google_sso", {})
    google_sso_cfg = GoogleSSOConfig(enabled=gsso_data.get("enabled", False))

    # Secrets — env vars override YAML values (for cloud deployment)
    secrets_raw = _load_yaml(secrets_path)
    gsso_sec = secrets_raw.get("google_sso", {})
    google_sso_secrets_cfg = GoogleSSOSecretsConfig(
        client_id=_env("CONDUCTOR_GOOGLE_CLIENT_ID", gsso_sec.get("client_id", "")),
        client_secret=_env("CONDUCTOR_GOOGLE_CLIENT_SECRET", gsso_sec.get("client_secret", "")),
    )

    jira_data = raw.get("jira", {})
    jira_teams_raw = jira_data.get("teams", [])
    jira_teams = [JiraTeamEntry(id=t["id"], name=t["name"]) for t in jira_teams_raw if t.get("id")]
    jira_cfg = JiraSettings(enabled=jira_data.get("enabled", False), teams=jira_teams)

    jira_sec = secrets_raw.get("jira", {})
    jira_secrets_cfg = JiraSecretsConfig(
        client_id=_env("CONDUCTOR_JIRA_CLIENT_ID", jira_sec.get("client_id", "")),
        client_secret=_env("CONDUCTOR_JIRA_CLIENT_SECRET", jira_sec.get("client_secret", "")),
    )

    summary_data = raw.get("summary", {})
    summary_cfg = SummaryConfig(
        enabled=summary_data.get("enabled", False),
        default_model=summary_data.get("default_model", "claude-3-haiku-bedrock"),
    )

    aps_data = raw.get("ai_provider_settings", {})
    ai_provider_settings_cfg = AIProviderSettingsConfig(
        anthropic_enabled=aps_data.get("anthropic_enabled", False),
        aws_bedrock_enabled=aps_data.get("aws_bedrock_enabled", False),
        openai_enabled=aps_data.get("openai_enabled", False),
        alibaba_enabled=aps_data.get("alibaba_enabled", False),
        moonshot_enabled=aps_data.get("moonshot_enabled", False),
    )

    ap_sec = secrets_raw.get("ai_providers", {})
    anth_sec = ap_sec.get("anthropic", {})
    bdr_sec = ap_sec.get("aws_bedrock", {})
    oai_sec = ap_sec.get("openai", {})
    ali_sec = ap_sec.get("alibaba", {})
    moon_sec = ap_sec.get("moonshot", {})
    ai_providers_cfg = AIProvidersSecretsConfig(
        anthropic=AnthropicSecretsConfig(
            api_key=_env("CONDUCTOR_ANTHROPIC_API_KEY", anth_sec.get("api_key", "")),
        ),
        aws_bedrock=AWSBedrockSecretsConfig(
            access_key_id=_env("CONDUCTOR_AWS_ACCESS_KEY_ID", bdr_sec.get("access_key_id", "")),
            secret_access_key=_env("CONDUCTOR_AWS_SECRET_ACCESS_KEY", bdr_sec.get("secret_access_key", "")),
            session_token=_env("CONDUCTOR_AWS_SESSION_TOKEN", bdr_sec.get("session_token") or ""),
            region=_env("CONDUCTOR_AWS_REGION", bdr_sec.get("region", "us-east-1")),
        ),
        openai=OpenAISecretsConfig(
            api_key=_env("CONDUCTOR_OPENAI_API_KEY", oai_sec.get("api_key", "")),
            organization=oai_sec.get("organization"),
        ),
        alibaba=AlibabaSecretsConfig(
            api_key=_env("CONDUCTOR_ALIBABA_API_KEY", ali_sec.get("api_key", "")),
            base_url=_env(
                "CONDUCTOR_ALIBABA_BASE_URL",
                ali_sec.get("base_url", "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"),
            ),
        ),
        moonshot=MoonshotSecretsConfig(
            api_key=_env("CONDUCTOR_MOONSHOT_API_KEY", moon_sec.get("api_key", "")),
            base_url=_env("CONDUCTOR_MOONSHOT_BASE_URL", moon_sec.get("base_url", "https://api.moonshot.ai/v1")),
        ),
    )

    ai_models_raw = raw.get("ai_models", [])
    ai_models_cfg = [AIModelConfig(**m) for m in ai_models_raw]

    return ConductorConfig(
        summary=summary_cfg,
        ai_providers=ai_providers_cfg,
        ai_provider_settings=ai_provider_settings_cfg,
        ai_models=ai_models_cfg,
        sso=sso_cfg,
        google_sso=google_sso_cfg,
        google_sso_secrets=google_sso_secrets_cfg,
        logging=logging_cfg,
        prompt=prompt_cfg,
        change_limits=change_limits_cfg,
        jira=jira_cfg,
        jira_secrets=jira_secrets_cfg,
    )


# ---------------------------------------------------------------------------
# Global ConductorConfig singleton (lazy-loaded)
# ---------------------------------------------------------------------------

_conductor_config: Optional[ConductorConfig] = None


def get_config() -> ConductorConfig:
    """Return the process-wide :class:`ConductorConfig` singleton.

    The config is loaded lazily on first call and cached for the lifetime
    of the process.  Call :func:`reset_config` in tests to clear the cache.
    """
    global _conductor_config
    if _conductor_config is None:
        _conductor_config = load_config()
    return _conductor_config


def reset_config() -> None:
    """Clear the cached :class:`ConductorConfig` singleton (for use in tests)."""
    global _conductor_config
    _conductor_config = None
