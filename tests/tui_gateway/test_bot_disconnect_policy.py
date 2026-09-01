from __future__ import annotations

import threading
import types

import pytest

from tui_gateway import server


@pytest.fixture(autouse=True)
def _disable_agent_prewarm(monkeypatch):
    monkeypatch.setattr(server, "_schedule_agent_build", lambda *args, **kwargs: None)


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
        **extra,
    }


def test_compute_host_stop_publishes_cancel_before_interrupt(monkeypatch):
    observed = []
    session = _session(
        _compute_host_active=True,
        running=True,
        _queued_prompt_generation=7,
    )

    class _Supervisor:
        def interrupt(self, sid, *, request_id=None):
            observed.append(
                {
                    "sid": sid,
                    "request_id": request_id,
                    "cancelled": session.get("_turn_cancel_requested"),
                    "generation": session.get("_queued_prompt_generation"),
                }
            )

    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: True)
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: _Supervisor())
    monkeypatch.setattr(server, "_clear_pending", lambda _sid: None)

    assert server._interrupt_session_turn(
        "compute-stop",
        session,
        request_id="stop-request",
    ) is True
    assert observed == [
        {
            "sid": "compute-stop",
            "request_id": "stop-request",
            "cancelled": True,
            "generation": 8,
        }
    ]


def test_local_stop_rechecks_turn_after_waiting_for_queue_gate(monkeypatch):
    started = threading.Event()
    finished = threading.Event()
    interrupted = []

    class _RunThread:
        def is_alive(self):
            return True

    session = _session(
        agent=types.SimpleNamespace(
            interrupt=lambda: interrupted.append("interrupted")
        ),
        running=False,
        _run_thread=_RunThread(),
    )
    gate = server._queued_dispatch_gate(session)
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: False)
    monkeypatch.setattr(server, "_clear_pending", lambda _sid: None)

    def _stop():
        started.set()
        server._interrupt_session_turn("local-stop", session)
        finished.set()

    stop_thread = threading.Thread(target=_stop)
    gate.acquire()
    try:
        stop_thread.start()
        assert started.wait(timeout=1)
        assert not finished.wait(timeout=0.05)
        session["running"] = True
    finally:
        gate.release()
    stop_thread.join(timeout=1)

    assert finished.is_set()
    assert interrupted == ["interrupted"]
    assert session["_turn_cancel_requested"] is True


def test_compute_host_rejects_queued_submit_after_stop(monkeypatch):
    submitted = []
    session = _session(
        _compute_host_active=True,
        running=False,
        _turn_cancel_requested=True,
        _queued_prompt_generation=8,
    )

    class _Supervisor:
        def submit_turn(self, frame, *, on_complete=None):
            submitted.append(frame)

    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda _cfg=None: _Supervisor())

    response = server._submit_prompt_to_compute_host(
        "rid",
        "compute-stop",
        session,
        "must not run",
        queued_prompt_generation=7,
    )

    assert response["error"]["code"] == 4099
    assert submitted == []


def test_ws_orphan_reap_preserves_opted_in_running_turn_until_it_settles(monkeypatch):
    callbacks = []
    interrupted = []
    torn_down = []

    class _Timer:
        def __init__(self, _delay, callback):
            callbacks.append(callback)

        def start(self):
            return None

    class _LiveThread:
        def is_alive(self):
            return True

    session = _session(
        agent=types.SimpleNamespace(
            interrupt=lambda: interrupted.append("interrupted")
        ),
        transport=server._detached_ws_transport,
        running=True,
        _run_thread=_LiveThread(),
        preserve_running_on_disconnect=True,
    )
    server._sessions["bot-sid"] = session
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0.01)
    monkeypatch.setattr(server, "_WS_ORPHAN_INTERRUPT_REAP_MAX_POLLS", 1)
    monkeypatch.setattr(server.threading, "Timer", _Timer)
    monkeypatch.setattr(
        server,
        "_teardown_popped_session",
        lambda claimed, *, end_reason: torn_down.append((claimed, end_reason)) or True,
    )

    try:
        server._schedule_ws_orphan_reap("bot-sid")
        callbacks.pop(0)()

        assert interrupted == []
        assert "bot-sid" in server._sessions
        assert session.get("_turn_cancel_requested") is not True
        assert len(callbacks) == 1

        # A long-running Bot turn remains protected across multiple orphan
        # polls rather than receiving one delayed interrupt.
        callbacks.pop(0)()
        assert interrupted == []
        assert "bot-sid" in server._sessions
        assert len(callbacks) == 1

        # The ordinary-session interrupt budget is intentionally not a hidden
        # lifetime cap for Bot work. Only an explicit Stop (or the turn's own
        # model/tool limits) may end a protected turn.
        callbacks.pop(0)()
        assert interrupted == []
        assert "bot-sid" in server._sessions
        assert len(callbacks) == 1

        # An accepted next turn remains protected during the brief handoff
        # after the current turn clears ``running``.
        session["running"] = False
        session["queued_prompt"] = {
            "text": "accepted next turn",
            "transport": None,
        }
        callbacks.pop(0)()
        assert "bot-sid" in server._sessions
        assert len(callbacks) == 1

        # The queue claim marker closes the atomic handoff after the envelope
        # leaves ``queued_prompt`` but before the next turn owns ``running``.
        session["queued_prompt"] = None
        session["_queued_prompt_claimed"] = True
        callbacks.pop(0)()
        assert "bot-sid" in server._sessions
        assert len(callbacks) == 1

        # Once active, queued, and claimed work all settle, the detached
        # runtime is still reclaimed normally.
        session.pop("_queued_prompt_claimed", None)
        callbacks.pop(0)()
        assert "bot-sid" not in server._sessions
        assert torn_down == [(session, "ws_orphan_reap")]
    finally:
        server._sessions.pop("bot-sid", None)


