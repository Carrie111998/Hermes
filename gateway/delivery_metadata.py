"""Validated metadata for terminal cross-platform response delivery.

Platform ``send()`` is also used for typing/status/interim content.  Callers
that need the completed turn must therefore rely on an explicit gateway-owned
marker, never on message text, timing, or the broader ``notify`` flag.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping


TERMINAL_DELIVERY_METADATA_KEY = "hermes_terminal_delivery"
TERMINAL_DELIVERY_VERSION = 1
TERMINAL_DELIVERY_OUTCOMES = frozenset({"success", "error"})
_MAX_CORRELATION_ID_BYTES = 256
_MAX_DELIVERY_ID_BYTES = 256


def _bounded_identity(value: Any, *, prefix: str, max_bytes: int) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    encoded = raw.encode("utf-8")
    if len(encoded) <= max_bytes:
        return raw
    return f"{prefix}:{hashlib.sha256(encoded).hexdigest()}"


def mark_terminal_delivery(
    metadata: Mapping[str, Any] | None,
    *,
    outcome: str,
    correlation_id: Any,
    delivery_id: Any,
) -> dict[str, Any]:
    """Return cloned metadata with a gateway-owned terminal marker.

    Oversized opaque identities are replaced with stable SHA-256 identifiers
    instead of being truncated, which avoids collisions while keeping the
    cross-platform envelope bounded.
    """
    if outcome not in TERMINAL_DELIVERY_OUTCOMES:
        raise ValueError(f"Unsupported terminal delivery outcome: {outcome}")

    correlation = _bounded_identity(
        correlation_id,
        prefix="sha256",
        max_bytes=_MAX_CORRELATION_ID_BYTES,
    )
    delivery = _bounded_identity(
        delivery_id,
        prefix="sha256",
        max_bytes=_MAX_DELIVERY_ID_BYTES,
    )
    if not correlation or not delivery:
        raise ValueError("Terminal delivery identities must be non-empty")

    result = dict(metadata) if metadata else {}
    result[TERMINAL_DELIVERY_METADATA_KEY] = {
        "version": TERMINAL_DELIVERY_VERSION,
        "outcome": outcome,
        "correlation_id": correlation,
        "delivery_id": delivery,
    }
    return result


def project_terminal_delivery(
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any] | None:
    """Return an exact validated terminal marker, otherwise ``None``.

    The strict field set prevents arbitrary source metadata from crossing a
    platform boundary under the reserved key.
    """
    if not isinstance(metadata, Mapping):
        return None
    marker = metadata.get(TERMINAL_DELIVERY_METADATA_KEY)
    if not isinstance(marker, Mapping):
        return None
    if set(marker) != {"version", "outcome", "correlation_id", "delivery_id"}:
        return None
    if marker.get("version") != TERMINAL_DELIVERY_VERSION:
        return None
    if marker.get("outcome") not in TERMINAL_DELIVERY_OUTCOMES:
        return None

    correlation = marker.get("correlation_id")
    delivery = marker.get("delivery_id")
    if not isinstance(correlation, str) or not correlation.strip():
        return None
    if not isinstance(delivery, str) or not delivery.strip():
        return None
    if len(correlation.encode("utf-8")) > _MAX_CORRELATION_ID_BYTES:
        return None
    if len(delivery.encode("utf-8")) > _MAX_DELIVERY_ID_BYTES:
        return None

    return {
        "version": TERMINAL_DELIVERY_VERSION,
        "outcome": marker["outcome"],
        "correlation_id": correlation,
        "delivery_id": delivery,
    }
