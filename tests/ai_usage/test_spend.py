import sqlite3
from datetime import datetime, timezone

from ai_usage.spend import spend_provider

# Mid-month so "this month" has both an in-window and an out-of-window edge.
NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)


def _db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE session_model_usage ("
        "billing_provider TEXT, model TEXT, input_tokens INT, output_tokens INT, "
        "estimated_cost_usd REAL, last_seen REAL)"
    )
    return conn


def _insert(conn, provider, model, inp, out, age_seconds):
    conn.execute(
        "INSERT INTO session_model_usage VALUES (?,?,?,?,?,?)",
        (provider, model, inp, out, 0.0, NOW.timestamp() - age_seconds),
    )


def test_month_to_date_spend_priced_per_model():
    conn = _db()
    # 0.2M in / 0.1M out on Pro = $1.25
    _insert(conn, "gemini", "gemini-2.5-pro", 200_000, 100_000, 3600)
    # 1M in / 1M out on Flash = $2.80
    _insert(conn, "gemini", "gemini-2.5-flash", 1_000_000, 1_000_000, 7200)
    out = spend_provider("gemini", "Gemini", conn, NOW)
    assert out["mode"] == "spend"
    assert out["state"] == "ok"
    assert out["spend_usd"] == 4.05  # 1.25 + 2.80
    assert out["detail"] == "$4.05 this month"
    assert out["windows"] == []  # spend mode has no per-window bars
    assert out["fetched_at"] == "2026-08-15T12:00:00Z"


def test_rows_before_month_start_are_excluded():
    conn = _db()
    _insert(conn, "gemini", "gemini-2.5-pro", 200_000, 100_000, 3600)      # this month
    _insert(conn, "gemini", "gemini-2.5-pro", 999_000_000, 999_000_000,
            20 * 86400)  # ~Jul 26 → previous month, must not count
    out = spend_provider("gemini", "Gemini", conn, NOW)
    assert out["spend_usd"] == 1.25  # only the in-window row


def test_alias_matching_catches_google_billing_labels():
    conn = _db()
    _insert(conn, "generativelanguage.googleapis.com", "gemini-2.5-flash",
            1_000_000, 1_000_000, 60)
    out = spend_provider("gemini", "Gemini", conn, NOW)
    assert out["state"] == "ok"
    assert out["spend_usd"] == 2.80


def test_no_traffic_is_unconfigured_not_zero_dollars():
    conn = _db()
    _insert(conn, "openai-codex", "gpt-x", 999, 999, 60)  # different provider
    out = spend_provider("gemini", "Gemini", conn, NOW)
    assert out["state"] == "unconfigured"
    assert out["detail"] == "no usage yet"
    assert out["spend_usd"] == 0.0
