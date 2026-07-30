from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import PlatformConfig
from plugins.platforms.discord.adapter import DiscordAdapter


def _capture_channel(adapter):
    sent = {}

    async def fake_send(**kwargs):
        sent.update(kwargs)
        return SimpleNamespace(id=1234)

    channel = SimpleNamespace(send=AsyncMock(side_effect=fake_send))
    adapter._client = SimpleNamespace(
        get_channel=lambda _chat_id: channel,
        fetch_channel=AsyncMock(),
    )
    return sent


@pytest.mark.asyncio
async def test_exec_approval_prompt_uses_visible_content_with_command_and_reason():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    command = "python scripts/deploy.py --env prod --force"
    result = await adapter.send_exec_approval(
        chat_id="555",
        command=command,
        session_key="discord:555",
        description="script execution via -c flag",
    )

    assert result.success is True
    assert sent["view"] is not None
    assert sent["embed"] is not None

    prompt_text = sent["content"]
    assert "Command Approval Required" in prompt_text
    assert "Do you want Hermes to run this command?" in prompt_text
    assert "Requested command" in prompt_text
    assert command in prompt_text
    assert "Reason" in prompt_text
    assert "script execution via -c flag" in prompt_text


@pytest.mark.asyncio
async def test_exec_approval_prompt_truncates_long_command_in_content():
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    sent = _capture_channel(adapter)

    long_command = "python -c '" + ("x" * 5000) + "'"
    result = await adapter.send_exec_approval(
        chat_id="555",
        command=long_command,
        session_key="discord:555",
        description="long generated shell command",
    )

    assert result.success is True
    assert len(sent["content"]) <= adapter.MAX_MESSAGE_LENGTH
    assert "... [truncated]" in sent["content"]
    assert "long generated shell command" in sent["content"]
    assert len(sent["embed"].description) > len(sent["content"])


@pytest.mark.asyncio
async def test_run_coro_on_loop_uses_gateway_loop():
    """_run_coro_on_loop should schedule coroutines on _gateway_loop,
    not fall back to _run_async, when the gateway loop is set."""
    import asyncio
    from unittest.mock import patch
    import threading
    import time

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    _capture_channel(adapter)

    # Create a dedicated event loop to simulate the gateway loop and run it
    # in a background thread so run_coroutine_threadsafe works.
    gateway_loop = asyncio.new_event_loop()
    adapter._gateway_loop = gateway_loop
    loop_id = id(gateway_loop)
    _stop = False

    def _run_gateway_loop():
        asyncio.set_event_loop(gateway_loop)
        gateway_loop.call_soon(lambda: None)  # wake-up call
        while not _stop:
            gateway_loop.run_forever()

    t = threading.Thread(target=_run_gateway_loop, daemon=True)
    t.start()
    # Wait for the loop to start accepting tasks.
    time.sleep(0.1)

    async def identify_loop() -> dict:
        """Return the id of the loop this coroutine runs on."""
        loop = asyncio.get_running_loop()
        return {"loop_id": id(loop)}

    # The helper should run the coroutine on *gateway_loop*.
    sync_result = adapter._run_coro_on_loop(identify_loop(), timeout=5)
    assert isinstance(sync_result, dict)
    assert sync_result.get("loop_id") == loop_id, (
        f"Expected loop id {loop_id}, got {sync_result.get('loop_id')} — "
        "coroutine ran on the wrong event loop"
    )

    # Prove _run_async is NOT called when the gateway loop is available
    # by patching it and verifying it is never invoked.
    with patch("model_tools._run_async") as mock_run_async:
        sync_result2 = adapter._run_coro_on_loop(identify_loop(), timeout=5)
        mock_run_async.assert_not_called()

    _stop = True
    gateway_loop.call_soon_threadsafe(gateway_loop.stop)
    t.join(timeout=2)
    gateway_loop.close()


@pytest.mark.asyncio
async def test_run_coro_on_loop_falls_back_to_run_async_without_gateway_loop():
    """Without _gateway_loop set, _run_coro_on_loop should fall back to
    _run_async (standalone/cron context)."""
    from unittest.mock import patch

    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="***"))
    _capture_channel(adapter)
    assert adapter._gateway_loop is None

    async def dummy() -> str:
        return "fallback"

    with patch("model_tools._run_async", return_value="fallback") as mock_run_async:
        result = adapter._run_coro_on_loop(dummy(), timeout=5)
        mock_run_async.assert_called_once()
        assert result == "fallback"
