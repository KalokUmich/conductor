"""Langfuse observability integration for workflow engine.

Provides a thin wrapper around the Langfuse SDK that:
  - Initializes from Conductor config (settings + secrets YAML)
  - Provides @observe decorator (or no-op when disabled)
  - Gracefully degrades if Langfuse is unavailable or not installed

Usage:
    from app.workflow.observability import init_langfuse, observe

    init_langfuse(settings)  # call once at startup

    @observe(name="my_operation")
    async def my_function(): ...
"""
from __future__ import annotations

import logging
from functools import wraps
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

_langfuse_enabled = False
_langfuse_initialized = False

# Try to import langfuse SDK.
# v2: langfuse.decorators.observe + langfuse_context
# v4: langfuse.observe (top-level)
_langfuse_context = None
_get_langfuse_client = None
try:
    from langfuse.decorators import langfuse_context as _langfuse_context
    from langfuse.decorators import observe as _langfuse_observe
    _LANGFUSE_AVAILABLE = True
except ImportError:
    try:
        from langfuse import observe as _langfuse_observe, get_client as _get_langfuse_client
        _LANGFUSE_AVAILABLE = True
    except ImportError:
        _LANGFUSE_AVAILABLE = False
        _langfuse_observe = None


def init_langfuse(settings=None) -> bool:
    """Initialize Langfuse from Conductor settings.

    Call once at app startup (e.g. in main.py lifespan).
    Returns True if Langfuse was successfully initialized.
    """
    global _langfuse_enabled, _langfuse_initialized

    if _langfuse_initialized:
        return _langfuse_enabled

    if settings is None:
        _langfuse_initialized = True
        return False

    langfuse_settings = getattr(settings, "langfuse", None)
    if not langfuse_settings or not langfuse_settings.enabled:
        logger.info("Langfuse: disabled in settings")
        _langfuse_initialized = True
        return False

    if not _LANGFUSE_AVAILABLE:
        logger.warning("Langfuse: enabled in settings but langfuse package not installed")
        _langfuse_initialized = True
        return False

    # Get API keys from secrets
    secrets = getattr(settings, "secrets", None)
    langfuse_secrets = getattr(secrets, "langfuse", None) if secrets else None

    public_key = getattr(langfuse_secrets, "public_key", "") if langfuse_secrets else ""
    secret_key = getattr(langfuse_secrets, "secret_key", "") if langfuse_secrets else ""

    if not public_key or not secret_key:
        logger.warning(
            "Langfuse: enabled but missing API keys in conductor.secrets.yaml. "
            "Set langfuse.public_key and langfuse.secret_key."
        )
        _langfuse_initialized = True
        return False

    # Configure Langfuse via environment (the SDK reads these)
    import os
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", secret_key)
    os.environ.setdefault("LANGFUSE_HOST", langfuse_settings.host)

    _langfuse_enabled = True
    _langfuse_initialized = True
    logger.info("Langfuse: initialized (host=%s)", langfuse_settings.host)
    return True


def observe(
    name: Optional[str] = None,
    as_type: Optional[str] = None,
    **kwargs,
) -> Callable:
    """Decorator for Langfuse tracing.

    Wraps the function so that Langfuse tracing is checked at **call time**,
    not at decoration time. This is critical because @observe decorators are
    evaluated at module import, before init_langfuse() runs during lifespan.

    Args:
        name: Span name (e.g. "workflow:pr-review", "agent:security").
        as_type: Span type (e.g. "generation" for LLM calls).
        **kwargs: Additional kwargs passed to langfuse @observe.
    """
    observe_kwargs = {k: v for k, v in kwargs.items()}
    if name is not None:
        observe_kwargs["name"] = name
    if as_type is not None:
        observe_kwargs["as_type"] = as_type

    def decorator(fn: Callable) -> Callable:
        _wrapped = None  # lazily created on first call

        @wraps(fn)
        async def async_wrapper(*args, **kw):
            nonlocal _wrapped
            if _langfuse_enabled and _LANGFUSE_AVAILABLE and _langfuse_observe is not None:
                if _wrapped is None:
                    _wrapped = _langfuse_observe(**observe_kwargs)(fn)
                return await _wrapped(*args, **kw)
            return await fn(*args, **kw)

        @wraps(fn)
        def sync_wrapper(*args, **kw):
            nonlocal _wrapped
            if _langfuse_enabled and _LANGFUSE_AVAILABLE and _langfuse_observe is not None:
                if _wrapped is None:
                    _wrapped = _langfuse_observe(**observe_kwargs)(fn)
                return _wrapped(*args, **kw)
            return fn(*args, **kw)

        import asyncio
        if asyncio.iscoroutinefunction(fn):
            return async_wrapper
        return sync_wrapper
    return decorator


def update_trace(
    metadata: Optional[dict] = None,
    tags: Optional[list] = None,
    **kwargs,
) -> None:
    """Update the current Langfuse trace with metadata/tags.

    No-op when Langfuse is disabled.
    """
    if not _langfuse_enabled or not _LANGFUSE_AVAILABLE:
        return
    try:
        if _langfuse_context is not None:
            _langfuse_context.update_current_trace(metadata=metadata, tags=tags, **kwargs)
    except Exception:
        pass  # never fail the main code path


def flush() -> None:
    """Flush any pending Langfuse events. Call on shutdown."""
    if not _langfuse_enabled or not _LANGFUSE_AVAILABLE:
        return
    try:
        if _langfuse_context is not None:
            _langfuse_context.flush()
        elif _get_langfuse_client is not None:
            _get_langfuse_client().flush()
    except Exception:
        pass
