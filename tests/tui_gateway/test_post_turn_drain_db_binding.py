"""The post-turn notification drain must stay bound to the turn thread's home.

``_run_prompt_submit`` starts a daemon turn thread. After the turn's ``finally``
releases the per-turn ``HERMES_HOME`` override, the thread drains completion
notifications that arrived mid-turn and calls ``claim_event_delivery`` /
``complete_event_delivery`` / ``release_event_delivery`` — which reach
``tools.async_delegation._connect()`` -> ``_db_path()`` -> ``parent.mkdir()`` +
``sqlite3.connect()``, CREATING ``<home>/state.db``.

Why the detector never flagged it: it reports only ONE env/write pair per
candidate, and the turn thread already has one (``_sync_agent_model_with_config``
-> ``load_config``). This second late resolve was masked behind it — the exact
corollary recorded on GBrain ``concepts/import-time-hermes-home-snapshot-bug``.

WHICH home is the right one to capture is the whole design question here, and
it is NOT the same answer as the notification poller's:

  * The turn thread sets its own override from ``session["profile_home"]`` for a
    resumed remote profile — but the drain runs AFTER that override is reset, so
    today it resolves the PROCESS home, not the profile home.
  * The session's notification poller captured ``_db_path()`` under the process
    home too. ``claim_event_delivery`` is a cross-consumer claim: if the poller
    and this drain used different databases, the claim could not stop double
    delivery. They must agree.
  * ``threading.Thread`` does not propagate contextvars, so an override active
    on whichever thread called ``_run_prompt_submit`` never reaches the turn
    thread anyway.

So the capture belongs at the TOP of the turn thread, before the per-turn
override is installed — thread start, in the thread's own context, which is
exactly what the drain resolves today. Capturing at ``_run_prompt_submit``
entry (on the caller's thread) would be subtly wrong.
"""

import inspect
import threading
import types

import pytest

from tui_gateway import server


def _session(**extra):
    return {
        "agent": types.SimpleNamespace(),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "image_counter": 0,
        "cols": 80,
        "slash_worker": None,
        "show_reasoning": False,
        "tool_progress_mode": "all",
        **extra,
    }


@pytest.fixture()
def homes(tmp_path, monkeypatch):
    home_a = tmp_path / "home_a"
    home_b = tmp_path / "home_b"
    home_a.mkdir()
    home_b.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home_a))
    return home_a, home_b


def _delegation_event(sid):
    return {
        "type": "async_delegation",
        "delegation_id": "deleg_post_turn",
        "origin_ui_session_id": sid,
        "session_id": "proc_post_turn",
        "command": "echo hi",
        "exit_code": 0,
        "output": "hi",
    }


def _stub_drain(monkeypatch, sid, drained):
    from tools.process_registry import process_registry

    monkeypatch.setattr(
        process_registry, "drain_notifications", lambda **kw: list(drained)
    )
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **kw: None)


# ---------------------------------------------------------------------------
# The seam has to exist to be regression-tested at all.
# ---------------------------------------------------------------------------


def test_the_drain_is_module_level_and_testable():
    """A closure inside _run_prompt_submit cannot be bound or tested."""
    assert callable(getattr(server, "_drain_post_turn_notifications", None)), (
        "the post-turn drain is still a closure inside _run_prompt_submit — "
        "extract it so the captured db path can be carried and asserted"
    )


# ---------------------------------------------------------------------------
# The carry: a drain running after the env moved must not follow it.
# ---------------------------------------------------------------------------


def test_post_turn_drain_stays_with_the_captured_home(homes, monkeypatch):
    home_a, home_b = homes
    sid = "sid_post_turn"
    sess = _session()
    _stub_drain(monkeypatch, sid, [(_delegation_event(sid), "[IMPORTANT: done]")])

    # The moment monkeypatch teardown restores the env under the turn thread.
    monkeypatch.setenv("HERMES_HOME", str(home_b))

    server._drain_post_turn_notifications(
        "rid-1", sid, sess, db_path=home_a / "state.db"
    )

    assert not (home_b / "state.db").exists(), (
        "the post-turn drain followed HERMES_HOME after the env moved — on a "
        "real run that call CREATES ~/.hermes/state.db from a turn thread"
    )
    assert (home_a / "state.db").exists(), (
        "delivery never reached the db captured at turn start"
    )


def test_post_turn_drain_degrades_when_its_captured_home_is_gone(homes, monkeypatch, tmp_path):
    """A turn bound to a deleted tmp_path must not recreate it, and must not crash."""
    _, home_b = homes
    sid = "sid_gone"
    sess = _session()
    gone = tmp_path / "deleted_home"  # never created
    _stub_drain(monkeypatch, sid, [(_delegation_event(sid), "[IMPORTANT: done]")])
    monkeypatch.setenv("HERMES_HOME", str(home_b))

    server._drain_post_turn_notifications(
        "rid-1", sid, sess, db_path=gone / "state.db"
    )

    assert not gone.exists(), "the drain recreated a home that no longer exists"
    assert not (home_b / "state.db").exists(), "the drain fell back to the live home"


def test_direct_callers_still_resolve_live(homes, monkeypatch):
    """Passing no path keeps today's behaviour for any non-deferred caller."""
    home_a, _ = homes
    sid = "sid_live"
    _stub_drain(monkeypatch, sid, [(_delegation_event(sid), "[IMPORTANT: done]")])

    server._drain_post_turn_notifications("rid-1", sid, _session())

    assert (home_a / "state.db").exists()


# ---------------------------------------------------------------------------
# The capture moment — this is the design decision, so assert it directly.
# ---------------------------------------------------------------------------


def test_the_capture_precedes_the_per_turn_home_override():
    """Capture at the TOP of the turn thread, before session["profile_home"].

    Capturing after ``set_hermes_home_override`` would bind the resumed remote
    profile's home, which disagrees with the home the session's notification
    poller captured — and the two share a cross-consumer delivery claim.
    """
    src = inspect.getsource(server._run_prompt_submit)

    assert "_drain_post_turn_notifications(" in src, (
        "the turn thread no longer calls the extracted drain"
    )
    drain_call = src[src.index("_drain_post_turn_notifications(") :]
    drain_call = drain_call[: drain_call.index(")") + 1]
    assert "db_path=" in drain_call, (
        "the turn thread calls the drain without carrying a captured db path"
    )

    capture_idx = src.index("_db_path")
    override_idx = src.index("set_hermes_home_override")
    assert capture_idx < override_idx, (
        "the db path is captured AFTER the per-turn HERMES_HOME override is "
        "installed, so it binds the resumed profile's home instead of the "
        "process home the notification poller shares a delivery claim with"
    )
