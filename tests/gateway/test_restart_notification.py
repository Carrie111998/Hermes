"""Tests for /restart notification — the gateway notifies the requester on comeback."""

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import gateway.run as gateway_run
from gateway.config import HomeChannel, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent, MessageType, SendResult
from gateway.session import build_session_key
from tests.gateway.restart_test_helpers import (
    make_restart_runner,
    make_restart_source,
)


# ── restart marker helpers ───────────────────────────────────────────────


def test_planned_restart_notification_pending_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    marker = tmp_path / ".restart_pending.json"

    assert gateway_run._planned_restart_notification_pending() is False
    marker.write_text("{}")
    assert gateway_run._planned_restart_notification_pending() is True

    gateway_run._clear_planned_restart_notification()

    assert gateway_run._planned_restart_notification_pending() is False


@pytest.mark.parametrize(
    "marker_name",
    [
        ".restart_notify.json",
        ".restart_notify.retry.old.json",
        ".restart_notify.claimed.interrupted.json",
    ],
)
def test_restart_notification_pending_includes_every_durable_state(
    tmp_path, monkeypatch, marker_name
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / marker_name).write_text("{}")

    assert gateway_run._restart_notification_pending() is True
    assert gateway_run.GatewayRunner._restart_notification_pending() is True


# ── _handle_restart_command writes .restart_notify.json ──────────────────


@pytest.mark.asyncio
async def test_restart_command_writes_notify_file(tmp_path, monkeypatch):
    """When /restart fires, the requester's routing info is persisted to disk."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    source = make_restart_source(chat_id="42")
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
    )

    result = await runner._handle_restart_command(event)
    assert "Restarting" in result

    notify_path = tmp_path / ".restart_notify.json"
    assert notify_path.exists()
    data = json.loads(notify_path.read_text())
    assert data["platform"] == "telegram"
    assert data["chat_id"] == "42"
    assert data["chat_type"] == "dm"
    assert data["message_id"] == "m1"
    assert "thread_id" not in data  # no thread → omitted


@pytest.mark.asyncio
async def test_restart_command_uses_atomic_json_writes_for_marker_files(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    calls = []

    def _fake_atomic_json_write(path, payload, **kwargs):
        calls.append((Path(path).name, payload, kwargs))

    # _handle_restart_command lives in gateway/slash_commands.py (extracted from
    # run.py); it uses that module's top-level atomic_json_write import.
    import gateway.slash_commands as gateway_slash
    monkeypatch.setattr(gateway_slash, "atomic_json_write", _fake_atomic_json_write)
    monkeypatch.setattr(gateway_run, "atomic_json_write", _fake_atomic_json_write)

    runner, _adapter = make_restart_runner()
    runner.request_restart = MagicMock(return_value=True)

    source = make_restart_source(chat_id="42")
    event = MessageEvent(
        text="/restart",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m1",
    )

    await runner._handle_restart_command(event)

    names = [name for name, _payload, _kwargs in calls]
    assert names == [".restart_notify.json", ".restart_last_processed.json"]
    assert calls[0][1]["chat_id"] == "42"
    assert calls[1][1]["platform"] == "telegram"


@pytest.mark.asyncio
async def test_sethome_updates_running_config_for_same_process_restart(tmp_path, monkeypatch):
    """/sethome persists to env and updates in-memory config before restart."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    saved = {}

    def _fake_save_env_value(key, value):
        saved[key] = value

    monkeypatch.setattr("hermes_cli.config.save_env_value", _fake_save_env_value)
    monkeypatch.setattr("gateway.slash_commands.persist_home_channel", lambda home, **kwargs: None)

    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="home-42")
    source.chat_name = "Ops Home"
    event = MessageEvent(
        text="/sethome",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-home",
    )

    result = await runner._handle_set_home_command(event)

    home = runner.config.get_home_channel(Platform.TELEGRAM)
    assert "Home channel set" in result
    assert saved["TELEGRAM_HOME_CHANNEL"] == "home-42"
    assert home is not None
    assert home.chat_id == "home-42"
    assert home.name == "Ops Home"


