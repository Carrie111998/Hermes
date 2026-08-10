from types import SimpleNamespace
from pathlib import Path

import pytest

import gateway.runtime_state as runtime_state_module
from gateway.config import GatewayConfig
from gateway.runtime_state import (
    RuntimeStateIntegrationError,
    RuntimeStateManager,
    RuntimeStateProfile,
    _resolve_db_path,
)


def test_runtime_state_path_is_profile_scoped(tmp_path):
    config = SimpleNamespace(runtime_state_db_path="runtime/runtime.db")
    profile_home = tmp_path / "profile"
    profile_home.mkdir()

    assert _resolve_db_path(config, profile_home) == (
        profile_home / "runtime" / "runtime.db"
    ).resolve()

    outside = SimpleNamespace(runtime_state_db_path=tmp_path / "outside.db")
    with pytest.raises(RuntimeStateIntegrationError, match="inside"):
        _resolve_db_path(outside, profile_home)


def test_gateway_config_accepts_profile_scoped_runtime_state_path():
    direct = GatewayConfig.from_dict(
        {"runtime_state": {"db_path": "runtime/direct.db"}}
    )
    nested = GatewayConfig.from_dict(
        {"gateway": {"runtime_state": {"db_path": "runtime/nested.db"}}}
    )

    assert direct.runtime_state_db_path == Path("runtime/direct.db")
    assert nested.runtime_state_db_path == Path("runtime/nested.db")


def test_runtime_state_profile_records_session_and_task(tmp_path):
    config = SimpleNamespace(runtime_state_db_path=None)
    profile = RuntimeStateProfile(config, "default", tmp_path / "profile")
    try:
        session = profile.ensure_session("session-1", "user-1")
        assert session.success
        compression = profile.ensure_compression("session-1")
        assert compression.success

        lease = profile.begin_task("session-1")
        approval = profile.begin_approval("session-1", lease.task_id)
        profile.finish_approval(approval, "denied")
        profile.record_compression("session-1")
        profile.finish_task(lease, "succeeded")

        rows = profile.db.connection.execute(
            "SELECT status, owner FROM task_state "
            "WHERE profile_name = 'default' AND task_id = ?",
            (lease.task_id,),
        ).fetchone()
        assert rows == ("succeeded", None)
        approval_row = profile.db.connection.execute(
            "SELECT approval_status, owner FROM approval_state "
            "WHERE profile_name = 'default' AND approval_id = ?",
            (approval.approval_id,),
        ).fetchone()
        assert approval_row == ("denied", None)
        compression_row = profile.db.connection.execute(
            "SELECT compression_status, owner FROM compression_state "
            "WHERE profile_name = 'default' AND session_id = 'session-1'"
        ).fetchone()
        assert compression_row == ("succeeded", None)
    finally:
        profile.close()


def test_manager_preflight_rejects_path_escape(monkeypatch, tmp_path):
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    monkeypatch.setattr(runtime_state_module, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(runtime_state_module, "get_hermes_home", lambda: profile_home)

    manager = RuntimeStateManager(
        SimpleNamespace(runtime_state_db_path="../outside.db")
    )
    with pytest.raises(RuntimeStateIntegrationError, match="inside"):
        manager.preflight()
    manager.close()


def test_empty_active_profile_name_fails_closed(monkeypatch):
    monkeypatch.setattr(runtime_state_module, "get_active_profile_name", lambda: "")

    with pytest.raises(RuntimeStateIntegrationError, match="empty"):
        runtime_state_module._profile_name()