def test_canonical_bot_turn_adopts_disconnect_policy_for_older_desktops(monkeypatch):
    session = _session(
        agent=types.SimpleNamespace(_session_title_hint="Bot Chat"),
        preserve_running_on_disconnect=False,
    )
    monkeypatch.setattr(
        "tools.bot_mode_probe.capability_fingerprint",
        lambda _home=None: "unavailable",
    )

    server._sync_bot_capabilities("bot-sid", session)

    assert session["preserve_running_on_disconnect"] is True


def test_prompt_submit_adopts_bot_disconnect_policy_before_turn_slot(monkeypatch):
    session = _session(preserve_running_on_disconnect=False)
    server._sessions["bot-submit"] = session
    monkeypatch.setattr(
        server,
        "_ensure_active_session_slot",
        lambda *_args, **_kwargs: "test stop before turn",
    )
    try:
        response = server.handle_request(
            {
                "id": "1",
                "method": "prompt.submit",
                "params": {
                    "session_id": "bot-submit",
                    "text": "continue working",
                    "preserve_running_on_disconnect": True,
                },
            }
        )
    finally:
        server._sessions.pop("bot-submit", None)

    assert response["error"]["code"] == 4090
    assert session["preserve_running_on_disconnect"] is True


def test_old_client_prompt_adopts_pending_bot_title_before_agent_build(monkeypatch):
    session = _session(
        agent=None,
        pending_title="Bot Chat",
        preserve_running_on_disconnect=False,
    )
    server._sessions["legacy-bot-submit"] = session
    monkeypatch.setattr(
        server,
        "_ensure_active_session_slot",
        lambda *_args, **_kwargs: "test stop before turn",
    )
    try:
        response = server.handle_request(
            {
                "id": "legacy-submit",
                "method": "prompt.submit",
                "params": {
                    "session_id": "legacy-bot-submit",
                    "text": "continue working",
                },
            }
        )
    finally:
        server._sessions.pop("legacy-bot-submit", None)

    assert response["error"]["code"] == 4090
    assert session["preserve_running_on_disconnect"] is True


def test_compressed_bot_tip_inherits_root_disconnect_policy(monkeypatch):
    root_reads = []

    class _DB:
        def get_compression_lineage(self, session_id):
            assert session_id == "tip-sid"
            root_reads.append(session_id)
            return ["root-sid", "tip-sid"]

        def get_session_title(self, session_id):
            assert session_id == "root-sid"
            return "Bot Chat"

    session = _session(
        agent=types.SimpleNamespace(
            _session_db=_DB(),
            _session_title_hint="UAT continuity test",
        ),
        session_key="tip-sid",
        preserve_running_on_disconnect=False,
    )
    monkeypatch.setattr(
        "tools.bot_mode_probe.capability_fingerprint",
        lambda _home=None: "unavailable",
    )

    server._sync_bot_capabilities("bot-tip", session)
    server._sync_bot_capabilities("bot-tip", session)

    assert session["preserve_running_on_disconnect"] is True
    assert session["_canonical_bot_chat"] is True
    assert root_reads == ["tip-sid"]


