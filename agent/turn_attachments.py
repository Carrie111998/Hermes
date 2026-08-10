"""Task-local attachments supplied by trusted plugins for a turn's final reply."""
from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Iterable
from urllib.parse import urlsplit

_FINAL_REPLY_METADATA_KEY = "_hermes_final_reply_metadata"


@dataclass
class _Collector:
    metadata: dict[str, Any] = field(default_factory=dict)
    lock: Lock = field(default_factory=Lock)


_current: ContextVar[_Collector | None] = ContextVar("hermes_turn_attachments", default=None)


def begin_turn() -> Token:
    """Bind a fresh collector to the current task and its child work."""
    return _current.set(_Collector())


def end_turn(token: Token) -> None:
    _current.reset(token)


def attach_final_reply_link_buttons(buttons: Iterable[dict[str, Any]]) -> bool:
    """Attach validated link buttons to this turn's final platform send.

    This function is intentionally not a model tool. It is exposed only through
    PluginContext, so model-produced JSON cannot create outbound metadata.
    """
    collector = _current.get()
    if collector is None:
        return False
    validated: list[dict[str, Any]] = []
    for raw in buttons:
        if not isinstance(raw, dict) or set(raw) - {"text", "kind", "url", "row"}:
            raise ValueError("invalid final reply link button")
        text = str(raw.get("text") or "").strip()
        kind = str(raw.get("kind") or "")
        url = str(raw.get("url") or "").strip()
        parsed = urlsplit(url)
        if (
            not text or len(text) > 64 or kind not in {"web_app", "url"}
            or parsed.scheme.lower() != "https" or not parsed.hostname
            or parsed.username or parsed.password
        ):
            raise ValueError("invalid final reply link button")
        try:
            row = int(raw.get("row") or 0)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid final reply link button row") from exc
        if row < 0 or row > 3:
            raise ValueError("invalid final reply link button row")
        validated.append({"text": text, "kind": kind, "url": url, "row": row})
    if not validated or len(validated) > 4:
        raise ValueError("final reply link buttons must contain 1..4 items")
    with collector.lock:
        # One-shot replace semantics avoid duplicates when a tool retries in-turn.
        collector.metadata["link_buttons"] = validated
    return True


def snapshot() -> dict[str, Any]:
    collector = _current.get()
    if collector is None:
        return {}
    with collector.lock:
        return {key: list(value) if isinstance(value, list) else value for key, value in collector.metadata.items()}
