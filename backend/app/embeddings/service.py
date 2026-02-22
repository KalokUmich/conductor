"""EmbeddingService — thin orchestration layer over EmbeddingProvider.

Validates batch-size constraints and delegates to the configured provider.
A module-level singleton is initialised in ``app/main.py`` from config.
"""
import logging
from typing import Optional

from .provider import EmbeddingProvider
from .schemas import MAX_BATCH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_service: Optional["EmbeddingService"] = None


def get_embedding_service() -> Optional["EmbeddingService"]:
    """Return the global EmbeddingService, or None if not yet initialised."""
    return _service


def set_embedding_service(service: "EmbeddingService") -> None:
    """Set (or replace) the global EmbeddingService instance."""
    global _service
    _service = service


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class EmbeddingService:
    """Validates requests and delegates to an EmbeddingProvider.

    Args:
        provider: Concrete embedding provider to use.
    """

    def __init__(self, provider: EmbeddingProvider) -> None:
        self._provider = provider

    # -----------------------------------------------------------------------
    # Public API
    # -----------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return self._provider.model_id

    @property
    def dim(self) -> int:
        return self._provider.dim

    def embed(self, texts: list[str], input_type: str = "search_document") -> list[list[float]]:
        """Embed a validated batch of texts.

        Args:
            texts: 1–MAX_BATCH strings to embed.
            input_type: Embedding input type (``"search_document"`` for indexing,
                        ``"search_query"`` for queries).

        Returns:
            Ordered list of float vectors.

        Raises:
            ValueError: If ``texts`` is empty or exceeds ``MAX_BATCH``.
            Exception:  On provider-level errors.
        """
        if not texts:
            raise ValueError("texts must not be empty")
        if len(texts) > MAX_BATCH:
            raise ValueError(
                f"Batch size {len(texts)} exceeds maximum of {MAX_BATCH}"
            )

        logger.debug(
            "[EmbeddingService] embedding %d text(s) via provider=%s model=%s input_type=%s",
            len(texts),
            type(self._provider).__name__,
            self._provider.model_id,
            input_type,
        )
        return self._provider.embed(texts, input_type=input_type)
