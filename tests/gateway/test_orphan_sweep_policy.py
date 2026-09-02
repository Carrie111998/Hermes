"""The orphaned-session sweep must obey the user's reset policy.

The sweep exists because these rows never reach ``_session_expiry_watcher``,
so their reset policy is never evaluated. The first version drew the wrong
conclusion from that: if nothing else will close them, close them on age
alone. That substitutes a flat 24h rule for the user's configuration.

It matters more than an edge case, because ``SessionResetPolicy.mode``
defaults to ``"none"`` — never auto-reset. That became the default in July
2026 precisely because the old behaviour "surprised users who expected their
conversations to persist". An age-only sweep therefore auto-resets sessions on
every install that configured nothing at all, which is the exact complaint the
default was changed to fix.

These tests pin the rule: end a row only when its OWN effective policy would
have ended it, and fail CLOSED whenever the policy cannot be resolved. Keeping
a stale row costs disk. Ending a session the user asked to keep forever
destroys their context.

Also covered here: the two other ways this sweep can misbehave quietly —
resolving its DB handle through private attributes (a rename becomes a silent
permanent no-op) and ending an unbounded backlog in one pass.
"""

import asyncio
import time
import types

import pytest


DAY = 86400.0


# ── doubles ──────────────────────────────────────────────────────────────────

class _Policy:
    def __init__(self, mode="none", idle_minutes=1440):
        self.mode = mode
        self.idle_minutes = idle_minutes


class _Config:
    """Stands in for GatewayConfig's resolution order.

    Mirrors get_reset_policy: platform override > type override > default.
    """

    def __init__(self, default=None, by_platform=None, by_type=None):
        self.default_reset_policy = default or _Policy()
        self.reset_by_platform = by_platform or {}
        self.reset_by_type = by_type or {}

    def get_reset_policy(self, platform=None, session_type=None):
        if platform is not None and platform in self.reset_by_platform:
            return self.reset_by_platform[platform]
        if session_type and session_type in self.reset_by_type:
            return self.reset_by_type[session_type]
        return self.default_reset_policy


class _Store:
    def __init__(self, config, live_keys=(), active=()):
        self.config = config
        self._entries = {k: object() for k in live_keys}
        self._active = set(active)
        self.live_keys_calls = 0

    def live_session_keys(self):
        self.live_keys_calls += 1
        return set(self._entries)

    def _has_active_processes_safe(self, key, *, context):
        return key in self._active


class _DB:
    def __init__(self, rows):
        self._rows = rows
        self.ended = []
        self.list_calls = 0

    def list_orphaned_open_sessions(self, *, older_than_seconds):
        self.list_calls += 1
        cutoff = time.time() - older_than_seconds
        return [r for r in self._rows if r["last_active"] < cutoff]

    def end_session(self, session_id, reason):
        self.ended.append((session_id, reason))


def _runner(rows, config, *, live_keys=(), active=()):
    """Build a minimal object carrying the real methods under test."""
    from gateway.run import GatewayRunner

    r = types.SimpleNamespace()
    db = _DB(rows)
    r._session_db = types.SimpleNamespace(_db=db)
    r.session_store = _Store(config, live_keys, active)
    r._reconcile_orphaned_open_sessions = types.MethodType(
        GatewayRunner._reconcile_orphaned_open_sessions, r)
    r._orphan_sweep_policy_permits_end = types.MethodType(
        GatewayRunner._orphan_sweep_policy_permits_end, r)
    return r, db


def _row(sid, *, idle_days=30, source="api_server", key=None, chat_type=None):
    return {
        "id": sid,
        "session_key": key,
        "source": source,
        "chat_type": chat_type,
        "last_active": time.time() - idle_days * DAY,
    }


def _sweep(runner):
    return asyncio.run(runner._reconcile_orphaned_open_sessions())


# ── 1. the reset policy must be consulted ────────────────────────────────────

def test_mode_none_is_never_swept():
    """The SHIPPED DEFAULT. This is the regression that matters most."""
    runner, db = _runner([_row("keep-me")], _Config(_Policy("none")))
    assert _sweep(runner) == 0
    assert db.ended == [], (
        "ended a session whose policy is 'never auto-reset' — this is the "
        "default, so it would fire on installs that configured nothing"
    )