def test_ordinary_branch_does_not_inherit_bot_disconnect_policy(monkeypatch):
    lineage_reads = []

    class _DB:
        def get_compression_lineage(self, session_id):
            lineage_reads.append(session_id)
            return [session_id]

        def get_session_title(self, session_id):
            raise AssertionError("ordinary branch must not consult its parent root")

    session = _session(
        agent=types.SimpleNamespace(
            _session_db=_DB(),
            _session_title_hint="Research branch",
        ),
        session_key="branch-sid",
        preserve_running_on_disconnect=False,
    )

    server._sync_bot_capabilities("branch-ui", session)
    server._sync_bot_capabilities("branch-ui", session)

    assert session["preserve_running_on_disconnect"] is False
    assert session.get("_canonical_bot_chat") is not True
    assert lineage_reads == ["branch-sid"]


def test_non_bot_turn_keeps_default_disconnect_policy():
    session = _session(
        agent=types.SimpleNamespace(_session_title_hint="Research notes"),
        preserve_running_on_disconnect=False,
    )

    server._sync_bot_capabilities("ordinary-sid", session)

    assert session["preserve_running_on_disconnect"] is False


def test_session_title_invalidates_live_bot_identity_cache(monkeypatch):
    class _FakeDB:
        def __init__(self):
            self.title = "Research"

        def get_session_title(self, _key):
            return self.title

        def get_session(self, _key):
            return {"id": _key, "title": self.title}

        def set_session_title(self, _key, title):
            self.title = title
            return True

        def get_compression_lineage(self, key):
            return [key]

    db = _FakeDB()
    agent = types.SimpleNamespace(
        _session_db=db,
        _session_title_hint="Research",
    )
    session = _session(
        agent=agent,
        preserve_running_on_disconnect=False,
        session_key="title-cache",
    )
    session["_bot_identity_checked"] = ("title-cache", "Research")
    server._sessions["sid-title-cache"] = session
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(
        "tools.bot_mode_probe.capability_fingerprint",
        lambda _home=None: "unavailable",
    )
    try:
        renamed = server.handle_request(
            {
                "id": "rename-bot",
                "method": "session.title",
                "params": {"session_id": "sid-title-cache", "title": "Bot Chat"},
            }
        )
        assert "error" not in renamed
        assert agent._session_title_hint == "Bot Chat"
        assert session.get("_bot_identity_checked") is None

        server._sync_bot_capabilities("sid-title-cache", session)
        assert session["preserve_running_on_disconnect"] is True
        assert session["_canonical_bot_chat"] is True

        renamed_back = server.handle_request(
            {
                "id": "rename-ordinary",
                "method": "session.title",
                "params": {"session_id": "sid-title-cache", "title": "Research"},
            }
        )
        assert "error" not in renamed_back
        assert agent._session_title_hint == "Research"
        assert session.get("_canonical_bot_chat") is None

        server._sync_bot_capabilities("sid-title-cache", session)
        assert session["_bot_identity_checked"] == ("title-cache", "Research")
        # The live runtime remains monotone after adopting the policy; a new
        # resume will classify the persisted non-Bot title normally.
        assert session["preserve_running_on_disconnect"] is True
    finally:
        server._sessions.pop("sid-title-cache", None)


def test_stop_during_queued_claim_does_not_restore_cancelled_prompt(monkeypatch):
    session = _session(
        agent=types.SimpleNamespace(interrupt=lambda: None),
        queued_prompt={"text": "must stay stopped", "transport": None},
        preserve_running_on_disconnect=True,
        running=False,
    )
    base_lock = threading.RLock()

    class _ClaimInterlock:
        entries = 0

        def __enter__(self):
            base_lock.acquire()
            self.entries += 1
            return self

        def __exit__(self, exc_type, exc, traceback):
            base_lock.release()
            if self.entries == 1:
                server._interrupt_session_turn("queue-stop", session)
            return False

    session["history_lock"] = _ClaimInterlock()
    monkeypatch.setattr(server, "_clear_pending", lambda _sid: None)

    assert server._drain_queued_prompt("rid", "queue-stop", session) is True
    assert session.get("queued_prompt") is None
    assert not session.get("queued_prompts")
    assert session.get("_queued_prompt_claimed") is not True
    assert session["running"] is False
    assert session["_turn_cancel_requested"] is True


