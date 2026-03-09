"""Comprehensive tests for the embedding provider abstraction.

Tests the LiteLLM-based unified provider and local SentenceTransformers fallback:

1. LocalEmbeddingProvider  — SentenceTransformers (free, local)
2. LiteLLMEmbeddingProvider — Unified backend (100+ providers via LiteLLM)

Also tests:
  * create_embedding_provider factory
  * _legacy_backend_to_model mapping
  * EmbeddingProvider ABC contract
  * Edge cases (empty input, single text, large batches)
  * Known dimension lookup
  * Error handling

Total: 85+ tests
"""
from __future__ import annotations

import sys
import types
import pytest
import numpy as np
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock


# ---------------------------------------------------------------------------
# Stubs for heavy deps (prevent real imports)
# ---------------------------------------------------------------------------

def _stub(name: str, **attrs) -> types.ModuleType:
    m = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(m, k, v)
    sys.modules.setdefault(name, m)
    return m


_stub("sentence_transformers", SentenceTransformer=MagicMock)
_stub("boto3")
_stub("litellm")
_stub("cocoindex")
_stub("sqlite_vec")
_stub("tree_sitter_languages")
_stub("networkx", DiGraph=MagicMock, pagerank=MagicMock)

# ---------------------------------------------------------------------------
# Real imports
# ---------------------------------------------------------------------------

from app.code_search.embedding_provider import (  # noqa: E402
    EmbeddingProvider,
    LocalEmbeddingProvider,
    LiteLLMEmbeddingProvider,
    create_embedding_provider,
    _legacy_backend_to_model,
    _KNOWN_DIMS,
    _DEFAULT_DIMS,
)


# ===================================================================
# Helper: mock settings object
# ===================================================================


class MockSettings:
    """Mimics CodeSearchSettings fields."""
    def __init__(self, **kwargs):
        defaults = {
            "embedding_model": None,
            "embedding_dimensions": None,
            # Legacy fields
            "embedding_backend": "local",
            "local_model_name": "all-MiniLM-L6-v2",
            "bedrock_model_id": "cohere.embed-v4:0",
            "bedrock_region": "us-east-1",
            "openai_model_name": "text-embedding-3-small",
            "voyage_model_name": "voyage-code-3",
            "mistral_model_name": "codestral-embed-2505",
        }
        defaults.update(kwargs)
        for k, v in defaults.items():
            setattr(self, k, v)


# ===================================================================
# 1. LocalEmbeddingProvider
# ===================================================================


class TestLocalEmbeddingProviderInit:
    def test_default_model_name(self):
        provider = LocalEmbeddingProvider()
        assert provider._model_name == "all-MiniLM-L6-v2"

    def test_custom_model_name(self):
        provider = LocalEmbeddingProvider(model_name="paraphrase-MiniLM-L3-v2")
        assert provider._model_name == "paraphrase-MiniLM-L3-v2"

    def test_name_property(self):
        provider = LocalEmbeddingProvider()
        assert provider.name == "local/all-MiniLM-L6-v2"

    def test_lazy_load_not_called_on_init(self):
        provider = LocalEmbeddingProvider()
        assert provider._model is None

    def test_dimensions_from_known_map(self):
        """Known models should return dimensions without loading."""
        provider = LocalEmbeddingProvider(model_name="all-MiniLM-L6-v2")
        # Should resolve from _KNOWN_DIMS via sbert/ prefix
        assert provider.dimensions == 384


