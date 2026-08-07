"""Input-size caps for tui_gateway transports and command fields.

Two-layer defense against denial-of-service via oversized payloads:

1. **Transport frame cap** — enforced at the WebSocket / stdio read boundary,
   *before* ``json.loads``. A 1 MB frame still costs 1 MB of memory if we
   decode it before checking, so the rejection has to happen at byte
   accumulation. ``check_frame_size(raw_bytes)`` is called from the WS
   receive loop and the stdio read loops in both ``entry.py`` and
   ``slash_worker.py``.

2. **Command-field cap** — enforced inside the JSON-RPC handler bodies for
   ``command.dispatch``, ``slash.exec``, and ``shell.exec``. Even with the
   transport cap, an in-spec frame can carry pathological field values (a
   900 KB ``arg`` for a quick-command subprocess, say). ``check_field`` is
   the shared validator wired into all three handlers via
   ``methods_tools.register``'s imports.

Bytes, not code points: emoji-heavy payloads inflate under UTF-8, so a
4096-character ``len()`` check is a 16 KB+ memory floor before rejection.
We measure ``len(value.encode("utf-8"))`` consistently.

Both limits are env-overridable for ops debugging without code changes:
``HERMES_TUI_MAX_FRAME_BYTES``, ``HERMES_TUI_MAX_FIELD_BYTES``.
"""

from __future__ import annotations

import os
from typing import Final


def _env_positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


# 1 MB transport frame. Big enough for skin payloads + reasonable tool
# attachments, small enough that one attack frame can't pin the gateway.
MAX_FRAME_BYTES: Final[int] = _env_positive_int("HERMES_TUI_MAX_FRAME_BYTES", 1 * 1024 * 1024)

# 64 KB single command field. A long prompt argument fits; a 100 MB
# shell-piped blob doesn't.
MAX_FIELD_BYTES: Final[int] = _env_positive_int("HERMES_TUI_MAX_FIELD_BYTES", 64 * 1024)


class FrameTooLarge(ValueError):
    """Raised when a transport frame exceeds ``MAX_FRAME_BYTES``.

    Caught at the WS / stdio boundary; the connection is closed with a
    protocol-level error rather than a Python traceback leak.
    """

    def __init__(self, size: int, limit: int) -> None:
        super().__init__(f"frame too large: {size} bytes (limit {limit})")
        self.size = size
        self.limit = limit


class FieldTooLarge(ValueError):
    """Raised when a command-field value exceeds ``MAX_FIELD_BYTES``.

    Caught inside the JSON-RPC handler; the caller converts this to a
    standard ``{"error": {"code": 4000, "message": "input too long"}}``
    envelope so clients see a normal protocol response instead of a 500.
    """

    def __init__(self, name: str, size: int, limit: int) -> None:
        super().__init__(f"{name} too large: {size} bytes (limit {limit})")
        self.name = name
        self.size = size
        self.limit = limit


def check_frame_size(raw_bytes: bytes) -> None:
    """Reject a raw transport frame whose byte length exceeds the cap.

    Call BEFORE ``json.loads`` (or any other decoder). Callers may also
    pass ``memoryview`` / ``bytearray`` — both implement ``__len__`` over
    the underlying buffer, which is what we want here.
    """
    size = len(raw_bytes)
    if size > MAX_FRAME_BYTES:
        raise FrameTooLarge(size, MAX_FRAME_BYTES)


def check_field(name: str, value: str, *, kind: str = "field") -> None:
    """Reject a string field whose UTF-8 byte length exceeds the cap.

    ``name`` is the JSON-RPC field name (``"arg"``, ``"command"``,
    ``"name"``) for error messages. ``kind`` lets callers tag the error
    context (e.g. ``"shell.exec"``) without changing the validator
    signature.
    """
    if value is None:
        return
    size = len(value.encode("utf-8"))
    if size > MAX_FIELD_BYTES:
        raise FieldTooLarge(name, size, MAX_FIELD_BYTES)
