"""RPC coverage for usage.meter.* methods."""

from __future__ import annotations

from pathlib import Path

import pytest

import tui_gateway.server as server

from plugins import usage_meter_api


def _call(method, params=None):
    handler = server._methods[method]
    resp = handler(1, params or {})
    assert "error" not in resp, resp.get("error")
    return resp["result"]


@pytest.fixture()
def meter_db(tmp_path: Path):
    mod = usage_meter_api._load()
    db = tmp_path / "ledger.db"
    mod.ledger.set_db_path_override(db)
    yield db
    mod.ledger.set_db_path_override(None)


def test_usage_meter_methods_registered():
    for name in ("usage.meter.summary", "usage.meter.details", "usage.meter.recent"):
        assert name in server._methods


def test_usage_meter_summary_rpc(meter_db: Path):
    mod = usage_meter_api._load()
    mod.ledger.append_event(
        {
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "input_tokens": 100,
            "output_tokens": 10,
            "cache_read_tokens": 400,
            "estimated_cost_usd": 0.0005,
            "pricing_status": "estimated",
            "pricing_source": "official_docs_snapshot",
        }
    )
    result = _call("usage.meter.summary", {})
    assert result["month"]["summary"]["calls"] == 1
    assert result["month"]["summary"]["cache_read_tokens"] == 400
    assert "caveat" in result


def test_usage_meter_details_and_recent_rpc(meter_db: Path):
    mod = usage_meter_api._load()
    mod.ledger.append_event(
        {
            "provider": "custom",
            "model": "mystery",
            "input_tokens": 5,
            "output_tokens": 1,
            "pricing_status": "unpriced",
        }
    )
    details = _call("usage.meter.details", {"scope": "all"})
    assert details["event_count"] == 1
    assert details["routes"][0]["unpriced_calls"] == 1

    bad = server._methods["usage.meter.details"](3, {"scope": "week"})
    assert "error" in bad
    assert bad["error"]["code"] == 5075

    recent = _call("usage.meter.recent", {"limit": 10})
    assert len(recent["events"]) == 1
