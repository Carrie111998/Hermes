"""Unit tests for gateway.runtime_footer — the opt-in runtime-metadata footer
appended to final gateway replies."""

from __future__ import annotations

import os

import pytest

from gateway.runtime_footer import (
    _compaction_marker,
    _home_relative_cwd,
    _model_short,
    build_footer_line,
    build_meter_footer,
    compaction_percent,
    format_runtime_footer,
    resolve_footer_config,
    resolve_meter_config,
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


def test_home_relative_cwd_leaves_abs_path_alone(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "other"))
    result = _home_relative_cwd(str(tmp_path / "outside" / "dir"))
    assert result == str(tmp_path / "outside" / "dir")


def test_home_relative_cwd_empty_returns_empty():
    assert _home_relative_cwd("") == ""


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


def test_format_footer_context_pct_clamped_to_100():
    out = format_runtime_footer(
        model="m",
        context_tokens=500_000,  # way over
        context_length=100_000,
        cwd="",
        fields=("context_pct",),
    )
    assert out == "100%"


def test_format_footer_context_pct_never_negative():
    out = format_runtime_footer(
        model="m",
        context_tokens=-50,
        context_length=100,
        cwd="",
        fields=("context_pct",),
    )
    # Negative input => no field emitted (we require context_tokens >= 0)
    assert out == ""


def test_format_footer_empty_fields_returns_empty():
    out = format_runtime_footer(
        model="m", context_tokens=0, context_length=100,
        cwd="/x", fields=(),
    )
    assert out == ""


def test_format_footer_drops_cwd_when_empty(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=50, context_length=100,
        cwd="",
        fields=("model", "context_pct", "cwd"),
    )
    # cwd silently dropped; model + pct remain
    assert out == "gpt-5.4 · 50%"


def test_format_footer_custom_field_order():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=50, context_length=100,
        cwd="/opt/project",
        fields=("context_pct", "model"),  # swapped + no cwd
    )
    assert out == "50% · gpt-5.4"


def test_format_footer_unknown_field_silently_ignored():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=50, context_length=100,
        cwd="/x",
        fields=("model", "bogus", "context_pct"),
    )
    assert out == "gpt-5.4 · 50%"


# ---------------------------------------------------------------------------
# resolve_footer_config
# ---------------------------------------------------------------------------

def test_resolve_defaults_off_empty_config():
    cfg = resolve_footer_config({}, "telegram")
    assert cfg == {"enabled": False, "fields": ["model", "context_pct", "cwd"]}


def test_resolve_global_enable():
    user = {"display": {"runtime_footer": {"enabled": True}}}
    cfg = resolve_footer_config(user, "telegram")
    assert cfg["enabled"] is True
    assert cfg["fields"] == ["model", "context_pct", "cwd"]


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


def test_resolve_ignores_malformed_config():
    # Non-dict runtime_footer shouldn't crash
    user = {"display": {"runtime_footer": "on"}}
    cfg = resolve_footer_config(user, "telegram")
    assert cfg["enabled"] is False


# ---------------------------------------------------------------------------
# build_footer_line — top-level entry point used by gateway/run.py
# ---------------------------------------------------------------------------

def test_build_footer_empty_when_disabled():
    out = build_footer_line(
        user_config={},
        platform_key="telegram",
        model="openai/gpt-5.4",
        context_tokens=10, context_length=100,
        cwd="/tmp",
    )
    assert out == ""


def test_build_footer_returns_rendered_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    out = build_footer_line(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        platform_key="telegram",
        model="openai/gpt-5.4",
        context_tokens=25, context_length=100,
        cwd=str(tmp_path / "proj"),
    )
    (tmp_path / "proj").mkdir(exist_ok=True)
    assert "gpt-5.4" in out
    assert "25%" in out


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


def test_build_footer_no_data_returns_empty_even_when_enabled():
    # Enabled, but context_length is None AND cwd empty AND model empty ⇒ no fields
    out = build_footer_line(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        platform_key="telegram",
        model="",
        context_tokens=0, context_length=None,
        cwd="",
    )
    # With no TERMINAL_CWD env either
    if not os.environ.get("TERMINAL_CWD"):
        assert out == ""


# ---------------------------------------------------------------------------
# compaction_percent + _compaction_marker — the context-meter primitives
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "tokens,threshold,expected",
    [
        (3500, 5000, 70),     # 70% of the way to compaction
        (5000, 5000, 100),    # compaction fires now
        (6000, 5000, 120),    # overshoot allowed (checked after send)
        (0, 5000, 0),
    ],
)
def test_compaction_percent_basic(tokens, threshold, expected):
    assert compaction_percent(tokens, threshold) == expected


