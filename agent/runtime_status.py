"""Opt-in local runtime-status snapshots for process supervisors.

The owning frontend supplies ``agent.runtime_status_file``.  Hermes never
publishes these metrics externally and does no work when the attribute is
unset.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _counter(value: Any, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return max(0, parsed)


def runtime_status_payload(agent: Any) -> dict[str, Any]:
    compressor = getattr(agent, "context_compressor", None)
    context_size = _counter(getattr(compressor, "context_length", 0))
    context_used = _counter(getattr(compressor, "last_prompt_tokens", 0))
    if context_size:
        context_used = min(context_used, context_size)

    return {
        "schema_version": "1.0.0",
        "pid": os.getpid(),
        "session_id": str(getattr(agent, "session_id", "") or ""),
        "context_used": context_used,
        "context_size": context_size,
        "compression_count": _counter(getattr(compressor, "compression_count", 0)),
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }


def emit_runtime_status(agent: Any) -> bool:
    """Atomically write one agent's current context/compression snapshot."""
    raw_target = getattr(agent, "runtime_status_file", None)
    if not isinstance(raw_target, str) or not raw_target.strip():
        return False

    target = Path(raw_target)
    temporary: Path | None = None
    try:
        target = target.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=str(target.parent),
        )
        temporary = Path(temporary_name)
        try:
            fchmod = getattr(os, "fchmod", None)
            if callable(fchmod):
                fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(runtime_status_payload(agent), handle, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, target)
            temporary = None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return True
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        logger.debug("runtime status write failed for %s: %s", target, exc)
        return False
