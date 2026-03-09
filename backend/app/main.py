"""Conducator FastAPI application entry point.

Lifespan initializes:
  * Database connection pool
  * Git Workspace Service (replaces Live Share)
  * CocoIndex Code Search Service (replaces home-built RAG)
  * Embedding Provider via LiteLLM (100+ backends, single model string)
  * RepoMap Graph Service (Aider-style file dependency graph + PageRank)
  * Rerank Provider (configurable: none / cohere / bedrock / cross_encoder)
  * Optional Postgres backend for incremental processing

Removed in this version:
  * FAISS index loading
  * Bedrock Embeddings initialisation
  * Old RAG module imports
  * Hand-written per-provider embedding classes (replaced by LiteLLM)

"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse

from .config import AppSettings, _inject_embedding_env_vars, load_settings
from .git_workspace.service import GitWorkspaceService
from .git_workspace.delegate_broker import DelegateBroker
from .code_search.service import CodeSearchService
from .code_search.rerank_provider import create_rerank_provider

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application startup / shutdown lifecycle."""
    settings: AppSettings = load_settings()

    # ---- Git Workspace ----
    git_service    = GitWorkspaceService()
    delegate_broker = DelegateBroker()
    if settings.git_workspace.enabled:
        await git_service.initialize(settings.git_workspace)
        logger.info("Git Workspace module initialized.")
    app.state.git_workspace_service = git_service
    app.state.delegate_broker       = delegate_broker

    # ---- CocoIndex Code Search ----
    code_search_service = CodeSearchService()
    if settings.code_search.enabled:
        _inject_embedding_env_vars(settings)    # inject secrets → env vars
        await code_search_service.initialize(settings.code_search)
        logger.info("CocoIndex Code Search initialized.")
    app.state.code_search_service = code_search_service

    # ---- RepoMap Graph Service ----
    repo_map_service = None
    if settings.code_search.repo_map_enabled:
        try:
            from .repo_graph.service import RepoMapService
            repo_map_service = RepoMapService(
                top_n=settings.code_search.repo_map_top_n,
            )
            logger.info("RepoMap graph service initialized (top_n=%d).",
                         settings.code_search.repo_map_top_n)
        except ImportError as exc:
            logger.warning(
                "RepoMap dependencies not available (%s). "
                "Install tree-sitter + networkx to enable.", exc
            )
    app.state.repo_map_service = repo_map_service

    # ---- Reranking Service ----
    rerank_provider = None
    try:
        rerank_provider = create_rerank_provider(settings.code_search)
        logger.info("Rerank provider initialized: %s", rerank_provider.name)
    except Exception as exc:
        logger.warning(
            "Failed to create rerank provider (%s): %s — reranking disabled.",
            settings.code_search.rerank_backend,
            exc,
        )
    app.state.rerank_provider = rerank_provider

    logger.info("Conducator startup complete.")
    yield
    # ---- Shutdown ----
    await git_service.shutdown()
    await code_search_service.shutdown()
    logger.info("Conducator shutdown complete.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title       = "Conducator",
        description = "Real-time collaborative coding backend",
        version     = "2.0.0",
        lifespan    = lifespan,
    )

    # --- CORS ---
    _s = settings or load_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins     = _s.server.allowed_origins,
        allow_credentials = True,
        allow_methods     = ["*"],
        allow_headers     = ["*"],
    )

    # --- Routers ---
    from .git_workspace.router import router as git_workspace_router
    from .code_search.router   import router as code_search_router
    from .context.router       import router as context_router
    from .ai_provider.router   import router as ai_provider_router
    from .audit.router         import router as audit_router
    from .policy.router        import router as policy_router
    from .chat.router          import router as chat_router
    from .chat.settings_router import router as chat_settings_router
    from .agent.router         import router as agent_router

    app.include_router(git_workspace_router)
    app.include_router(code_search_router)
    app.include_router(context_router)
    app.include_router(ai_provider_router)
    app.include_router(audit_router)
    app.include_router(policy_router)
    app.include_router(chat_router)
    app.include_router(chat_settings_router)
    app.include_router(agent_router)

    # --- Health check ---
    @app.get("/health", include_in_schema=True)
    async def health() -> dict:
        """Simple liveness probe."""
        return {"status": "ok"}

    # --- Prometheus-compatible metrics scrape endpoint ---
    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """Minimal Prometheus metrics endpoint.

        Returns a single ``conducator_up`` gauge so that Prometheus / Victoria
        Metrics scrapers receive a 200 instead of a 404.  Install
        ``prometheus-fastapi-instrumentator`` if you need real request metrics.
        """
        body = (
            "# HELP conducator_up Whether the Conducator backend is running\n"
            "# TYPE conducator_up gauge\n"
            "conducator_up 1\n"
        )
        return PlainTextResponse(body, media_type="text/plain; version=0.0.4; charset=utf-8")

    return app


# Module-level app instance (used by uvicorn and test fixtures)
app = create_app()
