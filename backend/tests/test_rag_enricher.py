"""Tests for RAG integration in the context enricher.

Covers: RAG context injected into prompt, graceful failure, no workspace_id.
"""
from unittest.mock import MagicMock

import pytest

from app.context.enricher import ContextEnricher
from app.context.schemas import ExplainRequest
from app.context.skills import build_explanation_prompt
from app.rag.schemas import SearchResultItem


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_request(**overrides) -> ExplainRequest:
    """Build a minimal ExplainRequest with optional overrides."""
    defaults = {
        "room_id": "room1",
        "snippet": "def greet(name):\n    return f'Hello, {name}'",
        "file_path": "app/greet.py",
        "line_start": 1,
        "line_end": 2,
        "language": "python",
    }
    defaults.update(overrides)
    return ExplainRequest(**defaults)


def _make_provider(explanation: str = "This function greets a user.") -> MagicMock:
    provider = MagicMock()
    provider.call_model.return_value = explanation
    provider.model_id = "test-model"
    return provider


def _make_rag_indexer(results: list | None = None) -> MagicMock:
    indexer = MagicMock()
    if results is None:
        results = [
            SearchResultItem(
                file_path="app/utils.py",
                start_line=10,
                end_line=20,
                symbol_name="format_name",
                symbol_type="function",
                content="def format_name(n): ...",
                score=0.88,
                language="python",
            )
        ]
    indexer.search.return_value = results
    return indexer


# ---------------------------------------------------------------------------
# build_explanation_prompt with rag_context
# ---------------------------------------------------------------------------

class TestBuildPromptRag:
    def test_rag_context_included(self):
        prompt = build_explanation_prompt(
            snippet="x = 1",
            file_path="a.py",
            language="python",
            rag_context='<chunk file="b.py">some code</chunk>',
        )
        assert "<related_workspace_code>" in prompt
        assert 'file="b.py"' in prompt

    def test_no_rag_context(self):
        prompt = build_explanation_prompt(
            snippet="x = 1",
            file_path="a.py",
            language="python",
        )
        assert "<related_workspace_code>" not in prompt

    def test_rag_context_none(self):
        prompt = build_explanation_prompt(
            snippet="x = 1",
            file_path="a.py",
            language="python",
            rag_context=None,
        )
        assert "<related_workspace_code>" not in prompt


# ---------------------------------------------------------------------------
# ContextEnricher with RAG
# ---------------------------------------------------------------------------

class TestContextEnricherRag:
    def test_rag_context_injected_into_prompt(self):
        provider = _make_provider()
        indexer = _make_rag_indexer()

        enricher = ContextEnricher(provider=provider, rag_indexer=indexer)
        request = _make_request(workspace_id="ws1")
        response = enricher.explain(request)

        assert response.explanation == "This function greets a user."
        # Verify RAG search was called
        indexer.search.assert_called_once()
        # Verify the prompt sent to LLM contains RAG context
        prompt_arg = provider.call_model.call_args[0][0]
        assert "<related_workspace_code>" in prompt_arg

    def test_no_rag_without_workspace_id(self):
        provider = _make_provider()
        indexer = _make_rag_indexer()

        enricher = ContextEnricher(provider=provider, rag_indexer=indexer)
        request = _make_request()  # no workspace_id
        response = enricher.explain(request)

        assert response.explanation == "This function greets a user."
        # RAG should NOT be called
        indexer.search.assert_not_called()
        prompt_arg = provider.call_model.call_args[0][0]
        assert "<related_workspace_code>" not in prompt_arg

    def test_no_rag_without_indexer(self):
        provider = _make_provider()

        enricher = ContextEnricher(provider=provider)  # no rag_indexer
        request = _make_request(workspace_id="ws1")
        response = enricher.explain(request)

        assert response.explanation == "This function greets a user."
        prompt_arg = provider.call_model.call_args[0][0]
        assert "<related_workspace_code>" not in prompt_arg

    def test_rag_error_is_graceful(self):
        """RAG search failure should not break the explain pipeline."""
        provider = _make_provider()
        indexer = MagicMock()
        indexer.search.side_effect = RuntimeError("FAISS crashed")

        enricher = ContextEnricher(provider=provider, rag_indexer=indexer)
        request = _make_request(workspace_id="ws1")
        response = enricher.explain(request)

        # Should still return a valid response
        assert response.explanation == "This function greets a user."
        # Prompt should NOT contain RAG context (it failed)
        prompt_arg = provider.call_model.call_args[0][0]
        assert "<related_workspace_code>" not in prompt_arg

    def test_rag_skips_same_file_results(self):
        """RAG results from the same file should be filtered out."""
        provider = _make_provider()
        indexer = _make_rag_indexer([
            SearchResultItem(
                file_path="app/greet.py",  # same as the request file
                start_line=5,
                end_line=15,
                symbol_name="helper",
                symbol_type="function",
                content="",
                score=0.99,
                language="python",
            )
        ])

        enricher = ContextEnricher(provider=provider, rag_indexer=indexer)
        request = _make_request(workspace_id="ws1")
        response = enricher.explain(request)

        # RAG context should be None (all results from same file)
        prompt_arg = provider.call_model.call_args[0][0]
        assert "<related_workspace_code>" not in prompt_arg

    def test_rag_empty_results(self):
        provider = _make_provider()
        indexer = _make_rag_indexer([])

        enricher = ContextEnricher(provider=provider, rag_indexer=indexer)
        request = _make_request(workspace_id="ws1")
        response = enricher.explain(request)

        prompt_arg = provider.call_model.call_args[0][0]
        assert "<related_workspace_code>" not in prompt_arg

    def test_workspace_id_defaults_from_request(self):
        """workspace_id on ExplainRequest can be absent (defaults to None)."""
        request = _make_request()
        assert request.workspace_id is None

        request_with = _make_request(workspace_id="ws-abc")
        assert request_with.workspace_id == "ws-abc"
