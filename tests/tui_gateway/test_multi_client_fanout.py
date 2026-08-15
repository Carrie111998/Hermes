"""A session streams to EVERY attached client, not just the newest one.

Before this, ``session["transport"]`` held exactly one client and every
``prompt.submit`` / ``session.resume`` / ``session.activate`` / queued-prompt
drain rebound it — so a second client either saw nothing, stole the stream from
the first, or silenced the turn the first was reading. The slot now holds a
``FanoutTransport`` as soon as a second client attaches, and the disconnect path
detaches instead of parking whenever another client is still there.

Single-client behaviour is the control condition throughout: with one client
attached the slot holds the bare transport and every path behaves exactly as it
did before fan-out existed.
"""

import asyncio
import json
import threading
import types

from tui_gateway import server
from tui_gateway import ws as ws_mod
from tui_gateway.transport import FanoutTransport


class _FakeClient:
    """A connected client: records the frames it receives.

    ``ok`` False models a peer that has gone away (``write`` returns False, the
    gateway's peer-gone signal); ``boom`` models a wedged peer whose write
    raises. Both must be pruned without disturbing the healthy clients.

    ``transport_id`` mirrors the opaque per-connection id ``tui_gateway.ws``
    mints at accept — the value the wire contract calls ``origin``.
    """

    def __init__(
        self,
        name: str,
        *,
        ok: bool = True,
        boom: bool = False,
        transport_id: str | None = None,
    ) -> None:
        self.name = name
        self.frames: list[dict] = []
        self.closed = False
        self.transport_id = transport_id if transport_id is not None else f"tid-{name}"
        self._ok = ok
        self._boom = boom

    def write(self, obj: dict) -> bool:
        if self._boom:
            raise RuntimeError(f"{self.name} is wedged")
        self.frames.append(obj)
        return self._ok

    def close(self) -> None:
        self.closed = True

    def types(self) -> list[str]:
        return [(f.get("params") or {}).get("type") for f in self.frames]


def _session(**extra) -> dict:
    return {
        "agent": None,
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "attached_images": [],
        "transport": None,
        **extra,
    }


# ── FanoutTransport ────────────────────────────────────────────────────────


def test_fanout_delivers_to_every_attached_transport():
    a, b = _FakeClient("a"), _FakeClient("b")
    fan = FanoutTransport(a, b)

    assert fan.write({"frame": 1}) is True
    assert a.frames == b.frames == [{"frame": 1}]
    assert fan.transports() == [a, b]


def test_fanout_attach_is_idempotent_and_detach_is_by_identity():
    a, b = _FakeClient("a"), _FakeClient("b")
    fan = FanoutTransport(a)

    assert fan.attach(a) is False  # already attached
    assert fan.attach(b) is True
    assert fan.contains(a) and fan.contains(b)
    assert fan.has_transports(excluding=a) is True

    assert fan.detach(a) is True
    assert fan.detach(a) is False
    assert fan.transports() == [b]
    assert fan.has_transports(excluding=b) is False


def test_fanout_prunes_a_wedged_peer_without_disturbing_the_rest():
    """(g) A raising peer is dropped; the healthy client keeps its stream."""
    healthy, wedged = _FakeClient("healthy"), _FakeClient("wedged", boom=True)
    fan = FanoutTransport(wedged, healthy)

    assert fan.write({"frame": 1}) is True
    assert healthy.frames == [{"frame": 1}]
    assert fan.transports() == [healthy]

    # And the pruning is permanent — no retry storm against the wedged peer.
    assert fan.write({"frame": 2}) is True
    assert len(healthy.frames) == 2


def test_fanout_prunes_a_peer_that_reports_gone_and_reports_all_dead():
    gone = _FakeClient("gone", ok=False)
    fan = FanoutTransport(gone)

    # write() returned False -> peer gone -> pruned, and an empty fan-out
    # reports peer-gone exactly like a single dead transport would.
    assert fan.write({"frame": 1}) is False
    assert fan.transports() == []
    assert fan.write({"frame": 2}) is False


