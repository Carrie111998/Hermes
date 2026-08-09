"""Embedding contracts and adapters for Semantic Graph retrieval."""

from .base import (
    EmbeddingBackend,
    EmbeddingBackendError,
    EmbeddingModelIdentity,
)
from .fake import DeterministicFakeEmbeddingBackend

__all__ = [
    "EmbeddingBackend",
    "EmbeddingBackendError",
    "EmbeddingModelIdentity",
    "DeterministicFakeEmbeddingBackend",
]
