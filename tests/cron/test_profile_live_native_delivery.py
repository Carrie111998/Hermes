"""Named-profile cron delivery through a live native gateway adapter."""

import asyncio
from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from agent import secret_scope
import cron.scheduler as scheduler
from cron.scheduler import _deliver_result
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import _cron_adapters_for_profile, _profile_runtime_scope


def _run_coroutine_now(coro, _loop):
    future = Future()
    try:
        future.set_result(asyncio.run(coro))
    except BaseException as exc:  # noqa: BLE001
        future.set_exception(exc)
    return future


def _deliver(config, *, adapter_available, loop, adapter_success=True):
    adapter = AsyncMock()
    adapter.send.return_value = MagicMock(
        success=adapter_success,
        error=None if adapter_success else "gateway send failed",
        raw_response=None,
    )
    adapter.splits_long_messages = False
    standalone_send = AsyncMock(return_value={"success": True})
    job = {
        "id": "str-cron",
        "deliver": "origin",
        "origin": {"platform": "discord", "chat_id": "123"},
    }

    with (
        patch("gateway.config.load_gateway_config", return_value=config),
        patch("cron.scheduler.load_config", return_value={"cron": {"wrap_response": False}}),
        patch("asyncio.run_coroutine_threadsafe", side_effect=_run_coroutine_now),
        patch("tools.send_message_tool._send_to_platform", new=standalone_send),
    ):
        result = _deliver_result(
            job,
            "Named-profile report.",
            adapters={Platform.DISCORD: adapter} if adapter_available else {},
            loop=loop,
        )

    return result, adapter, standalone_send


def _running_loop():
    loop = MagicMock()
    loop.is_running.return_value = True
    return loop


def test_named_profile_uses_live_native_adapter_without_local_platform_config():
    """The running gateway adapter is the authenticated delivery transport."""
    result, adapter, standalone_send = _deliver(
        GatewayConfig(platforms={}),
        adapter_available=True,
        loop=_running_loop(),
    )

    assert result is None
    adapter.send.assert_awaited_once()
    standalone_send.assert_not_awaited()


def test_failed_live_native_delivery_without_config_does_not_fallback_standalone():
    result, adapter, standalone_send = _deliver(
        GatewayConfig(platforms={}),
        adapter_available=True,
        loop=_running_loop(),
        adapter_success=False,
    )

    assert "gateway send failed" in result
    adapter.send.assert_awaited_once()
    standalone_send.assert_not_awaited()


def test_standalone_delivery_without_platform_config_stays_rejected():
    result, adapter, standalone_send = _deliver(
        GatewayConfig(platforms={}),
        adapter_available=False,
        loop=None,
    )

    assert result == "platform 'discord' not configured/enabled"
    adapter.send.assert_not_awaited()
    standalone_send.assert_not_awaited()


def test_live_native_adapter_without_gateway_loop_stays_rejected():
    result, adapter, standalone_send = _deliver(
        GatewayConfig(platforms={}),
        adapter_available=True,
        loop=None,
    )

    assert result == "platform 'discord' not configured/enabled"
    adapter.send.assert_not_awaited()
    standalone_send.assert_not_awaited()


def test_live_native_adapter_on_stopped_gateway_loop_stays_rejected():
    loop = MagicMock()
    loop.is_running.return_value = False
    result, adapter, standalone_send = _deliver(
        GatewayConfig(platforms={}),
        adapter_available=True,
        loop=loop,
    )

    assert result == "platform 'discord' not configured/enabled"
    adapter.send.assert_not_awaited()
    standalone_send.assert_not_awaited()


def test_running_gateway_loop_without_native_adapter_stays_rejected():
    result, adapter, standalone_send = _deliver(
        GatewayConfig(platforms={}),
        adapter_available=False,
        loop=_running_loop(),
    )

    assert result == "platform 'discord' not configured/enabled"
    adapter.send.assert_not_awaited()
    standalone_send.assert_not_awaited()


def test_explicitly_disabled_native_platform_stays_rejected():
    result, adapter, standalone_send = _deliver(
        GatewayConfig(
            platforms={Platform.DISCORD: PlatformConfig(enabled=False)},
        ),
        adapter_available=True,
        loop=_running_loop(),
    )

    assert result == "platform 'discord' not configured/enabled"
    adapter.send.assert_not_awaited()
    standalone_send.assert_not_awaited()


def test_named_active_profile_uses_its_primary_adapter_map():
    """A gateway started from a named profile still owns runner.adapters."""
    primary_discord = object()
    runner = SimpleNamespace(
        adapters={Platform.DISCORD: primary_discord},
        _profile_adapters={},
        _active_profile_name=lambda: "ops",
    )

    assert _cron_adapters_for_profile(
        runner,  # type: ignore[arg-type]
        "ops",
    ) == {
        Platform.DISCORD: primary_discord
    }


