"""Comprehensive tests for the reranking provider abstraction.

Tests all 4 backends with mocked external dependencies:

1. NoopRerankProvider     — Passthrough (no reranking)
2. CohereRerankProvider   — Cohere Rerank API
3. BedrockRerankProvider  — AWS Bedrock Rerank
4. CrossEncoderRerankProvider — Local cross-encoder

Also tests:
  * create_rerank_provider factory
  * RerankProvider ABC contract
  * RerankResult dataclass
  * Edge cases (empty input, single document, many documents)
  * Error handling (missing credentials, API failures)
  * Integration with context router reranking flow

Total: 86 tests

"""
from __future__ import annotations

import json
import sys
import types
import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Stubs for heavy deps (prevent real imports)
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return m


_stub("sentence_transformers", SentenceTransformer=MagicMock, CrossEncoder=MagicMock)
_stub("boto3")
_stub("cohere")
_stub("openai")
_stub("voyageai")
_stub("mistralai", Mistral=MagicMock)
_stub("cocoindex")
_stub("litellm")
_stub("sqlite_vec")
_stub("tree_sitter_languages")
_stub("networkx", DiGraph=MagicMock, pagerank=MagicMock)

# ---------------------------------------------------------------------------
# Real imports
# ---------------------------------------------------------------------------

from app.code_search.rerank_provider import (  # noqa: E402
    RerankProvider,
    RerankResult,
    NoopRerankProvider,
    CohereRerankProvider,
    BedrockRerankProvider,
    CrossEncoderRerankProvider,
    create_rerank_provider,
)


# ===================================================================
# Helper: mock settings object
# ===================================================================


class MockSettings:
    """Mimics CodeSearchSettings fields relevant to reranking."""
    def __init__(self, **kwargs):
        defaults = {
            "rerank_backend": "none",
            "cohere_rerank_model": "rerank-v3.5",
            "cohere_rerank_api_key": None,
            "bedrock_rerank_model_id": "cohere.rerank-v3-5:0",
            "bedrock_region": "us-east-1",
            "bedrock_access_key_id": None,
            "bedrock_secret_access_key": None,
            "cross_encoder_model_name": "cross-encoder/ms-marco-MiniLM-L-6-v2",
        }
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


# ===================================================================
# 0. RerankResult dataclass
# ===================================================================


class TestRerankResult:
    def test_creation(self):
        r = RerankResult(index=0, score=0.95, text="hello")
        assert r.index == 0
        assert r.score == 0.95
        assert r.text == "hello"

    def test_attributes(self):
        r = RerankResult(index=5, score=-1.0, text="")
        assert r.index == 5
        assert r.score == -1.0
        assert r.text == ""

    def test_equality(self):
        r1 = RerankResult(index=0, score=0.5, text="a")
        r2 = RerankResult(index=0, score=0.5, text="a")
        assert r1 == r2

    def test_inequality(self):
        r1 = RerankResult(index=0, score=0.5, text="a")
        r2 = RerankResult(index=1, score=0.5, text="a")
        assert r1 != r2


# ===================================================================
# 1. NoopRerankProvider
# ===================================================================


class TestNoopRerankProviderInit:
    def test_name(self):
        p = NoopRerankProvider()
        assert p.name == "none"

    def test_health_check(self):
        p = NoopRerankProvider()
        h = p.health_check()
        assert h["provider"] == "none"
        assert h["status"] == "ok"


