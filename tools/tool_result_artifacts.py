"""Keep oversized tool results out of the active model context.

The full result is written beneath HERMES_HOME and the model receives a
bounded preview plus a local reference it can page through with read_file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


DEFAULT_MAX_CONTEXT_CHARS = 12_000
DEFAULT_PREVIEW_CHARS = 3_000


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _load_limits() -> tuple[bool, int, int]:
    try:
        from hermes_cli.config import load_config

        config = load_config() or {}
        raw = config.get("tool_result_context") or {}
    except Exception:
        raw = {}
    if not isinstance(raw, dict):
        raw = {}
    enabled = raw.get("enabled", True) is not False
    max_chars = _positive_int(
        raw.get("max_chars"), DEFAULT_MAX_CONTEXT_CHARS
    )
    preview_chars = min(
        max_chars,
        _positive_int(raw.get("preview_chars"), DEFAULT_PREVIEW_CHARS),
    )
    return enabled, max_chars, preview_chars


def _safe_segment(value: str, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", value or "").strip("-._")
    return (cleaned[:80] or fallback)


def _artifact_root() -> Path:
    try:
        from hermes_constants import get_hermes_home

        return Path(get_hermes_home()) / "artifacts" / "tool-results"
    except Exception:
        return Path(tempfile.gettempdir()) / "hermes-tool-results"


def _looks_like_error(result: str) -> bool:
    try:
        parsed = json.loads(result)
    except (TypeError, ValueError):
        return False
    if not isinstance(parsed, dict):
        return False
    try:
        http_status = int(parsed.get("httpStatus") or 0)
    except (TypeError, ValueError):
        http_status = 0
    return (
        parsed.get("success") is False
        or bool(parsed.get("error"))
        or http_status >= 400
    )


def externalize_large_tool_result(
    *,
    tool_name: str,
    result: Any,
    session_id: str = "",
    task_id: str = "",
    tool_call_id: str = "",
) -> Any:
    """Return a bounded structured reference when ``result`` is oversized."""
    if not isinstance(result, str):
        return result
    enabled, max_chars, preview_chars = _load_limits()
    if not enabled or len(result) <= max_chars or _looks_like_error(result):
        return result

    digest = hashlib.sha256(result.encode("utf-8", errors="replace")).hexdigest()[:16]
    owner = _safe_segment(session_id or task_id, "shared")
    call = _safe_segment(tool_call_id, digest)
    directory = _artifact_root() / owner
    path = directory / f"{call}-{digest}.txt"

    try:
        directory.mkdir(parents=True, exist_ok=True)
        try:
            directory.chmod(0o700)
        except OSError:
            pass
        fd, temporary = tempfile.mkstemp(prefix=".tool-result-", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(result)
            os.replace(temporary, path)
            try:
                path.chmod(0o600)
            except OSError:
                pass
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        return result

    head_chars = max(1, preview_chars * 2 // 3)
    tail_chars = max(0, preview_chars - head_chars)
    preview = result[:head_chars]
    if tail_chars:
        preview += (
            f"\n... [{len(result) - preview_chars:,} chars stored outside context] ...\n"
            + result[-tail_chars:]
        )

    return json.dumps(
        {
            "success": True,
            "tool": tool_name,
            "externalized": True,
            "originalChars": len(result),
            "sha256": digest,
            "artifactRef": str(path),
            "preview": preview,
            "nextAction": (
                "Use read_file with artifactRef and offset/limit only if more "
                "detail is required."
            ),
        },
        ensure_ascii=False,
    )
