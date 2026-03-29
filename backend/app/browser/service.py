"""Browser session management using Playwright.

Provides a singleton BrowserService that lazily launches a headless Chromium
browser and maintains one BrowserContext (with a Page) per session.  Sessions
are keyed by an opaque string — typically the workspace path so that all
browser tool calls within a single agent-loop run share the same page.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

logger = logging.getLogger(__name__)

_DEFAULT_VIEWPORT = {"width": 1280, "height": 720}
_PAGE_TIMEOUT_MS = 30_000  # 30 s default navigation timeout


class BrowserService:
    """Manages a shared headless Chromium browser and per-session pages."""

    def __init__(self, headless: bool = True) -> None:
        self._headless = headless
        self._pw = None
        self._browser: Optional[Browser] = None
        self._sessions: dict[str, BrowserContext] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def _ensure_browser(self) -> None:
        """Lazily start Playwright and launch Chromium (must hold _lock)."""
        if self._browser is None:
            logger.info("Launching headless Chromium …")
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self._headless)
            logger.info("Chromium ready (pid=%s)", self._browser.contexts)

    def get_page(self, session_id: str) -> Page:
        """Return the active page for *session_id*, creating one if needed."""
        with self._lock:
            self._ensure_browser()
            if session_id not in self._sessions:
                ctx = self._browser.new_context(viewport=_DEFAULT_VIEWPORT)
                self._sessions[session_id] = ctx
                logger.info("New browser session: %s", session_id)
            ctx = self._sessions[session_id]
        # Outside lock — page operations are thread-safe per context
        pages = ctx.pages
        if not pages:
            page = ctx.new_page()
            page.set_default_timeout(_PAGE_TIMEOUT_MS)
            return page
        return pages[-1]

    def close_session(self, session_id: str) -> None:
        """Close and discard the browser context for *session_id*."""
        with self._lock:
            ctx = self._sessions.pop(session_id, None)
        if ctx:
            try:
                ctx.close()
            except Exception as exc:  # TODO: narrow to playwright.sync_api.Error
                logger.debug("Error closing browser session %s: %s", session_id, exc)
            logger.info("Closed browser session: %s", session_id)

    def list_sessions(self) -> list[str]:
        """Return active session IDs."""
        with self._lock:
            return list(self._sessions)

    def shutdown(self) -> None:
        """Close all sessions, the browser, and Playwright."""
        with self._lock:
            for sid, ctx in self._sessions.items():
                try:
                    ctx.close()
                except Exception as exc:  # TODO: narrow to playwright.sync_api.Error
                    logger.debug("Error closing session %s during shutdown: %s", sid, exc)
            self._sessions.clear()
            if self._browser:
                try:
                    self._browser.close()
                except Exception as exc:  # TODO: narrow to playwright.sync_api.Error
                    logger.debug("Error closing browser during shutdown: %s", exc)
                self._browser = None
            if self._pw:
                try:
                    self._pw.stop()
                except Exception as exc:  # TODO: narrow to playwright.sync_api.Error
                    logger.debug("Error stopping Playwright during shutdown: %s", exc)
                self._pw = None
        logger.info("BrowserService shut down")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_instance: Optional[BrowserService] = None
_instance_lock = threading.Lock()


def get_browser_service() -> BrowserService:
    """Return (or create) the module-level BrowserService singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = BrowserService()
    return _instance


def shutdown_browser_service() -> None:
    """Shut down the singleton if it exists."""
    global _instance
    with _instance_lock:
        if _instance is not None:
            _instance.shutdown()
            _instance = None