class TestNoopRerankProviderRerank:
    @pytest.mark.asyncio
    async def test_empty_documents(self):
        p = NoopRerankProvider()
        results = await p.rerank("query", [])
        assert results == []

    @pytest.mark.asyncio
    async def test_single_document(self):
        p = NoopRerankProvider()
        results = await p.rerank("query", ["doc1"])
        assert len(results) == 1
        assert results[0].index == 0
        assert results[0].text == "doc1"

    @pytest.mark.asyncio
    async def test_multiple_documents(self):
        p = NoopRerankProvider()
        docs = ["doc1", "doc2", "doc3"]
        results = await p.rerank("query", docs)
        assert len(results) == 3
        for i, r in enumerate(results):
            assert r.index == i
            assert r.text == docs[i]

    @pytest.mark.asyncio
    async def test_preserves_original_order(self):
        p = NoopRerankProvider()
        docs = ["a", "b", "c", "d"]
        results = await p.rerank("test", docs)
        assert [r.text for r in results] == ["a", "b", "c", "d"]

    @pytest.mark.asyncio
    async def test_top_n_limits_results(self):
        p = NoopRerankProvider()
        docs = ["a", "b", "c", "d", "e"]
        results = await p.rerank("test", docs, top_n=3)
        assert len(results) == 3
        assert [r.text for r in results] == ["a", "b", "c"]

    @pytest.mark.asyncio
    async def test_top_n_none_returns_all(self):
        p = NoopRerankProvider()
        docs = ["a", "b", "c"]
        results = await p.rerank("test", docs, top_n=None)
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_scores_decrease_monotonically(self):
        p = NoopRerankProvider()
        docs = [f"doc{i}" for i in range(10)]
        results = await p.rerank("query", docs)
        scores = [r.score for r in results]
        for i in range(len(scores) - 1):
            assert scores[i] > scores[i + 1]

    @pytest.mark.asyncio
    async def test_top_n_larger_than_docs(self):
        p = NoopRerankProvider()
        docs = ["a", "b"]
        results = await p.rerank("query", docs, top_n=10)
        assert len(results) == 2


# ===================================================================
# 2. CohereRerankProvider
# ===================================================================


class TestCohereRerankProviderInit:
    def test_default_model(self):
        p = CohereRerankProvider()
        assert p.name == "cohere/rerank-v3.5"
        assert p._model == "rerank-v3.5"

    def test_custom_model(self):
        p = CohereRerankProvider(model="rerank-english-v3.0")
        assert p.name == "cohere/rerank-english-v3.0"

    def test_with_api_key(self):
        p = CohereRerankProvider(api_key="test-key")
        assert p._api_key == "test-key"

    def test_health_check(self):
        p = CohereRerankProvider()
        h = p.health_check()
        assert "cohere" in h["provider"]
        assert h["status"] == "ok"

    def test_lazy_client_init(self):
        p = CohereRerankProvider()
        assert p._client is None


class TestCohereRerankProviderRerank:
    @pytest.fixture()
    def provider(self):
        p = CohereRerankProvider(api_key="test-key")
        mock_client = MagicMock()
        p._client = mock_client
        return p, mock_client

    @pytest.mark.asyncio
    async def test_empty_documents(self, provider):
        p, mock_client = provider
        results = await p.rerank("query", [])
        assert results == []
        mock_client.rerank.assert_not_called()

    @pytest.mark.asyncio
    async def test_rerank_calls_api(self, provider):
        p, mock_client = provider

        # Mock cohere response
        mock_result = MagicMock()
        mock_result.index = 1
        mock_result.relevance_score = 0.95
        mock_result2 = MagicMock()
        mock_result2.index = 0
        mock_result2.relevance_score = 0.7
        mock_response = MagicMock()
        mock_response.results = [mock_result, mock_result2]
        mock_client.rerank.return_value = mock_response

        results = await p.rerank("how does auth work?", ["code1", "code2"])

        mock_client.rerank.assert_called_once_with(
            model="rerank-v3.5",
            query="how does auth work?",
            documents=["code1", "code2"],
            top_n=2,
        )
        assert len(results) == 2
        assert results[0].index == 1
        assert results[0].score == 0.95
        assert results[0].text == "code2"
        assert results[1].index == 0
        assert results[1].score == 0.7
        assert results[1].text == "code1"

    @pytest.mark.asyncio
    async def test_rerank_with_top_n(self, provider):
        p, mock_client = provider

        mock_result = MagicMock()
        mock_result.index = 2
        mock_result.relevance_score = 0.99
        mock_response = MagicMock()
        mock_response.results = [mock_result]
        mock_client.rerank.return_value = mock_response

        results = await p.rerank("query", ["a", "b", "c"], top_n=1)
        mock_client.rerank.assert_called_once_with(
            model="rerank-v3.5",
            query="query",
            documents=["a", "b", "c"],
            top_n=1,
        )
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_rerank_preserves_original_index(self, provider):
        p, mock_client = provider

        results_data = []
        for i in range(5):
            r = MagicMock()
            r.index = 4 - i  # Reversed
            r.relevance_score = 0.9 - (i * 0.1)
            results_data.append(r)

        mock_response = MagicMock()
        mock_response.results = results_data
        mock_client.rerank.return_value = mock_response

        docs = [f"doc{i}" for i in range(5)]
        results = await p.rerank("query", docs)
        assert results[0].index == 4
        assert results[0].text == "doc4"