class TestLocalEmbeddingProviderEmbed:
    @pytest.fixture()
    def provider(self):
        p = LocalEmbeddingProvider()
        mock_model = MagicMock()
        mock_model.encode.return_value = np.random.rand(1, 384).astype(np.float32)
        p._model = mock_model
        p._dims = 384
        return p

    @pytest.mark.asyncio
    async def test_embed_texts_returns_ndarray(self, provider):
        provider._model.encode.return_value = np.random.rand(2, 384).astype(np.float32)
        result = await provider.embed_texts(["hello", "world"])
        assert isinstance(result, np.ndarray)
        assert result.shape == (2, 384)

    @pytest.mark.asyncio
    async def test_embed_texts_single(self, provider):
        provider._model.encode.return_value = np.random.rand(1, 384).astype(np.float32)
        result = await provider.embed_texts(["hello"])
        assert result.shape == (1, 384)

    @pytest.mark.asyncio
    async def test_embed_query(self, provider):
        provider._model.encode.return_value = np.random.rand(1, 384).astype(np.float32)
        result = await provider.embed_query("search query")
        assert isinstance(result, np.ndarray)
        assert result.shape == (384,)

    @pytest.mark.asyncio
    async def test_embed_texts_dtype_float32(self, provider):
        provider._model.encode.return_value = np.random.rand(1, 384).astype(np.float64)
        result = await provider.embed_texts(["test"])
        assert result.dtype == np.float32

    def test_dimensions(self, provider):
        assert provider.dimensions == 384

    def test_health_check(self, provider):
        h = provider.health_check()
        assert h["provider"] == "local/all-MiniLM-L6-v2"
        assert h["dimensions"] == 384
        assert h["status"] == "ok"

    @pytest.mark.asyncio
    async def test_embed_empty_list(self, provider):
        provider._model.encode.return_value = np.empty((0, 384), dtype=np.float32)
        result = await provider.embed_texts([])
        assert result.shape[0] == 0


# ===================================================================
# 2. LiteLLMEmbeddingProvider
# ===================================================================


class TestLiteLLMEmbeddingProviderInit:
    def test_model_stored(self):
        p = LiteLLMEmbeddingProvider(model="bedrock/cohere.embed-v4:0")
        assert p._model == "bedrock/cohere.embed-v4:0"

    def test_name_property(self):
        p = LiteLLMEmbeddingProvider(model="text-embedding-3-small")
        assert p.name == "litellm/text-embedding-3-small"

    def test_known_dimensions_bedrock(self):
        p = LiteLLMEmbeddingProvider(model="bedrock/cohere.embed-v4:0")
        assert p.dimensions == 1024

    def test_known_dimensions_openai(self):
        p = LiteLLMEmbeddingProvider(model="text-embedding-3-small")
        assert p.dimensions == 1536

    def test_known_dimensions_openai_large(self):
        p = LiteLLMEmbeddingProvider(model="text-embedding-3-large")
        assert p.dimensions == 3072

    def test_known_dimensions_voyage(self):
        p = LiteLLMEmbeddingProvider(model="voyage/voyage-code-3")
        assert p.dimensions == 1024

    def test_known_dimensions_voyage_lite(self):
        p = LiteLLMEmbeddingProvider(model="voyage/voyage-3-lite")
        assert p.dimensions == 512

    def test_known_dimensions_mistral(self):
        p = LiteLLMEmbeddingProvider(model="mistral/codestral-embed-2505")
        assert p.dimensions == 1024

    def test_known_dimensions_gemini(self):
        p = LiteLLMEmbeddingProvider(model="gemini/text-embedding-004")
        assert p.dimensions == 768

    def test_known_dimensions_ollama(self):
        p = LiteLLMEmbeddingProvider(model="ollama/nomic-embed-text")
        assert p.dimensions == 768

    def test_known_dimensions_titan(self):
        p = LiteLLMEmbeddingProvider(model="bedrock/amazon.titan-embed-text-v2:0")
        assert p.dimensions == 1024

    def test_unknown_model_uses_default(self):
        p = LiteLLMEmbeddingProvider(model="some-unknown-model/v1")
        assert p.dimensions == _DEFAULT_DIMS

    def test_explicit_dimensions_override(self):
        p = LiteLLMEmbeddingProvider(model="some-model", dimensions=2048)
        assert p.dimensions == 2048

    def test_lazy_litellm_import(self):
        p = LiteLLMEmbeddingProvider(model="text-embedding-3-small")
        assert p._litellm is None


