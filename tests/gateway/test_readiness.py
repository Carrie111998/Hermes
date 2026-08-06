from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from gateway.readiness import (
    _probe_profile_state_dbs,
    _probe_state_db,
    collect_runtime_readiness,
)


def _seed_session_store(db_path: Path) -> None:
    """Create a real schema-converged session store (not a bare fixture DB).

    Readiness is schema-aware since OOF-76: a database holding only foreign
    tables is exactly the drift the probe exists to catch, so healthy
    fixtures must carry the real contract.
    """
    from hermes_state import SessionDB

    db_path.parent.mkdir(parents=True, exist_ok=True)
    SessionDB(db_path=db_path).close()


def _drop_sessions_column(db_path: Path, column: str) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"ALTER TABLE sessions DROP COLUMN {column}")


def _force_disk_ok(monkeypatch) -> None:
    """Pin the disk probe: it measures the developer's real volume, and a
    host sitting above the 90% threshold must not fail unrelated tests."""
    import gateway.readiness as readiness_mod

    monkeypatch.setattr(
        readiness_mod, "_probe_disk", lambda _home: {"status": "ok"}
    )


def test_collect_runtime_readiness_reports_healthy_local_runtime(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  model: test/model\n",
        encoding="utf-8",
    )
    _seed_session_store(home / "state.db")
    monkeypatch.setenv("HERMES_HOME", str(home))
    _force_disk_ok(monkeypatch)

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
    assert result["checks"]["profile_state_dbs"]["status"] == "ok"


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




def test_probe_state_db_flags_schema_drift(tmp_path):
    """A readable store missing contract columns must degrade, not stay green.

    This is the OOF-76 false-green: readability-only probes reported "ok"
    while every dashboard session route 500'd on the missing columns.
    """
    home = tmp_path / ".hermes"
    _seed_session_store(home / "state.db")
    assert _probe_state_db(home)["status"] == "ok"

    _drop_sessions_column(home / "state.db", "archived")

    check = _probe_state_db(home)
    assert check["status"] == "degraded"
    assert check["detail"] == "schema behind contract"
    assert check["mismatches"] >= 1


def test_probe_state_db_foreign_tables_are_stale_not_ok(tmp_path):
    """A non-empty DB holding only unrelated tables is drift, not 'ok'."""
    home = tmp_path / ".hermes"
    home.mkdir()
    with sqlite3.connect(home / "state.db") as conn:
        conn.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
    assert _probe_state_db(home)["status"] == "degraded"


def test_probe_state_db_missing_and_empty_stores_are_ok(tmp_path):
    """Uninitialised stores belong to bootstrap, not the readiness probe."""
    home = tmp_path / ".hermes"
    home.mkdir()
    assert _probe_state_db(home) == {"status": "ok", "detail": "not initialized"}

    # Zero-byte / no-tables store: created but never bootstrapped.
    sqlite3.connect(home / "state.db").close()
    assert _probe_state_db(home)["status"] == "ok"


def test_probe_profile_state_dbs_reports_idle_profile_drift(tmp_path):
    """Named-profile stores drift silently (no writable opens while idle) —
    the aggregate probe must surface counts without leaking profile names."""
    home = tmp_path / ".hermes"
    _seed_session_store(home / "profiles" / "alpha" / "state.db")
    _seed_session_store(home / "profiles" / "bravo" / "state.db")
    assert _probe_profile_state_dbs(home)["status"] == "ok"

    _drop_sessions_column(home / "profiles" / "bravo" / "state.db", "pinned")

    check = _probe_profile_state_dbs(home)
    assert check["status"] == "degraded"
    assert check["checked"] == 2
    assert check["stale"] == 1
    assert check["errors"] == 0
    # Aggregate counts only: never profile names or paths.
    assert "alpha" not in json.dumps(check)
    assert "bravo" not in json.dumps(check)


