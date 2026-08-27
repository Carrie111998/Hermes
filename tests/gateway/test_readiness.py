from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from gateway.readiness import collect_runtime_readiness


def test_collect_runtime_readiness_reports_healthy_local_runtime(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  model: test/model\n",
        encoding="utf-8",
    )
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {"telegram": {"state": "connected"}},
            "updated_at": "2026-07-09T00:00:00Z",
        },
        active_api_runs=2,
    )

    assert result["status"] == "ok"
    assert result["checks"]["state_db"]["status"] == "ok"
    assert result["checks"]["session_store"]["status"] == "ok"
    assert result["checks"]["config"]["status"] == "ok"
    assert result["checks"]["model"]["status"] == "ok"
    assert result["checks"]["gateway"]["status"] == "ok"
    assert result["checks"]["background_queues"]["active_api_runs"] == 2
    assert result["checks"]["disk"]["status"] in {"ok", "degraded"}


def test_collect_runtime_readiness_degrades_on_invalid_config_and_stopped_gateway(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text("model: [unterminated", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="",
        runtime_status={"gateway_state": "stopped", "platforms": {}},
    )

    assert result["status"] == "degraded"
    assert result["checks"]["config"]["status"] == "degraded"
    assert result["checks"]["model"]["status"] == "degraded"
    assert result["checks"]["gateway"]["status"] == "degraded"
    # Readiness is diagnostic data, not an exception or a destructive repair.
    assert (home / "config.yaml").read_text(encoding="utf-8") == "model: [unterminated"


def test_readiness_degrades_when_current_platform_is_retrying(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "pid": 123,
            "start_time": 456,
            "platforms": {
                "telegram": {
                    "state": "connected",
                    "writer_pid": 123,
                    "writer_start_time": 456,
                },
                "matrix": {
                    "state": "retrying",
                    "writer_pid": 123,
                    "writer_start_time": 456,
                },
            },
        },
    )

    gateway = result["checks"]["gateway"]
    assert result["status"] == "degraded"
    assert gateway["status"] == "degraded"
    assert gateway["connected_platforms"] == 1
    assert gateway["unhealthy_platforms"] == 1
    assert gateway["platforms"] == 2


def test_readiness_ignores_platform_entries_from_previous_process(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "pid": 200,
            "start_time": 300,
            "platforms": {
                "matrix": {
                    "state": "fatal",
                    "writer_pid": 100,
                    "writer_start_time": 150,
                }
            },
        },
    )

    gateway = result["checks"]["gateway"]
    assert gateway["status"] == "ok"
    assert gateway["platforms"] == 0
    assert gateway["unhealthy_platforms"] == 0


@pytest.mark.parametrize("platform_state", ["retrying", "fatal", "paused"])
def test_readiness_degrades_for_current_unhealthy_platform_state(
    tmp_path, monkeypatch, platform_state
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "pid": 123,
            "start_time": 456,
            "platforms": {
                "matrix": {
                    "state": platform_state,
                    "writer_pid": 123,
                    "writer_start_time": 456,
                }
            },
        },
    )

    assert result["checks"]["gateway"]["status"] == "degraded"
    assert result["checks"]["gateway"]["unhealthy_platforms"] == 1


def test_readiness_does_not_degrade_for_intentionally_disabled_platform(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "pid": 123,
            "start_time": 456,
            "platforms": {
                "matrix": {
                    "state": "disabled",
                    "writer_pid": 123,
                    "writer_start_time": 456,
                }
            },
        },
    )

    gateway = result["checks"]["gateway"]
    assert gateway["status"] == "ok"
    assert gateway["platforms"] == 1
    assert gateway["unhealthy_platforms"] == 0


@pytest.mark.parametrize(
    "entry",
    [
        {"state": "fatal", "writer_pid": 123},
        {"state": "fatal", "writer_start_time": 456},
        {"state": "fatal", "writer_pid": "not-a-pid", "writer_start_time": 456},
    ],
)
def test_readiness_ignores_partial_or_malformed_writer_provenance(
    tmp_path, monkeypatch, entry
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "pid": 123,
            "start_time": 456,
            "platforms": {"matrix": entry},
        },
    )

    gateway = result["checks"]["gateway"]
    assert gateway["status"] == "ok"
    assert gateway["platforms"] == 0


def test_readiness_ignores_unstamped_entry_when_runtime_has_writer_identity(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "pid": 123,
            "start_time": 456,
            "platforms": {"matrix": {"state": "retrying"}},
        },
    )

    gateway = result["checks"]["gateway"]
    assert gateway["status"] == "ok"
    assert gateway["platforms"] == 0
    assert gateway["unhealthy_platforms"] == 0


@pytest.mark.parametrize(
    ("runtime_identity", "entry"),
    [
        ({"pid": 123}, {"state": "fatal"}),
        ({"start_time": 456}, {"state": "fatal"}),
        (
            {},
            {
                "state": "fatal",
                "writer_pid": 123,
                "writer_start_time": 456,
            },
        ),
        (
            {"pid": True, "start_time": 1},
            {
                "state": "fatal",
                "writer_pid": 1,
                "writer_start_time": True,
            },
        ),
        (
            {"pid": "123", "start_time": "456"},
            {
                "state": "fatal",
                "writer_pid": "123",
                "writer_start_time": "456",
            },
        ),
        (
            {"pid": 123, "start_time": 456.0},
            {
                "state": "fatal",
                "writer_pid": 123,
                "writer_start_time": 456.0,
            },
        ),
        (
            {"pid": 123, "start_time": 10**1000},
            {
                "state": "fatal",
                "writer_pid": 123,
                "writer_start_time": 10**1000,
            },
        ),
    ],
)
def test_readiness_rejects_unverifiable_runtime_or_platform_identity(
    tmp_path, monkeypatch, runtime_identity, entry
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            **runtime_identity,
            "platforms": {"matrix": entry},
        },
    )

    gateway = result["checks"]["gateway"]
    assert gateway["status"] == "ok"
    assert gateway["platforms"] == 0
    assert gateway["unhealthy_platforms"] == 0


def test_readiness_keeps_legacy_unstamped_entry_without_runtime_identity(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {"matrix": {"state": "retrying"}},
        },
    )

    gateway = result["checks"]["gateway"]
    assert gateway["status"] == "degraded"
    assert gateway["platforms"] == 1
    assert gateway["unhealthy_platforms"] == 1


def test_readiness_uses_running_session_store_state_over_independent_probe(
    tmp_path, monkeypatch
):
    home = tmp_path / ".hermes"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    monkeypatch.setenv("HERMES_HOME", str(home))

    unavailable = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {},
            "session_store": {"status": "unavailable"},
        },
    )

    assert unavailable["checks"]["state_db"]["status"] == "ok"
    assert unavailable["checks"]["session_store"] == {"status": "unavailable"}
    assert unavailable["status"] == "degraded"

    recovered = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {},
            "session_store": {"status": "ok"},
        },
    )
    assert recovered["checks"]["session_store"] == {"status": "ok"}


