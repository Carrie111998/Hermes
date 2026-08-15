from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

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




def test_state_db_probe_degrades_on_unrepaired_corruption_ledger(tmp_path, monkeypatch):
    """A repair-attempts ledger matching the current file bytes must flip the
    state_db probe to degraded even though the schema page still reads fine
    (OOF-106: page-corrupt state.db stayed "ok" for 10+ days)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    st = db_path.stat()
    (home / "state.db.repair-attempts.json").write_text(
        json.dumps(
            {
                "fingerprint": f"{st.st_size}:{st.st_mtime_ns}",
                "failed_attempts": 1,
                "last_attempt": "2026-08-15T00:00:00",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={"gateway_state": "running", "platforms": {}},
        active_api_runs=0,
    )

    assert result["checks"]["state_db"]["status"] == "degraded"
    assert result["checks"]["state_db"]["detail"] == "unrepaired corruption"


def test_state_db_probe_ignores_stale_corruption_ledger(tmp_path, monkeypatch):
    """A ledger whose fingerprint no longer matches (file repaired/replaced
    since) must NOT degrade the probe."""
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    (home / "state.db.repair-attempts.json").write_text(
        json.dumps({"fingerprint": "1:1", "failed_attempts": 3}),
        encoding="utf-8",
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={"gateway_state": "running", "platforms": {}},
        active_api_runs=0,
    )

    assert result["checks"]["state_db"]["status"] == "ok"


def test_state_db_probe_ignores_malformed_corruption_ledger(tmp_path, monkeypatch):
    """Garbage in the ledger file must read as "no signal", never crash the
    probe or degrade a healthy database."""
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    (home / "state.db.repair-attempts.json").write_text(
        "not json at all", encoding="utf-8"
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={"gateway_state": "running", "platforms": {}},
        active_api_runs=0,
    )

    assert result["checks"]["state_db"]["status"] == "ok"


def test_state_db_probe_catches_sessions_root_page_corruption(tmp_path, monkeypatch):
    """Page-level damage inside the sessions table b-tree (schema page intact)
    must degrade the probe — the exact OOF-106 false-green failure mode."""
    home = tmp_path / ".hermes"
    home.mkdir()
    db_path = home / "state.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("PRAGMA page_size = 4096")
        conn.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, data TEXT)")
        conn.executemany(
            "INSERT INTO sessions VALUES (?, ?)",
            [(f"s{i}", "x" * 3500) for i in range(40)],
        )
    # Find the sessions table's root page and zero it out: sqlite_master
    # (page 1) stays valid, so the schema probe alone would still pass.
    with sqlite3.connect(db_path) as conn:
        rootpage = conn.execute(
            "SELECT rootpage FROM sqlite_master WHERE type='table' AND name='sessions'"
        ).fetchone()[0]
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    with open(db_path, "r+b") as fh:
        fh.seek((rootpage - 1) * page_size)
        fh.write(b"\x00" * page_size)
    monkeypatch.setenv("HERMES_HOME", str(home))

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={"gateway_state": "running", "platforms": {}},
        active_api_runs=0,
    )

    assert result["checks"]["state_db"]["status"] == "degraded"
