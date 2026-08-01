"""Guard: `hermes update` must detect a stale Windows updater binary.

The Tauri-based ``Hermes-Setup.exe`` staged at ``HERMES_HOME`` is NOT rebuilt
by ``hermes update`` (only the desktop app and web UI are). A binary built
before the lock-handoff fix (upstream commit 8c76fe19f) deadlocks every
desktop in-app update on Windows with "Another Hermes update is already
running", because it cannot pass the update marker lock it already holds to
its ``hermes update`` child.

These tests pin the detection helpers in ``hermes_cli.update_cmd``: the
staleness probe must be a read-only best-effort check (never raises, never
nags a healthy install) and the notice must point at the repair path.
"""

import os
import sys

import hermes_cli.update_cmd as update_mod


def _write_fake_updater(path, *, with_marker: bool) -> None:
    """Create a fake Windows updater binary at ``path``."""
    payload = b"\x00\x01MZfake-windows-updater\x00\x02"
    if with_marker:
        payload += update_mod._WINDOWS_UPDATER_HANDOFF_MARKER + b"\x00"
    path.write_bytes(payload)


def test_handoff_marker_string_is_present():
    """The marker we probe for must be the one the fix embedded."""
    assert update_mod._WINDOWS_UPDATER_HANDOFF_MARKER == b"HERMES_UPDATE_HANDOFF_PID"


def test_stale_detected_when_marker_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(update_mod._m(), "_is_windows", lambda: True)
    updater = tmp_path / "hermes-setup.exe"
    _write_fake_updater(updater, with_marker=False)

    def fake_home():
        return tmp_path

    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", fake_home)
    assert update_mod._windows_updater_is_stale() is True


def test_fresh_updater_not_stale(monkeypatch, tmp_path):
    monkeypatch.setattr(update_mod._m(), "_is_windows", lambda: True)
    updater = tmp_path / "hermes-setup.exe"
    _write_fake_updater(updater, with_marker=True)

    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    assert update_mod._windows_updater_is_stale() is False


def test_missing_updater_not_stale(monkeypatch, tmp_path):
    """No staged updater -> nothing to warn about (source/dev installs)."""
    monkeypatch.setattr(update_mod._m(), "_is_windows", lambda: True)

    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    assert update_mod._windows_updater_is_stale() is False


def test_non_windows_never_stale(monkeypatch):
    monkeypatch.setattr(update_mod._m(), "_is_windows", lambda: False)
    assert update_mod._windows_updater_is_stale() is False


def test_probe_never_raises_on_garbage(monkeypatch, tmp_path):
    """Unreadable/garbage updater must not raise or block an update."""
    monkeypatch.setattr(update_mod._m(), "_is_windows", lambda: True)
    updater = tmp_path / "hermes-setup.exe"
    updater.write_bytes(b"\xff\xfe\x00\x01")  # random binary, no marker

    import hermes_constants

    monkeypatch.setattr(hermes_constants, "get_hermes_home", lambda: tmp_path)
    # No exception, and a marker-less binary is treated as stale.
    assert update_mod._windows_updater_is_stale() is True


def test_notice_points_at_repair_path(capsys):
    """The stale-updater notice must tell the user how to fix it."""
    update_mod._print_windows_updater_stale_notice()
    out = capsys.readouterr().out
    assert "Stale Windows updater" in out
    assert "hermes-agent.nousresearch.com" in out
    assert "8c76fe19f" in out


def test_notice_mentions_the_failure_signature(capsys):
    """Users hitting the deadlock must recognize it in the notice."""
    update_mod._print_windows_updater_stale_notice()
    out = capsys.readouterr().out
    assert "Another Hermes update" in out
