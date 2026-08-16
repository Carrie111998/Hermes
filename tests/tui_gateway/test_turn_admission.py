"""Admission: who is allowed to start a turn on a conversation, and how it refuses.

Two ``hermes serve`` processes can share one ``HERMES_HOME``, so "is this
conversation busy" is a cross-process question and the answer lives in
``state.db``. Admission polarity is set by who initiated the turn:

* machine-initiated (the auto-continue of a crash-interrupted turn) is
  fail-closed. It peeks at the conversation's turn lease before it does
  anything at all to the ``interrupted_turns`` row -- continue it or retire it
  -- and it consumes the row with a compare-and-swap that exactly one process
  can win. Anything unproven resolves to abstaining, which defers the recovery
  to the next resume rather than losing it: the record stays where it was.
* user-initiated is fail-open. It waits a bounded time with a visible notice
  and then refuses in the open, because a user who is watching would rather be
  told the session is busy than be dropped.

Caller class is carried by construction: ``_run_prompt_submit`` takes a
``user_initiated`` keyword that the ``prompt.submit`` handler sets and no
other dispatch site does. Every turn the gateway synthesizes for itself is
therefore machine class by omission, which is the fail-safe default.

The user-initiated half is dormant against engines whose ``run_conversation``
cannot accept a caller-supplied lease holder, which is every engine today.
Both arms are pinned here, so the day the engine gains the parameter the
behavior it switches on is already covered.
"""

from __future__ import annotations

import inspect
import os
import sqlite3
import threading
import time
import types

import pytest

import hermes_state
from hermes_state import SessionDB
from tui_gateway import server

_FOREIGN_OWNER = "pid=999999:platform=other"
# A lease held by a process the dead-holder probe cannot write off. Real pid,
# foreign turn: the probe never reclaims a live pid, which is the whole point.
_LIVE_HOLDER = f"pid={os.getpid()}:turn=live:platform=other"
_DEAD_HOLDER = "pid=999999:turn=dead:platform=other"


class _InlineThread:
    """Run threads synchronously so tests observe final state."""

    def __init__(self, target=None, daemon=None, args=(), kwargs=None):
        self._target = target
        self._args = args
        self._kwargs = kwargs or {}

    def start(self):
        if self._target is not None:
            self._target(*self._args, **self._kwargs)

    def is_alive(self):
        return False

    def join(self, timeout=None):
        return None


def _session(agent=None, **extra):
    return {
        "agent": agent if agent is not None else types.SimpleNamespace(),
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
        "inflight_turn": None,
        **extra,
    }


def _record(db, session_key, prompt, *, attempts=0, owner=_FOREIGN_OWNER):
    assert db.record_interrupted_turn(
        session_key, prompt, attempts=attempts, owner=owner
    )


def _read(db, session_key):
    return db.read_interrupted_turn(session_key)


@pytest.fixture()
def emits(monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload=None: captured.append((event, sid, payload)),
    )
    return captured


