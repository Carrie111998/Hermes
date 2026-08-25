"""Mid-stream repetition-degeneration guard (#94224).

Issue: on very large contexts a model can enter a degenerate repetition
loop (e.g. echoing ``！！！`` forever). Before this fix nothing watched the
LIVE stream — the loop ran for minutes, flooded the consumer with deltas,
and the poisoned draft was stitched into the conversation as if it were a
real answer. The #86581 guard only ran AFTER a ``finish_reason=length``
truncation, so a loop that never hits the cap was invisible to it.

The fix reuses :func:`agent.repetition_guard.is_repetition_dominated`
(ONE detector, two call sites) on the accumulated visible stream text,
cheaply: only every ``_REPETITION_GUARD_CHECK_INTERVAL`` chars of growth.
On trip, the live stream attempt is cancelled and ``StreamDegenerationError``
propagates to the conversation loop, which discards the aborted draft
(keeping strict role alternation intact) and shows the same user-facing
notice shape as the post-truncation repetition guard.

Behavior contracts pinned here:
1. A degenerate stream is aborted BEFORE the provider iterator is drained.
2. The conversation history never receives the aborted draft.
3. Healthy long streams never trip the guard.
4. Tool-call streams never trip the guard (action turns are exempt).
5. A raising heuristic fails open — the stream completes untouched.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest


# ── Chunk factories (mirror tests/run_agent/test_streaming.py) ──────────


def _make_stream_chunk(
    content=None, tool_calls=None, finish_reason=None,
    model=None, reasoning_content=None, usage=None,
):
    delta = SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        reasoning_content=reasoning_content,
        reasoning=None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=model, usage=usage)


def _make_tool_call_delta(index=0, tc_id=None, name=None, arguments=None):
    return SimpleNamespace(
        index=index,
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )


def _degenerate_text(chars=1600):
    """The #94224 incident shape: one short fragment echoed far past the
    heuristic's MIN_FRAGMENT_LENGTH (400) with 60+-char windows repeated
    5+ times covering half the fragment."""
    return "！！！" * (chars // 3 + 1)


def _healthy_prose(target_chars=2200):
    """Varied sentences — no 60-char window repeats anywhere near the
    dominance ratio."""
    sentences = [
        "The parser walks the token stream left to right.",
        "Each node carries a span back to its source range.",
        "Recovery happens at statement boundaries only.",
        "Diagnostics are deduplicated before rendering.",
        "The whole pass stays allocation-light in the hot path.",
    ]
    out = []
    total = 0
    i = 0
    while total < target_chars:
        s = f"{i}. {sentences[i % len(sentences)]}\n"
        out.append(s)
        total += len(s)
        i += 1
    return "".join(out)


def _tracking_stream(chunks):
    """Iterator wrapper recording delivery order so tests can assert the
    stream was aborted BEFORE being fully drained."""
    consumed = []

    def _gen():
        for c in chunks:
            consumed.append(c)
            yield c

    return _gen(), consumed


def _make_stream_agent():
    """AIAgent wired for the chat_completions streaming path with a mocked
    provider client (mirrors tests/run_agent/test_streaming.py)."""
    from run_agent import AIAgent

    agent = AIAgent(
        api_key="test-key",
        base_url="https://openrouter.ai/api/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


@contextmanager
def _staged_stream_client(gen):
    """Stage a mocked provider client returning ``gen`` as the stream."""
    mock_client = MagicMock()
    mock_client.chat.completions.create.return_value = gen
    with (
        patch(
            "run_agent.AIAgent._create_request_openai_client",
            return_value=mock_client,
        ),
        patch("run_agent.AIAgent._close_request_openai_client"),
    ):
        yield mock_client


# ── 1. Mid-stream abort on the streaming helper ──────────────────────────


class TestMidStreamAbort:
    def test_degenerate_stream_aborts_before_iterator_ends(self):
        """A '！！！' echo loop must raise StreamDegenerationError while the
        provider iterator still has unread chunks left."""
        from agent.repetition_guard import StreamDegenerationError

        echo = "！！！" * 12  # 36 chars/chunk → trips within ~14 chunks
        filler = "Intro paragraph explaining the answer.\n\n"
        chunks = [_make_stream_chunk(content=filler)]
        chunks += [_make_stream_chunk(content=echo) for _ in range(80)]
        chunks.append(_make_stream_chunk(finish_reason="stop", model="m"))
        gen, consumed = _tracking_stream(chunks)

        agent = _make_stream_agent()
        with _staged_stream_client(gen):
            with pytest.raises(StreamDegenerationError):
                agent._interruptible_streaming_api_call({})

        assert len(consumed) < len(chunks), (
            "The guard must abort the LIVE stream — chunks after the "
            "detection point were still consumed, meaning the iterator "
            "was drained to completion."
        )

    def test_abort_happens_mid_stream_not_after_final_chunk(self):
        """Detection point must precede the finish_reason chunk."""
        from agent.repetition_guard import StreamDegenerationError

        echo = "！！！" * 12
        chunks = [_make_stream_chunk(content=echo) for _ in range(80)]
        final = _make_stream_chunk(finish_reason="stop", model="m")
        chunks.append(final)
        gen, consumed = _tracking_stream(chunks)

        agent = _make_stream_agent()
        with _staged_stream_client(gen):
            with pytest.raises(StreamDegenerationError):
                agent._interruptible_streaming_api_call({})

        assert final not in consumed

    def test_short_repeated_text_is_fail_open(self):
        """Below MIN_FRAGMENT_LENGTH chars the guard must never fire — a
        short stuttering reply is not evidence of degeneration."""
        chunks = [
            _make_stream_chunk(content="！！！" * 20),  # 60 chars < 400
            _make_stream_chunk(content="Done.", finish_reason="stop", model="m"),
        ]
        agent = _make_stream_agent()
        with _staged_stream_client(iter(chunks)):
            response = agent._interruptible_streaming_api_call({})
        assert response.choices[0].finish_reason == "stop"
        assert "Done." in response.choices[0].message.content


# ── 2. Exemptions: healthy streams and tool-call streams ────────────────


class TestGuardExemptions:
    def test_long_healthy_stream_never_trips(self):
        """Varied prose well past the check interval completes normally."""
        chunks = [
            _make_stream_chunk(content=_healthy_prose()),
            _make_stream_chunk(content=" All done.", finish_reason="stop", model="m"),
        ]
        agent = _make_stream_agent()
        with _staged_stream_client(iter(chunks)):
            response = agent._interruptible_streaming_api_call({})
        assert response.choices[0].finish_reason == "stop"
        assert "All done." in response.choices[0].message.content

    def test_tool_call_stream_with_looking_narration_never_trips(self):
        """Once a tool call is in flight the stream is an action turn: even
        repetitive-looking suppressed narration must not abort it."""
        echo = _degenerate_text()
        chunks = [
            _make_stream_chunk(content="Reading the file now."),
            _make_stream_chunk(
                tool_calls=[
                    _make_tool_call_delta(
                        index=0, tc_id="call_1",
                        name="terminal", arguments='{"command": "ls"}',
                    )
                ]
            ),
        ]
        chunks += [_make_stream_chunk(content=echo) for _ in range(40)]
        chunks.append(_make_stream_chunk(finish_reason="tool_calls", model="m"))
        agent = _make_stream_agent()
        with _staged_stream_client(iter(chunks)):
            response = agent._interruptible_streaming_api_call({})
        assert response.choices[0].finish_reason == "tool_calls"
        assert response.choices[0].message.tool_calls[0].function.name == "terminal"

    def test_short_preamble_then_tool_call_never_trips(self):
        """Ordinary chatty preambles are far below the fragment floor."""
        chunks = [
            _make_stream_chunk(content=f"Step {i}: checking. " * 4)
            for i in range(8)
        ]
        chunks += [
            _make_stream_chunk(
                tool_calls=[
                    _make_tool_call_delta(
                        index=0, tc_id="call_1", name="files",
                        arguments='{"action": "list"}',
                    )
                ]
            ),
            _make_stream_chunk(finish_reason="tool_calls", model="m"),
        ]
        agent = _make_stream_agent()
        with _staged_stream_client(iter(chunks)):
            response = agent._interruptible_streaming_api_call({})
        assert response.choices[0].finish_reason == "tool_calls"


# ── 3. Fail-open on heuristic errors ────────────────────────────────────


class TestHeuristicFailOpen:
    def test_raising_heuristic_never_aborts_the_stream(self):
        """A bug inside is_repetition_dominated must degrade to 'keep the
        stream', never kill a possibly-healthy response."""
        chunks = [
            _make_stream_chunk(content=_degenerate_text()),
            _make_stream_chunk(content="tail", finish_reason="stop", model="m"),
        ]
        agent = _make_stream_agent()
        with (
            _staged_stream_client(iter(chunks)),
            patch(
                "agent.chat_completion_helpers.is_repetition_dominated",
                side_effect=RuntimeError("heuristic bug"),
            ),
        ):
            response = agent._interruptible_streaming_api_call({})
        assert response.choices[0].finish_reason == "stop"
        assert response.choices[0].message.content.endswith("tail")

    def test_non_string_accumulation_cannot_raise(self):
        """Non-str deltas are filtered by the heuristic's own guard; the
        stream completes."""
        weird = SimpleNamespace(content=None, tool_calls=None,
                                reasoning_content=None, reasoning=None)
        chunks = [
            SimpleNamespace(
                choices=[SimpleNamespace(index=0, delta=weird,
                                         finish_reason=None)],
                model="m", usage=None,
            ),
            _make_stream_chunk(content="ok", finish_reason="stop", model="m"),
        ]
        agent = _make_stream_agent()
        with _staged_stream_client(iter(chunks)):
            response = agent._interruptible_streaming_api_call({})
        assert response.choices[0].message.content == "ok"


