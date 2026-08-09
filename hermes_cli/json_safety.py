"""UTF-8-safe JSON serialization for user-controlled transport payloads."""

from __future__ import annotations

import json
import re
from typing import Any


_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def dumps_utf8_safe(value: Any, **kwargs: Any) -> str:
    """Serialize JSON while preserving valid Unicode and repairing surrogates.

    ``json.dumps(..., ensure_ascii=False)`` can return a Python string that
    still contains lone UTF-16 surrogate code units. The failure arrives one
    step later when an HTTP or WebSocket library encodes that string as UTF-8.
    Keep the normal serialized form unchanged and repair only that invalid
    output, without mutating the caller's object.
    """
    serialized = json.dumps(value, **kwargs)
    try:
        serialized.encode("utf-8")
    except UnicodeEncodeError:
        serialized = _SURROGATE_RE.sub("\ufffd", serialized)
    return serialized
