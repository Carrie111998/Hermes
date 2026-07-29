"""Tests for content-aware stale-stream detection (#73872).

The stale-stream detector kills connections that receive SSE keep-alive
frames but never deliver real content.  Prior to the fix, the timer was
reset on *every* chunk (including heartbeats), defeating the detector and
hanging the agent indefinitely.  These tests verify that only content-
bearing chunks refresh the timer.

The first class unit-tests the production helper
``_stream_chunk_carries_content`` (a real production function).  The second
class drives the *actual* streaming loop in ``interruptible_streaming_api_call``
with a heartbeat-only provider stream and asserts the stale watchdog fires —
this is the genuine end-to-end regression: on ``upstream/main`` the heartbeats
keep resetting the timer so the watchdog never fires and the assertion fails.
"""
from __future__ import annotations

import sys
import time
import types
from types import SimpleNamespace

import pytest

# Stub optional heavy imports so run_agent imports cleanly in isolation.
sys.modules.setdefault("fire", types.SimpleNamespace(Fire=lambda *a, **k: None))
sys.modules.setdefault("firecrawl", types.SimpleNamespace(Firecrawl=object))
sys.modules.setdefault("fal_client", types.SimpleNamespace())

from agent.chat_completion_helpers import _stream_chunk_carries_content


