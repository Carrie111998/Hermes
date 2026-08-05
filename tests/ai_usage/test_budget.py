from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from ai_usage.budget import budget_provider


@dataclass
class FakeWin:
    label: str
    used_percent: Optional[float]
    reset_at: Optional[datetime]


@dataclass
class FakeSnap:
    available: bool
    windows: tuple
    fetched_at: datetime = datetime(2026, 8, 4, 15, 0, tzinfo=timezone.utc)
    unavailable_reason: Optional[str] = None


def test_maps_anthropic_windows_and_detail():
    snap = FakeSnap(
        available=True,
        windows=(
            FakeWin("Current session", 62.4, datetime(2026, 8, 4, 18, 0, tzinfo=timezone.utc)),
            FakeWin("Current week", 40.0, None),
        ),
    )
    # tz injected for a deterministic reset label (2026-08-04 is a Tuesday, 18:00Z).
    out = budget_provider("anthropic", "Claude", snap, tz=timezone.utc)
    assert out["mode"] == "budget" and out["state"] == "ok"
    ids = {w["id"]: w for w in out["windows"]}
    assert ids["5h"]["used_pct"] == 62.4
    assert ids["5h"]["resets_at"] == "2026-08-04T18:00:00Z"
    assert "resets_at" not in ids["wk"]  # None reset omitted
    # 5h has a reset → parenthetical; wk reset is None → no parenthetical.
    assert out["detail"] == "5h 62% (Tue 8/4 6pm) · wk 40%"
    assert out["fetched_at"] == "2026-08-04T15:00:00Z"


def test_detail_includes_reset_times_in_local_tz():
    # Reproduces the user's requested format exactly, in UTC-7.
    tz = timezone(timedelta(hours=-7))
    snap = FakeSnap(
        available=True,
        windows=(
            FakeWin("Session", 10.0, datetime(2026, 8, 6, 17, 0, tzinfo=timezone.utc)),   # Thu 8/6 10am
            FakeWin("Weekly", 40.0, datetime(2026, 8, 8, 3, 0, tzinfo=timezone.utc)),      # Fri 8/7 8pm
        ),
    )
    out = budget_provider("openai-codex", "Codex", snap, tz=tz)
    assert out["detail"] == "5h 10% (Thu 8/6 10am) · wk 40% (Fri 8/7 8pm)"


def test_detail_omits_reset_paren_when_reset_absent():
    tz = timezone(timedelta(hours=-7))
    snap = FakeSnap(available=True, windows=(FakeWin("Session", 10.0, None),))
    out = budget_provider("openai-codex", "Codex", snap, tz=tz)
    assert out["detail"] == "5h 10%"


def test_detail_off_the_hour_reset_keeps_minutes():
    tz = timezone.utc
    snap = FakeSnap(
        available=True,
        windows=(FakeWin("Session", 5.0, datetime(2026, 8, 5, 8, 59, tzinfo=timezone.utc)),),
    )
    out = budget_provider("kimi", "Kimi K3", snap, tz=tz)
    assert out["detail"] == "5h 5% (Wed 8/5 8:59am)"


def test_skips_windows_with_no_percent_or_unknown_label():
    snap = FakeSnap(
        available=True,
        windows=(FakeWin("Mystery", 10.0, None), FakeWin("Session", None, None)),
    )
    out = budget_provider("openai-codex", "Codex", snap)
    assert out["windows"] == []
    assert out["detail"] == "ok"


def test_unavailable_snapshot_is_unconfigured():
    snap = FakeSnap(available=False, windows=(), unavailable_reason="no oauth token")
    out = budget_provider("anthropic", "Claude", snap)
    assert out["state"] == "unconfigured"
    assert out["windows"] == []


def test_none_snapshot_is_error():
    out = budget_provider("anthropic", "Claude", None)
    assert out["state"] == "error"
