"""Content-free Modal sandbox lifecycle events for OTLP export."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from agent.monitoring.emitter import emit

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")
_IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/:@+-]{0,255}$")


def fingerprint(value: Any) -> str | None:
    """Return a stable, non-reversible identifier suitable for telemetry."""
    if value is None:
        return None
    text = str(value)
    if not text:
        return None
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def sandbox_id(sandbox: Any) -> str | None:
    """Extract the SDK identifier without assuming one Modal SDK version."""
    for attr in ("object_id", "sandbox_id", "id"):
        value = getattr(sandbox, attr, None)
        if value:
            return str(value)
    return None


def record(
    operation: str,
    *,
    task_id: Any = None,
    lease_id: Any = None,
    sandbox_id: Any = None,
    image: Any = None,
    duration_ms: Any = None,
    error: BaseException | None = None,
) -> None:
    """Emit one sanitized Modal lifecycle event without ever raising."""
    try:
        event: dict[str, Any] = {
            "event": "modal_lifecycle",
            "provider": "modal",
            "operation": operation if _TOKEN_RE.fullmatch(operation) else "unknown",
            "result": "error" if error is not None else "ok",
        }
        if duration_ms is not None:
            event["duration_ms"] = max(0, int(duration_ms))
        if error is not None:
            event["error_class"] = type(error).__name__
        for key, value in (
            ("task_id_hash", fingerprint(task_id)),
            ("lease_id_hash", fingerprint(lease_id)),
            ("sandbox_id_hash", fingerprint(sandbox_id)),
        ):
            if value is not None:
                event[key] = value
        if image and _IMAGE_RE.fullmatch(str(image)):
            event["image_ref"] = str(image)

        emit(event)
    except Exception:
        return


__all__ = ["fingerprint", "record", "sandbox_id"]
