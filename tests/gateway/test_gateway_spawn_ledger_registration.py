"""Behavioral coverage for gateway spawn-ledger registration."""

from __future__ import annotations

import os


def test_gateway_registration_writes_identity_to_isolated_ledger(tmp_path, monkeypatch):
    from gateway import run
    from hermes_cli import process_identity as pi
    from hermes_cli import profiles

    ledger = tmp_path / "spawn-ledger.json"
    monkeypatch.setattr(pi, "_ledger_path", lambda: ledger)
    monkeypatch.setattr(pi, "install_id", lambda *args, **kwargs: "gateway-install")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "jarvis")

    run._register_gateway_spawn_ledger()

    entries = pi._read_ledger(ledger)
    assert entries is not None
    gateway_entries = [entry for entry in entries if entry["purpose"] == "gateway"]
    assert len(gateway_entries) == 1
    entry = gateway_entries[0]
    assert entry["pid"] == os.getpid()
    assert isinstance(entry["create_time"], float)
    assert entry["create_time"] > 0
    assert entry["install"] == "gateway-install"
    assert entry["profile"] == "jarvis"


def test_gateway_registration_failure_is_non_fatal(monkeypatch):
    from gateway import run
    from hermes_cli import process_identity as pi

    def fail_register(*args, **kwargs):
        raise OSError("ledger unavailable")

    monkeypatch.setattr(pi, "register_self", fail_register)
    run._register_gateway_spawn_ledger()
