"""Regression coverage for multiplexed Desktop session change events."""

from __future__ import annotations

import os

from tui_gateway import server


def _touch(path, mtime_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    os.utime(path, ns=(mtime_ns, mtime_ns))


def test_sessions_signature_moves_when_a_named_profile_changes(monkeypatch, tmp_path):
    """A Bot Chat write in any named profile must emit sessions.changed."""
    _touch(tmp_path / "state.db", 100)
    _touch(tmp_path / "profiles" / "productions" / "state.db", 200)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)

    assert server._sessions_sig() == 200


def test_sessions_signature_includes_named_profile_wal(monkeypatch, tmp_path):
    _touch(tmp_path / "state.db", 100)
    _touch(tmp_path / "profiles" / "editorial" / "state.db-wal", 300)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)

    assert server._sessions_sig() == 300


def test_sessions_signature_includes_root_home_wal(monkeypatch, tmp_path):
    _touch(tmp_path / "state.db", 100)
    _touch(tmp_path / "state.db-wal", 350)
    _touch(tmp_path / "profiles" / "editorial" / "state.db", 300)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)

    assert server._sessions_sig() == 350


def test_sessions_signature_finds_siblings_from_a_named_profile_home(monkeypatch, tmp_path):
    profiles = tmp_path / "profiles"
    active = profiles / "engineering"
    _touch(active / "state.db", 100)
    _touch(profiles / "productions" / "state.db", 400)
    monkeypatch.setattr(server, "_watcher_home", lambda: active)

    assert server._sessions_sig() == 400


def test_sessions_signature_ignores_symlinked_profile_directories(monkeypatch, tmp_path):
    outside = tmp_path / "outside"
    profiles = tmp_path / "profiles"
    _touch(tmp_path / "state.db", 100)
    _touch(outside / "state.db", 500)
    profiles.mkdir()
    (profiles / "linked").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(server, "_watcher_home", lambda: tmp_path)

    assert server._sessions_sig() == 100
