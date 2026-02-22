"""Tests for the context enrichment router — /context/explain and /context/explain-rich."""
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.context.router import router


@pytest.fixture()
def _context_app():
    """Minimal FastAPI app with only the context router."""
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture()
def _mock_provider():
    """A mock AIProvider whose call_model returns a canned explanation."""
    provider = MagicMock()
    provider.call_model.return_value = "This code processes data by splitting on commas."
    provider.model_id = "test-model-1"
    return provider


# ---------------------------------------------------------------------------
# POST /context/explain-rich
# ---------------------------------------------------------------------------


class TestExplainRich:
    """Tests for the new prompt-through endpoint."""

    def test_forwards_assembled_prompt(self, _context_app, _mock_provider):
        """The assembled_prompt should be forwarded directly to call_model."""
        from app.ai_provider.resolver import set_resolver

        resolver = MagicMock()
        resolver.get_active_provider.return_value = _mock_provider
        resolver.resolve.return_value = _mock_provider
        set_resolver(resolver)

        try:
            client = TestClient(_context_app)
            xml_prompt = (
                "<context>\n"
                "  <current-file path='app.py'>def greet(): ...</current-file>\n"
                "  <definition path='utils.py'>def helper(): ...</definition>\n"
                "  <question>Explain this code.</question>\n"
                "</context>"
            )
            response = client.post("/context/explain-rich", json={
                "assembled_prompt": xml_prompt,
                "snippet": "def greet(): ...",
                "file_path": "app.py",
                "line_start": 1,
                "line_end": 1,
                "language": "python",
            })

            assert response.status_code == 200
            data = response.json()
            assert data["explanation"] == "This code processes data by splitting on commas."

            # Verify the full XML prompt was forwarded and a system prompt added.
            _mock_provider.call_model.assert_called_once_with(
                xml_prompt,
                max_tokens=4096,
                system=_mock_provider.call_model.call_args.kwargs["system"],
            )
            # System prompt should be non-empty.
            assert _mock_provider.call_model.call_args.kwargs["system"]
        finally:
            set_resolver(None)

    def test_returns_model_in_response(self, _context_app, _mock_provider):
        """Response should include the provider's model_id."""
        from app.ai_provider.resolver import set_resolver

        resolver = MagicMock()
        resolver.get_active_provider.return_value = _mock_provider
        resolver.resolve.return_value = _mock_provider
        set_resolver(resolver)

        try:
            client = TestClient(_context_app)
            response = client.post("/context/explain-rich", json={
                "assembled_prompt": "Explain X",
                "snippet": "x = 1",
                "file_path": "x.py",
                "line_start": 1,
                "line_end": 1,
            })

            assert response.status_code == 200
            data = response.json()
            assert data["model"] == "test-model-1"
            assert data["file_path"] == "x.py"
            assert data["line_start"] == 1
            assert data["line_end"] == 1
        finally:
            set_resolver(None)

    def test_returns_503_when_no_provider(self, _context_app):
        """Should return 503 when no healthy AI provider is available."""
        from app.ai_provider.resolver import set_resolver

        resolver = MagicMock()
        resolver.get_active_provider.return_value = None
        resolver.resolve.return_value = None
        set_resolver(resolver)

        try:
            client = TestClient(_context_app)
            response = client.post("/context/explain-rich", json={
                "assembled_prompt": "Explain X",
                "snippet": "x = 1",
                "file_path": "x.py",
                "line_start": 1,
                "line_end": 1,
            })

            assert response.status_code == 503
            assert "No healthy AI provider" in response.json()["error"]
        finally:
            set_resolver(None)

    def test_returns_503_when_resolver_is_none(self, _context_app):
        """Should return 503 when the resolver itself is not initialized."""
        from app.ai_provider.resolver import set_resolver

        set_resolver(None)

        client = TestClient(_context_app)
        response = client.post("/context/explain-rich", json={
            "assembled_prompt": "Explain X",
            "snippet": "x = 1",
            "file_path": "x.py",
            "line_start": 1,
            "line_end": 1,
        })

        assert response.status_code == 503
        assert "not available" in response.json()["error"]

    def test_returns_500_on_llm_error(self, _context_app, _mock_provider):
        """Should return 500 when the LLM call raises."""
        from app.ai_provider.resolver import set_resolver

        _mock_provider.call_model.side_effect = RuntimeError("model overloaded")
        resolver = MagicMock()
        resolver.get_active_provider.return_value = _mock_provider
        resolver.resolve.return_value = _mock_provider
        set_resolver(resolver)

        try:
            client = TestClient(_context_app)
            response = client.post("/context/explain-rich", json={
                "assembled_prompt": "Explain X",
                "snippet": "x = 1",
                "file_path": "x.py",
                "line_start": 1,
                "line_end": 1,
            })

            assert response.status_code == 500
            assert "model overloaded" in response.json()["error"]
        finally:
            set_resolver(None)

    def test_validates_line_start_ge_1(self, _context_app):
        """line_start must be >= 1."""
        from app.ai_provider.resolver import set_resolver
        set_resolver(None)

        client = TestClient(_context_app)
        response = client.post("/context/explain-rich", json={
            "assembled_prompt": "Explain X",
            "snippet": "x = 1",
            "file_path": "x.py",
            "line_start": 0,
            "line_end": 1,
        })

        assert response.status_code == 422