@pytest.fixture()
def marker_home(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    return tmp_path


@pytest.fixture()
def turn_db(monkeypatch, marker_home):
    db = SessionDB(marker_home / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    return db


@pytest.fixture()
def turn_env(monkeypatch, tmp_path, turn_db):
    """Neutralize the turn pipeline's environment-heavy side paths."""
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_wire_callbacks", lambda sid: None)
    monkeypatch.setattr(server, "_sync_agent_model_with_config", lambda sid, session: None)
    monkeypatch.setattr(server, "_session_cwd", lambda session: str(tmp_path))
    monkeypatch.setattr(server, "_register_session_cwd", lambda session: None)
    monkeypatch.setattr(server, "_tts_stream_begin", lambda: None)
    monkeypatch.setattr(server, "_sync_session_key_after_compress", lambda *a, **k: None)
    monkeypatch.setattr(server, "_get_usage", lambda agent: {})


@pytest.fixture()
def schedule_env(monkeypatch, turn_db):
    monkeypatch.setattr(server.threading, "Thread", _InlineThread)
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    monkeypatch.setattr(server, "_wait_agent", lambda session, rid, timeout=30.0: None)
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    submitted: list = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda rid, sid, session, text, **kw: submitted.append((text, kw)),
    )
    return submitted


# ── Machine-initiated admission: peek, then claim ──────────────────────


def test_a_live_holder_defers_the_continuation(emits, schedule_env, turn_db):
    """The other process is still running the turn: do not queue a second one.

    The interrupted-turn record is proof that a turn started, not that it
    stopped. While another process holds the conversation's turn lease the
    record is that process's business, so the schedule abstains and leaves the
    record untouched for a later resume that finds the conversation free.
    """
    _record(turn_db, "session-key", "the turn that is actually running")
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env
    survivor = _read(turn_db, "session-key")
    assert survivor is not None
    assert survivor["attempts"] == 0
    assert survivor["owner"] == _FOREIGN_OWNER


def test_a_live_holder_defers_a_stale_record_too(
    emits, schedule_env, turn_db, monkeypatch
):
    """The lease vetoes the policy retirements, not only the claim.

    Freshness is a number in this process's config file, and the record it
    would be applied to here belongs to a turn that is provably still running.
    A process configured with a shorter window than the one running the turn
    would otherwise force-delete a live turn's record -- the same deletion the
    owner check exists to prevent, reached by a different route. The record is
    left where it is; when the lease lapses the next resume applies the window
    to it.
    """
    _record(turn_db, "session-key", "the turn that is actually running")
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )
    # Server-side clock only: the lease row's expiry is read through
    # hermes_state's own time, so the record goes stale while the lease stays
    # live, which is the whole point of the case.
    monkeypatch.setattr(
        server, "time", types.SimpleNamespace(time=lambda: time.time() + 3600)
    )

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env
    survivor = _read(turn_db, "session-key")
    assert survivor is not None
    assert survivor["attempts"] == 0
    assert survivor["owner"] == _FOREIGN_OWNER


def test_a_live_holder_defers_a_disabled_process_too(
    emits, schedule_env, turn_db, monkeypatch
):
    """Turning auto-continue off does not license deleting a running turn's row.

    The record here is this process's OWN, which is what makes the case
    discriminating: the disabled arm's clear is owner-checked, so it would
    land. It does not, because a lease is live on the conversation and a turn
    that is running now needs its record whatever this process's config says
    about continuing turns later.
    """
    session = _session()
    server._record_interrupted_turn(session, "session-key", "own turn, still running")
    assert _read(turn_db, "session-key") is not None
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )
    monkeypatch.setattr(
        server,
        "_load_cfg",
        lambda: {"desktop": {"auto_continue": {"enabled": False}}},
    )

    result = server._maybe_schedule_auto_continue("sid", session, "session-key")

    assert result is None
    assert not schedule_env
    survivor = _read(turn_db, "session-key")
    assert survivor is not None
    assert survivor["prompt"] == "own turn, still running"


def test_a_live_holder_defers_an_exhausted_record_too(
    emits, schedule_env, turn_db
):
    """The third retirement arm, and the one that reads most like a fact.

    An attempt count at the ceiling looks like a property of the record rather
    than of the reader, but the ceiling it is compared against is this
    process's config, and the count is only evidence that earlier continuations
    were started. Neither says the turn running right now has stopped. The row
    a live lease covers is that turn's own record, so deleting it here does not
    stop a loop, it removes the trace the turn would crash into: if the turn
    then dies, no record remains and nothing recovers it at all. Deferring
    keeps the ceiling intact for the resume that follows the lease.
    """
    _record(turn_db, "session-key", "the turn that is actually running", attempts=2)
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env
    survivor = _read(turn_db, "session-key")
    assert survivor is not None
    assert survivor["attempts"] == 2
    assert survivor["owner"] == _FOREIGN_OWNER


def test_no_holder_leaves_the_policy_retirements_intact(
    schedule_env, turn_db, monkeypatch
):
    """The lease is a veto, not an exemption.

    The companion to the two above, stated here rather than left implicit in
    the freshness tests next door: with nothing holding the conversation, a
    stale record is still collected. Otherwise the peek would have turned the
    windows off.
    """
    _record(turn_db, "session-key", "old prompt")
    assert turn_db.get_session_turn_lease_holder("session-key") is None
    monkeypatch.setattr(
        server, "time", types.SimpleNamespace(time=lambda: time.time() + 3600)
    )

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env
    assert _read(turn_db, "session-key") is None