def test_compression_rotation_during_queued_claim_restores_prompt(monkeypatch):
    queued = {"text": "survive compression", "transport": None}
    session = _session(
        queued_prompt=queued,
        preserve_running_on_disconnect=True,
        running=False,
    )
    base_lock = threading.RLock()

    class _ClaimInterlock:
        entries = 0

        def __enter__(self):
            base_lock.acquire()
            self.entries += 1
            return self

        def __exit__(self, exc_type, exc, traceback):
            base_lock.release()
            if self.entries == 1:
                session["_queued_prompt_generation"] = 1
            return False

    session["history_lock"] = _ClaimInterlock()

    assert server._drain_queued_prompt("rid", "queue-compress", session) is True
    assert session["queued_prompt"] == queued
    assert not session.get("queued_prompts")
    assert session.get("_queued_prompt_claimed") is not True
    assert session["running"] is False
    assert session.get("_turn_cancel_requested") is not True


@pytest.mark.parametrize("boundary", ["close", "shutdown"])
def test_close_boundaries_cancel_claimed_prompt_before_dispatch(monkeypatch, boundary):
    session = _session(
        queued_prompt={"text": "must not dispatch", "transport": None},
        preserve_running_on_disconnect=True,
        running=False,
    )
    base_lock = threading.RLock()

    class _ClaimInterlock:
        entries = 0

        def __enter__(self):
            base_lock.acquire()
            self.entries += 1
            return self

        def __exit__(self, exc_type, exc, traceback):
            base_lock.release()
            if self.entries == 1:
                if boundary == "close":
                    server._pop_session_by_id("queue-close")
                else:
                    server._shutdown_sessions()
            return False

    session["history_lock"] = _ClaimInterlock()
    server._sessions["queue-close"] = session
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: True)
    monkeypatch.setattr(
        server,
        "_submit_prompt_to_compute_host",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("closed prompt must not dispatch")
        ),
    )
    monkeypatch.setattr(
        server,
        "_teardown_popped_session",
        lambda *args, **kwargs: True,
    )
    try:
        assert server._drain_queued_prompt("rid", "queue-close", session) is True
        assert "queue-close" not in server._sessions
        assert session["_closing"] is True
        assert session.get("_queued_prompt_claimed") is not True
        assert session["running"] is False
    finally:
        server._sessions.pop("queue-close", None)


def test_close_does_not_wait_for_running_queued_turn(monkeypatch):
    entered_dispatch = threading.Event()
    release_dispatch = threading.Event()
    close_finished = threading.Event()
    session = _session(
        queued_prompt={"text": "admit once", "transport": None},
        preserve_running_on_disconnect=True,
        running=False,
    )
    server._sessions["queue-gated-close"] = session

    def _fake_dispatch(*args, **kwargs):
        entered_dispatch.set()
        assert release_dispatch.wait(timeout=2)
        return True

    monkeypatch.setattr(server, "_dispatch_queued_claim", _fake_dispatch)
    drain_thread = threading.Thread(
        target=server._drain_queued_prompt,
        args=("rid", "queue-gated-close", session),
    )

    def _close():
        server._pop_session_by_id("queue-gated-close")
        close_finished.set()

    close_thread = threading.Thread(target=_close)
    try:
        drain_thread.start()
        assert entered_dispatch.wait(timeout=1)
        close_thread.start()
        assert close_finished.wait(timeout=1)

        release_dispatch.set()
        drain_thread.join(timeout=1)
        close_thread.join(timeout=1)

        assert not drain_thread.is_alive()
        assert not close_thread.is_alive()
        assert close_finished.is_set()
        assert "queue-gated-close" not in server._sessions
        assert session["_closing"] is True
    finally:
        release_dispatch.set()
        drain_thread.join(timeout=1)
        close_thread.join(timeout=1)
        server._sessions.pop("queue-gated-close", None)


def test_predicate_reaper_and_dispatch_gate_have_one_lock_order(monkeypatch):
    gate_owned = threading.Event()
    let_dispatch_take_sessions = threading.Event()
    dispatch_done = threading.Event()
    reaper_done = threading.Event()
    session = _session()
    server._sessions["predicate-lock-order"] = session
    monkeypatch.setattr(
        server,
        "_teardown_popped_session",
        lambda *args, **kwargs: True,
    )

    def _dispatch_side():
        with server._queued_dispatch_gate(session):
            gate_owned.set()
            assert let_dispatch_take_sessions.wait(timeout=1)
            with server._sessions_lock:
                pass
        dispatch_done.set()

    def _reaper_side():
        server._close_session_by_id(
            "predicate-lock-order",
            predicate=lambda _session: True,
        )
        reaper_done.set()

    dispatch_thread = threading.Thread(target=_dispatch_side)
    reaper_thread = threading.Thread(target=_reaper_side)
    try:
        dispatch_thread.start()
        assert gate_owned.wait(timeout=1)
        reaper_thread.start()
        let_dispatch_take_sessions.set()

        dispatch_thread.join(timeout=1)
        reaper_thread.join(timeout=1)

        assert dispatch_done.is_set()
        assert reaper_done.is_set()
        assert "predicate-lock-order" not in server._sessions
    finally:
        let_dispatch_take_sessions.set()
        dispatch_thread.join(timeout=1)
        reaper_thread.join(timeout=1)
        server._sessions.pop("predicate-lock-order", None)


