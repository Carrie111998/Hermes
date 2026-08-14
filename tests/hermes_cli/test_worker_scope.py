from __future__ import annotations

import shutil

import pytest


def test_scope_command_pins_identity_and_uses_systemd_scope(monkeypatch):
    from hermes_cli.worker_scope import build_scoped_worker_command

    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}" if name in {"systemd-run", "systemctl"} else None)
    env = {
        "HERMES_PROFILE": "researcher",
        "HERMES_KANBAN_TASK": "t_12345678",
        "HERMES_KANBAN_RUN_ID": "91",
        "HERMES_KANBAN_CLAIM_LOCK": "host:pid",
        "HERMES_KANBAN_BOARD": "default",
        "HERMES_HOME": "/profiles/researcher",
        "TERMINAL_CWD": "/workspace",
        "HERMES_KANBAN_MODEL": "model-x",
        "HERMES_KANBAN_PROVIDER": "provider-y",
        "HERMES_KANBAN_TOOLSETS": "terminal,file",
    }
    cmd = build_scoped_worker_command(["hermes", "chat"], env=env, require_isolation=True)
    assert cmd[:3] == ["/usr/bin/systemd-run", "--user", "--scope"]
    assert any(part == "--unit=hermes-kanban-t_12345678-r91" for part in cmd)
    for key in env:
        assert any(part == f"--setenv={key}={env[key]}" for part in cmd)


def test_required_scope_fails_closed_when_backend_missing(monkeypatch):
    from hermes_cli.worker_scope import WorkerIsolationError, build_scoped_worker_command

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    with pytest.raises(WorkerIsolationError):
        build_scoped_worker_command(["hermes"], env={"HERMES_KANBAN_TASK": "t_1"}, require_isolation=True)


def test_bounded_fallback_is_explicit_when_isolation_optional(monkeypatch):
    from hermes_cli.worker_scope import build_scoped_worker_command

    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert build_scoped_worker_command(["hermes"], env={}, require_isolation=False) == ["hermes"]