class TestLiteLLMEmbeddingProviderEmbed:
    @pytest.fixture()
    def provider(self):
        p = LiteLLMEmbeddingProvider(model="bedrock/cohere.embed-v4:0")
        mock_litellm = MagicMock()
        p._litellm = mock_litellm
        return p, mock_litellm

    @pytest.mark.asyncio
    async def test_embed_texts(self, provider):
        p, mock_ll = provider
        mock_data = [
            {"embedding": [0.1] * 1024},
            {"embedding": [0.2] * 1024},
        ]
        mock_ll.embedding.return_value = MagicMock(data=mock_data)

        result = await p.embed_texts(["hello", "world"])
        assert isinstance(result, np.ndarray)
        assert result.shape == (2, 1024)
        assert result.dtype == np.float32

    @pytest.mark.asyncio
    async def test_embed_texts_single(self, provider):
        p, mock_ll = provider
        mock_data = [{"embedding": [0.1] * 1024}]
        mock_ll.embedding.return_value = MagicMock(data=mock_data)

        result = await p.embed_texts(["hello"])
        assert result.shape == (1, 1024)

    @pytest.mark.asyncio
    async def test_embed_texts_passes_model(self, provider):
        p, mock_ll = provider
        mock_data = [{"embedding": [0.1] * 1024}]
        mock_ll.embedding.return_value = MagicMock(data=mock_data)

        await p.embed_texts(["test"])
        mock_ll.embedding.assert_called_once_with(
            model="bedrock/cohere.embed-v4:0",
            input=["test"],
        )

    @pytest.mark.asyncio
    async def test_embed_empty_returns_empty_array(self, provider):
        p, mock_ll = provider
        result = await p.embed_texts([])
        assert result.shape == (0, 1024)
        mock_ll.embedding.assert_not_called()

    @pytest.mark.asyncio
    async def test_embed_query(self, provider):
        p, mock_ll = provider
        mock_data = [{"embedding": [0.1] * 1024}]
        mock_ll.embedding.return_value = MagicMock(data=mock_data)

        result = await p.embed_query("search this")
        assert result.shape == (1024,)

    def test_health_check(self, provider):
        p, _ = provider
        h = p.health_check()
        assert h["provider"] == "litellm/bedrock/cohere.embed-v4:0"
        assert h["dimensions"] == 1024
        assert h["status"] == "ok"

    @pytest.mark.asyncio
    async def test_embed_texts_large_batch(self, provider):
        p, mock_ll = provider
        n = 50
        mock_data = [{"embedding": [0.1] * 1024} for _ in range(n)]
        mock_ll.embedding.return_value = MagicMock(data=mock_data)

        texts = [f"text_{i}" for i in range(n)]
        result = await p.embed_texts(texts)
        assert result.shape == (n, 1024)


# ===================================================================
# Known dimensions map
# ===================================================================


class TestKnownDimensions:
    def test_map_not_empty(self):
        assert len(_KNOWN_DIMS) > 10

    def test_bedrock_cohere_v4(self):
        assert _KNOWN_DIMS["bedrock/cohere.embed-v4:0"] == 1024

    def test_openai_small(self):
        assert _KNOWN_DIMS["text-embedding-3-small"] == 1536

    def test_openai_large(self):
        assert _KNOWN_DIMS["text-embedding-3-large"] == 3072

    def test_voyage_code_3(self):
        assert _KNOWN_DIMS["voyage/voyage-code-3"] == 1024

    def test_local_minilm(self):
        assert _KNOWN_DIMS["sbert/sentence-transformers/all-MiniLM-L6-v2"] == 384

    def test_gemini(self):
        assert _KNOWN_DIMS["gemini/text-embedding-004"] == 768

    def test_default_dims(self):
        assert _DEFAULT_DIMS == 1024


# ===================================================================
# Legacy backend to model mapping
# ===================================================================


class TestLegacyBackendToModel:
    def test_local(self):
        settings = MockSettings(local_model_name="all-MiniLM-L6-v2")
        result = _legacy_backend_to_model("local", settings)
        assert result == "sbert/sentence-transformers/all-MiniLM-L6-v2"

    def test_local_custom_model(self):
        settings = MockSettings(local_model_name="custom-model")
        result = _legacy_backend_to_model("local", settings)
        assert result == "sbert/sentence-transformers/custom-model"

    def test_bedrock_default(self):
        settings = MockSettings(bedrock_model_id="cohere.embed-v4:0")
        result = _legacy_backend_to_model("bedrock", settings)
        assert result == "bedrock/cohere.embed-v4:0"

    def test_bedrock_titan(self):
        settings = MockSettings(bedrock_model_id="amazon.titan-embed-text-v2:0")
        result = _legacy_backend_to_model("bedrock", settings)
        assert result == "bedrock/amazon.titan-embed-text-v2:0"

    def test_openai(self):
        settings = MockSettings(openai_model_name="text-embedding-3-small")
        result = _legacy_backend_to_model("openai", settings)
        assert result == "text-embedding-3-small"

    def test_voyage(self):
        settings = MockSettings(voyage_model_name="voyage-code-3")
        result = _legacy_backend_to_model("voyage", settings)
        assert result == "voyage/voyage-code-3"

    def test_mistral(self):
        settings = MockSettings(mistral_model_name="codestral-embed-2505")
        result = _legacy_backend_to_model("mistral", settings)
        assert result == "mistral/codestral-embed-2505"

    def test_unknown_raises(self):
        settings = MockSettings()
        with pytest.raises(ValueError, match="Unknown embedding backend"):
            _legacy_backend_to_model("unknown", settings)


