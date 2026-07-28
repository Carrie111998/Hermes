"""Regression tests for issue #30963 — partial-stream stub finish_reason.

Pins the contract:

- any partial stream → stub.finish_reason == "length", but the
  conversation loop recognizes the stub id and returns the delivered body
  without a continuation request.
- partial mid-tool-call → tool_calls stays None and the loop reports the
  dropped action without executing or retrying it.
- conversation_loop's length-continuation prompt distinguishes a real
  output-length truncation from a partial-stream-stub network error
  via response.id.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_constants import PARTIAL_STREAM_STUB_ID, FINISH_REASON_LENGTH
from agent.conversation_loop import _get_continuation_prompt


# ── Helpers (mirrors test_streaming.py) ────────────────────────────────────

def _make_stream_chunk(content=None, tool_calls=None, finish_reason=None):
    delta = SimpleNamespace(
        content=content, tool_calls=tool_calls,
        reasoning_content=None, reasoning=None,
    )
    choice = SimpleNamespace(index=0, delta=delta, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice], model=None, usage=None)


def _make_tool_call_delta(index=0, tc_id=None, name=None, arguments=None):
    func = SimpleNamespace(name=name, arguments=arguments)
    return SimpleNamespace(index=index, id=tc_id, function=func)


def _make_agent():
    from run_agent import AIAgent
    agent = AIAgent(
        api_key="test-key",
        base_url="https://example.com/v1",
        model="test/model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.api_mode = "chat_completions"
    agent._interrupt_requested = False
    return agent


# ── Stub finish_reason ────────────────────────────────────────────────────

class TestPartialStreamStubFinishReason:
    """The stub returned by interruptible_streaming_api_call when the
    upstream connection dies mid-flight."""

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_text_only_partial_returns_length(self, _mock_close, mock_create, monkeypatch):
        """The low-level stub keeps its historical length classification;
        the loop handles its id as an abnormal partial terminal result."""

        def _stalling_stream():
            yield _make_stream_chunk(content="Here's my answer so far")
            raise RuntimeError("simulated upstream stall")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = lambda *a, **kw: _stalling_stream()
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._current_streamed_assistant_text = "Here's my answer so far"

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH, (
            "Text-only partial streams retain finish_reason=length while the "
            "conversation loop returns the recovered body once (issue #30963)."
        )
        assert response.choices[0].message.content == "Here's my answer so far"
        assert response.choices[0].message.tool_calls is None

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_partial_tool_call_uses_length(self, _mock_close, mock_create, monkeypatch):
        """Mid-tool-call partials retain finish_reason=length for diagnostics;
        tool_calls=None prevents auto-execution or a duplicate provider call."""

        def _stalling_stream():
            yield _make_stream_chunk(content="Let me write the audit: ")
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_1", name="write_file"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments='{"path": "/tmp/x", '),
            ])
            raise RuntimeError("simulated upstream stall")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = lambda *a, **kw: _stalling_stream()
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None
        agent._current_streamed_assistant_text = "Let me write the audit: "

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH, (
            "Partial mid-tool-call must retain finish_reason=length for the "
            "terminal warning path (#31998)."
        )
        assert response.choices[0].message.tool_calls is None, (
            "tool_calls must remain None (no auto-execution of side-effectful "
            "tool calls)."
        )
        # The stub should carry dropped tool names for continuation prompt
        assert getattr(response, "_dropped_tool_names", None) == ["write_file"]
        content = response.choices[0].message.content or ""
        assert "Stream stalled mid tool-call" in content
        assert "write_file" in content


# ── Clean stream-end mid-tool-call (no exception, no finish_reason) ─────────

class TestCleanStreamEndMidToolCall:
    """The upstream closes the SSE stream cleanly after delivering a tool
    name + the opening '{' of its arguments — NO exception, NO finish_reason,
    NO [DONE].  Observed live on NVIDIA Nemotron Ultra via the Nous dedicated
    endpoint: it stalls/drops during large tool-arg generation.

    The mock-builder must NOT stamp this as finish_reason='length' (which
    routes it through the max_tokens-boost truncation path and finally
    reports the misleading 'Response truncated due to output length limit').
    It must route through the partial-stream-stub path so the loop reports
    an honest mid-tool-call drop and asks the model to chunk its output.
    """

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_no_finish_reason_partial_tool_args_routes_to_stub(
        self, _mock_close, mock_create, monkeypatch,
    ):
        def _clean_ending_stream():
            # Reasoning + tool name + the lone opening brace, then the
            # generator simply RETURNS (StopIteration) — no raise, no
            # finish_reason chunk, no [DONE].
            yield _make_stream_chunk(content="\n")
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_x", name="execute_code"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments="{"),
            ])
            # falls off the end — clean close, no terminator

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _clean_ending_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None

        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID, (
            "A clean stream-end mid tool-call (no finish_reason) must be "
            "tagged as a partial-stream stub, not a 'stream-<uuid>' "
            "truncation — otherwise the loop reports the false 'output "
            "length limit' error."
        )
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH
        assert response.choices[0].message.tool_calls is None, (
            "Incomplete tool args must never auto-execute."
        )
        assert getattr(response, "_dropped_tool_names", None) == ["execute_code"]

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_real_length_truncation_still_uses_uuid_id(
        self, _mock_close, mock_create, monkeypatch,
    ):
        """Control: when the provider DOES send finish_reason='length' with
        partial tool args, it is a genuine output cap — keep the existing
        non-stub behaviour (boost max_tokens and retry)."""

        def _capped_stream():
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_y", name="execute_code"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments="{"),
            ])
            # Provider explicitly reports the output cap.
            yield _make_stream_chunk(finish_reason="length")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _capped_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None

        response = agent._interruptible_streaming_api_call({})

        assert response.id != PARTIAL_STREAM_STUB_ID, (
            "A provider-reported finish_reason='length' is a real output cap "
            "and must keep the existing truncation path, not the stream-drop "
            "stub path."
        )
        assert response.id.startswith("stream-")
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_no_finish_reason_text_only_routes_to_stub(
        self, _mock_close, mock_create, monkeypatch,
    ):
        """A clean stream-end with no finish_reason after text-only
        delivery must route through the partial-stream-stub path so the
        conversation loop continues instead of silently accepting
        truncated text as a complete response (#32086)."""

        def _clean_ending_stream():
            yield _make_stream_chunk(content="Let me compare the ")
            yield _make_stream_chunk(content="vision configs:")
            # falls off the end — clean close, no terminator

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _clean_ending_stream()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None

        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID, (
            "A clean stream-end with no finish_reason after text-only "
            "delivery must be tagged as a partial-stream stub, not "
            "silently accepted as complete (#32086)."
        )
        assert response.choices[0].finish_reason == FINISH_REASON_LENGTH
        assert response.choices[0].message.content == "Let me compare the vision configs:"
        assert response.choices[0].message.tool_calls is None
        assert getattr(response, "_dropped_tool_names", None) is None, (
            "Text-only drops must not carry dropped tool names — there "
            "were no tool calls in flight."
        )


# ── Length-continuation prompt branching ──────────────────────────────────

class TestLengthContinuationPromptBranching:
    """When finish_reason=length, the continuation prompt that reaches the
    model has to tell the truth: real truncation vs. network interruption
    vs. dropped tool call (#31998).  Three distinct prompts now exist."""

    def _simulate_branch(self, response_id: str, dropped_tools=None) -> str:
        """Return the continuation prompt text the loop would inject for
        a `finish_reason=length` response with the given id."""
        is_partial = response_id == PARTIAL_STREAM_STUB_ID
        return _get_continuation_prompt(is_partial, dropped_tools)

    def test_partial_stream_stub_uses_network_prompt(self):
        prompt = self._simulate_branch(PARTIAL_STREAM_STUB_ID)
        assert "network error mid-stream" in prompt
        assert "output length limit" not in prompt

    def test_real_truncation_uses_length_prompt(self):
        prompt = self._simulate_branch("chatcmpl-abc123")
        assert "output length limit" in prompt
        assert "network error" not in prompt

    def test_no_id_falls_through_to_length_prompt(self):
        prompt = self._simulate_branch("")
        assert "output length limit" in prompt

    def test_dropped_tool_call_uses_chunking_prompt(self):
        """When the stub dropped a tool call, the continuation prompt
        must guide the model to break its output into smaller chunks
        instead of retrying the same large tool call (#31998)."""
        prompt = self._simulate_branch(
            PARTIAL_STREAM_STUB_ID, dropped_tools=["write_file"],
        )
        assert "too large" in prompt
        assert "break" in prompt.lower()
        assert "write_file" in prompt
        assert "network error" not in prompt
        assert "output length limit" not in prompt


# ── Integration: live conversation loop ───────────────────────────────────

@pytest.fixture()
def loop_agent():
    """AIAgent with a mocked OpenAI client (mirrors test_run_agent's fixture)
    so we can stage a stub + continuation pair on .chat.completions.create."""
    from run_agent import AIAgent
    with (
        patch("run_agent.get_tool_definitions", return_value=[]),
        patch("run_agent.check_toolset_requirements", return_value={}),
        patch("run_agent.OpenAI"),
    ):
        a = AIAgent(
            api_key="test-key-1234567890",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        a.client = MagicMock()
        a._cached_system_prompt = "You are helpful."
        a._use_prompt_caching = False
        a.tool_delay = 0
        a.compression_enabled = False
        a.save_trajectories = False
        return a


class TestConversationLoopPartialStreamRecovery:
    """End-to-end recovery without duplicate model calls."""

    def test_text_partial_stream_returns_body_without_continuation(self, loop_agent):
        """A text-only stub returns its delivered body after one API call."""

        from tests.run_agent.test_run_agent import _mock_assistant_msg

        partial_stub = SimpleNamespace(
            id=PARTIAL_STREAM_STUB_ID,
            model="test/model",
            choices=[SimpleNamespace(
                index=0,
                message=_mock_assistant_msg(content="The first half of "),
                finish_reason=FINISH_REASON_LENGTH,
            )],
            usage=None,
        )
        loop_agent.client.chat.completions.create.return_value = partial_stub
        persisted_messages = []

        with (
            patch.object(
                loop_agent,
                "_persist_session",
                side_effect=lambda messages, _history: persisted_messages.append(
                    [dict(message) for message in messages]
                ),
            ),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources") as cleanup,
        ):
            result = loop_agent.run_conversation("ask me something")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert result["final_response"] == "The first half of"
        assert result["completed"] is False
        assert result["partial"] is True
        assert result["failed"] is False
        assert (
            result["error"]
            == "network_stream_interrupted_after_partial_response"
        )
        assert result["messages"][-1]["role"] == "assistant"
        assert result["messages"][-1]["finish_reason"] == FINISH_REASON_LENGTH
        assert [
            message.get("content")
            for message in result["messages"]
            if message.get("role") == "user"
        ] == ["ask me something"]
        assert sum(
            sum(
                message.get("role") == "assistant"
                and message.get("content") == "The first half of"
                for message in snapshot
            )
            for snapshot in persisted_messages
        ) == 1
        cleanup.assert_called_once()

    def test_incomplete_chunked_read_runs_transport_to_loop_once(
        self, loop_agent, monkeypatch
    ):
        """A real transport read failure preserves its body without call two."""
        import httpx

        attempts = {"count": 0}

        def _stream():
            yield _make_stream_chunk(content="Recovered before disconnect")
            raise httpx.ReadError("incomplete chunked read")

        request_client = MagicMock()

        def _create(*_args, **_kwargs):
            attempts["count"] += 1
            return _stream()

        request_client.chat.completions.create.side_effect = _create
        loop_agent.stream_delta_callback = lambda _text: None
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")

        with (
            patch.object(
                loop_agent,
                "_create_request_openai_client",
                return_value=request_client,
            ),
            patch.object(loop_agent, "_close_request_openai_client"),
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("keep the partial")

        assert attempts["count"] == 1
        assert result["final_response"] == "Recovered before disconnect"
        assert result["partial"] is True
        assert result["messages"][-1]["finish_reason"] == FINISH_REASON_LENGTH
        assert result["messages"][-1]["content"] == "Recovered before disconnect"

    def test_anthropic_read_error_runs_transport_to_loop_once(self, monkeypatch):
        """The Anthropic compatibility stub bypasses native-message validation
        and reaches the terminal partial path without an outer retry."""
        import httpx

        from run_agent import AIAgent

        attempts = {"count": 0}

        class _BrokenStream:
            response = None

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def __iter__(self):
                yield SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(type="text_delta", text="partial body"),
                )
                raise httpx.ReadError("incomplete chunked read")

        request_client = MagicMock()

        def _make_stream(**_kwargs):
            attempts["count"] += 1
            return _BrokenStream()

        request_client.messages.stream.side_effect = _make_stream
        monkeypatch.setenv("HERMES_STREAM_RETRIES", "2")

        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            agent = AIAgent(
                api_key="test-key",
                base_url="https://api.anthropic.com",
                provider="anthropic",
                api_mode="chat_completions",
                model="claude-test",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )

        agent.api_mode = "anthropic_messages"
        agent._interrupt_requested = False
        agent.stream_delta_callback = lambda _text: None
        agent._cached_system_prompt = "You are helpful."
        agent._use_prompt_caching = False
        agent.tool_delay = 0
        agent.compression_enabled = False
        agent.save_trajectories = False

        with (
            patch.object(
                agent,
                "_create_request_anthropic_client",
                return_value=request_client,
            ),
            patch.object(agent, "_persist_session"),
            patch.object(agent, "_save_trajectory"),
            patch.object(agent, "_cleanup_task_resources"),
        ):
            result = agent.run_conversation("keep the partial")

        assert attempts["count"] == 1
        assert result["partial"] is True
        assert result["final_response"] == "partial body"
        assert result["messages"][-1]["finish_reason"] == FINISH_REASON_LENGTH
        assert "tool_calls" not in result["messages"][-1]

    def test_tool_partial_stub_returns_warning_without_continuation(self, loop_agent):
        """A dropped tool call is terminal and can never be auto-executed."""

        from tests.run_agent.test_run_agent import _mock_assistant_msg

        partial_stub = SimpleNamespace(
            id=PARTIAL_STREAM_STUB_ID,
            model="test/model",
            choices=[SimpleNamespace(
                index=0,
                message=_mock_assistant_msg(content="Let me write the file."),
                finish_reason=FINISH_REASON_LENGTH,
            )],
            usage=None,
            _dropped_tool_names=["write_file"],
        )
        loop_agent.client.chat.completions.create.return_value = partial_stub
        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("write it")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert "Stream stalled mid tool-call" in result["final_response"]
        assert result["messages"][-1]["role"] == "assistant"
        assert result["messages"][-1]["finish_reason"] == FINISH_REASON_LENGTH
        assert "tool_calls" not in result["messages"][-1]

    def test_partial_stream_after_real_length_preserves_full_body(self, loop_agent):
        """A dropped continuation retains both parts without call three."""

        from tests.run_agent.test_run_agent import (
            _mock_assistant_msg,
            _mock_response,
        )

        partial_stub = SimpleNamespace(
            id=PARTIAL_STREAM_STUB_ID,
            model="test/model",
            choices=[SimpleNamespace(
                index=0,
                message=_mock_assistant_msg(content="Second half partial."),
                finish_reason=FINISH_REASON_LENGTH,
            )],
            usage=None,
            _dropped_tool_names=["write_file"],
        )
        loop_agent.client.chat.completions.create.side_effect = [
            _mock_response(content="The first half. ", finish_reason="length"),
            partial_stub,
        ]

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
            patch.object(loop_agent, "_execute_tool_calls") as execute_tools,
        ):
            result = loop_agent.run_conversation("ask me something")

        assert loop_agent.client.chat.completions.create.call_count == 2
        assert result["final_response"].startswith(
            "The first half. Second half partial."
        )
        assert "Stream stalled mid tool-call" in result["final_response"]
        assert result["partial"] is True
        assert result["failed"] is False
        assert result["messages"][-2]["role"] == "user"
        assert "output length limit" in result["messages"][-2]["content"]
        assert result["messages"][-1]["role"] == "assistant"
        assert "tool_calls" not in result["messages"][-1]
        execute_tools.assert_not_called()


class TestContentFilterStallReturnsPartial:
    """Regression for #32421: a provider output-layer content safety filter
    (e.g. MiniMax ``output new_sensitive (1027)``) terminates a streaming
    response mid-delivery.  The raw error is swallowed into a
    finish_reason=length partial-stream stub, so before the fix the loop
    burned 3 continuation retries against the SAME primary (re-hitting the
    content-deterministic filter every time) and gave up with
    ``"Response remained truncated after 3 continuation attempts"`` — the
    configured fallback chain was never consulted.

    The fix has three layers:
      1. error_classifier classifies ``new_sensitive`` as
         ``content_policy_blocked``.
      2. interruptible_streaming_api_call runs the swallowed error through
         that classifier and stamps the stub ``_content_filter_terminated``.
      3. the conversation loop returns the tagged partial once without
         continuation or provider fallback.
    """

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_streaming_call_tags_content_filter_stub(
        self, _mock_close, mock_create, monkeypatch,
    ):
        """Layer 2: the real streaming path stamps _content_filter_terminated
        when the swallowed error matches a content-filter pattern."""

        def _minimax_stall():
            yield _make_stream_chunk(content="Writing the file: ")
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, tc_id="call_1", name="write_file"),
            ])
            yield _make_stream_chunk(tool_calls=[
                _make_tool_call_delta(index=0, arguments='{"path": "/tmp/x", '),
            ])
            raise RuntimeError("output new_sensitive (1027) [MiniMax-M2.7]")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _minimax_stall()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None
        agent._current_streamed_assistant_text = "Writing the file: "

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert getattr(response, "_content_filter_terminated", False) is True, (
            "MiniMax new_sensitive stream stall must retain its diagnostic tag "
            "without triggering another provider call (#32421)."
        )

    @patch("run_agent.AIAgent._create_request_openai_client")
    @patch("run_agent.AIAgent._close_request_openai_client")
    def test_plain_network_stall_not_tagged(
        self, _mock_close, mock_create, monkeypatch,
    ):
        """A plain network stall (no content-filter signature) must NOT be
        tagged or switched to another provider."""

        def _network_stall():
            yield _make_stream_chunk(content="Writing the file: ")
            raise RuntimeError("connection reset by peer")

        mock_client = MagicMock()
        mock_client.chat.completions.create.side_effect = (
            lambda *a, **kw: _network_stall()
        )
        mock_create.return_value = mock_client

        agent = _make_agent()
        agent._fire_stream_delta = lambda text: None
        agent._current_streamed_assistant_text = "Writing the file: "

        monkeypatch.setenv("HERMES_STREAM_RETRIES", "0")
        response = agent._interruptible_streaming_api_call({})

        assert response.id == PARTIAL_STREAM_STUB_ID
        assert getattr(response, "_content_filter_terminated", False) is False, (
            "A plain network stall must not be misclassified as a content "
            "filter — that would needlessly switch providers."
        )

    def test_tagged_stub_does_not_activate_fallback(self, loop_agent):
        """Layer 3: a tagged partial is terminal even with a fallback."""
        from tests.run_agent.test_run_agent import _mock_assistant_msg

        def _filter_stub():
            return SimpleNamespace(
                id=PARTIAL_STREAM_STUB_ID,
                model="minimax/MiniMax-M2.7",
                choices=[SimpleNamespace(
                    index=0,
                    message=_mock_assistant_msg(content="Writing the file..."),
                    finish_reason=FINISH_REASON_LENGTH,
                )],
                usage=None,
                _dropped_tool_names=["write_file"],
                _content_filter_terminated=True,
            )

        loop_agent.client.chat.completions.create.return_value = _filter_stub()
        loop_agent._fallback_chain = [
            {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.7"},
        ]
        loop_agent._fallback_index = 0
        fb_calls = {"n": 0}

        def _fake_activate(reason=None):
            fb_calls["n"] += 1
            loop_agent._fallback_index = len(loop_agent._fallback_chain)
            return True

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
            patch.object(loop_agent, "_try_activate_fallback",
                         side_effect=_fake_activate),
        ):
            result = loop_agent.run_conversation("write me a long file")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert fb_calls["n"] == 0
        assert result["partial"] is True
        assert result["completed"] is False
        assert "Stream stalled mid tool-call" in result["final_response"]
        assert result["messages"][-1]["finish_reason"] == FINISH_REASON_LENGTH
        assert "tool_calls" not in result["messages"][-1]

    def test_tagged_stub_without_fallback_is_terminal(self, loop_agent):
        """A tagged partial without a fallback is also terminal."""
        from tests.run_agent.test_run_agent import _mock_assistant_msg

        def _filter_stub():
            return SimpleNamespace(
                id=PARTIAL_STREAM_STUB_ID,
                model="minimax/MiniMax-M2.7",
                choices=[SimpleNamespace(
                    index=0,
                    message=_mock_assistant_msg(content="partial "),
                    finish_reason=FINISH_REASON_LENGTH,
                )],
                usage=None,
                _dropped_tool_names=["write_file"],
                _content_filter_terminated=True,
            )

        loop_agent.client.chat.completions.create.return_value = _filter_stub()
        # No fallback chain configured.
        loop_agent._fallback_chain = []
        loop_agent._fallback_index = 0
        fb_calls = {"n": 0}

        def _fake_activate(reason=None):
            fb_calls["n"] += 1
            return False

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
            patch.object(loop_agent, "_try_activate_fallback",
                         side_effect=_fake_activate),
        ):
            result = loop_agent.run_conversation("write me a long file")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert fb_calls["n"] == 0
        assert result["partial"] is True
        assert result["completed"] is False


