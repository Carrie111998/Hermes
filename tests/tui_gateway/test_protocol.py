"""Tests for tui_gateway JSON-RPC protocol plumbing."""

import io
import json
import sys
import threading
import time
import types
from unittest.mock import MagicMock, patch

import pytest

_original_stdout = sys.stdout


@pytest.fixture(autouse=True)
def _restore_stdout():
    yield
    sys.stdout = _original_stdout


@pytest.fixture()
def server():
    # The sys.modules mocks only need to cover the *initial* import — once
    # tui_gateway.server is cached, they are inert. Keeping them active for
    # the whole test poisons any module first imported inside a test body:
    # e.g. hermes_cli.active_sessions would bind the mocked get_hermes_home
    # (a fixed shared path) forever, leaking active-session registry entries
    # across every later test in the process. Scope the patch to the import.
    with patch.dict("sys.modules", {
        "hermes_constants": MagicMock(get_hermes_home=MagicMock(return_value="/tmp/hermes_test")),
        "hermes_cli.env_loader": MagicMock(),
        "hermes_cli.banner": MagicMock(),
        "hermes_state": MagicMock(),
    }):
        import importlib
        mod = importlib.import_module("tui_gateway.server")

    # Snapshot the RPC registry: several tests below stub handlers
    # ("slash.exec", "fast.ping", ...) directly in the module-level dict,
    # which is shared with every other test file in the process.
    methods = dict(mod._methods)
    real_stdout = mod._real_stdout
    yield mod
    # Reset module-level state without re-importing. importlib.reload
    # would re-register the module's atexit hooks (ThreadPoolExecutor
    # shutdown, _shutdown_sessions); the duplicates race the stderr
    # buffer at interpreter shutdown and surface as Fatal Python error:
    # _enter_buffered_busy. Restoring the dicts in place gives the next
    # test a clean slate.
    mod._methods.clear()
    mod._methods.update(methods)
    mod._real_stdout = real_stdout
    for sid in list(mod._sessions):
        mod._close_session_by_id(sid, end_reason="test_cleanup")
    mod._pending.clear()
    mod._answers.clear()
    mod._live_transports.clear()


def test_shared_fixture_cleanup_uses_full_session_teardown(server, monkeypatch):
    """The cross-file autouse cleanup must close every retained resource."""
    from tests import conftest

    closed = {"worker": 0, "agent": 0, "lease": 0}

    class _Closable:
        def __init__(self, key):
            self.key = key

        def close(self):
            closed[self.key] += 1

    class _Lease:
        def release(self):
            closed["lease"] += 1

    monkeypatch.setattr(server, "_get_db", lambda: None)
    server._sessions["leaked"] = {
        "session_key": "leaked",
        "agent": _Closable("agent"),
        "slash_worker": _Closable("worker"),
        "active_session_lease": _Lease(),
        "history": [],
    }

    conftest._teardown_tui_server_sessions(server)

    assert server._sessions == {}
    assert closed == {"worker": 1, "agent": 1, "lease": 1}


@pytest.fixture()
def capture(server):
    """Redirect server's real stdout to a StringIO and return (server, buf)."""
    buf = io.StringIO()
    server._real_stdout = buf
    return server, buf


# ── JSON-RPC envelope ────────────────────────────────────────────────


def test_unknown_method(server):
    resp = server.handle_request({"id": "1", "method": "bogus"})
    assert resp["error"]["code"] == -32601


def test_ok_envelope(server):
    assert server._ok("r1", {"x": 1}) == {
        "jsonrpc": "2.0", "id": "r1", "result": {"x": 1},
    }


def test_err_envelope(server):
    assert server._err("r2", 4001, "nope") == {
        "jsonrpc": "2.0", "id": "r2", "error": {"code": 4001, "message": "nope"},
    }


@pytest.mark.parametrize("kind", ["legacy", "hard-only", "dynamic-getattr"])
def test_session_interrupt_uses_explicit_stop_compatibility(server, monkeypatch, kind):
    calls = []

    class _Legacy:
        def interrupt(self):
            calls.append("legacy")

    class _HardOnly:
        def hard_interrupt(self):
            calls.append("hard")

    class _Dynamic:
        def interrupt(self):
            calls.append("legacy")

        def __getattr__(self, name):
            if name == "hard_interrupt":
                return lambda: calls.append("fabricated-hard")
            raise AttributeError(name)

    agent = {
        "legacy": _Legacy(),
        "hard-only": _HardOnly(),
        "dynamic-getattr": _Dynamic(),
    }[kind]
    session = {
        "agent": agent,
        "history_lock": threading.Lock(),
        "running": True,
        "queued_prompt": "later",
        "session_key": "session-key",
        "_run_thread": None,
    }
    monkeypatch.setattr(server, "_tts_stream_stop", lambda: None)
    monkeypatch.setattr(server, "_sess_nowait", lambda _params, _rid: (session, None))
    monkeypatch.setattr(server, "_sess", lambda _params, _rid: (session, None))
    monkeypatch.setattr(server, "_session_uses_compute_host", lambda _session: False)
    monkeypatch.setattr(server, "_clear_pending", lambda _sid: None)
    response = server._methods["session.interrupt"](
        "stop", {"session_id": "ui-session"}
    )

    assert response["result"]["status"] == "interrupted"
    assert calls == ["hard" if kind == "hard-only" else "legacy"]


# ── write_json ────────────────────────────────────────────────


def test_write_json(capture):
    server, buf = capture
    assert server.write_json({"test": True})
    assert json.loads(buf.getvalue()) == {"test": True}


def test_disable_flush_env_var_actually_wires_to_module_constant(monkeypatch):
    """End-to-end: setting `HERMES_TUI_GATEWAY_NO_FLUSH=1` and importing
    `tui_gateway.transport` fresh actually flips `_DISABLE_FLUSH` true.

    Reloads only the transport module — server.py is untouched so its
    atexit hooks/worker pool stay intact."""
    import importlib

    monkeypatch.setenv("HERMES_TUI_GATEWAY_NO_FLUSH", "1")
    transport_mod = importlib.reload(importlib.import_module("tui_gateway.transport"))

    try:
        assert transport_mod._DISABLE_FLUSH is True
    finally:
        # Restore the env-disabled state so other tests see the default.
        monkeypatch.delenv("HERMES_TUI_GATEWAY_NO_FLUSH", raising=False)
        importlib.reload(transport_mod)


# ── _emit ────────────────────────────────────────────────────────────


def test_emit_with_payload(capture):
    server, buf = capture
    server._emit("test.event", "s1", {"key": "val"})
    msg = json.loads(buf.getvalue())

    assert msg["method"] == "event"
    assert msg["params"]["type"] == "test.event"
    assert msg["params"]["session_id"] == "s1"
    assert msg["params"]["payload"]["key"] == "val"


# ── Blocking prompt round-trip ───────────────────────────────────────


