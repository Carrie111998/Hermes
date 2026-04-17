"""hermes events doctor — diagnose notification layer health."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from events.paths import (
    audit_log_path, events_db_path, quiet_hours_path,
    telegram_topics_path, telegram_verbosity_path,
)

REQUIRED_SUBSCRIBERS = [
    "audit-logger", "telegram-notifier", "whatsapp-escalator",
    "digest-composer", "memory-writer", "telegram-mirror",
    "mailbox-translator",
]


def _check(name: str, ok: bool, detail: str = "") -> bool:
    marker = "OK" if ok else "FAIL"
    print(f"[{marker}] {name}{' -- ' + detail if detail else ''}")
    return ok


def run_doctor(check_telegram_api: bool = True) -> int:
    issues = 0

    db = events_db_path()
    if not _check("events db exists", db.exists(), str(db)):
        issues += 1

    if db.exists():
        try:
            conn = sqlite3.connect(str(db))
            conn.execute("SELECT 1 FROM events LIMIT 1")
            _check("events db readable", True)

            cursors = {row[0] for row in conn.execute(
                "SELECT subscriber_id FROM subscriber_cursors")}
            for sub in REQUIRED_SUBSCRIBERS:
                if not _check(f"subscriber cursor: {sub}",
                              sub in cursors, "present" if sub in cursors else "missing"):
                    issues += 1

            since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            cnt = conn.execute(
                "SELECT COUNT(*) FROM events WHERE timestamp > ?", (since,)
            ).fetchone()[0]
            _check(f"events emitted in last 24h", cnt > 0, f"{cnt} events")
            if cnt == 0:
                issues += 1

            conn.close()
        except sqlite3.Error as e:
            _check("events db readable", False, str(e))
            issues += 1

    for label, p in [
        ("topics.json", telegram_topics_path()),
        ("verbosity.json", telegram_verbosity_path()),
        ("quiet_hours.json", quiet_hours_path()),
        ("audit.jsonl", audit_log_path()),
    ]:
        ok = p.exists()
        detail = str(p) if not ok else ""
        if not _check(f"{label}", ok, detail):
            issues += 1

    if check_telegram_api:
        token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        if not token:
            _check("TELEGRAM_BOT_TOKEN env", False, "unset")
            issues += 1
        else:
            try:
                import urllib.request
                with urllib.request.urlopen(
                    f"https://api.telegram.org/bot{token}/getMe", timeout=5
                ) as r:
                    data = json.loads(r.read().decode())
                    _check("telegram getMe", data.get("ok") is True,
                           data.get("result", {}).get("username", ""))
                    if not data.get("ok"):
                        issues += 1
            except Exception as e:
                _check("telegram getMe", False, str(e))
                issues += 1

    print()
    if issues:
        print(f"events doctor: {issues} issue(s) found")
        return 1
    print("events doctor: all checks passed")
    return 0


def _cli() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--no-telegram-api", action="store_true",
                    help="Skip live getMe check")
    ns = ap.parse_args()
    sys.exit(run_doctor(check_telegram_api=not ns.no_telegram_api))


if __name__ == "__main__":
    _cli()