def test_fanout_close_releases_peers_without_closing_their_sockets():
    a = _FakeClient("a")
    fan = FanoutTransport(a)

    fan.close()

    assert fan.transports() == []
    # Each client owns its own socket; the WS handler closes it on disconnect.
    assert a.closed is False


# ── attach ladder ──────────────────────────────────────────────────────────


def test_attach_replaces_an_empty_or_stdio_slot_without_wrapping():
    """The single-client shape is unchanged: no FanoutTransport in sight."""
    a = _FakeClient("a")

    empty = _session(transport=None)
    assert server._attach_session_transport(empty, a) is True
    assert empty["transport"] is a

    stdio = _session(transport=server._stdio_transport)
    assert server._attach_session_transport(stdio, a) is True
    assert stdio["transport"] is a

    parked = _session(transport=server._detached_ws_transport)
    assert server._attach_session_transport(parked, a) is True
    assert parked["transport"] is a


def test_attach_of_the_same_transport_is_a_noop():
    a = _FakeClient("a")
    session = _session(transport=a)

    assert server._attach_session_transport(session, a) is True
    assert session["transport"] is a


def test_attach_of_a_second_client_wraps_both_and_a_third_joins_the_fanout():
    a, b, c = _FakeClient("a"), _FakeClient("b"), _FakeClient("c")
    session = _session(transport=a)

    server._attach_session_transport(session, b)
    assert isinstance(session["transport"], FanoutTransport)
    assert session["transport"].transports() == [a, b]

    server._attach_session_transport(session, c)
    assert session["transport"].transports() == [a, b, c]


def test_attach_never_lets_stdio_displace_a_live_client():
    """An unbound-context activate must not silence the websocket that owns it."""
    a = _FakeClient("a")
    session = _session(transport=a)

    assert server._attach_session_transport(session, server._stdio_transport) is False
    assert session["transport"] is a


def test_attach_flattens_a_fanout_argument_instead_of_nesting_it():
    a, b, c = _FakeClient("a"), _FakeClient("b"), _FakeClient("c")
    session = _session(transport=a)
    server._attach_session_transport(session, b)

    server._attach_session_transport(session, FanoutTransport(b, c))

    assert session["transport"].transports() == [a, b, c]


def test_detach_collapses_back_to_the_single_remaining_client():
    a, b = _FakeClient("a"), _FakeClient("b")
    session = _session(transport=a)
    server._attach_session_transport(session, b)

    assert server._detach_session_transport(session, a) is True
    assert session["transport"] is b
    assert server._detach_session_transport(session, b) is False


def test_detach_of_a_single_client_leaves_the_slot_for_the_caller_to_park():
    a = _FakeClient("a")
    session = _session(transport=a)

    # False == "no live client remains" — the disconnect path parks the sentinel.
    assert server._detach_session_transport(session, a) is False
    assert session["transport"] is a


def test_live_transport_predicates_ignore_stdio_and_the_drop_sentinel():
    a = _FakeClient("a")

    assert server._session_has_live_transport(_session(transport=a)) is True
    assert server._session_has_live_transport(_session(transport=a), excluding=a) is False
    assert server._session_has_live_transport(_session(transport=None)) is False
    assert (
        server._session_has_live_transport(_session(transport=server._stdio_transport))
        is False
    )
    assert (
        server._session_has_live_transport(
            _session(transport=server._detached_ws_transport)
        )
        is False
    )


def test_steer_authority_recognizes_the_exact_client_inside_a_fanout():
    """Wrapping attached peers must not revoke the commissioning peer's authority."""
    owner, watcher, stranger = (
        _FakeClient("owner"),
        _FakeClient("watcher"),
        _FakeClient("stranger"),
    )
    session = _session(transport=FanoutTransport(owner, watcher))
    server._sessions["sid"] = session
    try:
        token = server.bind_transport(owner)
        try:
            assert server._current_session_steer_authority("sid") == (owner, session)
        finally:
            server.reset_transport(token)

        token = server.bind_transport(stranger)
        try:
            assert server._current_session_steer_authority("sid") == (None, None)
        finally:
            server.reset_transport(token)
    finally:
        server._sessions.pop("sid", None)


