"""Structured action buttons on cron deliveries (issue #78999).

Cron workers never receive the ``clarify`` toolset — jobs must not block
waiting for a human. This module is the delivery-time alternative:

1. Parse an optional last-line JSON envelope from the cron final response.
2. Fall back to a per-job ``delivery_choices`` list.
3. Register pending buttons (no wait in ``run_job``).
4. A tap resolves to the choice text so the adapter can inject it as a
   user turn in the continuable session.

Stale taps (expired, superseded by a newer delivery for the same job)
fail visibly rather than silently accepting.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)

DELIVERY_CHOICES_KEY = "delivery_choices"
TTL_SECONDS = 6 * 3600
MAX_CHOICES = 8
STALE_HINT = "This action expired. Wait for the next preview."

_lock = threading.Lock()
_entries: dict[str, "DeliveryChoiceEntry"] = {}


@dataclass
class DeliveryChoiceEntry:
    choices: list[str]
    job_id: str
    created_at: float
    expires_at: float


def normalize_delivery_choices(raw: Any) -> Optional[list[str]]:
    """Return a cleaned choice list, or None when the field is absent/empty."""
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return None
    choices: list[str] = []
    for item in raw:
        text = str(item).strip()
        if text:
            choices.append(text)
        if len(choices) >= MAX_CHOICES:
            break
    return choices or None


def _parse_envelope_line(line: str) -> Optional[tuple[bool, Optional[list[str]]]]:
    """Return (True, choices|None) when ``line`` is a delivery_choices object.

    ``choices`` is None when the envelope explicitly lists no buttons.
    """
    candidate = (line or "").strip()
    if not candidate.startswith("{") or not candidate.endswith("}"):
        return None
    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict) or DELIVERY_CHOICES_KEY not in parsed:
        return None
    return True, normalize_delivery_choices(parsed.get(DELIVERY_CHOICES_KEY))


def split_delivery_choices(content: str) -> tuple[str, Optional[list[str]], bool]:
    """Strip a last-line envelope if present.

    Returns ``(visible_text, choices, saw_envelope)``. Invalid JSON, or JSON
    without ``delivery_choices``, is left untouched so a JSON preview body is
    never eaten.
    """
    if not content:
        return content, None, False
    lines = content.splitlines()
    last_idx = None
    for i in range(len(lines) - 1, -1, -1):
        if lines[i].strip():
            last_idx = i
            break
    if last_idx is None:
        return content, None, False
    parsed = _parse_envelope_line(lines[last_idx])
    if parsed is None:
        return content, None, False
    _, choices = parsed
    kept = lines[:last_idx]
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join(kept), choices, True


def resolve_delivery_choices(
    content: str,
    job: Optional[dict] = None,
) -> tuple[str, Optional[list[str]]]:
    """Envelope wins; job ``delivery_choices`` is fallback when no envelope."""
    cleaned, envelope_choices, saw_envelope = split_delivery_choices(content or "")
    if saw_envelope:
        return cleaned, envelope_choices
    return cleaned, normalize_delivery_choices((job or {}).get(DELIVERY_CHOICES_KEY))


def new_delivery_id() -> str:
    return uuid.uuid4().hex[:12]


def register_delivery_choices(
    delivery_id: str,
    choices: list[str],
    job_id: str,
    *,
    ttl_seconds: int = TTL_SECONDS,
    now: Optional[float] = None,
) -> str:
    """Store pending buttons. Supersedes earlier deliveries for the same job."""
    cleaned = normalize_delivery_choices(choices)
    if not cleaned:
        raise ValueError("delivery_choices must be a non-empty list of strings")
    ts = time.time() if now is None else now
    with _lock:
        _supersede_job_locked(job_id)
        _entries[delivery_id] = DeliveryChoiceEntry(
            choices=list(cleaned),
            job_id=str(job_id or ""),
            created_at=ts,
            expires_at=ts + max(1, int(ttl_seconds)),
        )
    return delivery_id


def resolve_delivery_choice(
    delivery_id: str,
    index: int,
    *,
    now: Optional[float] = None,
) -> Optional[str]:
    """Pop and return the choice text, or None when missing/stale/bad index."""
    ts = time.time() if now is None else now
    with _lock:
        entry = _entries.pop(delivery_id, None)
        if entry is None:
            return None
        if ts >= entry.expires_at:
            return None
        if index < 0 or index >= len(entry.choices):
            return None
        return entry.choices[index]


def peek_delivery_choices(delivery_id: str) -> Optional[list[str]]:
    with _lock:
        entry = _entries.get(delivery_id)
        if entry is None:
            return None
        return list(entry.choices)


def supersede_job_deliveries(job_id: str) -> int:
    with _lock:
        return _supersede_job_locked(job_id)


def _supersede_job_locked(job_id: str) -> int:
    if not job_id:
        return 0
    stale = [key for key, entry in _entries.items() if entry.job_id == job_id]
    for key in stale:
        _entries.pop(key, None)
    return len(stale)


def clear_delivery_choices() -> None:
    """Test helper."""
    with _lock:
        _entries.clear()
