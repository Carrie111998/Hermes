import asyncio
import concurrent.futures
import json
import threading
import time

from hermes_cli import mcp_startup
from tui_gateway import mobile_contract
from tui_gateway import server
from tui_gateway import ws as ws_mod
from tui_gateway.mobile_sync import SessionEventStream


def test_gateway_ready_advertises_revisioned_mobile_sync(monkeypatch):
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

    authorization = {
        "subject": "user-1",
        "provider": "stub",
        "audience": "hermes.mobile",
        "scopes": ("conversation.read", "conversation.write"),
    }

    asyncio.run(ws_mod.handle_ws(FakeWS(), authorization=authorization))

    ready = sent[0]["params"]["payload"]
    assert ready["schemas"]["session.synchronization"] == 1
    assert ready["schemas"]["session.event"] == 1
    sync = ready["capabilities"]["conversation.sync"]
    assert sync["delta_offsets"] == {"unit": "utf8_bytes"}
    assert sync["replay"]["max_events"] > 0
    assert sync["replay"]["max_bytes"] > 0
    assert ready["authorization"]["audience"] == "hermes.mobile"


def test_mobile_authorization_is_enforced_on_requests_from_the_live_socket(
    monkeypatch,
):
    sent = []
    received = False
    monkeypatch.setattr(mcp_startup, "start_background_mcp_discovery", lambda **_kw: None)

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            sent.append(json.loads(line))

        async def receive_text(self):
            nonlocal received
            if not received:
                received = True
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "mobile-request",
                        "method": "not.a.mobile.method",
                        "params": {},
                    }
                )
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(
        ws_mod.handle_ws(
            FakeWS(),
            authorization={
                "subject": "user-1",
                "provider": "stub",
                "audience": "hermes.mobile",
                "scopes": ("conversation.read",),
            },
        )
    )

    assert sent[1]["error"] == {
        "code": 4030,
        "message": "insufficient authorization scope",
        "data": {
            "reason": "method_not_available_to_mobile",
            "method": "not.a.mobile.method",
            "required_scope": "mobile.unavailable",
            "required_scopes": ["mobile.unavailable"],
            "missing_scopes": ["mobile.unavailable"],
            "granted_scopes": ["conversation.read"],
            "grantable": False,
        },
    }