@pytest.mark.asyncio
async def test_sethome_preserves_thread_target_for_same_process_restart(tmp_path, monkeypatch):
    """/sethome from a topic/thread stores the thread-aware home target."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    saved = {}

    def _fake_save_env_value(key, value):
        saved[key] = value

    monkeypatch.setattr("hermes_cli.config.save_env_value", _fake_save_env_value)
    monkeypatch.setattr("gateway.slash_commands.persist_home_channel", lambda home, **kwargs: None)

    runner, _adapter = make_restart_runner()
    source = make_restart_source(chat_id="parent-42", thread_id="topic-7")
    source.chat_name = "Ops Topic"
    event = MessageEvent(
        text="/sethome",
        message_type=MessageType.TEXT,
        source=source,
        message_id="m-home-thread",
    )

    result = await runner._handle_set_home_command(event)

    home = runner.config.get_home_channel(Platform.TELEGRAM)
    assert "Home channel set" in result
    assert saved["TELEGRAM_HOME_CHANNEL"] == "parent-42"
    assert saved["TELEGRAM_HOME_CHANNEL_THREAD_ID"] == "topic-7"
    assert home is not None
    assert home.chat_id == "parent-42"
    assert home.thread_id == "topic-7"


# ── home-channel startup notifications ─────────────────────────────────────


@pytest.mark.asyncio
async def test_send_home_channel_startup_notification_preserves_thread_metadata(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, adapter = make_restart_runner()
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="parent-42",
        name="Ops Topic",
        thread_id="777",
    )
    # Declare the DM-topic lookup on the adapter CLASS, not the instance.
    # _is_telegram_dm_topic_target resolves _get_dm_topic_info via type(adapter)
    # so a MagicMock auto-attribute (instance-level) is intentionally ignored;
    # a real adapter exposes the method on its class. Mirrors the fake-adapter
    # pattern in test_telegram_topic_mode.py.
    class _DmTopicAdapter(type(adapter)):
        def _get_dm_topic_info(self, chat_id, thread_id):
            return {"name": "Ops Topic"}

    adapter.__class__ = _DmTopicAdapter
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="home"))

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == {("telegram", "parent-42", "777")}
    adapter.send.assert_called_once_with(
        "parent-42",
        "♻️ Gateway online — Hermes is back and ready.",
        metadata={
            "thread_id": "777",
            "telegram_dm_topic_reply_fallback": True,
            "direct_messages_topic_id": "777",
        },
    )


@pytest.mark.asyncio
async def test_relay_fronted_logical_home_gets_startup_notification(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    runner, _native = make_restart_runner()
    relay = MagicMock()
    relay.fronts_platform.side_effect = lambda platform: platform == Platform.SLACK
    relay.send_for_platform = AsyncMock(return_value=SendResult(success=True, message_id="home"))
    runner.adapters = {Platform.RELAY: relay}
    runner.config.platforms = {
        Platform.RELAY: PlatformConfig(enabled=True),
        Platform.SLACK: PlatformConfig(
            enabled=False,
            home_channel=HomeChannel(
                platform=Platform.SLACK,
                chat_id="D123",
                name="Owner DM",
                user_id="U123",
                scope_id="T123",
            ),
        ),
    }

    delivered = await runner._send_home_channel_startup_notifications()

    assert delivered == {("slack", "D123", None)}
    relay.send_for_platform.assert_awaited_once()
    assert relay.send_for_platform.await_args.args[:3] == (
        Platform.SLACK,
        "D123",
        "♻️ Gateway online — Hermes is back and ready.",
    )
    assert relay.send_for_platform.await_args.kwargs["metadata"]["user_id"] == "U123"
    assert relay.send_for_platform.await_args.kwargs["metadata"]["scope_id"] == "T123"


# ── _send_restart_notification ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_relay_restart_notification_uses_logical_platform_and_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(
        json.dumps(
            {
                "platform": "slack",
                "chat_id": "D123",
                "chat_type": "dm",
                "user_id": "U123",
                "scope_id": "T123",
                "delivered_via_upstream_relay": True,
            }
        )
    )

    runner, _native = make_restart_runner()
    relay = MagicMock()
    relay.fronts_platform.side_effect = lambda platform: platform == Platform.SLACK
    relay.send_for_platform = AsyncMock(
        return_value=SendResult(success=True, message_id="restart")
    )
    runner.adapters = {Platform.RELAY: relay}
    runner.config.platforms = {
        Platform.RELAY: PlatformConfig(enabled=True),
        Platform.SLACK: PlatformConfig(enabled=False),
    }

    delivered_target = await runner._send_restart_notification()

    assert delivered_target == ("slack", "D123", None)
    relay.send_for_platform.assert_awaited_once()
    assert relay.send_for_platform.await_args.args[0:2] == (Platform.SLACK, "D123")
    metadata = relay.send_for_platform.await_args.kwargs["metadata"]
    assert metadata["user_id"] == "U123"
    assert metadata["scope_id"] == "T123"
    assert not notify_path.exists()


@pytest.mark.asyncio
async def test_restart_notification_single_call_drains_existing_snapshot(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    now = gateway_run.time.time()
    markers = [
        (".restart_notify.claimed.interrupted.json", "41", now - 3),
        (".restart_notify.retry.old.json", "42", now - 2),
        (".restart_notify.json", "43", now - 1),
    ]
    for name, chat_id, mtime in markers:
        path = tmp_path / name
        path.write_text(json.dumps({"platform": "telegram", "chat_id": chat_id}))
        os.utime(path, (mtime, mtime))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="delivered")
    )

    delivered = await runner._send_restart_notification(schedule_retry=False)

    assert delivered == ("telegram", "41", None)
    assert [call.args[0] for call in adapter.send.await_args_list] == ["41", "42", "43"]
    assert not list(tmp_path.glob(".restart_notify*.json"))


@pytest.mark.asyncio
async def test_failed_old_retry_does_not_block_new_canonical(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    retry = tmp_path / ".restart_notify.retry.old.json"
    canonical = tmp_path / ".restart_notify.json"
    retry.write_text(json.dumps({"platform": "telegram", "chat_id": "old"}))
    canonical.write_text(json.dumps({"platform": "telegram", "chat_id": "new"}))
    now = gateway_run.time.time()
    os.utime(retry, (now - 2, now - 2))
    os.utime(canonical, (now - 1, now - 1))
    runner, adapter = make_restart_runner()

    async def _send(chat_id, *_args, **_kwargs):
        if chat_id == "old":
            return SendResult(success=False, error="still unavailable")
        return SendResult(success=True, message_id="new-delivered")

    adapter.send = AsyncMock(side_effect=_send)

    delivered = await runner._send_restart_notification(schedule_retry=False)

    assert delivered == ("telegram", "new", None)
    assert [call.args[0] for call in adapter.send.await_args_list] == ["old", "new"]
    remaining = list(tmp_path.glob(".restart_notify*.json"))
    assert len(remaining) == 1
    assert json.loads(remaining[0].read_text())["chat_id"] == "old"


@pytest.mark.asyncio
async def test_hung_old_restart_receipt_is_detached_and_newer_receipt_delivers(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_RESTART_NOTIFICATION_SEND_TIMEOUT_SECONDS", 0.01
    )
    retry = tmp_path / ".restart_notify.retry.old.json"
    canonical = tmp_path / ".restart_notify.json"
    retry.write_text(json.dumps({"platform": "telegram", "chat_id": "old"}))
    canonical.write_text(json.dumps({"platform": "telegram", "chat_id": "new"}))
    now = gateway_run.time.time()
    os.utime(retry, (now - 2, now - 2))
    os.utime(canonical, (now - 1, now - 1))
    runner, adapter = make_restart_runner()
    release_old = asyncio.Event()
    attempts = []

    async def _send(chat_id, *_args, **_kwargs):
        attempts.append(chat_id)
        if chat_id == "old":
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                await release_old.wait()
            return SendResult(success=True, message_id="ambiguous-old")
        return SendResult(success=True, message_id="new-delivered")

    adapter.send = AsyncMock(side_effect=_send)
    delivery = asyncio.create_task(
        runner._send_restart_notification(schedule_retry=False)
    )
    try:
        await asyncio.sleep(0.2)
        completed_before_release = delivery.done()
        delivered = delivery.result() if completed_before_release else None
        remaining = list(tmp_path.glob(".restart_notify*.json"))
    finally:
        release_old.set()
        await delivery
        await asyncio.sleep(0)

    assert completed_before_release is True
    assert delivered == ("telegram", "new", None)
    assert attempts == ["old", "new"]
    assert len(remaining) == 1
    assert json.loads(remaining[0].read_text())["chat_id"] == "old"


@pytest.mark.asyncio
async def test_cancel_after_transport_acceptance_is_at_least_once(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / ".restart_notify.json").write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    runner, adapter = make_restart_runner()
    accepted = asyncio.Event()
    release = asyncio.Event()
    attempts = []

    async def _send(chat_id, *_args, **_kwargs):
        attempts.append(chat_id)
        accepted.set()
        await release.wait()
        return SendResult(success=True, message_id="accepted")

    adapter.send = AsyncMock(side_effect=_send)
    first = asyncio.create_task(
        runner._send_restart_notification(schedule_retry=False)
    )
    await accepted.wait()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first

    claims = list(tmp_path.glob(".restart_notify.claimed.*.json"))
    assert len(claims) == 1

    release.set()
    await runner._send_restart_notification(schedule_retry=False)

    # A crash/cancellation after remote acceptance but before durable cleanup
    # cannot be distinguished from a failed send; at-least-once may duplicate.
    assert attempts == ["42", "42"]
    assert not list(tmp_path.glob(".restart_notify*.json"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "marker_text",
    ["{not-json", "[]", json.dumps({"platform": "unknown", "chat_id": "42"})],
)
async def test_restart_notification_discards_invalid_durable_marker(
    tmp_path, monkeypatch, marker_text
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(marker_text)
    runner, adapter = make_restart_runner()

    assert await runner._send_restart_notification() is None

    assert not notify_path.exists()
    assert not hasattr(runner, "_restart_notification_retry_task")
    assert adapter.sent == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("chat_id", []),
        ("thread_id", {}),
        ("chat_type", []),
        ("message_id", {}),
        ("user_id", []),
        ("scope_id", {}),
        ("delivered_via_upstream_relay", "true"),
    ],
)
async def test_restart_notification_discards_marker_with_invalid_field_type(
    tmp_path, monkeypatch, field, invalid_value
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    marker = {"platform": "telegram", "chat_id": "42"}
    marker[field] = invalid_value
    notify_path.write_text(json.dumps(marker))
    runner, adapter = make_restart_runner()

    assert await runner._send_restart_notification() is None

    assert not notify_path.exists()
    assert not hasattr(runner, "_restart_notification_retry_task")
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_restart_notification_discards_marker_when_freshness_stat_fails(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({"platform": "telegram", "chat_id": "42"}))
    runner, adapter = make_restart_runner()
    real_stat = Path.stat

    def _fail_explicit_marker_stat(path, *args, **kwargs):
        if path.name == ".restart_notify.json" or path.name.startswith(
            ".restart_notify.claimed."
        ):
            raise OSError("freshness unavailable")
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _fail_explicit_marker_stat)

    assert await runner._send_restart_notification() is None

    monkeypatch.setattr(Path, "stat", real_stat)
    assert not notify_path.exists()
    assert not hasattr(runner, "_restart_notification_retry_task")
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_restart_notification_discards_future_dated_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    future = gateway_run.time.time() + 60
    os.utime(notify_path, (future, future))
    runner, adapter = make_restart_runner()

    assert await runner._send_restart_notification() is None

    assert not notify_path.exists()
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_restart_notification_discards_expired_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_RESTART_NOTIFICATION_MAX_AGE_SECONDS", -1
    )
    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    runner, adapter = make_restart_runner()

    assert await runner._send_restart_notification() is None

    assert not notify_path.exists()
    assert adapter.sent == []


@pytest.mark.asyncio
async def test_concurrent_restart_notification_senders_deliver_once(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="delivered-once")
    )

    first, second = await asyncio.gather(
        runner._send_restart_notification(),
        runner._send_restart_notification(),
    )

    assert adapter.send.await_count == 1
    assert not notify_path.exists()
    assert sorted(value is None for value in (first, second)) == [False, True]


@pytest.mark.asyncio
async def test_restart_notification_preserves_replacement_written_during_send(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    old_marker = {"platform": "telegram", "chat_id": "42"}
    new_marker = {"platform": "telegram", "chat_id": "99"}
    notify_path.write_text(json.dumps(old_marker))
    runner, adapter = make_restart_runner()
    send_started = asyncio.Event()
    release_send = asyncio.Event()

    async def _send(*args, **kwargs):
        send_started.set()
        await release_send.wait()
        return SendResult(success=True, message_id="old-delivered")

    adapter.send = AsyncMock(side_effect=_send)
    delivery = asyncio.create_task(runner._send_restart_notification())
    await send_started.wait()
    replacement = tmp_path / ".restart_notify.replacement.json"
    replacement.write_text(json.dumps(new_marker))
    os.replace(replacement, notify_path)
    release_send.set()

    assert await delivery == ("telegram", "42", None)
    assert json.loads(notify_path.read_text()) == new_marker
    assert adapter.send.await_count == 1


@pytest.mark.asyncio
async def test_restart_notification_claim_isolated_from_pre_read_replacement(
    tmp_path, monkeypatch
):
    """A marker published after claim is neither read nor cleaned as the old one."""
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    old_marker = {"platform": "telegram", "chat_id": "42"}
    new_marker = {"platform": "telegram", "chat_id": "99"}
    notify_path.write_text(json.dumps(old_marker))
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="delivered")
    )
    real_replace = os.replace
    replacement_published = False

    def _replace_and_publish_new(source, destination):
        nonlocal replacement_published
        real_replace(source, destination)
        destination_name = Path(destination).name
        if (
            not replacement_published
            and Path(source) == notify_path
            and destination_name.startswith(".restart_notify.claimed.")
        ):
            replacement_published = True
            notify_path.write_text(json.dumps(new_marker))

    monkeypatch.setattr(gateway_run.os, "replace", _replace_and_publish_new)

    assert await runner._send_restart_notification() == ("telegram", "42", None)

    assert replacement_published is True
    assert json.loads(notify_path.read_text()) == new_marker
    assert await runner._send_restart_notification() == ("telegram", "99", None)
    assert [call.args[0] for call in adapter.send.await_args_list] == ["42", "99"]
    assert not list(tmp_path.glob(".restart_notify*.json"))


@pytest.mark.asyncio
async def test_restart_notification_transient_claim_retries_without_overwriting_replacement(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    notify_path = tmp_path / ".restart_notify.json"
    old_marker = {"platform": "telegram", "chat_id": "42"}
    new_marker = {"platform": "telegram", "chat_id": "99"}
    notify_path.write_text(json.dumps(old_marker))
    runner, adapter = make_restart_runner()
    attempted_chat_ids = []

    async def _send(chat_id, *_args, **_kwargs):
        attempted_chat_ids.append(chat_id)
        if len(attempted_chat_ids) == 1:
            notify_path.write_text(json.dumps(new_marker))
            return SendResult(success=False, error="temporary timeout")
        return SendResult(success=True, message_id=f"delivered-{chat_id}")

    adapter.send = AsyncMock(side_effect=_send)

    assert await runner._send_restart_notification(schedule_retry=False) is None

    retained_payloads = [
        json.loads(path.read_text())
        for path in tmp_path.glob(".restart_notify*.json")
    ]
    assert old_marker in retained_payloads
    assert new_marker in retained_payloads

    assert await runner._send_restart_notification(schedule_retry=False) == (
        "telegram",
        "42",
        None,
    )
    assert attempted_chat_ids == ["42", "42", "99"]
    assert not list(tmp_path.glob(".restart_notify*.json"))


@pytest.mark.asyncio
async def test_restart_notification_fsyncs_claim_and_success_cleanup(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / ".restart_notify.json").write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    fsyncs = MagicMock()
    monkeypatch.setattr(
        gateway_run, "_fsync_restart_notification_directory", fsyncs, raising=False
    )
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(
        return_value=SendResult(success=True, message_id="delivered")
    )

    await runner._send_restart_notification(schedule_retry=False)

    assert fsyncs.call_count == 2


@pytest.mark.asyncio
async def test_restart_notification_confirms_live_platform_health(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / ".restart_notify.json").write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    runner, adapter = make_restart_runner()
    adapter._running = True
    whatsapp = MagicMock()
    whatsapp.is_connected = True
    runner.adapters[Platform.WHATSAPP] = whatsapp

    await runner._send_restart_notification()

    message = adapter.sent[0]
    assert "Gateway restarted successfully" in message
    assert "System is operating normally" in message
    assert "Telegram, Whatsapp" in message


@pytest.mark.asyncio
async def test_restart_notification_reports_configured_failed_platform(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / ".restart_notify.json").write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    runner, adapter = make_restart_runner()
    adapter._running = True
    runner.config.platforms[Platform.SLACK] = PlatformConfig(enabled=True, token="***")
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "gateway_state": "running",
            "platforms": {
                "telegram": {"state": "connected"},
                "slack": {"state": "retrying"},
            },
        },
    )

    await runner._send_restart_notification()

    message = adapter.sent[0]
    assert "System is operating with limitations" in message
    assert "Disconnected platforms: Slack" in message


@pytest.mark.asyncio
async def test_restart_notification_reports_runtime_only_retrying_platform(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / ".restart_notify.json").write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    runner, adapter = make_restart_runner()
    adapter._running = True
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "platforms": {
                "telegram": {"state": "connected"},
                "slack": {"state": "retrying"},
                "future_plugin_platform": {"state": "retrying"},
            }
        },
    )

    await runner._send_restart_notification()

    message = adapter.sent[0]
    assert "System is operating with limitations" in message
    assert "Disconnected platforms: Slack" in message
    assert "future_plugin_platform" not in message


@pytest.mark.asyncio
async def test_restart_notification_health_summary_is_localized(tmp_path, monkeypatch):
    from agent import i18n

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    (tmp_path / ".restart_notify.json").write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    runner, adapter = make_restart_runner()
    adapter._running = True
    runner.config.platforms[Platform.SLACK] = PlatformConfig(enabled=True, token="***")
    monkeypatch.setattr(
        "gateway.status.read_runtime_status",
        lambda: {
            "platforms": {
                "telegram": {"state": "connected"},
                "slack": {"state": "retrying"},
            }
        },
    )
    monkeypatch.setenv("HERMES_LANGUAGE", "ru")
    i18n.reset_language_cache()
    try:
        await runner._send_restart_notification()
    finally:
        i18n.reset_language_cache()

    message = adapter.sent[0]
    assert "Gateway успешно перезапущен" in message
    assert "Система работает с ограничениями" in message
    assert "Подключённые платформы: Telegram" in message
    assert "Отключённые платформы: Slack" in message
    assert "Gateway restarted successfully" not in message
    assert "Disconnected platforms" not in message


@pytest.mark.asyncio
async def test_send_restart_notification_logs_warning_on_sendresult_failure(
    tmp_path, monkeypatch, caplog
):
    """Adapter that returns SendResult(success=False) must log a WARNING, not INFO.

    Regression guard: adapter.send() catches provider errors (e.g. Telegram
    "Chat not found") and returns SendResult(success=False) rather than
    raising. The caller previously ignored the return value and always
    logged "Sent restart notification to ..." at INFO — masking real
    delivery failures behind a fake success line.
    """
    from gateway.platforms.base import SendResult

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(
        return_value=SendResult(success=False, error="Chat not found"),
    )

    with caplog.at_level("DEBUG", logger="gateway.run"):
        delivered_target = await runner._send_restart_notification()

    success_lines = [
        r for r in caplog.records
        if r.levelname == "INFO" and "Sent restart notification" in r.getMessage()
    ]
    warning_lines = [
        r for r in caplog.records
        if r.levelname == "WARNING"
        and "was not delivered" in r.getMessage()
        and "Chat not found" in r.getMessage()
    ]
    assert delivered_target is None
    assert not success_lines, (
        "Expected no INFO 'Sent restart notification' line when send failed, "
        f"got: {[r.getMessage() for r in success_lines]}"
    )
    assert warning_lines, (
        "Expected a WARNING line mentioning the failure; "
        f"got records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    # Keep the durable receipt so a later reconnect can retry delivery.
    retained_paths = list(tmp_path.glob(".restart_notify*.json"))
    assert len(retained_paths) == 1
    assert json.loads(retained_paths[0].read_text()) == {
        "platform": "telegram",
        "chat_id": "42",
    }


@pytest.mark.asyncio
async def test_restart_notification_retries_transient_failure_while_connected(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_RESTART_NOTIFICATION_RETRY_DELAYS", (0.0,), raising=False
    )
    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(
        json.dumps({"platform": "telegram", "chat_id": "42"})
    )
    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(
        side_effect=[
            SendResult(success=False, error="temporary timeout"),
            SendResult(success=True, message_id="retry-ok"),
        ]
    )

    assert await runner._send_restart_notification() is None
    retry_task = runner._restart_notification_retry_task
    await retry_task

    assert adapter.send.await_count == 2
    assert not notify_path.exists()
    assert retry_task.done()


@pytest.mark.asyncio
async def test_send_restart_notification_logs_info_on_sendresult_success(
    tmp_path, monkeypatch, caplog
):
    """Adapter returning SendResult(success=True) keeps the INFO log line."""
    from gateway.platforms.base import SendResult

    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)

    notify_path = tmp_path / ".restart_notify.json"
    notify_path.write_text(json.dumps({
        "platform": "telegram",
        "chat_id": "42",
    }))

    runner, adapter = make_restart_runner()
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="m-1"))

    with caplog.at_level("DEBUG", logger="gateway.run"):
        delivered_target = await runner._send_restart_notification()

    success_lines = [
        r for r in caplog.records
        if r.levelname == "INFO" and "Sent restart notification" in r.getMessage()
    ]
    assert delivered_target == ("telegram", "42", None)
    assert success_lines, (
        "Expected INFO 'Sent restart notification' when send succeeded; "
        f"got records: {[(r.levelname, r.getMessage()) for r in caplog.records]}"
    )
    assert not notify_path.exists()


@pytest.mark.asyncio
async def test_shutdown_notifications_use_cached_live_thread_source_when_origin_missing():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="parent-42", chat_type="group", thread_id="topic-7")
    session_key = build_session_key(source)

    runner._running_agents[session_key] = object()
    runner.session_store._entries[session_key] = MagicMock(origin=None)
    runner._cache_session_source(session_key, source)
    adapter.send = AsyncMock(return_value=SendResult(success=True, message_id="shutdown"))

    await runner._notify_active_sessions_of_shutdown()

    adapter.send.assert_awaited_once_with(
        "parent-42",
        "⚠️ Gateway shutting down — Your current task will be interrupted.",
        metadata={"thread_id": "topic-7"},
    )


@pytest.mark.asyncio
async def test_shutdown_notifications_are_fully_muted_when_flag_disabled():
    runner, adapter = make_restart_runner()
    source = make_restart_source(chat_id="active-42", chat_type="group", thread_id="topic-7")
    session_key = build_session_key(source)

    runner.config.platforms[Platform.TELEGRAM].gateway_restart_notification = False
    runner.config.platforms[Platform.TELEGRAM].home_channel = HomeChannel(
        platform=Platform.TELEGRAM,
        chat_id="home-42",
        name="Ops Home",
    )
    runner._running_agents[session_key] = object()
    runner.session_store._entries[session_key] = MagicMock(origin=source)
    adapter.send = AsyncMock()

    await runner._notify_active_sessions_of_shutdown()

    adapter.send.assert_not_awaited()


