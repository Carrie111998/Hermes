"""Kanban inline-owner vs dispatched-worker ownership hardening.

Regression coverage for the nudge-loop / double-owner bug: an INLINE owner
(a ``kanban claim`` / gateway session) holds a card as ``running`` with
``worker_pid NULL``. The dispatcher's auto-reclaim passes must NEVER release
such a claim — releasing it requeues the card to ``ready`` and the dispatcher
spawns a duplicate worker beside the inline owner (the protocol-violation /
crash cascade observed on the control-plane migration).

Auto-reclaim (TTL / heartbeat-stale / orphan reconciliation) only ever applies
to dispatcher-spawned workers, i.e. ``status='running' AND worker_pid IS NOT
NULL``. A genuinely dead dispatcher worker (worker_pid set) MUST still be
reclaimed, so crash recovery is preserved.
"""

from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture()
def kanban_home(tmp_path, monkeypatch):
    import os
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claim(conn, tt, *, worker_pid, expired=True):
    """Claim a fresh card however the caller wants (inline vs dispatched)."""
    host = kb._claimer_id().split(":", 1)[0]
    if worker_pid is None:
        # Inline owner: CLI/gateway claim, no dispatcher subprocess.
        kb.claim_task(conn, tt, claimer=f"{host}:inline-session")
    else:
        # Dispatcher-spawned worker.
        kb.claim_task(conn, tt, claimer=kb._claimer_id())
        kb._set_worker_pid(conn, tt, worker_pid)
    if expired:
        old = int(time.time()) - 3600
        conn.execute(
            "UPDATE tasks SET claim_expires = ? WHERE id = ?",
            (old, tt),
        )


def _dispatch_spawn(conn):
    spawned = []

    def _spawn(task, ws, **kw):
        spawned.append(task.id)
        return 9999

    kb.dispatch_once(
        conn,
        spawn_fn=_spawn,
        max_spawn=4,
        failure_limit=2,
        reconcile_orphans=True,
    )
    return spawned


def test_inline_owned_card_not_reclaimed(kanban_home, monkeypatch):
    """An inline-owned card (worker_pid NULL) is NEVER auto-reclaimed to ready."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tt = kb.create_task(conn, title="inline", assignee="developer")
        _claim(conn, tt, worker_pid=None)

        assert kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None) == 0
        assert kb.detect_stale_running(conn, stale_timeout_seconds=30) == []
        assert kb.reconcile_orphaned_running(conn) == []

        assert kb.get_task(conn, tt).status == "running", \
            "inline-owned card must stay running (dispatcher stayed out)"
        # And a full dispatcher tick must NOT spawn a duplicate.
        assert _dispatch_spawn(conn) == []


def test_dispatcher_worker_still_reclaimed(kanban_home, monkeypatch):
    """A dead dispatcher-spawned worker (worker_pid set) MUST still be reclaimed."""
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    with kb.connect() as conn:
        tt = kb.create_task(conn, title="dispatched", assignee="developer")
        _claim(conn, tt, worker_pid=12345)

        assert kb.release_stale_claims(conn, signal_fn=lambda _p, _s: None) >= 1
        assert kb.get_task(conn, tt).status == "ready", \
            "dead dispatcher worker reclaim -> crash recovery preserved"