def test_a_dead_holders_lease_does_not_block_the_continuation(
    emits, schedule_env, turn_db, monkeypatch
):
    """A lease left behind by a process that died is not a running turn."""
    _record(turn_db, "session-key", "fix the flaky test")
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _DEAD_HOLDER, ttl_seconds=300
    )
    probed: list[int] = []

    def pid_exists(pid: int) -> bool:
        probed.append(pid)
        return False

    monkeypatch.setattr(
        hermes_state, "psutil", types.SimpleNamespace(pid_exists=pid_exists)
    )

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert probed == [999999]
    assert result is not None
    assert len(schedule_env) == 1


def test_an_expired_lease_does_not_block_the_continuation(
    emits, schedule_env, turn_db
):
    _record(turn_db, "session-key", "fix the flaky test")
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=0.1
    )
    time.sleep(0.2)

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is not None
    assert len(schedule_env) == 1


def test_the_claim_is_durable_before_the_kickoff(
    emits, schedule_env, turn_db, monkeypatch
):
    """The attempts bump lands in storage before anything fallible starts.

    A continuation that dies inside its own agent build must still count
    against the crash-loop ceiling, or a build that always dies re-fires
    forever.

    The bump is also the ONLY thing the claim writes. The owner stamp is the
    retire check, and a claimant that took it would leave the record's true
    owner unable to retire its own record when its turn ended -- so a claim
    that landed on a live turn would strand that turn's record for a later
    resume to re-run. The claimant stamps itself the ordinary way, in the
    turn prologue, on its way into the turn it actually runs.
    """
    seen: list = []
    monkeypatch.setattr(
        server,
        "_start_agent_build",
        lambda sid, session: seen.append(_read(turn_db, "session-key")),
    )
    _record(turn_db, "session-key", "crashy prompt")

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result["attempt"] == 1
    assert seen and seen[0]["attempts"] == 1
    assert seen[0]["owner"] == _FOREIGN_OWNER
    assert seen[0]["owner"] != server._turn_record_owner()


def test_a_turn_that_starts_between_the_read_and_the_claim_defeats_the_claim(
    emits, schedule_env, turn_db, monkeypatch
):
    """The realizable ABA, driven through the scheduler end to end.

    The gate reads an orphan at attempts=0. Another process then starts a real
    user turn: its prologue re-records the row, last-writer-wins, at attempts=0
    again -- and it does that long before the engine takes the turn lease, so
    the peek in between has nothing to see and reads the conversation as free.
    A claim whose token was the counter alone would match, and this process
    would auto-continue the prompt the other process is running right now.

    The window is injected at the peek because that is exactly where it lives:
    between the gate's read and the compare-and-swap.
    """
    _record(turn_db, "session-key", "the ORPHANED prompt", attempts=0)

    def _peek_then_a_live_turn_starts(session, key):
        _record(
            turn_db,
            "session-key",
            "the prompt the other process is running right now",
            attempts=0,
            owner="pid=424242:platform=other",
        )
        return False  # no lease yet: the engine acquires much later

    monkeypatch.setattr(
        server, "_turn_is_held_elsewhere", _peek_then_a_live_turn_starts
    )

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env
    # The live turn's record is untouched, including its attempt count.
    survivor = _read(turn_db, "session-key")
    assert survivor["prompt"] == "the prompt the other process is running right now"
    assert survivor["attempts"] == 0
    assert survivor["owner"] == "pid=424242:platform=other"


def test_a_claim_leaves_the_records_owner_able_to_retire_it(
    emits, schedule_env, turn_db
):
    """A claim must not cost the true owner the ability to close its own turn.

    ``clear_interrupted_turn`` is owner-checked, so a claim that restamped
    ``owner`` would make the record of a COMPLETED turn unretireable by the
    process that ran it -- and the next resume would auto-continue finished
    work.
    """
    _record(turn_db, "session-key", "a turn that is still running")

    assert server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert turn_db.clear_interrupted_turn("session-key", owner=_FOREIGN_OWNER)
    assert _read(turn_db, "session-key") is None


