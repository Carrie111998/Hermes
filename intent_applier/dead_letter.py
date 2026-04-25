"""Move a failed intent file to dead-letter/ with a sidecar describing the error."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


def write_dead_letter(
    intent_file: Path,
    *,
    dead_letter_dir: Path,
    error_class: str,
    error_message: str,
    stack_trace: str,
    retry_count: int,
) -> Path:
    """Move intent_file to dead_letter_dir and write a `.error.json` sidecar.

    Returns the new path of the moved intent file.
    """
    dead_letter_dir.mkdir(parents=True, exist_ok=True)
    dest = dead_letter_dir / intent_file.name
    intent_file.replace(dest)

    sidecar = dead_letter_dir / f"{intent_file.name}.error.json"
    sidecar_data = {
        "error_class": error_class,
        "error_message": error_message,
        "stack_trace": stack_trace,
        "retry_count": retry_count,
        "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        "intent_file": dest.name,
    }
    sidecar.write_text(json.dumps(sidecar_data, indent=2), encoding="utf-8")
    logger.warning(
        "intent dead-lettered: %s (error=%s)", dest.name, error_class,
    )
    return dest
