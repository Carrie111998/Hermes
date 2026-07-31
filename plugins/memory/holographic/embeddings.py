"""Optional SBERT embeddings for semantic similarity.

Falls back gracefully when sentence-transformers is not installed.
No hard dependency — import this module safely in any environment.
"""

import logging
import struct
from typing import Callable, Optional, Sequence

logger = logging.getLogger(__name__)

_EMBED_DIM = 384
_EMBED_MODEL = "all-MiniLM-L6-v2"
_cache = None  # SentenceTransformer | None


def _load_model():
    """Lazy-load the SBERT model. Cached after first call."""
    global _cache
    if _cache is not None:
        return _cache
    try:
        from sentence_transformers import SentenceTransformer

        _cache = SentenceTransformer(_EMBED_MODEL)
        logger.info("Loaded SBERT model: %s", _EMBED_MODEL)
    except ImportError:
        logger.debug(
            "sentence-transformers not installed — semantic features disabled"
        )
    except Exception as exc:
        logger.warning("Failed to load SBERT model: %s", exc)
    return _cache


def get_embedder():
    # type: () -> Optional[Callable[[Sequence[str]], list]]
    """Return a callable that embeds a batch of texts, or None."""
    model = _load_model()
    if model is None:
        return None

    import numpy as np

    def embed(texts):
        # type: (Sequence[str]) -> list
        if isinstance(texts, str):
            texts = [texts]
        embeddings = model.encode(
            list(texts), normalize_embeddings=True, show_progress_bar=False
        )
        return [np.asarray(e, dtype=np.float32) for e in embeddings]

    return embed


def embedding_dim():
    # type: () -> int
    return _EMBED_DIM


def pack_vector(vec):
    # type: (...) -> bytes
    """Pack a float32 vector into a BLOB."""
    import numpy as np

    arr = np.asarray(vec, dtype=np.float32)
    return struct.pack(f"{len(arr)}f", *arr)


def unpack_vector(blob):
    # type: (bytes) -> ...
    """Unpack a BLOB into a float32 numpy array."""
    import numpy as np

    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def cosine_similarity(a, b):
    # type: (...) -> float
    """Cosine similarity between two numpy vectors. Both assumed normalized."""
    import numpy as np

    return float(
        np.dot(np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32))
    )
