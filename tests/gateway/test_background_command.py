"""Tests for /background gateway slash command.

Tests the _handle_background_command handler (run a prompt in a separate
background session) across gateway messenger platforms.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway import delivery_ledger as dl
from gateway.config import Platform
from gateway.platforms.base import MessageEvent, SendResult
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def _isolated_delivery_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(dl, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(dl, "ledger_enabled", lambda: True)


def _only_run_receipt():
    with dl._connect() as conn:
        rows = conn.execute(
            "SELECT run_receipt_id FROM run_terminal_receipts"
        ).fetchall()
    assert len(rows) == 1
    receipt = dl.get_run_terminal_receipt(rows[0][0])
    assert receipt is not None
    return receipt


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
        mock_adapter.send = AsyncMock(
            return_value=SendResult(success=True, message_id="error-1")
        )
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
        receipt = _only_run_receipt()
        assert receipt["run_terminal_state"] == "failed"
        assert receipt["run_end_reason"] == "background_missing_credentials"
        assert receipt["final_generated"] is True
        assert receipt["final_delivery_status"] == "delivered"

    @pytest.mark.asyncio
    async def test_successful_task_sends_result(self):
        """When the agent completes successfully, the result is sent."""
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock(
            return_value=SendResult(success=True, message_id="result-1")
        )
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

            await runner._run_background_task(
                "say hello",
                source,
                "bg_test",
                event_message_id="incoming-1",
            )

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
        receipt = _only_run_receipt()
        assert mock_agent_instance.run_conversation.call_args.kwargs[
            "run_receipt_id"
        ] == receipt["run_receipt_id"]
        assert receipt["run_terminal_state"] == "done"
        assert receipt["final_generated"] is True
        assert receipt["delivery_obligation_id"]
        assert receipt["final_delivery_status"] == "delivered"
        source_session_key = runner._session_key_for_source(source)
        foreground_receipt_id = dl.compute_run_receipt_id(
            source_session_key,
            "incoming-1",
            0,
            run_index=0,
        )
        assert receipt["session_key"] == (
            f"{source_session_key}:background:bg_test"
        )
        assert receipt["run_receipt_id"] != foreground_receipt_id

    @pytest.mark.asyncio
    async def test_media_only_result_has_one_tracked_text_final(self):
        runner = _make_runner()
        mock_adapter = AsyncMock()
        delivery_order = []

        async def send_terminal(**_kwargs):
            delivery_order.append("terminal")
            return SendResult(success=True, message_id="media-final-1")

        async def send_document(**_kwargs):
            delivery_order.append("attachment")
            return SendResult(success=True, message_id="document-1")

        mock_adapter.send = AsyncMock(side_effect=send_terminal)
        mock_adapter.extract_media = MagicMock(
            return_value=([("/tmp/result.pdf", False)], "")
        )
        mock_adapter.extract_images = MagicMock(return_value=([], ""))
        mock_adapter.send_document = AsyncMock(side_effect=send_document)
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = _make_event().source

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value={"api_key": "test-key"},
        ), patch(
            "gateway.run._load_gateway_config",
            return_value={},
        ), patch(
            "gateway.platforms.base.BasePlatformAdapter.filter_media_delivery_paths",
            return_value=[("/tmp/result.pdf", False)],
        ), patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {
                "final_response": "MEDIA:/tmp/result.pdf",
                "completed": True,
                "run_terminal_state": "done",
                "run_end_reason": "completed",
                "final_generated": True,
            }
            MockAgent.return_value = mock_agent

            await runner._run_background_task(
                "make a file",
                source,
                "bg_media",
            )

        assert mock_adapter.send.call_count == 1
        assert "(All required attachments were delivered.)" in (
            mock_adapter.send.call_args.kwargs["content"]
        )
        mock_adapter.send_document.assert_awaited_once()
        assert delivery_order == ["attachment", "terminal"]
        receipt = _only_run_receipt()
        assert receipt["run_terminal_state"] == "done"
        assert receipt["final_generated"] is True
        assert receipt["delivery_obligation_id"]
        assert receipt["final_delivery_status"] == "delivered"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        (
            "attachment_outcome",
            "expected_reason",
            "expected_notice",
        ),
        [
            (
                SendResult(
                    success=False,
                    error="upload rejected",
                    error_kind="forbidden",
                ),
                "background_media_delivery_rejected",
                "required attachments were not delivered",
            ),
            (
                SendResult(
                    success=False,
                    error="upload timed out",
                    error_kind="unknown",
                ),
                "background_media_delivery_unknown",
                "delivery could not be confirmed",
            ),
            (
                RuntimeError("upload connection broke"),
                "background_media_delivery_unknown:RuntimeError",
                "delivery could not be confirmed",
            ),
        ],
    )
    async def test_media_failure_truth_precedes_tracked_terminal(
        self,
        attachment_outcome,
        expected_reason,
        expected_notice,
    ):
        runner = _make_runner()
        mock_adapter = AsyncMock()
        delivery_order = []

        async def send_terminal(**_kwargs):
            delivery_order.append("terminal")
            return SendResult(success=True, message_id="terminal-1")

        async def send_document(**_kwargs):
            delivery_order.append("attachment")
            if isinstance(attachment_outcome, BaseException):
                raise attachment_outcome
            return attachment_outcome

        mock_adapter.send = AsyncMock(side_effect=send_terminal)
        mock_adapter.extract_media = MagicMock(
            return_value=([("/tmp/result.pdf", False)], "")
        )
        mock_adapter.extract_images = MagicMock(return_value=([], ""))
        mock_adapter.send_document = AsyncMock(side_effect=send_document)
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = _make_event().source

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value={"api_key": "test-key"},
        ), patch(
            "gateway.run._load_gateway_config",
            return_value={},
        ), patch(
            "gateway.platforms.base.BasePlatformAdapter.filter_media_delivery_paths",
            return_value=[("/tmp/result.pdf", False)],
        ), patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {
                "final_response": "MEDIA:/tmp/result.pdf",
                "completed": True,
                "run_terminal_state": "done",
                "run_end_reason": "completed",
                "final_generated": True,
            }
            MockAgent.return_value = mock_agent

            await runner._run_background_task(
                "make a file",
                source,
                "bg_media_failure",
            )

        assert delivery_order == ["attachment", "terminal"]
        terminal_content = mock_adapter.send.call_args.kwargs["content"]
        assert expected_notice in terminal_content
        assert "All required attachments were delivered" not in terminal_content
        receipt = _only_run_receipt()
        assert receipt["run_terminal_state"] == "failed"
        assert receipt["run_end_reason"] == expected_reason
        assert receipt["final_generated"] is True
        assert receipt["delivery_obligation_id"]
        assert receipt["final_delivery_status"] == "delivered"

    @pytest.mark.asyncio
    async def test_media_cancellation_closes_cancelled_without_terminal_claim(self):
        runner = _make_runner()
        mock_adapter = AsyncMock()
        delivery_order = []

        async def send_terminal(**_kwargs):
            delivery_order.append("terminal")
            return SendResult(success=True, message_id="terminal-1")

        async def cancel_document(**_kwargs):
            delivery_order.append("attachment")
            raise asyncio.CancelledError()

        mock_adapter.send = AsyncMock(side_effect=send_terminal)
        mock_adapter.extract_media = MagicMock(
            return_value=([("/tmp/result.pdf", False)], "")
        )
        mock_adapter.extract_images = MagicMock(return_value=([], ""))
        mock_adapter.send_document = AsyncMock(side_effect=cancel_document)
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = _make_event().source

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value={"api_key": "test-key"},
        ), patch(
            "gateway.run._load_gateway_config",
            return_value={},
        ), patch(
            "gateway.platforms.base.BasePlatformAdapter.filter_media_delivery_paths",
            return_value=[("/tmp/result.pdf", False)],
        ), patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {
                "final_response": "MEDIA:/tmp/result.pdf",
                "completed": True,
                "run_terminal_state": "done",
                "run_end_reason": "completed",
                "final_generated": True,
            }
            MockAgent.return_value = mock_agent

            with pytest.raises(asyncio.CancelledError):
                await runner._run_background_task(
                    "make a file",
                    source,
                    "bg_media_cancelled",
                )

        assert delivery_order == ["attachment"]
        mock_adapter.send.assert_not_awaited()
        receipt = _only_run_receipt()
        assert receipt["run_terminal_state"] == "cancelled"
        assert receipt["run_end_reason"] == "background_cancelled"
        assert receipt["final_generated"] is False
        assert receipt["delivery_obligation_id"] is None
        assert receipt["final_delivery_status"] is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("send_result", "expected_delivery_status"),
        [
            (
                SendResult(
                    success=False,
                    error="channel rejected final",
                    error_kind="forbidden",
                ),
                "failed",
            ),
            (
                SendResult(
                    success=False,
                    error="platform timed out",
                    error_kind="unknown",
                ),
                "attempting",
            ),
        ],
    )
    async def test_generated_result_is_not_mislabeled_delivered(
        self,
        send_result,
        expected_delivery_status,
    ):
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock(return_value=send_result)
        mock_adapter.extract_media = MagicMock(return_value=([], "Finished work."))
        mock_adapter.extract_images = MagicMock(return_value=([], "Finished work."))
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = _make_event().source

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value={"api_key": "test-key"},
        ), patch(
            "gateway.run._load_gateway_config",
            return_value={},
        ), patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {
                "final_response": "Finished work.",
                "completed": True,
                "run_terminal_state": "done",
                "run_end_reason": "completed",
                "final_generated": True,
            }
            MockAgent.return_value = mock_agent

            await runner._run_background_task(
                "finish this",
                source,
                "bg_delivery_proof",
            )

        receipt = _only_run_receipt()
        assert receipt["run_terminal_state"] == "done"
        assert receipt["final_generated"] is True
        assert receipt["delivery_obligation_id"]
        assert receipt["final_delivery_status"] == expected_delivery_status
        assert receipt["final_delivery_status"] != "delivered"

    @pytest.mark.asyncio
    async def test_empty_agent_result_delivers_and_closes_failed_receipt(self):
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock(
            return_value=SendResult(success=True, message_id="empty-1")
        )
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = _make_event().source

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value={"api_key": "test-key"},
        ), patch(
            "gateway.run._load_gateway_config",
            return_value={},
        ), patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {}
            MockAgent.return_value = mock_agent

            await runner._run_background_task(
                "return nothing",
                source,
                "bg_empty",
            )

        receipt = _only_run_receipt()
        assert "(No response generated)" in mock_adapter.send.call_args.kwargs["content"]
        assert receipt["run_terminal_state"] == "failed"
        assert receipt["run_end_reason"] == "background_agent_empty_response"
        assert receipt["final_generated"] is True
        assert receipt["final_delivery_status"] == "delivered"

    @pytest.mark.asyncio
    async def test_agent_exception_delivers_error_and_closes_failed_receipt(self):
        runner = _make_runner()
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock(
            return_value=SendResult(success=True, message_id="failure-1")
        )
        runner.adapters[Platform.TELEGRAM] = mock_adapter
        source = _make_event().source

        with patch(
            "gateway.run._resolve_runtime_agent_kwargs",
            return_value={"api_key": "test-key"},
        ), patch(
            "gateway.run._load_gateway_config",
            return_value={},
        ), patch("run_agent.AIAgent") as MockAgent:
            mock_agent = MagicMock()
            mock_agent.run_conversation.side_effect = RuntimeError("provider exploded")
            MockAgent.return_value = mock_agent

            await runner._run_background_task(
                "explode",
                source,
                "bg_exception",
            )

        receipt = _only_run_receipt()
        assert "failed" in mock_adapter.send.call_args.kwargs["content"].lower()
        assert receipt["run_terminal_state"] == "failed"
        assert receipt["run_end_reason"] == "background_exception:RuntimeError"
        assert receipt["final_generated"] is True
        assert receipt["final_delivery_status"] == "delivered"

    @pytest.mark.asyncio
    async def test_missing_adapter_still_closes_pre_agent_receipt(self):
        runner = _make_runner()
        source = _make_event().source

        await runner._run_background_task(
            "cannot route",
            source,
            "bg_missing_adapter",
        )

        receipt = _only_run_receipt()
        assert receipt["run_terminal_state"] == "failed"
        assert receipt["run_end_reason"] == "background_adapter_unavailable"
        assert receipt["final_generated"] is False
        assert receipt["delivery_obligation_id"] is None

    @pytest.mark.asyncio
    async def test_metadata_failure_still_closes_pre_agent_receipt(self):
        runner = _make_runner()
        runner._thread_metadata_for_source = MagicMock(
            side_effect=RuntimeError("metadata unavailable")
        )
        source = _make_event().source

        await runner._run_background_task(
            "cannot prepare",
            source,
            "bg_metadata_failure",
        )

        receipt = _only_run_receipt()
        assert receipt["run_terminal_state"] == "failed"
        assert receipt["run_end_reason"] == "background_exception:RuntimeError"
        assert receipt["final_generated"] is False
        assert receipt["delivery_obligation_id"] is None


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
