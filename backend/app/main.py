"""Conductor Backend Application.

This is the main entry point for the Conductor backend service.
Conductor is a VS Code extension that combines Live Share, real-time chat,
and AI-powered code generation for collaborative development.

Modules:
    - chat: WebSocket-based real-time chat rooms
    - summary: Chat message summarization (keyword-based extraction)
    - agent: AI code generation (currently MockAgent for testing)
    - policy: Auto-apply policy evaluation
    - audit: DuckDB-based audit logging
    - ai_provider: AI provider resolution and management
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.agent.router import router as agent_router
from app.ai_provider.resolver import ProviderResolver, set_resolver
from app.ai_provider.router import router as ai_router
from app.audit.router import router as audit_router
from app.chat.router import router as chat_router
from app.config import get_config
from app.files.router import router as files_router
from app.ngrok_service import get_public_url, start_ngrok, stop_ngrok
from app.policy.router import router as policy_router
from app.summary.router import router as summary_router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan manager for startup/shutdown events."""
    # Startup
    config = get_config()

    if config.ngrok_settings.enabled:
        logger.info("Ngrok is enabled in config, starting tunnel...")
        public_url = start_ngrok(
            port=config.server.port,
            authtoken=config.ngrok_secrets.authtoken,
            region=config.ngrok_settings.region
        )
        if public_url:
            logger.info(f"✅ Ngrok tunnel active: {public_url}")
            print(f"\n{'='*60}")
            print(f"🌐 PUBLIC URL: {public_url}")
            print(f"{'='*60}\n")
        else:
            logger.warning(
                "⚠️ Failed to start ngrok tunnel. "
                f"Falling back to localhost:{config.server.port}"
            )
            print(f"\n{'='*60}")
            print(f"⚠️ Ngrok failed - using http://localhost:{config.server.port}")
            print(f"{'='*60}\n")
    else:
        logger.info(
            f"Ngrok disabled. Server running on "
            f"http://{config.server.host}:{config.server.port}"
        )

    # Initialize AI provider resolver if summary is enabled
    if config.summary.enabled:
        logger.info("Summary enabled, resolving AI providers...")
        resolver = ProviderResolver(config)  # Pass full config for new architecture
        active = resolver.resolve()
        set_resolver(resolver)
        if active:
            logger.info(f"✅ AI active: model={resolver.active_model_id}, provider={resolver.active_provider_type}")
        else:
            logger.warning("⚠️ No healthy AI provider found")
    else:
        logger.info("Summary disabled, skipping AI provider resolution")

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
app.include_router(summary_router)
app.include_router(agent_router)
app.include_router(policy_router)
app.include_router(audit_router)
app.include_router(files_router)
app.include_router(ai_router)


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

