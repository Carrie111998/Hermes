import json
from datetime import datetime, timezone

from ai_usage.manual_snapshot import MANUAL_STALE_SECONDS, read_manual_snapshot

NOW = datetime(2026, 8, 8, 19, 30, tzinfo=timezone.utc)


def _write(path, data):
    path.write_text(json.dumps(data), encoding="utf-8")


def test_valid_snapshot_is_budget_row(tmp_path):
    path = tmp_path / "ai-usage-manual.json"
    _write(path, {
        "schema_version": 1,
        "providers": {
            "gemini": {"used_pct": 42.0, "saved_at": "2026-08-08T19:30:00Z"},
        },
    })

    result = read_manual_snapshot(str(path), NOW)

    assert "gemini" in result
    gemini = result["gemini"]
    assert gemini["key"] == "gemini"
    assert gemini["label"] == "Gemini"
    assert gemini["mode"] == "budget"
    assert gemini["source"] == "manual"
    assert gemini["state"] == "ok"
    assert gemini["fetched_at"] == "2026-08-08T19:30:00Z"
    assert gemini["windows"] == [{
        "id": "subscription",
        "label": "Subscription",
        "used_pct": 42.0,
    }]
    assert "Manual · Subscription 42%" in gemini["detail"]


def test_resets_at_is_emitted_and_in_detail(tmp_path):
    path = tmp_path / "ai-usage-manual.json"
    _write(path, {
        "schema_version": 1,
        "providers": {
            "gemini": {
                "used_pct": 42.0,
                "resets_at": "2026-08-10T16:00:00Z",
                "saved_at": "2026-08-08T19:30:00Z",
            },
        },
    })

    gemini = read_manual_snapshot(str(path), NOW)["gemini"]

    assert gemini["windows"][0]["resets_at"] == "2026-08-10T16:00:00Z"
    assert "Subscription 42%" in gemini["detail"]


def test_24h_boundary_is_fresh_and_over_is_stale(tmp_path):
    path = tmp_path / "ai-usage-manual.json"
    _write(path, {
        "schema_version": 1,
        "providers": {
            "gemini": {
                "used_pct": 50.0,
                "saved_at": "2026-08-07T19:30:00Z",
            },
        },
    })

    exact_boundary = read_manual_snapshot(str(path), NOW)
    assert MANUAL_STALE_SECONDS == 86400
    assert exact_boundary["gemini"]["state"] == "ok"

    over_boundary = read_manual_snapshot(
        str(path), datetime(2026, 8, 8, 19, 30, 1, tzinfo=timezone.utc)
    )
    assert over_boundary["gemini"]["state"] == "stale"


def test_future_saved_at_is_fresh_under_clock_skew(tmp_path):
    path = tmp_path / "ai-usage-manual.json"
    _write(path, {
        "schema_version": 1,
        "providers": {
            "xai": {"used_pct": 60.0, "saved_at": "2026-08-08T20:30:00Z"},
        },
    })

    result = read_manual_snapshot(str(path), NOW)

    assert result["xai"]["state"] == "ok"


def test_malformed_or_missing_store_is_empty(tmp_path):
    missing = tmp_path / "missing.json"
    assert read_manual_snapshot(str(missing), NOW) == {}

    malformed = tmp_path / "malformed.json"
    malformed.write_text("{ not json", encoding="utf-8")
    assert read_manual_snapshot(str(malformed), NOW) == {}


def test_invalid_utf8_store_is_ignored(tmp_path):
    path = tmp_path / "invalid-utf8.json"
    path.write_bytes(b"\xff\xfe{")

    assert read_manual_snapshot(str(path), NOW) == {}


def test_invalid_records_are_ignored_individually(tmp_path):
    path = tmp_path / "ai-usage-manual.json"
    _write(path, {
        "schema_version": 1,
        "providers": {
            "gemini": {"used_pct": 150.0, "saved_at": "2026-08-08T19:30:00Z"},
            "xai": {"used_pct": 10.0, "saved_at": "2026-08-08T19:30:00Z"},
            "anthropic": {"used_pct": 80.0, "saved_at": "2026-08-08T19:30:00Z"},
        },
    })

    result = read_manual_snapshot(str(path), NOW)

    assert set(result) == {"xai"}


def test_non_finite_pct_is_ignored(tmp_path):
    path = tmp_path / "ai-usage-manual.json"
    path.write_text(
        '{"schema_version":1,"providers":{"gemini":'
        '{"used_pct":NaN,"saved_at":"2026-08-08T19:30:00Z"}}}',
        encoding="utf-8",
    )

    assert read_manual_snapshot(str(path), NOW) == {}


def test_invalid_reset_timestamp_is_ignored_individually(tmp_path):
    path = tmp_path / "ai-usage-manual.json"
    _write(path, {
        "schema_version": 1,
        "providers": {
            "gemini": {
                "used_pct": 42.0,
                "resets_at": "not-a-timestamp",
                "saved_at": "2026-08-08T19:30:00Z",
            },
            "opencode-go": {"used_pct": 10.0, "saved_at": "2026-08-08T19:30:00Z"},
        },
    })

    result = read_manual_snapshot(str(path), NOW)

    assert set(result) == {"opencode-go"}


def test_invalid_schema_and_missing_required_fields_are_empty(tmp_path):
    wrong_schema = tmp_path / "wrong-schema.json"
    _write(wrong_schema, {
        "schema_version": 2,
        "providers": {"gemini": {"used_pct": 42.0, "saved_at": "2026-08-08T19:30:00Z"}},
    })
    assert read_manual_snapshot(str(wrong_schema), NOW) == {}

    missing_used_pct = tmp_path / "missing-used-pct.json"
    _write(missing_used_pct, {
        "schema_version": 1,
        "providers": {"gemini": {"saved_at": "2026-08-08T19:30:00Z"}},
    })
    assert read_manual_snapshot(str(missing_used_pct), NOW) == {}
