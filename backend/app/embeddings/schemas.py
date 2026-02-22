"""Pydantic schemas for the /embeddings endpoint."""
from pydantic import BaseModel, Field

MAX_BATCH = 96


class EmbedRequest(BaseModel):
    """Request body for POST /embeddings.

    Attributes:
        texts: Non-empty list of strings to embed.  Max ``MAX_BATCH`` items.
    """
    texts: list[str] = Field(
        ...,
        min_length=1,
        max_length=MAX_BATCH,
        description="Texts to embed.  Maximum 96 per request.",
    )


class EmbedResponse(BaseModel):
    """Response body for POST /embeddings.

    Attributes:
        vectors: One float vector per input text, in the same order.
        model:   The model ID used to produce the embeddings.
        dim:     Dimensionality of each vector.
    """
    vectors: list[list[float]]
    model: str
    dim: int
