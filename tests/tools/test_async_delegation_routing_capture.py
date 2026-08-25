"""Regression: async delegation must carry a STRUCTURED return address.

``tools/async_delegation.py`` persisted ``session_key`` plus a few origin fields
(scope_id / user_id / user_name) and nothing else, so a completion had to be
reconstructed by splitting the session key on ``":"``. That grammar is
platform-specific and lossy — a Matrix room id is ``!room:server``, and a Slack
key carries the workspace id between the chat-type slot and the chat id — so the
completion could target the wrong chat even with the adapter lookup fixed.

And ``profile`` (the runtime namespace) is not the transport owner: one shared
credential can serve several routed runtimes. ``transport_profile`` is carried
separately, and is what the gateway re-resolves the delivering adapter from.

These tests pin the capture at dispatch, its replay onto the completion event,
and its survival across a restart (durable record -> recovery -> event).
"""

import json
import os

import pytest

from gateway.session_context import clear_session_vars, set_session_vars
from tools import async_delegation as ad


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    """Point the durable state.db at a temp dir — no network, no shared state."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    return tmp_path


@pytest.fixture
def matrix_turn():
    """A turn that arrived on the SHARED PRIMARY Matrix transport but is routed
    into the secondary runtime ``alpha`` — the topology where the runtime
    namespace and the transport owner disagree."""
    tokens = set_session_vars(
        platform="matrix",
        chat_id="!room:example.org",
        chat_type="group",
        thread_id="",
        user_id="@user:example.org",
        user_name="user",
        session_key="agent:alpha:matrix:group:!room:example.org",
        profile="alpha",
        transport_profile="default",
    )
    try:
        yield
    finally:
        clear_session_vars(tokens)


@pytest.fixture
def slack_turn():
    """A Slack turn in a scoped (workspace-qualified) channel."""
    tokens = set_session_vars(
        platform="slack",
        chat_id="C0CHANNEL",
        chat_type="group",
        scope_id="T0WORKSPACE",
        user_id="U0USER",
        session_key="agent:main:slack:group:T0WORKSPACE:C0CHANNEL:U0USER",
        profile="",
        transport_profile="default",
    )
    try:
        yield
    finally:
        clear_session_vars(tokens)


def test_capture_records_the_full_return_address_matrix(matrix_turn):
    """(d) The colon-bearing Matrix room id is captured whole — not recovered
    later by splitting the key, which would yield ``!room``."""
    origin = ad._capture_routing_origin()

    assert origin["platform"] == "matrix"
    assert origin["chat_type"] == "group"
    assert origin["chat_id"] == "!room:example.org"
    assert origin["profile"] == "alpha"
    assert origin["transport_profile"] == "default"


def test_capture_records_the_slack_workspace_scope(slack_turn):
    """(e) The workspace scope and the channel are separate captured fields, so
    neither can end up in the other's slot."""
    origin = ad._capture_routing_origin()

    assert origin["platform"] == "slack"
    assert origin["scope_id"] == "T0WORKSPACE"
    assert origin["chat_id"] == "C0CHANNEL"
    assert origin["transport_profile"] == "default"


def test_capture_outside_a_gateway_turn_is_empty():
    """CLI / contextvar-unaware paths persist nothing new."""
    tokens = set_session_vars()
    try:
        assert ad._capture_routing_origin() == {}
    finally:
        clear_session_vars(tokens)


def _pushed_event(monkeypatch, record):
    """Run _push_completion_event against a stub queue and return the event."""
    pushed = []

    class _Q:
        @staticmethod
        def put(evt):
            pushed.append(evt)

    class _Registry:
        completion_queue = _Q()

    import tools.process_registry as pr
    monkeypatch.setattr(pr, "process_registry", _Registry)
    monkeypatch.setattr(ad, "_persist_completion", lambda *a, **k: None)
    ad._push_completion_event(record, {"status": "completed", "summary": "done"}, "completed")
    assert pushed, "no completion event was queued"
    return pushed[0]


def test_completion_event_carries_the_captured_address(monkeypatch, matrix_turn):
    """The completion the gateway consumes is fully addressed — no session-key
    parsing needed on the delivery path."""
    record = {
        "delegation_id": "del_1",
        "session_key": "agent:alpha:matrix:group:!room:example.org",
        "goal": "g",
        "dispatched_at": 1.0,
        "completed_at": 2.0,
        **ad._capture_routing_origin(),
    }

    evt = _pushed_event(monkeypatch, record)

    assert evt["platform"] == "matrix"
    assert evt["chat_type"] == "group"
    assert evt["chat_id"] == "!room:example.org"
    assert evt["profile"] == "alpha"
    assert evt["transport_profile"] == "default"


def test_slack_completion_event_routes_to_the_right_chat(monkeypatch, slack_turn):
    """(e) end to end: the channel is the chat id and the workspace is the
    scope, on the event the gateway delivers from."""
    record = {
        "delegation_id": "del_slack",
        "session_key": "agent:main:slack:group:T0WORKSPACE:C0CHANNEL:U0USER",
        "goal": "g",
        "dispatched_at": 1.0,
        "completed_at": 2.0,
        **ad._capture_routing_origin(),
    }

    evt = _pushed_event(monkeypatch, record)

    assert evt["chat_id"] == "C0CHANNEL"
    assert evt["scope_id"] == "T0WORKSPACE"


def test_routing_address_survives_a_restart(hermes_home, matrix_turn, monkeypatch):
    """(f) Durable leg: persist the dispatch, then recover it as an owner-died
    record in a fresh process. The replayed event must carry the same address
    and the same transport provenance."""
    record = {
        "delegation_id": "del_restart",
        "session_key": "agent:alpha:matrix:group:!room:example.org",
        "goal": "g",
        "dispatched_at": 1.0,
        # An owner pid that cannot be alive, so recovery classifies it.
        **ad._capture_routing_origin(),
    }
    ad._persist_dispatch(record)

    # The address is on disk, not merely in memory.
    with ad._connect() as conn:
        task_json = conn.execute(
            "SELECT task_json FROM async_delegations WHERE delegation_id=?",
            ("del_restart",),
        ).fetchone()[0]
    persisted = json.loads(task_json)
    assert persisted["chat_id"] == "!room:example.org"
    assert persisted["transport_profile"] == "default"

    # --- restart: the owning process is gone ---
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    assert ad.recover_abandoned_delegations() >= 1

    with ad._connect() as conn:
        event_json = conn.execute(
            "SELECT event_json FROM async_delegations WHERE delegation_id=?",
            ("del_restart",),
        ).fetchone()[0]
    event = json.loads(event_json)

    assert event["platform"] == "matrix"
    assert event["chat_type"] == "group"
    assert event["chat_id"] == "!room:example.org"
    assert event["profile"] == "alpha"
    assert event["transport_profile"] == "default"
