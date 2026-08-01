"""RecursiveIntell poly-kv vector operations for Hermes.

Provides Rust-backed KV-cache and vector utilities via the poly-kv crate.

Usage::

    from agent.transports.ri_poly_kv import validate_shape, native_available

    if native_available():
        result = validate_shape(json.dumps(shape_spec))
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def native_available() -> bool:
    """Check if the poly-kv native extension is installed."""
    try:
        import poly_kv._native  # noqa: F401

        return True
    except ImportError:
        return False


def validate_shape(shape_json: str) -> str:
    """Validate a KV-cache shape specification. Returns JSON result string."""
    from poly_kv._native import validate_shape_json

    return validate_shape_json(shape_json)


def build_synthetic_pool(shape_json: str) -> str:
    """Build a synthetic KV-cache pool. Returns JSON receipt string."""
    from poly_kv._native import build_synthetic_pool_receipts_json

    return build_synthetic_pool_receipts_json(shape_json)