def test_steer_authority_is_granted_to_an_attached_watcher():
    """A client that attached to watch a session may also steer its subagents.

    This is INTENTIONAL and wider than the pre-fan-out rule, which admitted only
    whichever client happened to hold the transport slot. A mirrored session has
    no single owner in that slot, so authority is membership in it. Narrowing
    this back to the commissioning peer would need a per-subagent record of who
    commissioned it, which this change does not add; the widening is stated in
    the pull request description, and this test pins it so it cannot be changed
    silently in either direction.
    """
    owner, watcher, stranger = (
        _FakeClient("owner"),
        _FakeClient("watcher"),
        _FakeClient("stranger"),
    )
    session = _session(transport=owner)
    server._attach_session_transport(session, watcher)
    solo = _session(transport=owner)
    server._sessions["sid"] = session
    server._sessions["solo"] = solo
    try:
        token = server.bind_transport(watcher)
        try:
            assert server._current_session_steer_authority("sid") == (watcher, session)
            # Control: on a single-client session the same watcher is a stranger,
            # so the widening reaches attached clients and nobody else.
            assert server._current_session_steer_authority("solo") == (None, None)
        finally:
            server.reset_transport(token)

        token = server.bind_transport(stranger)
        try:
            assert server._current_session_steer_authority("sid") == (None, None)
        finally:
            server.reset_transport(token)
    finally:
        server._sessions.pop("sid", None)
        server._sessions.pop("solo", None)


# ── event delivery ─────────────────────────────────────────────────────────


def test_two_attached_clients_both_receive_a_session_event(monkeypatch):
    """(a) The headline behaviour: one emit, two clients."""
    a, b = _FakeClient("a"), _FakeClient("b")
    session = _session(transport=a)
    server._attach_session_transport(session, b)
    server._sessions["sid"] = session
    try:
        server._emit("message.delta", "sid", {"text": "hi"})
    finally:
        server._sessions.pop("sid", None)

    assert a.types() == ["message.delta"]
    assert b.types() == ["message.delta"]
    assert a.frames[0] == b.frames[0]


def test_a_single_client_session_writes_through_the_bare_transport(monkeypatch):
    """(j) Control: nothing about the one-client path changed."""
    a = _FakeClient("a")
    server._sessions["sid"] = _session(transport=a)
    try:
        assert server._sessions["sid"]["transport"] is a
        server._emit("message.delta", "sid", {"text": "hi"})
    finally:
        server._sessions.pop("sid", None)

    assert a.frames == [
        {
            "jsonrpc": "2.0",
            "method": "event",
            "params": {
                "type": "message.delta",
                "session_id": "sid",
                "payload": {"text": "hi"},
            },
        }
    ]


def test_both_attached_clients_receive_the_terminal_message_complete():
    """The frame that ENDS a turn fans out too, not just the streaming deltas.

    ``message.complete`` is how a client knows the turn is over. A mirrored
    session that delivered deltas but dropped the terminal frame would leave the
    second client rendering a turn that never finishes.
    """
    a, b = _FakeClient("a"), _FakeClient("b")
    session = _session(transport=a)
    server._attach_session_transport(session, b)
    server._sessions["sid"] = session
    try:
        server._emit("message.delta", "sid", {"text": "par"})
        server._emit("message.complete", "sid", {"text": "part", "status": "ok"})
    finally:
        server._sessions.pop("sid", None)

    assert a.types() == ["message.delta", "message.complete"]
    assert b.types() == ["message.delta", "message.complete"]
    assert a.frames[-1] == b.frames[-1]
    assert a.frames[-1]["params"]["payload"] == {"text": "part", "status": "ok"}

    # Control: the one-client path delivers the same terminal frame through the
    # bare transport, with no fan-out in the slot.
    solo = _FakeClient("solo")
    server._sessions["solo-sid"] = _session(transport=solo)
    try:
        assert server._sessions["solo-sid"]["transport"] is solo
        server._emit("message.complete", "solo-sid", {"text": "part", "status": "ok"})
    finally:
        server._sessions.pop("solo-sid", None)

    assert solo.types() == ["message.complete"]
    assert solo.frames[-1]["params"]["payload"] == {"text": "part", "status": "ok"}


