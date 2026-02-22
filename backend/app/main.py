"""Conductor Backend Application.

This is the main entry point for the Conductor backend service.
Conductor is a VS Code extension that combines Live Share, real-time chat,
and AI-powered code generation for collaborative development.

Modules:
    - chat: WebSocket-based real-time chat rooms
    - ai_provider: AI provider resolution and management
    - agent: AI code generation (currently MockAgent for testing)
    - policy: Auto-apply policy evaluation
    - audit: DuckDB-based audit logging
    - auth: AWS SSO (IAM Identity Center) authentication
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.router import router as agent_router
from app.ai_provider.resolver import ProviderResolver, set_resolver
from app.ai_provider.router import router as ai_router
from app.audit.router import router as audit_router
from app.auth.router import router as auth_router
from app.chat.router import router as chat_router
from app.chat.settings_router import router as room_settings_router
from app.config import get_config
from app.context.router import router as context_router
from app.files.router import router as files_router
from app.ngrok_service import get_public_url, start_ngrok, stop_ngrok
from app.policy.router import router as policy_router
from app.todos.router import router as todos_router
from app.embeddings.bedrock import BedrockEmbeddingProvider
from app.embeddings.router import router as embeddings_router
from app.embeddings.service import EmbeddingService, get_embedding_service, set_embedding_service
from app.rag.indexer import RagIndexer
from app.rag.router import router as rag_router, set_indexer

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# Silence verbose third-party loggers.
# botocore.auth logs the full SigV4 canonical request — including
# x-amz-security-token — which leaks credentials into the console.
# urllib3/httpx/httpcore log every TCP connection and TLS handshake.
# None of these are useful when debugging business logic.
for _noisy in (
    "botocore",
    "boto3",
    "urllib3",
    "urllib3.connectionpool",
    "httpx",
    "httpcore",
    "httpcore.http11",
    "httpcore.connection",
    "pyngrok",               # "join connections" on every HTTP request
):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown events."""
    # Startup
    config = get_config()

    # Apply configured log level to root logger so that
    # `logging.level: "debug"` in conductor.settings.yaml activates DEBUG output.
    configured_level = getattr(logging, config.logging.level.upper(), None)
    if configured_level is not None:
        logging.getLogger().setLevel(configured_level)
        logger.info("Root logger level set to %s", config.logging.level.upper())

    if config.ngrok_settings.enabled:
        logger.info("Ngrok is enabled in config, starting tunnel...")
        public_url = start_ngrok(
            port=config.server.port,
            authtoken=config.ngrok_secrets.authtoken,
            region=config.ngrok_settings.region
        )
        if public_url:
            logger.info(f"Ngrok tunnel active: {public_url}")
        else:
            logger.warning(
                f"Failed to start ngrok tunnel. "
                f"Falling back to localhost:{config.server.port}"
            )
    else:
        logger.info(
            f"Ngrok disabled. Server running on "
            f"http://{config.server.host}:{config.server.port}"
        )

    # Initialize AI provider resolver if AI features are enabled
    if config.summary.enabled:
        logger.info("AI features enabled, resolving providers...")
        resolver = ProviderResolver(config)  # Pass full config for new architecture
        active = resolver.resolve()
        set_resolver(resolver)
        if active:
            logger.info(f"AI active: model={resolver.active_model_id}, provider={resolver.active_provider_type}")
        else:
            logger.warning("No healthy AI provider found")
    else:
        logger.info("AI features disabled, skipping provider resolution")

    # Initialise embedding service if Bedrock credentials are available.
    emb_cfg = config.embedding
    bedrock_cfg = config.ai_providers.aws_bedrock
    if emb_cfg.provider == "bedrock":
        try:
            provider = BedrockEmbeddingProvider(
                model_id=emb_cfg.model,
                dim=emb_cfg.dim,
                aws_access_key_id=bedrock_cfg.access_key_id or None,
                aws_secret_access_key=bedrock_cfg.secret_access_key or None,
                aws_session_token=bedrock_cfg.session_token or None,
                region_name=bedrock_cfg.region,
            )
            set_embedding_service(EmbeddingService(provider))
            logger.info(
                "Embedding service ready: provider=bedrock model=%s dim=%d",
                emb_cfg.model,
                emb_cfg.dim,
            )
        except Exception as exc:
            logger.warning("Failed to initialise embedding service: %s", exc)
    else:
        logger.info(
            "Embedding provider '%s' not yet supported; service disabled.",
            emb_cfg.provider,
        )

    # Initialise RAG indexer if enabled and embedding service is available.
    rag_cfg = config.rag
    if rag_cfg.enabled and get_embedding_service() is not None:
        try:
            indexer = RagIndexer(
                data_dir=rag_cfg.data_dir,
                dim=emb_cfg.dim,
            )
            set_indexer(indexer)
            logger.info(
                "RAG indexer ready: data_dir=%s dim=%d",
                rag_cfg.data_dir,
                emb_cfg.dim,
            )
        except Exception as exc:
            logger.warning("Failed to initialise RAG indexer: %s", exc)
    elif rag_cfg.enabled:
        logger.info("RAG enabled but no embedding service available; indexer disabled.")
    else:
        logger.info("RAG disabled in config.")

    yield  # Application runs here

    # Shutdown
    stop_ngrok()
    logger.info("Application shutdown complete")


# Create FastAPI application with metadata
app = FastAPI(
    title="Conductor API",
    description="Backend service for Conductor - AI collaborative coding extension",
    version="0.1.0",
    lifespan=lifespan,
)

# Register all routers
app.include_router(chat_router)
app.include_router(agent_router)
app.include_router(policy_router)
app.include_router(audit_router)
app.include_router(files_router)
app.include_router(ai_router)
app.include_router(auth_router)
app.include_router(room_settings_router)
app.include_router(context_router)
app.include_router(todos_router)
app.include_router(embeddings_router)
app.include_router(rag_router)


@app.get("/health")
async def health() -> dict:
    """Health check endpoint.

    Returns:
        dict: Status object indicating the server is running.
    """
    return {"status": "ok"}


@app.get("/public-url")
async def public_url() -> dict:
    """Get the public URL (ngrok) if available.

    Returns:
        dict: Object with public_url field (null if not available).
    """
    return {"public_url": get_public_url()}

