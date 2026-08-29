"""A refused transport bind must be terminal for every mutation path.

``_bind_live_session_transport`` proves registry authority under
``_session_resume_lock -> _sessions_lock``.  It used to answer with a bare
``bool``, which collapsed two very different situations into one:

* the record is still authoritative but the request socket died, and
* the record was popped/marked by a concurrent disconnect teardown.

Every mutation caller discarded that ``False``, so a ``prompt.submit`` worker
that had resolved its session through the UNLOCKED ``_sess_nowait()`` read
could receive an explicit stale-record proof and then keep appending history,
queueing prompts and latching ``running`` on a record teardown already owned.
The queued drain had the same hole in the opposite order: it claimed the queue
and set ``running`` BEFORE asking about ownership.

These tests pin the fail-closed contract at the method boundary, not at the
helper: they barrier a real ``prompt.submit`` between the registry read and the
bind, let a real ``_pop_session_by_id()`` teardown claim the exact record, and
then prove the RPC refuses instead of mutating the popped object.
"""

from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server, session_ownership
from tui_gateway.session_ownership import SessionBindOutcome
from tui_gateway.transport import bind_transport, reset_transport


class _LiveTransport:
    """Minimal live client transport (``_transport_is_dead`` reads ``_closed``)."""

    def __init__(self) -> None:
        self._closed = False
        self.writes: list[dict] = []

    def write(self, obj: dict) -> bool:
        self.writes.append(obj)
        return True


def _record(**extra) -> dict:
    session = {
        "agent": types.SimpleNamespace(),
        "session_key": "stored-key",
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
        "transport": _LiveTransport(),
    }
    session.update(extra)
    return session


@pytest.fixture(autouse=True)
def _clean_registry():
    server._sessions.clear()
    yield
    server._sessions.clear()


# --------------------------------------------------------------------------
# The primitive itself: three outcomes, not two.
# --------------------------------------------------------------------------


def test_bind_reports_bound_for_the_registered_record():
    session = _record()
    server._sessions["sid"] = session
    replacement = _LiveTransport()

    outcome = server._bind_live_session_transport("sid", session, replacement)

    assert outcome is SessionBindOutcome.BOUND
    assert outcome.is_bound
    assert not outcome.is_stale_record
    assert session["transport"] is replacement
    assert replacement in session["viewers"]


def test_bind_reports_transport_dead_without_losing_the_record():
    session = _record()
    server._sessions["sid"] = session
    previous = session["transport"]
    dead = _LiveTransport()
    dead._closed = True

    outcome = server._bind_live_session_transport("sid", session, dead)

    assert outcome is SessionBindOutcome.TRANSPORT_DEAD
    # NOT stale: the caller may still finish work it already admitted.
    assert not outcome.is_stale_record
    assert session["transport"] is previous


def test_bind_reports_stale_record_for_a_popped_session():
    session = _record()
    server._sessions["sid"] = session
    popped = server._pop_session_by_id("sid")
    assert popped is session

    outcome = server._bind_live_session_transport("sid", session, _LiveTransport())

    assert outcome is SessionBindOutcome.STALE_RECORD
    assert outcome.is_stale_record


def test_bind_reports_stale_record_for_a_closing_but_still_registered_record():
    """``_closing`` is set under the registry lock BEFORE the pop completes."""
    session = _record(_closing=True)
    server._sessions["sid"] = session

    outcome = server._bind_live_session_transport("sid", session, _LiveTransport())

    assert outcome is SessionBindOutcome.STALE_RECORD


def test_stale_record_wins_over_a_dead_transport():
    """The two refusals are distinguishable even when both apply."""
    session = _record()
    server._sessions["sid"] = session
    server._pop_session_by_id("sid")
    dead = _LiveTransport()
    dead._closed = True

    assert (
        server._bind_live_session_transport("sid", session, dead)
        is SessionBindOutcome.STALE_RECORD
    )


# --------------------------------------------------------------------------
# prompt.submit at the method boundary.
# --------------------------------------------------------------------------


def _barrier_prompt_submit(monkeypatch):
    """Suspend prompt.submit after ``_sess_nowait()`` and before the bind.

    ``_load_dashboard_process_isolation_config()`` is the last call the handler
    makes before it claims the transport, which makes it the exact seam the
    race needs -- no sleeps, no polling.
    """
    reached = threading.Event()
    release = threading.Event()

    def _blocking_isolation_config():
        reached.set()
        assert release.wait(timeout=5)
        return {}

    monkeypatch.setattr(
        server,
        "_load_dashboard_process_isolation_config",
        _blocking_isolation_config,
    )
    return reached, release