def test_mobile_socket_create_delta_and_live_resume_share_one_sync_stream(
    monkeypatch,
):
    """Exercise synchronization through the real handle_ws transport boundary."""
    sent = []
    receive_index = 0
    state = {}
    server._sessions.clear()
    monkeypatch.setattr(
        mcp_startup, "start_background_mcp_discovery", lambda **_kw: None
    )
    monkeypatch.setattr(
        server, "_claim_active_session_slot", lambda *_a, **_k: (None, None)
    )
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_profile_home", lambda *_a, **_k: None)
    monkeypatch.setattr(
        server, "_completion_cwd", lambda *_a, **_k: server.os.getcwd()
    )
    monkeypatch.setattr(server, "_git_branch_for_cwd", lambda *_a, **_k: "")
    monkeypatch.setattr(server, "_resolve_model", lambda: "test/model")
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(
        server, "_close_sessions_for_transport", lambda *_a, **_k: (0, 0)
    )

    class FakeDB:
        def get_session(self, session_id):
            return {"id": session_id, "cwd": server.os.getcwd()}

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, session_id):
            return session_id

        def get_messages_as_conversation(self, _session_id, include_ancestors=False):
            return []

    monkeypatch.setattr(server, "_get_db", lambda: FakeDB())

    async def wait_for_response(request_id):
        deadline = asyncio.get_running_loop().time() + 2
        while asyncio.get_running_loop().time() < deadline:
            match = next(
                (frame for frame in sent if frame.get("id") == request_id), None
            )
            if match is not None:
                return match
            await asyncio.sleep(0.01)
        raise AssertionError(f"missing WebSocket response {request_id}")

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            sent.append(json.loads(line))

        async def receive_text(self):
            nonlocal receive_index
            receive_index += 1
            if receive_index == 1:
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "create-sync",
                        "method": "session.create",
                        "params": {"cols": 100},
                    }
                )
            if receive_index == 2:
                created = (await wait_for_response("create-sync"))["result"]
                state["created"] = created
                sid = created["session_id"]
                session = server._sessions[sid]

                def emit_deltas():
                    with session["history_lock"]:
                        server._start_inflight_turn(session, "question")
                    server._emit_inflight_delta(sid, session, "A💡")
                    server._emit_inflight_delta(sid, session, "B")

                await asyncio.to_thread(emit_deltas)
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "resume-sync",
                        "method": "session.resume",
                        "params": {
                            "session_id": created["stored_session_id"],
                            "cols": 100,
                            "cursor": created["synchronization"]["recovery"]["cursor"],
                        },
                    }
                )
            await wait_for_response("resume-sync")
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    authorization = {
        "subject": "mobile-user",
        "provider": "stub",
        "audience": "hermes.mobile",
        "scopes": (
            "conversation.read",
            "conversation.write",
            "conversation.control",
        ),
    }

    try:
        asyncio.run(ws_mod.handle_ws(FakeWS(), authorization=authorization))
    finally:
        server._sessions.clear()

    created = state["created"]
    resumed = next(frame for frame in sent if frame.get("id") == "resume-sync")[
        "result"
    ]
    deltas = [
        frame["params"]
        for frame in sent
        if frame.get("params", {}).get("type") == "message.delta"
    ]
    assert [params["payload"]["offset"] for params in deltas] == [0, 5]
    assert len({params["payload"]["turn_id"] for params in deltas}) == 1
    assert [params["sequence"] for params in deltas] == [1, 2]
    assert resumed["session_id"] == created["session_id"]
    assert (
        resumed["synchronization"]["snapshot"]["stream_id"]
        == created["synchronization"]["snapshot"]["stream_id"]
    )
    assert resumed["synchronization"]["recovery"]["outcome"] == "complete"
    assert len(resumed["synchronization"]["recovery"]["events"]) == 2


def _isolate_mobile_ws_gateway(monkeypatch, db):
    server._sessions.clear()
    monkeypatch.setattr(
        mcp_startup, "start_background_mcp_discovery", lambda **_kw: None
    )
    monkeypatch.setattr(
        server, "_claim_active_session_slot", lambda *_a, **_k: (None, None)
    )
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_register_session_cwd", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_profile_home", lambda *_a, **_k: None)
    monkeypatch.setattr(
        server, "_completion_cwd", lambda *_a, **_k: server.os.getcwd()
    )
    monkeypatch.setattr(server, "_git_branch_for_cwd", lambda *_a, **_k: "")
    monkeypatch.setattr(server, "_resolve_model", lambda: "test/model")
    monkeypatch.setattr(server, "_current_profile_name", lambda: "default")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(
        server, "_close_sessions_for_transport", lambda *_a, **_k: (0, 0)
    )


class _MobileSessionDB:
    def __init__(self, session_id, messages=None):
        self.session_id = session_id
        self.messages = list(messages or [])

    def get_session(self, session_id):
        if session_id == self.session_id:
            return {"id": session_id, "cwd": server.os.getcwd()}
        return None

    def get_session_by_title(self, _title):
        return None

    def resolve_resume_session_id(self, session_id):
        return session_id

    def reopen_session(self, _session_id):
        return None

    def get_messages_as_conversation(self, _session_id, include_ancestors=False):
        return list(self.messages)

    def get_resume_conversations(self, _session_id):
        messages = list(self.messages)
        return messages, messages

    def get_ancestor_display_prefix(self, _session_id):
        return []


_MOBILE_AUTHORIZATION = {
    "subject": "mobile-user",
    "provider": "stub",
    "audience": "hermes.mobile",
    "scopes": (
        "conversation.read",
        "conversation.write",
        "conversation.control",
    ),
}