def test_session_activate_can_adopt_bot_disconnect_policy(monkeypatch):
    monkeypatch.setattr(server, "_session_info", lambda agent: {"model": agent.model})
    session = _session(
        agent=types.SimpleNamespace(model="model-live"),
        preserve_running_on_disconnect=False,
    )
    server._sessions["sid-bot"] = session
    try:
        response = server.handle_request(
            {
                "id": "1",
                "method": "session.activate",
                "params": {
                    "session_id": "sid-bot",
                    "preserve_running_on_disconnect": True,
                },
            }
        )

        assert "error" not in response
        assert session["preserve_running_on_disconnect"] is True
    finally:
        server._sessions.pop("sid-bot", None)


def test_session_create_records_preserve_running_on_disconnect_flag(monkeypatch):
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: None)
    server._sessions.clear()
    try:
        on = server.handle_request(
            {
                "id": "1",
                "method": "session.create",
                "params": {"preserve_running_on_disconnect": True},
            }
        )["result"]["session_id"]
        off = server.handle_request(
            {"id": "2", "method": "session.create", "params": {}}
        )["result"]["session_id"]
        legacy_bot = server.handle_request(
            {
                "id": "3",
                "method": "session.create",
                "params": {"title": "Bot Chat"},
            }
        )["result"]["session_id"]
        assert server._sessions[on]["preserve_running_on_disconnect"]
        assert not server._sessions[off]["preserve_running_on_disconnect"]
        assert server._sessions[legacy_bot]["preserve_running_on_disconnect"]
    finally:
        server._sessions.clear()


def test_old_client_cold_bot_resume_adopts_disconnect_policy_before_build(monkeypatch):
    class FakeDB:
        def get_session(self, target):
            return {"id": target, "title": "Bot Chat", "message_count": 0}

        def get_compression_tip(self, target):
            return target

        def get_compression_lineage(self, target):
            return [target]

        def get_session_title(self, target):
            return "Bot Chat"

        def assert_resume_safe(self, target):
            return None

    monkeypatch.setattr(server, "_get_db", lambda: FakeDB())
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_schedule_resume_hydration", lambda *args, **kwargs: None)
    server._sessions.clear()
    try:
        response = server.handle_request(
            {
                "id": "legacy-resume",
                "method": "session.resume",
                "params": {
                    "session_id": "stored-bot",
                    "defer_history": True,
                },
            }
        )

        assert "error" not in response
        sid = response["result"]["session_id"]
        assert server._sessions[sid]["agent"] is None
        assert server._sessions[sid]["preserve_running_on_disconnect"] is True
    finally:
        server._sessions.clear()


def test_cold_resume_does_not_inherit_policy_from_noncompression_parent(monkeypatch):
    class FakeDB:
        def get_session(self, target):
            return {"id": target, "title": "Research branch", "message_count": 0}

        def resolve_resume_session_id(self, target):
            return target

        def get_compression_lineage(self, target):
            return [target]

        def get_session_title(self, target):
            raise AssertionError("branch parent title must not be consulted")

        def assert_resume_safe(self, target):
            return None

    monkeypatch.setattr(server, "_get_db", lambda: FakeDB())
    monkeypatch.setattr(server, "_enable_gateway_prompts", lambda: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    monkeypatch.setattr(server, "_schedule_resume_hydration", lambda *args, **kwargs: None)
    server._sessions.clear()
    try:
        response = server.handle_request(
            {
                "id": "branch-resume",
                "method": "session.resume",
                "params": {
                    "session_id": "branch-sid",
                    "defer_history": True,
                },
            }
        )

        assert "error" not in response
        sid = response["result"]["session_id"]
        assert server._sessions[sid]["preserve_running_on_disconnect"] is False
    finally:
        server._sessions.clear()
