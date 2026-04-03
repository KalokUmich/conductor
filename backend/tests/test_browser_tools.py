"""Tests for browser automation tools (Playwright).

Uses mocked Playwright objects to avoid needing a real browser.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from app.code_tools.schemas import (
    TOOL_DEFINITIONS,
    TOOL_PARAM_MODELS,
    ToolResult,
    WebClickParams,
    WebExtractParams,
    WebFillParams,
    WebNavigateParams,
    WebScreenshotParams,
    WebSearchParams,
)


# ---------------------------------------------------------------------------
# Schema tests
# ---------------------------------------------------------------------------


class TestBrowserSchemas:
    """Verify browser tool schemas are registered."""

    BROWSER_TOOLS = [
        "web_search",
        "web_navigate",
        "web_click",
        "web_fill",
        "web_screenshot",
        "web_extract",
    ]

    def test_all_browser_tools_in_definitions(self):
        names = {t["name"] for t in TOOL_DEFINITIONS}
        for tool in self.BROWSER_TOOLS:
            assert tool in names, f"{tool} missing from TOOL_DEFINITIONS"

    def test_all_browser_tools_in_param_models(self):
        for tool in self.BROWSER_TOOLS:
            assert tool in TOOL_PARAM_MODELS, f"{tool} missing from TOOL_PARAM_MODELS"

    def test_web_search_params_validation(self):
        p = WebSearchParams(query="playwright timeout")
        assert p.query == "playwright timeout"
        assert p.max_results == 10

    def test_web_navigate_params_validation(self):
        p = WebNavigateParams(url="https://example.com")
        assert p.url == "https://example.com"
        assert p.wait_until == "domcontentloaded"

    def test_web_click_params_validation(self):
        p = WebClickParams(selector="button.submit")
        assert p.selector == "button.submit"
        assert p.text is None

    def test_web_fill_params_validation(self):
        p = WebFillParams(selector="input#email", value="test@example.com")
        assert p.value == "test@example.com"
        assert p.press_enter is False

    def test_web_screenshot_params_defaults(self):
        p = WebScreenshotParams()
        assert p.selector is None
        assert p.full_page is True

    def test_web_extract_params_validation(self):
        p = WebExtractParams(selector="table tr")
        assert p.max_results == 20
        assert p.attribute is None


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------


def _make_mock_page(url="https://example.com", title="Example", text="Hello World"):
    """Create a mock Playwright Page."""
    page = MagicMock()
    page.url = url
    page.title.return_value = title
    page.inner_text.return_value = text
    page.eval_on_selector_all.return_value = [
        {"text": "Link 1", "href": "https://example.com/1"},
    ]
    page.set_default_timeout = MagicMock()
    return page


def _make_mock_service(page):
    """Create a mock BrowserService that returns the given page."""
    service = MagicMock()
    service.get_page.return_value = page
    return service


# ---------------------------------------------------------------------------
# Tool implementation tests
# ---------------------------------------------------------------------------


class TestWebSearch:
    """Tests for web_search tool."""

    @patch("app.browser.tools.get_browser_service")
    def test_search_success(self, mock_get_service):
        from app.browser.tools import web_search

        page = _make_mock_page()
        page.eval_on_selector_all.return_value = [
            {"title": "Playwright Docs", "url": "https://playwright.dev", "snippet": "Fast browser automation"},
            {"title": "Stack Overflow", "url": "https://stackoverflow.com/q/123", "snippet": "Timeout fix"},
        ]
        mock_get_service.return_value = _make_mock_service(page)

        result = web_search(workspace="/tmp/ws", query="playwright timeout")

        assert result.success is True
        assert result.tool_name == "web_search"
        assert result.data["query"] == "playwright timeout"
        assert len(result.data["results"]) == 2
        assert result.data["results"][0]["title"] == "Playwright Docs"
        page.goto.assert_called_once()
        assert "google.com/search" in page.goto.call_args[0][0]

    def test_search_empty_query(self):
        from app.browser.tools import web_search

        result = web_search(workspace="/tmp/ws", query="")
        assert result.success is False
        assert "Empty" in result.error

    @patch("app.browser.tools.get_browser_service")
    def test_search_no_results_fallback(self, mock_get_service):
        from app.browser.tools import web_search

        page = _make_mock_page()
        # First call (div.g) returns empty, second call (a[href] h3) also empty
        page.eval_on_selector_all.return_value = []
        mock_get_service.return_value = _make_mock_service(page)

        result = web_search(workspace="/tmp/ws", query="very obscure query")

        assert result.success is True
        assert result.data["count"] == 0

    @patch("app.browser.tools.get_browser_service")
    def test_search_respects_max_results(self, mock_get_service):
        from app.browser.tools import web_search

        page = _make_mock_page()
        page.eval_on_selector_all.return_value = [
            {"title": f"Result {i}", "url": f"https://example.com/{i}", "snippet": f"Snippet {i}"}
            for i in range(15)
        ]
        mock_get_service.return_value = _make_mock_service(page)

        result = web_search(workspace="/tmp/ws", query="test", max_results=5)

        assert result.success is True
        assert len(result.data["results"]) == 5
        assert result.truncated is True

    @patch("app.browser.tools.get_browser_service")
    def test_search_handles_exception(self, mock_get_service):
        from app.browser.tools import web_search

        service = _make_mock_service(_make_mock_page())
        service.get_page.side_effect = RuntimeError("Browser crashed")
        mock_get_service.return_value = service

        result = web_search(workspace="/tmp/ws", query="test")
        assert result.success is False
        assert "Browser crashed" in result.error



class TestWebNavigate:
    """Tests for web_navigate tool."""

    @patch("app.browser.tools.get_browser_service")
    def test_navigate_success(self, mock_get_service):
        from app.browser.tools import web_navigate

        page = _make_mock_page()
        mock_get_service.return_value = _make_mock_service(page)

        result = web_navigate(workspace="/tmp/ws", url="https://example.com")

        assert result.success is True
        assert result.tool_name == "web_navigate"
        assert result.data["url"] == "https://example.com"
        assert result.data["title"] == "Example"
        assert "Hello World" in result.data["text"]
        assert len(result.data["links"]) == 1
        page.goto.assert_called_once_with("https://example.com", wait_until="domcontentloaded")

    def test_navigate_invalid_url(self):
        from app.browser.tools import web_navigate

        result = web_navigate(workspace="/tmp/ws", url="ftp://bad.example.com")
        assert result.success is False
        assert "http://" in result.error

    @patch("app.browser.tools.get_browser_service")
    def test_navigate_truncates_long_text(self, mock_get_service):
        from app.browser.tools import web_navigate

        long_text = "x" * 50_000
        page = _make_mock_page(text=long_text)
        mock_get_service.return_value = _make_mock_service(page)

        result = web_navigate(workspace="/tmp/ws", url="https://example.com")
        assert result.success is True
        assert result.truncated is True
        assert len(result.data["text"]) < 50_000

    @patch("app.browser.tools.get_browser_service")
    def test_navigate_handles_exception(self, mock_get_service):
        from app.browser.tools import web_navigate

        service = _make_mock_service(_make_mock_page())
        service.get_page.side_effect = RuntimeError("Browser crashed")
        mock_get_service.return_value = service

        result = web_navigate(workspace="/tmp/ws", url="https://example.com")
        assert result.success is False
        assert "Browser crashed" in result.error


class TestWebClick:
    """Tests for web_click tool."""

    @patch("app.browser.tools.get_browser_service")
    def test_click_by_selector(self, mock_get_service):
        from app.browser.tools import web_click

        page = _make_mock_page()
        mock_get_service.return_value = _make_mock_service(page)

        result = web_click(workspace="/tmp/ws", selector="button.submit")

        assert result.success is True
        page.click.assert_called_once_with("button.submit", timeout=10_000)

    @patch("app.browser.tools.get_browser_service")
    def test_click_by_text(self, mock_get_service):
        from app.browser.tools import web_click

        page = _make_mock_page()
        mock_get_service.return_value = _make_mock_service(page)

        result = web_click(workspace="/tmp/ws", text="Sign In")

        assert result.success is True
        page.get_by_text.assert_called_once_with("Sign In", exact=False)

    def test_click_no_target(self):
        from app.browser.tools import web_click

        result = web_click(workspace="/tmp/ws")
        assert result.success is False
        assert "selector" in result.error


class TestWebFill:
    """Tests for web_fill tool."""

    @patch("app.browser.tools.get_browser_service")
    def test_fill_basic(self, mock_get_service):
        from app.browser.tools import web_fill

        page = _make_mock_page()
        mock_get_service.return_value = _make_mock_service(page)

        result = web_fill(workspace="/tmp/ws", selector="input#email", value="a@b.com")

        assert result.success is True
        page.fill.assert_called_once_with("input#email", "a@b.com", timeout=10_000)
        page.press.assert_not_called()

    @patch("app.browser.tools.get_browser_service")
    def test_fill_with_enter(self, mock_get_service):
        from app.browser.tools import web_fill

        page = _make_mock_page()
        mock_get_service.return_value = _make_mock_service(page)

        result = web_fill(
            workspace="/tmp/ws",
            selector="input#search",
            value="query",
            press_enter=True,
        )

        assert result.success is True
        page.press.assert_called_once_with("input#search", "Enter")


class TestWebScreenshot:
    """Tests for web_screenshot tool."""

    @patch("app.browser.tools.get_browser_service")
    def test_screenshot_full_page(self, mock_get_service):
        from app.browser.tools import web_screenshot

        page = _make_mock_page()
        mock_get_service.return_value = _make_mock_service(page)

        result = web_screenshot(workspace="/tmp/ws")

        assert result.success is True
        assert result.data["path"].endswith(".png")
        page.screenshot.assert_called_once()

    @patch("app.browser.tools.get_browser_service")
    def test_screenshot_element(self, mock_get_service):
        from app.browser.tools import web_screenshot

        page = _make_mock_page()
        element = MagicMock()
        page.query_selector.return_value = element
        mock_get_service.return_value = _make_mock_service(page)

        result = web_screenshot(workspace="/tmp/ws", selector="div.chart")

        assert result.success is True
        element.screenshot.assert_called_once()

    @patch("app.browser.tools.get_browser_service")
    def test_screenshot_element_not_found(self, mock_get_service):
        from app.browser.tools import web_screenshot

        page = _make_mock_page()
        page.query_selector.return_value = None
        mock_get_service.return_value = _make_mock_service(page)

        result = web_screenshot(workspace="/tmp/ws", selector="div.missing")

        assert result.success is False
        assert "not found" in result.error


class TestWebExtract:
    """Tests for web_extract tool."""

    @patch("app.browser.tools.get_browser_service")
    def test_extract_text(self, mock_get_service):
        from app.browser.tools import web_extract

        page = _make_mock_page()
        el1, el2 = MagicMock(), MagicMock()
        el1.inner_text.return_value = "Row 1"
        el2.inner_text.return_value = "Row 2"
        page.query_selector_all.return_value = [el1, el2]
        mock_get_service.return_value = _make_mock_service(page)

        result = web_extract(workspace="/tmp/ws", selector="table tr")

        assert result.success is True
        assert result.data["matches"] == ["Row 1", "Row 2"]
        assert result.data["count"] == 2

    @patch("app.browser.tools.get_browser_service")
    def test_extract_attribute(self, mock_get_service):
        from app.browser.tools import web_extract

        page = _make_mock_page()
        el = MagicMock()
        el.get_attribute.return_value = "https://example.com/img.png"
        page.query_selector_all.return_value = [el]
        mock_get_service.return_value = _make_mock_service(page)

        result = web_extract(workspace="/tmp/ws", selector="img", attribute="src")

        assert result.success is True
        assert result.data["matches"] == ["https://example.com/img.png"]

    @patch("app.browser.tools.get_browser_service")
    def test_extract_no_matches(self, mock_get_service):
        from app.browser.tools import web_extract

        page = _make_mock_page()
        page.query_selector_all.return_value = []
        mock_get_service.return_value = _make_mock_service(page)

        result = web_extract(workspace="/tmp/ws", selector="div.nonexistent")

        assert result.success is True
        assert result.data["matches"] == []
        assert result.data["count"] == 0

    @patch("app.browser.tools.get_browser_service")
    def test_extract_truncation(self, mock_get_service):
        from app.browser.tools import web_extract

        page = _make_mock_page()
        elements = [MagicMock() for _ in range(30)]
        for i, el in enumerate(elements):
            el.inner_text.return_value = f"Item {i}"
        page.query_selector_all.return_value = elements
        mock_get_service.return_value = _make_mock_service(page)

        result = web_extract(workspace="/tmp/ws", selector="li", max_results=5)

        assert result.success is True
        assert len(result.data["matches"]) == 5
        assert result.data["count"] == 30
        assert result.truncated is True


# ---------------------------------------------------------------------------
# Tool registry integration
# ---------------------------------------------------------------------------


class TestBrowserToolRegistry:
    """Verify browser tools are registered in the unified TOOL_REGISTRY."""

    def test_browser_tools_in_registry(self):
        from app.code_tools.tools import TOOL_REGISTRY

        for name in ["web_search", "web_navigate", "web_click", "web_fill", "web_screenshot", "web_extract"]:
            assert name in TOOL_REGISTRY, f"{name} not in TOOL_REGISTRY"

    @patch("app.browser.tools.get_browser_service")
    def test_execute_tool_dispatches_browser_tool(self, mock_get_service):
        from app.code_tools.tools import execute_tool

        page = _make_mock_page()
        mock_get_service.return_value = _make_mock_service(page)

        result = execute_tool(
            "web_navigate",
            workspace="/tmp/ws",
            params={"url": "https://example.com"},
        )
        assert result.success is True
        assert result.tool_name == "web_navigate"


# ---------------------------------------------------------------------------
# Query classifier integration
# ---------------------------------------------------------------------------