def test_idle_mode_sweeps_only_past_its_own_window():
    """A 120-minute idle policy: 30 days idle is well past it."""
    cfg = _Config(_Policy("idle", idle_minutes=120))
    runner, db = _runner([_row("stale", idle_days=30)], cfg)
    assert _sweep(runner) == 1
    assert db.ended == [("stale", "orphaned_expiry")]


def test_idle_mode_respects_a_window_longer_than_the_floor():
    """idle_minutes can exceed the 24h sweep floor; the policy still wins.

    A 90-day idle policy must not be cut short at 24h just because the sweep's
    own floor has passed.
    """
    cfg = _Config(_Policy("idle", idle_minutes=90 * 24 * 60))
    runner, db = _runner([_row("young", idle_days=30)], cfg)
    assert _sweep(runner) == 0
    assert db.ended == []


def test_daily_mode_sweeps_since_a_boundary_has_certainly_passed():
    """Only rows already >24h idle reach the check, so a 04:00 boundary passed."""
    runner, db = _runner([_row("d", idle_days=30)], _Config(_Policy("daily")))
    assert _sweep(runner) == 1


def test_both_mode_uses_the_idle_leg():
    cfg = _Config(_Policy("both", idle_minutes=120))
    runner, _ = _runner([_row("b", idle_days=30)], cfg)
    assert _sweep(runner) == 1


def test_platform_override_beats_the_default():
    """A 'none' pin on one platform must survive a permissive global default."""
    from gateway.config import Platform

    cfg = _Config(
        default=_Policy("idle", idle_minutes=60),
        by_platform={Platform("matrix"): _Policy("none")},
    )
    rows = [_row("matrix-keep", source="matrix"), _row("api-go")]
    runner, db = _runner(rows, cfg)
    assert _sweep(runner) == 1
    assert [s for s, _ in db.ended] == ["api-go"]


def test_type_override_is_resolved_from_chat_type():
    """reset_by_type is reachable because the row carries chat_type."""
    cfg = _Config(
        default=_Policy("idle", idle_minutes=60),
        by_type={"dm": _Policy("none")},
    )
    rows = [_row("dm-keep", chat_type="dm"), _row("grp-go", chat_type="group")]
    runner, db = _runner(rows, cfg)
    assert _sweep(runner) == 1
    assert [s for s, _ in db.ended] == ["grp-go"]


def test_unresolvable_type_fails_closed():
    """Per-type policies configured but the row has no chat_type -> keep it."""
    cfg = _Config(default=_Policy("idle", idle_minutes=60),
                  by_type={"dm": _Policy("none")})
    runner, db = _runner([_row("unknown-type", chat_type=None)], cfg)
    assert _sweep(runner) == 0


def test_unrecognised_mode_fails_closed():
    """A mode from a newer config must not be treated as permission."""
    runner, _ = _runner([_row("x")], _Config(_Policy("quantum")))
    assert _sweep(runner) == 0


def test_missing_config_fails_closed():
    """No resolvable config at all -> end nothing."""
    runner, db = _runner([_row("x")], _Config(_Policy("idle", idle_minutes=1)))
    runner.session_store.config = None
    assert _sweep(runner) == 0


# ── 2. no silent no-op, no unlocked index read ───────────────────────────────

def test_missing_db_handle_warns_instead_of_returning_zero(caplog):
    """A rename of the private chain must be visible, not silent.

    `getattr(getattr(self,'_session_db',None),'_db',None)` returning None
    yields "0 orphans" forever, indistinguishable from a healthy install.
    """
    from gateway.run import GatewayRunner

    r = types.SimpleNamespace()
    r._session_db = types.SimpleNamespace()  # no ._db -> chain broken
    r.session_store = _Store(_Config(_Policy("idle", idle_minutes=1)))
    r._reconcile_orphaned_open_sessions = types.MethodType(
        GatewayRunner._reconcile_orphaned_open_sessions, r)

    with caplog.at_level("WARNING"):
        assert asyncio.run(r._reconcile_orphaned_open_sessions()) == 0
    assert any("Orphaned-session sweep disabled" in m for m in caplog.messages), (
        f"broken attribute chain produced no warning: {caplog.messages}"
    )