# ── prompt.submit / queued drain ───────────────────────────────────────────


def test_second_clients_submit_attaches_and_the_first_keeps_streaming(monkeypatch):
    """(c) A second client's prompt.submit must not cut the first one out."""
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)

    watcher, submitter = _FakeClient("watcher"), _FakeClient("submitter")
    session = _session(transport=watcher, agent=types.SimpleNamespace())
    server._sessions["sid"] = session
    token = server.bind_transport(submitter)
    try:
        server._methods["prompt.submit"]("r1", {"session_id": "sid", "text": "hello"})
        server._emit("message.delta", "sid", {"text": "answer"})
    finally:
        server.reset_transport(token)
        server._sessions.pop("sid", None)

    assert isinstance(session["transport"], FanoutTransport)
    assert watcher.types() == ["message.delta"]
    assert submitter.types() == ["message.delta"]


def test_queued_prompt_drain_keeps_both_clients_attached(monkeypatch):
    """(d) The hole every competing patch missed: the drain used to rebind.

    Client A is streaming; client B submits mid-turn, so B's prompt is queued
    with B's transport pinned to it. When the drain fires it must ATTACH B —
    rebinding pinned the whole drained turn to B and silenced A.
    """
    dispatched = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda rid, sid, _session, text, **kw: dispatched.append((rid, text)),
    )

    a, b = _FakeClient("a"), _FakeClient("b")
    session = _session(transport=a)
    server._enqueue_prompt(session, "from B", b)
    server._sessions["sid"] = session
    try:
        assert server._drain_queued_prompt("drain", "sid", session) is True
        server._emit("message.delta", "sid", {"text": "drained answer"})
    finally:
        server._sessions.pop("sid", None)

    assert dispatched == [("drain", "from B")]
    assert isinstance(session["transport"], FanoutTransport)
    assert a.types() == ["message.delta"]
    assert b.types() == ["message.delta"]


def test_queued_prompt_drain_skips_a_queuer_that_disconnected(monkeypatch):
    """B goes away while its prompt waits: the prompt runs, the dead pin does not.

    Attaching a transport whose client already left would pin a dead peer into
    the slot until the first failed write prunes it. A keeps its stream and
    stays the only attached client.
    """
    dispatched = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda rid, sid, _session, text, **kw: dispatched.append((rid, text)),
    )

    a, b = _FakeClient("a"), _FakeClient("b")
    session = _session(transport=a)
    server._enqueue_prompt(session, "from B", b)
    b._closed = True  # what _transport_is_dead reads: B's socket went away
    server._sessions["sid"] = session
    try:
        assert server._drain_queued_prompt("drain", "sid", session) is True
        server._emit("message.delta", "sid", {"text": "drained answer"})
    finally:
        server._sessions.pop("sid", None)

    assert dispatched == [("drain", "from B")]  # drain semantics unchanged
    assert session["transport"] is a
    assert server._session_transport_contains(session, b) is False
    assert a.types() == ["message.delta"]
    assert b.types() == []


def test_queued_prompt_drain_still_rebinds_a_single_client_session(monkeypatch):
    """(j) Control: with one client the drain lands on the queuer's transport."""
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: None)

    b = _FakeClient("b")
    session = _session(transport=server._detached_ws_transport)
    server._enqueue_prompt(session, "from B", b)

    assert server._drain_queued_prompt("drain", "sid", session) is True
    assert session["transport"] is b


# ── disconnect ─────────────────────────────────────────────────────────────