def test_block_and_respond(capture):
    server, _ = capture
    result = [None]

    threading.Thread(
        target=lambda: result.__setitem__(0, server._block("test.prompt", "s1", {"q": "?"}, timeout=5)),
    ).start()

    for _ in range(100):
        if server._pending:
            break
        threading.Event().wait(0.01)

    rid = next(iter(server._pending))
    server._answers[rid] = "my_answer"
    # _pending values are (sid, Event) tuples — unpack to set the Event
    _, ev = server._pending[rid]
    ev.set()

    threading.Event().wait(0.1)
    assert result[0] == "my_answer"


@pytest.mark.parametrize(
    "event",
    ["secret.request", "sudo.request", "clarify.request", "terminal.read.request"],
)
def test_sensitive_prompt_timeout_emits_expiry(capture, event):
    server, buf = capture

    assert server._block(event, "s1", {}, timeout=0) == ""

    messages = [json.loads(line) for line in buf.getvalue().splitlines()]
    request, expiry = [message["params"] for message in messages]
    assert request["type"] == event
    assert expiry["type"] == event.removesuffix(".request") + ".expire"
    assert expiry["session_id"] == "s1"
    assert expiry["payload"]["request_id"] == request["payload"]["request_id"]


@pytest.mark.parametrize(
    ("method", "value_key"),
    [
        ("secret.respond", "value"),
        ("sudo.respond", "password"),
        ("clarify.respond", "answer"),
        ("terminal.read.respond", "text"),
    ],
)
def test_late_prompt_response_is_idempotent(server, method, value_key):
    """All four blocking bridges tolerate a late reply after their request has
    expired — the `*.respond` returns a graceful `{"status": "expired"}` instead
    of the raw 4009 protocol error a client would otherwise surface verbatim."""
    response = server.handle_request(
        {
            "id": "late-response",
            "method": method,
            "params": {"request_id": "expired-request", value_key: ""},
        }
    )

    assert response["result"] == {"status": "expired"}


def test_clear_pending(server):
    ev = threading.Event()
    # _pending values are (sid, Event) tuples
    server._pending["r1"] = ("sid-x", ev)
    server._clear_pending()

    assert ev.is_set()
    assert server._answers["r1"] == ""


# ── Session lookup ───────────────────────────────────────────────────


def test_sess_missing(server):
    _, err = server._sess({"session_id": "nope"}, "r1")
    assert err["error"]["code"] == 4001


# ── session.resume payload ────────────────────────────────────────────


def test_session_resume_returns_hydrated_messages(server, monkeypatch):
    class _DB:
        def get_session(self, _sid):
            return {"id": "20260409_010101_abc123"}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True, include_ids=True),
            )

        def get_messages_as_conversation(self, _sid, include_ancestors=False, include_ids=False, repair_alternation=False):
            rows = [
                {"id": 101, "role": "user", "content": "hello"},
                {"id": 102, "role": "assistant", "content": "yo", "reasoning": "thoughts"},
                {"id": 103, "role": "tool", "content": "searched"},
                {"id": 104, "role": "assistant", "content": "   "},
                {"id": 105, "role": "assistant", "content": None},
                {"id": 106, "role": "narrator", "content": "skip"},
            ]
            if include_ids:
                return rows
            return [{k: v for k, v in row.items() if k != "id"} for row in rows]

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_make_agent", lambda sid, key, session_id=None, session_db=None, **_kwargs: object())
    monkeypatch.setattr(server, "_init_session", lambda sid, key, agent, history, cols=80, **_kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda _agent, _session=None: {"model": "test/model"})

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            # eager_build: exercise the synchronous build path (this test
            # monkeypatches _make_agent/_init_session/_session_info).
            "params": {"session_id": "20260409_010101_abc123", "cols": 100, "eager_build": True},
        }
    )

    assert "error" not in resp
    assert resp["result"]["message_count"] == 3
    assert resp["result"]["messages"] == [
        {"role": "user", "text": "hello", "db_id": 101},
        {"role": "assistant", "text": "yo", "db_id": 102, "reasoning": "thoughts"},
        {"role": "tool", "name": "tool", "context": "", "db_id": 103},
    ]


def test_session_resume_defaults_to_deferred_build(server, monkeypatch):
    """A normal cold resume (no ``eager_build``) must return the full display
    transcript immediately and register an upgradable live session WITHOUT
    building the agent on the response path — that eager build is the
    multi-second switch latency. Deferred is the default; ``eager_build: true``
    opts back into the synchronous path."""

    target = "20260409_010101_abc123"

    class _DB:
        def get_session(self, _sid):
            return {
                "id": target,
                "model": "vendor/cool-model",
                "model_config": {"provider": "vendor"},
            }

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, sid):
            return sid

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True, include_ids=True),
            )

        def get_messages_as_conversation(self, _sid, include_ancestors=False, include_ids=False, repair_alternation=False):
            return [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "yo"},
            ]

    builds: list = []

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    # The response path must never call _make_agent; route the deferred timer
    # through a recorder so a 50ms fire can't build (or crash) under the test.
    monkeypatch.setattr(
        server, "_make_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no eager build"))
    )
    monkeypatch.setattr(server, "_start_agent_build", lambda sid, session: builds.append(sid))
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100},
        }
    )

    assert "error" not in resp
    result = resp["result"]
    assert result["resumed"] == target
    assert result["session_key"] == target
    assert result["message_count"] == 2
    assert result["messages"] == [
        {"role": "user", "text": "hello"},
        {"role": "assistant", "text": "yo"},
    ]
    # Lazy info contract (same shape session.create returns), with the session's
    # persisted model/provider restored rather than the global default.
    assert result["info"]["lazy"] is True
    assert result["info"]["model"] == "vendor/cool-model"
    assert result["info"]["provider"] == "vendor"
    assert result["info"]["desktop_contract"] == server.DESKTOP_BACKEND_CONTRACT

    sid = result["session_id"]
    session = server._sessions[sid]
    # Registered but not built: agent is None and the resume key is carried so a
    # later prompt.submit / _sess() upgrade continues THIS stored conversation.
    assert session["agent"] is None
    assert session["resume_session_id"] == target
    assert not session["agent_ready"].is_set()
    # Not a watch spectator: a normal deferred resume is a real session.
    assert not session.get("lazy")
    # The persisted runtime identity is stashed for the deferred build so it
    # can't drop the provider ("No LLM provider configured").
    assert session["resume_runtime_overrides"]["model_override"]["model"] == "vendor/cool-model"
    assert server._find_live_session_by_key(target) == (sid, session)