@pytest.mark.parametrize("threshold", [None, 0, -10])
def test_compaction_percent_none_when_threshold_unknown(threshold):
    assert compaction_percent(50_000, threshold) is None


def test_compaction_percent_none_for_negative_tokens():
    assert compaction_percent(-5, 5000) is None


@pytest.mark.parametrize(
    "pct,emoji",
    [(70, "🟡"), (84, "🟡"), (85, "🟠"), (99, "🟠"), (100, "🔴"), (130, "🔴")],
)
def test_compaction_marker_emoji_tiers(pct, emoji):
    marker = _compaction_marker(pct)
    assert marker.startswith(emoji)
    assert f"{pct}% to compaction" in marker


# ---------------------------------------------------------------------------
# format_runtime_footer — compaction field
# ---------------------------------------------------------------------------

def test_format_footer_compaction_field():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=3500,
        context_length=100_000,
        cwd="",
        fields=("model", "compaction"),
        threshold_tokens=5000,
    )
    assert out == "gpt-5.4 · 🟡 70% to compaction"


def test_format_footer_compaction_field_dropped_without_threshold():
    out = format_runtime_footer(
        model="openai/gpt-5.4",
        context_tokens=3500,
        context_length=100_000,
        cwd="",
        fields=("model", "compaction"),
        threshold_tokens=None,
    )
    assert out == "gpt-5.4"


# ---------------------------------------------------------------------------
# resolve_meter_config
# ---------------------------------------------------------------------------

def test_resolve_meter_default_floor():
    assert resolve_meter_config({})["footer_floor"] == 0.70
    assert resolve_meter_config(None)["footer_floor"] == 0.70


def test_resolve_meter_custom_floor():
    user = {"display": {"context_meter": {"footer_floor": 0.5}}}
    assert resolve_meter_config(user)["footer_floor"] == 0.5


@pytest.mark.parametrize("bad", [0, -0.2, 1.5, "high", None])
def test_resolve_meter_out_of_range_falls_back(bad):
    user = {"display": {"context_meter": {"footer_floor": bad}}}
    assert resolve_meter_config(user)["footer_floor"] == 0.70


# ---------------------------------------------------------------------------
# build_meter_footer — manual toggle + always-on-past-floor behavior
# ---------------------------------------------------------------------------

def _meter(**kw):
    base = dict(
        user_config={},
        platform_key="telegram",
        model="openai/gpt-5.4",
        context_length=200_000,
        cwd="",
    )
    base.update(kw)
    return build_meter_footer(**base)


def test_meter_footer_silent_below_floor(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    # 60% to compaction, floor is 70% → nothing
    out = _meter(context_tokens=3000, threshold_tokens=5000)
    assert out == ""


def test_meter_footer_surfaces_past_floor(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    # 80% to compaction, manual footer OFF → always-on meter footer appears
    out = _meter(context_tokens=4000, threshold_tokens=5000)
    assert out == "gpt-5.4 · 🟡 80% to compaction"


def test_meter_footer_escalates_emoji_past_compaction(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    out = _meter(context_tokens=5200, threshold_tokens=5000)
    assert "🔴 104% to compaction" in out


def test_meter_footer_manual_on_below_floor_shows_window_pct(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    # Manual footer on, below floor → configured footer (window %), no marker
    out = _meter(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        context_tokens=40_000,      # 20% of 200k window
        threshold_tokens=100_000,   # 40% to compaction → below 70 floor
    )
    assert out == "gpt-5.4 · 20%"
    assert "to compaction" not in out


def test_meter_footer_manual_on_past_floor_appends_marker(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    # Manual footer on AND past floor → window footer + compaction marker
    out = _meter(
        user_config={"display": {"runtime_footer": {"enabled": True}}},
        context_tokens=80_000,      # 40% of 200k window
        threshold_tokens=100_000,   # 80% to compaction → past floor
    )
    assert out == "gpt-5.4 · 40% · 🟡 80% to compaction"


def test_meter_footer_manual_with_compaction_field_not_doubled(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    # Manual footer already includes the compaction field → don't append twice
    out = _meter(
        user_config={
            "display": {
                "runtime_footer": {"enabled": True, "fields": ["model", "compaction"]}
            }
        },
        context_tokens=80_000,
        threshold_tokens=100_000,
    )
    assert out == "gpt-5.4 · 🟡 80% to compaction"
    assert out.count("to compaction") == 1


def test_meter_footer_custom_floor_respected(monkeypatch):
    monkeypatch.delenv("TERMINAL_CWD", raising=False)
    # Lower the floor to 50% → a 60% reading now surfaces
    out = _meter(
        user_config={"display": {"context_meter": {"footer_floor": 0.5}}},
        context_tokens=3000,
        threshold_tokens=5000,
    )
    assert out == "gpt-5.4 · 🟡 60% to compaction"
