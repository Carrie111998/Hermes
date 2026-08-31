"""Behavior contracts for cache-safe per-turn current-time context."""

from __future__ import annotations

import types
from datetime import datetime, timedelta, timezone as dt_timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

import hermes_time
from agent.chat_completion_helpers import handle_max_iterations
from agent.context_compressor import MAX_ITERATIONS_SUMMARY_REQUEST
from agent.turn_context import (
    build_turn_context,
    compose_user_api_content,
    substitute_api_content,
)
from hermes_time import format_current_time_context


def _reset_hermes_time_cache():
    hermes_time._cached_tz = None
    hermes_time._cached_tz_name = None
    hermes_time._cache_resolved = False


@pytest.fixture(autouse=True)
def _clean_time_env(monkeypatch):
    monkeypatch.delenv("HERMES_TIMEZONE", raising=False)
    _reset_hermes_time_cache()
    yield
    _reset_hermes_time_cache()


class TestFormatCurrentTimeContext:
    def test_configured_timezone_and_offset(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", "Asia/Shanghai")
        fixed = datetime(2026, 8, 29, 13, 25, 42, tzinfo=ZoneInfo("Asia/Shanghai"))

        assert format_current_time_context(now_dt=fixed) == (
            "[Runtime Context]\n"
            "Current datetime: 2026-08-29T13:25:42+08:00\n"
            "Timezone: Asia/Shanghai\n"
            "UTC offset: +08:00"
        )

    @pytest.mark.parametrize(
        ("month", "expected_offset"),
        [(8, "-07:00"), (12, "-08:00")],
    )
    def test_timezone_offset_follows_dst(self, monkeypatch, month, expected_offset):
        monkeypatch.setenv("HERMES_TIMEZONE", "America/Los_Angeles")
        fixed = datetime(2026, month, 29, 6, 0, tzinfo=ZoneInfo("America/Los_Angeles"))

        assert f"UTC offset: {expected_offset}" in format_current_time_context(fixed)

    def test_default_time_is_timezone_aware(self):
        datetime_line = next(
            line
            for line in format_current_time_context().splitlines()
            if line.startswith("Current datetime:")
        )
        parsed = datetime.fromisoformat(datetime_line.split(": ", 1)[1])

        assert parsed.tzinfo is not None
        assert isinstance(parsed.utcoffset(), timedelta)

    def test_invalid_timezone_falls_back_safely(self, monkeypatch):
        monkeypatch.setenv("HERMES_TIMEZONE", "Not/AZone")

        context = format_current_time_context()
        assert "Current datetime:" in context
        assert "Timezone: Not/AZone" not in context

    def test_prepend_helper_fails_open(self):
        with patch.object(
            hermes_time,
            "format_current_time_context",
            side_effect=RuntimeError("boom"),
        ):
            assert hermes_time.prepend_current_time_context("summary") == "summary"


class _FakeTodoStore:
    def has_items(self):
        return True


class _FakeGuardrails:
    def reset_for_turn(self):
        pass


class _FakeAgent:
    def __init__(self, *, platform="cli", api_mode="chat_completions"):
        self.session_id = "time-context-test"
        self.model = "test/model"
        self.provider = "openrouter"
        self.base_url = "https://openrouter.ai/api/v1"
        self.api_key = "test-key"
        self.api_mode = api_mode
        self.platform = platform
        self.quiet_mode = True
        self.max_iterations = 90
        self.tools = []
        self.valid_tool_names = set()
        self._skip_mcp_refresh = True
        self.compression_enabled = False
        self.context_compressor = types.SimpleNamespace(
            protect_first_n=2, protect_last_n=2
        )
        self._cached_system_prompt = "STABLE SYSTEM"
        self._memory_store = None
        self._memory_manager = None
        self._memory_nudge_interval = 0
        self._turns_since_memory = 0
        self._user_turn_count = 0
        self._todo_store = _FakeTodoStore()
        self._tool_guardrails = _FakeGuardrails()
        self._compression_warning = None
        self._interrupt_requested = False
        self._memory_write_origin = "assistant_tool"
        self._stream_context_scrubber = None
        self._stream_think_scrubber = None
        self.persisted_content = None

    def _ensure_db_session(self):
        pass

    def _restore_primary_runtime(self):
        pass

    def _cleanup_dead_connections(self):
        return False

    def _emit_status(self, _message):
        pass

    def _replay_compression_warning(self):
        pass

    def _hydrate_todo_store(self, *_args, **_kwargs):
        pass

    def _safe_print(self, *_args, **_kwargs):
        pass

    def _persist_session(self, messages, _history=None):
        self.persisted_content = messages[-1]["content"]


def _build(agent, user_message="hello"):
    return build_turn_context(
        agent=agent,
        user_message=user_message,
        system_message=None,
        conversation_history=None,
        task_id=None,
        stream_callback=None,
        persist_user_message=None,
        restore_or_build_system_prompt=lambda *_a, **_k: None,
        install_safe_stdio=lambda: None,
        sanitize_surrogates=lambda value: value,
        summarize_user_message_for_log=lambda value: str(value),
        set_session_context=lambda _sid: None,
        set_current_write_origin=lambda _origin: None,
        ra=lambda: types.SimpleNamespace(_set_interrupt=lambda *_a, **_k: None),
    )


@pytest.fixture(autouse=True)
def _stub_runtime_main():
    with (
        patch("agent.auxiliary_client.set_runtime_main", lambda *_a, **_k: None),
        patch("hermes_cli.plugins.invoke_hook", return_value=[]),
    ):
        yield


class TestSharedTurnPrologue:
    @pytest.mark.parametrize(
        ("platform", "api_mode"),
        [
            ("cli", "chat_completions"),
            ("discord", "chat_completions"),
            ("cli", "codex_responses"),
        ],
    )
    def test_every_surface_gets_time_in_user_api_copy(self, platform, api_mode):
        agent = _FakeAgent(platform=platform, api_mode=api_mode)
        fixed = datetime(2026, 8, 29, 10, 0, tzinfo=dt_timezone.utc)

        with patch.object(hermes_time, "now", return_value=fixed):
            ctx = _build(agent)

        message = ctx.messages[ctx.current_turn_user_idx]
        assert message["content"] == "hello"
        assert message["api_content"].startswith("hello\n\n[Runtime Context]")
        assert "2026-08-29T10:00:00+00:00" in message["api_content"]
        assert agent.persisted_content == "hello"
        assert ctx.active_system_prompt == "STABLE SYSTEM"

    def test_time_regenerates_without_changing_cached_system_prompt(self):
        first = datetime(2026, 8, 29, 10, 0, tzinfo=dt_timezone.utc)
        second = datetime(2026, 8, 29, 10, 5, 30, tzinfo=dt_timezone.utc)

        with patch.object(hermes_time, "now", side_effect=[first, second]):
            first_ctx = _build(_FakeAgent())
            second_ctx = _build(_FakeAgent())

        first_message = first_ctx.messages[first_ctx.current_turn_user_idx]
        second_message = second_ctx.messages[second_ctx.current_turn_user_idx]
        assert first_ctx.active_system_prompt == second_ctx.active_system_prompt
        assert first_message["api_content"] != second_message["api_content"]
        assert "10:00:00" in first_message["api_content"]
        assert "10:05:30" in second_message["api_content"]

    def test_responses_adapter_receives_substituted_runtime_context(self):
        from agent.codex_responses_adapter import _chat_messages_to_responses_input

        ctx = _build(_FakeAgent(api_mode="codex_responses"))
        api_message = ctx.messages[ctx.current_turn_user_idx].copy()
        substitute_api_content(api_message)

        response_input = _chat_messages_to_responses_input([api_message])
        assert "[Runtime Context]" in str(response_input)
        assert "api_content" not in str(response_input)

    def test_multimodal_turn_is_not_mutated_without_byte_stable_sidecar(self):
        original = [
            {"type": "text", "text": "look"},
            {
                "type": "image_url",
                "image_url": {"url": "https://example.invalid/a.png"},
            },
        ]
        ctx = _build(_FakeAgent(), user_message=original)
        clean_content = ctx.messages[ctx.current_turn_user_idx]["content"]
        assert clean_content == original
        assert "api_content" not in ctx.messages[ctx.current_turn_user_idx]

    def test_codex_app_server_input_copy_gets_runtime_context(self):
        ctx = _build(_FakeAgent(api_mode="codex_app_server"))
        api_input = compose_user_api_content(
            ctx.user_message, "", ctx.runtime_time_context
        )

        assert api_input is not None
        assert api_input.startswith("hello\n\n[Runtime Context]")
        assert ctx.messages[ctx.current_turn_user_idx]["content"] == "hello"


class TestMaxIterationsSummary:
    def _agent(self):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            quiet_mode=True,
            skip_context_files=True,
            skip_memory=True,
        )
        agent._cached_system_prompt = "STABLE SYSTEM"
        return agent

    def test_chat_completions_gets_time_without_mutating_marker(self):
        agent = self._agent()
        captured = {}

        class _Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return "RAW"

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=_Completions())
        )
        transport = types.SimpleNamespace(
            normalize_response=lambda _response: types.SimpleNamespace(
                content="SUMMARY"
            )
        )
        messages = [{"role": "user", "content": "question"}]

        with (
            patch.object(agent, "_ensure_primary_openai_client", return_value=client),
            patch.object(agent, "_get_transport", return_value=transport),
        ):
            assert handle_max_iterations(agent, messages, 5) == "SUMMARY"

        persisted_summary = next(
            message
            for message in messages
            if message.get("role") == "user"
            and message.get("content") == MAX_ITERATIONS_SUMMARY_REQUEST
        )
        assert persisted_summary["content"] == MAX_ITERATIONS_SUMMARY_REQUEST
        assert persisted_summary["api_content"].endswith(MAX_ITERATIONS_SUMMARY_REQUEST)
        wire_summary = captured["messages"][-1]["content"]
        # Consecutive-user repair may merge the preceding question into the
        # wire message, but the persisted sidecar must remain its exact suffix
        # so replay produces the same repaired bytes.
        assert wire_summary.endswith(persisted_summary["api_content"])
        assert "[Runtime Context]" in wire_summary
        assert wire_summary.endswith(MAX_ITERATIONS_SUMMARY_REQUEST)

    def test_codex_responses_gets_fresh_time(self):
        agent = self._agent()
        agent.api_mode = "codex_responses"
        captured = {}

        def _build_api_kwargs(messages):
            captured["messages"] = messages
            return {"messages": messages, "tools": []}

        agent._build_api_kwargs = _build_api_kwargs
        agent._run_codex_stream = lambda _kwargs: "RAW"
        transport = types.SimpleNamespace(
            normalize_response=lambda _response: types.SimpleNamespace(
                content="SUMMARY"
            )
        )
        messages = [{"role": "user", "content": "question"}]

        with patch.object(agent, "_get_transport", return_value=transport):
            assert handle_max_iterations(agent, messages, 5) == "SUMMARY"

        persisted_summary = next(
            message
            for message in messages
            if message.get("role") == "user"
            and message.get("content") == MAX_ITERATIONS_SUMMARY_REQUEST
        )
        assert persisted_summary["content"] == MAX_ITERATIONS_SUMMARY_REQUEST
        assert persisted_summary["api_content"].endswith(MAX_ITERATIONS_SUMMARY_REQUEST)
        wire_summary = captured["messages"][-1]["content"]
        assert wire_summary.endswith(persisted_summary["api_content"])
        assert "[Runtime Context]" in wire_summary
        assert wire_summary.endswith(MAX_ITERATIONS_SUMMARY_REQUEST)

    def test_historical_summary_marker_keeps_its_original_bytes(self):
        agent = self._agent()
        captured = {}

        class _Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return "RAW"

        client = types.SimpleNamespace(
            chat=types.SimpleNamespace(completions=_Completions())
        )
        transport = types.SimpleNamespace(
            normalize_response=lambda _response: types.SimpleNamespace(
                content="SUMMARY"
            )
        )
        messages = [
            {"role": "user", "content": MAX_ITERATIONS_SUMMARY_REQUEST},
            {"role": "assistant", "content": "older summary"},
            {"role": "user", "content": "continue"},
        ]

        with (
            patch.object(agent, "_ensure_primary_openai_client", return_value=client),
            patch.object(agent, "_get_transport", return_value=transport),
            patch.object(
                agent, "_sanitize_api_messages", side_effect=lambda value: value
            ),
            patch.object(
                agent,
                "_drop_thinking_only_and_merge_users",
                side_effect=lambda value: value,
            ),
        ):
            assert handle_max_iterations(agent, messages, 5) == "SUMMARY"

        sent_users = [
            message["content"]
            for message in captured["messages"]
            if message.get("role") == "user"
        ]
        assert sent_users.count(MAX_ITERATIONS_SUMMARY_REQUEST) == 1
        assert (
            sum(
                "[Runtime Context]" in content
                and content.endswith(MAX_ITERATIONS_SUMMARY_REQUEST)
                for content in sent_users
            )
            == 1
        )
        new_summary = messages[-2]
        assert new_summary["content"] == MAX_ITERATIONS_SUMMARY_REQUEST
        assert new_summary["api_content"] == sent_users[-1]
