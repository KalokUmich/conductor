"""Conducator FastAPI application entry point.

Lifespan initializes:
  * Database connection pool
  * Git Workspace Service (replaces Live Share)
  * AI Provider Resolver (unified — powers summary, agent loop, etc.)
  * Agent Loop provider (for agentic code search)
  * Code Tools (code intelligence tools for the agent loop)
  * Ngrok tunnel (when enabled) for external access from VS Code webview

"""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from .config import (
    AppSettings,
    _find_config_file,
    _load_yaml,
    get_config,
    load_settings,
)
from .git_workspace.service import GitWorkspaceService
from .git_workspace.delegate_broker import DelegateBroker
from .ai_provider.resolver import ProviderResolver, set_resolver
from .ngrok_service import get_public_url, start_ngrok, stop_ngrok

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Logging configuration
# ---------------------------------------------------------------------------

def _configure_logging() -> None:
    """Set up logging for all app.* loggers.

    Called once at module import time so all logger.info() calls are visible
    in the uvicorn console.
    """
    log_format = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
    logging.basicConfig(level=logging.INFO, format=log_format, force=True)
    # Quiet down noisy libraries
    logging.getLogger("botocore").setLevel(logging.WARNING)
    logging.getLogger("boto3").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)

_configure_logging()


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

    # ---- AI Provider Resolver ----
    agent_provider = None
    try:
        conductor_config = get_config()
        resolver = ProviderResolver(conductor_config)
        resolver.resolve()
        set_resolver(resolver)
        ai_status = resolver.get_status()
        logger.info(
            "AI Provider Resolver initialized: active_model=%s, active_provider=%s",
            ai_status.active_model,
            ai_status.active_provider,
        )
        # Use the active provider for the agent loop
        agent_provider = resolver.get_active_provider()
        if agent_provider:
            logger.info("Agent loop provider ready.")
        else:
            logger.warning("No healthy AI provider — agent loop disabled.")

        # Classifier provider (lightweight model for query pre-classification)
        classifier_provider = resolver.get_classifier_provider()
        if classifier_provider:
            logger.info("Classifier provider ready.")
        else:
            logger.info("No classifier model configured — using keyword classification.")
    except Exception as exc:
        logger.warning("Failed to initialize AI provider resolver: %s", exc)
        classifier_provider = None
    app.state.agent_provider = agent_provider
    app.state.classifier_provider = classifier_provider

    # Auto-enable classifier if a classifier model is available
    active_classifier_id = None
    if classifier_provider is not None:
        status = resolver.get_status()
        for m in status.models:
            if m.classifier and m.available:
                active_classifier_id = m.id
                break
    app.state.active_classifier_model_id = active_classifier_id

    # ---- Session Trace Writer ----
    from .agent_loop.trace import TraceWriter
    trace_writer = TraceWriter.from_settings(settings.trace)
    app.state.trace_writer = trace_writer
    logger.info(
        "Trace writer: enabled=%s, backend=%s",
        settings.trace.enabled, settings.trace.backend,
    )

    # ---- Ngrok tunnel ----
    # Read ngrok config from raw YAML (not modelled in AppSettings).
    # Required for VS Code Remote-WSL: the webview runs in the Windows
    # Electron process and cannot reach WSL's localhost directly.
    _settings_raw = _load_yaml(_find_config_file("conductor.settings.yaml"))
    _secrets_raw  = _load_yaml(_find_config_file("conductor.secrets.yaml"))
    ngrok_cfg = _settings_raw.get("ngrok", {})
    ngrok_sec = _secrets_raw.get("ngrok", {})

    if ngrok_cfg.get("enabled", False):
        logger.info("Ngrok is enabled, starting tunnel...")
        ngrok_url = start_ngrok(
            port=settings.server.port,
            authtoken=ngrok_sec.get("authtoken", ""),
            region=ngrok_cfg.get("region", "us"),
        )
        if ngrok_url:
            logger.info("Ngrok tunnel active: %s", ngrok_url)
        else:
            logger.warning(
                "Failed to start ngrok tunnel. "
                "Falling back to localhost:%s", settings.server.port
            )
    else:
        logger.info(
            "Ngrok disabled. Server running on http://%s:%s",
            settings.server.host, settings.server.port,
        )

    # ---- Bedrock Model Catalog ----
    try:
        _catalog_config = get_config()
        _bdr = _catalog_config.ai_providers.aws_bedrock
        bedrock_region = _bdr.region or "eu-west-2"
        from .langextract.catalog import BedrockCatalog
        catalog = BedrockCatalog(
            region=bedrock_region,
            access_key_id=_bdr.access_key_id or None,
            secret_access_key=_bdr.secret_access_key or None,
            session_token=_bdr.session_token or None,
        )
        catalog.refresh()
        app.state.bedrock_catalog = catalog
        logger.info("Bedrock catalog: %d models in %s", len(catalog.get_all_models()), bedrock_region)
    except Exception as exc:
        logger.warning("Failed to initialize Bedrock catalog: %s", exc)
        app.state.bedrock_catalog = None

    logger.info("Conducator startup complete.")
    yield
    # ---- Shutdown ----
    stop_ngrok()
    await git_service.shutdown()
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

    # --- Private Network Access (PNA) middleware ---
    # Chrome 105+ blocks WebSocket/fetch from vscode-webview:// origins to
    # localhost unless the server returns Access-Control-Allow-Private-Network: true.
    #
    # IMPORTANT: We use a pure ASGI middleware (not BaseHTTPMiddleware) because
    # BaseHTTPMiddleware buffers the response body, which is incompatible with the
    # HTTP 101 Switching Protocols response that WebSocket upgrade requires.
    # Using BaseHTTPMiddleware would silently kill every WebSocket connection with
    # close code 1006 before the request ever reaches the FastAPI handler.
    class PrivateNetworkAccessMiddleware:
        """Pure ASGI middleware — safe for both HTTP and WebSocket connections."""

        def __init__(self, app: ASGIApp) -> None:
            self.app = app

        async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http":
                headers = dict(scope.get("headers", []))
                method = scope.get("method", "")
                pna_requested = headers.get(b"access-control-request-private-network", b"").decode()

                if method == "OPTIONS" and pna_requested == "true":
                    # Respond to PNA preflight immediately — do NOT call the app
                    origin = headers.get(b"origin", b"*").decode()
                    await send({
                        "type": "http.response.start",
                        "status": 204,
                        "headers": [
                            (b"access-control-allow-origin", origin.encode()),
                            (b"access-control-allow-private-network", b"true"),
                            (b"access-control-allow-methods", b"*"),
                            (b"access-control-allow-headers", b"*"),
                            (b"access-control-max-age", b"7200"),
                        ],
                    })
                    await send({"type": "http.response.body", "body": b""})
                    return

                # Regular HTTP: inject PNA header into every response
                async def send_with_pna(message: dict) -> None:
                    if message["type"] == "http.response.start":
                        message = dict(message)
                        message["headers"] = list(message.get("headers", [])) + [
                            (b"access-control-allow-private-network", b"true"),
                        ]
                    await send(message)

                await self.app(scope, receive, send_with_pna)

            elif scope["type"] == "websocket":
                # WebSocket: inject PNA header into the 101 accept response so
                # Chrome's PNA check passes, then let the connection proceed normally.
                async def send_with_pna(message: dict) -> None:
                    if message["type"] == "websocket.accept":
                        message = dict(message)
                        extra = list(message.get("headers") or [])
                        extra.append((b"access-control-allow-private-network", b"true"))
                        message["headers"] = extra
                    await send(message)

                await self.app(scope, receive, send_with_pna)

            else:
                # lifespan, etc. — pass through unchanged
                await self.app(scope, receive, send)

    app.add_middleware(PrivateNetworkAccessMiddleware)

    # --- Routers ---
    from .git_workspace.router import router as git_workspace_router
    from .code_tools.router    import router as code_tools_router
    from .agent_loop.router    import router as agent_loop_router
    from .ai_provider.router   import router as ai_provider_router
    from .audit.router         import router as audit_router
    from .policy.router        import router as policy_router
    from .chat.router          import router as chat_router
    from .chat.settings_router import router as chat_settings_router
    from .agent.router         import router as agent_router
    from .auth.router          import router as auth_router
    from .files.router         import router as files_router
    from .todos.router         import router as todos_router
    from .workspace_files.router import router as workspace_files_router
    from .langextract.router     import router as langextract_router
    from .code_review.router     import router as code_review_router

    app.include_router(git_workspace_router)
    app.include_router(code_tools_router)
    app.include_router(agent_loop_router)
    app.include_router(ai_provider_router)
    app.include_router(audit_router)
    app.include_router(policy_router)
    app.include_router(chat_router)
    app.include_router(chat_settings_router)
    app.include_router(agent_router)
    app.include_router(auth_router)
    app.include_router(files_router)
    app.include_router(todos_router)
    app.include_router(workspace_files_router)
    app.include_router(langextract_router)
    app.include_router(code_review_router)

    # --- Health check ---
    @app.get("/health", include_in_schema=True)
    async def health() -> dict:
        """Simple liveness probe."""
        return {"status": "ok"}

    # --- Public URL ---
    _settings_for_public_url = settings or load_settings()

    @app.get("/public-url", include_in_schema=True)
    async def public_url() -> dict:
        """Return the public URL so the extension can build correct invite
        links.  Priority: live ngrok tunnel > configured public_url in YAML."""
        # Prefer the live ngrok tunnel URL (set by start_ngrok in lifespan)
        live_url = get_public_url()
        if live_url:
            return {"public_url": live_url}
        # Fall back to the static value from conductor.settings.yaml
        url = (_settings_for_public_url.server.public_url or "").strip()
        return {"public_url": url}

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