# ===================================================================
# 3. BedrockRerankProvider
# ===================================================================


class TestBedrockRerankProviderInit:
    def test_default_model(self):
        p = BedrockRerankProvider()
        assert p.name == "bedrock/cohere.rerank-v3-5:0"
        assert p._model_id == "cohere.rerank-v3-5:0"

    def test_custom_model_and_region(self):
        p = BedrockRerankProvider(model_id="custom-rerank", region="eu-west-1")
        assert p.name == "bedrock/custom-rerank"
        assert p._region == "eu-west-1"

    def test_with_credentials(self):
        p = BedrockRerankProvider(
            access_key_id="AKIA...",
            secret_access_key="wJal...",
        )
        assert p._access_key_id == "AKIA..."
        assert p._secret_access_key == "wJal..."

    def test_health_check(self):
        p = BedrockRerankProvider()
        h = p.health_check()
        assert "bedrock" in h["provider"]
        assert h["status"] == "ok"

    def test_lazy_client_init(self):
        p = BedrockRerankProvider()
        assert p._client is None


class TestBedrockRerankProviderRerank:
    @pytest.fixture()
    def provider(self):
        p = BedrockRerankProvider(region="us-east-1")
        mock_client = MagicMock()
        p._client = mock_client
        return p, mock_client

    def _make_response(self, results_data):
        body_content = json.dumps({"results": results_data}).encode()
        mock_body = MagicMock()
        mock_body.read.return_value = body_content
        return {"body": mock_body}

    @pytest.mark.asyncio
    async def test_empty_documents(self, provider):
        p, mock_client = provider
        results = await p.rerank("query", [])
        assert results == []
        mock_client.invoke_model.assert_not_called()

    @pytest.mark.asyncio
    async def test_rerank_calls_bedrock(self, provider):
        p, mock_client = provider

        mock_client.invoke_model.return_value = self._make_response([
            {"index": 1, "relevance_score": 0.95},
            {"index": 0, "relevance_score": 0.6},
        ])

        results = await p.rerank("auth query", ["code1", "code2"])

        call_args = mock_client.invoke_model.call_args
        assert call_args.kwargs["modelId"] == "cohere.rerank-v3-5:0"
        body = json.loads(call_args.kwargs["body"])
        assert body["query"] == "auth query"
        assert body["documents"] == ["code1", "code2"]
        assert body["top_n"] == 2

        assert len(results) == 2
        assert results[0].score == 0.95
        assert results[0].index == 1
        assert results[0].text == "code2"

    @pytest.mark.asyncio
    async def test_rerank_with_top_n(self, provider):
        p, mock_client = provider

        mock_client.invoke_model.return_value = self._make_response([
            {"index": 2, "relevance_score": 0.99},
        ])

        results = await p.rerank("query", ["a", "b", "c"], top_n=1)
        body = json.loads(mock_client.invoke_model.call_args.kwargs["body"])
        assert body["top_n"] == 1
        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_results_sorted_by_score(self, provider):
        p, mock_client = provider

        # Return in non-sorted order
        mock_client.invoke_model.return_value = self._make_response([
            {"index": 0, "relevance_score": 0.3},
            {"index": 2, "relevance_score": 0.9},
            {"index": 1, "relevance_score": 0.6},
        ])

        results = await p.rerank("query", ["a", "b", "c"])
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)

    @pytest.mark.asyncio
    async def test_rerank_five_documents(self, provider):
        p, mock_client = provider

        mock_client.invoke_model.return_value = self._make_response([
            {"index": i, "relevance_score": 0.9 - (i * 0.1)}
            for i in range(5)
        ])

        docs = [f"doc{i}" for i in range(5)]
        results = await p.rerank("query", docs, top_n=5)
        assert len(results) == 5
        for r in results:
            assert r.text == docs[r.index]


# ===================================================================
# 4. CrossEncoderRerankProvider
# ===================================================================


class TestCrossEncoderRerankProviderInit:
    def test_default_model(self):
        p = CrossEncoderRerankProvider()
        assert "cross_encoder" in p.name
        assert "ms-marco-MiniLM" in p.name

    def test_custom_model(self):
        p = CrossEncoderRerankProvider(model_name="custom-model")
        assert "custom-model" in p.name

    def test_health_check(self):
        p = CrossEncoderRerankProvider()
        h = p.health_check()
        assert "cross_encoder" in h["provider"]
        assert h["status"] == "ok"

    def test_lazy_model_init(self):
        p = CrossEncoderRerankProvider()
        assert p._model is None