def test_enforce_session_cap_evicts_oldest_detached_only(server, monkeypatch):
    """The LRU cap frees the least-recently-active DETACHED sessions when over
    the limit, and never a live-transport / running / mid-build one."""

    monkeypatch.setattr(server, "_load_cfg", lambda: {"max_live_sessions": 2})
    evicted: list[str] = []
    monkeypatch.setattr(
        server,
        "_close_session_by_id",
        lambda sid, end_reason=None, predicate=None: evicted.append(sid),
    )

    def _ready() -> threading.Event:
        ev = threading.Event()
        ev.set()
        return ev

    detached = server._detached_ws_transport
    live = object()  # no _closed attr -> live transport, never evictable

    server._sessions.clear()
    server._sessions.update(
        {
            "old_detached": {"transport": detached, "last_active": 100.0, "agent_ready": _ready()},
            "new_detached": {"transport": detached, "last_active": 300.0, "agent_ready": _ready()},
            "running_detached": {
                "transport": detached,
                "last_active": 50.0,
                "running": True,
                "agent_ready": _ready(),
            },
            "focused_live": {"transport": live, "last_active": 200.0, "agent_ready": _ready()},
        }
    )

    server._enforce_session_cap()

    # 4 sessions, cap 2 -> evict 2. Only detached+idle+built are eligible, oldest
    # first; the running one and the live-transport one are exempt.
    assert evicted == ["old_detached", "new_detached"]


def test_enforce_session_cap_disabled_is_noop(server, monkeypatch):
    monkeypatch.setattr(server, "_load_cfg", lambda: {"max_live_sessions": 0})
    evicted: list[str] = []
    monkeypatch.setattr(
        server, "_close_session_by_id", lambda sid, end_reason=None: evicted.append(sid)
    )
    server._sessions.clear()
    server._sessions.update(
        {
            f"s{i}": {"transport": server._detached_ws_transport, "last_active": float(i)}
            for i in range(5)
        }
    )

    server._enforce_session_cap()

    assert evicted == []


def test_session_resume_handles_multimodal_list_content(server, monkeypatch):
    """A user message persisted with list-shaped multimodal content used to
    crash session resume with ``'list' object has no attribute 'strip'``."""

    multimodal_user = {
        "role": "user",
        "content": [
            {"type": "text", "text": "describe this"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,AAAA"},
            },
        ],
    }
    text_only_assistant = {"role": "assistant", "content": "ok"}

    class _DB:
        def get_session(self, _sid):
            return {"id": "20260502_000000_listcontent"}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True, include_ids=True),
            )

        def get_messages_as_conversation(self, _sid, include_ancestors=False, include_ids=False, repair_alternation=False):
            return [multimodal_user, text_only_assistant]

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_make_agent", lambda sid, key, session_id=None, session_db=None, **_kwargs: object())
    monkeypatch.setattr(server, "_init_session", lambda sid, key, agent, history, cols=80, **_kwargs: None)
    monkeypatch.setattr(server, "_session_info", lambda _agent, _session=None: {"model": "test/model"})

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": "20260502_000000_listcontent", "cols": 100, "eager_build": True},
        }
    )

    assert "error" not in resp
    assert resp["result"]["message_count"] == 2
    # The image_url part is preserved as a raw data URL inside the text so
    # the desktop renderer (which extracts embedded images) sees the same
    # content the optimistic local cache returns. Otherwise the inline
    # image flashes during initial cache hydration and then vanishes when
    # the resume payload overwrites it with cleaned text.
    assert resp["result"]["messages"] == [
        {
            "role": "user",
            "text": "describe this\ndata:image/png;base64,AAAA",
        },
        {"role": "assistant", "text": "ok"},
    ]


def test_session_resume_lazy_registers_watch_session_without_agent(server, monkeypatch):
    """``lazy: true`` (subagent watch windows) must register the live session
    — keyed for the child mirror, on this transport — WITHOUT building an
    agent. The eager build is what made opening a subagent window contend
    with the already-running parent turn."""

    target = "20260612_000000_child99"

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True, include_ids=True),
            )

        def get_messages_as_conversation(self, _sid, include_ancestors=False, include_ids=False, repair_alternation=False):
            return [
                {"role": "user", "content": "delegated goal"},
            ]

    def _boom(*_args, **_kwargs):
        raise AssertionError("lazy resume must not build an agent")

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_make_agent", _boom)

    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100, "lazy": True},
        }
    )

    assert "error" not in resp
    result = resp["result"]
    assert result["resumed"] == target
    assert result["session_key"] == target
    assert result["info"]["lazy"] is True
    assert result["info"]["desktop_contract"] == server.DESKTOP_BACKEND_CONTRACT
    assert result["messages"] == [{"role": "user", "text": "delegated goal"}]

    sid = result["session_id"]
    session = server._sessions[sid]
    assert session["agent"] is None
    # The child mirror finds the watch window by stored key.
    assert server._find_live_session_by_key(target) == (sid, session)
    # A later prompt.submit upgrade must continue THIS stored conversation.
    assert session["resume_session_id"] == target
    # No build started: the idle reaper must still be able to evict it, and
    # the live status must not report a never-ending "starting".
    assert not session["agent_ready"].is_set()
    assert server._session_live_status(sid, session) != "starting"
    session["transport"] = server._detached_ws_transport
    far_future = time.time() + 999999
    assert server._session_is_evictable(sid, session, far_future)

    # Resuming again (window refresh) reuses the same live session.
    resp2 = server.handle_request(
        {
            "id": "r2",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100, "lazy": True},
        }
    )
    assert "error" not in resp2
    assert resp2["result"]["session_id"] == sid
    assert len(server._sessions) == 1


def test_session_resume_lazy_reports_running_for_inflight_child(server, monkeypatch):
    """A watch window attaching to a child mid-delegation must learn the run is
    live from the resume response itself — the child can sit silent inside a
    long tool call, so waiting for the next stream event leaves the window
    looking dead."""

    target = "20260612_000000_child42"

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True, include_ids=True),
            )

        def get_messages_as_conversation(self, _sid, include_ancestors=False, include_ids=False, repair_alternation=False):
            return [{"role": "user", "content": "delegated goal"}]

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(
        server, "_make_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no build"))
    )
    server._active_child_runs[target] = time.time()
    try:
        resp = server.handle_request(
            {
                "id": "r1",
                "method": "session.resume",
                "params": {"session_id": target, "cols": 100, "lazy": True},
            }
        )
    finally:
        server._active_child_runs.pop(target, None)

    assert "error" not in resp
    assert resp["result"]["running"] is True
    assert resp["result"]["status"] == "streaming"