async def _wait_for_ws_response(sent, request_id):
    deadline = asyncio.get_running_loop().time() + 2
    while asyncio.get_running_loop().time() < deadline:
        match = next((frame for frame in sent if frame.get("id") == request_id), None)
        if match is not None:
            return match
        await asyncio.sleep(0.01)
    raise AssertionError(f"missing WebSocket response {request_id}")


def test_mobile_socket_cold_resume_reports_stream_reset(monkeypatch):
    sent = []
    received = False
    stored_id = "stored-cold"
    _isolate_mobile_ws_gateway(
        monkeypatch,
        _MobileSessionDB(
            stored_id,
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "hi"},
            ],
        ),
    )

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            sent.append(json.loads(line))

        async def receive_text(self):
            nonlocal received
            if not received:
                received = True
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "cold-resume",
                        "method": "session.resume",
                        "params": {
                            "session_id": stored_id,
                            "cols": 100,
                            "cursor": {
                                "server_instance_id": (
                                    mobile_contract.SERVER_INSTANCE_ID
                                ),
                                "stream_id": "retired-stream",
                                "sequence": 12,
                            },
                        },
                    }
                )
            await _wait_for_ws_response(sent, "cold-resume")
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    try:
        asyncio.run(
            ws_mod.handle_ws(FakeWS(), authorization=_MOBILE_AUTHORIZATION)
        )
    finally:
        server._sessions.clear()

    result = next(frame for frame in sent if frame.get("id") == "cold-resume")[
        "result"
    ]
    synchronization = result["synchronization"]
    assert synchronization["snapshot"]["messages"] == [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "hi"},
    ]
    assert synchronization["recovery"]["outcome"] == "reset"
    assert synchronization["recovery"]["reason"] == "stream_changed"
    assert synchronization["recovery"]["snapshot_required"] is True
    assert synchronization["recovery"]["events"] == []


def test_mobile_socket_resume_reports_gap_after_replay_eviction(monkeypatch):
    sent = []
    receive_index = 0
    db = _MobileSessionDB("unused-until-create")
    _isolate_mobile_ws_gateway(monkeypatch, db)

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            sent.append(json.loads(line))

        async def receive_text(self):
            nonlocal receive_index
            receive_index += 1
            if receive_index == 1:
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "gap-create",
                        "method": "session.create",
                        "params": {"cols": 100},
                    }
                )
            if receive_index == 2:
                created = (await _wait_for_ws_response(sent, "gap-create"))["result"]
                sid = created["session_id"]
                db.session_id = created["stored_session_id"]
                stream = SessionEventStream(
                    mobile_contract.SERVER_INSTANCE_ID,
                    max_events=2,
                    max_bytes=1024 * 1024,
                )
                server._sessions[sid]["mobile_sync"] = stream
                cursor = stream.cursor()

                def fill_replay():
                    for index in range(3):
                        server._emit(
                            "status.update",
                            sid,
                            {"kind": "step", "text": str(index)},
                        )

                await asyncio.to_thread(fill_replay)
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "gap-resume",
                        "method": "session.resume",
                        "params": {
                            "session_id": created["stored_session_id"],
                            "cols": 100,
                            "cursor": cursor,
                        },
                    }
                )
            await _wait_for_ws_response(sent, "gap-resume")
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    try:
        asyncio.run(
            ws_mod.handle_ws(FakeWS(), authorization=_MOBILE_AUTHORIZATION)
        )
    finally:
        server._sessions.clear()

    result = next(frame for frame in sent if frame.get("id") == "gap-resume")[
        "result"
    ]
    recovery = result["synchronization"]["recovery"]
    assert recovery["outcome"] == "gap"
    assert recovery["reason"] == "replay_evicted"
    assert recovery["available_after"] == 1
    assert recovery["snapshot_required"] is True
    assert recovery["events"] == []