def test_a_storage_error_in_the_claim_abstains(emits, schedule_env, turn_db, monkeypatch):
    """Not every sqlite error is a lock, and none of them may schedule a turn.

    ``acquire_session_turn_lease`` re-raises anything it cannot classify as a
    lock, so a claim built on it would propagate a disk error out of the
    resume path. The claim swallows storage failure into an abstention
    instead, which leaves the record for the next resume.
    """

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    _record(turn_db, "session-key", "prompt")
    monkeypatch.setattr(turn_db, "_execute_write", _boom)

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env
    survivor = _read(turn_db, "session-key")
    assert survivor is not None
    assert survivor["attempts"] == 0


def test_a_storage_error_in_the_peek_abstains(emits, schedule_env, turn_db, monkeypatch):
    """Cannot read the lease means cannot rule out a running turn.

    The substitute raises what the real accessor raises. ``sqlite3.Error`` is
    not swallowed into a None down there: reporting no-holder off a failed
    read would hand this function a licence the read never earned. So the
    contract pinned here is the one that runs in production, not one that only
    holds against a stub.
    """

    def _boom(*args, **kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    _record(turn_db, "session-key", "prompt")
    monkeypatch.setattr(turn_db, "get_session_turn_lease_holder", _boom)

    result = server._maybe_schedule_auto_continue("sid", _session(), "session-key")

    assert result is None
    assert not schedule_env
    assert _read(turn_db, "session-key")["attempts"] == 0


# ── The note says only what the claim proved ───────────────────────────


def test_the_recovery_note_claims_only_what_was_proved(turn_db):
    """The note is read by the model, so it must not assert an unproven fact.

    The claim proves two things: this process took the interrupted-turn
    record, and no live turn-lease claim stood on the conversation when it
    did. It does not prove that the process which started the turn stopped --
    that process may be alive and simply past its lease -- and it does not
    prove that nothing is running the conversation, because the peek can only
    report on claims that were unexpired and whose holder the pid probe failed
    to write off, at the instant it looked.

    Asserting the old sentence's absence would not be enough on its own: a
    rewrite could drop it and assert something equally unproven. The positive
    claim is pinned too.
    """
    note = server._auto_continue_note("rerun the migration")

    assert "the app or its backend process stopped" not in note
    assert "no other process is running this conversation" not in note
    assert "no completion was ever recorded for it" in note
    assert (
        "this process claimed that turn's record with no live turn held "
        "on the conversation" in note
    )
    assert "rerun the migration" in note


def test_both_recovery_note_matchers_still_match():
    """Two consumers match this note by prefix; neither may be broken."""
    from gateway.run import _is_auto_continue_noise

    note = server._auto_continue_note("rerun the migration")

    assert note.startswith(server._AUTO_CONTINUE_NOTE_PREFIX)
    assert server._legacy_display_kind("user", note) == "auto_continue"
    assert _is_auto_continue_noise(note) is True


# ── User-initiated admission: dormant until the engine takes a holder ──
#
# The arm is entered on caller class, not on how the turn renders: the
# ``user_initiated`` keyword is set at the ``prompt.submit`` handler and
# nowhere else, so these tests set it the way that handler does. Everything
# the gateway synthesizes for itself leaves it off and is covered below.


def _agent(run, **extra):
    return types.SimpleNamespace(
        session_id="session-key",
        run_conversation=run,
        clear_interrupt=lambda: None,
        **extra,
    )


def test_the_rpc_handler_is_the_only_user_initiated_dispatch():
    """The flag's whole value is that exactly one call site sets it.

    Read the source rather than the behavior: an internal dispatch that set it
    would hand a machine-synthesized turn a bounded wait and a "send it again"
    refusal aimed at a user who is not there, and no unit test of that caller
    would notice while the arm is dormant.
    """
    import ast
    import pathlib

    package = pathlib.Path(server.__file__).resolve().parent
    dispatches: list[tuple[str, int, bool]] = []
    for path in sorted(package.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name != "_run_prompt_submit":
                continue
            flagged = any(
                kw.arg == "user_initiated" and getattr(kw.value, "value", None) is True
                for kw in node.keywords
            )
            dispatches.append((path.name, node.lineno, flagged))

    assert [name for name, _line, flagged in dispatches if flagged] == [
        "methods_prompt.py"
    ], dispatches
    # And the machine-class majority is real, not an empty set that would make
    # the assertion above vacuous.
    assert len([1 for _n, _l, flagged in dispatches if not flagged]) >= 8, dispatches


def test_a_stock_engine_turn_takes_no_lease(emits, turn_env, turn_db):
    """An engine that cannot be handed a holder is left entirely alone.

    Pre-acquiring under a holder the engine never learns about would make the
    engine's own acquire block on this process's row for its full wait and
    then reclaim it at TTL, which is worse than not pre-acquiring at all.
    """
    seen: list = []

    def _run(message, conversation_history=None, stream_callback=None, **kwargs):
        seen.append(kwargs)
        assert turn_db.get_session_turn_lease_holder("session-key") is None
        return {"final_response": "done"}

    server._run_prompt_submit(
        "rid",
        "sid",
        _session(agent=_agent(_run), running=True),
        "do the thing",
        user_initiated=True,
    )

    assert seen and "session_turn_lease_holder" not in seen[0]
    assert turn_db.get_session_turn_lease_holder("session-key") is None


def test_a_holder_aware_engine_is_handed_a_preacquired_lease(
    emits, turn_env, turn_db
):
    seen: list = []

    def _run(
        message,
        conversation_history=None,
        stream_callback=None,
        session_turn_lease_holder=None,
        session_turn_lease_ttl_seconds=None,
        **kwargs,
    ):
        seen.append(session_turn_lease_holder)
        assert (
            turn_db.get_session_turn_lease_holder("session-key")
            == session_turn_lease_holder
        )
        return {"final_response": "done"}

    server._run_prompt_submit(
        "rid",
        "sid",
        _session(agent=_agent(_run), running=True),
        "do the thing",
        user_initiated=True,
    )

    assert seen and seen[0]
    # Released with the turn, so the next process in is not made to wait.
    assert turn_db.get_session_turn_lease_holder("session-key") is None


def test_a_busy_conversation_refuses_the_turn_without_starting_the_engine(
    emits, turn_env, turn_db, monkeypatch
):
    """The bounded wait ends in a visible refusal, not a silent drop.

    The user is watching, so the honest answer is that the message was not
    sent and can be sent again. The interrupted-turn record belongs to the
    process that is running, and the refusal does not touch it.
    """
    monkeypatch.setattr(server, "_TURN_LEASE_WAIT_SECONDS", 0.15)
    monkeypatch.setattr(server, "_TURN_LEASE_WAIT_NOTICE_SECONDS", 0.0)
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )
    _record(turn_db, "session-key", "the turn that is actually running")
    started: list = []

    def _run(
        message,
        conversation_history=None,
        stream_callback=None,
        session_turn_lease_holder=None,
        **kwargs,
    ):
        started.append(message)
        return {"final_response": "done"}

    session = _session(agent=_agent(_run), running=True)
    server._run_prompt_submit(
        "rid", "sid", session, "do the thing", user_initiated=True
    )

    assert not started
    completes = [p for event, _sid, p in emits if event == "message.complete"]
    assert len(completes) == 1
    assert completes[0]["status"] == "error"
    assert completes[0]["recoverable"] is True
    assert "again" in completes[0]["error"]
    # A waiting notice reached the client rather than a silent stall.
    assert any(event == "status.update" for event, _sid, _p in emits)
    # The running process still owns its record.
    survivor = _read(turn_db, "session-key")
    assert survivor is not None
    assert survivor["owner"] == _FOREIGN_OWNER
    assert session["running"] is False


def test_a_refused_turn_hands_off_anything_queued_behind_it(
    emits, turn_env, turn_db, monkeypatch
):
    """A prompt sent during the wait was queued against this turn, which ended.

    The normal path drains after its turn settles. A refusal settles a turn
    too, and leaving the queue unattended would strand the message until some
    later turn happened to run.
    """
    monkeypatch.setattr(server, "_TURN_LEASE_WAIT_SECONDS", 0.15)
    drained: list = []
    monkeypatch.setattr(
        server,
        "_drain_queued_prompt",
        lambda rid, sid, session: drained.append(rid) or False,
    )
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )

    def _run(message, session_turn_lease_holder=None, **kwargs):
        raise AssertionError("engine must not start")

    server._run_prompt_submit(
        "rid",
        "sid",
        _session(agent=_agent(_run), running=True),
        "do the thing",
        user_initiated=True,
    )

    assert drained == ["rid"]


