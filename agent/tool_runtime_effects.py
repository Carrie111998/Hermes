"""Host-only correlation for trusted tool effects.

Effects in this registry are runtime authority, not tool output. They never
enter plugin hooks, model context, transcripts, or durable session messages.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
from collections import OrderedDict
from typing import Any, Iterator, Optional


_CURRENT_KEY: contextvars.ContextVar[
    tuple[str, str, str, str] | None
] = contextvars.ContextVar("hermes_tool_runtime_effect_key", default=None)
_EFFECTS: "OrderedDict[tuple[str, str, str, str], dict[str, Any]]" = (
    OrderedDict()
)
_LOCK = threading.Lock()
_MAX_EFFECTS = 512


@contextlib.contextmanager
def bind_tool_runtime_effect(
    *,
    tool_name: str,
    session_id: str,
    turn_id: str,
    tool_call_id: str,
) -> Iterator[None]:
    key = (
        str(session_id or ""),
        str(turn_id or ""),
        str(tool_call_id or ""),
        str(tool_name or ""),
    )
    token = _CURRENT_KEY.set(key)
    try:
        yield
    finally:
        _CURRENT_KEY.reset(token)


def record_current_tool_runtime_effect(effect: dict[str, Any]) -> bool:
    key = _CURRENT_KEY.get()
    if key is None or not key[2]:
        return False
    with _LOCK:
        _EFFECTS[key] = dict(effect)
        _EFFECTS.move_to_end(key)
        while len(_EFFECTS) > _MAX_EFFECTS:
            _EFFECTS.popitem(last=False)
    return True


def consume_tool_runtime_effect(
    *,
    tool_name: str,
    session_id: Optional[str],
    turn_id: Optional[str],
    tool_call_id: Optional[str],
) -> Optional[dict[str, Any]]:
    if not tool_call_id:
        return None
    key = (
        str(session_id or ""),
        str(turn_id or ""),
        str(tool_call_id),
        str(tool_name or ""),
    )
    with _LOCK:
        effect = _EFFECTS.pop(key, None)
    return dict(effect) if effect is not None else None
