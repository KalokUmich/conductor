"""Embedding provider abstraction for code search.

Uses **LiteLLM** as a unified backend to support 100+ embedding providers
through a single interface.  This replaces five hand-written provider
classes with one thin wrapper around ``litellm.embedding()``.

A lightweight ``LocalEmbeddingProvider`` is kept as a zero-cost fallback
for users who don't want to call any external API.

Supported model strings (LiteLLM format)
-----------------------------------------
* ``sbert/sentence-transformers/all-MiniLM-L6-v2`` — local, free
* ``bedrock/cohere.embed-v4:0``       — AWS Bedrock Cohere
* ``bedrock/amazon.titan-embed-text-v2:0`` — AWS Bedrock Titan
* ``text-embedding-3-small``          — OpenAI
* ``voyage/voyage-code-3``            — Voyage AI
* ``mistral/mistral-embed``           — Mistral
* ``cohere/embed-english-v3.0``       — Cohere Direct
* ``gemini/text-embedding-004``       — Google Gemini
* ``ollama/nomic-embed-text``         — Ollama (local)
* Any other LiteLLM-supported model string

Usage::

    provider = create_embedding_provider(settings)
    vectors  = await provider.embed_texts(["def main(): pass"])
    dims     = provider.dimensions

Migration note
--------------
The previous five provider classes (Local, Bedrock, OpenAI, Voyage,
Mistral) are replaced.  The ``embedding_model`` setting now accepts a
LiteLLM model string directly.  The old ``embedding_backend`` field is
no longer used.
"""
from __future__ import annotations

import abc
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Well-known dimension map (avoids a probe call when the model is known)
# ---------------------------------------------------------------------------

_KNOWN_DIMS: Dict[str, int] = {
    # Local (SentenceTransformers via sbert/ prefix)
    "sbert/sentence-transformers/all-MiniLM-L6-v2":      384,
    "sbert/nomic-ai/CodeRankEmbed":                      768,
    # AWS Bedrock
    "bedrock/cohere.embed-english-v3":                   1024,
    "bedrock/cohere.embed-multilingual-v3":              1024,
    "bedrock/cohere.embed-v4:0":                         1024,
    "bedrock/amazon.titan-embed-text-v1":                1536,
    "bedrock/amazon.titan-embed-text-v2:0":              1024,
    # OpenAI
    "text-embedding-3-small":                            1536,
    "text-embedding-3-large":                            3072,
    "text-embedding-ada-002":                            1536,
    # Voyage AI
    "voyage/voyage-code-3":                              1024,
    "voyage/voyage-3":                                   1024,
    "voyage/voyage-3-lite":                              512,
    # Mistral
    "mistral/codestral-embed-2505":                      1024,
    "mistral/mistral-embed":                             1024,
    # Cohere (direct)
    "cohere/embed-english-v3.0":                         1024,
    "cohere/embed-english-v4.0":                         1024,
    # Google Gemini
    "gemini/text-embedding-004":                         768,
    # Ollama (local)
    "ollama/nomic-embed-text":                           768,
}

