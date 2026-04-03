"""Browser tool implementations using Playwright.

Each tool accepts a ``workspace`` parameter (used as the browser session key)
plus tool-specific parameters, and returns a ``ToolResult``.
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Any, Dict, List, Optional

from ..code_tools.schemas import ToolResult
from .service import get_browser_service

logger = logging.getLogger(__name__)

_MAX_TEXT_CHARS = 30_000  # truncate page text to stay within token budget
_MAX_LINKS = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _page_snapshot(page) -> Dict[str, Any]:
    """Return a lightweight summary of the current page state."""
    title = page.title()
    url = page.url
    text = page.inner_text("body") or ""
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + f"\n… (truncated, {len(text)} chars total)"
    return {"url": url, "title": title, "text": text}


def _extract_links(page, max_links: int = _MAX_LINKS) -> List[Dict[str, str]]:
    """Extract visible links from the page."""
    links = page.eval_on_selector_all(
        "a[href]",
        f"""els => els.slice(0, {max_links}).map(a => ({{
            text: (a.innerText || '').trim().substring(0, 120),
            href: a.href
        }}))""",
    )
    return links


def _validate_url(url: str) -> str:
    """Basic URL validation."""
    url = url.strip()
    if not re.match(r"^https?://", url, re.IGNORECASE):
        raise ValueError(f"URL must start with http:// or https://, got: {url!r}")
    return url


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------


def web_search(
    workspace: str,
    query: str,
    max_results: int = 10,
) -> ToolResult:
    """Search Google and return structured results."""
    if not query or not query.strip():
        return ToolResult(tool_name="web_search", success=False, error="Empty search query")

    try:
        service = get_browser_service()
        page = service.get_page(session_id=workspace)

        # Navigate to Google search
        encoded_q = query.strip().replace(" ", "+")
        page.goto(
            f"https://www.google.com/search?q={encoded_q}&hl=en",
            wait_until="domcontentloaded",
        )

        # Extract search results from Google's result page.
        # Each organic result lives in a div with class 'g' (or similar).
        results = page.eval_on_selector_all(
            "div.g",
            """els => els.map(el => {
                const a = el.querySelector('a[href]');
                const h3 = el.querySelector('h3');
                // Snippet lives in various containers
                const snippet = el.querySelector('[data-sncf], .VwiC3b, .IsZvec, .s3v9rd')
                    || el.querySelector('div[style*="-webkit-line-clamp"]')
                    || el.querySelector('span.st');
                return {
                    title: h3 ? h3.innerText.trim() : '',
                    url: a ? a.href : '',
                    snippet: snippet ? snippet.innerText.trim() : ''
                };
            }).filter(r => r.title && r.url && !r.url.startsWith('https://www.google.com'))""",
        )

        results = results[:max_results]

        # If Google's layout changed and we got nothing, fall back to
        # extracting any visible links with headings.
        if not results:
            results = page.eval_on_selector_all(
                "a[href] h3",
                f"""els => els.slice(0, {max_results}).map(h3 => {{
                    const a = h3.closest('a');
                    const parent = a ? a.closest('div') : null;
                    const snippet = parent
                        ? (parent.querySelector('span') || {{}}).innerText || ''
                        : '';
                    return {{
                        title: h3.innerText.trim(),
                        url: a ? a.href : '',
                        snippet: snippet.trim().substring(0, 300)
                    }};
                }}).filter(r => r.url && !r.url.startsWith('https://www.google.com'))""",
            )

        truncated = len(results) >= max_results
        return ToolResult(
            tool_name="web_search",
            data={
                "query": query.strip(),
                "results": results,
                "count": len(results),
            },
            truncated=truncated,
        )
    except Exception as exc:
        return ToolResult(tool_name="web_search", success=False, error=str(exc))


def web_navigate(
    workspace: str,
    url: str,
    wait_until: str = "domcontentloaded",
) -> ToolResult:
    """Navigate the browser to *url* and return page content."""
    try:
        url = _validate_url(url)
    except ValueError as e:
        return ToolResult(tool_name="web_navigate", success=False, error=str(e))

    if wait_until not in ("load", "domcontentloaded", "networkidle"):
        wait_until = "domcontentloaded"

    try:
        service = get_browser_service()
        page = service.get_page(session_id=workspace)
        page.goto(url, wait_until=wait_until)

        snapshot = _page_snapshot(page)
        links = _extract_links(page)
        snapshot["links"] = links
        truncated = len(snapshot["text"]) >= _MAX_TEXT_CHARS

        return ToolResult(
            tool_name="web_navigate",
            data=snapshot,
            truncated=truncated,
        )
    except Exception as exc:
        return ToolResult(tool_name="web_navigate", success=False, error=str(exc))


def web_click(
    workspace: str,
    selector: Optional[str] = None,
    text: Optional[str] = None,
) -> ToolResult:
    """Click an element on the current page."""
    if not selector and not text:
        return ToolResult(
            tool_name="web_click",
            success=False,
            error="Provide either 'selector' or 'text' to identify the element.",
        )

    try:
        service = get_browser_service()
        page = service.get_page(session_id=workspace)

        if selector:
            page.click(selector, timeout=10_000)
        else:
            page.get_by_text(text, exact=False).first.click(timeout=10_000)

        # Wait briefly for any navigation or DOM update
        page.wait_for_load_state("domcontentloaded", timeout=5_000)

        snapshot = _page_snapshot(page)
        return ToolResult(tool_name="web_click", data=snapshot)
    except Exception as exc:
        return ToolResult(tool_name="web_click", success=False, error=str(exc))


def web_fill(
    workspace: str,
    selector: str,
    value: str,
    press_enter: bool = False,
) -> ToolResult:
    """Fill a form input field."""
    try:
        service = get_browser_service()
        page = service.get_page(session_id=workspace)

        page.fill(selector, value, timeout=10_000)
        if press_enter:
            page.press(selector, "Enter")
            page.wait_for_load_state("domcontentloaded", timeout=5_000)

        snapshot = _page_snapshot(page)
        return ToolResult(tool_name="web_fill", data=snapshot)
    except Exception as exc:
        return ToolResult(tool_name="web_fill", success=False, error=str(exc))


def web_screenshot(
    workspace: str,
    selector: Optional[str] = None,
    full_page: bool = True,
) -> ToolResult:
    """Take a screenshot of the page or a specific element."""
    try:
        service = get_browser_service()
        page = service.get_page(session_id=workspace)

        # Save to a temp file
        fd, path = tempfile.mkstemp(suffix=".png", prefix="conductor_screenshot_")
        os.close(fd)

        if selector:
            element = page.query_selector(selector)
            if element is None:
                return ToolResult(
                    tool_name="web_screenshot",
                    success=False,
                    error=f"Element not found: {selector}",
                )
            element.screenshot(path=path)
        else:
            page.screenshot(path=path, full_page=full_page)

        return ToolResult(
            tool_name="web_screenshot",
            data={
                "path": path,
                "url": page.url,
                "title": page.title(),
            },
        )
    except Exception as exc:
        return ToolResult(tool_name="web_screenshot", success=False, error=str(exc))


def web_extract(
    workspace: str,
    selector: str,
    attribute: Optional[str] = None,
    max_results: int = 20,
) -> ToolResult:
    """Extract text or attribute values from elements matching *selector*."""
    try:
        service = get_browser_service()
        page = service.get_page(session_id=workspace)

        elements = page.query_selector_all(selector)
        if not elements:
            return ToolResult(
                tool_name="web_extract",
                data={"matches": [], "count": 0},
            )

        matches = []
        for el in elements[:max_results]:
            if attribute:
                val = el.get_attribute(attribute)
            else:
                val = el.inner_text()
            if val is not None:
                # Truncate individual values
                if len(val) > 2000:
                    val = val[:2000] + "…"
                matches.append(val)

        truncated = len(elements) > max_results
        return ToolResult(
            tool_name="web_extract",
            data={
                "matches": matches,
                "count": len(elements),
                "selector": selector,
            },
            truncated=truncated,
        )
    except Exception as exc:
        return ToolResult(tool_name="web_extract", success=False, error=str(exc))


# ---------------------------------------------------------------------------
# Registry (imported by code_tools.tools)
# ---------------------------------------------------------------------------

BROWSER_TOOL_REGISTRY = {
    "web_search": web_search,
    "web_navigate": web_navigate,
    "web_click": web_click,
    "web_fill": web_fill,
    "web_screenshot": web_screenshot,
    "web_extract": web_extract,
}
