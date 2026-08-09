"""Embedding contracts and adapters for Semantic Graph retrieval."""

from .base import (
    EmbeddingBackend,
    EmbeddingBackendError,
    EmbeddingModelIdentity,
)
from .fake import DeterministicFakeEmbeddingBackend
from .serializer import (
    QUERY_INSTRUCTION,
    serialize_embedding_node,
    serialize_embedding_query,
    source_text_hash,
)

__all__ = [
    "EmbeddingBackend",
    "EmbeddingBackendError",
    "EmbeddingModelIdentity",
    "DeterministicFakeEmbeddingBackend",
    "QUERY_INSTRUCTION",
    "serialize_embedding_node",
    "serialize_embedding_query",
    "source_text_hash",
]
