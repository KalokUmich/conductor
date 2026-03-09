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

    Embedding backend choices:
      * ``local``   – SentenceTransformers (free, no API key)
      * ``bedrock`` – AWS Bedrock (default: Cohere Embed v4)
      * ``openai``  – OpenAI Embeddings API
      * ``voyage``  – Voyage AI (code-specialised)
      * ``mistral`` – Mistral Embeddings (Codestral Embed)

    Rerank backend choices:
      * ``none``           – No reranking (default)
      * ``cohere``         – Cohere Rerank API (rerank-v3.5)
      * ``bedrock``        – AWS Bedrock Rerank (Cohere on Bedrock)
      * ``cross_encoder``  – Local cross-encoder model

    The default embedding is ``bedrock`` with ``cohere.embed-v4:0``.
    Credentials are read from ``conductor.secrets.yaml`` and injected
    into environment variables by :func:`_inject_embedding_env_vars`.
    """
    enabled:             bool = True
    index_dir:           str  = "./cocoindex_data"
    embedding_backend:   Literal["local", "bedrock", "openai", "voyage", "mistral"] = "bedrock"

    # -- Local (SentenceTransformers) --
    local_model_name:    str = "all-MiniLM-L6-v2"

    # -- Bedrock --
    bedrock_model_id:    str = "cohere.embed-v4:0"
    bedrock_region:      str = "us-east-1"

    # -- OpenAI --
    openai_model_name:   str = "text-embedding-3-small"

    # -- Voyage --
    voyage_model_name:   str = "voyage-code-3"

    # -- Mistral --
    mistral_model_name:  str = "codestral-embed-2505"

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

    Each embedding SDK reads credentials from well-known env vars.
    This function maps our unified secrets config to those env vars so
    the SDKs work without extra configuration.

    Mapping::

        bedrock  → AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_DEFAULT_REGION
        openai   → OPENAI_API_KEY
        voyage   → VOYAGE_API_KEY
        mistral  → MISTRAL_API_KEY
        cohere   → CO_API_KEY (for direct Cohere Rerank API)
    """
    backend = settings.code_search.embedding_backend
    secrets = settings.secrets

    if backend == "bedrock":
        if secrets.aws.access_key_id:
            os.environ["AWS_ACCESS_KEY_ID"]     = secrets.aws.access_key_id
        if secrets.aws.secret_access_key:
            os.environ["AWS_SECRET_ACCESS_KEY"] = secrets.aws.secret_access_key
        region = settings.code_search.bedrock_region or secrets.aws.region
        if region:
            os.environ["AWS_DEFAULT_REGION"]    = region
        logger.info("Injected AWS credentials for Bedrock embedding backend.")

    elif backend == "openai":
        if secrets.openai.api_key:
            os.environ["OPENAI_API_KEY"] = secrets.openai.api_key
        logger.info("Injected OpenAI API key for OpenAI embedding backend.")

    elif backend == "voyage":
        if secrets.voyage.api_key:
            os.environ["VOYAGE_API_KEY"] = secrets.voyage.api_key
        logger.info("Injected Voyage API key for Voyage embedding backend.")

    elif backend == "mistral":
        if secrets.mistral.api_key:
            os.environ["MISTRAL_API_KEY"] = secrets.mistral.api_key
        logger.info("Injected Mistral API key for Mistral embedding backend.")

    else:  # local
        logger.debug("Local embedding backend — no env var injection needed.")

    # --- Reranking credentials ---
    rerank_backend = settings.code_search.rerank_backend

    if rerank_backend == "cohere":
        if secrets.cohere.api_key:
            os.environ["CO_API_KEY"] = secrets.cohere.api_key
        logger.info("Injected Cohere API key for Cohere rerank backend.")

    elif rerank_backend == "bedrock":
        # Reuse AWS credentials already injected above (or inject if not yet)
        if secrets.aws.access_key_id and "AWS_ACCESS_KEY_ID" not in os.environ:
            os.environ["AWS_ACCESS_KEY_ID"]     = secrets.aws.access_key_id
        if secrets.aws.secret_access_key and "AWS_SECRET_ACCESS_KEY" not in os.environ:
            os.environ["AWS_SECRET_ACCESS_KEY"] = secrets.aws.secret_access_key
        region = settings.code_search.bedrock_region or secrets.aws.region
        if region and "AWS_DEFAULT_REGION" not in os.environ:
            os.environ["AWS_DEFAULT_REGION"]    = region
        logger.info("Injected AWS credentials for Bedrock rerank backend.")


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
        "embedding_backend=%s, rerank_backend=%s)",
        app_settings.server.host,
        app_settings.server.port,
        app_settings.git_workspace.enabled,
        app_settings.code_search.enabled,
        app_settings.code_search.embedding_backend,
        app_settings.code_search.rerank_backend,
    )
    return app_settings
