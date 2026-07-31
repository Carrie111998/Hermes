"""Tests for TUI gateway /compress lock-hold signalling.

Covers the shared _compress_session_history choke point plus ALL THREE
in-process manual-compress consumers (session.compress RPC, the
command.dispatch compress branch, and the slash.exec mirror via
_mirror_slash_side_effects) — each must surface the lock-skip reason
instead of the misleading "No changes from compression" no-op text or a
generic "compress failed" error.
"""
import threading
from unittest.mock import MagicMock, patch

import pytest


def _make_history():
    return [
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
    ]


def _make_lock_skip_agent(signal):
    """Agent double whose _compress_context no-ops and sets the lock signal."""
    agent = MagicMock()
    agent._cached_system_prompt = ""
    agent.tools = None
    agent.session_id = "sess-lock"
    # Deferred-notify contract: no pending notification exists.
    agent._pending_context_engine_compression_notification = None

    def _fake_compress(msgs=None, *_args, **_kwargs):
        agent._compression_skipped_due_to_lock = signal
        return (list(msgs) if msgs is not None else _make_history(), "")

    agent._compress_context.side_effect = _fake_compress
    return agent


def _make_session(agent, history):
    return {
        "agent": agent,
        "history_lock": threading.Lock(),
        "history": history,
        "history_version": 1,
        "running": False,
        "session_key": "sess-lock",
    }


def test_compress_session_history_raises_on_lock_skip():
    """When _compression_skipped_due_to_lock is set on the agent,
    _compress_session_history must raise CompressionLockHeld with
    the holder string so callers can surface a clear message."""
    from tui_gateway.server import _compress_session_history, CompressionLockHeld

    history = _make_history()
    agent = _make_lock_skip_agent("pid=99999:tid=1:agent=1:nonce=abc")
    session = _make_session(agent, history)

    with (
        patch(
            "agent.model_metadata.estimate_request_tokens_rough", return_value=100
        ),
        pytest.raises(CompressionLockHeld) as exc_info,
    ):
        _compress_session_history(session)

    assert exc_info.value.holder == "pid=99999:tid=1:agent=1:nonce=abc"
    # The history must be untouched by the lock-skip.
    assert session["history"] == history
    assert session["history_version"] == 1


# ── Consumer 1: session.compress RPC ───────────────────────────────────


def test_session_compress_rpc_returns_lock_held_payload():
    from tui_gateway import server

    agent = _make_lock_skip_agent("pid=4242")
    session = _make_session(agent, _make_history())
    sid = "sid-lock-rpc"
    server._sessions[sid] = session
    try:
        with (
            patch.object(server, "_sess", return_value=(session, None)),
            patch.object(server, "_session_uses_compute_host", return_value=False),
            patch.object(server, "_status_update"),
            patch(
                "agent.model_metadata.estimate_request_tokens_rough",
                return_value=100,
            ),
        ):
            r = server._methods["session.compress"]("r1", {"session_id": sid})
    finally:
        server._sessions.pop(sid, None)

    result = r["result"]
    assert result["lock_held"] is True
    assert result["compressed"] is False
    assert "Compression already in progress" in result["message"]
    assert "pid=4242" in result["message"]
    assert "No changes from compression" not in result["message"]


# ── Consumer 2: command.dispatch compress branch ───────────────────────


def test_command_dispatch_compress_reports_lock_skip_as_output_not_error():
    """CompressionLockHeld must NOT fall through to the generic
    'compress failed' error handler — it's a clean no-op with feedback."""
    from tui_gateway import server

    agent = _make_lock_skip_agent("pid=5150")
    session = _make_session(agent, _make_history())
    sid = "sid-lock-dispatch"
    server._sessions[sid] = session
    try:
        with (
            patch.object(server, "_session_uses_compute_host", return_value=False),
            patch(
                "agent.model_metadata.estimate_request_tokens_rough",
                return_value=100,
            ),
        ):
            r = server._methods["command.dispatch"](
                "r2", {"name": "compress", "arg": "", "session_id": sid}
            )
    finally:
        server._sessions.pop(sid, None)

    assert "error" not in r, f"lock-skip surfaced as error: {r.get('error')}"
    output = r["result"]["output"]
    assert r["result"]["type"] == "exec"
    assert "Compression already in progress" in output
    assert "pid=5150" in output
    assert "compress failed" not in output
    assert "No changes from compression" not in output


# ── Consumer 3: slash.exec mirror (_mirror_slash_side_effects) ─────────