def test_watcher_disconnect_leaves_the_other_client_streaming():
    """(e) One client leaving must not park or reap a session someone is reading."""
    a, watcher = _FakeClient("a"), _FakeClient("watcher")
    session = _session(transport=a)
    server._attach_session_transport(session, watcher)
    server._sessions["sid"] = session
    try:
        assert server._close_sessions_for_transport(watcher) == (0, 0)
        assert session["transport"] is a
        assert server._ws_session_is_orphaned(session) is False
        server._emit("message.delta", "sid", {"text": "still here"})
    finally:
        server._sessions.pop("sid", None)

    assert a.types() == ["message.delta"]
    assert watcher.types() == []


def test_last_client_disconnect_parks_exactly_as_before(monkeypatch):
    """(f) Once the last client goes, today's park + grace-reap path runs."""
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)

    a, watcher = _FakeClient("a"), _FakeClient("watcher")
    session = _session(transport=a)
    server._attach_session_transport(session, watcher)
    server._sessions["sid"] = session
    try:
        assert server._close_sessions_for_transport(watcher) == (0, 0)
        assert server._close_sessions_for_transport(a) == (0, 1)
        assert session["transport"] is server._detached_ws_transport
        assert server._ws_session_is_orphaned(session) is True
    finally:
        server._sessions.pop("sid", None)


def test_last_client_disconnect_reaps_a_close_on_disconnect_session(monkeypatch):
    """(f) close_on_disconnect still fires — but only for the LAST client."""
    closed = []
    monkeypatch.setattr(
        server,
        "_close_session_by_id",
        lambda sid, end_reason=None: closed.append((sid, end_reason)),
    )

    a, watcher = _FakeClient("a"), _FakeClient("watcher")
    session = _session(transport=a, close_on_disconnect=True)
    server._attach_session_transport(session, watcher)
    server._sessions["sid"] = session
    try:
        assert server._close_sessions_for_transport(watcher) == (0, 0)
        assert closed == []
        assert server._close_sessions_for_transport(
            a, end_reason="ws_disconnect"
        ) == (1, 0)
    finally:
        server._sessions.pop("sid", None)

    assert closed == [("sid", "ws_disconnect")]


def test_orphan_check_spares_a_session_that_still_has_a_fanout_peer():
    a, watcher = _FakeClient("a"), _FakeClient("watcher")
    session = _session(transport=a)
    server._attach_session_transport(session, watcher)

    assert server._ws_session_is_orphaned(session) is False

    # Losing one of two clients collapses the slot but keeps the session live.
    assert server._detach_session_transport(session, a) is True
    assert server._ws_session_is_orphaned(session) is False

    # Losing the last one reports "no client left"; the caller then parks the
    # drop sentinel, which is what makes the session orphaned.
    assert server._detach_session_transport(session, watcher) is False
    session["transport"] = server._detached_ws_transport
    assert server._ws_session_is_orphaned(session) is True


# ── wire contract: gateway.ready ───────────────────────────────────────────


def test_gateway_ready_announces_session_mirroring_and_this_connections_origin(
    monkeypatch,
):
    """(h) The client learns the backend mirrors, and learns its own id."""
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    monkeypatch.setattr(server, "resolve_skin", lambda: {"name": "test-skin"})

    created = []
    real_transport = ws_mod.WSTransport
    monkeypatch.setattr(
        ws_mod,
        "WSTransport",
        lambda ws, loop, **kw: created.append(real_transport(ws, loop, **kw))
        or created[-1],
    )
    sent = []

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            sent.append(json.loads(line))

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(ws_mod.handle_ws(FakeWS()))

    ready = [f for f in sent if (f.get("params") or {}).get("type") == "gateway.ready"]
    assert len(ready) == 1
    payload = ready[0]["params"]["payload"]
    assert payload["session_mirroring"] is True
    assert payload["origin"] == created[0].transport_id
    # Additive only — the pre-existing keys are untouched.
    assert payload["change_events"] is True
    assert payload["skin"] == {"name": "test-skin"}


