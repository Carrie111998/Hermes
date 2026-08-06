"""Regression tests for the Codex time-to-first-byte (TTFB) watchdog.

The chatgpt.com/backend-api/codex endpoint has an intermittent failure mode
where it accepts the connection but never emits a single stream event. The
watchdog in ``interruptible_api_call`` kills such a connection at a short TTFB
cutoff (instead of waiting out the much longer wall-clock stale timeout) so the
retry loop can reconnect promptly. Once any stream event arrives, the TTFB
watchdog is satisfied and a separate idle watchdog handles streams that stop
emitting SSE events.

The "bytes flowing" signal is ``agent._codex_stream_last_event_ts``, set on
*any* event by ``codex_runtime.run_codex_stream`` — so reasoning-only or
tool-call-only turns (which emit no output-text deltas) are not mistaken for a
stall.
"""

from __future__ import annotations

import sys
import threading
import time
import types
from types import SimpleNamespace

import httpx
import pytest

# Stub optional heavy imports so run_agent imports cleanly in isolation.
sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())


def _make_codex_agent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    from run_agent import AIAgent

    agent = AIAgent(
        model="gpt-5.5",
        provider="openai-codex",
        api_key="sk-dummy",
        base_url="https://chatgpt.com/backend-api/codex",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    # The watchdog is gated on the codex_responses api_mode; assert/force it so
    # the test is robust to detection-logic changes elsewhere.
    agent.api_mode = "codex_responses"
    monkeypatch.setattr(agent, "_emit_status", lambda *a, **k: None)
    # Keep the wall-clock stale timeout high so any early kill is unambiguously
    # the TTFB path, not the stale-call path.
    monkeypatch.setattr(
        agent, "_compute_non_stream_stale_timeout", lambda *a, **k: 60.0
    )
    return agent


def test_ttfb_includes_silent_hang_hint_for_gpt_5_5(tmp_path, monkeypatch):
    """The no-first-byte watchdog should surface the same actionable hint as the
    stale-call timeout path when the model matches the silent-hang heuristic."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "0.4")

    closes: list = []
    statuses: list[str] = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(agent, "_buffer_status", lambda msg: statuses.append(msg))
    monkeypatch.setattr(agent, "_emit_status", lambda msg: statuses.append(msg))
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    stop = {"flag": False}

    def fake_hang(api_kwargs, client=None, on_first_delta=None):
        deadline = time.time() + 30
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_hang)

    try:
        with pytest.raises(TimeoutError) as excinfo:
            h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
        message = str(excinfo.value)
        assert "gpt-5.4" in message
        assert "gpt-5.3-codex" in message
        assert "gpt-5.4-codex" in message
        assert "codex_ttfb_kill" in closes
        assert statuses, "expected a user-facing watchdog status"
        assert any("gpt-5.4" in s and "gpt-5.3-codex" in s for s in statuses)
    finally:
        stop["flag"] = True


def test_ttfb_does_not_kill_when_events_flow(tmp_path, monkeypatch):
    """Once a stream event has arrived, a generation that runs past the TTFB
    cutoff is NOT killed by the watchdog — it completes normally."""
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "0.4")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    sentinel = SimpleNamespace(ok=True)

    def fake_stream(api_kwargs, client=None, on_first_delta=None):
        # Bytes flowing: mark stream activity right away, then keep generating
        # past the 0.4s TTFB cutoff before returning a real response.
        agent._codex_stream_last_event_ts = time.time()
        if on_first_delta:
            on_first_delta()
        time.sleep(0.9)
        return sentinel

    monkeypatch.setattr(agent, "_run_codex_stream", fake_stream)

    resp = h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "hi"})
    assert resp is sentinel
    assert "codex_ttfb_kill" not in closes


@pytest.mark.parametrize(
    "stale_timeout",
    [float("inf"), float("-inf"), float("nan")],
)
def test_wait_notice_omits_reconnect_when_all_deadlines_are_non_finite(
    stale_timeout,
):
    """A disabled watchdog must not be advertised as a future reconnect."""
    from agent import chat_completion_helpers as h

    recovery = h._codex_wait_notice_recovery(
        stale_timeout=stale_timeout,
        ttfb_enabled=False,
        ttfb_timeout=float("nan"),
        last_event_ts=None,
        call_start=100.0,
        idle_enabled=False,
        idle_timeout=float("nan"),
        elapsed=30.0,
    )

    assert recovery == ""


def test_moa_heartbeat_survives_infinite_stale_timeout(monkeypatch):
    """The full 100-poll MoA heartbeat must leave a healthy call running."""
    from agent import chat_completion_helpers as h

    notices: list[str] = []
    response = SimpleNamespace(ok=True)
    agent = SimpleNamespace(
        platform="desktop",
        api_mode="chat_completions",
        provider="moa",
        _consecutive_stale_streams=0,
        _interrupt_requested=False,
        _compute_non_stream_stale_timeout=lambda _kwargs: float("inf"),
        _touch_activity=lambda _message: None,
        _emit_wait_notice=notices.append,
    )

    class HeartbeatThread:
        """Keep the synthetic worker alive through one heartbeat."""

        def __init__(self, *, target, daemon):
            self._polls = 0
            self._target = target

        def start(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            self._polls += 1
            if self._polls == 101:
                self._target()
                return False
            return True

    monkeypatch.setattr(h.threading, "Thread", HeartbeatThread)
    monkeypatch.setattr(
        h,
        "_dispatch_nonstreaming_api_request",
        lambda *_args, **_kwargs: response,
    )

    result = h.interruptible_api_call(agent, {"model": "openai-xai-wide"})

    assert result is response
    assert len(notices) == 1
    assert "waiting on openai-xai-wide" in notices[0]
    assert "auto-reconnect" not in notices[0]


def test_wait_notice_formatting_error_does_not_abort_request(monkeypatch):
    """Status construction is fail-open even if its formatter breaks."""
    from agent import chat_completion_helpers as h

    response = SimpleNamespace(ok=True)
    agent = SimpleNamespace(
        platform="desktop",
        api_mode="chat_completions",
        provider="moa",
        _consecutive_stale_streams=0,
        _interrupt_requested=False,
        _compute_non_stream_stale_timeout=lambda _kwargs: float("inf"),
        _touch_activity=lambda _message: None,
        _emit_wait_notice=lambda _message: None,
    )

    class HeartbeatThread:
        def __init__(self, *, target, daemon):
            self._polls = 0
            self._target = target

        def start(self):
            pass

        def join(self, timeout=None):
            pass

        def is_alive(self):
            self._polls += 1
            if self._polls == 101:
                self._target()
                return False
            return True

    monkeypatch.setattr(h.threading, "Thread", HeartbeatThread)
    monkeypatch.setattr(
        h,
        "_dispatch_nonstreaming_api_request",
        lambda *_args, **_kwargs: response,
    )
    monkeypatch.setattr(
        h,
        "_codex_wait_notice_recovery",
        lambda **_kwargs: (_ for _ in ()).throw(ValueError("bad display state")),
    )

    result = h.interruptible_api_call(agent, {"model": "openai-xai-wide"})

    assert result is response


def test_large_codex_request_hard_ceiling_reclaims_silent_stall(tmp_path, monkeypatch):
    """#64507 regression: a large Codex request (TTFB watchdog disabled by the
    size gate, stale floor *raised*) that never emits a single byte must still
    be reclaimed at a finite hard ceiling — not hang for 13+ minutes while the
    worker stays idle and the session shows as active.

    Uses the real default TTFB threshold (120s) and asserts the request dies at
    the hard ceiling regardless of the size-based TTFB disable.
    """
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    # Real default TTFB threshold (no HERMES_CODEX_TTFB_* override) → for a
    # >10k-token request the no-byte TTFB watchdog is auto-disabled.
    monkeypatch.setenv("HERMES_CODEX_HARD_TIMEOUT_SECONDS", "3")

    closes: list = []
    dummy_client = SimpleNamespace()
    monkeypatch.setattr(agent, "_create_request_openai_client", lambda **k: dummy_client)
    monkeypatch.setattr(
        agent, "_abort_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )
    monkeypatch.setattr(
        agent, "_close_request_openai_client",
        lambda c, reason=None: closes.append(reason),
    )

    stop = {"flag": False}

    def fake_hang(api_kwargs, client=None, on_first_delta=None):
        # No event marker AND no event ever: the exact issue-64507 stall.
        deadline = time.time() + 120
        while time.time() < deadline and not stop["flag"] and not agent._interrupt_requested:
            time.sleep(0.02)
        raise RuntimeError("connection closed")

    monkeypatch.setattr(agent, "_run_codex_stream", fake_hang)

    large_input = "x" * 44_000  # ~11k estimated tokens → TTFB disabled, stale raised
    t0 = time.time()
    try:
        with pytest.raises(TimeoutError) as excinfo:
            h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": large_input})
        elapsed = time.time() - t0
        # Must die at the hard ceiling (3s), nowhere near the raised stale floor.
        assert elapsed < 30, f"hard ceiling took {elapsed:.1f}s — stall not reclaimed"
        assert "stale_call_kill" in closes, f"stale kill expected, got {closes}"
        assert "timed out after" in str(excinfo.value)
        assert "with no response" in str(excinfo.value)
    finally:
        stop["flag"] = True


def test_ttfb_abandoned_worker_cannot_supersede_replacement_request(
    tmp_path, monkeypatch
):
    """A watchdog-abandoned Codex worker must not reclaim writer ownership via
    run_codex_stream's inner retry after the outer layer has already started a
    replacement request.

    Timeline pinned here:
    1. Request A opens a physical stream that never emits a first byte.
    2. The TTFB watchdog aborts A and returns to the caller after the 2s join.
    3. Replacement request B starts and claims the single-writer token.
    4. The abandoned A worker finally sees its transport error.
    5. Buggy behavior: A performs the inner retry, reclaims the writer token,
       emits stale deltas, and fences B out before B reaches its terminal
       response.

    The fix must stop A before step 5: no inner retry, no stale deltas, B keeps
    the writer token and consumes its terminal response.
    """
    from agent import chat_completion_helpers as h

    agent = _make_codex_agent(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_CODEX_TTFB_TIMEOUT_SECONDS", "0.2")

    delivered: list[str] = []
    setattr(agent, "stream_delta_callback", lambda text: delivered.append(text))
    setattr(agent, "_stream_callback", None)

    a_retry_started = threading.Event()
    a_retry_finished = threading.Event()

    def _event(event_type, **fields):
        return SimpleNamespace(type=event_type, **fields)

    class _AbortThenTimeoutStream:
        def __init__(self, abort_event: threading.Event):
            self._abort_event = abort_event
            self._raised = False

        def __iter__(self):
            return self

        def __next__(self):
            if self._raised:
                raise StopIteration
            assert self._abort_event.wait(timeout=5.0), "watchdog never aborted request A"
            self._raised = True
            # Outlive interruptible_api_call's fixed 2s join so the outer layer
            # abandons this worker before the inner retry path wakes up.
            time.sleep(2.05)
            raise httpx.ReadTimeout("request A transport aborted")

        def close(self):
            self._abort_event.set()

    class _A2RetryStream:
        def __init__(self):
            self._step = 0

        def __iter__(self):
            return self

        def __next__(self):
            self._step += 1
            if self._step == 1:
                return _event("response.output_text.delta", delta="A2")
            if self._step == 2:
                return _event(
                    "response.completed",
                    response=SimpleNamespace(
                        id="resp_a2", status="completed", output=[], usage=None
                    ),
                )
            a_retry_finished.set()
            raise StopIteration

        def close(self):
            a_retry_finished.set()

    class _ReplacementStream:
        def __init__(self):
            self._step = 0

        def __iter__(self):
            return self

        def __next__(self):
            self._step += 1
            if self._step == 1:
                return _event("response.output_text.delta", delta="B1")
            if self._step == 2:
                # Give the abandoned A worker a small window to attempt the
                # buggy inner retry. Under the fix no retry ever starts, so we
                # simply continue to B's tail delta and terminal event.
                a_retry_started.wait(timeout=0.75)
                return _event("response.output_text.delta", delta="B2")
            if self._step == 3:
                return _event(
                    "response.completed",
                    response=SimpleNamespace(
                        id="resp_b", status="completed", output=[], usage=None
                    ),
                )
            raise StopIteration

        def close(self):
            return None

    class _FakeRequestClient:
        def __init__(self, logical_name: str):
            self.logical_name = logical_name
            self.abort_event = threading.Event()
            self._attempt = 0
            self.responses = SimpleNamespace(create=self._create)

        def _create(self, **_kwargs):
            self._attempt += 1
            if self.logical_name == "A":
                if self._attempt == 1:
                    return _AbortThenTimeoutStream(self.abort_event)
                if self._attempt == 2:
                    a_retry_started.set()
                    return _A2RetryStream()
                raise AssertionError(f"unexpected A attempt {self._attempt}")
            if self.logical_name == "B":
                if self._attempt == 1:
                    return _ReplacementStream()
                raise AssertionError(f"unexpected B attempt {self._attempt}")
            raise AssertionError(f"unknown logical request {self.logical_name}")

    client_order = iter(["A", "B"])

    monkeypatch.setattr(
        agent,
        "_create_request_openai_client",
        lambda **_kwargs: _FakeRequestClient(next(client_order)),
    )
    monkeypatch.setattr(
        agent,
        "_abort_request_openai_client",
        lambda client, reason=None: client.abort_event.set(),
    )
    monkeypatch.setattr(agent, "_close_request_openai_client", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_buffer_status", lambda *a, **k: None)
    monkeypatch.setattr(agent, "_emit_status", lambda *a, **k: None)

    with pytest.raises(TimeoutError):
        h.interruptible_api_call(agent, {"model": "gpt-5.5", "input": "request A"})

    replacement = h.interruptible_api_call(
        agent, {"model": "gpt-5.5", "input": "request B"}
    )
    assert replacement is not None

    if a_retry_started.is_set():
        assert a_retry_finished.wait(timeout=2.0), "abandoned retry never finished"

    assert not a_retry_started.is_set(), "abandoned request A retried after watchdog timeout"
    assert delivered == ["B1", "B2"]
    assert replacement.output_text == "B1B2"
    assert replacement.id == "resp_b"