def test_a_refused_turn_restores_a_one_turn_model_override(
    emits, turn_env, turn_db, monkeypatch
):
    """`/model --once` is scoped to a turn, and this turn never happened.

    The override is popped off the session before admission runs and restored
    only in the turn's `finally`. A refusal returns before that block, so
    without an unwind here the next-turn-only model becomes the session's
    model permanently.
    """
    monkeypatch.setattr(server, "_TURN_LEASE_WAIT_SECONDS", 0.15)
    restored: list = []
    monkeypatch.setattr(
        server,
        "_restore_agent_model_runtime",
        lambda agent, snapshot: restored.append(snapshot),
    )
    reworked: list = []
    monkeypatch.setattr(
        server, "_restart_slash_worker", lambda sid, session: reworked.append("worker")
    )
    monkeypatch.setattr(
        server, "_persist_live_session_runtime", lambda session: reworked.append("runtime")
    )
    monkeypatch.setattr(
        server,
        "_persist_live_session_system_prompt",
        lambda session: reworked.append("prompt"),
    )
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )
    snapshot = {"model": "the model before /model --once"}

    def _run(message, session_turn_lease_holder=None, **kwargs):
        raise AssertionError("engine must not start")

    session = _session(
        agent=_agent(_run), running=True, one_turn_model_restore=snapshot
    )
    server._run_prompt_submit(
        "rid", "sid", session, "do the thing", user_initiated=True
    )

    assert restored == [snapshot]
    assert reworked == ["worker", "runtime", "prompt"]


