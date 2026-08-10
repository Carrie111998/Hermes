"""Deterministic recovery scan for activations the event path missed.

Events are primary but not exclusive. A dropped event, a dispatcher restart, or
a message written while the subscriber was down would otherwise leave real work
sitting in an inbox indefinitely. This scan is the safety net, and it reaches
its verdict through exactly the same routing table and claim ledger as the
dispatcher — two sources of truth would eventually disagree.

Routing is **filename-only**. Real names look like
``20260810T200409_TAILOR_REQUEST_tracker_3af5f853.json`` and sometimes carry a
sequence segment (``20260412T162256_01_TAILOR_REQUEST_main.json``), so
positional parsing is fragile. Matching against the closed set of known types
is not, and it means a corrupt body can never hide real work.

The message body is opened only to look for a correlation ID, under a byte cap,
and nothing from it is ever copied into an Activation beyond that one field.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .contracts import ROUTES, Activation, route_mailbox
from .store import ActivationStore

#: Only these inbox directories are scanned. An unknown destination is not
#: silently treated as work.
DESTINATIONS: tuple[str, ...] = tuple(sorted({dest for _type, dest in ROUTES}))

#: Known message types, longest first, so TAILOR_MODULE_REQUEST is never read
#: as TAILOR_REQUEST.
_TYPES: tuple[str, ...] = tuple(
    sorted({mtype for mtype, _dest in ROUTES}, key=len, reverse=True)
)

#: Bodies are only probed for a correlation ID.
CORRELATION_READ_BYTES = 64 * 1024


def _message_type(filename: str) -> str | None:
    upper = filename.upper()
    for candidate in _TYPES:
        if candidate in upper:
            return candidate
    return None


def _correlation_id(path: Path) -> str | None:
    try:
        raw = path.read_text(encoding="utf-8", errors="strict")[:CORRELATION_READ_BYTES]
        body: Any = json.loads(raw)
    except (OSError, UnicodeDecodeError, ValueError):
        # A malformed body must not hide work; routing never depended on it.
        return None
    if not isinstance(body, dict):
        return None
    value = body.get("correlation_id")
    return value if isinstance(value, str) and value.strip() else None


def scan_actionable(
    mailbox_root: Path,
    store: ActivationStore,
    *,
    now: float,
    profile: str = "main",
) -> list[Activation]:
    """Return activations for actionable messages nobody currently holds."""
    root = Path(mailbox_root)
    found: list[Activation] = []

    for destination in DESTINATIONS:
        inbox = root / destination / "inbox"
        try:
            entries = sorted(p for p in inbox.iterdir() if p.is_file())
        except OSError:
            continue

        for path in entries:
            if path.suffix.lower() != ".json":
                continue
            message_type = _message_type(path.name)
            if message_type is None:
                continue
            targets = route_mailbox(message_type, destination, {})
            if not targets:
                continue

            message_key = f"{destination}/inbox/{path.name}"
            correlation_id = _correlation_id(path)
            for activity_id in targets:
                if not _is_available(store, message_key, activity_id, now):
                    continue
                found.append(
                    Activation(
                        activity_id=activity_id,
                        profile=profile,
                        message_key=message_key,
                        correlation_id=correlation_id,
                        reason="reconcile",
                    )
                )
    return found


def _is_available(
    store: ActivationStore, message_key: str, activity_id: str, now: float
) -> bool:
    """True when no live claim and no completion covers this work.

    Read-only on purpose: the scan reports what needs doing, the caller decides
    whether to claim it. Claiming here would mean a scan that merely *looks*
    also consumes the work.
    """
    row = store.get(message_key, activity_id)
    if row is None:
        return True
    if row.state == "completed":
        return False
    return (now - (row.claimed_at or 0.0)) > store.lease_seconds
