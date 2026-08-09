from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from ai_usage.balance import balance_provider
from ai_usage.budget import budget_provider
from ai_usage.contract import PROVIDERS, iso
from ai_usage.manual_snapshot import MANUAL_PROVIDER_KEYS, read_manual_snapshot
from ai_usage.spend import spend_provider
from ai_usage.tokensum import tokensum_provider


def _carry_forward(prev: Optional[dict], key: str) -> Optional[dict]:
    if not prev:
        return None
    for p in prev.get("providers", []):
        if p.get("key") == key:
            if p.get("state") not in ("ok", "stale"):
                return None
            carried = dict(p)
            carried["state"] = "stale"
            carried.setdefault("source", "hermes")
            return carried
    return None


def _hermes_error_row(key: str, label: str, mode: str) -> dict:
    return {
        "key": key,
        "label": label,
        "mode": mode,
        "source": "hermes",
        "state": "error",
        "windows": [],
        "detail": "db error",
    }


def _state_db_row(
    key: str,
    label: str,
    mode: str,
    conn: Optional[sqlite3.Connection],
    now: datetime,
    prev: Optional[dict],
) -> dict:
    """Build a fresh Hermes row, or preserve a prior row when state.db is unavailable."""
    try:
        if conn is None:
            raise sqlite3.OperationalError("db unavailable")
        make = spend_provider if mode == "spend" else tokensum_provider
        row = make(key, label, conn, now)
    except sqlite3.Error:
        return _carry_forward(prev, key) or _hermes_error_row(key, label, mode)
    row["source"] = "hermes"
    return row


def collect(
    *,
    db_path: str,
    prev: Optional[dict],
    fetch_usage: Callable[[str], object],
    now: Optional[datetime] = None,
    manual_store_path: Optional[str] = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    manual = read_manual_snapshot(manual_store_path, now) if manual_store_path else {}

    conn: Optional[sqlite3.Connection] = None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
        conn.execute("PRAGMA busy_timeout=5000")
    except sqlite3.Error:
        conn = None

    providers: list[dict] = []
    try:
        for key, label, mode in PROVIDERS:
            if key in MANUAL_PROVIDER_KEYS and key in manual:
                providers.append(dict(manual[key]))
                continue

            if mode in ("budget", "balance"):
                make = budget_provider if mode == "budget" else balance_provider
                try:
                    snapshot = fetch_usage(key)
                except Exception:
                    snapshot = None
                if snapshot is None or not getattr(snapshot, "available", False):
                    row = _carry_forward(prev, key)
                    if row is None:
                        row = make(key, label, snapshot)
                        row["source"] = "official"
                else:
                    row = make(key, label, snapshot)
                    row["source"] = "official"
                providers.append(row)
                continue

            providers.append(_state_db_row(key, label, mode, conn, now, prev))
    finally:
        if conn is not None:
            conn.close()

    return {"generated_at": iso(now), "providers": providers}


def write_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        for attempt in range(5):
            try:
                os.replace(tmp, path)
                return
            except PermissionError:
                if attempt == 4:
                    raise
                time.sleep(0.05 * (attempt + 1))
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