def test_each_ws_connection_gets_its_own_opaque_origin():
    loop = asyncio.new_event_loop()
    try:
        first = ws_mod.WSTransport(object(), loop, peer="a")
        second = ws_mod.WSTransport(object(), loop, peer="b")
        assert first.transport_id and second.transport_id
        assert first.transport_id != second.transport_id
    finally:
        loop.close()


# ── wire contract: event origin ────────────────────────────────────────────


def test_event_frames_carry_the_submitters_origin_during_its_turn(monkeypatch):
    """(i) Both clients see the frame; both can tell WHO started the turn."""
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)

    watcher = _FakeClient("watcher", transport_id="conn-watcher")
    submitter = _FakeClient("submitter", transport_id="conn-submitter")
    session = _session(transport=watcher, agent=types.SimpleNamespace())
    server._sessions["sid"] = session
    token = server.bind_transport(submitter)
    try:
        server._methods["prompt.submit"]("r1", {"session_id": "sid", "text": "hello"})
        server._emit("message.delta", "sid", {"text": "answer"})
    finally:
        server.reset_transport(token)
        server._sessions.pop("sid", None)

    assert session["turn_origin"] == "conn-submitter"
    for client in (watcher, submitter):
        assert client.frames[0]["params"]["origin"] == "conn-submitter"


def test_event_frames_omit_origin_when_no_client_started_the_turn():
    """(j) A stdio/unknown origin adds no key at all — frames keep their shape."""
    server._sessions["sid"] = _session(transport=None)
    try:
        frame = server._event_frame("message.delta", "sid", {"text": "hi"})
        assert "origin" not in frame["params"]

        # Empty string is "unknown", not an origin worth wiring.
        server._sessions["sid"]["turn_origin"] = ""
        assert "origin" not in server._event_frame("message.delta", "sid")["params"]
    finally:
        server._sessions.pop("sid", None)

    # Session-less global broadcasts never carry an origin either.
    assert "origin" not in server._event_frame("skin.changed", "")["params"]


def test_a_mid_turn_submit_that_redirects_leaves_the_running_turns_origin(monkeypatch):
    """A turn belongs to the client that CLAIMED it, not to whoever typed last.

    A's turn is running. B types into the session it is mirroring, and under the
    default ``interrupt`` policy a redirect-capable agent takes the
    redirect-in-place branch: the live turn is corrected, no new turn starts,
    and B's submit answers ``redirected``. Every remaining frame of A's turn —
    ``message.complete`` included — therefore still belongs to A.

    Stamping the origin at attach time instead inverted the attribution for the
    rest of the turn: A watched its own turn finish under B's origin and
    un-latched, while B latched a turn it had never started.
    """
    # An empty config selects the default busy policy (``interrupt``) without
    # depending on the developer's config.yaml.
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)

    redirects: list[str] = []
    agent = types.SimpleNamespace(
        _supports_active_turn_redirect=True,
        redirect=lambda text: bool(redirects.append(text)) or True,
    )
    a = _FakeClient("a", transport_id="conn-a")
    b = _FakeClient("b", transport_id="conn-b")
    session = _session(
        transport=a,
        agent=agent,
        running=True,
        turn_origin="conn-a",
        inflight_turn={"user": "A's prompt", "assistant": "partial "},
    )
    server._sessions["sid"] = session
    token = server.bind_transport(b)
    try:
        response = server._methods["prompt.submit"](
            "r1", {"session_id": "sid", "text": "actually, in Rust"}
        )
        server._emit("message.complete", "sid", {"text": "A's answer"})
    finally:
        server.reset_transport(token)
        server._sessions.pop("sid", None)

    assert response["result"]["status"] == "redirected"
    assert redirects == ["actually, in Rust"]
    # B is attached now — it is watching — but it did not take the turn.
    assert isinstance(session["transport"], FanoutTransport)
    assert session["turn_origin"] == "conn-a"
    for client in (a, b):
        assert client.types() == ["message.complete"]
        assert client.frames[-1]["params"]["origin"] == "conn-a"