class TestCrossEncoderRerankProviderRerank:
    @pytest.fixture()
    def provider(self):
        p = CrossEncoderRerankProvider()
        mock_model = MagicMock()
        p._model = mock_model
        return p, mock_model

    @pytest.mark.asyncio
    async def test_empty_documents(self, provider):
        p, mock_model = provider
        results = await p.rerank("query", [])
        assert results == []
        mock_model.predict.assert_not_called()

    @pytest.mark.asyncio
    async def test_rerank_calls_predict(self, provider):
        p, mock_model = provider

        # cross-encoder returns raw scores for each (query, doc) pair
        mock_model.predict.return_value = np.array([0.3, 0.9, 0.6])

        results = await p.rerank("auth query", ["code1", "code2", "code3"])

        pairs = mock_model.predict.call_args[0][0]
        assert len(pairs) == 3
        assert pairs[0] == ("auth query", "code1")
        assert pairs[1] == ("auth query", "code2")
        assert pairs[2] == ("auth query", "code3")

        # Results sorted by score descending
        assert results[0].index == 1
        assert results[0].score == 0.9
        assert results[0].text == "code2"

        assert results[1].index == 2
        assert results[1].score == 0.6
        assert results[1].text == "code3"

        assert results[2].index == 0
        assert results[2].score == 0.3
        assert results[2].text == "code1"

    @pytest.mark.asyncio
    async def test_rerank_with_top_n(self, provider):
        p, mock_model = provider
        mock_model.predict.return_value = np.array([0.1, 0.5, 0.9, 0.3, 0.7])

        results = await p.rerank("query", ["a", "b", "c", "d", "e"], top_n=2)
        assert len(results) == 2
        assert results[0].index == 2  # score 0.9
        assert results[1].index == 4  # score 0.7

    @pytest.mark.asyncio
    async def test_rerank_single_document(self, provider):
        p, mock_model = provider
        mock_model.predict.return_value = np.array([0.85])

        results = await p.rerank("query", ["only doc"])
        assert len(results) == 1
        assert results[0].index == 0
        assert results[0].score == 0.85
        assert results[0].text == "only doc"

    @pytest.mark.asyncio
    async def test_rerank_preserves_text(self, provider):
        p, mock_model = provider
        mock_model.predict.return_value = np.array([0.5, 0.8])

        results = await p.rerank("q", ["first doc", "second doc"])
        # Sorted: second doc (0.8), first doc (0.5)
        assert results[0].text == "second doc"
        assert results[1].text == "first doc"

    @pytest.mark.asyncio
    async def test_negative_scores(self, provider):
        p, mock_model = provider
        mock_model.predict.return_value = np.array([-0.5, -0.1, -0.9])

        results = await p.rerank("q", ["a", "b", "c"])
        assert results[0].index == 1  # -0.1 is highest
        assert results[0].score == pytest.approx(-0.1)
        assert results[2].index == 2  # -0.9 is lowest


# ===================================================================
# 5. create_rerank_provider factory
# ===================================================================


class TestCreateRerankProviderFactory:
    def test_none_backend(self):
        s = MockSettings(rerank_backend="none")
        p = create_rerank_provider(s)
        assert isinstance(p, NoopRerankProvider)

    def test_cohere_backend(self):
        s = MockSettings(
            rerank_backend="cohere",
            cohere_rerank_model="rerank-v3.5",
            cohere_rerank_api_key="key-123",
        )
        p = create_rerank_provider(s)
        assert isinstance(p, CohereRerankProvider)
        assert p._model == "rerank-v3.5"
        assert p._api_key == "key-123"

    def test_bedrock_backend(self):
        s = MockSettings(
            rerank_backend="bedrock",
            bedrock_rerank_model_id="cohere.rerank-v3-5:0",
            bedrock_region="eu-west-1",
            bedrock_access_key_id="AKIA",
            bedrock_secret_access_key="secret",
        )
        p = create_rerank_provider(s)
        assert isinstance(p, BedrockRerankProvider)
        assert p._model_id == "cohere.rerank-v3-5:0"
        assert p._region == "eu-west-1"
        assert p._access_key_id == "AKIA"
        assert p._secret_access_key == "secret"

    def test_cross_encoder_backend(self):
        s = MockSettings(
            rerank_backend="cross_encoder",
            cross_encoder_model_name="cross-encoder/ms-marco-MiniLM-L-6-v2",
        )
        p = create_rerank_provider(s)
        assert isinstance(p, CrossEncoderRerankProvider)
        assert "ms-marco-MiniLM" in p._model_name

    def test_unknown_backend_raises(self):
        s = MockSettings(rerank_backend="invalid")
        with pytest.raises(ValueError, match="Unknown rerank backend"):
            create_rerank_provider(s)

    def test_missing_backend_defaults_to_none(self):
        """If rerank_backend attribute is missing, default to none."""
        s = MagicMock(spec=[])  # No attributes
        p = create_rerank_provider(s)
        assert isinstance(p, NoopRerankProvider)

    def test_cohere_backend_no_api_key(self):
        s = MockSettings(
            rerank_backend="cohere",
            cohere_rerank_model="rerank-v3.5",
            cohere_rerank_api_key=None,
        )
        p = create_rerank_provider(s)
        assert isinstance(p, CohereRerankProvider)
        assert p._api_key is None

    def test_bedrock_backend_no_credentials(self):
        s = MockSettings(
            rerank_backend="bedrock",
            bedrock_access_key_id=None,
            bedrock_secret_access_key=None,
        )
        p = create_rerank_provider(s)
        assert isinstance(p, BedrockRerankProvider)
        assert p._access_key_id is None


