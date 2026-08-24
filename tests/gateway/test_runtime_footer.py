"""Unit tests for gateway.runtime_footer — the opt-in runtime-metadata footer
appended to final gateway replies."""

from __future__ import annotations

import os

import pytest

from gateway.runtime_footer import (
    _home_relative_cwd,
    _model_short,
    build_footer_line,
    format_runtime_footer,
    resolve_footer_config,
)


# ---------------------------------------------------------------------------
# _model_short + _home_relative_cwd
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "model,expected",
    [
        ("openai/gpt-5.4", "gpt-5.4"),
        ("anthropic/claude-sonnet-4.6", "claude-sonnet-4.6"),
        ("gpt-5.4", "gpt-5.4"),
        ("", ""),
        (None, ""),
    ],
)
def test_model_short_drops_vendor_prefix(model, expected):
    assert _model_short(model) == expected


def test_home_relative_cwd_collapses_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    sub = tmp_path / "projects" / "hermes"
    sub.mkdir(parents=True)
    result = _home_relative_cwd(str(sub))
    assert result == "~/projects/hermes"


# ---------------------------------------------------------------------------
# format_runtime_footer
# ---------------------------------------------------------------------------

def test_format_footer_all_fields(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path / "projects" / "hermes"))
    (tmp_path / "projects" / "hermes").mkdir(parents=True)
    out = format_runtime_footer(
        model="openrouter/openai/gpt-5.4",
        context_tokens=68000,
        context_length=100000,
        cwd=None,  # falls back to TERMINAL_CWD env var
        fields=("model", "context_pct", "cwd"),
    )
    assert out == "gpt-5.4 · 68% · ~/projects/hermes"


def test_format_footer_skips_missing_context_length():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=500,
        context_length=None,
        cwd="/tmp/wd",
        fields=("model", "context_pct", "cwd"),
    )
    # context_pct dropped silently; no "?%" artifact
    assert "%" not in out
    assert "gpt-5.4" in out
    assert "/tmp/wd" in out


# ---------------------------------------------------------------------------
# resolve_footer_config
# ---------------------------------------------------------------------------


def test_resolve_platform_override_wins():
    user = {
        "display": {
            "runtime_footer": {"enabled": True, "fields": ["model"]},
            "platforms": {
                "slack": {"runtime_footer": {"enabled": False}},
            },
        },
    }
    # Telegram picks up the global enable
    assert resolve_footer_config(user, "telegram")["enabled"] is True
    # Slack overrides to off
    assert resolve_footer_config(user, "slack")["enabled"] is False


def test_resolve_platform_can_add_fields_only():
    user = {
        "display": {
            "runtime_footer": {"enabled": True},
            "platforms": {
                "discord": {"runtime_footer": {"fields": ["context_pct"]}},
            },
        },
    }
    tg = resolve_footer_config(user, "telegram")
    assert tg["enabled"] is True
    assert tg["fields"] == ["model", "context_pct", "cwd"]
    dc = resolve_footer_config(user, "discord")
    assert dc["enabled"] is True
    assert dc["fields"] == ["context_pct"]


# ---------------------------------------------------------------------------
# build_footer_line — top-level entry point used by gateway/run.py
# ---------------------------------------------------------------------------


def test_build_footer_per_platform_off_suppresses():
    user = {
        "display": {
            "runtime_footer": {"enabled": True},
            "platforms": {"slack": {"runtime_footer": {"enabled": False}}},
        },
    }
    out = build_footer_line(
        user_config=user,
        platform_key="slack",
        model="openai/gpt-5.4",
        context_tokens=10, context_length=100,
        cwd="/tmp",
    )
    assert out == ""



# ---------------------------------------------------------------------------
# latency — opt-in wall-clock turn duration
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0.0, "<1s"),
        (0.4, "<1s"),
        (0.999, "<1s"),
        (1.0, "1s"),
        (22.0, "22s"),
        (22.4, "22s"),
        (59.4, "59s"),
        (59.6, "1m00s"),
        (60.0, "1m00s"),
        (65.0, "1m05s"),
        (125.0, "2m05s"),
        (3600.0, "60m00s"),
    ],
)
def test_format_latency(seconds, expected):
    from gateway.runtime_footer import _format_latency

    assert _format_latency(seconds) == expected


def test_format_footer_latency_renders():
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=22.0,
        fields=("latency",),
    )
    assert out == "22s"