# ──────────────────────────────────────────────────────────────────────────
# Unit tests for the production helper _stream_chunk_carries_content.
# These exercise real production code and document its contract.
# ──────────────────────────────────────────────────────────────────────────
class TestStreamChunkCarriesContent:
    """Verify _stream_chunk_carries_content distinguishes heartbeats from content."""

    def test_text_delta_is_content(self):
        """A chunk with choices containing text content should be flagged as content."""
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content="hello"))]
        )
        assert _stream_chunk_carries_content(chunk) is True

    def test_tool_call_delta_is_content(self):
        """A chunk carrying tool-call deltas represents real content."""
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None, tool_calls=[SimpleNamespace(id="1")])
            )]
        )
        assert _stream_chunk_carries_content(chunk) is True

    def test_reasoning_delta_is_content(self):
        """Reasoning content is real model output."""
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(
                delta=SimpleNamespace(content=None, reasoning_content="thinking...")
            )]
        )
        assert _stream_chunk_carries_content(chunk) is True

    def test_finish_reason_chunk_is_content(self):
        """A chunk with a finish_reason is a valid stream terminator, not a heartbeat."""
        chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=None, finish_reason="stop")]
        )
        assert _stream_chunk_carries_content(chunk) is True

    def test_heartbeat_no_choices_is_not_content(self):
        """An SSE heartbeat frame (data: {}) has no choices — must not refresh timer."""
        chunk = SimpleNamespace(choices=[])
        assert _stream_chunk_carries_content(chunk) is False

    def test_heartbeat_missing_choices_attr(self):
        """Some providers omit the choices key entirely on heartbeats."""
        chunk = SimpleNamespace()
        assert _stream_chunk_carries_content(chunk) is False

    def test_usage_only_chunk_is_not_content(self):
        """The final usage chunk has no choices — it carries metadata, not content."""
        chunk = SimpleNamespace(
            choices=[],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
        assert _stream_chunk_carries_content(chunk) is False


# ──────────────────────────────────────────────────────────────────────────
# End-to-end integration tests that drive the REAL streaming loop.
#
# These exercise ``interruptible_streaming_api_call`` (not a re-computation of
# the fix inside the test body) with a fake OpenAI-compatible provider stream.
# They prove the production contract: heartbeat-only streams become stale and
# are killed; content streams are not.
# ──────────────────────────────────────────────────────────────────────────
def _make_streaming_agent(tmp_path, monkeypatch, *, stale_timeout=2):
    """A real AIAgent wired for the OpenAI chat-completions streaming path."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_STREAM_STALE_TIMEOUT", str(stale_timeout))
    (tmp_path / ".env").write_text("", encoding="utf-8")
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")
    from run_agent import AIAgent

    agent = AIAgent(
        model="gpt-4o-mini",
        provider="openai",
        api_key="sk-dummy",
        base_url="https://api.openai.com/v1",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        platform="cli",
    )
    # Force the chat-completions path (not anthropic / codex).
    agent.api_mode = "chat_completions"
    # Silence status/display side effects.
    for attr in ("_emit_status", "_buffer_status", "_emit_wait_notice"):
        if hasattr(agent, attr):
            monkeypatch.setattr(agent, attr, lambda *a, **k: None)
    return agent


class _FakeCompletions:
    """A fake ``client.chat.completions`` whose ``create(stream=True)`` yields a
    caller-supplied list of chunks (already spaced in time by the caller)."""

    def __init__(self, chunks):
        self._chunks = chunks

    def create(self, **kwargs):
        return iter(self._chunks)


class TestStaleWatchdogHeartbeatIntegration:
    """Drive the real streaming loop to verify the stale watchdog is content-aware."""

    def test_heartbeat_only_stream_triggers_stale_kill(self, tmp_path, monkeypatch):
        """A provider that sends only heartbeat frames (no choices) must let the
        stale-stream watchdog fire and kill the connection.

        Regression for #73872: before the fix, heartbeats reset
        ``last_chunk_time`` on every frame, so the watchdog never fired and the
        agent hung indefinitely.
        """
        from agent import chat_completion_helpers as h

        agent = _make_streaming_agent(tmp_path, monkeypatch, stale_timeout=2)

        closes: list = []
        dummy_client = SimpleNamespace(
            chat=SimpleNamespace(completions=_FakeCompletions([]))
        )
        monkeypatch.setattr(
            agent, "_create_request_openai_client", lambda **k: dummy_client
        )
        monkeypatch.setattr(
            agent, "_abort_request_openai_client",
            lambda c, reason=None: closes.append(reason),
        )
        monkeypatch.setattr(
            agent, "_close_request_openai_client",
            lambda c, reason=None: closes.append(reason),
        )

        # Heartbeat-only stream: empty-choices frames spaced 0.3s apart.  We
        # yield enough frames to outlast the 2s stale timeout, then end.
        def _heartbeat_stream():
            for _ in range(9):
                time.sleep(0.3)
                yield SimpleNamespace(choices=[])  # pure heartbeat

        dummy_client.chat.completions.create = lambda **kwargs: _heartbeat_stream()

        t0 = time.time()
        # A heartbeat-only provider can never deliver content, so after the
        # stale watchdog kills each attempt the streaming function exhausts its
        # retries and raises.  What we assert is that the stale kill actually
        # *fired* (the regression: before the fix, heartbeats reset the timer
        # so the watchdog never fired and the agent hung for 50+ minutes
        # instead of reconnecting).
        saw_stale_kill = False
        try:
            h.interruptible_streaming_api_call(
                agent,
                {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
            )
        except Exception:
            # Retries exhausted — expected for a provider that never sends
            # content.  The real symptom (#73872) was hanging, not raising.
            pass
        elapsed = time.time() - t0

        assert "stale_stream_kill" in closes, (
            "Heartbeat-only stream should have been killed by the stale watchdog "
            f"(closes={closes}, elapsed={elapsed:.1f}s)"
        )
        # The watchdog reconnects quickly rather than hanging for minutes.
        assert elapsed < 30, f"Stale reconnect took {elapsed:.1f}s (should be fast)"

    def test_content_stream_is_not_killed_by_stale_watchdog(self, tmp_path, monkeypatch):
        """The inverse: a stream that delivers real content (choices) must keep
        the timer fresh so the watchdog does NOT fire.  This guards against the
        fix being too aggressive (killing healthy streams)."""
        from agent import chat_completion_helpers as h

        agent = _make_streaming_agent(tmp_path, monkeypatch, stale_timeout=2)

        closes: list = []
        dummy_client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace())
        )
        monkeypatch.setattr(
            agent, "_create_request_openai_client", lambda **k: dummy_client
        )
        monkeypatch.setattr(
            agent, "_abort_request_openai_client",
            lambda c, reason=None: closes.append(reason),
        )
        monkeypatch.setattr(
            agent, "_close_request_openai_client",
            lambda c, reason=None: closes.append(reason),
        )

        # Content stream: real delta chunks spaced 0.3s apart for ~3s (> stale
        # window).  Because each carries content, the timer stays fresh.
        def _content_stream():
            for i in range(10):
                time.sleep(0.3)
                yield SimpleNamespace(
                    choices=[SimpleNamespace(
                        delta=SimpleNamespace(content=f"word{i}"), finish_reason=None
                    )]
                )
            # Terminator chunk (still content-bearing).
            yield SimpleNamespace(
                choices=[SimpleNamespace(delta=None, finish_reason="stop")]
            )

        dummy_client.chat.completions.create = lambda **kw: _content_stream()

        h.interruptible_streaming_api_call(
            agent, {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}
        )

        assert "stale_stream_kill" not in closes, (
            f"Content stream should NOT have been killed (closes={closes})"
        )
