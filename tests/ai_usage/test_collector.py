import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ai_usage.collector import collect, write_atomic

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)


@dataclass
class FakeWin:
    label: str
    used_percent: Optional[float]
    reset_at: Optional[datetime]


@dataclass
class FakeSnap:
    available: bool
    windows: tuple
    fetched_at: datetime = NOW
    unavailable_reason: Optional[str] = None


def _seed_db(path):
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE session_model_usage ("
        "billing_provider TEXT, input_tokens INT, output_tokens INT, "
        "estimated_cost_usd REAL, last_seen REAL)"
    )
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?)",
        ("kimi-coding", 100, 50, 0.01, NOW.timestamp() - 60),
    )
    conn.commit()
    conn.close()


def test_collect_builds_five_providers(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))

    def fetch(provider):
        if provider == "anthropic":
            return FakeSnap(True, (FakeWin("Current session", 62.0, None),))
        # codex + kimi are both budget-mode; unavailable fetch → unconfigured
        return FakeSnap(False, (), unavailable_reason="no token")

    data = collect(db_path=str(db), prev=None, fetch_usage=fetch, now=NOW)
    assert data["generated_at"] == "2026-08-04T15:00:00Z"
    by = {p["key"]: p for p in data["providers"]}
    assert list(by.keys()) == ["anthropic", "openai-codex", "kimi", "gemini", "xai"]
    assert by["anthropic"]["state"] == "ok"
    assert by["openai-codex"]["state"] == "unconfigured"
    # kimi is budget-mode now: routed through fetch, not the state.db token-sum
    assert by["kimi"]["mode"] == "budget"
    assert by["kimi"]["state"] == "unconfigured"
    assert by["gemini"]["state"] == "unconfigured"


def test_collect_carries_forward_last_known_on_fetch_failure(tmp_path):
    db = tmp_path / "state.db"
    _seed_db(str(db))
    prev = {
        "generated_at": "2026-08-04T14:00:00Z",
        "providers": [
            {"key": "anthropic", "label": "Claude", "mode": "budget",
             "state": "ok", "fetched_at": "2026-08-04T14:00:00Z",
             "windows": [{"id": "5h", "label": "5h", "used_pct": 55.0}],
             "detail": "5h 55%"},
        ],
    }

    def fetch(provider):
        return None  # total fetch failure

    data = collect(db_path=str(db), prev=prev, fetch_usage=fetch, now=NOW)
    by = {p["key"]: p for p in data["providers"]}
    assert by["anthropic"]["state"] == "stale"
    assert by["anthropic"]["windows"][0]["used_pct"] == 55.0  # last-known preserved


def test_unconfigured_prev_does_not_carry_forward_as_stale(tmp_path):
    # Prior run had this budget provider as unconfigured; the live fetch is
    # still unavailable this run. It must stay "unconfigured", NOT flip to
    # "stale" (which would imply stale DATA that never existed).
    db = tmp_path / "state.db"
    _seed_db(str(db))
    prev = {
        "generated_at": "2026-08-04T14:00:00Z",
        "providers": [
            {"key": "openai-codex", "label": "Codex", "mode": "budget",
             "state": "unconfigured", "windows": [], "detail": "no data"},
        ],
    }

    def fetch(provider):
        # still unconfigured this run (unavailable with a reason)
        return FakeSnap(False, (), unavailable_reason="no token")

    data = collect(db_path=str(db), prev=prev, fetch_usage=fetch, now=NOW)
    by = {p["key"]: p for p in data["providers"]}
    assert by["openai-codex"]["state"] == "unconfigured"
    assert by["openai-codex"]["state"] != "stale"


def test_write_atomic_roundtrip(tmp_path):
    path = tmp_path / "sub" / "ai-tokens.json"
    write_atomic(path, {"generated_at": "x", "providers": []})
    assert json.loads(path.read_text(encoding="utf-8"))["generated_at"] == "x"