def test_format_footer_latency_skipped_when_unmeasured():
    """A call site that doesn't measure timing leaves the field out entirely."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=None,
        fields=("latency",),
    )
    assert out == ""


def test_format_footer_latency_skipped_when_negative():
    """A nonsensical (negative) duration is dropped rather than rendered."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=-1.0,
        fields=("latency",),
    )
    assert out == ""


def test_format_footer_latency_zero_renders_sub_second():
    """Zero is a real measurement (a very fast turn), not missing data."""
    out = format_runtime_footer(
        model="m",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=0.0,
        fields=("latency",),
    )
    assert out == "<1s"


def test_format_footer_latency_in_field_order(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=68_000,
        context_length=100_000,
        cwd=str(tmp_path),
        turn_seconds=65.0,
        fields=("model", "context_pct", "latency", "cwd"),
    )
    assert out == "gpt-5.4 · 68% · 1m05s · ~"


def test_build_footer_line_threads_turn_seconds(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = build_footer_line(
        user_config={
            "display": {
                "runtime_footer": {
                    "enabled": True,
                    "fields": ["model", "latency"],
                }
            }
        },
        platform_key="discord",
        model="gpt-5.4",
        context_tokens=0,
        context_length=None,
        cwd="",
        turn_seconds=22.0,
    )
    assert out == "gpt-5.4 · 22s"


# ---------------------------------------------------------------------------
# Byte-stability: `latency` is opt-in, so the DEFAULT footer is unchanged.
#
# Upstream doctrine: a system prompt / rendered surface must be byte-stable for
# the life of a conversation.  Adding a field to _DEFAULT_FIELDS would silently
# change the footer text of every user who already enabled it.  These tests pin
# the default set and the exact default-config output strings.
# ---------------------------------------------------------------------------

_LEGACY_DEFAULT_FIELDS = ["model", "context_pct", "cwd"]


def test_latency_not_in_default_fields():
    from gateway.runtime_footer import _DEFAULT_FIELDS

    assert "latency" not in _DEFAULT_FIELDS
    assert list(_DEFAULT_FIELDS) == _LEGACY_DEFAULT_FIELDS


def test_resolve_footer_config_default_fields_exclude_latency():
    assert resolve_footer_config({}, "telegram")["fields"] == _LEGACY_DEFAULT_FIELDS
    assert resolve_footer_config(
        {"display": {"runtime_footer": {"enabled": True}}}, "discord"
    )["fields"] == _LEGACY_DEFAULT_FIELDS


@pytest.mark.parametrize(
    "model,tokens,window,cwd,expected",
    [
        ("openai/gpt-5.4", 50_247, 1_000_000, "/var/data", "gpt-5.4 · 5% · /var/data"),
        ("claude-opus-4-8", 68_000, 100_000, "/var/data", "claude-opus-4-8 · 68% · /var/data"),
        ("m", 0, None, "/var/data", "m · /var/data"),
        ("", 10, 100, "/var/data", "10% · /var/data"),
        ("m", 10, 100, "", "m · 10%"),
    ],
)
def test_default_footer_renders_byte_identically(
    monkeypatch, model, tokens, window, cwd, expected
):
    """Default-config output is byte-for-byte what it was before `latency`.

    Note `turn_seconds` IS supplied — proving that even when the caller
    measures timing, a default-configured footer does not show it.
    """
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = format_runtime_footer(
        model=model,
        context_tokens=tokens,
        context_length=window,
        cwd=cwd,
        turn_seconds=22.0,
        # fields deliberately NOT passed — exercises the default.
    )
    assert out == expected


def test_default_build_footer_line_ignores_turn_seconds(monkeypatch):
    """build_footer_line with default fields is unaffected by turn_seconds."""
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    common = dict(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        platform_key="discord",
        model="openai/gpt-5.4",
        context_tokens=50_247,
        context_length=1_000_000,
        cwd="/var/data",
    )
    baseline = build_footer_line(**common)
    with_timing = build_footer_line(**common, turn_seconds=125.0)
    assert baseline == "gpt-5.4 · 5% · /var/data"
    assert with_timing == baseline


def test_rate_tier_not_in_default_fields():
    """rate_tier is opt-in — default field set stays byte-identical."""
    from gateway.runtime_footer import _DEFAULT_FIELDS

    assert "rate_tier" not in _DEFAULT_FIELDS
    assert list(_DEFAULT_FIELDS) == _LEGACY_DEFAULT_FIELDS


def test_rate_tier_renders_only_for_matching_models(monkeypatch):
    """rate_tier appears for models matching a configured window, skipped otherwise."""
    import datetime as dt

    from gateway.runtime_footer import format_runtime_footer, rate_tier_for_model

    # deterministic time: inside the built-in DeepSeek peak window (UTC 08:00 → SGT 16:00)
    monkeypatch.setattr(
        "gateway.runtime_footer._dt.datetime",
        _FakeDateTime(dt.datetime(2026, 8, 20, 8, 30, tzinfo=dt.timezone.utc)),
    )

    ds = format_runtime_footer(
        model="deepseek/deepseek-v4-flash",
        context_tokens=10_000,
        context_length=100_000,
        fields=("model", "context_pct", "rate_tier", "cwd"),
        cwd="/var/data",
    )
    assert ds == "deepseek-v4-flash · 10% · peak · /var/data"

    # No window matches → field silently skipped.
    non_ds = format_runtime_footer(
        model="claude-opus-5",
        context_tokens=10_000,
        context_length=100_000,
        fields=("model", "context_pct", "rate_tier"),
        cwd="/var/data",
    )
    assert non_ds == "claude-opus-5 · 10%"

    # Direct function: same guarantees at the unit level.
    assert rate_tier_for_model("deepseek-v4-flash") == "peak"
    assert rate_tier_for_model("claude-opus-5") is None


def test_rate_tier_custom_windows_sgt(monkeypatch):
    """User rate_windows override: custom tz + windows win over the built-in default."""
    import datetime as dt

    from gateway.runtime_footer import (
        format_runtime_footer,
        rate_tier_for_model,
        resolve_footer_config,
        _DEFAULT_RATE_WINDOWS,
    )

    # Windows {mymodel: {tz: Asia/Singapore, peak: [[20,22]]}}.
    # At 12:30 UTC = 20:30 SGT → custom PEAK; the built-in DeepSeek UTC window
    # (01-04/06-10) says off-peak. Proves tz + custom windows are authoritative.
    cfg = {
        "display": {
            "runtime_footer": {
                "enabled": True,
                "fields": ["model", "rate_tier"],
                "rate_windows": {
                    "mymodel": {"tz": "Asia/Singapore", "peak": [[20, 22]]}
                },
            }
        }
    }
    resolved = resolve_footer_config(cfg, "telegram")
    # merge keeps the built-in deepseek default AND the custom entry
    assert "deepseek" in resolved["rate_windows"]
    assert "mymodel" in resolved["rate_windows"]

    monkeypatch.setattr(
        "gateway.runtime_footer._dt.datetime",
        _FakeDateTime(dt.datetime(2026, 8, 20, 12, 30, tzinfo=dt.timezone.utc)),
    )
    out = format_runtime_footer(
        model="acme/mymodel-2",
        context_tokens=10_000,
        context_length=100_000,
        fields=("model", "rate_tier"),
        rate_windows=resolved["rate_windows"],
    )
    assert out == "mymodel-2 · peak"

    # the same instant is off-peak for the built-in deepseek default
    assert rate_tier_for_model("deepseek-v4-flash", resolved["rate_windows"]) == "off-peak"
    assert _DEFAULT_RATE_WINDOWS["deepseek"]["tz"] == "UTC"


def test_rate_tier_default_windows_boundaries():
    """Built-in DeepSeek peak windows are 01-03 and 06-09 UTC."""
    import datetime as dt

    from gateway.runtime_footer import rate_tier_for_model

    peak_hours = {1, 2, 3, 6, 7, 8, 9}
    for hour in range(24):
        t = dt.datetime(2026, 8, 20, hour, 30, tzinfo=dt.timezone.utc)
        expected = "peak" if hour in peak_hours else "off-peak"
        assert rate_tier_for_model("deepseek-v4-flash", now=t) == expected, (
            f"UTC {hour}:00 wrong"
        )


def test_rate_tier_longest_match_beats_insertion_order():
    """A user's model-specific window beats the built-in generic matcher.

    Regression for review: _merge_rate_windows seeds 'deepseek' FIRST, so
    iterating in insertion order returns the built-in default before the
    user's 'deepseek-v4-flash' entry is ever checked — silently inverting
    their config. Longest-key matching makes specificity win regardless of
    dict order.
    """
    import datetime as dt

    from gateway.runtime_footer import _merge_rate_windows, rate_tier_for_model

    windows = _merge_rate_windows(
        {"deepseek-v4-flash": {"tz": "UTC", "peak": [[20, 23]]}}
    )
    # merged order: built-in 'deepseek' is first, user's specific key second
    assert list(windows)[0] == "deepseek"
    assert "deepseek-v4-flash" in windows

    # 21:00 UTC — the user's [[20,23]] says PEAK; the built-in deepseek
    # window (01-04/06-10) says off-peak. The user's config must win.
    t = dt.datetime(2026, 8, 20, 21, 0, tzinfo=dt.timezone.utc)
    assert rate_tier_for_model("deepseek-v4-flash", windows, now=t) == "peak"

    # The generic matcher still applies to other deepseek models.
    t2 = dt.datetime(2026, 8, 20, 8, 0, tzinfo=dt.timezone.utc)
    assert rate_tier_for_model("deepseek-r1", windows, now=t2) == "peak"


def test_rate_tier_midnight_crossing_window():
    """A peak window that wraps midnight ([[22, 2]]) is peak for 22:00-01:59.

    Regression for review: `start <= hour < end` is unsatisfiable for a
    wrapped window, so it parsed fine but silently matched nothing.
    """
    import datetime as dt

    from gateway.runtime_footer import rate_tier_for_model

    windows = {"nightowl": {"tz": "UTC", "peak": [[22, 2]]}}
    cases = {
        0: "peak", 1: "peak", 2: "off-peak", 3: "off-peak",
        21: "off-peak", 22: "peak", 23: "peak",
    }
    for hour, expected in cases.items():
        t = dt.datetime(2026, 8, 20, hour, 30, tzinfo=dt.timezone.utc)
        assert rate_tier_for_model("nightowl-1", windows, now=t) == expected, (
            f"hour {hour}: expected {expected}"
        )


def test_rate_tier_deepseek_weekend_off_peak_beijing():
    """DeepSeek bills off-peak ALL DAY on Beijing weekends (fixed UTC+8).

    Regression for review: the old default mislabelled 14 of every 168 hours
    as peak because it had no day-of-week axis. Weekend-ness is a property of
    the BEIJING date, not the UTC date: the Beijing weekend runs Friday
    16:00 UTC -> Sunday 16:00 UTC. A UTC-date weekday check would agree only
    while every peak window sits clear of the 16:00-24:00 UTC stretch where
    the two dates disagree.
    """
    import datetime as dt

    from gateway.runtime_footer import rate_tier_for_model

    # Beijing Saturday 10:00 (UTC 02:00) — inside the UTC peak window 01-04,
    # but DeepSeek is off-peak all weekend.
    sat = dt.datetime(2026, 8, 22, 2, 0, tzinfo=dt.timezone.utc)
    assert rate_tier_for_model("deepseek-v4-flash", now=sat) == "off-peak"

    # Beijing Sunday 16:00 (UTC 08:00) — inside the UTC peak window 06-10.
    sun = dt.datetime(2026, 8, 23, 8, 0, tzinfo=dt.timezone.utc)
    assert rate_tier_for_model("deepseek-v4-flash", now=sun) == "off-peak"

    # Same UTC clock time on Monday: weekday, so the peak window applies again.
    mon = dt.datetime(2026, 8, 24, 2, 0, tzinfo=dt.timezone.utc)
    assert rate_tier_for_model("deepseek-v4-flash", now=mon) == "peak"

    # Beijing Saturday begins at UTC Friday 16:00 — already weekend there.
    fri_eve = dt.datetime(2026, 8, 21, 16, 30, tzinfo=dt.timezone.utc)
    assert rate_tier_for_model("deepseek-v4-flash", now=fri_eve) == "off-peak"

    # Non-DeepSeek models match no window and stay silent.
    assert rate_tier_for_model("claude-opus-5", now=sat) is None


def test_deepseek_rate_tier_legacy_helper():
    """Backward-compat helper still returns the same UTC-based tiers."""
    import datetime as dt

    from gateway.runtime_footer import deepseek_rate_tier

    assert deepseek_rate_tier(dt.datetime(2026, 8, 20, 2, 30, tzinfo=dt.timezone.utc)) == "peak"
    assert deepseek_rate_tier(dt.datetime(2026, 8, 20, 5, 30, tzinfo=dt.timezone.utc)) == "off-peak"


class _FakeDateTime:
    """Minimal stand-in so monkeypatching gateway.runtime_footer._dt.datetime works."""

    def __init__(self, fixed):
        self._fixed = fixed

    def now(self, tz=None):
        return self._fixed.astimezone(tz) if tz else self._fixed
