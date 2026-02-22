"""FastAPI router for POST /embeddings.

The extension calls this endpoint to obtain embedding vectors for code
symbols.  The backend delegates to the configured EmbeddingProvider
(Bedrock by default) so the extension never calls a cloud API directly.

Constraints
-----------
- Batch size: 1–32 texts per request (enforced by the Pydantic schema).
- Returns 503 when no embedding service is configured.
- Returns 422 for schema violations (Pydantic automatic validation).
- Returns 500 with a JSON ``{"error": "..."}`` body on provider failure.
"""
import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .schemas import EmbedRequest, EmbedResponse
from .service import get_embedding_service, set_embedding_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/embeddings", tags=["embeddings"])


class EmbedConfigResponse(BaseModel):
    """Current embedding configuration sourced from conductor.settings.yaml."""
    model: str
    dim: int
    provider: str


@router.get("/config", response_model=EmbedConfigResponse)
async def get_embedding_config() -> EmbedConfigResponse:
    """Return the active embedding model configuration.

    The extension calls this once on startup so it knows which model ID to
    use as the cache key when reading vectors from SQLite, without having to
    duplicate the value in ``.conductor/config.json``.

    Returns:
        ``{ model: string, dim: int, provider: string }``

    Example::

        GET /embeddings/config

        200 OK
        {
            "model": "cohere.embed-v4",
            "dim": 1024,
            "provider": "bedrock"
        }
    """
    from app.config import get_config  # local import to avoid circular deps

    service = get_embedding_service()
    if service is not None:
        # Service is live — report what it is actually using.
        cfg = get_config()
        return EmbedConfigResponse(
            model=service.model_id,
            dim=service.dim,
            provider=cfg.embedding.provider,
        )

    # Service not yet initialised (e.g. Bedrock creds not configured).
    # Still return the configured values from settings so the extension
    # can pre-populate its cache key.
    cfg = get_config()
    return EmbedConfigResponse(
        model=cfg.embedding.model,
        dim=cfg.embedding.dim,
        provider=cfg.embedding.provider,
    )


@router.post("", response_model=EmbedResponse)
async def embed_texts(request: EmbedRequest) -> EmbedResponse:
    """Generate embedding vectors for a batch of texts.

    The extension sends symbol signatures (or any short text) and receives
    back raw float vectors that it stores locally in SQLite.

    Args:
        request: ``{ texts: string[] }``  — 1 to 32 strings.

    Returns:
        ``{ vectors: number[][], model: string, dim: int }``

    Example::

        POST /embeddings
        { "texts": ["function greet(name: string): string", "class Animal"] }

        200 OK
        {
            "vectors": [[0.12, -0.04, ...], [0.08, 0.31, ...]],
            "model": "cohere.embed-v4",
            "dim": 1024
        }
    """
    service = get_embedding_service()
    if service is None:
        logger.warning("[embeddings] No embedding service configured")
        return JSONResponse(
            {"error": "Embedding service not available"},
            status_code=503,
        )

    try:
        vectors = service.embed(request.texts)
        logger.info(
            "[embeddings] embedded %d text(s) model=%s dim=%d",
            len(request.texts),
            service.model_id,
            service.dim,
        )
        return EmbedResponse(
            vectors=vectors,
            model=service.model_id,
            dim=service.dim,
        )
    except Exception as exc:
        exc_str = str(exc)
        # Non-recoverable AWS credential errors — disable the service so the
        # extension stops retrying until the backend is restarted with fresh creds.
        _NON_RECOVERABLE = (
            'ExpiredTokenException',
            'InvalidClientTokenId',
            'UnrecognizedClientException',
            'InvalidIdentityToken',
            'TokenRefreshRequired',
        )
        if any(code in exc_str for code in _NON_RECOVERABLE):
            logger.error(
                "[embeddings] AWS credentials invalid/expired — disabling embedding service: %s", exc
            )
            set_embedding_service(None)
            return JSONResponse(
                {"error": "Embedding credentials expired — service disabled until restart"},
                status_code=503,
            )
        logger.exception("[embeddings] Provider error: %s", exc)
        return JSONResponse(
            {"error": f"Embedding failed: {exc}"},
            status_code=500,
        )