def test_mobile_socket_concurrent_events_have_monotonic_wire_sequences(
    monkeypatch,
):
    sent = []
    receive_index = 0
    db = _MobileSessionDB("unused-until-create")
    _isolate_mobile_ws_gateway(monkeypatch, db)

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            sent.append(json.loads(line))

        async def receive_text(self):
            nonlocal receive_index
            receive_index += 1
            if receive_index == 1:
                return json.dumps(
                    {
                        "jsonrpc": "2.0",
                        "id": "concurrent-create",
                        "method": "session.create",
                        "params": {"cols": 100},
                    }
                )

            created = (await _wait_for_ws_response(sent, "concurrent-create"))[
                "result"
            ]
            sid = created["session_id"]
            start = threading.Barrier(9)

            def publish(index):
                start.wait()
                server._emit(
                    "status.update",
                    sid,
                    {"kind": "worker", "text": str(index)},
                )

            def publish_concurrently():
                threads = [
                    threading.Thread(target=publish, args=(index,))
                    for index in range(8)
                ]
                for thread in threads:
                    thread.start()
                start.wait()
                for thread in threads:
                    thread.join(timeout=1)
                    assert not thread.is_alive()

            await asyncio.to_thread(publish_concurrently)
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    try:
        asyncio.run(
            ws_mod.handle_ws(FakeWS(), authorization=_MOBILE_AUTHORIZATION)
        )
    finally:
        server._sessions.clear()

    events = [
        frame
        for frame in sent
        if frame.get("params", {}).get("type") == "status.update"
    ]
    assert [event["params"]["sequence"] for event in events] == list(range(1, 9))
    assert all(event["params"]["schema_major"] == 1 for event in events)
    assert len({event["params"]["stream_id"] for event in events}) == 1


def test_ws_does_not_own_mcp_discovery_startup(monkeypatch):
    """MCP discovery belongs to the profile-scoped agent build path."""

    calls = []

    monkeypatch.setattr(
        mcp_startup,
        "start_background_mcp_discovery",
        lambda **kw: calls.append(kw),
    )

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            pass

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    server._sessions.clear()
    try:
        asyncio.run(ws_mod.handle_ws(FakeWS()))
    finally:
        server._sessions.clear()

    assert calls == []


def _run_disconnect(monkeypatch, seed):
    """Drive handle_ws to its disconnect `finally`, seeding sessions against the
    live WSTransport the moment it exists. Returns nothing; inspect _sessions."""
    # Disable the grace-reap Timer: detached sessions normally schedule a
    # threading.Timer via _schedule_ws_orphan_reap, which would outlive the test
    # and fire _reap during interpreter teardown — touching _sessions/DB and
    # producing spurious post-run errors under the per-file CI runner. Grace=0
    # short-circuits the Timer (see _schedule_ws_orphan_reap) so the test leaves
    # no lingering thread.
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)

    # Mirror the real _finalize_session chokepoint: it is the single place that
    # closes the slash-worker (#38095). Stub it but keep that behavior so the
    # disconnect-reap path still exercises worker teardown.
    def _fake_finalize(s, end_reason="tui_close"):
        w = s.get("slash_worker")
        if w:
            w.close()

    monkeypatch.setattr(server, "_finalize_session", _fake_finalize)

    created = []
    real_transport = ws_mod.WSTransport
    monkeypatch.setattr(
        ws_mod, "WSTransport",
        lambda ws, loop, **kw: created.append(real_transport(ws, loop, **kw)) or created[-1],
    )

    class FakeWS:
        async def accept(self):
            pass

        async def send_text(self, line):
            pass

        async def receive_text(self):
            seed(created[0])  # transport now exists; attach it to sessions
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(ws_mod.handle_ws(FakeWS()))


def test_ws_disconnect_reaps_flagged_session_and_closes_worker(monkeypatch):
    closed = []

    class FakeWorker:
        def close(self):
            closed.append(True)

    server._sessions.clear()
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: server._sessions.update(
                flagged={
                    "transport": t,
                    "close_on_disconnect": True,
                    "slash_worker": FakeWorker(),
                    "session_key": "k",
                }
            ),
        )
        assert "flagged" not in server._sessions
        assert closed == [True]
    finally:
        server._sessions.clear()




