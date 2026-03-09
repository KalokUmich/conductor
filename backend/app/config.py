"""Conducator application configuration.

Loads settings from two YAML files:
  * conductor.settings.yaml  — non-secret configuration
  * conductor.secrets.yaml   — secrets (never committed)

Supported embedding backends: local, bedrock, openai, voyage, mistral.
Default backend is ``bedrock`` with Cohere Embed v4.

Supported rerank backends: none, cohere, bedrock, cross_encoder.
Default is ``none`` (disabled). Enable for better search precision.

Secrets for Voyage, Mistral, and Cohere are loaded from
conductor.secrets.yaml and injected into environment variables so
downstream SDKs can use them.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field

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


class AwsSecrets(BaseModel):
    access_key_id:     Optional[str] = None
    secret_access_key: Optional[str] = None
    region:            Optional[str] = "us-east-1"


class OpenAISecrets(BaseModel):
    api_key: Optional[str] = None


class VoyageSecrets(BaseModel):
    """Voyage AI credentials (voyage-code-3 etc.)."""
    api_key: Optional[str] = None


class MistralSecrets(BaseModel):
    """Mistral AI credentials (codestral-embed-2505 etc.)."""
    api_key: Optional[str] = None


class CohereSecrets(BaseModel):
    """Cohere credentials (reranking and embedding)."""
    api_key: Optional[str] = None


class DatabaseSecrets(BaseModel):
    url: Optional[str] = None


class JWTSecrets(BaseModel):
    secret_key: str = "change-me-in-production"
    algorithm:  str = "HS256"


class Secrets(BaseModel):
    aws:      AwsSecrets      = Field(default_factory=AwsSecrets)
    openai:   OpenAISecrets   = Field(default_factory=OpenAISecrets)
    voyage:   VoyageSecrets   = Field(default_factory=VoyageSecrets)
    mistral:  MistralSecrets  = Field(default_factory=MistralSecrets)
    cohere:   CohereSecrets   = Field(default_factory=CohereSecrets)
    database: DatabaseSecrets = Field(default_factory=DatabaseSecrets)
    jwt:      JWTSecrets      = Field(default_factory=JWTSecrets)


# ---------------------------------------------------------------------------
# Settings models
# ---------------------------------------------------------------------------


class ServerSettings(BaseModel):
    host:         str  = "0.0.0.0"
    port:         int  = 8000
    debug:        bool = False
    reload:       bool = False
    log_level:    str  = "info"
    allowed_origins: List[str] = Field(default_factory=lambda: ["*"])


class DatabaseSettings(BaseModel):
    pool_size:     int = 10
    max_overflow:  int = 20
    pool_timeout:  int = 30
    echo_sql:      bool = False


class AuthSettings(BaseModel):
    token_expire_minutes:   int  = 60
    refresh_expire_days:    int  = 7
    require_email_verify:   bool = False


class RoomSettings(BaseModel):
    max_participants:       int = 50
    max_rooms_per_user:     int = 10
    session_timeout_minutes: int = 120
    enable_persistence:     bool = True


class LiveShareSettings(BaseModel):
    """Kept for backwards compatibility; disabled by default in new deployments."""
    enabled:              bool = False
    vscode_extension_id:  str  = "ms-vsliveshare.vsliveshare"
    host_timeout_seconds: int  = 300


class GitWorkspaceSettings(BaseModel):
    """Configuration for the Git Workspace module."""
    enabled:                bool                    = True
    workspaces_dir:         str                     = "./workspaces"
    git_auth_mode:          Literal["token", "delegate"] = "token"
    credential_ttl_seconds: int                     = 3600
    max_worktrees_per_repo: int                     = 20
    cleanup_on_room_close:  bool                    = True


class CodeSearchSettings(BaseModel):
    """Configuration for CocoIndex Code Search.

    Embedding uses LiteLLM model strings — supports 100+ providers::

        "sbert/sentence-transformers/all-MiniLM-L6-v2"  — local, free
        "bedrock/cohere.embed-v4:0"                     — AWS Bedrock
        "text-embedding-3-small"                        — OpenAI
        "voyage/voyage-code-3"                          — Voyage AI
        "mistral/codestral-embed-2505"                  — Mistral
        "cohere/embed-english-v3.0"                     — Cohere Direct
        "gemini/text-embedding-004"                     — Google Gemini

    Storage backends:
      * ``sqlite``   – embedded, zero setup (default)
      * ``postgres`` – incremental processing, concurrent access

    Rerank backend choices:
      * ``none``           – No reranking (default)
      * ``cohere``         – Cohere Rerank API (rerank-v3.5)
      * ``bedrock``        – AWS Bedrock Rerank (Cohere on Bedrock)
      * ``cross_encoder``  – Local cross-encoder model

    Credentials are read from ``conductor.secrets.yaml`` and injected
    into environment variables by :func:`_inject_embedding_env_vars`.
    """
    enabled:             bool = True
    index_dir:           str  = "./cocoindex_data"

    # -- Embedding (LiteLLM model string) --
    embedding_model:     str = "bedrock/cohere.embed-v4:0"
    embedding_dimensions: Optional[int] = None  # Auto-detected for known models

    # -- Storage backend --
    storage_backend:     Literal["sqlite", "postgres"] = "sqlite"
    postgres_url:        Optional[str] = None  # e.g. "postgresql://user:pass@host:5432/cocoindex"
    incremental:         bool = True  # Only effective with postgres backend

    # -- Chunking / search --
    chunk_size:          int = 512
    top_k_results:       int = 5

    # -- RepoMap (graph-based context) --
    repo_map_enabled:    bool = True
    repo_map_top_n:      int  = 10  # Top N files by PageRank to include in map

    # -- Reranking (post-retrieval) --
    rerank_backend:         Literal["none", "cohere", "bedrock", "cross_encoder"] = "none"
    rerank_top_n:           int  = 5   # Return top N after reranking
    rerank_candidates:      int  = 20  # Fetch this many candidates from vector search before reranking

    # -- Cohere Rerank (direct API) --
    cohere_rerank_model:    str = "rerank-v3.5"

    # -- Bedrock Rerank --
    bedrock_rerank_model_id: str = "cohere.rerank-v3-5:0"

    # -- Cross-encoder (local) --
    cross_encoder_model_name: str = "cross-encoder/ms-marco-MiniLM-L-6-v2"


class AppSettings(BaseModel):
    server:         ServerSettings       = Field(default_factory=ServerSettings)
    database:       DatabaseSettings     = Field(default_factory=DatabaseSettings)
    auth:           AuthSettings         = Field(default_factory=AuthSettings)
    rooms:          RoomSettings         = Field(default_factory=RoomSettings)
    live_share:     LiveShareSettings    = Field(default_factory=LiveShareSettings)
    git_workspace:  GitWorkspaceSettings = Field(default_factory=GitWorkspaceSettings)
    code_search:    CodeSearchSettings   = Field(default_factory=CodeSearchSettings)
    secrets:        Secrets              = Field(default_factory=Secrets)


# ---------------------------------------------------------------------------
# Environment variable injection for embedding + reranking providers
# ---------------------------------------------------------------------------


def _inject_embedding_env_vars(settings: AppSettings) -> None:
    """Inject credentials from conductor.secrets.yaml into environment variables.

    LiteLLM and cocoindex-code read credentials from well-known env vars.
    This function maps our unified secrets config to those env vars so
    everything works without extra configuration.

    The embedding model string (e.g. ``bedrock/cohere.embed-v4:0``) determines
    which credentials are needed.  We inject all available credentials so
    switching models only requires changing the model string.

    Mapping::

        AWS       → AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
        OpenAI    → OPENAI_API_KEY
        Voyage    → VOYAGE_API_KEY
        Mistral   → MISTRAL_API_KEY
        Cohere    → CO_API_KEY
        Postgres  → COCOINDEX_DATABASE_URL
    """
    secrets = settings.secrets

    # --- Always inject all available credentials ---
    # This allows model switching without re-injecting env vars.

    # AWS (Bedrock embedding, reranking)
    if secrets.aws.access_key_id:
        os.environ.setdefault("AWS_ACCESS_KEY_ID", secrets.aws.access_key_id)
    if secrets.aws.secret_access_key:
        os.environ.setdefault("AWS_SECRET_ACCESS_KEY", secrets.aws.secret_access_key)
    if secrets.aws.region:
        os.environ.setdefault("AWS_DEFAULT_REGION", secrets.aws.region)

    # OpenAI
    if secrets.openai.api_key:
        os.environ.setdefault("OPENAI_API_KEY", secrets.openai.api_key)

    # Voyage AI
    if secrets.voyage.api_key:
        os.environ.setdefault("VOYAGE_API_KEY", secrets.voyage.api_key)

    # Mistral AI
    if secrets.mistral.api_key:
        os.environ.setdefault("MISTRAL_API_KEY", secrets.mistral.api_key)

    # Cohere (embedding + reranking)
    if secrets.cohere.api_key:
        os.environ.setdefault("CO_API_KEY", secrets.cohere.api_key)

    # Postgres (CocoIndex incremental processing backend)
    if settings.code_search.storage_backend == "postgres":
        pg_url = settings.code_search.postgres_url
        if pg_url:
            os.environ.setdefault("COCOINDEX_DATABASE_URL", pg_url)
            logger.info("Injected COCOINDEX_DATABASE_URL for Postgres backend.")

    # CocoIndex embedding model env var
    model = settings.code_search.embedding_model
    os.environ["COCOINDEX_CODE_EMBEDDING_MODEL"] = model

    logger.info(
        "Injected embedding env vars (model=%s, storage=%s).",
        model,
        settings.code_search.storage_backend,
    )


# ---------------------------------------------------------------------------
# Public loader
# ---------------------------------------------------------------------------


def load_settings() -> AppSettings:
    """Load and merge settings + secrets into a single *AppSettings* object.

    Searches for config files in standard locations
    (see :func:`_find_config_file` for the search order).
    """
    settings_path = _find_config_file("conductor.settings.yaml")
    secrets_path  = _find_config_file("conductor.secrets.yaml")

    settings_data = _load_yaml(settings_path)
    secrets_data  = _load_yaml(secrets_path)

    # Merge: secrets live under the "secrets" key in AppSettings
    settings_data["secrets"] = secrets_data

    app_settings = AppSettings(**settings_data)
    logger.info(
        "Settings loaded (server=%s:%s, git_workspace.enabled=%s, code_search.enabled=%s, "
        "embedding_model=%s, storage=%s, rerank_backend=%s)",
        app_settings.server.host,
        app_settings.server.port,
        app_settings.git_workspace.enabled,
        app_settings.code_search.enabled,
        app_settings.code_search.embedding_model,
        app_settings.code_search.storage_backend,
        app_settings.code_search.rerank_backend,
    )
    return app_settings


# ---------------------------------------------------------------------------
# AI Provider config models
# ---------------------------------------------------------------------------


class SummaryConfig(BaseModel):
    """Configuration for AI summarization feature."""
    enabled:       bool = False
    default_model: str  = "claude-3-haiku-bedrock"


class AnthropicSecretsConfig(BaseModel):
    """Anthropic API credentials."""
    api_key: str = ""


class AWSBedrockSecretsConfig(BaseModel):
    """AWS Bedrock credentials."""
    access_key_id:     str           = ""
    secret_access_key: str           = ""
    session_token:     Optional[str] = None
    region:            str           = "us-east-1"


class OpenAISecretsConfig(BaseModel):
    """OpenAI API credentials."""
    api_key:      str           = ""
    organization: Optional[str] = None


class AIProvidersSecretsConfig(BaseModel):
    """Credentials for all AI providers."""
    anthropic:   AnthropicSecretsConfig   = Field(default_factory=AnthropicSecretsConfig)
    aws_bedrock: AWSBedrockSecretsConfig  = Field(default_factory=AWSBedrockSecretsConfig)
    openai:      OpenAISecretsConfig      = Field(default_factory=OpenAISecretsConfig)


class AIProviderSettingsConfig(BaseModel):
    """Enable/disable flags for each AI provider."""
    anthropic_enabled:   bool = False
    aws_bedrock_enabled: bool = False
    openai_enabled:      bool = False


class AIModelConfig(BaseModel):
    """Configuration for a single AI model."""
    id:           str  = ""
    provider:     str  = ""
    model_name:   str  = ""
    display_name: str  = ""
    enabled:      bool = True


# ---------------------------------------------------------------------------
# SSO config models
# ---------------------------------------------------------------------------


class SSOConfig(BaseModel):
    """AWS SSO configuration."""
    enabled:   bool = False
    start_url: str  = ""
    region:    str  = "us-east-1"


class GoogleSSOConfig(BaseModel):
    """Google OAuth SSO configuration."""
    enabled: bool = False


class GoogleSSOSecretsConfig(BaseModel):
    """Google OAuth credentials."""
    client_id:     str = ""
    client_secret: str = ""


# ---------------------------------------------------------------------------
# Logging config model
# ---------------------------------------------------------------------------


class LoggingConfig(BaseModel):
    """Logging and audit configuration."""
    audit_enabled: bool           = False
    audit_path:    Optional[str]  = None


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
    max_total_lines:       int = 50
    auto_apply:            AutoApplyLimitsConfig = Field(default_factory=AutoApplyLimitsConfig)


# ---------------------------------------------------------------------------
# Top-level ConductorConfig (used by AI provider, audit, auth, policy modules)
# ---------------------------------------------------------------------------


class SessionConfig(BaseModel):
    """Session / room participation limits."""
    max_participants: int = 50


class ConductorConfig(BaseModel):
    """Unified top-level configuration used by newer modules."""
    summary:             SummaryConfig             = Field(default_factory=SummaryConfig)
    ai_providers:        AIProvidersSecretsConfig  = Field(default_factory=AIProvidersSecretsConfig)
    ai_provider_settings: AIProviderSettingsConfig = Field(default_factory=AIProviderSettingsConfig)
    ai_models:           List[AIModelConfig]       = Field(default_factory=list)
    sso:                 SSOConfig                 = Field(default_factory=SSOConfig)
    google_sso:          GoogleSSOConfig           = Field(default_factory=GoogleSSOConfig)
    google_sso_secrets:  GoogleSSOSecretsConfig    = Field(default_factory=GoogleSSOSecretsConfig)
    logging:             LoggingConfig             = Field(default_factory=LoggingConfig)
    prompt:              PromptConfig              = Field(default_factory=PromptConfig)
    change_limits:       ChangeLimitsConfig        = Field(default_factory=ChangeLimitsConfig)
    session:             SessionConfig             = Field(default_factory=SessionConfig)


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

    # Secrets
    secrets_raw = _load_yaml(secrets_path)
    gsso_sec = secrets_raw.get("google_sso", {})
    google_sso_secrets_cfg = GoogleSSOSecretsConfig(
        client_id=gsso_sec.get("client_id", ""),
        client_secret=gsso_sec.get("client_secret", ""),
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
    )

    ap_sec = secrets_raw.get("ai_providers", {})
    anth_sec = ap_sec.get("anthropic", {})
    bdr_sec = ap_sec.get("aws_bedrock", {})
    oai_sec = ap_sec.get("openai", {})
    ai_providers_cfg = AIProvidersSecretsConfig(
        anthropic=AnthropicSecretsConfig(api_key=anth_sec.get("api_key", "")),
        aws_bedrock=AWSBedrockSecretsConfig(
            access_key_id=bdr_sec.get("access_key_id", ""),
            secret_access_key=bdr_sec.get("secret_access_key", ""),
            session_token=bdr_sec.get("session_token"),
            region=bdr_sec.get("region", "us-east-1"),
        ),
        openai=OpenAISecretsConfig(
            api_key=oai_sec.get("api_key", ""),
            organization=oai_sec.get("organization"),
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
