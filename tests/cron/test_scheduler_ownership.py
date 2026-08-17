"""Shared scheduler-owner lease: gateway + desktop single-owner handoff."""

from __future__ import annotations

import threading
import time


def test_scheduler_ownership_exclusive_between_roles(tmp_path, monkeypatch):
    import cron.scheduler_ownership as so

    monkeypatch.setattr(so, "_hermes_home", lambda: tmp_path)
    so._lease_fh = None
    so._lease_fallback_path = None
    so._lease_role = None

    assert so.try_acquire_scheduler_ownership("desktop-fallback") is True
    assert so.get_scheduler_owner_role() == "desktop-fallback"

    # Simulate a second process: clear only the in-memory handle mirror is not
    # enough on fcntl hosts because the first handle still holds LOCK_EX. Drop
    # the module handle reference while keeping the OS lock alive via a local.
    held = so._lease_fh
    so._lease_fh = None
    so._lease_role = None
    assert so.try_acquire_scheduler_ownership("gateway") is False

    so._lease_fh = held
    so.release_scheduler_ownership()
    assert so.try_acquire_scheduler_ownership("gateway") is True
    assert so.get_scheduler_owner_role() == "gateway"
    so.release_scheduler_ownership()


def test_wait_for_scheduler_ownership_handoff_from_desktop(tmp_path, monkeypatch):
    import cron.scheduler_ownership as so

    monkeypatch.setattr(so, "_hermes_home", lambda: tmp_path)
    so._lease_fh = None
    so._lease_fallback_path = None
    so._lease_role = None

    assert so.try_acquire_scheduler_ownership("desktop-fallback") is True
    held = so._lease_fh

    def yield_later():
        time.sleep(0.2)
        # Restore holder and release as desktop would on gateway-live watch.
        so._lease_fh = held
        so._lease_role = "desktop-fallback"
        so.release_scheduler_ownership()

    # Detach module ownership so wait loop observes the OS lock only.
    so._lease_fh = None
    so._lease_role = None
    threading.Thread(target=yield_later, daemon=True).start()

    assert so.wait_for_scheduler_ownership("gateway", timeout_seconds=2.0, poll_seconds=0.05) is True
    assert so.get_scheduler_owner_role() == "gateway"
    so.release_scheduler_ownership()


def test_desktop_admit_rechecks_gateway_after_lease(tmp_path, monkeypatch):
    import hermes_cli.web_server as ws
    import cron.scheduler_ownership as so

    monkeypatch.setattr(so, "_hermes_home", lambda: tmp_path)
    so._lease_fh = None
    so._lease_fallback_path = None
    so._lease_role = None
    ws._desktop_scheduler_lease_fh = None
    ws._desktop_scheduler_lease_fallback_path = None

    monkeypatch.setattr(ws, "_SCHEDULER_ROLE", "local-primary")
    monkeypatch.setattr(ws, "_SSH_OWNER_NONCE", None)
    monkeypatch.setenv("HERMES_DESKTOP", "1")

    live_calls = {"n": 0}

    def gateway_live(**_k):
        live_calls["n"] += 1
        # First probe (pre-acquire) false; post-acquire true → must release.
        return live_calls["n"] >= 2

    monkeypatch.setattr(ws, "_canonical_gateway_is_live", gateway_live)

    assert ws.admit_desktop_scheduler_fallback() is False
    assert so._lease_fh is None
    assert live_calls["n"] >= 2


def test_no_fcntl_shared_lease_still_exclusive(tmp_path, monkeypatch):
    import builtins
    import cron.scheduler_ownership as so

    monkeypatch.setattr(so, "_hermes_home", lambda: tmp_path)
    so._lease_fh = None
    so._lease_fallback_path = None
    so._lease_role = None

    real_import = builtins.__import__

    def no_fcntl(name, *args, **kwargs):
        if name == "fcntl":
            raise ImportError("simulated Windows")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_fcntl)

    assert so.try_acquire_scheduler_ownership("desktop-fallback") is True
    path = so.scheduler_owner_lease_path()
    assert path.exists()
    held = so._lease_fh
    so._lease_fh = None
    so._lease_role = None
    assert so.try_acquire_scheduler_ownership("gateway") is False
    so._lease_fh = held
    so.release_scheduler_ownership()
    assert not path.exists()