def test_ws_connection_registers_then_disconnect_unregisters_live_transport(monkeypatch):
    """A connected client must be tracked in the live-transport registry so a
    session-less global broadcast (skin.changed from the background watcher)
    reaches it, and dropped on disconnect so no stale write targets a dead peer.
    This is the WS half of the cross-surface live-theme fix."""
    server._sessions.clear()
    server._live_transports.clear()
    seen = {}
    try:
        _run_disconnect(
            monkeypatch,
            lambda t: seen.__setitem__("registered", t in server._live_transports),
        )
        # Seeded at receive_text time — i.e. after gateway.ready registered it.
        assert seen["registered"] is True
        # handle_ws's finally must have unregistered it.
        assert not server._live_transports
    finally:
        server._sessions.clear()
        server._live_transports.clear()


def test_ws_disconnect_releases_wake_word_owner(monkeypatch):
    released = []
    created = []
    monkeypatch.setattr(
        server,
        "_release_wake_for_transport",
        lambda transport: released.append(transport) or True,
    )

    _run_disconnect(monkeypatch, lambda transport: created.append(transport))

    assert released == created


def test_old_socket_disconnect_cannot_detach_a_concurrent_reconnect(monkeypatch):
    old_transport = object()
    new_transport = object()
    cleanup_reached_session = threading.Event()
    allow_cleanup = threading.Event()
    scheduled = []

    class GateLock:
        def __enter__(self):
            cleanup_reached_session.set()
            assert allow_cleanup.wait(timeout=2)
            return self

        def __exit__(self, *_exc):
            return False

    server._sessions.clear()
    server._sessions["raced"] = {
        "transport": old_transport,
        "close_on_disconnect": False,
        "history_lock": GateLock(),
        "session_key": "stored",
    }
    monkeypatch.setattr(
        server,
        "_schedule_ws_orphan_reap",
        lambda sid: scheduled.append(sid),
    )
    result = {}

    def disconnect_old_socket():
        result["counts"] = server._close_sessions_for_transport(old_transport)

    cleanup = threading.Thread(target=disconnect_old_socket)
    cleanup.start()
    assert cleanup_reached_session.wait(timeout=1)
    server._sessions["raced"]["transport"] = new_transport
    allow_cleanup.set()
    cleanup.join(timeout=1)
    assert not cleanup.is_alive()

    assert result["counts"] == (0, 0)
    assert server._sessions["raced"]["transport"] is new_transport
    assert scheduled == []
    server._sessions.clear()


def test_close_on_disconnect_claim_blocks_a_concurrent_reconnect(monkeypatch):
    old_transport = object()
    new_transport = object()
    teardown_entered = threading.Event()
    release_teardown = threading.Event()
    session = {
        "agent": None,
        "close_on_disconnect": True,
        "history": [],
        "history_lock": threading.Lock(),
        "running": False,
        "session_key": "stored",
        "transport": old_transport,
    }
    server._sessions.clear()
    server._sessions["flagged"] = session

    def blocked_teardown(claimed, *, end_reason):
        assert claimed is session
        assert end_reason == "ws_disconnect"
        teardown_entered.set()
        assert release_teardown.wait(timeout=2)

    monkeypatch.setattr(server, "_teardown_session", blocked_teardown)
    result = {}

    def disconnect_old_socket():
        result["counts"] = server._close_sessions_for_transport(old_transport)

    cleanup = threading.Thread(target=disconnect_old_socket)
    cleanup.start()
    assert teardown_entered.wait(timeout=1)
    assert "flagged" not in server._sessions

    payload = server._live_session_payload(
        "flagged",
        session,
        transport=new_transport,
    )
    assert payload is None
    assert session["transport"] is old_transport

    release_teardown.set()
    cleanup.join(timeout=1)
    assert not cleanup.is_alive()
    assert result["counts"] == (1, 0)
    server._sessions.clear()