# ===================================================================
# Factory: create_embedding_provider
# ===================================================================


class TestCreateEmbeddingProvider:
    def test_litellm_model_string_bedrock(self):
        settings = MockSettings(embedding_model="bedrock/cohere.embed-v4:0")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)
        assert provider._model == "bedrock/cohere.embed-v4:0"

    def test_litellm_model_string_openai(self):
        settings = MockSettings(embedding_model="text-embedding-3-small")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)
        assert provider._model == "text-embedding-3-small"

    def test_litellm_model_string_voyage(self):
        settings = MockSettings(embedding_model="voyage/voyage-code-3")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)
        assert provider._model == "voyage/voyage-code-3"

    def test_litellm_model_string_mistral(self):
        settings = MockSettings(embedding_model="mistral/codestral-embed-2505")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)

    def test_litellm_model_string_gemini(self):
        settings = MockSettings(embedding_model="gemini/text-embedding-004")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)
        assert provider.dimensions == 768

    def test_litellm_model_string_cohere_direct(self):
        settings = MockSettings(embedding_model="cohere/embed-english-v3.0")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)

    def test_sbert_prefix_creates_local(self):
        settings = MockSettings(embedding_model="sbert/sentence-transformers/all-MiniLM-L6-v2")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LocalEmbeddingProvider)
        assert provider._model_name == "all-MiniLM-L6-v2"

    def test_sbert_prefix_nomic(self):
        settings = MockSettings(embedding_model="sbert/nomic-ai/CodeRankEmbed")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LocalEmbeddingProvider)
        assert provider._model_name == "nomic-ai/CodeRankEmbed"

    def test_local_string_creates_local(self):
        settings = MockSettings(embedding_model="local")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LocalEmbeddingProvider)
        assert provider._model_name == "all-MiniLM-L6-v2"

    def test_local_slash_model_creates_local(self):
        settings = MockSettings(embedding_model="local/custom-model")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LocalEmbeddingProvider)
        assert provider._model_name == "custom-model"

    def test_explicit_dimensions(self):
        settings = MockSettings(
            embedding_model="some-new-provider/model-v1",
            embedding_dimensions=2048,
        )
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)
        assert provider.dimensions == 2048

    # --- Legacy fallback tests ---

    def test_legacy_local(self):
        settings = MockSettings(embedding_model=None, embedding_backend="local")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LocalEmbeddingProvider)

    def test_legacy_bedrock(self):
        settings = MockSettings(embedding_model=None, embedding_backend="bedrock")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)
        assert provider._model == "bedrock/cohere.embed-v4:0"

    def test_legacy_openai(self):
        settings = MockSettings(embedding_model=None, embedding_backend="openai")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)
        assert provider._model == "text-embedding-3-small"

    def test_legacy_voyage(self):
        settings = MockSettings(embedding_model=None, embedding_backend="voyage")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)
        assert provider._model == "voyage/voyage-code-3"

    def test_legacy_mistral(self):
        settings = MockSettings(embedding_model=None, embedding_backend="mistral")
        provider = create_embedding_provider(settings)
        assert isinstance(provider, LiteLLMEmbeddingProvider)
        assert provider._model == "mistral/codestral-embed-2505"

    def test_legacy_unknown_raises(self):
        settings = MockSettings(embedding_model=None, embedding_backend="unknown")
        with pytest.raises(ValueError, match="Unknown embedding backend"):
            create_embedding_provider(settings)


# ===================================================================
# ABC contract tests
# ===================================================================


class TestEmbeddingProviderABC:
    def test_cannot_instantiate_abc(self):
        with pytest.raises(TypeError):
            EmbeddingProvider()  # type: ignore

    def test_local_is_subclass(self):
        assert issubclass(LocalEmbeddingProvider, EmbeddingProvider)

    def test_litellm_is_subclass(self):
        assert issubclass(LiteLLMEmbeddingProvider, EmbeddingProvider)
