"""Persistence and revocation contract for linked-browser credentials."""

from __future__ import annotations

import importlib
import stat
import sqlite3

from hermes_cli.dashboard_auth import linked_devices


def test_linked_device_persists_hash_without_plaintext_and_authenticates(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(linked_devices, "get_hermes_home", lambda: tmp_path)
    device_id, credential = linked_devices.create_or_rotate(
        label="iPhone", session_id="s", profile="default"
    )
    assert linked_devices.authenticate(credential)["id"] == device_id
    with sqlite3.connect(tmp_path / "linked_devices.sqlite3") as db:
        assert credential not in repr(
            db.execute("SELECT * FROM linked_devices").fetchall()
        )
    assert credential.encode() not in (tmp_path / "linked_devices.sqlite3").read_bytes()
    mode = stat.S_IMODE((tmp_path / "linked_devices.sqlite3").stat().st_mode)
    assert mode == 0o600


def test_linked_device_survives_module_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _device_id, credential = linked_devices.create_or_rotate(
        label="iPhone",
        session_id="persisted-session",
        profile="default",
    )

    restarted = importlib.reload(linked_devices)

    record = restarted.authenticate(credential)
    assert record is not None
    assert record["session_id"] == "persisted-session"


def test_linked_device_rotation_and_revocation(monkeypatch, tmp_path):
    monkeypatch.setattr(linked_devices, "get_hermes_home", lambda: tmp_path)
    device_id, old = linked_devices.create_or_rotate(
        label="Browser", session_id="s", profile=""
    )
    same_id, new = linked_devices.create_or_rotate(
        existing_id=device_id, label="Browser", session_id="s", profile=""
    )
    assert same_id == device_id and linked_devices.authenticate(old) is None
    assert len(linked_devices.list_devices()) == 1
    assert linked_devices.revoke(device_id) and linked_devices.authenticate(new) is None


def test_linked_device_sliding_expiry(monkeypatch, tmp_path):
    monkeypatch.setattr(linked_devices, "get_hermes_home", lambda: tmp_path)
    now = {"value": 1000}
    monkeypatch.setattr(linked_devices, "_now", lambda: now["value"])
    _, credential = linked_devices.create_or_rotate(
        label="iPad", session_id="s", profile=""
    )
    now["value"] += linked_devices.DEVICE_COOKIE_TTL_SECONDS - 1
    assert linked_devices.authenticate(credential) is not None
    now["value"] += linked_devices.DEVICE_COOKIE_TTL_SECONDS + 1
    assert linked_devices.authenticate(credential) is None
