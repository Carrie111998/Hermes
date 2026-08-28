"""Behavior tests for safe session model-route audit and reset."""

import json
from argparse import Namespace
from pathlib import Path

from hermes_state import SessionDB


def _seed_session(
    db: SessionDB,
    session_id: str,
    *,
    model: str,
    billing_provider: str,
    model_config: dict,
    source: str = "cli",
) -> None:
    db.create_session(
        session_id,
        source=source,
        model=model,
        model_config=model_config,
    )
    db._conn.execute(
        "UPDATE sessions SET billing_provider = ?, title = ? WHERE id = ?",
        (billing_provider, f"Title {session_id}", session_id),
    )
    db._conn.commit()


def test_audit_filters_glob_and_reports_route_desync(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        _seed_session(
            db,
            "stale",
            model="new/model",
            billing_provider="openrouter",
            model_config={
                "model": "old/model",
                "provider": "legacy",
                "gateway_runtime": {
                    "model": "old/model",
                    "provider": "legacy",
                    "base_url": "https://legacy.invalid/v1",
                },
                "_branched_from": "parent",
            },
            source="discord",
        )
        _seed_session(
            db,
            "healthy",
            model="new/model",
            billing_provider="openrouter",
            model_config={
                "model": "new/model",
                "provider": "openrouter",
                "gateway_runtime": {
                    "model": "new/model",
                    "provider": "openrouter",
                },
            },
        )

        rows = db.audit_session_model_routes(model_glob="new/*", provider="legacy")

        assert [row["id"] for row in rows] == ["stale"]
        assert rows[0]["source"] == "discord"
        assert rows[0]["stored_provider"] == "legacy"
        assert "model_config.model != sessions.model" in rows[0]["route_issues"]
        assert "model_config.provider != sessions.billing_provider" in rows[0]["route_issues"]
    finally:
        db.close()


def test_audit_does_not_treat_bare_billing_bucket_as_provider_desync(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        _seed_session(
            db,
            "custom",
            model="vendor/model",
            billing_provider="custom",
            model_config={
                "model": "vendor/model",
                "provider": "custom:team-endpoint",
            },
        )

        assert db.audit_session_model_routes(inconsistent_only=True) == []
    finally:
        db.close()


def test_reset_dry_run_is_read_only_and_creates_no_backup(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    try:
        _seed_session(
            db,
            "stale",
            model="old/model",
            billing_provider="legacy",
            model_config={"model": "old/model", "provider": "legacy"},
        )

        report = db.reset_session_model_routes(
            target_model="new/model",
            target_provider="openrouter",
            dry_run=True,
        )

        assert report["dry_run"] is True
        assert report["rows_affected"] == 1
        assert report["row_ids"] == ["stale"]
        assert report["backup_path"] is None
        assert db.get_session("stale")["model"] == "old/model"
        assert list(tmp_path.glob("state.db.pre-model-reset-backup-*")) == []
    finally:
        db.close()


def test_reset_backs_up_then_updates_only_session_route_fields_and_verifies(tmp_path):
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    try:
        original_config = {
            "model": "old/model",
            "provider": "legacy",
            "base_url": "https://legacy.invalid/v1",
            "api_mode": "anthropic_messages",
            "gateway_runtime": {
                "model": "old/model",
                "provider": "legacy",
                "base_url": "https://legacy.invalid/v1",
                "api_mode": "anthropic_messages",
            },
            "browser_model_lock": {"model": "old/model", "confirmed": True},
            "_branched_from": "parent",
            "future_key": {"keep": True},
        }
        _seed_session(
            db,
            "stale",
            model="old/model",
            billing_provider="legacy",
            model_config=original_config,
        )
        db._conn.execute(
            "UPDATE sessions SET billing_base_url = ?, billing_mode = ?, message_count = 7 WHERE id = ?",
            ("https://legacy.invalid/v1", "anthropic_messages", "stale"),
        )
        db._conn.execute("CREATE TABLE reset_sentinel (value TEXT)")
        db._conn.execute("INSERT INTO reset_sentinel VALUES ('untouched')")
        db._conn.commit()

        report = db.reset_session_model_routes(
            target_model="new/model",
            target_provider="openrouter",
            target_base_url="https://openrouter.ai/api/v1",
            target_api_mode="chat_completions",
        )

        assert report["rows_affected"] == 1
        assert report["remaining_non_target"] == 0
        backup = Path(report["backup_path"])
        assert backup.is_file()
        row = db.get_session("stale")
        config = json.loads(row["model_config"])
        assert row["model"] == "new/model"
        assert row["billing_provider"] == "openrouter"
        assert row["billing_base_url"] == "https://openrouter.ai/api/v1"
        assert row["billing_mode"] == "chat_completions"
        assert row["message_count"] == 7
        assert config["model"] == "new/model"
        assert config["provider"] == "openrouter"
        assert config["gateway_runtime"] == {
            "model": "new/model",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        }
        assert "browser_model_lock" not in config
        assert config["_branched_from"] == "parent"
        assert config["future_key"] == {"keep": True}
        assert db._conn.execute("SELECT value FROM reset_sentinel").fetchone()[0] == "untouched"

        backup_db = SessionDB(backup)
        try:
            assert backup_db.get_session("stale")["model"] == "old/model"
        finally:
            backup_db.close()
    finally:
        db.close()


def test_reset_clears_stale_endpoint_keys_when_target_has_none(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        _seed_session(
            db,
            "stale",
            model="old/model",
            billing_provider="legacy",
            model_config={
                "model": "old/model",
                "provider": "legacy",
                "base_url": "https://legacy.invalid/v1",
                "api_mode": "anthropic_messages",
                "gateway_runtime": {
                    "model": "old/model",
                    "provider": "legacy",
                    "base_url": "https://legacy.invalid/v1",
                    "api_mode": "anthropic_messages",
                },
            },
        )

        db.reset_session_model_routes(
            target_model="new/model",
            target_provider="openrouter",
        )

        row = db.get_session("stale")
        config = json.loads(row["model_config"])
        assert row["billing_base_url"] is None
        assert row["billing_mode"] is None
        assert "base_url" not in config
        assert "api_mode" not in config
        assert "base_url" not in config["gateway_runtime"]
        assert "api_mode" not in config["gateway_runtime"]
    finally:
        db.close()


def test_sessions_list_model_filter_renders_audit_columns(
    tmp_path, monkeypatch, capsys
):
    import hermes_state
    from hermes_cli.sessions_cmd import cmd_sessions

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    db = SessionDB(tmp_path / "state.db")
    try:
        _seed_session(
            db,
            "stale",
            model="old/model",
            billing_provider="legacy",
            model_config={"model": "old/model", "provider": "legacy"},
            source="telegram",
        )
    finally:
        db.close()

    rc = cmd_sessions(
        Namespace(
            sessions_action="list",
            source=None,
            limit=20,
            workspace=None,
            model="old/*",
            provider="legacy",
        )
    )

    output = capsys.readouterr().out
    assert rc in (None, 0)
    assert "Model" in output and "Provider" in output and "Status" in output
    assert "old/model" in output
    assert "legacy" in output
    assert "telegram" in output
    assert "stale" in output


def test_sessions_reset_model_requires_explicit_all_scope(tmp_path, monkeypatch, capsys):
    import hermes_state
    from hermes_cli.sessions_cmd import cmd_sessions

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    db = SessionDB(tmp_path / "state.db")
    db.close()

    rc = cmd_sessions(
        Namespace(
            sessions_action="reset-model",
            all=False,
            dry_run=True,
            to="new/model",
            provider="openrouter",
        )
    )

    assert rc == 2
    assert "--all" in capsys.readouterr().out


def test_sessions_reset_model_uses_profile_default_and_prints_verification(
    tmp_path, monkeypatch, capsys
):
    import hermes_state
    import hermes_cli.sessions_cmd as sessions_cmd

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    monkeypatch.setattr(
        sessions_cmd,
        "_resolve_session_model_target",
        lambda _args: {
            "model": "new/model",
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_mode": "chat_completions",
        },
    )
    db = SessionDB(tmp_path / "state.db")
    try:
        _seed_session(
            db,
            "stale",
            model="old/model",
            billing_provider="legacy",
            model_config={"model": "old/model", "provider": "legacy"},
        )
    finally:
        db.close()

    rc = sessions_cmd.cmd_sessions(
        Namespace(
            sessions_action="reset-model",
            all=True,
            dry_run=False,
            to=None,
            provider=None,
        )
    )

    output = capsys.readouterr().out
    assert rc in (None, 0)
    assert "backup:" in output
    assert "remaining non-target sessions: 0" in output


def test_doctor_route_diagnostics_are_read_only(tmp_path):
    from hermes_cli.doctor import _session_model_route_diagnostics

    db_path = tmp_path / "state.db"
    db = SessionDB(db_path)
    try:
        _seed_session(
            db,
            "stale",
            model="new/model",
            billing_provider="openrouter",
            model_config={"model": "old/model", "provider": "legacy"},
        )
    finally:
        db.close()
    before = db_path.stat().st_mtime_ns

    report = _session_model_route_diagnostics(db_path)

    assert report["count"] == 1
    assert report["session_ids"] == ["stale"]
    assert db_path.stat().st_mtime_ns == before