def test_ws_write_loop_stall_does_not_latch_transport(monkeypatch):
    """A write that times out because the event loop is stalled (GIL-heavy
    agent turn) must NOT latch the transport closed — the frame is already
    scheduled and flushes when the loop recovers. Latching here permanently
    silenced live watch windows after one slow write."""
    monkeypatch.setattr(ws_mod, "_WS_WRITE_TIMEOUT_S", 0.05)
    sent = []

    class FakeWS:
        async def send_text(self, line):
            sent.append(line)

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        transport = ws_mod.WSTransport(FakeWS(), loop, peer="stall-test")
        # Stall the loop well past the write timeout, then write from this
        # (non-loop) thread: the wait times out but the send stays in flight.
        loop.call_soon_threadsafe(time.sleep, 0.3)
        assert transport.write({"a": 1}) is True
        assert transport._closed is False

        # Once the loop breathes again, both the stalled frame and new writes
        # must reach the socket.
        assert transport.write({"b": 2}) is True
        deadline = time.time() + 2
        while len(sent) < 2 and time.time() < deadline:
            time.sleep(0.01)
        assert len(sent) == 2
        assert transport._closed is False
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_ws_starts_mcp_discovery_before_ready(monkeypatch):
    import tui_gateway.entry as entry

    calls = []
    events = []

    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)
    monkeypatch.setattr(entry, "ensure_mcp_discovery_started", lambda: calls.append("mcp"))

    class FakeWS:
        async def accept(self):
            events.append("accept")

        async def send_text(self, line):
            if '"gateway.ready"' in line:
                events.append(f"ready_after_{len(calls)}")

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    asyncio.run(ws_mod.handle_ws(FakeWS()))

    # Discovery moved to profile-aware agent construction. WebSocket transport
    # should not start MCP discovery before a profile has been bound.
    assert calls == []
    assert events == ["accept", "ready_after_0"]


def test_ws_transport_serializes_concurrent_sends():
    active_sends = 0
    max_active_sends = 0
    sent = []

    class FakeWS:
        async def send_text(self, line):
            nonlocal active_sends, max_active_sends
            active_sends += 1
            max_active_sends = max(max_active_sends, active_sends)
            try:
                await asyncio.sleep(0.05)
                sent.append(line)
            finally:
                active_sends -= 1

    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        transport = ws_mod.WSTransport(FakeWS(), loop, peer="serialize-test")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(transport.write, {"idx": 1}),
                pool.submit(transport.write, {"idx": 2}),
            ]
            assert [f.result(timeout=2) for f in futures] == [True, True]

        assert len(sent) == 2
        assert max_active_sends == 1
        assert transport._closed is False
    finally:
        loop.call_soon_threadsafe(loop.stop)
        thread.join(timeout=2)
        loop.close()


def test_ws_transport_preserves_cross_batch_order():
    async def scenario():
        entered = []
        first_entered = asyncio.Event()
        release_first = asyncio.Event()
        second_started = asyncio.Event()

        class FakeWS:
            async def send_text(self, line):
                entered.append(line)
                if line == "A1":
                    first_entered.set()
                    await release_first.wait()

        transport = ws_mod.WSTransport(
            FakeWS(), asyncio.get_running_loop(), peer="batch-order-test"
        )
        first = asyncio.create_task(transport._safe_send_many(["A1", "A2"]))
        await first_entered.wait()

        async def send_second():
            second_started.set()
            await transport._safe_send_many(["B1", "B2"])

        second = asyncio.create_task(send_second())
        await second_started.wait()

        # The second task has reached the transport. Without whole-batch
        # serialization it runs B1/B2 before this task can resume.
        assert entered == ["A1"]

        release_first.set()
        await asyncio.gather(first, second)
        assert entered == ["A1", "A2", "B1", "B2"]

    asyncio.run(scenario())


