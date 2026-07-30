from __future__ import annotations

from collections.abc import Mapping
from typing import Any


_NON_TEXT_PART_TYPES = {"image", "image_url", "input_image", "audio", "input_audio"}
_TEXT_PART_TYPES = {"text", "input_text", "output_text", "summary_text"}
_TEXT_KEYS = ("text", "content", "input_text", "output_text", "summary_text")


def _field(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _text_from_part(part: Any) -> str:
    if part is None:
        return ""
    if isinstance(part, str):
        return part

    part_type = str(_field(part, "type") or "").strip().lower()
    if part_type in _NON_TEXT_PART_TYPES:
        return ""
    if part_type and part_type not in _TEXT_PART_TYPES:
        return ""

    for key in _TEXT_KEYS:
        text = _field(part, key)
        if isinstance(text, str):
            return text
    return ""


def flatten_message_text(content: Any, *, sep: str = "\n") -> str:
    """Return the visible text from common chat/Responses message content shapes."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, Mapping):
        # A typed part owns the meaning of all nested fields.  Media payloads
        # are opaque even when a provider happens to put a ``content`` key
        # inside them; only untyped message/envelope shapes recurse.
        if str(content.get("type") or "").strip():
            return _text_from_part(content)
        if "content" in content:
            return flatten_message_text(content.get("content"), sep=sep)
    if isinstance(content, list):
        chunks = [_text_from_part(part) for part in content]
        return sep.join(chunk for chunk in chunks if chunk)

    text = _text_from_part(content)
    if text:
        return text
    if isinstance(content, Mapping):
        return ""
    try:
        return str(content)
    except Exception:
        return ""
