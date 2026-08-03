"""Cache-safe automatic context handoff artifacts.

The artifact is deliberately metadata-only: the durable SessionDB remains the
source of conversation truth, so this hook never rewrites messages, roles,
tools, or the system prompt.  Compression/rotation is still performed by the
existing pre-model compression path.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Optional


_MAX_HANDOFF_ARTIFACTS = 256


def handoff_artifact_path(*, hermes_home: Path, session_id: str) -> Path:
    """Return a collision-safe path while retaining the original id in JSON."""
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return Path(hermes_home) / "sessions" / "handoffs" / f"{digest}.json"


def handoff_is_due(*, enabled: bool, estimated_tokens: int, threshold_tokens: Any) -> bool:
    """Pure policy predicate used by the pre-model boundary."""
    return bool(
        enabled
        and isinstance(threshold_tokens, int)
        and not isinstance(threshold_tokens, bool)
        and threshold_tokens > 0
        and estimated_tokens >= threshold_tokens
    )


def write_handoff_artifact(
    *,
    hermes_home: Path,
    session_id: Optional[str],
    estimated_tokens: int,
    threshold_tokens: int,
    model: str = "",
    reason: str = "context_threshold",
) -> Optional[Path]:
    """Atomically write a bounded resumable handoff marker.

    No transcript is copied into this file.  This keeps artifact size bounded
    and avoids creating a second, divergent conversation store.  A future
    session can resume the durable session by id; the normal compression path
    performs the actual context reduction before the next model request.
    """
    if (
        not session_id
        or isinstance(threshold_tokens, bool)
        or not isinstance(threshold_tokens, int)
        or threshold_tokens <= 0
        or estimated_tokens < threshold_tokens
    ):
        return None
    path = handoff_artifact_path(hermes_home=Path(hermes_home), session_id=session_id)
    # One marker per session is sufficient. Avoid rewriting/fsyncing it on every
    # qualifying turn; a new session id (rotation) gets a new marker naturally.
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
            if (
                existing.get("session_id") == session_id
                and existing.get("threshold_tokens") == threshold_tokens
            ):
                _cleanup_handoff_artifacts(path.parent)
                return path
        except (OSError, ValueError, TypeError):
            pass
    payload = {
        "version": 1,
        "session_id": session_id,
        "created_at": time.time(),
        "estimated_tokens": int(estimated_tokens),
        "threshold_tokens": int(threshold_tokens),
        "model": str(model or ""),
        "reason": reason,
        "resumable": True,
        "resume_command": f"hermes --resume {session_id}",
        "conversation_source": "SessionDB",
        "note": (
            "Automatic handoff boundary reached. The durable session is the "
            "source of truth; resume it or continue after pre-model compression."
        ),
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
    _cleanup_handoff_artifacts(path.parent)
    return path


def _cleanup_handoff_artifacts(directory: Path) -> None:
    """Keep a bounded recent marker set; SessionDB remains the transcript store."""
    try:
        artifacts = sorted(
            directory.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True
        )
        for stale in artifacts[_MAX_HANDOFF_ARTIFACTS:]:
            try:
                stale.unlink()
            except OSError:
                pass
    except OSError:
        pass
