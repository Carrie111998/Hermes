from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from ai_usage.balance import balance_provider

FETCHED = datetime(2026, 8, 5, 15, 0, tzinfo=timezone.utc)


@dataclass
class FakeSnap:
    available: bool
    balance_usd: Optional[float] = None
    fetched_at: datetime = FETCHED
    unavailable_reason: Optional[str] = None


def test_healthy_balance_renders_dollars():
    out = balance_provider("deepseek", "DeepSeek", FakeSnap(True, balance_usd=9.744))
    assert out["mode"] == "balance"
    assert out["state"] == "ok"
    assert out["balance_usd"] == 9.74  # rounded to cents
    assert out["detail"] == "$9.74 left"
    assert out["windows"] == []  # balance mode has no per-window bars
    assert out["fetched_at"] == "2026-08-05T15:00:00Z"


def test_zero_balance_is_ok_state_not_error():
    # A drained key still fetched successfully — it's $0.00, not "no data".
    out = balance_provider("deepseek", "DeepSeek", FakeSnap(True, balance_usd=0.0))
    assert out["state"] == "ok"
    assert out["balance_usd"] == 0.0
    assert out["detail"] == "$0.00 left"


def test_unavailable_snapshot_is_unconfigured():
    snap = FakeSnap(False, unavailable_reason="no deepseek key")
    out = balance_provider("deepseek", "DeepSeek", snap)
    assert out["state"] == "unconfigured"
    assert out["windows"] == []


def test_none_snapshot_is_error():
    out = balance_provider("deepseek", "DeepSeek", None)
    assert out["state"] == "error"
    assert out["detail"] == "no data"
