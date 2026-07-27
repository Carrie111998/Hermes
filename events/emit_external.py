"""Emit an event onto the bus from a non-Python producer.

Added 2026-07-27. Producers that live outside the gateway process — today
``~/laptop-start.ps1``'s boot summary, tomorrow anything else in PowerShell or
a shell hook — had no supported way to reach the bus, so they posted raw text
straight at a Telegram thread. That bypasses ``events.formatting`` entirely and
lands the one message in the feed with no priority dot, no icon and no
source/timestamp header (the 2026-07-27 operator report).

Usage::

    <payload-json> | python -m events.emit_external \
        --type boot_summary --source laptop-start [--priority high]

Payload JSON arrives on stdin so callers never have to quote a dict through a
shell. Exits 0 on success and prints the event_id; exits non-zero with a
message on stderr so the caller can fall back to its own delivery path.
"""

from __future__ import annotations

import argparse
import json
import sys

from events.bus import EventBus
from events.schema import EventType, Priority


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="events.emit_external")
    parser.add_argument("--type", required=True,
                        help="EventType string, e.g. boot_summary")
    parser.add_argument("--source", required=True,
                        help="Producer name shown in the message header")
    parser.add_argument("--priority", default=None,
                        help="Override the type's default priority "
                             "(low|normal|high|critical)")
    args = parser.parse_args(argv)

    event_type = EventType.from_string(args.type)
    if event_type is None:
        print(f"unknown event type: {args.type}", file=sys.stderr)
        return 2

    priority = None
    if args.priority:
        # Priority.from_string silently falls back to NORMAL, which would turn
        # a typo into a quietly-downgraded alert. Validate against the labels.
        label = args.priority.strip().lower()
        if label not in {p.label for p in Priority}:
            print(f"unknown priority: {args.priority}", file=sys.stderr)
            return 2
        priority = Priority.from_string(label)

    # Read bytes and decode utf-8-sig: Windows PowerShell 5.1 prefixes a UTF-8
    # BOM when it pipes a string to a native exe, and json.loads rejects it.
    # Falls back to sys.stdin for tests/callers that patch a text stream.
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is not None:
        raw = buffer.read().decode("utf-8-sig", errors="replace").strip()
    else:
        raw = sys.stdin.read().lstrip("﻿").strip()
    if not raw:
        print("no payload on stdin", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except ValueError as exc:
        print(f"payload is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(payload, dict):
        print("payload must be a JSON object", file=sys.stderr)
        return 2

    event_id = EventBus().emit(
        event_type=event_type,
        source=args.source,
        payload=payload,
        priority=priority,
    )
    print(event_id)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(main())