# Default fallback dimensions
_DEFAULT_DIMS = 1024


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class EmbeddingProvider(abc.ABC):
    """Base class for all embedding backends."""

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Human-readable backend name (e.g. ``"litellm/bedrock/cohere.embed-v4:0"``)."""

    @property
    @abc.abstractmethod
    def dimensions(self) -> int:
        """Dimensionality of the embedding vectors produced by this model."""

    @abc.abstractmethod
    async def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        """Embed a batch of texts.

        Returns
        -------
        np.ndarray
            Shape ``(len(texts), self.dimensions)`` float32 array.
        """

    async def embed_query(self, query: str) -> np.ndarray:
        """Embed a single search query.

        Some models use a different prefix / input type for queries.
        Override this if needed; default delegates to *embed_texts*.
        """
        result = await self.embed_texts([query])
        return result[0]

    def health_check(self) -> Dict[str, Any]:
        """Return a JSON-friendly dict describing provider status."""
        return {
            "provider": self.name,
            "dimensions": self.dimensions,
            "status": "ok",
        }


# ---------------------------------------------------------------------------
# 1. Local (SentenceTransformers) — zero-cost fallback
# ---------------------------------------------------------------------------


class LocalEmbeddingProvider(EmbeddingProvider):
    """SentenceTransformers model running locally.

    Default model: ``all-MiniLM-L6-v2`` (384-d, ~80 MB).
    """

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        self._model_name = model_name
        self._model: Any = None  # lazy-loaded
        self._dims: Optional[int] = None

    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        from sentence_transformers import SentenceTransformer  # type: ignore

        self._model = SentenceTransformer(self._model_name)
        dummy = self._model.encode(["test"], convert_to_numpy=True)
        self._dims = dummy.shape[1]
        logger.info(
            "LocalEmbeddingProvider loaded model=%s dims=%d",
            self._model_name,
            self._dims,
        )

    @property
    def name(self) -> str:
        return f"local/{self._model_name}"

    @property
    def dimensions(self) -> int:
        if self._dims is not None:
            return self._dims
        # Check known map with sbert/ prefix
        sbert_key = f"sbert/sentence-transformers/{self._model_name}"
        if sbert_key in _KNOWN_DIMS:
            return _KNOWN_DIMS[sbert_key]
        self._ensure_loaded()
        assert self._dims is not None
        return self._dims

    async def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        self._ensure_loaded()
        loop = asyncio.get_event_loop()
        vectors = await loop.run_in_executor(
            None,
            lambda: self._model.encode(list(texts), convert_to_numpy=True),
        )
        return vectors.astype(np.float32)


# ---------------------------------------------------------------------------
# 2. LiteLLM (unified provider for 100+ backends)
# ---------------------------------------------------------------------------


class LiteLLMEmbeddingProvider(EmbeddingProvider):
    """Unified embedding provider using LiteLLM.

    Supports any model string that LiteLLM recognises::

        bedrock/cohere.embed-v4:0
        text-embedding-3-small
        voyage/voyage-code-3
        mistral/mistral-embed
        cohere/embed-english-v3.0
        gemini/text-embedding-004

    Environment variables for credentials (set by _inject_embedding_env_vars):

    * ``AWS_ACCESS_KEY_ID`` / ``AWS_SECRET_ACCESS_KEY`` for Bedrock
    * ``OPENAI_API_KEY`` for OpenAI
    * ``VOYAGE_API_KEY`` for Voyage
    * ``MISTRAL_API_KEY`` for Mistral
    * ``CO_API_KEY`` for Cohere
    * ``GEMINI_API_KEY`` for Gemini
    """

    def __init__(self, model: str, dimensions: Optional[int] = None) -> None:
        self._model = model
        self._dims = dimensions or _KNOWN_DIMS.get(model, _DEFAULT_DIMS)
        self._litellm: Any = None  # lazy import

    def _ensure_litellm(self) -> None:
        if self._litellm is not None:
            return
        import litellm  # type: ignore
        self._litellm = litellm
        logger.info("LiteLLM loaded for embedding model=%s", self._model)

    @property
    def name(self) -> str:
        return f"litellm/{self._model}"

    @property
    def dimensions(self) -> int:
        return self._dims

    async def embed_texts(self, texts: Sequence[str]) -> np.ndarray:
        if len(texts) == 0:
            return np.empty((0, self._dims), dtype=np.float32)

        self._ensure_litellm()
        loop = asyncio.get_event_loop()
        response = await loop.run_in_executor(
            None,
            lambda: self._litellm.embedding(
                model=self._model,
                input=list(texts),
            ),
        )
        vectors = [d["embedding"] for d in response.data]
        return np.array(vectors, dtype=np.float32)

    async def embed_query(self, query: str) -> np.ndarray:
        result = await self.embed_texts([query])
        return result[0]


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_embedding_provider(settings) -> EmbeddingProvider:
    """Instantiate the configured embedding provider.

    Parameters
    ----------
    settings:
        An object with ``embedding_model`` (LiteLLM model string).
        Falls back to ``embedding_backend`` for legacy compatibility.

    Returns
    -------
    EmbeddingProvider
    """
    # New unified field: embedding_model (LiteLLM model string)
    model = getattr(settings, "embedding_model", None)

    if model is None:
        # Legacy fallback: map old embedding_backend to LiteLLM model string
        backend = getattr(settings, "embedding_backend", "local")
        model = _legacy_backend_to_model(backend, settings)

    # Local SentenceTransformers models (sbert/ prefix or "local" backend)
    if model.startswith("sbert/"):
        # Strip "sbert/" prefix — not needed for SentenceTransformers
        st_model = model.replace("sbert/sentence-transformers/", "").replace("sbert/", "")
        logger.info("Creating LocalEmbeddingProvider (model=%s)", st_model)
        return LocalEmbeddingProvider(model_name=st_model)

    if model == "local" or model.startswith("local/"):
        local_name = model.replace("local/", "") if "/" in model else "all-MiniLM-L6-v2"
        logger.info("Creating LocalEmbeddingProvider (model=%s)", local_name)
        return LocalEmbeddingProvider(model_name=local_name)

    # Everything else goes through LiteLLM
    dims = getattr(settings, "embedding_dimensions", None)
    logger.info("Creating LiteLLMEmbeddingProvider (model=%s)", model)
    return LiteLLMEmbeddingProvider(model=model, dimensions=dims)


def _legacy_backend_to_model(backend: str, settings) -> str:
    """Map old-style embedding_backend values to LiteLLM model strings."""
    if backend == "local":
        model_name = getattr(settings, "local_model_name", "all-MiniLM-L6-v2")
        return f"sbert/sentence-transformers/{model_name}"

    if backend == "bedrock":
        model_id = getattr(settings, "bedrock_model_id", "cohere.embed-v4:0")
        return f"bedrock/{model_id}"

    if backend == "openai":
        return getattr(settings, "openai_model_name", "text-embedding-3-small")

    if backend == "voyage":
        model_name = getattr(settings, "voyage_model_name", "voyage-code-3")
        return f"voyage/{model_name}"

    if backend == "mistral":
        model_name = getattr(settings, "mistral_model_name", "codestral-embed-2505")
        return f"mistral/{model_name}"

    raise ValueError(
        f"Unknown embedding backend: {backend!r}. "
        f"Use a LiteLLM model string in embedding_model instead."
    )