class TestPartialStreamStubPersistence:
    """Partial stubs persist one safe assistant result without replay."""

    def test_empty_stub_persists_warning_without_continuation(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_assistant_msg

        empty_stub = SimpleNamespace(
            id=PARTIAL_STREAM_STUB_ID,
            model="test/model",
            choices=[SimpleNamespace(
                index=0,
                message=_mock_assistant_msg(content=""),
                finish_reason=FINISH_REASON_LENGTH,
            )],
            usage=None,
            _dropped_tool_names=["write_file"],
        )
        loop_agent.client.chat.completions.create.return_value = empty_stub

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("make me a webpage")

        assert loop_agent.client.chat.completions.create.call_count == 1
        assert result["partial"] is True
        assert result["completed"] is False
        assert "Stream stalled mid tool-call (write_file)" in result["final_response"]
        assert result["messages"][-1]["role"] == "assistant"
        assert result["messages"][-1]["content"].strip()
        assert [
            message for message in result["messages"][1:]
            if message.get("role") == "user"
        ] == []

    def test_non_empty_partial_stub_persists_body_once(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_assistant_msg

        partial_stub = SimpleNamespace(
            id=PARTIAL_STREAM_STUB_ID,
            model="test/model",
            choices=[SimpleNamespace(
                index=0,
                message=_mock_assistant_msg(content="The first half of "),
                finish_reason=FINISH_REASON_LENGTH,
            )],
            usage=None,
        )
        loop_agent.client.chat.completions.create.return_value = partial_stub

        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation("ask me something")

        assert loop_agent.client.chat.completions.create.call_count == 1
        partial_assistants = [
            m for m in result["messages"]
            if m.get("role") == "assistant" and "first half" in (m.get("content") or "")
        ]
        assert len(partial_assistants) == 1
        assert result["final_response"] == "The first half of"
        assert result["partial"] is True


class TestBuildAssistantMessageEmptyContentPad:
    """Regression layer 2 (chat_completion_helpers.build_assistant_message):
    never serialize a textless assistant turn with ``content: ""`` — pad to
    a single space, the same trick as the reasoning_content pad (#15250).
    Tool-call turns are exempt (``content: ""`` + ``tool_calls`` is accepted
    everywhere)."""

    def _agent_for_builder(self):
        from run_agent import AIAgent
        with (
            patch("run_agent.get_tool_definitions", return_value=[]),
            patch("run_agent.check_toolset_requirements", return_value={}),
            patch("run_agent.OpenAI"),
        ):
            a = AIAgent(
                api_key="test-key-1234567890",
                base_url="https://openrouter.ai/api/v1",
                quiet_mode=True,
                skip_context_files=True,
                skip_memory=True,
            )
        return a

    def test_empty_content_padded_to_space(self):
        from agent.chat_completion_helpers import build_assistant_message
        from tests.run_agent.test_run_agent import _mock_assistant_msg

        agent = self._agent_for_builder()
        msg = build_assistant_message(agent, _mock_assistant_msg(content=""), "stop")
        assert msg["content"] == " ", (
            "Textless assistant turn must be padded to a single space — "
            "Moonshot/Kimi reject empty assistant content with HTTP 400."
        )

    def test_none_content_padded_to_space(self):
        from agent.chat_completion_helpers import build_assistant_message
        from tests.run_agent.test_run_agent import _mock_assistant_msg

        agent = self._agent_for_builder()
        msg = build_assistant_message(agent, _mock_assistant_msg(content=None), "stop")
        assert msg["content"] == " "

    def test_tool_call_turn_content_left_empty(self):
        from agent.chat_completion_helpers import build_assistant_message
        from tests.run_agent.test_run_agent import _mock_assistant_msg, _mock_tool_call

        agent = self._agent_for_builder()
        msg = build_assistant_message(
            agent,
            _mock_assistant_msg(content="", tool_calls=[_mock_tool_call()]),
            "tool_calls",
        )
        assert msg["content"] == "", (
            "Tool-call turns are exempt from the pad: content:'' alongside "
            "tool_calls is accepted by every provider."
        )
        assert msg["tool_calls"]

    def test_non_empty_content_unchanged(self):
        from agent.chat_completion_helpers import build_assistant_message
        from tests.run_agent.test_run_agent import _mock_assistant_msg

        agent = self._agent_for_builder()
        msg = build_assistant_message(agent, _mock_assistant_msg(content="hi"), "stop")
        assert msg["content"] == "hi"


class TestSendTimeEmptyAssistantPad:
    """Durable repair for ALREADY-poisoned persisted sessions: a partial
    -stream-stub row written by an older build (content:'' ,
    finish_reason:'length') is rebuilt to content:'' on every reload —
    ``_rows_to_conversation`` strips whitespace, so a DB-side pad cannot
    survive.  The send-time pad in conversation_loop's api_messages loop
    must therefore repair the empty textless assistant turn at the
    serialization boundary, so a RESUMED poisoned session replays
    cleanly against strict providers (Moonshot/Kimi HTTP 400 "message ...
    with role 'assistant' must not be empty")."""

    def _run_one_turn_with_history(self, loop_agent, history):
        from tests.run_agent.test_run_agent import _mock_response
        loop_agent.client.chat.completions.create.return_value = _mock_response(
            content="ok", finish_reason="stop",
        )
        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            loop_agent.run_conversation(
                "continue", conversation_history=history,
            )
        kwargs = loop_agent.client.chat.completions.create.call_args_list[0]
        return kwargs.kwargs.get("messages") or kwargs.args[0].get("messages")

    def test_poisoned_resumed_history_padded_on_send(self, loop_agent):
        # Byte-shape of a persisted poisoned session:
        # user -> assistant('' , finish_reason='length', NO tool_calls) -> user.
        poisoned = [
            {"role": "user", "content": "make me a webpage"},
            {"role": "assistant", "content": "", "finish_reason": "length"},
            {"role": "user", "content": "please proceed"},
        ]
        sent = self._run_one_turn_with_history(loop_agent, poisoned)
        empties = [
            m for m in sent
            if m.get("role") == "assistant"
            and not m.get("tool_calls")
            and m.get("content") == ""
        ]
        assert empties == [], (
            "A resumed session carrying a persisted empty partial-stream "
            "stub must be repaired at the send boundary — strict providers "
            "reject the replay with HTTP 400 otherwise."
        )
        stub = next(
            (m for m in sent if m.get("role") == "assistant"
             and not m.get("tool_calls")),
            None,
        )
        assert stub is not None and stub["content"] == " "

    def test_tool_call_turn_not_padded_on_send(self, loop_agent):
        history = [
            {"role": "user", "content": "search something"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "call_1", "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {"role": "user", "content": "and now?"},
        ]
        sent = self._run_one_turn_with_history(loop_agent, history)
        tc_turn = next(
            (m for m in sent if m.get("role") == "assistant" and m.get("tool_calls")),
            None,
        )
        assert tc_turn is not None
        assert tc_turn["content"] == "", (
            "Tool-call turns are exempt from the pad: content:'' alongside "
            "tool_calls is accepted by every provider and normalizing it "
            "would alter prompt-cache keys."
        )


class TestSendTimePadMultimodalSafety:
    """Regression: the send-time pad must skip non-string (list) assistant
    content instead of crashing — a forked session whose new user turn
    attaches an image hit AttributeError: 'list' object has no attribute
    'strip' inside the pad loop.

    Note: current main flattens multimodal assistant list-content to a
    plain string upstream of the send boundary, so the list shape rarely
    survives to the pad loop in this path — but other builders/callers can
    still produce list content, and the ``isinstance(str)`` guard must hold
    regardless of upstream flattening.  This test drives a multimodal
    history through the loop and asserts (a) no crash, and (b) the
    assistant turn's text is neither dropped nor replaced by the pad.
    """

    def test_multimodal_assistant_content_not_touched(self, loop_agent):
        from tests.run_agent.test_run_agent import _mock_response
        multimodal = [
            {"role": "user", "content": "look at this"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "I see an image"},
            ]},
            {"role": "user", "content": [
                {"type": "text", "text": "animate it"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
            ]},
        ]
        loop_agent.client.chat.completions.create.return_value = _mock_response(
            content="ok", finish_reason="stop",
        )
        with (
            patch.object(loop_agent, "_persist_session"),
            patch.object(loop_agent, "_save_trajectory"),
            patch.object(loop_agent, "_cleanup_task_resources"),
        ):
            result = loop_agent.run_conversation(
                "animate it", conversation_history=multimodal,
            )
        assert result["completed"] is True
        kwargs = loop_agent.client.chat.completions.create.call_args_list[0]
        sent = kwargs.kwargs.get("messages") or kwargs.args[0].get("messages")
        # The assistant turn survives with its text intact — regardless of
        # whether upstream passes flattened it to a str or kept the list.
        mm = next(
            m for m in sent
            if m.get("role") == "assistant" and not m.get("tool_calls")
        )
        c = mm["content"]
        if isinstance(c, list):
            assert c == [{"type": "text", "text": "I see an image"}], (
                "Multimodal assistant list content must pass through untouched."
            )
        else:
            assert "I see an image" in (c or ""), (
                "Flattened multimodal assistant text must survive the pad loop."
            )
        assert c != " ", "The pad must never replace real multimodal content."

    def test_pad_loop_skips_list_content_directly(self):
        """Unit-shape check: the pad predicate itself must skip list content
        (the exact AttributeError shape) and pad only textless str turns."""
        api_messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            {"role": "assistant", "content": ""},
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1"}]},
        ]
        # Mirror of the send-boundary pad in conversation_loop.
        for am in api_messages:
            if (
                am.get("role") == "assistant"
                and not am.get("tool_calls")
                and isinstance(am.get("content"), str)
                and not am["content"].strip()
            ):
                am["content"] = " "
        assert api_messages[0]["content"] == [{"type": "text", "text": "hi"}]
        assert api_messages[1]["content"] == " "
        assert api_messages[2]["content"] == ""
