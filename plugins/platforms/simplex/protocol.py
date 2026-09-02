"""Pure SimpleX daemon protocol helpers shared by adapter mixins."""

from __future__ import annotations

import json
from typing import Optional


def simplex_payload_len(text: str) -> int:
    """Measure text as serialized in an ``ensure_ascii=False`` JSON string."""
    return len(json.dumps(text, ensure_ascii=False).encode("utf-8")) - 2


def response_type(response: Optional[dict]) -> str:
    return (
        str((response or {}).get("type") or "")
        if isinstance(response, dict)
        else ""
    )


def response_error(response: Optional[dict]) -> Optional[str]:
    """Return a bounded diagnostic for a daemon error response."""
    if not isinstance(response, dict):
        return "SimpleX daemon did not answer"
    kind = response_type(response)
    if kind in {"localCommandOutcomeUnknown", "localCommandNotSubmitted"}:
        return str(response.get("error") or "SimpleX command outcome is unknown")[
            :1000
        ]
    if kind not in {"chatCmdError", "chatError", "chatErrors"}:
        return None
    detail = response.get("chatError") or response.get("chatErrors") or response
    try:
        rendered = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        rendered = str(detail)
    return f"{kind}: {rendered[:1000]}"


def response_item_ids(response: Optional[dict]) -> list[str]:
    """Extract stable daemon chat-item IDs from a command response."""
    if not isinstance(response, dict):
        return []
    wrappers: list[dict] = []
    if isinstance(response.get("chatItems"), list):
        wrappers.extend(
            item for item in response["chatItems"] if isinstance(item, dict)
        )
    if isinstance(response.get("chatItem"), dict):
        wrappers.append(response["chatItem"])
    ids: list[str] = []
    for wrapper in wrappers:
        inner = wrapper.get("chatItem", {}) if isinstance(wrapper, dict) else {}
        meta = inner.get("meta", {}) if isinstance(inner, dict) else {}
        item_id = meta.get("itemId") if isinstance(meta, dict) else None
        if item_id is not None:
            ids.append(str(item_id))
    return ids