# ── 4. Conversation-loop integration: draft kept out of history ─────────


@pytest.fixture()
def loop_agent():
    """AIAgent for run_conversation with a mocked provider client staged on
    .chat.completions.create (mirrors test_dropped_tool_call_recovery).
    agent.client must NOT be a Mock instance, otherwise the loop disables
    streaming and the mid-stream guard never engages."""
    from run_agent import AIAgent
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        agent = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent.client = SimpleNamespace()  # not a Mock → streaming allowed
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False
        return agent


class TestConversationLoopDiscardsDraft:
    def test_degenerate_turn_aborts_and_keeps_draft_out_of_history(
        self, loop_agent
    ):
        """End to end: the poisoned draft must never become an assistant
        message; the user-facing notice explains the abort; role
        alternation is preserved (history still ends on the user message);
        no continuation retry burns budget."""
        echo = "！！！" * 12
        chunks = [_make_stream_chunk(content="Sure, here is the answer.\n\n")]
        chunks += [_make_stream_chunk(content=echo) for _ in range(80)]
        chunks.append(_make_stream_chunk(finish_reason="stop", model="m"))
        gen, consumed = _tracking_stream(chunks)

        seen_deltas = []
        loop_agent.stream_delta_callback = seen_deltas.append

        with (
            _staged_stream_client(gen) as mock_client,
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("write me a haiku")

        # Deltas really streamed before the abort (this was a LIVE stream).
        assert any(echo.strip() in d for d in seen_deltas)

        # Abort surfaced as StreamDegenerationError, not a stub/retry.
        assert len(consumed) < len(chunks)
        assert mock_client.chat.completions.create.call_count == 1, (
            "No continuation/retry may follow a degeneration abort."
        )

        # User-facing notice mirrors the #86581 post-truncation guard tone.
        assert "Repetition Detected" in result["final_response"]
        assert result["completed"] is False
        assert result.get("partial") is True

        # THE core contract: the aborted draft is nowhere in history and
        # strict role alternation holds (turn still ends on the user msg).
        messages = result["messages"]
        for m in messages:
            if m.get("role") == "assistant":
                assert "！！！" not in (m.get("content") or ""), (
                    "The degenerate draft leaked into the conversation "
                    "history — it must be discarded entirely."
                )
        assert messages[-1]["role"] == "user"

    def test_healthy_streamed_turn_still_completes_normally(self, loop_agent):
        """Control: a healthy streamed turn is untouched by the guard."""
        chunks = [
            _make_stream_chunk(content=_healthy_prose()),
            _make_stream_chunk(
                content=" Hope that helps!", finish_reason="stop", model="m"
            ),
        ]
        with (
            _staged_stream_client(iter(chunks)),
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("hello")

        assert result["completed"] is True
        assert "Hope that helps!" in result["final_response"]

        assistant = [
            m for m in result["messages"] if m.get("role") == "assistant"
        ]
        assert assistant, "Healthy response must land in history"