def test_named_profile_adapter_resolution_owns_local_platform_boundaries(
    tmp_path, monkeypatch
):
    """Connected and failed local adapters must both mask the primary bot."""
    profile_home = tmp_path / "profiles" / "ops"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text(
        "DISCORD_BOT_TOKEN=ops-token\n", encoding="utf-8"
    )
    (profile_home / "config.yaml").write_text(
        "platforms:\n  discord:\n    enabled: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "default-process-token")

    primary_discord = object()
    primary_telegram = object()
    own_discord = object()
    runner = SimpleNamespace(
        adapters={
            Platform.DISCORD: primary_discord,
            Platform.TELEGRAM: primary_telegram,
        },
        _profile_adapters={"ops": {Platform.DISCORD: own_discord}},
    )

    previous = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    try:
        with _profile_runtime_scope(profile_home):
            connected = _cron_adapters_for_profile(runner, "ops")
            runner._profile_adapters["ops"] = {}
            failed = _cron_adapters_for_profile(runner, "ops")
    finally:
        secret_scope.set_multiplex_active(previous)

    assert connected[Platform.DISCORD] is own_discord
    assert connected[Platform.TELEGRAM] is primary_telegram
    assert Platform.DISCORD not in failed
    assert failed[Platform.TELEGRAM] is primary_telegram


def test_named_profile_absent_platform_may_share_primary_live_adapter(
    tmp_path, monkeypatch
):
    """A Default process token does not turn an absent profile block local."""
    profile_home = tmp_path / "profiles" / "reports"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text("", encoding="utf-8")
    (profile_home / "config.yaml").write_text("cron: {}\n", encoding="utf-8")
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "default-process-token")

    primary_discord = object()
    runner = SimpleNamespace(
        adapters={Platform.DISCORD: primary_discord},
        _profile_adapters={"reports": {}},
    )

    previous = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    try:
        with _profile_runtime_scope(profile_home):
            resolved = _cron_adapters_for_profile(runner, "reports")
    finally:
        secret_scope.set_multiplex_active(previous)

    assert resolved[Platform.DISCORD] is primary_discord


def test_run_and_delivery_scope_ignore_process_global_discord_token(
    tmp_path, monkeypatch
):
    """Real config loading cannot authorize Default standalone fallback."""
    from hermes_constants import (
        reset_hermes_home_override,
        set_hermes_home_override,
    )

    profile_home = tmp_path / "profiles" / "reports"
    profile_home.mkdir(parents=True)
    (profile_home / ".env").write_text("", encoding="utf-8")
    (profile_home / "config.yaml").write_text(
        "cron:\n  wrap_response: false\n", encoding="utf-8"
    )
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "default-process-token")

    shared_adapter = AsyncMock()
    shared_adapter.send.return_value = MagicMock(
        success=False,
        error="shared gateway send failed",
        raw_response=None,
    )
    shared_adapter.splits_long_messages = False
    standalone_send = AsyncMock(return_value={"success": True})
    marked = []

    monkeypatch.setattr(
        scheduler,
        "run_job",
        lambda *_args, **_kwargs: (True, "output", "Named report", None),
    )
    monkeypatch.setattr(
        scheduler, "save_job_output", lambda job_id, _output: f"/tmp/{job_id}.txt"
    )
    monkeypatch.setattr(scheduler, "claim_dispatch", lambda _job_id: True)
    monkeypatch.setattr(scheduler, "mark_execution_running", lambda _exec_id: None)
    monkeypatch.setattr(
        scheduler, "create_execution", lambda *_args, **_kwargs: {"id": "exec-report"}
    )
    monkeypatch.setattr(
        scheduler,
        "mark_job_run",
        lambda *args, **kwargs: marked.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(scheduler, "finish_execution", lambda *_args, **_kwargs: None)

    home_token = set_hermes_home_override(str(profile_home))
    previous = secret_scope.is_multiplex_active()
    secret_scope.set_multiplex_active(True)
    try:
        with (
            patch(
                "asyncio.run_coroutine_threadsafe",
                side_effect=_run_coroutine_now,
            ),
            patch(
                "tools.send_message_tool._send_to_platform",
                new=standalone_send,
            ),
        ):
            processed = scheduler.run_one_job(
                {
                    "id": "reports-cron",
                    "deliver": "origin",
                    "origin": {"platform": "discord", "chat_id": "123"},
                },
                adapters={Platform.DISCORD: shared_adapter},
                loop=_running_loop(),
            )
    finally:
        secret_scope.set_multiplex_active(previous)
        reset_hermes_home_override(home_token)

    assert processed is True
    shared_adapter.send.assert_awaited_once()
    standalone_send.assert_not_awaited()
    assert "shared gateway send failed" in marked[0][1]["delivery_error"]
    assert secret_scope.current_secret_scope() is None
