"""Abstract EmbeddingProvider interface.

Every embedding back-end (Bedrock, OpenAI, Cohere direct, local, …) must
implement this interface so the service layer stays provider-agnostic.
"""
from abc import ABC, abstractmethod


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers.

    Implementations must be thread-safe: the service layer may call
    ``embed()`` from multiple threads via FastAPI's thread-pool executor.
    """

    @property
    @abstractmethod
    def model_id(self) -> str:
        """Provider-internal model identifier (used for logging and cache keys)."""

    @property
    @abstractmethod
    def dim(self) -> int:
        """Dimensionality of the embedding vectors produced by this model."""

    @abstractmethod
    def embed(self, texts: list[str], input_type: str = "search_document") -> list[list[float]]:
        """Generate embedding vectors for a batch of texts.

        Args:
            texts: Non-empty list of strings to embed.  The service layer
                   guarantees ``1 ≤ len(texts) ≤ 32``.
            input_type: Embedding input type hint.  Use ``"search_document"``
                        when indexing and ``"search_query"`` when querying.

        Returns:
            A list of float vectors, one per input text, each of length
            ``self.dim``.

        Raises:
            Exception: On provider error (network, auth, quota, …).
        """