def test_prompt_submit_refuses_a_record_teardown_claimed_mid_request(monkeypatch):
    """The disconnect-teardown race the ownership gate exists to stop."""
    session = _record()
    server._sessions["sid"] = session
    reached, release = _barrier_prompt_submit(monkeypatch)
    request_transport = _LiveTransport()
    responses: list[dict] = []

    def _submit():
        token = bind_transport(request_transport)
        try:
            responses.append(
                server._methods["prompt.submit"](
                    "rid-1", {"session_id": "sid", "text": "hello"}
                )
            )
        finally:
            reset_transport(token)

    worker = threading.Thread(target=_submit)
    worker.start()
    try:
        assert reached.wait(timeout=5)
        # A close_on_disconnect teardown claims this exact record.
        assert server._pop_session_by_id("sid") is session
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert responses[0]["error"]["code"] == 4001
    # Nothing was started, queued or latched on the popped record.
    assert session["running"] is False
    assert session["history"] == []
    assert session.get("queued_prompt") is None
    assert session.get("queued_prompts") is None
    assert session["transport"] is not request_transport


def test_prompt_submit_refuses_a_claimed_record_with_no_request_transport(monkeypatch):
    """No socket on the request still has to prove registry authority."""
    session = _record()
    server._sessions["sid"] = session
    reached, release = _barrier_prompt_submit(monkeypatch)
    responses: list[dict] = []

    def _submit():
        token = bind_transport(None)
        try:
            responses.append(
                server._methods["prompt.submit"](
                    "rid-2", {"session_id": "sid", "text": "hello"}
                )
            )
        finally:
            reset_transport(token)

    worker = threading.Thread(target=_submit)
    worker.start()
    try:
        assert reached.wait(timeout=5)
        assert server._pop_session_by_id("sid") is session
    finally:
        release.set()
        worker.join(timeout=5)

    assert not worker.is_alive()
    assert responses[0]["error"]["code"] == 4001
    assert session["running"] is False
    assert session["history"] == []


def test_prompt_submit_survives_a_dead_request_transport(monkeypatch):
    """A dead socket must NOT be treated like a lost record (no false refusal)."""
    session = _record()
    server._sessions["sid"] = session
    started: list[str] = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda rid, sid, sess, text, **_kw: started.append(text),
    )
    monkeypatch.setattr(server, "_emit", lambda *_a, **_k: None)

    dead = _LiveTransport()
    dead._closed = True
    token = bind_transport(dead)
    try:
        response = server._methods["prompt.submit"](
            "rid-3", {"session_id": "sid", "text": "still mine"}
        )
    finally:
        reset_transport(token)

    assert "error" not in response
    assert started == ["still mine"]


# --------------------------------------------------------------------------
# Retired registry ids.
# --------------------------------------------------------------------------


def test_retired_session_id_reuse_keeps_the_new_tombstone(monkeypatch):
    """Reusing a runtime id must not let an old bounded entry erase its replacement."""
    sid = "reused-runtime-sid"
    other = "other-runtime-sid"
    monkeypatch.setattr(session_ownership, "_RETIRED_SESSION_ID_LIMIT", 2)
    with session_ownership._retired_session_ids_lock:
        saved = session_ownership._retired_session_ids.copy()
        session_ownership._retired_session_ids.clear()
    try:
        server._remember_retired_session_id(sid)
        server._forget_retired_session_id(sid)
        server._remember_retired_session_id(sid)
        server._remember_retired_session_id(other)
        assert server._was_retired_session_id(sid)
    finally:
        with session_ownership._retired_session_ids_lock:
            session_ownership._retired_session_ids.clear()
            session_ownership._retired_session_ids.update(saved)


# --------------------------------------------------------------------------
# Queued drain.
# --------------------------------------------------------------------------


def test_queued_drain_refuses_a_popped_record_without_claiming_the_queue():
    queued = {"text": "queued turn", "transport": _LiveTransport()}
    session = _record(queued_prompt=queued)
    server._sessions["sid"] = session
    assert server._pop_session_by_id("sid") is session

    assert server._drain_queued_prompt("rid", "sid", session) is False

    # The claim never happened: the envelope and the idle latch are intact.
    assert session["queued_prompt"] is queued
    assert session["running"] is False


def test_queued_drain_restores_the_envelope_when_ownership_is_lost_mid_claim(
    monkeypatch,
):
    """Teardown between the authority check and the bind must not eat the turn."""
    queued = {"text": "queued turn", "transport": _LiveTransport()}
    session = _record(queued_prompt=queued)
    server._sessions["sid"] = session
    real_bind = server._bind_live_session_transport

    def _pop_then_bind(sid, record, transport):
        # Simulate the disconnect landing inside the claim window.
        server._pop_session_by_id(sid)
        return real_bind(sid, record, transport)

    monkeypatch.setattr(server, "_bind_live_session_transport", _pop_then_bind)

    assert server._drain_queued_prompt("rid", "sid", session) is False

    assert session["queued_prompt"] is queued
    assert session.get("queued_prompts") is None
    assert session["running"] is False