def test_session_resume_lazy_tolerates_missing_row_for_active_child(server, monkeypatch):
    """Race regression: a watch window opens on a freshly-spawned subagent and
    resumes BEFORE the child's first run_conversation() flushes its DB row.

    The child relays ``subagent.start`` (carrying child_session_id, which opens
    the window) before ``_ensure_db_session`` writes the row, so
    ``db.get_session(target)`` is momentarily empty. On slower hosts (WSL2) the
    window's lazy resume consistently lands in this gap. It used to hard-fail
    "session not found"; the frontend then 404'd on its REST messages fallback
    and the watch window spun forever. Since the child is provably live
    (``_child_run_active``), the lazy resume must instead register the live
    session with empty history so the mirror can stream the turn.
    """

    target = "20260616_131212_racey"

    class _DB:
        def get_session(self, _sid):
            # Row not flushed yet — the whole point of the race.
            return None

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True, include_ids=True),
            )

        def get_messages_as_conversation(self, _sid, include_ancestors=False, include_ids=False, repair_alternation=False):
            # No rows for an unwritten session.
            return []

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(
        server, "_make_agent", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no build"))
    )
    # Child is live in the relay registry even though its row isn't written.
    server._active_child_runs[target] = time.time()
    try:
        resp = server.handle_request(
            {
                "id": "r1",
                "method": "session.resume",
                "params": {"session_id": target, "cols": 100, "lazy": True},
            }
        )
    finally:
        server._active_child_runs.pop(target, None)

    # The resume must succeed (no "session not found") and register a live,
    # agent-less watch session the mirror can find by stored key.
    assert "error" not in resp
    result = resp["result"]
    assert result["resumed"] == target
    assert result["session_key"] == target
    assert result["info"]["lazy"] is True
    assert result["messages"] == []
    # Live for the mirror; reported running so the window shows a busy state.
    assert result["running"] is True
    assert result["status"] == "streaming"
    sid = result["session_id"]
    assert server._find_live_session_by_key(target) == (sid, server._sessions[sid])
    assert server._sessions[sid]["agent"] is None


def test_session_resume_missing_row_non_lazy_still_errors(server, monkeypatch):
    """The missing-row tolerance is scoped to lazy resumes of an ACTIVE child.
    A normal (non-lazy) resume of a genuinely unknown id must still fail fast
    with "session not found" rather than silently registering an empty session.
    """

    target = "20260616_000000_ghost"

    class _DB:
        def get_session(self, _sid):
            return None

        def get_session_by_title(self, _title):
            return None

    monkeypatch.setattr(server, "_get_db", lambda: _DB())

    # Non-lazy resume, no active child → hard error.
    resp = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100},
        }
    )
    assert "error" in resp
    assert "session not found" in resp["error"]["message"].lower()

    # Lazy resume but the child is NOT live → still an error (no live mirror to
    # justify an empty session; this would just be a dead, sessionless window).
    resp2 = server.handle_request(
        {
            "id": "r2",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100, "lazy": True},
        }
    )
    assert "error" in resp2
    assert "session not found" in resp2["error"]["message"].lower()


def test_session_resume_reuses_existing_live_session(server, monkeypatch):
    """Repeated resume must not allocate duplicate live agents."""

    target = "20260409_010101_abc123"
    created_sids: list[str] = []
    closed_sids: list[str] = []
    first_agent_started = threading.Event()
    agent_can_finish = threading.Event()

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True, include_ids=True),
            )

        def get_messages_as_conversation(self, _sid, include_ancestors=False, include_ids=False, repair_alternation=False):
            return [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "yo"},
            ]

    class _Worker:
        def close(self):
            pass

    class _Agent:
        def __init__(self, sid, session_id):
            self.sid = sid
            self.model = "test/model"
            self.session_id = session_id

        def close(self):
            closed_sids.append(self.sid)

    def make_agent(sid, key, session_id=None, session_db=None, **_kwargs):
        created_sids.append(sid)
        first_agent_started.set()
        assert agent_can_finish.wait(timeout=1)
        return _Agent(sid, session_id or key)

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_make_agent", make_agent)
    monkeypatch.setattr(server, "_SlashWorker", lambda _key, _model: _Worker())
    monkeypatch.setattr(
        server,
        "_start_notification_poller",
        lambda _sid, _session: threading.Event(),
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda _sid: None)
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, _session=None: {"model": "test/model"},
    )

    fake_approval = types.SimpleNamespace(
        load_permanent_allowlist=lambda: None,
        register_gateway_notify=lambda *_args, **_kwargs: None,
    )

    with patch.dict(sys.modules, {"tools.approval": fake_approval}):
        first_holder = {}

        def resume_first():
            first_holder["resp"] = server.handle_request(
                {
                    "id": "first",
                    "method": "session.resume",
                    # eager_build: this test drives the synchronous build race +
                    # double-checked locking that only the eager path exercises.
                    "params": {"session_id": target, "cols": 100, "eager_build": True},
                }
            )

        first_thread = threading.Thread(target=resume_first)
        first_thread.start()
        assert first_agent_started.wait(timeout=1)

        second_holder = {}

        def resume_second():
            second_holder["resp"] = server.handle_request(
                {
                    "id": "second",
                    "method": "session.resume",
                    "params": {"session_id": target, "cols": 120, "eager_build": True},
                }
            )

        second_thread = threading.Thread(target=resume_second)
        second_thread.start()
        agent_can_finish.set()

        first_thread.join(timeout=1)
        second_thread.join(timeout=1)
        assert not first_thread.is_alive()
        assert not second_thread.is_alive()
        first = first_holder["resp"]
        second = second_holder["resp"]

    assert "error" not in first
    assert "error" not in second
    # Both resumes resolve to the SAME single live session — the core invariant.
    assert second["result"]["session_id"] == first["result"]["session_id"]
    assert len(server._sessions) == 1
    assert [s.get("session_key") for s in server._sessions.values()].count(target) == 1
    winner = first["result"]["session_id"]
    # The agent build happens outside the resume lock, so a racing resume may
    # build a redundant agent; double-checked locking keeps only one live
    # session and closes any loser's agent (no worker/poller is wired for it).
    assert winner in created_sids
    survivors = [sid for sid in created_sids if sid not in closed_sids]
    assert survivors == [winner]
    assert all(sid == winner for sid in server._sessions)


def test_session_resume_reuses_live_agent_after_compression_rotation(server, monkeypatch):
    """Resume must match the live agent's current session_id, not stale session_key."""

    target = "20260409_020202_child"
    stale_parent = "20260409_010101_parent"
    sid = "live-rotated"
    server._sessions[sid] = {
        "agent": types.SimpleNamespace(model="test/model", session_id=target),
        "created_at": 123.0,
        "display_history_prefix": [],
        "history": [{"role": "assistant", "content": "live child"}],
        "history_lock": threading.RLock(),
        "last_active": 123.0,
        "running": False,
        "session_key": stale_parent,
        "transport": server._stdio_transport,
    }

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def resolve_resume_session_id(self, _target):
            return target

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, _session=None: {"model": "test/model"},
    )

    result = server.handle_request(
        {
            "id": "r1",
            "method": "session.resume",
            "params": {"session_id": target, "cols": 100},
        }
    )

    assert "error" not in result
    assert result["result"]["session_id"] == sid
    assert result["result"]["session_key"] == target
    assert len(server._sessions) == 1


