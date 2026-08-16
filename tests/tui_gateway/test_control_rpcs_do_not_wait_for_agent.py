"""Control RPCs must not block on the deferred agent build.

``approval.pending``, ``approval.received`` and ``approval.respond`` need the
session RECORD — specifically ``session_key`` — and never the agent. All three
reach ``tools.approval``'s module-global ``_gateway_queues``, which is keyed by
``session_key`` alone and has no relationship to the session's ``agent_ready``
event, so ``_sess``'s ``_wait_agent`` bought nothing at these sites and charged
up to 30 seconds for it.

None of the three is in ``_LONG_HANDLERS``, so that charge ran inline on the
socket reader thread and stalled every RPC queued behind it — the exact
failure mode ``_LONG_HANDLERS``' own comment cites ``approval.respond`` as the
reason to protect against. And the wait is reachable by construction rather
than by accident: the desktop replays pending approvals on ``gateway.ready`` /
``session.info``, i.e. precisely while a cold resume's deferred build is still
warming, and ``_start_agent_build`` deliberately early-returns for a lazy watch
session spectating an in-flight child — leaving ``agent_ready`` unset for the
whole child run, so these RPCs did not merely stall, they timed out at 5032.

These are invariants, not timings: each handler must complete, and do its real
work, while the build event is still unset.
"""

from __future__ import annotations

import threading

import pytest

from tui_gateway import server


def building_session(tmp_path, sid: str) -> dict:
    """A session record whose deferred agent build has NOT completed."""
    session = {
        "agent": None,
        "agent_ready": threading.Event(),  # deliberately never set
        "agent_error": None,
        "attached_images": [],
        "cwd": str(tmp_path),
        "history": [],
        "history_lock": threading.RLock(),
        "history_version": 0,
        "profile_home": str(tmp_path),
        "running": False,
        "session_key": sid,
        "transport": None,
    }
    server._sessions[sid] = session
    return session


@pytest.fixture
def no_build(monkeypatch):
    """Never let the real builder run — the point is the unfinished build."""
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)


@pytest.fixture
def session(tmp_path, no_build, request):
    sid = f"control-{request.node.name}"
    record = building_session(tmp_path, sid)
    yield sid, record
    server._sessions.pop(sid, None)


def call(method: str, params: dict) -> dict:
    return server._methods[method](1, params)


@pytest.fixture
def pending_approval(session):
    """One unresolved approval queued under this session's key.

    ``tools.approval`` has no public enqueue helper — the only producer is
    ``_await_gateway_decision`` on the agent thread — so this mirrors its three
    lines (build an ``_ApprovalEntry``, append it under ``_lock``) rather than
    reaching for a seam that does not exist. The queue is popped again on
    teardown because it is process-global state.
    """
    from tools import approval

    sid, record = session
    entry = approval._ApprovalEntry(
        {"command": "rm -rf /tmp/scratch", "description": "delete scratch", "request_id": "req-1"}
    )
    with approval._lock:
        approval._gateway_queues.setdefault(sid, []).append(entry)
    yield sid, record, entry
    with approval._lock:
        approval._gateway_queues.pop(sid, None)


def test_approval_pending_replays_the_queue_while_the_agent_is_building(pending_approval):
    """The replay the desktop fires on reconnect must not wait for the build."""
    sid, record, _entry = pending_approval

    response = call("approval.pending", {"session_id": sid})

    assert "error" not in response, response
    approvals = response["result"]["approvals"]
    assert [a["request_id"] for a in approvals] == ["req-1"]
    assert approvals[0]["command"] == "rm -rf /tmp/scratch"
    # The invariant that makes this a fix rather than a coincidence: the build
    # never finished, and the replay landed anyway.
    assert not record["agent_ready"].is_set()


def test_approval_received_acknowledges_while_the_agent_is_building(pending_approval):
    """Not waiting must not mean not acking — the entry is really flipped."""
    sid, record, entry = pending_approval
    assert entry.acknowledged is False

    response = call("approval.received", {"session_id": sid, "request_id": "req-1"})

    assert "error" not in response, response
    assert response["result"]["acknowledged"] is True
    assert entry.acknowledged is True
    assert not record["agent_ready"].is_set()


def test_approval_respond_resolves_while_the_agent_is_building(pending_approval):
    """The RPC _LONG_HANDLERS exists to protect must itself run unblocked."""
    sid, record, entry = pending_approval

    response = call(
        "approval.respond", {"session_id": sid, "request_id": "req-1", "choice": "deny"}
    )

    assert "error" not in response, response
    assert response["result"]["resolved"] == 1
    # The blocked agent thread is genuinely released, not just answered.
    assert entry.result == "deny"
    assert entry.event.is_set()
    assert not record["agent_ready"].is_set()


def test_approval_received_still_requires_a_request_id(session):
    """Dropping the agent wait must not drop parameter validation."""
    sid, _record = session

    response = call("approval.received", {"session_id": sid})

    assert response["error"]["code"] == 4006


@pytest.mark.parametrize(
    "method", ["approval.pending", "approval.received", "approval.respond"]
)
def test_approval_rpcs_still_reject_an_unknown_session(no_build, method):
    """_sess_building keeps _sess_nowait's 4001 — validation is not the wait."""
    response = call(method, {"session_id": "nope", "request_id": "req-1"})

    assert response["error"]["code"] == 4001

