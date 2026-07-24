from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import pytest

from hermes_cli.fleet.capacity import BridgeUsageAdapter
from hermes_cli.fleet.config import parse_fleet_config
from hermes_cli.fleet.parent_models import (
    ADMITTED_CLAUDE_PARENT_MODEL,
    is_admitted_parent_model,
    is_sonnet_model,
)
from hermes_cli.fleet.usage_paths import (
    DEFAULT_USAGE_RELATIVE,
    default_native_usage_path,
    resolve_usage_path,
)
from hermes_cli.fleet.usage_refresh import (
    UsageRefreshError,
    refresh_usage_document,
)
from hermes_cli.fleet.types import Freshness, ReasonCode


NOW = datetime(2026, 7, 24, 12, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class _Window:
    label: str
    used_percent: float


@dataclass(frozen=True)
class _Snapshot:
    windows: tuple[_Window, ...]


def test_default_usage_path_is_profile_home_relative(tmp_path, monkeypatch):
    home = tmp_path / "profile-a"
    monkeypatch.setenv("HERMES_HOME", str(home))

    path = default_native_usage_path()

    assert path == (home / DEFAULT_USAGE_RELATIVE).resolve()
    assert "HermesBridge" not in str(path)


def test_resolve_usage_path_profile_isolation(tmp_path):
    a = resolve_usage_path(None, home=tmp_path / "a")
    b = resolve_usage_path(None, home=tmp_path / "b")
    relative = resolve_usage_path("custom/usage.json", home=tmp_path / "a")
    absolute = resolve_usage_path(tmp_path / "abs.json", home=tmp_path / "a")

    assert a != b
    assert a.parent.name == "fleet"
    assert relative == (tmp_path / "a" / "custom" / "usage.json").resolve()
    assert absolute == (tmp_path / "abs.json")


def test_parse_fleet_config_defaults_to_native_home_path(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    config = parse_fleet_config({})

    assert config.bridge_usage_file == default_native_usage_path()
    assert config.bridge_usage_file.name == "usage-weekly.json"


def test_bridge_adapter_default_path_is_native(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hh"))
    adapter = BridgeUsageAdapter()
    assert adapter.path == default_native_usage_path()


def test_refresh_atomic_write_and_per_lane_freshness(tmp_path):
    path = tmp_path / "fleet" / "usage-weekly.json"
    mirror = tmp_path / "mirror" / "usage-weekly.json"

    def fetch(provider: str):
        if provider == "openai-codex":
            return _Snapshot((_Window("Weekly", 33.0),))
        if provider == "anthropic":
            return _Snapshot((_Window("Current week", 44.0),))
        return None

    report = refresh_usage_document(
        path=path,
        mirror_path=mirror,
        fetch_usage=fetch,
        now=NOW,
    )

    assert report.ok
    assert report.path == path
    assert report.mirrored_to == mirror
    document = json.loads(path.read_text(encoding="utf-8"))
    plans = {row["label"]: row for row in document["plans"]}
    assert plans["ChatGPT Pro · Codex"]["weekly_pct_used"] == 33.0
    assert plans["ChatGPT Pro · Codex"]["checked_at"].startswith("2026-07-24T12:00:00")
    assert plans["Claude Max 20x"]["checked_at"].startswith("2026-07-24T12:00:00")
    assert "checked_at" not in plans["SuperGrok"]
    assert "checked_at" not in plans["Google AI · Antigravity"]
    assert mirror.exists()

    # Failure preserves prior bytes.
    prior = path.read_bytes()

    def boom(provider: str):
        raise RuntimeError("network down")

    failed = refresh_usage_document(
        path=path,
        mirror_path=None,
        fetch_usage=boom,
        now=NOW,
        create_if_missing=False,
    )
    assert failed.ok is False
    assert path.read_bytes() == prior


def test_refresh_console_only_stale_cannot_win_capacity(tmp_path):
    path = tmp_path / "usage.json"
    path.write_text(
        json.dumps(
            {
                "checked_at": "2026-07-20T00:00:00Z",
                "plans": [
                    {
                        "label": "ChatGPT Pro · Codex",
                        "weekly_pct_used": 10,
                        "checked_at": "2026-07-24T11:00:00Z",
                    },
                    {
                        "label": "SuperGrok",
                        "weekly_pct_used": 0,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    def fetch(provider: str):
        if provider == "openai-codex":
            return _Snapshot((_Window("Weekly", 12.0),))
        return None

    refresh_usage_document(path=path, mirror_path=None, fetch_usage=fetch, now=NOW)
    adapter = BridgeUsageAdapter(path)
    grok = adapter.read("grok", now=NOW)
    codex = adapter.read("chatgpt_codex", now=NOW)

    assert grok.snapshot is not None
    assert grok.snapshot.freshness is Freshness.STALE
    assert grok.reason is ReasonCode.CAPACITY_STALE
    assert codex.snapshot is not None
    assert codex.snapshot.freshness is Freshness.FRESH


def test_sonnet_is_never_an_admitted_parent_model():
    assert is_sonnet_model("claude-sonnet-4-6")
    assert is_sonnet_model("anthropic/claude-sonnet-4.6")
    assert is_sonnet_model("Sonnet 4 6")
    assert not is_sonnet_model(ADMITTED_CLAUDE_PARENT_MODEL)
    assert not is_admitted_parent_model("claude-sonnet-4-6")
    assert is_admitted_parent_model("gpt-5.6-sol")
    assert is_admitted_parent_model(ADMITTED_CLAUDE_PARENT_MODEL)


def test_missing_usage_file_create_shell_without_fabricating_console_freshness(tmp_path):
    path = tmp_path / "missing.json"

    def fetch(provider: str):
        if provider == "openai-codex":
            return _Snapshot((_Window("Weekly", 1.0),))
        return None

    report = refresh_usage_document(
        path=path, mirror_path=None, fetch_usage=fetch, now=NOW
    )
    document = report.document
    grok = next(row for row in document["plans"] if "Grok" in row["label"])
    assert "checked_at" not in grok