def test_probe_profile_state_dbs_bounded_scan_flags_truncation(
    tmp_path, monkeypatch
):
    """The probe must stay bounded inside a health poll: entries past the
    cap are never opened, and the verdict admits its incomplete coverage
    via the truncated flag instead of degrading a healthy fleet."""
    import gateway.readiness as readiness_mod

    home = tmp_path / ".hermes"
    for name in ("alpha", "bravo", "charlie"):
        _seed_session_store(home / "profiles" / name / "state.db")
    monkeypatch.setattr(readiness_mod, "_PROFILE_STORE_PROBE_LIMIT", 2)
    # The capped window rotates across polls via a module-global cursor —
    # pin it so this test asserts against a known window regardless of
    # execution order.
    monkeypatch.setattr(readiness_mod, "_profile_probe_cursor", 0)

    check = _probe_profile_state_dbs(home)

    assert check["status"] == "ok"
    assert check["checked"] == 2
    assert check["truncated"] is True
    assert check["detail"] == "profile store probe incomplete"

    # A stale store INSIDE the scanned window still degrades the check even
    # when the scan is truncated.
    _drop_sessions_column(home / "profiles" / "alpha" / "state.db", "archived")
    stale_check = _probe_profile_state_dbs(home)
    assert stale_check["status"] == "degraded"
    assert stale_check["stale"] == 1
    assert stale_check["truncated"] is True


def test_probe_profile_state_dbs_capped_window_rotates_across_polls(
    tmp_path, monkeypatch
):
    """With more profiles than the cap, successive polls must rotate the
    scanned window: a stale store past the first window is eventually
    inspected instead of staying invisible forever (the OOF-76 blind spot
    relocated past the cap)."""
    import gateway.readiness as readiness_mod

    home = tmp_path / ".hermes"
    for name in ("alpha", "bravo", "charlie", "delta"):
        _seed_session_store(home / "profiles" / name / "state.db")
    # Stale store deliberately LAST in sort order, outside the first window.
    _drop_sessions_column(home / "profiles" / "delta" / "state.db", "archived")
    monkeypatch.setattr(readiness_mod, "_PROFILE_STORE_PROBE_LIMIT", 2)
    monkeypatch.setattr(readiness_mod, "_profile_probe_cursor", 0)

    first = _probe_profile_state_dbs(home)
    assert first["status"] == "ok"
    assert first["truncated"] is True

    second = _probe_profile_state_dbs(home)
    assert second["status"] == "degraded"
    assert second["stale"] == 1
    assert second["truncated"] is True


def test_probe_profile_state_dbs_no_profiles_is_ok(tmp_path):
    home = tmp_path / ".hermes"
    home.mkdir()
    assert _probe_profile_state_dbs(home) == {"status": "ok", "detail": "no profiles"}


def test_probe_profile_state_dbs_unreadable_store_counts_as_error(tmp_path):
    home = tmp_path / ".hermes"
    store = home / "profiles" / "broken" / "state.db"
    store.parent.mkdir(parents=True)
    store.write_bytes(b"this is not a sqlite database at all" * 40)

    check = _probe_profile_state_dbs(home)
    assert check["status"] == "degraded"
    assert check["errors"] == 1


def test_collect_runtime_readiness_reports_stale_profile_store_as_advisory(
    tmp_path, monkeypatch
):
    """Profile drift must be VISIBLE in checks but excluded from the
    restart-driving overall verdict: restarting cannot heal an idle profile
    store (only the dashboard's heal-capable read path can), so a degraded
    rollup here would invite an unhealable-signal restart loop (OOF-39
    class)."""
    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        "model:\n  provider: openrouter\n  model: test/model\n",
        encoding="utf-8",
    )
    _seed_session_store(home / "state.db")
    _seed_session_store(home / "profiles" / "ops" / "state.db")
    _drop_sessions_column(home / "profiles" / "ops" / "state.db", "archived")
    monkeypatch.setenv("HERMES_HOME", str(home))
    _force_disk_ok(monkeypatch)

    result = collect_runtime_readiness(
        configured_model="test/model",
        runtime_status={
            "gateway_state": "running",
            "platforms": {"telegram": {"state": "connected"}},
            "updated_at": "2026-07-09T00:00:00Z",
        },
    )

    assert result["status"] == "ok"
    assert result["checks"]["state_db"]["status"] == "ok"
    assert result["checks"]["profile_state_dbs"]["status"] == "degraded"
