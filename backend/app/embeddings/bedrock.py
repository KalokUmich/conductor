"""AWS Bedrock embedding provider (Cohere Embed default).

Calls ``bedrock-runtime:invoke_model`` with the Cohere Embed request schema.
The model ID and vector dimension are configurable so the same class covers
any Cohere model that Bedrock exposes (e.g. cohere.embed-english-v3,
cohere.embed-multilingual-v3, cohere.embed-v4).

Cohere request body
-------------------
::

    {
        "texts":       ["text1", "text2"],
        "input_type":  "search_document",   # or "search_query"
        "truncate":    "END"
    }

Cohere response body (both flat and nested float formats are handled)
---------------------------------------------------------------------
::

    # Flat format (Cohere Embed v2/v3):
    { "embeddings": [[...], [...]], ... }

    # Nested format (Cohere Embed v4 with explicit type):
    { "embeddings": { "float": [[...], [...]] }, ... }
"""
import json
import logging
from typing import Optional

from .provider import EmbeddingProvider

logger = logging.getLogger(__name__)

DEFAULT_MODEL_ID = "cohere.embed-english-v3"
DEFAULT_DIM      = 1024
DEFAULT_REGION   = "us-east-1"


class BedrockEmbeddingProvider(EmbeddingProvider):
    """Embedding provider backed by AWS Bedrock (Cohere Embed models).

    Uses ``bedrock-runtime:invoke_model`` with Cohere's embedding schema.

    Args:
        model_id:             Bedrock model ID for the embedding model.
        dim:                  Expected vector dimensionality.
        aws_access_key_id:    AWS access key.  ``None`` → default credential chain.
        aws_secret_access_key: AWS secret access key.
        aws_session_token:    Optional temporary-credential session token.
        region_name:          AWS region.  Defaults to ``us-east-1``.
    """

    def __init__(
        self,
        model_id: str = DEFAULT_MODEL_ID,
        dim: int = DEFAULT_DIM,
        aws_access_key_id: Optional[str] = None,
        aws_secret_access_key: Optional[str] = None,
        aws_session_token: Optional[str] = None,
        region_name: Optional[str] = None,
    ) -> None:
        self._model_id  = model_id
        self._dim       = dim
        self._access_key = aws_access_key_id
        self._secret_key = aws_secret_access_key
        self._session_token = aws_session_token
        self._region    = region_name or DEFAULT_REGION
        self._client: Optional[object] = None

    # -----------------------------------------------------------------------
    # EmbeddingProvider properties
    # -----------------------------------------------------------------------

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    # -----------------------------------------------------------------------
    # Internal helpers
    # -----------------------------------------------------------------------

    def _get_client(self) -> object:
        """Return a cached boto3 bedrock-runtime client."""
        if self._client is None:
            try:
                import boto3  # lazy import — not required when mocked in tests
            except ImportError as exc:
                raise ImportError(
                    "boto3 is required for BedrockEmbeddingProvider. "
                    "Install it with: pip install boto3"
                ) from exc

            kwargs: dict = {"region_name": self._region}
            if self._access_key and self._secret_key:
                kwargs["aws_access_key_id"]     = self._access_key
                kwargs["aws_secret_access_key"] = self._secret_key
            if self._session_token:
                kwargs["aws_session_token"] = self._session_token

            self._client = boto3.client("bedrock-runtime", **kwargs)

        return self._client

    # -----------------------------------------------------------------------
    # EmbeddingProvider implementation
    # -----------------------------------------------------------------------

    def embed(self, texts: list[str], input_type: str = "search_document") -> list[list[float]]:
        """Embed a batch of texts using the configured Cohere model on Bedrock.

        Args:
            texts: 1–32 strings to embed.
            input_type: Cohere input type (``"search_document"`` for indexing,
                        ``"search_query"`` for queries).

        Returns:
            List of float vectors, one per input text.

        Raises:
            ValueError:  If the provider returns an unexpected response shape.
            Exception:   On Bedrock API errors (network, auth, throttle, …).
        """
        client = self._get_client()

        request_body = json.dumps(
            {
                "texts":      texts,
                "input_type": input_type,
                "truncate":   "END",
            }
        )

        logger.debug(
            "[embeddings/bedrock] invoking model=%s texts=%d",
            self._model_id,
            len(texts),
        )

        response = client.invoke_model(
            modelId=self._model_id,
            body=request_body,
            contentType="application/json",
            accept="application/json",
        )

        data = json.loads(response["body"].read())

        # Support both flat and nested response formats.
        raw = data.get("embeddings")
        if raw is None:
            raise ValueError(
                f"Unexpected Bedrock response — 'embeddings' key missing: {list(data.keys())}"
            )

        if isinstance(raw, dict):
            # Cohere Embed v4 nested format: {"float": [[...], ...]}
            if "float" in raw:
                vectors = raw["float"]
            else:
                raise ValueError(
                    f"Unexpected nested embeddings format, keys: {list(raw.keys())}"
                )
        else:
            # Flat format: [[...], ...]
            vectors = raw

        if len(vectors) != len(texts):
            raise ValueError(
                f"Provider returned {len(vectors)} vectors for {len(texts)} texts"
            )

        logger.debug(
            "[embeddings/bedrock] received %d vectors dim=%d",
            len(vectors),
            len(vectors[0]) if vectors else 0,
        )
        return vectors