def test_sync_session_key_after_compress_reanchors_active_session_lease(
    server, monkeypatch, tmp_path
):
    home = tmp_path / ".hermes"
    monkeypatch.setenv("HERMES_HOME", str(home))

    from hermes_cli.active_sessions import (
        active_session_registry_snapshot,
        try_acquire_active_session,
    )

    lease, message = try_acquire_active_session(
        session_id="session-old",
        surface="tui",
        config={"max_concurrent_sessions": 1},
        metadata={"live_session_id": "ui-1"},
    )
    assert message is None
    assert lease is not None

    session = {
        "active_session_lease": lease,
        "agent": types.SimpleNamespace(session_id="session-new"),
        "session_key": "session-old",
    }
    fake_approval = types.SimpleNamespace(
        disable_session_yolo=lambda *_args, **_kwargs: None,
        enable_session_yolo=lambda *_args, **_kwargs: None,
        is_session_yolo_enabled=lambda *_args, **_kwargs: False,
        register_gateway_notify=lambda *_args, **_kwargs: None,
        unregister_gateway_notify=lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(server, "_restart_slash_worker", lambda *_args, **_kwargs: None)

    with patch.dict(sys.modules, {"tools.approval": fake_approval}):
        server._sync_session_key_after_compress("ui-1", session)

    snapshot = active_session_registry_snapshot()
    assert session["session_key"] == "session-new"
    assert lease.session_id == "session-new"
    assert [entry["session_id"] for entry in snapshot] == ["session-new"]
    lease.release()


def test_session_resume_live_payload_uses_current_history_with_ancestors(server, monkeypatch):
    """Live resume should not reuse a stale ancestor-inclusive snapshot."""

    target = "20260409_010101_child"
    ancestor_history = [{"role": "user", "content": "ancestor"}]
    current_history = [
        {"role": "user", "content": "current"},
        {"role": "assistant", "content": "current reply"},
    ]

    class _DB:
        def get_session(self, _sid):
            return {"id": target}

        def get_session_by_title(self, _title):
            return None

        def reopen_session(self, _sid):
            return None

        def get_resume_conversations(self, session_id):
            return (
                self.get_messages_as_conversation(session_id, repair_alternation=True),
                self.get_messages_as_conversation(session_id, include_ancestors=True, include_ids=True),
            )

        def get_messages_as_conversation(self, _sid, include_ancestors=False, include_ids=False, repair_alternation=False):
            if include_ancestors:
                return ancestor_history + current_history
            return list(current_history)

    class _Worker:
        def close(self):
            pass

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda _sid, key, session_id=None, session_db=None, **_kwargs: types.SimpleNamespace(
            model="test/model", session_id=session_id or key
        ),
    )
    monkeypatch.setattr(server, "_SlashWorker", lambda _key, _model: _Worker())
    monkeypatch.setattr(
        server,
        "_start_notification_poller",
        lambda _sid, _session: threading.Event(),
    )
    monkeypatch.setattr(server, "_notify_session_boundary", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(server, "_wire_callbacks", lambda _sid: None)
    monkeypatch.setattr(server, "_emit", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, _session=None: {"model": "test/model"},
    )

    fake_approval = types.SimpleNamespace(
        load_permanent_allowlist=lambda: None,
        register_gateway_notify=lambda *_args, **_kwargs: None,
    )

    with patch.dict(sys.modules, {"tools.approval": fake_approval}):
        first = server.handle_request(
            {
                "id": "first",
                "method": "session.resume",
                "params": {"session_id": target, "cols": 100},
            }
        )

        assert "error" not in first
        sid = first["result"]["session_id"]
        assert first["result"]["messages"] == [
            {"role": "user", "text": "ancestor"},
            {"role": "user", "text": "current"},
            {"role": "assistant", "text": "current reply"},
        ]

        with server._sessions[sid]["history_lock"]:
            server._sessions[sid]["history"] = current_history + [
                {"role": "user", "content": "new live turn"},
                {"role": "assistant", "content": "new live reply"},
            ]

        second = server.handle_request(
            {
                "id": "second",
                "method": "session.resume",
                "params": {"session_id": target, "cols": 120},
            }
        )

    assert "error" not in second
    assert second["result"]["session_id"] == sid
    assert second["result"]["messages"] == [
        {"role": "user", "text": "ancestor"},
        {"role": "user", "text": "current"},
        {"role": "assistant", "text": "current reply"},
        {"role": "user", "text": "new live turn"},
        {"role": "assistant", "text": "new live reply"},
    ]


def test_session_activate_rebinds_orphaned_ws_session_to_current_transport(server, monkeypatch):
    """Reconnect + activate must reattach a parked live session before orphan reap."""

    class _Transport:
        def write(self, _obj):
            return True

    sid = "runtime01"
    old_transport = server._stdio_transport
    new_transport = _Transport()
    server._sessions[sid] = {
        "agent": types.SimpleNamespace(model="test/model"),
        "created_at": 123.0,
        "history": [],
        "history_lock": threading.RLock(),
        "last_active": 123.0,
        "running": False,
        "session_key": "20260409_010101_abc123",
        "transport": old_transport,
    }
    monkeypatch.setattr(server, "current_transport", lambda: new_transport)
    monkeypatch.setattr(server, "_get_db", lambda: None)
    monkeypatch.setattr(
        server,
        "_session_info",
        lambda _agent, _session=None: {"model": "test/model"},
    )

    resp = server.handle_request(
        {"id": "activate", "method": "session.activate", "params": {"session_id": sid}}
    )

    assert "error" not in resp
    assert resp["result"]["session_id"] == sid
    assert server._sessions[sid]["transport"] is new_transport
    assert not server._ws_session_is_orphaned(server._sessions[sid])


def test_session_branch_at_branches_from_persisted_message_without_building_source_agent(server, monkeypatch):
    built_calls = {"count": 0}
    made_calls = {"count": 0}
    branch_at_call = {}

    class _DB:
        def get_session_title(self, _key):
            return "parent-title"

        def get_next_title_in_lineage(self, base):
            return f"{base} 2"

        def branch_at_message(self, source_session_id, source_message_id, **kwargs):
            branch_at_call["value"] = (source_session_id, source_message_id, kwargs)
            return {
                "session_id": kwargs["new_session_id"],
                "parent_session_id": source_session_id,
                "source_session_id": source_session_id,
                "source_message_id": source_message_id,
                "cut_mode": "assistant_after",
                "copied_message_count": 2,
                "prefill": None,
            }

        def get_messages_as_conversation(self, sid, include_ancestors=False, include_ids=False):
            if sid == "branch-session":
                return [{"id": 11, "role": "user", "content": "hello"}]
            return [{"id": 7, "role": "user", "content": "parent"}, {"id": 8, "role": "assistant", "content": "reply"}]

        def set_session_title(self, _key, _title):
            return None

    def _boom(*_args, **_kwargs):
        built_calls["count"] += 1
        raise AssertionError("branch_at must not build the source session")

    def _make_agent(*_args, **_kwargs):
        made_calls["count"] += 1
        return object()

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_start_agent_build", _boom)
    monkeypatch.setattr(server, "_wait_agent", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_make_agent", _make_agent)
    monkeypatch.setattr(server, "_set_session_context", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_init_session", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _s: "/tmp/branch-cwd")
    monkeypatch.setattr(server, "_new_session_key", lambda: "branch-session")

    parent_sid = "parent01"
    server._sessions[parent_sid] = {
        "session_key": "parent-session",
        "history": [{"role": "user", "content": "parent"}, {"role": "assistant", "content": "reply"}],
        "history_lock": threading.Lock(),
        "cols": 80,
    }

    resp = server.handle_request(
        {
            "id": "b1",
            "method": "session.branch_at",
            "params": {"session_id": parent_sid, "message_id": 8},
        }
    )

    assert "error" not in resp, resp
    assert built_calls["count"] == 0
    assert made_calls["count"] == 1
    assert branch_at_call["value"][0] == "parent-session"
    assert branch_at_call["value"][1] == 8
    assert resp["result"]["branch_from_message_id"] == 8
    assert resp["result"]["db_session_id"] == "branch-session"


def test_session_branch_at_rejects_invalid_ordinal_and_releases_lease(server, monkeypatch):
    released = {"count": 0}

    class _Lease:
        def release(self):
            released["count"] += 1

    class _DB:
        def get_session_title(self, _key):
            return "parent-title"

        def get_next_title_in_lineage(self, base):
            return f"{base} 2"

        def get_messages_as_conversation(self, _sid, include_ancestors=False, include_ids=False):
            return [{"id": 1, "role": "user", "content": "hello"}]

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_new_session_key", lambda: "branch-session")
    monkeypatch.setattr(server, "_claim_active_session_slot", lambda *a, **k: (_Lease(), None))

    parent_sid = "parent01"
    server._sessions[parent_sid] = {
        "session_key": "parent-session",
        "history": [{"role": "user", "content": "hello"}],
        "history_lock": threading.Lock(),
        "cols": 80,
    }

    resp = server.handle_request(
        {
            "id": "b2",
            "method": "session.branch_at",
            "params": {"session_id": parent_sid, "ordinal": 9},
        }
    )

    assert resp["error"]["code"] == 4008
    assert "not found" in resp["error"]["message"].lower()
    assert released["count"] == 1


def test_session_branch_with_count_truncates_history(server, monkeypatch):
    """Branch-from-a-specific-message support (issue: Branch in new chat
    loses the question): the desktop client passes ``count`` to keep only
    the first N messages of the parent's live history - everything after
    the clicked message must NOT be copied into the branch.
    """
    append_calls = []

    class _DB:
        def get_session_title(self, _key):
            return "parent-title"

        def get_next_title_in_lineage(self, base):
            return f"{base} 2"

        def create_session(self, new_key, **kwargs):
            return new_key

        def append_message(self, **kwargs):
            append_calls.append(kwargs)
            return None

        def set_session_title(self, _key, _title):
            return None

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_resolve_model", lambda: "test/model")
    monkeypatch.setattr(server, "_new_session_key", lambda: "20260101_000001_child0")
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda _sid, key, session_id=None, session_db=None, **_kwargs: types.SimpleNamespace(
            model="test/model", session_id=session_id or key
        ),
    )
    monkeypatch.setattr(server, "_init_session", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _s: "/tmp/branch-cwd")

    parent_sid = "parent01"
    parent_key = "20260101_000000_parent"
    server._sessions[parent_sid] = {
        "session_key": parent_key,
        "history": [
            {"role": "user", "content": "question one"},
            {"role": "assistant", "content": "answer one"},
            {"role": "user", "content": "question two"},
            {"role": "assistant", "content": "answer two"},
        ],
        "history_lock": threading.Lock(),
        "cols": 80,
    }

    resp = server.handle_request(
        {
            "id": "b1",
            "method": "session.branch",
            "params": {"session_id": parent_sid, "count": 2},
        }
    )

    assert "error" not in resp, resp
    assert len(append_calls) == 2
    assert append_calls[0]["content"] == "question one"
    assert append_calls[1]["content"] == "answer one"
    assert resp["result"]["message_count"] == 2


def test_session_branch_forwards_original_timestamps(server, monkeypatch):
    """TUI /branch must copy the parent's messages WITH their original
    timestamps — append_message otherwise stamps time.time() at INSERT and
    the branch's whole history silently appears authored "now" (#28841).
    """
    append_calls = []

    class _DB:
        def get_session_title(self, _key):
            return "parent-title"

        def get_next_title_in_lineage(self, base):
            return f"{base} 2"

        def create_session(self, new_key, **kwargs):
            return new_key

        def append_message(self, **kwargs):
            append_calls.append(kwargs)
            return None

        def set_session_title(self, _key, _title):
            return None

    monkeypatch.setattr(server, "_get_db", lambda: _DB())
    monkeypatch.setattr(server, "_resolve_model", lambda: "test/model")
    monkeypatch.setattr(server, "_new_session_key", lambda: "20260101_000001_child0")
    monkeypatch.setattr(
        server,
        "_make_agent",
        lambda _sid, key, session_id=None, session_db=None, **_kwargs: types.SimpleNamespace(
            model="test/model", session_id=session_id or key
        ),
    )
    monkeypatch.setattr(server, "_init_session", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_set_session_context", lambda *_a, **_k: [])
    monkeypatch.setattr(server, "_clear_session_context", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_session_cwd", lambda _s: "/tmp/branch-cwd")

    original_ts = [1_700_000_000.0, 1_700_000_020.0]
    parent_sid = "parent02"
    server._sessions[parent_sid] = {
        "session_key": "20260101_000000_parent",
        "history": [
            {"role": "user", "content": "hello", "timestamp": original_ts[0]},
            {"role": "assistant", "content": "hi!", "timestamp": original_ts[1]},
        ],
        "history_lock": threading.Lock(),
        "cols": 80,
    }

    resp = server.handle_request(
        {"id": "b2", "method": "session.branch", "params": {"session_id": parent_sid}}
    )

    assert "error" not in resp, resp
    assert len(append_calls) == 2
    assert [c.get("timestamp") for c in append_calls] == original_ts


def test_persist_branch_seed_forwards_original_timestamps(server, monkeypatch):
    """First-turn branch seed persist must carry each copied message's
    original timestamp through to append_message (#28841)."""
    import contextlib

    append_calls = []

    class _DB:
        def append_message(self, **kwargs):
            append_calls.append(kwargs)
            return None

    @contextlib.contextmanager
    def _fake_session_db(_session):
        yield _DB()

    monkeypatch.setattr(server, "_session_db", _fake_session_db)

    original_ts = [100.0, 200.0]
    session = {
        "session_key": "20260101_000002_seed00",
        "parent_session_id": "20260101_000000_parent",
        "history": [
            {"role": "user", "content": "a", "timestamp": original_ts[0]},
            {"role": "assistant", "content": "b", "timestamp": original_ts[1]},
        ],
        "history_lock": threading.Lock(),
    }

    server._persist_branch_seed(session)

    assert session.get("_branch_seed_persisted") is True
    assert [c.get("timestamp") for c in append_calls] == original_ts


def test_make_agent_accepts_list_system_prompt(server, monkeypatch):
    captured = {}

    class _Agent:
        def __init__(self, **kwargs):
            captured.update(kwargs)
            self.model = kwargs.get("model", "")

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=_Agent))
    monkeypatch.setitem(
        sys.modules,
        "hermes_cli.runtime_provider",
        types.SimpleNamespace(
            resolve_runtime_provider=lambda **_kwargs: {
                "provider": "test",
                "base_url": None,
                "api_key": None,
                "api_mode": None,
            }
        ),
    )
    monkeypatch.setattr(server, "_load_cfg", lambda: {"agent": {"system_prompt": ["one", "two"]}})
    monkeypatch.setattr(server, "_resolve_startup_runtime", lambda: ("test/model", "test"))
    monkeypatch.setattr(server, "_get_db", lambda: None)

    server._make_agent("sid", "session-key", session_id="session-key")

    assert captured["ephemeral_system_prompt"] == "one\ntwo"


# ── Config I/O ───────────────────────────────────────────────────────


def test_config_roundtrip(server, tmp_path):
    server._hermes_home = tmp_path
    server._save_cfg({"model": "test/model"})
    assert server._load_cfg()["model"] == "test/model"


# ── _cli_exec_blocked ────────────────────────────────────────────────


@pytest.mark.parametrize("argv", [
    [],
    ["setup"],
    ["gateway"],
    ["sessions", "browse"],
    ["config", "edit"],
])
def test_cli_exec_blocked(server, argv):
    assert server._cli_exec_blocked(argv) is not None


# ── slash.exec skill command interception ────────────────────────────


def test_slash_exec_rejects_skill_commands(server):
    """slash.exec must reject skill commands so the TUI falls through to command.dispatch."""
    # Register a mock session
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid, "agent": None}

    # Mock scan_skill_commands to return a known skill
    fake_skills = {"/hermes-agent-dev": {"name": "hermes-agent-dev", "description": "Dev workflow"}}

    with patch("agent.skill_commands.get_skill_commands", return_value=fake_skills):
        resp = server.handle_request({
            "id": "r1",
            "method": "slash.exec",
            "params": {"command": "hermes-agent-dev", "session_id": sid},
        })

    # Should return an error so the TUI's .catch() fires command.dispatch
    assert "error" in resp
    assert resp["error"]["code"] == 4018
    assert "skill command" in resp["error"]["message"]


def test_command_dispatch_queue_sends_message(server):
    """command.dispatch /queue returns {type: 'send', message: ...} for the TUI."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}

    resp = server.handle_request({
        "id": "r1",
        "method": "command.dispatch",
        "params": {"name": "queue", "arg": "tell me about quantum computing", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "tell me about quantum computing"


def test_skills_manage_search_uses_tools_hub_sources(server):
    result = type("Result", (), {
        "description": "Build better terminal demos",
        "name": "showroom",
    })()
    auth = MagicMock(return_value="auth")
    router = MagicMock(return_value=["source"])
    search = MagicMock(return_value=[result])
    fake_hub = types.SimpleNamespace(
        GitHubAuth=auth,
        create_source_router=router,
        unified_search=search,
    )

    with patch.dict(sys.modules, {"tools.skills_hub": fake_hub}):
        resp = server.handle_request({
            "id": "skills-search",
            "method": "skills.manage",
            "params": {"action": "search", "query": "showroom"},
        })

    assert "error" not in resp
    assert resp["result"] == {
        "results": [{"description": "Build better terminal demos", "name": "showroom"}]
    }
    auth.assert_called_once_with()
    router.assert_called_once_with("auth")
    search.assert_called_once_with("showroom", ["source"], source_filter="all", limit=20)


def test_command_dispatch_steer_fallback_sends_message(server):
    """command.dispatch /steer with no active agent falls back to send."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid, "agent": None}

    resp = server.handle_request({
        "id": "r3",
        "method": "command.dispatch",
        "params": {"name": "steer", "arg": "focus on testing", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "focus on testing"


def test_command_dispatch_retry_finds_last_user_message(server):
    """command.dispatch /retry walks session['history'] to find the last user message."""
    sid = "test-session"
    history = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
        {"role": "assistant", "content": "second answer"},
    ]
    server._sessions[sid] = {
        "session_key": sid,
        "agent": None,
        "history": history,
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    resp = server.handle_request({
        "id": "r4",
        "method": "command.dispatch",
        "params": {"name": "retry", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "second question"
    # Verify history was truncated: everything from last user message onward removed
    assert len(server._sessions[sid]["history"]) == 2
    assert server._sessions[sid]["history"][-1]["role"] == "assistant"
    assert server._sessions[sid]["history_version"] == 1


def test_command_dispatch_retry_empty_history(server):
    """command.dispatch /retry with empty history returns error."""
    sid = "test-session"
    server._sessions[sid] = {
        "session_key": sid,
        "agent": None,
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    resp = server.handle_request({
        "id": "r5",
        "method": "command.dispatch",
        "params": {"name": "retry", "session_id": sid},
    })

    assert "error" in resp
    assert resp["error"]["code"] == 4018


def test_command_dispatch_retry_handles_multipart_content(server):
    """command.dispatch /retry extracts text from multipart content lists."""
    sid = "test-session"
    history = [
        {"role": "user", "content": [
            {"type": "text", "text": "analyze this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,..."}}
        ]},
        {"role": "assistant", "content": "I see the image."},
    ]
    server._sessions[sid] = {
        "session_key": sid,
        "agent": None,
        "history": history,
        "history_lock": threading.Lock(),
        "history_version": 0,
    }

    resp = server.handle_request({
        "id": "r6",
        "method": "command.dispatch",
        "params": {"name": "retry", "session_id": sid},
    })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "send"
    assert result["message"] == "analyze this"


def test_command_dispatch_returns_skill_payload(server):
    """command.dispatch returns structured skill payload for the TUI to send()."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}

    import agent.skill_commands as skill_commands

    fake_skills = {"/hermes-agent-dev": {"name": "hermes-agent-dev", "description": "Dev workflow"}}
    fake_msg = "Loaded skill content here"

    with patch.object(skill_commands, "_skill_commands", fake_skills), \
         patch.object(skill_commands, "get_skill_commands", return_value=fake_skills), \
         patch.object(skill_commands, "build_skill_invocation_message", return_value=fake_msg):
        resp = server.handle_request({
            "id": "r2",
            "method": "command.dispatch",
            "params": {"name": "hermes-agent-dev", "session_id": sid},
        })

    assert "error" not in resp
    result = resp["result"]
    assert result["type"] == "skill"
    assert result["message"] == fake_msg
    assert result["name"] == "hermes-agent-dev"


def test_command_dispatch_returns_custom_bundle_payload(server):
    """command.dispatch preserves bundle arguments in a sendable agent turn."""
    sid = "test-session"
    server._sessions[sid] = {"session_key": sid}
    fake_bundles = {
        "/review-suite": {
            "name": "review-suite",
            "skills": ["source-check", "claim-audit", "enough-research"],
        }
    }
    arg = "audit the migration plan"
    fake_msg = (
        '[IMPORTANT: The user has invoked the "review-suite" skill bundle.]\n\n'
        f"User instruction: {arg}"
    )

    with patch("agent.skill_bundles.get_skill_bundles", return_value=fake_bundles), \
         patch(
             "agent.skill_bundles.build_bundle_invocation_message",
             return_value=(
                 fake_msg,
                 ["source-check", "claim-audit", "enough-research"],
                 [],
             ),
         ) as build_bundle, \
         patch("agent.skill_commands.build_skill_invocation_message") as build_skill, \
         patch.object(server, "_resolve_session_platform", return_value="tui"):
        resp = server.handle_request({
            "id": "r-bundle-dispatch",
            "method": "command.dispatch",
            "params": {"name": "review-suite", "arg": arg, "session_id": sid},
        })

    assert "error" not in resp
    assert resp["result"] == {
        "type": "send",
        "message": fake_msg,
        "notice": "⚡ Loading bundle: review-suite (3 skills)",
    }
    build_bundle.assert_called_once_with(
        "/review-suite",
        arg,
        task_id=sid,
        platform="tui",
    )
    build_skill.assert_not_called()


def test_command_dispatch_awaits_async_plugin_handler(server):
    async def _handler(arg):
        return f"async:{arg}"

    with patch(
        "hermes_cli.plugins.get_plugin_command_handler",
        lambda name: _handler if name == "async-cmd" else None,
    ):
        resp = server.handle_request({
            "id": "r-plugin",
            "method": "command.dispatch",
            "params": {"name": "async-cmd", "arg": "hello"},
        })

    assert "error" not in resp
    assert resp["result"] == {"type": "plugin", "output": "async:hello"}


# ── dispatch(): pool routing for long handlers (#12546) ──────────────


def test_dispatch_runs_short_handlers_inline(server):
    """Non-long handlers return their response synchronously from dispatch()."""
    server._methods["fast.ping"] = lambda rid, params: server._ok(rid, {"pong": True})

    resp = server.dispatch({"id": "r1", "method": "fast.ping", "params": {}})

    assert resp == {"jsonrpc": "2.0", "id": "r1", "result": {"pong": True}}


@pytest.mark.parametrize("completion_method", ["complete.path", "complete.slash"])
def test_completion_handlers_are_pool_routed(completion_method, server):
    """complete.path/complete.slash must run on the pool, never the reader thread.

    Regression for #21123: completion ran inline, so a slow git ls-files /
    skill-scan blocked prompt.submit and froze the TUI for the 120s RPC timeout.
    """
    assert completion_method in server._LONG_HANDLERS


def test_skin_live_switch_end_to_end(server, tmp_path, monkeypatch):
    """Real config + skin files: activating a skin (as `hermes config set` does)
    makes the per-tool reconcile broadcast skin.changed with the resolved palette.
    Exercises _load_cfg → _skin_sig → resolve_skin → _emit with no mocks in between."""
    import hermes_cli.skin_engine as skin_engine

    (tmp_path / "skins").mkdir()
    (tmp_path / "skins" / "midnight.yaml").write_text(
        "name: midnight\ndescription: t\ncolors:\n  banner_title: '#00ffcc'\n  background: '#001010'\n"
    )
    monkeypatch.setattr(skin_engine, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(server, "_hermes_home", tmp_path)
    monkeypatch.setattr(server, "_last_skin_sig", None, raising=False)
    server._cfg_cache = server._cfg_mtime = server._cfg_path = None

    emitted = []
    monkeypatch.setattr(server, "_emit", lambda ev, sid, payload=None: emitted.append((ev, payload)))

    # Baseline (default) — seeds the signature.
    (tmp_path / "config.yaml").write_text("display:\n  skin: default\n", encoding="utf-8")
    server._broadcast_skin_if_changed()
    emitted.clear()

    # Activate midnight, as `hermes config set display.skin midnight` would.
    time.sleep(0.01)  # ensure the config mtime moves
    (tmp_path / "config.yaml").write_text("display:\n  skin: midnight\n", encoding="utf-8")
    server._broadcast_skin_if_changed()

    assert [ev for ev, _ in emitted] == ["skin.changed"]
    assert emitted[0][1]["name"] == "midnight"
    assert emitted[0][1]["colors"]["banner_title"] == "#00ffcc"


def test_broadcast_skin_if_changed_on_any_signature_move(server, monkeypatch):
    """A skin the agent changes mid-turn goes live once per real move: a name
    switch (incl. switch-then-revert) OR an in-place color edit to the active skin
    (same name, new file mtime). An unchanged signature never re-broadcasts."""
    emitted = []
    # switch, no-op, switch, then a color edit (same name, bumped mtime).
    sigs = iter([("neon", 1.0), ("neon", 1.0), ("forest", 1.0), ("forest", 2.0)])
    monkeypatch.setattr(server, "_emit", lambda ev, sid, payload=None: emitted.append((ev, payload)))
    monkeypatch.setattr(server, "_last_skin_sig", None, raising=False)
    monkeypatch.setattr(server, "_skin_sig", lambda: next(sigs))
    monkeypatch.setattr(server, "resolve_skin", lambda: {"name": "x", "colors": {}})

    for _ in range(4):
        server._broadcast_skin_if_changed()

    assert [ev for ev, _ in emitted] == ["skin.changed"] * 3


# ── global-event broadcast (session-less events reach every WS client) ──


class _RecordingTransport:
    """Minimal Transport stand-in that records the frames written to it."""

    def __init__(self) -> None:
        self.frames: list[dict] = []

    def write(self, obj: dict) -> bool:
        self.frames.append(obj)
        return True

    def close(self) -> None:
        pass


def test_unregister_live_transport_stops_delivery(capture):
    """A disconnected peer (unregistered in the ws finally block) receives nothing
    — and a stale write is never attempted against its closed socket."""
    server, buf = capture
    a = _RecordingTransport()
    server.register_live_transport(a)
    server.unregister_live_transport(a)

    server._broadcast_global_event("skin.changed", {"name": "x"})

    assert a.frames == []
    # No live transports left → fell back to stdio.
    assert json.loads(buf.getvalue())["params"]["type"] == "skin.changed"


