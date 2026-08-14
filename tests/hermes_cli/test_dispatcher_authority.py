from __future__ import annotations

import os
from pathlib import Path

import pytest


def test_machine_authority_is_process_global_across_profile_and_cwd(tmp_path, monkeypatch):
    from hermes_cli.dispatcher_authority import canonical_lock_path

    state = tmp_path / "state"
    monkeypatch.setenv("HERMES_STATE_ROOT", str(state))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-a"))
    first = canonical_lock_path()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "profile-b"))
    monkeypatch.chdir(tmp_path)
    assert canonical_lock_path() == first == state / "kanban" / ".dispatcher.lock"


def test_exactly_one_authority_holder_and_contender_fails_closed(tmp_path, monkeypatch):
    from hermes_cli.dispatcher_authority import AcquireState, acquire_machine_dispatcher

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    first = acquire_machine_dispatcher("test:first")
    assert first.state is AcquireState.ACQUIRED
    try:
        second = acquire_machine_dispatcher("test:second")
        assert second.state is AcquireState.CONTENDED
        assert second.lease is None
    finally:
        first.lease.release()


def test_lease_is_opaque_live_and_released_lease_is_rejected(tmp_path, monkeypatch):
    from hermes_cli.dispatcher_authority import (
        DispatcherAuthorityError,
        acquire_machine_dispatcher,
        require_dispatcher_lease,
    )

    monkeypatch.setenv("HERMES_STATE_ROOT", str(tmp_path))
    result = acquire_machine_dispatcher("test")
    validated = require_dispatcher_lease(result.lease, "tick")
    assert validated is result.lease
    result.lease.release()
    with pytest.raises(DispatcherAuthorityError):
        require_dispatcher_lease(result.lease, "tick")
    with pytest.raises(DispatcherAuthorityError):
        require_dispatcher_lease(object(), "tick")


def test_unavailable_lock_parent_fails_closed_without_fallback(tmp_path, monkeypatch):
    from hermes_cli.dispatcher_authority import AcquireState, acquire_machine_dispatcher

    not_a_dir = tmp_path / "file"
    not_a_dir.write_text("x")
    monkeypatch.setenv("HERMES_STATE_ROOT", str(not_a_dir))
    result = acquire_machine_dispatcher("test")
    assert result.state is AcquireState.UNAVAILABLE
    assert result.lease is None
    assert result.error_class


def test_status_reader_never_creates_lock_parent(tmp_path, monkeypatch):
    from hermes_cli.dispatcher_authority import read_status_no_side_effects

    state = tmp_path / "absent"
    monkeypatch.setenv("HERMES_STATE_ROOT", str(state))
    status = read_status_no_side_effects()
    assert status.healthy is False
    assert status.error_class == "missing_parent"
    assert not state.exists()
