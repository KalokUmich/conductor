"""Conducator FastAPI application entry point.

Lifespan initializes:
  * Database connection pool
  * Git Workspace Service (backend-managed worktrees for multi-user mode)
  * AI Provider Resolver (unified — powers summary, agent loop, etc.)
  * Agent Loop provider (for agentic code search)
  * Code Tools (code intelligence tools for the agent loop)
  * Ngrok tunnel (when enabled) for external access from VS Code webview

"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager, suppress
from typing import AsyncGenerator, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .ai_provider.resolver import ProviderResolver, set_resolver
from .config import (
    AppSettings,
    get_config,
    load_settings,
)
from .git_workspace.delegate_broker import DelegateBroker
from .git_workspace.service import GitWorkspaceService
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

    # ---- PostgreSQL (with startup retry) ----
    # create_async_engine() is lazy and never actually connects, so we perform
    # a real SELECT 1 probe after creation.  If Postgres is not ready (common in
    # Docker Compose where data and app tiers start concurrently), we retry up to
    # 5 times with a 2-second delay before giving up.
    import asyncio as _asyncio

    from .db.engine import close_db, init_db

    _engine = None
    _pg_url = settings.build_postgres_url()
    for _attempt in range(1, 6):
        try:
            from sqlalchemy import text as _sql_text

            _engine = await init_db(
                url=_pg_url,
                pool_size=settings.postgres.pool_size,
                max_overflow=settings.postgres.max_overflow,
                echo=settings.database.echo_sql,
            )
            async with _engine.connect() as _probe_conn:
                await _probe_conn.execute(_sql_text("SELECT 1"))
            logger.info("PostgreSQL connected (attempt %d/%d)", _attempt, 5)
            break
        except Exception as exc:
            logger.warning(
                "PostgreSQL unavailable (attempt %d/5): %s — retrying in 2s",
                _attempt,
                exc,
            )
            _engine = None
            if _attempt < 5:
                await _asyncio.sleep(2)
    app.state.db_engine = _engine
    if _engine is None:
        logger.warning("PostgreSQL permanently unavailable — DB-backed services disabled")

    # ---- Initialize singleton services with DB engine ----
    if app.state.db_engine:
        try:
            from .audit.service import AuditLogService
            from .files.service import FileStorageService
            from .todos.service import TODOService

            TODOService.get_instance(engine=app.state.db_engine)
            AuditLogService.get_instance(engine=app.state.db_engine)
            FileStorageService.get_instance(engine=app.state.db_engine)

            from .auth.user_service import UserService

            UserService.init(app.state.db_engine)
            logger.info("Singleton services initialized: TODOService, AuditLogService, FileStorageService, UserService")
        except Exception as exc:
            logger.error("Failed to initialize singleton services: %s — DB-backed endpoints will return 503", exc)
            app.state.db_engine = None  # mark as unusable so routers return 503

    # ---- Redis ----
    from .db.redis import close_redis, init_redis

    redis_client = await init_redis(url=settings.build_redis_url())
    app.state.redis = redis_client

    # ---- Wire Redis into Chat Manager ----
    from .chat.manager import manager as chat_manager

    if redis_client:
        from .chat.redis_store import RedisChatStore

        chat_manager._redis_store = RedisChatStore(redis_client)
        logger.info("Chat Redis store: enabled (TTL=6h)")

    # ---- Chat Persistence (write-through micro-batch to Postgres) ----
    chat_persistence = None
    if app.state.db_engine:
        from .chat.persistence import ChatPersistenceService

        chat_persistence = ChatPersistenceService(app.state.db_engine)
        chat_manager._persistence = chat_persistence
        logger.info("Chat persistence: enabled (micro-batch, batch_size=3)")
    else:
        logger.info("Chat persistence: disabled (no database)")
    app.state.chat_persistence = chat_persistence

    # ---- Git Workspace ----
    git_service = GitWorkspaceService()
    delegate_broker = DelegateBroker()
    if settings.git_workspace.enabled:
        await git_service.initialize(settings.git_workspace)
        logger.info("Git Workspace module initialized.")
    app.state.git_workspace_service = git_service
    app.state.delegate_broker = delegate_broker

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

        # Explorer provider (sub-agent model)
        explorer_provider = resolver.get_explorer_provider()
        if explorer_provider:
            logger.info("Explorer (sub-agent) provider ready.")
        else:
            logger.info("No explorer model configured — sub-agents will use main provider.")
    except Exception as exc:
        logger.warning("Failed to initialize AI provider resolver: %s", exc)
        explorer_provider = None
    app.state.agent_provider = agent_provider
    app.state.explorer_provider = explorer_provider

    # Auto-detect active explorer model
    active_explorer_id = None
    if explorer_provider is not None:
        _resolver_status = resolver.get_status()
        if _resolver_status:
            for m in _resolver_status.models:
                if m.explorer and m.available:
                    active_explorer_id = m.id
                    break
    app.state.active_explorer_model_id = active_explorer_id

    # ---- Session Trace Writer ----
    from .agent_loop.trace import TraceWriter

    trace_writer = TraceWriter.from_settings(settings.trace, engine=app.state.db_engine)
    app.state.trace_writer = trace_writer
    logger.info(
        "Trace writer: enabled=%s, backend=%s",
        settings.trace.enabled,
        settings.trace.backend,
    )

    # ---- Langfuse Observability ----
    from .workflow.observability import init_langfuse

    langfuse_ok = init_langfuse(settings)
    if langfuse_ok:
        import os

        logger.info(
            "Langfuse observability: enabled (host=%s)", os.environ.get("LANGFUSE_HOST", settings.langfuse.host)
        )
    else:
        logger.info("Langfuse observability: disabled")

    # ---- Ngrok tunnel ----
    # Read ngrok config from raw YAML (not modelled in AppSettings).
    # Required for VS Code Remote-WSL: the webview runs in the Windows
    # Electron process and cannot reach WSL's localhost directly.
    from .config import _load_yaml_with_local

    _settings_raw = _load_yaml_with_local("conductor.settings.yaml")
    _secrets_raw = _load_yaml_with_local("conductor.secrets.yaml")
    ngrok_cfg = _settings_raw.get("ngrok", {})
    ngrok_sec = _secrets_raw.get("ngrok", {})

    if ngrok_cfg.get("enabled", False):
        logger.info("Ngrok is enabled, starting tunnel...")
        ngrok_url = start_ngrok(
            port=settings.server.port,
            authtoken=ngrok_sec.get("authtoken", ""),
            region=ngrok_cfg.get("region", "us"),
            domain=ngrok_cfg.get("domain"),
        )
        if ngrok_url:
            logger.info("Ngrok tunnel active: %s", ngrok_url)
        else:
            logger.warning("Failed to start ngrok tunnel. Falling back to localhost:%s", settings.server.port)
    else:
        logger.info(
            "Ngrok disabled. Server running on http://%s:%s",
            settings.server.host,
            settings.server.port,
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

    # ---- Jira Integration ----
    conductor_cfg = get_config()
    # Phase 7.7.11 — expose the loaded config + jira_project_guide on
    # app.state so the webhook router (which fires outside any user
    # session) can reach the readonly client + secrets without
    # re-loading them per request.
    app.state.conductor_config = conductor_cfg
    try:
        import yaml as _yaml

        from .workflow.loader import _find_config_dir
        _guide_path = _find_config_dir() / "jira_project_guide.yaml"
        if _guide_path.exists():
            app.state.jira_project_guide = _yaml.safe_load(_guide_path.read_text("utf-8")) or {}
        else:
            app.state.jira_project_guide = {}
    except Exception as _exc:
        logger.warning("Failed to load jira_project_guide.yaml: %s", _exc)
        app.state.jira_project_guide = {}

    if conductor_cfg.jira.enabled and conductor_cfg.jira_secrets.client_id:
        from .integrations.jira.models import JiraFieldOption
        from .integrations.jira.service import JiraOAuthService

        # Build redirect_uri from public URL or localhost
        public_url = get_public_url() or settings.server.public_url or f"http://localhost:{settings.server.port}"
        redirect_uri = f"{public_url}/api/integrations/jira/callback"
        static_teams = [JiraFieldOption(id=t.id, name=t.name) for t in conductor_cfg.jira.teams] or None
        jira_service = JiraOAuthService(
            client_id=conductor_cfg.jira_secrets.client_id,
            client_secret=conductor_cfg.jira_secrets.client_secret,
            redirect_uri=redirect_uri,
            static_teams=static_teams,
        )
        if static_teams:
            logger.info("Jira integration: loaded %d static teams from config", len(static_teams))
        app.state.jira_service = jira_service
        app.state.jira_allowed_projects = set(k.upper() for k in conductor_cfg.jira.allowed_projects)
        if app.state.jira_allowed_projects:
            logger.info("Jira integration: project filter = %s", app.state.jira_allowed_projects)
        # Initialize Jira tools for agent loop
        from .integrations.jira.tools import init_jira_tools

        init_jira_tools(jira_service, app.state.jira_allowed_projects)
        logger.info("Jira integration: enabled (redirect=%s)", redirect_uri)
    else:
        app.state.jira_service = None
        app.state.jira_allowed_projects = set()
        logger.info("Jira integration: disabled")

    # Atlassian readonly clients (Basic auth, shared account-level token) —
    # one classic API token drives both Jira and Confluence. Independent of 3LO.
    atl_ro = conductor_cfg.atlassian_readonly
    if atl_ro.site_url and atl_ro.email and atl_ro.api_token:
        from .integrations.confluence.readonly_client import ConfluenceReadonlyClient
        from .integrations.jira.readonly_client import JiraReadonlyClient

        app.state.jira_readonly_client = JiraReadonlyClient(
            site_url=atl_ro.site_url, email=atl_ro.email, api_token=atl_ro.api_token,
        )
        app.state.confluence_readonly_client = ConfluenceReadonlyClient(
            site_url=atl_ro.site_url, email=atl_ro.email, api_token=atl_ro.api_token,
        )
        logger.info("Atlassian readonly clients: enabled (site=%s, email=%s)", atl_ro.site_url, atl_ro.email)
    else:
        app.state.jira_readonly_client = None
        app.state.confluence_readonly_client = None
        logger.info("Atlassian readonly clients: disabled")

    # ---- Azure DevOps ----
    if conductor_cfg.azure_devops.enabled and conductor_cfg.azure_devops_secrets.pat:
        from .integrations.azure_devops.mcp_client import AzureDevOpsClient
        from .integrations.azure_devops.workspace import ensure_workspace

        app.state.azure_devops_client = AzureDevOpsClient(
            org_url=conductor_cfg.azure_devops_secrets.org_url,
            pat=conductor_cfg.azure_devops_secrets.pat,
        )

        # Auto-clone or fetch the repo so code review has a local workspace
        ado_workspace = await ensure_workspace(
            repo_url=conductor_cfg.azure_devops.repo_url,
            pat=conductor_cfg.azure_devops_secrets.pat,
            workspace_path=conductor_cfg.azure_devops.workspace_path,
        )
        app.state.azure_devops_workspace = ado_workspace

        # Initialize PRBrainOrchestrator factory for Azure DevOps PR reviews
        if agent_provider and ado_workspace:
            from .agent_loop.pr_brain import PRBrainOrchestrator
            from .code_tools.executor import LocalToolExecutor
            from .workflow.loader import load_agent_registry, load_pr_brain_config

            _pr_brain_config = load_pr_brain_config()
            _agent_registry = load_agent_registry()

            def _make_pr_brain(
                workspace: str,
                diff_spec: str,
                task_id: Optional[str] = None,
                pr_title: str = "",
                pr_description: str = "",
                ticket_context: str = "",
            ) -> PRBrainOrchestrator:
                return PRBrainOrchestrator(
                    provider=agent_provider,
                    explorer_provider=explorer_provider or agent_provider,
                    workspace_path=workspace,
                    diff_spec=diff_spec,
                    pr_brain_config=_pr_brain_config,
                    agent_registry=_agent_registry,
                    tool_executor=LocalToolExecutor(workspace),
                    trace_writer=trace_writer,
                    task_id=task_id,
                    pr_title=pr_title,
                    pr_description=pr_description,
                    ticket_context=ticket_context,
                )

            app.state.pr_brain_factory = _make_pr_brain
            # Phase 7.8.5 — expose the strong provider alongside the
            # factory so the ADO router can reuse it for the split-plan
            # helper (generate_pr_split_plan). Kept as a separate attr
            # rather than smuggled onto the closure so future callers
            # can pick it up without instantiating a brain.
            app.state.pr_brain_strong_provider = agent_provider
            logger.info("Azure DevOps PR Brain: ready")

        logger.info(
            "Azure DevOps integration: enabled (org=%s, workspace=%s)",
            conductor_cfg.azure_devops_secrets.org_url,
            ado_workspace,
        )
    else:
        app.state.azure_devops_client = None
        app.state.azure_devops_workspace = None
        logger.info("Azure DevOps integration: disabled")

    # Sweep orphan scratchpad sessions (older than 24h). Keeps
    # ~/.conductor/scratchpad/ from growing unbounded across dev runs
    # and crashed processes whose `cleanup()` path never fired.
    try:
        from .scratchpad import sweep_orphans

        sweep_orphans(max_age_hours=24)
    except Exception as exc:
        logger.warning("Scratchpad orphan sweep failed on startup: %s", exc)

    logger.info("Conducator startup complete.")
    yield
    # ---- Shutdown ----
    stop_ngrok()
    from .workflow.observability import flush as langfuse_flush

    langfuse_flush()
    # Flush chat persistence buffers before shutdown
    if chat_persistence:
        await chat_persistence.flush_all()
    # Shut down browser service if it was used
    try:
        from .browser.service import shutdown_browser_service

        shutdown_browser_service()
    except ImportError:
        pass
    await git_service.shutdown()
    await close_redis()
    await close_db()
    logger.info("Conducator shutdown complete.")


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------


def create_app(settings: AppSettings | None = None) -> FastAPI:
    """Application factory."""
    app = FastAPI(
        title="Conducator",
        description="Real-time collaborative coding backend",
        version="2.0.0",
        lifespan=lifespan,
    )

    # --- CORS ---
    _s = settings or load_settings()
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_s.server.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        # vscode-webview://<id> origins are not matchable as literal strings;
        # use a regex so any webview origin is accepted.
        allow_origin_regex=r"vscode-webview://.*",
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
                    await send(
                        {
                            "type": "http.response.start",
                            "status": 204,
                            "headers": [
                                (b"access-control-allow-origin", origin.encode()),
                                (b"access-control-allow-private-network", b"true"),
                                (b"access-control-allow-methods", b"*"),
                                (b"access-control-allow-headers", b"*"),
                                (b"access-control-max-age", b"7200"),
                            ],
                        }
                    )
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
    from .agent.router import router as agent_router
    from .agent_loop.router import router as agent_loop_router
    from .ai_provider.router import router as ai_provider_router
    from .audit.router import router as audit_router
    from .auth.router import router as auth_router
    from .chat.router import router as chat_router
    from .chat.settings_router import router as chat_settings_router
    from .code_tools.router import router as code_tools_router
    from .files.router import router as files_router
    from .git_workspace.router import router as git_workspace_router
    from .integrations.azure_devops.router import router as azure_devops_router
    from .integrations.confluence.router import router as confluence_router
    from .integrations.jira.router import router as jira_router
    from .integrations.jira.webhook_router import router as jira_webhook_router
    from .integrations.teams.router import router as teams_router
    from .langextract.router import router as langextract_router
    from .policy.router import router as policy_router
    from .todos.router import router as todos_router
    from .workflow.router import brain_router
    from .workspace_files.router import router as workspace_files_router

    # Browser tools (optional — only available when playwright is installed)
    _browser_router = None
    with suppress(ImportError):
        from .browser.router import router as _browser_router

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
    app.include_router(brain_router)
    app.include_router(jira_router)
    app.include_router(jira_webhook_router)
    app.include_router(confluence_router)
    app.include_router(azure_devops_router)
    app.include_router(teams_router)
    if _browser_router is not None:
        app.include_router(_browser_router)
        logger.info("Browser tools: enabled (Playwright)")
    else:
        logger.info("Browser tools: disabled (playwright not installed)")

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