def test_a_refused_turn_gives_the_user_their_images_back(
    emits, turn_env, turn_db, monkeypatch
):
    """The refusal says "send it again", so the attachments have to still be there.

    The prologue claims `attached_images` off the session at submission time so
    a later paste cannot be swallowed by this prompt. A refusal that kept that
    claim would drop the images on the floor while telling the user to resend.
    """
    monkeypatch.setattr(server, "_TURN_LEASE_WAIT_SECONDS", 0.15)
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )

    def _run(message, session_turn_lease_holder=None, **kwargs):
        raise AssertionError("engine must not start")

    session = _session(agent=_agent(_run), running=True)
    session["attached_images"] = ["/tmp/one.png", "/tmp/two.png"]
    server._run_prompt_submit(
        "rid", "sid", session, "look at these", user_initiated=True
    )

    assert session["attached_images"] == ["/tmp/one.png", "/tmp/two.png"]


def test_a_refused_turn_clears_the_auto_continue_latch(
    emits, turn_env, turn_db, monkeypatch
):
    """`_auto_continue_scheduled` is a one-shot guard the turn end releases.

    A refusal that left it set would silently disable crash recovery on this
    in-memory session for as long as it lives.
    """
    monkeypatch.setattr(server, "_TURN_LEASE_WAIT_SECONDS", 0.15)
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )

    def _run(message, session_turn_lease_holder=None, **kwargs):
        raise AssertionError("engine must not start")

    session = _session(agent=_agent(_run), running=True)
    session["_auto_continue_scheduled"] = True
    server._run_prompt_submit(
        "rid", "sid", session, "do the thing", user_initiated=True
    )

    assert not session.get("_auto_continue_scheduled")


