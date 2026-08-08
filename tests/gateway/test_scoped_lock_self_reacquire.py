"""A process must be able to reacquire a scoped lock it already owns.

``acquire_scoped_lock`` gated self-reacquisition on ``pid`` AND ``start_time``
both matching. ``start_time`` is ``None`` whenever the platform cannot report
it (macOS and Windows have no ``/proc``, and psutil can fail), so the two
writes can disagree. When they did, a gateway reacquiring its own lock after a
reconnect fell through to the staleness path and reported its own live pid as
a foreign holder of its own token.

The ``start_time`` guard exists to catch pid reuse by a *different* process.
That cannot happen for a live process's own pid, so it never belonged in the
self-ownership test.
"""

from __future__ import annotations

import json
import os

import pytest

from gateway import status as gw_status


@pytest.fixture(autouse=True)
def _isolated_lock_dir(tmp_path, monkeypatch):
    """Never touch the machine-global lock dir from a test."""
    monkeypatch.setattr(gw_status, "_get_lock_dir", lambda: tmp_path)
    return tmp_path


def _lock_path(lock_dir, scope):
    existing = list(lock_dir.glob("*"))
    assert existing, "expected the acquire call to have written a lock file"
    return existing[0]


def _rewrite(path, **changes):
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(changes)
    path.write_text(json.dumps(record), encoding="utf-8")
    return record


def _as_live_gateway(monkeypatch, *, start_time):
    """Make this process look to the lock code like a real running gateway.

    Production gateways satisfy ``_looks_like_gateway_process``; a pytest
    process does not, which is why an unpatched run would otherwise mark its
    own record stale and take the lock through the staleness path instead of
    failing the way the report describes.
    """
    monkeypatch.setattr(gw_status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(gw_status, "_looks_like_gateway_process", lambda pid: True)
    monkeypatch.setattr(gw_status, "_get_process_start_time", lambda pid: start_time)


def test_reacquires_when_the_stored_start_time_is_null(_isolated_lock_dir, monkeypatch):
    """The reported shape: our stored record has start_time=None.

    The platform can now report a start_time, so the two disagree and the
    conjoined self-ownership test fails on a lock this very process holds.
    """
    gw_status.acquire_scoped_lock("discord-bot-token", "tok-1")
    path = _lock_path(_isolated_lock_dir, "discord-bot-token")
    _rewrite(path, start_time=None)
    _as_live_gateway(monkeypatch, start_time=123.0)

    ok, existing = gw_status.acquire_scoped_lock("discord-bot-token", "tok-1")

    assert ok is True, (
        "the gateway could not reacquire its own lock; it reported its own live "
        f"pid ({os.getpid()}) as a foreign holder of its own token"
    )


def test_reacquires_when_the_platform_cannot_report_start_time(
    _isolated_lock_dir, monkeypatch
):
    """The mirror case: the stored value exists, the live probe returns None."""
    gw_status.acquire_scoped_lock("discord-bot-token", "tok-1")
    path = _lock_path(_isolated_lock_dir, "discord-bot-token")
    _rewrite(path, start_time=1.0)
    _as_live_gateway(monkeypatch, start_time=None)

    ok, _ = gw_status.acquire_scoped_lock("discord-bot-token", "tok-1")

    assert ok is True


def test_plain_reacquire_still_works(_isolated_lock_dir):
    """Guard: the untouched happy path is unchanged."""
    assert gw_status.acquire_scoped_lock("discord-bot-token", "tok-1")[0] is True
    assert gw_status.acquire_scoped_lock("discord-bot-token", "tok-1")[0] is True


def test_reacquire_refreshes_the_record_on_disk(_isolated_lock_dir):
    """Self-reacquisition must rewrite the record, not just report success."""
    gw_status.acquire_scoped_lock("discord-bot-token", "tok-1")
    path = _lock_path(_isolated_lock_dir, "discord-bot-token")
    # The lock file is keyed by scope+identity, so the reacquire must reuse
    # the same identity to land on the same file.
    _rewrite(path, start_time=None, scope="bogus-scope")

    gw_status.acquire_scoped_lock("discord-bot-token", "tok-1")

    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["pid"] == os.getpid()
    assert written["scope"] == "discord-bot-token", "the stale record was not refreshed"


def test_a_live_foreign_gateway_still_holds_its_lock(_isolated_lock_dir, monkeypatch):
    """Guard: this must not turn into 'always steal the lock'.

    A record naming a different, live, gateway-looking pid stays held.
    """
    gw_status.acquire_scoped_lock("discord-bot-token", "tok-1")
    path = _lock_path(_isolated_lock_dir, "discord-bot-token")
    foreign_pid = os.getpid() + 1
    _rewrite(path, pid=foreign_pid, start_time=None)

    monkeypatch.setattr(gw_status, "_pid_exists", lambda pid: True)
    monkeypatch.setattr(gw_status, "_looks_like_gateway_process", lambda pid: True)

    ok, existing = gw_status.acquire_scoped_lock("discord-bot-token", "tok-1")

    assert ok is False, "a live foreign gateway's lock must not be taken"
    assert existing is not None and existing.get("pid") == foreign_pid
