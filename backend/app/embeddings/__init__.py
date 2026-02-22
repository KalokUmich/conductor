"""Conductor embedding pipeline.

Provides vector embeddings for code symbols via cloud providers
(Bedrock by default) with a clean provider abstraction for future
extension to OpenAI, Cohere direct, or local models.
"""
from .provider import EmbeddingProvider
from .bedrock import BedrockEmbeddingProvider
from .service import EmbeddingService, get_embedding_service, set_embedding_service

__all__ = [
    "EmbeddingProvider",
    "BedrockEmbeddingProvider",
    "EmbeddingService",
    "get_embedding_service",
    "set_embedding_service",
]