def test_a_stopped_wait_refuses_without_burning_the_budget(
    emits, turn_env, turn_db, monkeypatch
):
    """/stop during the wait ends it immediately."""
    monkeypatch.setattr(server, "_TURN_LEASE_WAIT_SECONDS", 30.0)
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )
    started: list = []

    def _run(
        message,
        conversation_history=None,
        stream_callback=None,
        session_turn_lease_holder=None,
        **kwargs,
    ):
        started.append(message)
        return {"final_response": "done"}

    session = _session(
        agent=_agent(_run), running=True, _turn_cancel_requested=True
    )
    began = time.monotonic()
    server._run_prompt_submit(
        "rid", "sid", session, "do the thing", user_initiated=True
    )

    assert time.monotonic() - began < 10.0
    assert not started
    assert [p for event, _sid, p in emits if event == "message.complete"]


def test_an_auto_continue_turn_does_not_pre_acquire(emits, turn_env, turn_db):
    """Machine turns already made their admission decision by claiming.

    A bounded wait with a busy notice is the user-turn answer; a machine turn
    that finds the conversation busy abstains at schedule time instead, and
    the engine's own acquire remains the backstop underneath it.
    """
    seen: list = []

    def _run(
        message,
        conversation_history=None,
        stream_callback=None,
        session_turn_lease_holder=None,
        **kwargs,
    ):
        seen.append(session_turn_lease_holder)
        return {"final_response": "done"}

    server._run_prompt_submit(
        "rid",
        "sid",
        _session(agent=_agent(_run), running=True),
        "note",
        display_kind="auto_continue",
    )

    assert seen == [None]


def test_a_machine_synthesized_turn_does_not_pre_acquire(emits, turn_env, turn_db):
    """The default is machine class, so a new internal caller is safe by omission.

    This is the shape the goal continuation, the loop wakeup tick and the
    kanban notification batch all dispatch with: positional arguments only, no
    `display_kind` of any kind. None of them has a person watching, so none of
    them may inherit the bounded wait or the "send it again" refusal — even
    against an engine that could take a holder, and even with the conversation
    held by another process.
    """
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )
    seen: list = []

    def _run(
        message,
        conversation_history=None,
        stream_callback=None,
        session_turn_lease_holder=None,
        **kwargs,
    ):
        seen.append(session_turn_lease_holder)
        return {"final_response": "done"}

    began = time.monotonic()
    session = _session(agent=_agent(_run), running=True)
    server._run_prompt_submit("rid", "sid", session, "the goal judge says continue")

    # No wait was served and no refusal was emitted: it ran straight through.
    assert time.monotonic() - began < 10.0
    assert seen == [None]
    errors = [
        p
        for event, _sid, p in emits
        if event == "message.complete" and p.get("status") == "error"
    ]
    assert not errors
    # The other process still holds the conversation; the engine's own acquire
    # is what serializes against it, exactly as it does today.
    assert turn_db.get_session_turn_lease_holder("session-key") == _LIVE_HOLDER


def test_the_display_kind_is_not_the_admission_discriminator(emits, turn_env, turn_db):
    """A machine turn that carries its own `display_kind` is still machine class.

    The async-delegation completion synth passes
    `display_kind="async_delegation_complete"`, which is neither None nor
    "auto_continue". Keying admission off that field would have put this turn
    on the interactive arm.
    """
    assert turn_db.try_acquire_session_turn_lease(
        "session-key", _LIVE_HOLDER, ttl_seconds=300
    )
    seen: list = []

    def _run(
        message,
        conversation_history=None,
        stream_callback=None,
        session_turn_lease_holder=None,
        **kwargs,
    ):
        seen.append(session_turn_lease_holder)
        return {"final_response": "done"}

    server._run_prompt_submit(
        "rid",
        "sid",
        _session(agent=_agent(_run), running=True),
        "delegation finished",
        display_kind="async_delegation_complete",
    )

    assert seen == [None]


def test_the_holder_probe_reads_the_engine_signature(turn_env, turn_db):
    """The dormancy switch is the engine's own signature, nothing else."""

    def _stock(message, conversation_history=None, **kwargs):
        return {}

    def _aware(message, conversation_history=None, session_turn_lease_holder=None, **kwargs):
        return {}

    assert "session_turn_lease_holder" not in inspect.signature(_stock).parameters
    assert "session_turn_lease_holder" in inspect.signature(_aware).parameters
    assert server._engine_takes_lease_holder(_agent(_stock)) is False
    assert server._engine_takes_lease_holder(_agent(_aware)) is True