def test_an_uncontended_submit_still_stamps_the_submitter(monkeypatch):
    """(j) Control: on an IDLE session the claim is the submitter's, as before."""
    monkeypatch.setattr(server, "_load_cfg", lambda: {})
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)

    b = _FakeClient("b", transport_id="conn-b")
    session = _session(transport=None, agent=types.SimpleNamespace(), turn_origin="conn-a")
    server._sessions["sid"] = session
    token = server.bind_transport(b)
    try:
        response = server._methods["prompt.submit"](
            "r1", {"session_id": "sid", "text": "hello"}
        )
    finally:
        server.reset_transport(token)
        server._sessions.pop("sid", None)

    assert "error" not in response
    assert session["turn_origin"] == "conn-b"


def test_queued_prompt_drain_stamps_the_queuers_origin(monkeypatch):
    """The drained turn belongs to whoever queued it, not whoever is driving.

    The whole lifecycle: B's mid-turn prompt is QUEUED under the ``queue``
    policy, which starts no turn, so A keeps the origin for the rest of its own
    turn. The stamp moves to B only when the drain claims the next turn.
    """
    monkeypatch.setattr(
        server, "_load_cfg", lambda: {"display": {"busy_input_mode": "queue"}}
    )
    monkeypatch.setattr(server, "_run_prompt_submit", lambda *a, **k: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **k: None)

    a = _FakeClient("a", transport_id="conn-a")
    b = _FakeClient("b", transport_id="conn-b")
    session = _session(transport=a, running=True, turn_origin="conn-a")
    server._sessions["sid"] = session
    token = server.bind_transport(b)
    try:
        response = server._methods["prompt.submit"](
            "r1", {"session_id": "sid", "text": "from B"}
        )
        assert response["result"]["status"] == "queued"
        # A's turn is still the live one: it keeps its frames.
        assert session["turn_origin"] == "conn-a"

        session["running"] = False
        assert server._drain_queued_prompt("drain", "sid", session) is True
        server._emit("message.delta", "sid", {"text": "drained"})
    finally:
        server.reset_transport(token)
        server._sessions.pop("sid", None)

    assert session["turn_origin"] == "conn-b"
    assert a.frames[0]["params"]["origin"] == "conn-b"
    assert b.frames[0]["params"]["origin"] == "conn-b"


def test_a_drained_turn_is_the_queuers_even_after_the_queuer_disconnects(monkeypatch):
    """A queuer that left still owns the turn its prompt starts.

    The drain skips ATTACHING a transport whose client is gone, but attribution
    and delivery are different questions: the turn is still that client's work.
    Recording the origin only for a live queuer would leave the drained turn
    wearing the PREVIOUS turn's origin, which is the inversion this section
    exists to prevent, so the stamp is taken before the liveness check.
    """
    dispatched = []
    monkeypatch.setattr(
        server,
        "_run_prompt_submit",
        lambda rid, sid, _session, text, **kw: dispatched.append((rid, text)),
    )

    a = _FakeClient("a", transport_id="conn-a")
    b = _FakeClient("b", transport_id="conn-b")
    # A's turn has ended and left its origin behind: the stale value the drained
    # turn must not inherit.
    session = _session(transport=a, turn_origin="conn-a")
    server._enqueue_prompt(session, "from B", b)
    b._closed = True  # B's socket went away while its prompt sat in the queue
    server._sessions["sid"] = session
    try:
        assert server._drain_queued_prompt("drain", "sid", session) is True
        server._emit("message.delta", "sid", {"text": "drained answer"})
    finally:
        server._sessions.pop("sid", None)

    assert dispatched == [("drain", "from B")]  # the prompt still runs
    assert session["transport"] is a  # the dead pin was skipped
    assert session["turn_origin"] == "conn-b"  # but the turn is B's
    assert a.frames[0]["params"]["origin"] == "conn-b"  # not A's, not stale
    assert b.types() == []  # B is gone; nothing was delivered to it