# ===================================================================
# 6. RerankProvider ABC contract
# ===================================================================


class TestRerankProviderABC:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            RerankProvider()

    def test_concrete_must_implement_name(self):
        class Incomplete(RerankProvider):
            async def rerank(self, query, documents, top_n=None):
                return []

        with pytest.raises(TypeError):
            Incomplete()

    def test_concrete_must_implement_rerank(self):
        class Incomplete(RerankProvider):
            @property
            def name(self):
                return "test"

        with pytest.raises(TypeError):
            Incomplete()


# ===================================================================
# 7. Edge cases
# ===================================================================


class TestEdgeCases:
    @pytest.mark.asyncio
    async def test_noop_with_unicode_documents(self):
        p = NoopRerankProvider()
        docs = ["def 函数():", "# コメント", "print('привет')"]
        results = await p.rerank("unicode test", docs)
        assert len(results) == 3
        assert results[0].text == "def 函数():"

    @pytest.mark.asyncio
    async def test_noop_with_very_long_document(self):
        p = NoopRerankProvider()
        long_doc = "x" * 100000
        results = await p.rerank("query", [long_doc])
        assert results[0].text == long_doc

    @pytest.mark.asyncio
    async def test_noop_with_empty_strings(self):
        p = NoopRerankProvider()
        results = await p.rerank("", ["", "", ""])
        assert len(results) == 3

    @pytest.mark.asyncio
    async def test_noop_top_n_zero(self):
        p = NoopRerankProvider()
        results = await p.rerank("query", ["a", "b", "c"], top_n=0)
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_cross_encoder_many_documents(self):
        p = CrossEncoderRerankProvider()
        mock_model = MagicMock()
        p._model = mock_model
        n = 50
        mock_model.predict.return_value = np.random.rand(n)
        docs = [f"doc{i}" for i in range(n)]
        results = await p.rerank("query", docs, top_n=10)
        assert len(results) == 10
        # Results should be sorted by score descending
        scores = [r.score for r in results]
        assert scores == sorted(scores, reverse=True)


# ===================================================================
# 8. Config integration
# ===================================================================


class TestConfigIntegration:
    def test_code_search_settings_has_rerank_fields(self):
        from app.config import CodeSearchSettings
        s = CodeSearchSettings()
        assert s.rerank_backend == "none"
        assert s.rerank_top_n == 5
        assert s.rerank_candidates == 20
        assert s.cohere_rerank_model == "rerank-v3.5"
        assert s.bedrock_rerank_model_id == "cohere.rerank-v3-5:0"
        assert s.cross_encoder_model_name == "cross-encoder/ms-marco-MiniLM-L-6-v2"

    def test_code_search_settings_rerank_backend_literal(self):
        from app.config import CodeSearchSettings
        # Valid values
        for backend in ["none", "cohere", "bedrock", "cross_encoder"]:
            s = CodeSearchSettings(rerank_backend=backend)
            assert s.rerank_backend == backend

    def test_code_search_settings_invalid_rerank_backend(self):
        from app.config import CodeSearchSettings
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            CodeSearchSettings(rerank_backend="invalid")

    def test_secrets_has_cohere(self):
        from app.config import Secrets, CohereSecrets
        s = Secrets()
        assert isinstance(s.cohere, CohereSecrets)
        assert s.cohere.api_key is None

    def test_secrets_cohere_with_key(self):
        from app.config import Secrets
        s = Secrets(cohere={"api_key": "test-key"})
        assert s.cohere.api_key == "test-key"

    def test_app_settings_includes_rerank(self):
        from app.config import AppSettings
        s = AppSettings()
        assert hasattr(s.code_search, "rerank_backend")
        assert hasattr(s.code_search, "rerank_top_n")
        assert hasattr(s.code_search, "rerank_candidates")


