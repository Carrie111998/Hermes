"""Contract tests for persistent ``session.workspace.set`` workspace mutation.

Unlike ``session.cwd.set`` (which addresses a gateway-local live-session id),
this RPC addresses the durable SessionDB id.  It must therefore work for both
live sessions and historical sessions that have no in-memory agent.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_state import SessionDB
import tools.terminal_tool as terminal_tool
import tui_gateway.server as server


METHOD = "session.workspace.set"


def _rpc(**params):
    return server.handle_request(
        {"jsonrpc": "2.0", "id": "workspace-set", "method": METHOD, "params": params}
    )


def _assert_error(response, code: int, message: str) -> None:
    assert response == {
        "jsonrpc": "2.0",
        "id": "workspace-set",
        "error": {"code": code, "message": message},
    }


@pytest.fixture()
def gateway(tmp_path, monkeypatch):
    """Use only temp HOME/HERMES_HOME state, including named-profile DBs."""
    fake_home = tmp_path / "home"
    hermes_home = fake_home / ".hermes"
    hermes_home.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(server, "_hermes_home", hermes_home)

    previous_db = server._db
    server._sessions.clear()
    db = SessionDB(db_path=hermes_home / "state.db")
    server._db = db
    try:
        yield SimpleNamespace(
            db=db,
            fake_home=fake_home,
            hermes_home=hermes_home,
        )
    finally:
        server._sessions.clear()
        server._db = previous_db
        db.close()


def test_live_idle_session_uses_set_cwd_semantics_and_emits_info(
    gateway, tmp_path, monkeypatch
):
    durable_id = "20260802_120000_live01"
    ui_session_id = "live-ui"
    old_cwd = tmp_path / "old"
    workspace = gateway.fake_home / "workspace"
    old_cwd.mkdir()
    workspace.mkdir()
    gateway.db.create_session(durable_id, source="desktop", cwd=str(old_cwd))

    live = {
        "agent": object(),
        "cwd": str(old_cwd),
        "cwd_from_settle": True,
        "explicit_cwd": False,
        "running": False,
        "session_key": durable_id,
    }
    server._sessions[ui_session_id] = live

    registered = []
    cleaned = []
    git_meta = []
    emitted = []
    monkeypatch.setattr(
        terminal_tool,
        "register_task_env_overrides",
        lambda session_id, overrides: registered.append((session_id, overrides)),
    )
    monkeypatch.setattr(terminal_tool, "cleanup_vm", lambda session_id: cleaned.append(session_id))
    monkeypatch.setattr(
        server,
        "_persist_session_git_meta",
        lambda session, cwd: git_meta.append((session, cwd)),
    )
    expected_info = {
        "cwd": str(workspace),
        "branch": "feature/live",
        "project": {"id": "project-live"},
        "running": False,
    }
    monkeypatch.setattr(server, "_session_info", lambda agent, session: expected_info)
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, session_id, payload=None: emitted.append(
            (event, session_id, payload)
        ),
    )

    response = _rpc(
        session_id=durable_id,
        # Exercises both ~ expansion and normalization of a redundant segment.
        cwd="~/workspace/../workspace",
    )

    assert response["result"] == expected_info
    assert live["cwd"] == str(workspace)
    assert live["explicit_cwd"] is True
    assert live["cwd_from_settle"] is False
    assert gateway.db.get_session(durable_id)["cwd"] == str(workspace)
    assert registered == [(durable_id, {"cwd": str(workspace)})]
    assert cleaned == [durable_id]
    assert git_meta == [(live, str(workspace))]
    assert emitted == [("session.info", ui_session_id, expected_info)]


def test_live_busy_session_is_rejected_without_mutation(gateway, tmp_path, monkeypatch):
    durable_id = "20260802_120001_busy01"
    old_cwd = tmp_path / "old"
    requested = tmp_path / "requested"
    old_cwd.mkdir()
    requested.mkdir()
    gateway.db.create_session(durable_id, source="desktop", cwd=str(old_cwd))
    live = {
        "agent": object(),
        "cwd": str(old_cwd),
        "explicit_cwd": False,
        "running": True,
        "session_key": durable_id,
    }
    server._sessions["busy-ui"] = live
    cleaned = []
    monkeypatch.setattr(terminal_tool, "cleanup_vm", lambda sid: cleaned.append(sid))

    response = _rpc(session_id=durable_id, cwd=str(requested))

    _assert_error(response, 4009, "session busy")
    assert live["cwd"] == str(old_cwd)
    assert live["explicit_cwd"] is False
    assert gateway.db.get_session(durable_id)["cwd"] == str(old_cwd)
    assert cleaned == []


def test_inactive_session_is_updated_in_requested_profile(
    gateway, tmp_path, monkeypatch
):
    durable_id = "20260802_120002_profile01"
    old_default = tmp_path / "default-old"
    old_profile = tmp_path / "profile-old"
    workspace = tmp_path / "profile-workspace"
    for path in (old_default, old_profile, workspace):
        path.mkdir()

    # Same durable id in two profiles makes a wrong-profile write observable.
    gateway.db.create_session(durable_id, source="desktop", cwd=str(old_default))
    profile_home = gateway.hermes_home / "profiles" / "work"
    profile_home.mkdir(parents=True)
    profile_db = SessionDB(db_path=profile_home / "state.db")
    profile_db.create_session(durable_id, source="desktop", cwd=str(old_profile))
    profile_db.close()

    update_calls = []
    original_update = SessionDB.update_session_cwd

    def record_update(self, session_id, cwd, git_branch=None, git_repo_root=None):
        update_calls.append((session_id, cwd, git_branch, git_repo_root))
        return original_update(self, session_id, cwd, git_branch, git_repo_root)

    monkeypatch.setattr(SessionDB, "update_session_cwd", record_update)
    monkeypatch.setattr(server, "_git_branch_for_cwd", lambda cwd: "feature/profile")
    monkeypatch.setattr(
        server,
        "_project_info_for_cwd",
        lambda cwd: {"id": "project-profile", "primary_path": cwd},
    )

    response = _rpc(session_id=durable_id, profile="work", cwd=str(workspace))

    assert response["result"] == {
        "session_id": durable_id,
        "cwd": str(workspace),
        "branch": "feature/profile",
        "project": {"id": "project-profile", "primary_path": str(workspace)},
        "lazy": True,
    }
    assert update_calls and update_calls[0][:2] == (durable_id, str(workspace))
    assert gateway.db.get_session(durable_id)["cwd"] == str(old_default)
    check = SessionDB(db_path=profile_home / "state.db")
    try:
        assert check.get_session(durable_id)["cwd"] == str(workspace)
    finally:
        check.close()


def test_unknown_durable_id_is_stable_error_without_db_mutation(gateway, tmp_path):
    known_id = "20260802_120003_known01"
    old_cwd = tmp_path / "old"
    requested = tmp_path / "requested"
    old_cwd.mkdir()
    requested.mkdir()
    gateway.db.create_session(known_id, source="desktop", cwd=str(old_cwd))

    response = _rpc(session_id="does-not-exist", cwd=str(requested))

    _assert_error(response, 4007, "session not found")
    assert gateway.db.get_session(known_id)["cwd"] == str(old_cwd)


def test_missing_session_id_is_stable_error_without_db_mutation(gateway, tmp_path):
    known_id = "20260802_120004_known02"
    old_cwd = tmp_path / "old"
    requested = tmp_path / "requested"
    old_cwd.mkdir()
    requested.mkdir()
    gateway.db.create_session(known_id, source="desktop", cwd=str(old_cwd))

    response = _rpc(cwd=str(requested))

    _assert_error(response, 4006, "session_id required")
    assert gateway.db.get_session(known_id)["cwd"] == str(old_cwd)


def test_missing_cwd_is_stable_error_without_db_mutation(gateway, tmp_path):
    durable_id = "20260802_120005_missingcwd"
    old_cwd = tmp_path / "old"
    old_cwd.mkdir()
    gateway.db.create_session(durable_id, source="desktop", cwd=str(old_cwd))

    response = _rpc(session_id=durable_id)

    _assert_error(response, 4016, "cwd required")
    assert gateway.db.get_session(durable_id)["cwd"] == str(old_cwd)


@pytest.mark.parametrize("candidate_kind", ["missing", "file"])
def test_non_directory_is_stable_error_without_db_mutation(
    gateway, tmp_path, candidate_kind
):
    durable_id = f"20260802_120006_{candidate_kind}"
    old_cwd = tmp_path / "old"
    candidate = tmp_path / candidate_kind
    old_cwd.mkdir()
    if candidate_kind == "file":
        candidate.write_text("not a directory\n", encoding="utf-8")
    gateway.db.create_session(durable_id, source="desktop", cwd=str(old_cwd))

    response = _rpc(session_id=durable_id, cwd=str(candidate))

    _assert_error(
        response,
        4017,
        f"working directory does not exist: {os.fspath(candidate)}",
    )
    assert gateway.db.get_session(durable_id)["cwd"] == str(old_cwd)