def test_the_unavailable_warning_is_not_repeated_every_tick(caplog):
    """It runs every 300s forever; one warning, not a log flood."""
    from gateway.run import GatewayRunner

    r = types.SimpleNamespace()
    r._session_db = types.SimpleNamespace()
    r.session_store = _Store(_Config())
    r._reconcile_orphaned_open_sessions = types.MethodType(
        GatewayRunner._reconcile_orphaned_open_sessions, r)
    with caplog.at_level("WARNING"):
        for _ in range(5):
            asyncio.run(r._reconcile_orphaned_open_sessions())
    hits = [m for m in caplog.messages if "sweep disabled" in m]
    assert len(hits) == 1, f"warned {len(hits)} times across 5 ticks"


def test_live_index_is_read_through_the_locked_accessor():
    """Not `session_store._entries` directly — that races the rebuild paths."""
    cfg = _Config(_Policy("idle", idle_minutes=1))
    runner, db = _runner([_row("orphan")], cfg, live_keys=("some:other:key",))
    _sweep(runner)
    assert runner.session_store.live_keys_calls == 1, (
        "live_session_keys() was not used; the sweep read _entries unlocked"
    )


def test_row_still_in_the_routing_index_is_left_to_the_in_memory_path():
    cfg = _Config(_Policy("idle", idle_minutes=1))
    runner, db = _runner([_row("live", key="k1")], cfg, live_keys=("k1",))
    assert _sweep(runner) == 0
    assert db.ended == []


def test_row_with_live_background_process_is_skipped():
    cfg = _Config(_Policy("idle", idle_minutes=1))
    runner, db = _runner([_row("busy", key="k2")], cfg, active=("k2",))
    assert _sweep(runner) == 0


# ── 3. the first-run burst is bounded ────────────────────────────────────────

def test_backlog_is_capped_per_tick():
    """703 open rows were observed on one install; do not end them in one pass."""
    from gateway.run import _ORPHAN_SWEEP_MAX_PER_TICK

    cfg = _Config(_Policy("idle", idle_minutes=1))
    rows = [_row(f"s{i}") for i in range(_ORPHAN_SWEEP_MAX_PER_TICK * 4)]
    runner, db = _runner(rows, cfg)
    closed = _sweep(runner)
    assert closed == _ORPHAN_SWEEP_MAX_PER_TICK
    assert len(db.ended) == _ORPHAN_SWEEP_MAX_PER_TICK


def test_backlog_drains_across_successive_ticks():
    """Capping must not strand rows — later passes pick up the remainder."""
    from gateway.run import _ORPHAN_SWEEP_MAX_PER_TICK as CAP

    cfg = _Config(_Policy("idle", idle_minutes=1))
    rows = [_row(f"s{i}") for i in range(CAP + 7)]
    runner, db = _runner(rows, cfg)

    # end_session must actually remove rows from the selector, as it does in
    # the real DB (ended_at IS NOT NULL), or this test proves nothing.
    real_end = db.end_session

    def _end(sid, reason):
        real_end(sid, reason)
        db._rows = [r for r in db._rows if r["id"] != sid]

    db.end_session = _end

    assert _sweep(runner) == CAP
    assert _sweep(runner) == 7
    assert _sweep(runner) == 0
    assert len(db.ended) == CAP + 7


def test_cap_is_small_enough_to_bound_one_tick():
    from gateway.run import _ORPHAN_SWEEP_MAX_PER_TICK

    assert 1 <= _ORPHAN_SWEEP_MAX_PER_TICK <= 200


def test_ending_runs_off_the_event_loop():
    """end_session is blocking sqlite I/O; it must not run inline on the loop.

    Asserted by recording the thread the DB writes happen on and comparing it
    to the loop's thread.
    """
    import threading

    cfg = _Config(_Policy("idle", idle_minutes=1))
    runner, db = _runner([_row("a"), _row("b")], cfg)
    seen = []
    real_end = db.end_session
    db.end_session = lambda s, r: (seen.append(threading.get_ident()), real_end(s, r))

    async def _go():
        loop_thread = threading.get_ident()
        await runner._reconcile_orphaned_open_sessions()
        return loop_thread

    loop_thread = asyncio.run(_go())
    assert seen, "no end_session calls recorded"
    assert all(t != loop_thread for t in seen), (
        "end_session ran on the event-loop thread; a slow disk would stall "
        "every other gateway task behind this sweep"
    )


# ── the reason string is load-bearing ────────────────────────────────────────

def test_rows_are_ended_not_deleted_with_a_distinguishable_reason():
    cfg = _Config(_Policy("idle", idle_minutes=1))
    runner, db = _runner([_row("x")], cfg)
    _sweep(runner)
    assert db.ended == [("x", "orphaned_expiry")]