def test_mirror_slash_side_effects_reports_lock_skip():
    from tui_gateway import server

    agent = _make_lock_skip_agent("pid=6161")
    session = _make_session(agent, _make_history())
    sid = "sid-lock-mirror"
    server._sessions[sid] = session
    try:
        with (
            patch.object(server, "_sync_session_key_after_compress"),
            patch.object(server, "_emit"),
            patch(
                "agent.model_metadata.estimate_request_tokens_rough",
                return_value=100,
            ),
        ):
            output = server._mirror_slash_side_effects(sid, session, "/compress")
    finally:
        server._sessions.pop(sid, None)

    assert "Compression already in progress" in output
    assert "pid=6161" in output
    assert "No changes from compression" not in output
    assert "live session sync failed" not in output


def test_mirror_slash_side_effects_unconfirmed_lock_skip_wording():
    """signal=True (no confirmed holder) must use the 'could not acquire'
    wording rather than claiming another compression is running."""
    from tui_gateway import server

    agent = _make_lock_skip_agent(True)
    session = _make_session(agent, _make_history())
    sid = "sid-lock-mirror-unconfirmed"
    server._sessions[sid] = session
    try:
        with (
            patch.object(server, "_sync_session_key_after_compress"),
            patch.object(server, "_emit"),
            patch(
                "agent.model_metadata.estimate_request_tokens_rough",
                return_value=100,
            ),
        ):
            output = server._mirror_slash_side_effects(sid, session, "/compress")
    finally:
        server._sessions.pop(sid, None)

    assert "Compression skipped" in output
    assert "could not acquire" in output
    assert "already in progress" not in output


@pytest.mark.parametrize("route", ["rpc", "command_dispatch", "slash_mirror"])
def test_manual_compress_reserves_history_before_prompt_can_start(route):
    """Every in-process route queues prompts during manual compaction.

    The blocking compressor is a deterministic stand-in for the durable
    SessionDB commit window: while it is paused, ``prompt.submit`` must not
    mutate ``history`` or ``history_version``.  Once released, the compressed
    in-memory transcript is the sole committed successor.
    """
    from tui_gateway import server

    entered = threading.Event()
    release = threading.Event()
    history = _make_history()
    compressed = [{"role": "user", "content": "[summary]"}]
    agent = _make_lock_skip_agent(None)

    def _compress(msgs, *_args, **_kwargs):
        entered.set()
        assert release.wait(5), "test did not release the compressor"
        return compressed, ""

    agent._compress_context.side_effect = _compress
    session = _make_session(agent, history)
    sid = f"sid-compress-reservation-{route}"
    server._sessions[sid] = session
    result = {}

    def _run_compress():
        if route == "rpc":
            result["response"] = server._methods["session.compress"](
                "rpc-compress", {"session_id": sid}
            )
        elif route == "command_dispatch":
            result["response"] = server._methods["command.dispatch"](
                "dispatch-compress",
                {"name": "compress", "arg": "", "session_id": sid},
            )
        else:
            result["response"] = server._mirror_slash_side_effects(
                sid, session, "/compress"
            )

    try:
        with (
            patch.object(server, "_session_uses_compute_host", return_value=False),
            patch.object(server, "_status_update"),
            patch.object(server, "_sync_session_key_after_compress"),
            patch.object(server, "_emit"),
            patch.object(server, "_session_info", return_value={}),
            patch.object(server, "_get_usage", return_value={}),
            patch.object(server, "_ensure_active_session_slot", return_value=None),
            patch.object(server, "_load_dashboard_process_isolation_config", return_value={}),
            patch.object(server, "_drain_queued_prompt") as drain,
            patch(
                "agent.manual_compression_feedback.summarize_manual_compression",
                return_value={
                    "aborted": False,
                    "headline": "Compressed",
                    "token_line": "",
                    "note": "",
                },
            ),
            patch("agent.model_metadata.estimate_request_tokens_rough", return_value=100),
        ):
            thread = threading.Thread(target=_run_compress)
            thread.start()
            assert entered.wait(5), "compressor did not enter its commit window"

            prompt = server._methods["prompt.submit"](
                "prompt", {"session_id": sid, "text": "must run after compress"}
            )
            assert prompt["result"] == {"status": "queued"}
            assert session["history"] == history
            assert session["history_version"] == 1
            assert session["running"] is True
            assert session["queued_prompt"]["text"] == "must run after compress"

            release.set()
            thread.join(5)
            assert not thread.is_alive()

        if route == "slash_mirror":
            assert "live session sync failed" not in result["response"]
        else:
            assert "error" not in result["response"]
        assert session["history"] == compressed
        assert session["history_version"] == 2
        assert session["running"] is False
        assert "_manual_compression_reservation" not in session
        expected_rid = (
            "rpc-compress" if route == "rpc"
            else "dispatch-compress" if route == "command_dispatch"
            else "slash-compress"
        )
        drain.assert_called_once_with(expected_rid, sid, session)
    finally:
        release.set()
        server._sessions.pop(sid, None)
