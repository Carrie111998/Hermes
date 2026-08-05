import sqlite3
from datetime import datetime, timezone

from ai_usage.tokensum import tokensum_provider

NOW = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE session_model_usage ("
        "billing_provider TEXT, input_tokens INT, output_tokens INT, "
        "estimated_cost_usd REAL, last_seen REAL)"
    )
    return conn


def _insert(conn, provider, inp, out, cost, age_seconds):
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?)",
        (provider, inp, out, cost, NOW.timestamp() - age_seconds),
    )


def test_rolling_windows_sum_by_age():
    conn = _db()
    _insert(conn, "kimi-coding", 100, 50, 0.01, 60)          # in 5h/24h/7d
    _insert(conn, "kimi-coding", 200, 100, 0.02, 6 * 3600)   # in 24h/7d only
    _insert(conn, "kimi-coding", 1000, 500, 0.10, 3 * 86400) # in 7d only
    out = tokensum_provider("kimi", "Kimi K3", conn, NOW)
    by = {w["id"]: w for w in out["windows"]}
    assert by["5h"]["tokens"] == 150
    assert by["24h"]["tokens"] == 150 + 300
    assert by["7d"]["tokens"] == 150 + 300 + 1500
    assert by["7d"]["cost_usd"] == 0.13
    assert out["state"] == "ok"
    assert out["detail"] == "5h 150 · wk 1.9k"


def test_alias_matching_is_case_insensitive_substring():
    conn = _db()
    _insert(conn, "Moonshot-Kimi", 10, 5, 0.0, 60)
    out = tokensum_provider("kimi", "Kimi K3", conn, NOW)
    assert out["windows"][0]["tokens"] == 15


def test_no_rows_is_unconfigured():
    conn = _db()
    _insert(conn, "openai-codex", 999, 999, 1.0, 60)  # different provider
    out = tokensum_provider("gemini", "Gemini", conn, NOW)
    assert out["state"] == "unconfigured"
    assert out["detail"] == "no usage yet"
    assert all(w["tokens"] == 0 for w in out["windows"])
