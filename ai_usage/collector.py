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
            return carried
    return None


def collect(
    *,
    db_path: str,
    prev: Optional[dict],
    fetch_usage: Callable[[str], object],
    now: Optional[datetime] = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    conn.execute("PRAGMA busy_timeout=5000")
    providers: list[dict] = []
    try:
        for key, label, mode in PROVIDERS:
            # budget (%-windows) and balance (outstanding-$) are both HTTP-fetched
            # via fetch_usage and carry forward the last-known dict on failure.
            if mode in ("budget", "balance"):
                make = budget_provider if mode == "budget" else balance_provider
                snap = None
                try:
                    snap = fetch_usage(key)
                except Exception:
                    snap = None
                if snap is None or not getattr(snap, "available", False):
                    providers.append(
                        _carry_forward(prev, key) or make(key, label, snap)
                    )
                else:
                    providers.append(make(key, label, snap))
            else:
                # spend + tokens are both derived from state.db (no HTTP fetch).
                db_make = spend_provider if mode == "spend" else tokensum_provider
                try:
                    providers.append(db_make(key, label, conn, now))
                except sqlite3.Error:
                    providers.append(
                        _carry_forward(prev, key)
                        or {
                            "key": key,
                            "label": label,
                            "mode": mode,
                            "state": "error",
                            "windows": [],
                            "detail": "db error",
                        }
                    )
    finally:
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
