"""Tests for the installation-wide usage-meter ledger + capture hook."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from plugins import usage_meter_api


@pytest.fixture()
def meter_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "ledger.db"
    # Load plugin modules then force the ledger path.
    mod = usage_meter_api._load()
    mod.ledger.set_db_path_override(db)
    yield db
    mod.ledger.set_db_path_override(None)


def test_append_and_summarize_month_and_all_time(meter_db: Path):
    mod = usage_meter_api._load()
    now = time.time()
    mod.ledger.append_event(
        {
            "ts": now,
            "profile": "default",
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "api_mode": "chat_completions",
            "input_tokens": 100,
            "output_tokens": 20,
            "cache_read_tokens": 900,
            "cache_write_tokens": 0,
            "estimated_cost_usd": 0.001234,
            "pricing_status": "estimated",
            "pricing_source": "official_docs_snapshot",
        }
    )
    mod.ledger.append_event(
        {
            "ts": now,
            "profile": "work",
            "provider": "openai-codex",
            "model": "gpt-5.4",
            "input_tokens": 50,
            "output_tokens": 10,
            "estimated_cost_usd": 0.0,
            "pricing_status": "included",
            "pricing_source": "none",
        }
    )
    mod.ledger.append_event(
        {
            "ts": now,
            "provider": "custom",
            "model": "mystery",
            "input_tokens": 10,
            "output_tokens": 5,
            "estimated_cost_usd": None,
            "pricing_status": "unpriced",
            "pricing_source": "none",
        }
    )

    summary = mod.meter_summary()
    month = summary["month"]["summary"]
    assert month["calls"] == 3
    assert month["input_tokens"] == 160
    assert month["cache_read_tokens"] == 900
    assert month["unpriced_calls"] == 1
    assert month["included_calls"] == 1
    assert month["priced_calls"] == 1
    assert abs(month["estimated_cost_usd"] - 0.001234) < 1e-9
    assert month["has_unpriced"] is True
    # cache hit rate = cache_read / (input + cache_read) across all events
    expected_hit = 900 / (160 + 900)
    assert abs(month["cache_hit_rate"] - expected_hit) < 1e-9

    details = mod.meter_details(scope="all")
    assert details["event_count"] == 3
    assert len(details["routes"]) == 3
    recent = mod.meter_recent(limit=10)
    assert len(recent["events"]) == 3
    # Privacy: no content fields.
    for ev in recent["events"]:
        assert "prompt" not in ev
        assert "content" not in ev
        assert "authorization" not in ev


def test_hook_capture_fail_open_and_records(meter_db: Path, monkeypatch: pytest.MonkeyPatch):
    mod = usage_meter_api._load()

    # Force pricing to a known result without network.
    class _Cost:
        amount_usd = None
        status = "unknown"
        source = "none"

    monkeypatch.setattr(
        "agent.usage_pricing.estimate_usage_cost",
        lambda *a, **k: _Cost(),
    )

    usage_meter_api.on_post_api_request(
        session_id="s1",
        task_id="t1",
        api_request_id="r1",
        platform="cli",
        model="mystery-model",
        provider="custom",
        base_url="",
        api_mode="chat_completions",
        usage={
            "input_tokens": 11,
            "output_tokens": 3,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 0,
        },
        ended_at=time.time(),
    )

    recent = mod.meter_recent(limit=5)["events"]
    assert len(recent) == 1
    assert recent[0]["model"] == "mystery-model"
    assert recent[0]["pricing_status"] == "unpriced"
    assert recent[0]["input_tokens"] == 11

    # Fail-open: even a broken append must not raise.
    def _boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(mod.ledger, "append_event", _boom)
    usage_meter_api.on_post_api_request(
        model="x",
        provider="y",
        usage={"input_tokens": 1, "output_tokens": 1},
    )


def test_skip_empty_usage_callback(meter_db: Path):
    mod = usage_meter_api._load()
    usage_meter_api.on_post_api_request(model="x", provider="y", usage=None)
    assert mod.meter_recent(limit=5)["events"] == []


def test_skip_malformed_usage_callback(meter_db: Path):
    mod = usage_meter_api._load()
    usage_meter_api.on_post_api_request(
        model="gpt-5.6-sol",
        provider="openai",
        usage={"input_tokens": "not-a-number"},
    )
    assert mod.meter_recent(limit=5)["events"] == []