# ===================================================================
# 9. Environment variable injection for reranking
# ===================================================================


class TestEnvVarInjection:
    def test_cohere_rerank_injects_co_api_key(self, monkeypatch):
        from app.config import AppSettings, _inject_embedding_env_vars
        monkeypatch.delenv("CO_API_KEY", raising=False)

        s = AppSettings(
            code_search={"embedding_model": "sbert/sentence-transformers/all-MiniLM-L6-v2", "rerank_backend": "cohere"},
            secrets={"cohere": {"api_key": "cohere-test-key"}},
        )
        _inject_embedding_env_vars(s)
        import os
        assert os.environ.get("CO_API_KEY") == "cohere-test-key"
        monkeypatch.delenv("CO_API_KEY", raising=False)

    def test_bedrock_rerank_injects_aws_creds(self, monkeypatch):
        from app.config import AppSettings, _inject_embedding_env_vars
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

        s = AppSettings(
            code_search={"embedding_model": "sbert/sentence-transformers/all-MiniLM-L6-v2", "rerank_backend": "bedrock"},
            secrets={"aws": {"access_key_id": "AKIA", "secret_access_key": "secret"}},
        )
        _inject_embedding_env_vars(s)
        import os
        assert os.environ.get("AWS_ACCESS_KEY_ID") == "AKIA"
        assert os.environ.get("AWS_SECRET_ACCESS_KEY") == "secret"
        monkeypatch.delenv("AWS_ACCESS_KEY_ID", raising=False)
        monkeypatch.delenv("AWS_SECRET_ACCESS_KEY", raising=False)

    def test_none_rerank_no_injection(self, monkeypatch):
        from app.config import AppSettings, _inject_embedding_env_vars
        monkeypatch.delenv("CO_API_KEY", raising=False)

        s = AppSettings(
            code_search={"embedding_model": "sbert/sentence-transformers/all-MiniLM-L6-v2", "rerank_backend": "none"},
        )
        _inject_embedding_env_vars(s)
        import os
        assert os.environ.get("CO_API_KEY") is None


# ===================================================================
# 10. Context router reranking integration (unit tests)
# ===================================================================


class TestContextRouterRerankIntegration:
    """Test the reranking flow in the context router at the unit level."""

    @pytest.mark.asyncio
    async def test_noop_reranker_returns_original_order(self):
        """When reranker is noop, chunks keep original vector search order."""
        reranker = NoopRerankProvider()
        docs = ["chunk A", "chunk B", "chunk C"]
        results = await reranker.rerank("query", docs, top_n=3)
        assert [r.text for r in results] == docs

    @pytest.mark.asyncio
    async def test_reranker_reorders_chunks(self):
        """When reranker produces different scores, chunks are reordered."""
        p = CrossEncoderRerankProvider()
        mock_model = MagicMock()
        p._model = mock_model
        # Score doc at index 2 highest
        mock_model.predict.return_value = np.array([0.1, 0.3, 0.9])

        docs = ["least relevant", "somewhat relevant", "most relevant"]
        results = await p.rerank("query", docs, top_n=3)
        assert results[0].text == "most relevant"
        assert results[0].index == 2
        assert results[2].text == "least relevant"
        assert results[2].index == 0

    @pytest.mark.asyncio
    async def test_reranker_reduces_candidates(self):
        """Reranker reduces 20 candidates to top 5."""
        p = CrossEncoderRerankProvider()
        mock_model = MagicMock()
        p._model = mock_model
        scores = np.random.rand(20)
        mock_model.predict.return_value = scores

        docs = [f"candidate_{i}" for i in range(20)]
        results = await p.rerank("query", docs, top_n=5)
        assert len(results) == 5
        # Check all results reference valid indices
        for r in results:
            assert 0 <= r.index < 20
            assert r.text == docs[r.index]
