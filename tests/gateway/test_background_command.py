"""Tests for /background gateway slash command.

Tests the _handle_background_command handler (run a prompt in a separate
background session) across gateway messenger platforms.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, NonInteractiveWorkThreadHandle, SendResult
from gateway.run import _delivery_succeeded
from gateway.session import SessionSource


def _make_event(text="/background", platform=Platform.TELEGRAM,
                user_id="12345", chat_id="67890"):
    """Build a MessageEvent for testing."""
    source = SessionSource(
        platform=platform,
        user_id=user_id,
        chat_id=chat_id,
        user_name="testuser",
    )
    return MessageEvent(text=text, source=source)


def _make_runner():
    """Create a bare GatewayRunner with minimal mocks."""
    from gateway.run import GatewayRunner
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._voice_mode = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._running_agents = {}
    runner._background_tasks = set()

    mock_store = MagicMock()
    runner.session_store = mock_store

    from gateway.hooks import HookRegistry
    runner.hooks = HookRegistry()

    return runner


# ---------------------------------------------------------------------------
# _handle_background_command
# ---------------------------------------------------------------------------


class TestHandleBackgroundCommand:
    """Tests for GatewayRunner._handle_background_command."""

    @pytest.mark.asyncio
    async def test_no_prompt_shows_usage(self):
        """Running /background with no prompt shows usage."""
        runner = _make_runner()
        event = _make_event(text="/background")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result
        assert "/background" in result

    @pytest.mark.asyncio
    async def test_bg_alias_no_prompt_shows_usage(self):
        """Running /bg with no prompt shows usage."""
        runner = _make_runner()
        event = _make_event(text="/bg")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result

    @pytest.mark.asyncio
    async def test_empty_prompt_shows_usage(self):
        """Running /background with only whitespace shows usage."""
        runner = _make_runner()
        event = _make_event(text="/background   ")
        result = await runner._handle_background_command(event)
        assert "Usage:" in result

    @pytest.mark.asyncio
    async def test_discord_operations_route_creates_thread_and_passes_handle(self):
        runner = _make_runner()
        adapter = MagicMock()
        adapter.create_noninteractive_work_thread = AsyncMock(
            return_value=NonInteractiveWorkThreadHandle("999", "888", "background bg_test")
        )
        adapter.send_noninteractive_work_notification = AsyncMock()
        runner.adapters[Platform.DISCORD] = adapter
        event = _make_event(text="/background do the work", platform=Platform.DISCORD)

        with patch(
            "gateway.slash_commands.resolve_noninteractive_work_policy",
            return_value=MagicMock(
                enabled=True, channel_id="999", include_start_message=True,
                auto_archive_duration=1440, cleanup="archive",
                retain_failures=True, fallback_to_origin=True,
                route_for=lambda producer: "operations",
            ),
        ), patch.object(runner, "_run_background_task", new_callable=AsyncMock) as run_task:
            await runner._handle_background_command(event)
            await asyncio.sleep(0)

        adapter.create_noninteractive_work_thread.assert_awaited_once()
        assert run_task.await_args.kwargs["lifecycle_handle"].thread_id == "888"

    @pytest.mark.asyncio
    async def test_discord_operations_prompt_is_sanitized_before_thread_and_start_delivery(self):
        runner = _make_runner()
        adapter = MagicMock()
        adapter.create_noninteractive_work_thread = AsyncMock(
            return_value=NonInteractiveWorkThreadHandle("999", "888", "background bg_test")
        )
        adapter.send_noninteractive_work_notification = AsyncMock()
        runner.adapters[Platform.DISCORD] = adapter
        secret = "api_key=TOPSECRET"
        event = _make_event(text=f"/background do {secret}", platform=Platform.DISCORD)

        with patch(
            "gateway.slash_commands.resolve_noninteractive_work_policy",
            return_value=MagicMock(
                enabled=True, channel_id="999", include_start_message=True,
                auto_archive_duration=1440, cleanup="archive",
                retain_failures=True, fallback_to_origin=True,
                route_for=lambda producer: "operations",
            ),
        ), patch.object(runner, "_run_background_task", new_callable=AsyncMock):
            acknowledgement = await runner._handle_background_command(event)

        thread_name = adapter.create_noninteractive_work_thread.await_args.args[1]
        start_message = adapter.send_noninteractive_work_notification.await_args.args[1]
        assert "TOPSECRET" not in thread_name
        assert "TOPSECRET" not in start_message
        assert "TOPSECRET" not in acknowledgement


# ---------------------------------------------------------------------------
# _run_background_task
# ---------------------------------------------------------------------------


class TestRunBackgroundTask:
    """Tests for GatewayRunner._run_background_task (the actual execution)."""


    @pytest.mark.asyncio
    async def test_no_credentials_sends_error(self):
        """When provider credentials are missing, an error is sent."""
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}):
            await runner._run_background_task("test prompt", source, "bg_test")

        # Should have sent an error message
        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        assert "failed" in call_args[1].get("content", call_args[0][1] if len(call_args[0]) > 1 else "").lower()

    @pytest.mark.asyncio
    async def test_successful_origin_prompt_marks_discord_send_as_mentionless(self):
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock(return_value=SendResult(success=True))
        mock_adapter.extract_media = MagicMock(return_value=([], "done"))
        mock_adapter.extract_images = MagicMock(return_value=([], "done"))
        runner.adapters[Platform.DISCORD] = mock_adapter
        source = SessionSource(
            platform=Platform.DISCORD,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )
        hostile_prompt = "<@12345678901234567> <@&88888888888888888> @everyone @here"
        mock_result = {"final_response": "done", "messages": []}

        with (
            patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}),
            patch("gateway.run._load_gateway_config", return_value={}),
            patch("run_agent.AIAgent") as MockAgent,
        ):
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.return_value = mock_result
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task(hostile_prompt, source, "bg_test")

        mock_adapter.send.assert_called_once()
        metadata = mock_adapter.send.call_args.kwargs["metadata"]
        assert metadata["_hermes_origin_background"] is True
        assert hostile_prompt in mock_adapter.send.call_args.args[1]

    @pytest.mark.asyncio
    async def test_successful_task_sends_result(self):
        """When the agent completes successfully, the result is sent."""
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()
        mock_adapter.extract_media = MagicMock(return_value=([], "Hello from background!"))
        mock_adapter.extract_images = MagicMock(return_value=([], "Hello from background!"))
        runner.adapters[Platform.TELEGRAM] = mock_adapter

        source = SessionSource(
            platform=Platform.TELEGRAM,
            user_id="12345",
            chat_id="67890",
            user_name="testuser",
        )

        mock_result = {"final_response": "Hello from background!", "messages": []}

        checkpoint_config = {
            "checkpoints": {
                "enabled": True,
                "max_snapshots": 8,
                "max_total_size_mb": 222,
                "max_file_size_mb": 3,
            }
        }
        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("gateway.run._load_gateway_config", return_value=checkpoint_config), \
             patch("run_agent.AIAgent") as MockAgent:
            mock_agent_instance = MagicMock()
            mock_agent_instance.shutdown_memory_provider = MagicMock()
            mock_agent_instance.close = MagicMock()
            mock_agent_instance.run_conversation.return_value = mock_result
            MockAgent.return_value = mock_agent_instance

            await runner._run_background_task("say hello", source, "bg_test")

        # Should have sent the result
        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        content = call_args[1].get("content", call_args[0][1] if len(call_args[0]) > 1 else "")
        assert "Background task complete" in content
        assert "Hello from background!" in content
        agent_kwargs = MockAgent.call_args.kwargs
        assert agent_kwargs["checkpoints_enabled"] is True
        assert agent_kwargs["checkpoint_max_snapshots"] == 8
        assert agent_kwargs["checkpoint_max_total_size_mb"] == 222
        assert agent_kwargs["checkpoint_max_file_size_mb"] == 3
        mock_agent_instance.shutdown_memory_provider.assert_called_once()
        mock_agent_instance.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_operations_result_is_sent_to_thread_and_success_is_archived(self):
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.extract_media = MagicMock(return_value=([], "Hello from background!"))
        mock_adapter.extract_images = MagicMock(return_value=([], "Hello from background!"))
        mock_adapter.send_noninteractive_work_notification = AsyncMock()
        mock_adapter.archive_noninteractive_work_thread = AsyncMock(return_value=True)
        runner.adapters[Platform.DISCORD] = mock_adapter
        source = SessionSource(platform=Platform.DISCORD, user_id="12345", chat_id="67890")
        handle = NonInteractiveWorkThreadHandle("999", "888", "background bg_test")
        config = {"agent": {}, "platforms": {"discord": {}}}

        with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "test-key"}), \
             patch("gateway.run._load_gateway_config", return_value=config), \
             patch("run_agent.AIAgent") as MockAgent:
            instance = MagicMock()
            instance.run_conversation.return_value = {"final_response": "Hello from background!", "messages": []}
            MockAgent.return_value = instance
            await runner._run_background_task(
                "say hello", source, "bg_test", lifecycle_handle=handle,
                lifecycle_policy=MagicMock(cleanup="archive", retain_failures=True,
                                           chief_user_id=None, mention_on=("failure", "intervention")),
            )

        assert any(call.kwargs.get("event") == "success" for call in mock_adapter.send_noninteractive_work_notification.await_args_list)
        mock_adapter.archive_noninteractive_work_thread.assert_awaited_once_with(handle)


# ---------------------------------------------------------------------------
# /background in help and known_commands
# ---------------------------------------------------------------------------


class TestBackgroundInHelp:
    """Verify /background appears in help text and known commands."""

    @pytest.mark.asyncio
    async def test_background_in_help_output(self):
        """The /help output includes /background."""
        runner = _make_runner()
        event = _make_event(text="/help")
        result = await runner._handle_help_command(event)
        assert "/background" in result


# ---------------------------------------------------------------------------
# CLI /background command definition
# ---------------------------------------------------------------------------


class TestBackgroundInCLICommands:
    """Verify /background is registered in the CLI command system."""


    def test_background_autocompletes(self):
        """The /background command appears in autocomplete results."""
        pytest.importorskip("prompt_toolkit")
        from hermes_cli.commands import SlashCommandCompleter
        from prompt_toolkit.document import Document

        completer = SlashCommandCompleter()
        doc = Document("backgro")  # Partial match
        completions = list(completer.get_completions(doc, None))
        # Text doesn't start with / so no completions
        assert len(completions) == 0

        doc = Document("/backgro")  # With slash prefix
        completions = list(completer.get_completions(doc, None))
        cmd_displays = [str(c.display) for c in completions]
        assert any("/background" in d for d in cmd_displays)


class _Policy:
    cleanup = "archive"
    retain_failures = True
    fallback_to_origin = True
    chief_user_id = "12345678901234567"
    mention_on = ("failure", "intervention")


@pytest.mark.asyncio
async def test_result_error_is_failure_and_retains_thread():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], ""))
    adapter.extract_images = MagicMock(return_value=([], ""))
    adapter.send_noninteractive_work_notification = AsyncMock(return_value=SendResult(True))
    adapter.archive_noninteractive_work_thread = AsyncMock()
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="c")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background")
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "key"}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}), \
         patch("run_agent.AIAgent") as mock_agent:
        instance = MagicMock()
        instance.run_conversation.return_value = {"final_response": "", "error": "provider secret=TOPSECRET"}
        mock_agent.return_value = instance
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=_Policy())
    events = [call.kwargs["event"] for call in adapter.send_noninteractive_work_notification.await_args_list]
    assert "failure" in events
    adapter.archive_noninteractive_work_thread.assert_not_awaited()
    assert all("TOPSECRET" not in call.args[1] for call in adapter.send_noninteractive_work_notification.await_args_list)


@pytest.mark.asyncio
async def test_start_notification_failure_does_not_mislabel_successful_final_result():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], "successful result"))
    adapter.extract_images = MagicMock(return_value=([], "successful result"))
    adapter.send_noninteractive_work_notification = AsyncMock(
        return_value=SendResult(True)
    )
    adapter.archive_noninteractive_work_thread = AsyncMock()
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="c")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background")
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "key"}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}), \
         patch("run_agent.AIAgent") as mock_agent:
        instance = MagicMock()
        instance.run_conversation.return_value = {"final_response": "successful result", "messages": []}
        mock_agent.return_value = instance
        await runner._run_background_task(
            "prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=_Policy(),
            lifecycle_start_failed=True,
        )

    events = [call.kwargs["event"] for call in adapter.send_noninteractive_work_notification.await_args_list]
    assert "success" in events
    assert "result delivery failed" not in " ".join(call.args[1] for call in adapter.send_noninteractive_work_notification.await_args_list)
    adapter.archive_noninteractive_work_thread.assert_awaited_once_with(handle)


@pytest.mark.asyncio
async def test_media_send_result_failure_prevents_archive():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([(__file__, False)], "text"))
    adapter.extract_images = MagicMock(return_value=([], "text"))
    adapter.send_noninteractive_work_notification = AsyncMock(return_value=SendResult(True))
    adapter.send_document = AsyncMock(return_value=SendResult(False, error="upload failed"))
    adapter.archive_noninteractive_work_thread = AsyncMock()
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="c")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background")
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "key"}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}), \
         patch("run_agent.AIAgent") as mock_agent:
        instance = MagicMock()
        instance.run_conversation.return_value = {"final_response": "text", "messages": []}
        mock_agent.return_value = instance
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=_Policy())


@pytest.mark.asyncio
async def test_text_delivery_failure_escalates_and_does_not_archive():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], ""))
    adapter.extract_images = MagicMock(return_value=([], "text"))
    adapter.send_noninteractive_work_notification = AsyncMock(
        side_effect=[SendResult(False, error="thread send failed"), SendResult(False, error="escalation failed")]
    )
    adapter.send = AsyncMock(return_value=SendResult(False, error="origin send failed"))
    adapter.archive_noninteractive_work_thread = AsyncMock()
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background", guild_id="777")
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "key"}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}), \
         patch("run_agent.AIAgent") as mock_agent:
        instance = MagicMock()
        instance.run_conversation.return_value = {"final_response": "text", "messages": []}
        mock_agent.return_value = instance
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=_Policy())
    assert adapter.send_noninteractive_work_notification.await_count == 2
    adapter.archive_noninteractive_work_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_response_delivery_failure_escalates_and_does_not_archive():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], ""))
    adapter.extract_images = MagicMock(return_value=([], ""))
    adapter.send_noninteractive_work_notification = AsyncMock(
        side_effect=[SendResult(False, error="thread send failed"), SendResult(True)]
    )
    adapter.send = AsyncMock(return_value=SendResult(True))
    adapter.archive_noninteractive_work_thread = AsyncMock()
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background", guild_id="777")
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "key"}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}), \
         patch("run_agent.AIAgent") as mock_agent:
        instance = MagicMock()
        instance.run_conversation.return_value = {"final_response": "", "messages": []}
        mock_agent.return_value = instance
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=_Policy())
    assert adapter.send_noninteractive_work_notification.await_count == 2
    adapter.archive_noninteractive_work_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_delivery_failure_archives_when_failure_escalation_succeeds_and_retain_false():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], ""))
    adapter.extract_images = MagicMock(return_value=([], "text"))
    adapter.send_noninteractive_work_notification = AsyncMock(
        side_effect=[SendResult(False, error="thread send failed"), SendResult(True)]
    )
    adapter.send = AsyncMock(return_value=SendResult(True))
    adapter.archive_noninteractive_work_thread = AsyncMock(return_value=True)
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background", guild_id="777")
    policy = _Policy()
    policy.retain_failures = False
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "key"}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}), \
         patch("run_agent.AIAgent") as mock_agent:
        instance = MagicMock()
        instance.run_conversation.return_value = {"final_response": "text", "messages": []}
        mock_agent.return_value = instance
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=policy)
    adapter.archive_noninteractive_work_thread.assert_awaited_once_with(handle)


@pytest.mark.asyncio
async def test_text_delivery_failure_retains_when_failure_escalation_fails_and_retain_false():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([], ""))
    adapter.extract_images = MagicMock(return_value=([], "text"))
    adapter.send_noninteractive_work_notification = AsyncMock(
        side_effect=[
            SendResult(False, error="thread send failed"),
            SendResult(False, error="escalation failed"),
        ]
    )
    adapter.send = AsyncMock(return_value=SendResult(True))
    adapter.archive_noninteractive_work_thread = AsyncMock()
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background", guild_id="777")
    policy = _Policy()
    policy.retain_failures = False
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "key"}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}), \
         patch("run_agent.AIAgent") as mock_agent:
        instance = MagicMock()
        instance.run_conversation.return_value = {"final_response": "text", "messages": []}
        mock_agent.return_value = instance
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=policy)
    adapter.archive_noninteractive_work_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_text_delivery_failure_attempts_media_before_escalation_and_cleanup():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.extract_media = MagicMock(return_value=([(__file__, False)], ""))
    adapter.extract_images = MagicMock(return_value=([("https://example.test/result.png", "result")], "text"))
    adapter.send_noninteractive_work_notification = AsyncMock(
        side_effect=[
            SendResult(False, error="thread send failed"),
            SendResult(True),
        ]
    )
    adapter.send_image = AsyncMock(return_value=SendResult(True))
    adapter.send_document = AsyncMock(return_value=SendResult(True))
    adapter.archive_noninteractive_work_thread = AsyncMock(return_value=True)
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background", guild_id="777")
    policy = _Policy()
    policy.retain_failures = False
    order = []
    adapter.send_document.side_effect = lambda **kwargs: order.append("media") or SendResult(True)
    adapter.send_image.side_effect = lambda **kwargs: order.append("image") or SendResult(True)
    adapter.send_noninteractive_work_notification.side_effect = (
        lambda *args, **kwargs: order.append("notify") or SendResult(False if len(order) == 1 else True)
    )
    adapter.archive_noninteractive_work_thread.side_effect = (
        lambda handle: order.append("cleanup") or True
    )
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "key"}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}), \
         patch("run_agent.AIAgent") as mock_agent:
        instance = MagicMock()
        instance.run_conversation.return_value = {"final_response": "text", "messages": []}
        mock_agent.return_value = instance
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=policy)
    assert order == ["notify", "image", "media", "notify", "cleanup"]


@pytest.mark.asyncio
async def test_origin_failure_without_guild_does_not_fabricate_dm_link():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.send_noninteractive_work_notification = AsyncMock(return_value=SendResult(True))
    adapter.send = AsyncMock(return_value=SendResult(True))
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background")
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}):
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=_Policy())
    origin_content = adapter.send.await_args.args[1]
    assert "channels/@me/" not in origin_content
    assert "thread 888" in origin_content


@pytest.mark.asyncio
async def test_origin_failure_falls_back_without_chief_mention_target():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.send_noninteractive_work_notification = AsyncMock(return_value=SendResult(True))
    adapter.send = AsyncMock(return_value=SendResult(True))
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background", guild_id="777")
    policy = _Policy()
    policy.chief_user_id = None
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}):
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=policy)
    origin_content = adapter.send.await_args.args[1]
    assert "failed" in origin_content.lower()
    assert "<@" not in origin_content


@pytest.mark.asyncio
async def test_failure_cleanup_retains_thread_when_failure_notification_raises():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.send_noninteractive_work_notification = AsyncMock(side_effect=RuntimeError("send failed"))
    adapter.archive_noninteractive_work_thread = AsyncMock(return_value=True)
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background")
    policy = _Policy()
    policy.retain_failures = False
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}):
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=policy)
    adapter.archive_noninteractive_work_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_false_does_not_publish_to_origin_without_thread():
    runner = _make_runner()
    adapter = AsyncMock()
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    policy = _Policy()
    policy.fallback_to_origin = False
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}):
        await runner._run_background_task("prompt", source, "bg", lifecycle_policy=policy)
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_fallback_false_does_not_publish_origin_failure_with_operations_thread():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.send_noninteractive_work_notification = AsyncMock(return_value=SendResult(True))
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    policy = _Policy()
    policy.fallback_to_origin = False
    handle = NonInteractiveWorkThreadHandle("999", "888", "background")
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}):
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=policy)
    adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_task_cleans_up_only_after_failure_delivery_when_retain_false():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.send_noninteractive_work_notification = AsyncMock(return_value=SendResult(True))
    adapter.archive_noninteractive_work_thread = AsyncMock(return_value=True)
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    policy = _Policy()
    policy.retain_failures = False
    handle = NonInteractiveWorkThreadHandle("999", "888", "background")
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}):
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=policy)
    adapter.archive_noninteractive_work_thread.assert_awaited_once_with(handle)


@pytest.mark.asyncio
async def test_failed_task_keeps_thread_when_failure_delivery_fails_even_if_retain_false():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.send_noninteractive_work_notification = AsyncMock(return_value=SendResult(False, error="send failed"))
    adapter.archive_noninteractive_work_thread = AsyncMock()
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    policy = _Policy()
    policy.retain_failures = False
    handle = NonInteractiveWorkThreadHandle("999", "888", "background")
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": None}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}):
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=policy)
    adapter.archive_noninteractive_work_thread.assert_not_awaited()


@pytest.mark.asyncio
async def test_start_notification_dict_failure_is_not_treated_as_success():
    runner = _make_runner()
    adapter = MagicMock()
    adapter.create_noninteractive_work_thread = AsyncMock(
        return_value=NonInteractiveWorkThreadHandle("999", "888", "background bg_test")
    )
    adapter.send_noninteractive_work_notification = AsyncMock(return_value={"success": False, "error": "send failed"})
    runner.adapters[Platform.DISCORD] = adapter
    event = _make_event(text="/background do the work", platform=Platform.DISCORD)
    with patch(
        "gateway.slash_commands.resolve_noninteractive_work_policy",
        return_value=MagicMock(
            channel_id="999", include_start_message=True, auto_archive_duration=1440,
            route_for=lambda producer: "operations",
        ),
    ), patch.object(runner, "_run_background_task", new_callable=AsyncMock) as run_task:
        await runner._handle_background_command(event)
        await asyncio.sleep(0)
    assert run_task.await_args.kwargs["lifecycle_start_failed"] is True


@pytest.mark.asyncio
async def test_exception_origin_failure_uses_sanitized_error():
    runner = _make_runner()
    adapter = AsyncMock()
    adapter.send_noninteractive_work_notification = AsyncMock(return_value=SendResult(True))
    adapter.send = AsyncMock(return_value=SendResult(True))
    runner.adapters[Platform.DISCORD] = adapter
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background", guild_id="777")
    with patch("gateway.run._resolve_runtime_agent_kwargs", return_value={"api_key": "key"}), \
         patch("gateway.run._load_gateway_config", return_value={"agent": {}}), \
         patch.object(runner, "_run_in_executor_with_context", new_callable=AsyncMock,
                      side_effect=RuntimeError("api_key=TOPSECRET")):
        await runner._run_background_task("prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=_Policy())
    origin_content = adapter.send.await_args.args[1]
    assert "TOPSECRET" not in origin_content
    assert "***" in origin_content



def test_thread_handle_carries_guild_for_valid_discord_link():
    handle = NonInteractiveWorkThreadHandle("999", "888", "background", guild_id="777")
    assert handle.guild_id == "777"


@pytest.mark.asyncio
async def test_missing_runtime_adapter_cleans_created_lifecycle_thread():
    runner = _make_runner()
    runner.adapters.pop(Platform.DISCORD, None)
    lifecycle_adapter = AsyncMock()
    lifecycle_adapter.delete_noninteractive_work_thread = AsyncMock(return_value=True)
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background")
    policy = _Policy()
    policy.cleanup = "delete"
    policy.retain_failures = False
    await runner._run_background_task(
        "prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=policy,
        lifecycle_adapter=lifecycle_adapter,
    )
    lifecycle_adapter.delete_noninteractive_work_thread.assert_awaited_once_with(handle)


@pytest.mark.asyncio
async def test_missing_runtime_adapter_retains_created_thread_when_failures_are_retained():
    runner = _make_runner()
    runner.adapters.pop(Platform.DISCORD, None)
    lifecycle_adapter = AsyncMock()
    source = SessionSource(platform=Platform.DISCORD, user_id="u", chat_id="origin")
    handle = NonInteractiveWorkThreadHandle("999", "888", "background")
    policy = _Policy()
    policy.cleanup = "delete"
    policy.retain_failures = True
    await runner._run_background_task(
        "prompt", source, "bg", lifecycle_handle=handle, lifecycle_policy=policy,
        lifecycle_adapter=lifecycle_adapter,
    )
    lifecycle_adapter.delete_noninteractive_work_thread.assert_not_awaited()
    lifecycle_adapter.archive_noninteractive_work_thread.assert_not_awaited()


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (SendResult(success=True), True),
        (SendResult(success=False, error="delivery failed"), False),
        (None, True),
        (MagicMock(), True),
    ],
)
def test_delivery_succeeded_preserves_legacy_returns_but_honors_send_result(result, expected):
    assert _delivery_succeeded(result) is expected
