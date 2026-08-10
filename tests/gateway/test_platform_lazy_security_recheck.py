"""Active platform hooks must not trust stale importability alone."""

import sys
import types
import asyncio
import concurrent.futures
import threading
import time

import pytest


def test_discord_active_hook_rechecks_feature_contract(monkeypatch):
    import plugins.platforms.discord.adapter as module

    monkeypatch.setattr(module, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(module, "_DISCORD_ACTIVE_CHECK_FAILED", False)
    calls = []
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda feature, **kwargs: calls.append((feature, kwargs)),
    )
    monkeypatch.setattr(module, "_define_discord_view_classes", lambda: None)

    assert module.check_discord_requirements() is True
    assert calls == [("platform.discord", {"prompt": False})]


def test_discord_active_hook_preserves_live_bindings_on_repair_failure(monkeypatch):
    import plugins.platforms.discord.adapter as module

    monkeypatch.setattr(module, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(module, "_DISCORD_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "discord", object())
    monkeypatch.setattr(module, "DiscordMessage", object())
    monkeypatch.setattr(module, "Intents", object())
    monkeypatch.setattr(module, "commands", object())
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stale")),
    )

    assert module.check_discord_requirements() is False
    assert module.DISCORD_AVAILABLE is True
    assert module.discord is not None
    assert module.commands is not None
    assert module._DISCORD_ACTIVE_CHECK_FAILED is True
    assert module.discord_deps_present() is False


def test_discord_active_hook_preserves_on_non_import_error(monkeypatch):
    import builtins
    import plugins.platforms.discord.adapter as module

    monkeypatch.setattr(module, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(module, "_DISCORD_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "discord", object())
    original_import = builtins.__import__

    def fail_sdk_import(name, *args, **kwargs):
        if name == "discord" or name.startswith("discord."):
            raise RuntimeError("broken SDK initializer")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_sdk_import)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)

    assert module.check_discord_requirements() is False
    assert module.DISCORD_AVAILABLE is True
    assert module.discord is not None
    assert module._DISCORD_ACTIVE_CHECK_FAILED is True


def test_discord_active_hook_restores_view_and_sdk_bindings_on_post_bind_failure(
    monkeypatch,
):
    """A failed view setup must not replace a running adapter's SDK objects."""
    import types

    import plugins.platforms.discord.adapter as module

    view_names = (
        "ExecApprovalView",
        "SlashConfirmView",
        "UpdatePromptView",
        "ModelPickerView",
        "ClarifyChoiceView",
        "ChoicePickerView",
    )
    sdk_names = ("DISCORD_AVAILABLE", "discord", "DiscordMessage", "Intents", "commands")
    previous = {name: object() for name in (*sdk_names, *view_names)}
    for name, value in previous.items():
        monkeypatch.setitem(module.__dict__, name, value)

    fake_discord = types.ModuleType("discord")
    fake_discord.Message = object()
    fake_discord.Intents = object()
    fake_ext = types.ModuleType("discord.ext")
    fake_commands = object()
    fake_ext.commands = fake_commands
    fake_discord.ext = fake_ext
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    monkeypatch.setitem(sys.modules, "discord.ext", fake_ext)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)

    def fail_after_staging():
        module.ExecApprovalView = object()
        module.SlashConfirmView = object()
        raise RuntimeError("incompatible Discord UI surface")

    monkeypatch.setattr(module, "_define_discord_view_classes", fail_after_staging)

    assert module.check_discord_requirements() is False
    for name, value in previous.items():
        assert module.__dict__[name] is value
    assert module._DISCORD_ACTIVE_CHECK_FAILED is True


def test_slack_active_hook_rechecks_feature_contract(monkeypatch):
    import plugins.platforms.slack.adapter as module

    monkeypatch.setattr(module, "SLACK_AVAILABLE", True)
    monkeypatch.setattr(module, "_SLACK_ACTIVE_CHECK_FAILED", False)
    calls = []

    def fake_ensure_and_bind(feature, importer, target_globals, **kwargs):
        calls.append((feature, kwargs))
        return True

    monkeypatch.setattr("tools.lazy_deps.ensure_and_bind", fake_ensure_and_bind)

    assert module.check_slack_requirements() is True
    assert calls == [("platform.slack", {"prompt": False})]


def test_slack_active_hook_preserves_live_bindings_on_repair_failure(monkeypatch):
    import plugins.platforms.slack.adapter as module

    monkeypatch.setattr(module, "SLACK_AVAILABLE", True)
    monkeypatch.setattr(module, "_SLACK_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "AsyncApp", object())
    monkeypatch.setattr(module, "AsyncSocketModeHandler", object())
    monkeypatch.setattr(module, "AsyncWebClient", object())
    monkeypatch.setattr(module, "aiohttp", object())
    monkeypatch.setattr("tools.lazy_deps.ensure_and_bind", lambda *args, **kwargs: False)

    assert module.check_slack_requirements() is False
    assert module.SLACK_AVAILABLE is True
    assert module.aiohttp is not None
    assert module.AsyncApp is not module.Any
    assert module._SLACK_ACTIVE_CHECK_FAILED is True
    assert module.slack_deps_present() is False


def test_slack_active_checks_serialize_latch_updates(monkeypatch):
    """A failed check that started first must not overwrite a later success."""
    import plugins.platforms.slack.adapter as module

    monkeypatch.setattr(module, "SLACK_AVAILABLE", True)
    monkeypatch.setattr(module, "_SLACK_ACTIVE_CHECK_FAILED", False)
    first_started = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    calls = []

    def fake_ensure_and_bind(*_args, **_kwargs):
        calls.append(len(calls))
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
            return False
        second_entered.set()
        return True

    monkeypatch.setattr("tools.lazy_deps.ensure_and_bind", fake_ensure_and_bind)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(module.check_slack_requirements)
        assert first_started.wait(timeout=2)
        second = pool.submit(module.check_slack_requirements)
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        assert first.result() is False
        assert second.result() is True

    assert calls == [0, 1]
    assert module._SLACK_ACTIVE_CHECK_FAILED is False
    assert module.slack_deps_present() is True


def test_discord_active_checks_serialize_transactional_rebind(monkeypatch):
    """A later successful Discord bind must survive an earlier failed check."""
    import plugins.platforms.discord.adapter as module

    fake_discord = types.ModuleType("discord")
    fake_discord.Message = object()
    fake_discord.Intents = object()
    fake_ext = types.ModuleType("discord.ext")
    fake_ext.commands = object()
    fake_discord.ext = fake_ext
    monkeypatch.setitem(sys.modules, "discord", fake_discord)
    monkeypatch.setitem(sys.modules, "discord.ext", fake_ext)
    monkeypatch.setattr(module, "DISCORD_AVAILABLE", True)
    monkeypatch.setattr(module, "_DISCORD_ACTIVE_CHECK_FAILED", False)
    old_discord = object()
    monkeypatch.setattr(module, "discord", old_discord)
    first_started = threading.Event()
    release_first = threading.Event()
    second_entered = threading.Event()
    calls = []

    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)

    def fake_define_views():
        calls.append(len(calls))
        if len(calls) == 1:
            first_started.set()
            assert release_first.wait(timeout=2)
            raise RuntimeError("candidate view classes are incompatible")
        second_entered.set()

    monkeypatch.setattr(module, "_define_discord_view_classes", fake_define_views)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(module.check_discord_requirements)
        assert first_started.wait(timeout=2)
        second = pool.submit(module.check_discord_requirements)
        assert not second_entered.wait(timeout=0.1)
        release_first.set()
        assert first.result() is False
        assert second.result() is True

    assert calls == [0, 1]
    assert module._DISCORD_ACTIVE_CHECK_FAILED is False
    assert module.DISCORD_AVAILABLE is True
    assert module.discord is fake_discord


def test_dingtalk_active_hook_rechecks_feature_contract(monkeypatch):
    import httpx
    import plugins.platforms.dingtalk.adapter as module

    fake_stream = types.ModuleType("dingtalk_stream")
    fake_stream.ChatbotMessage = object
    fake_frames = types.ModuleType("dingtalk_stream.frames")
    fake_frames.CallbackMessage = object
    fake_frames.AckMessage = object
    monkeypatch.setitem(sys.modules, "dingtalk_stream", fake_stream)
    monkeypatch.setitem(sys.modules, "dingtalk_stream.frames", fake_frames)
    monkeypatch.setattr(module, "DINGTALK_STREAM_AVAILABLE", True)
    monkeypatch.setattr(module, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(module, "_DINGTALK_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "dingtalk_stream", object())
    monkeypatch.setattr(module, "httpx", httpx)
    monkeypatch.setattr(module, "_load_optional_card_sdk", lambda: True)
    calls = []
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda feature, **kwargs: calls.append((feature, kwargs)),
    )

    assert module.ensure_dingtalk_deps() is True
    assert calls == [("platform.dingtalk", {"prompt": False})]


def test_dingtalk_optional_card_failure_keeps_core_bindings(monkeypatch):
    import httpx
    import plugins.platforms.dingtalk.adapter as module

    fake_stream = types.ModuleType("dingtalk_stream")
    fake_stream.ChatbotMessage = object
    fake_frames = types.ModuleType("dingtalk_stream.frames")
    fake_frames.CallbackMessage = object
    fake_frames.AckMessage = object
    monkeypatch.setitem(sys.modules, "dingtalk_stream", fake_stream)
    monkeypatch.setitem(sys.modules, "dingtalk_stream.frames", fake_frames)
    monkeypatch.setattr(module, "DINGTALK_STREAM_AVAILABLE", False)
    monkeypatch.setattr(module, "HTTPX_AVAILABLE", False)
    monkeypatch.setattr(module, "CARD_SDK_AVAILABLE", False)
    monkeypatch.setattr(module, "dingtalk_stream", None)
    monkeypatch.setattr(module, "ChatbotMessage", None)
    monkeypatch.setattr(module, "CallbackMessage", None)
    monkeypatch.setattr(module, "AckMessage", None)
    monkeypatch.setattr(module, "httpx", None)
    monkeypatch.setattr(module, "_load_optional_card_sdk", lambda: False)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)

    assert module.ensure_dingtalk_deps() is True
    assert module.DINGTALK_STREAM_AVAILABLE is True
    assert module.HTTPX_AVAILABLE is True
    assert module.dingtalk_stream is fake_stream
    assert module.httpx is httpx
    assert module.CARD_SDK_AVAILABLE is False


def test_dingtalk_card_repair_is_deferred_to_the_optional_feature(monkeypatch):
    import plugins.platforms.dingtalk.adapter as module

    monkeypatch.setattr(module, "CARD_SDK_AVAILABLE", False)
    monkeypatch.setattr(module, "_CARD_DEPS_REPAIR_RESULT", None)
    calls = []
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda feature, **kwargs: calls.append((feature, kwargs)),
    )
    monkeypatch.setattr(module, "_load_optional_card_sdk", lambda: True)

    assert module.ensure_dingtalk_card_deps() is True
    assert calls == [("platform.dingtalk_card", {"prompt": False})]


def test_dingtalk_active_hook_preserves_live_bindings_on_repair_failure(monkeypatch):
    import plugins.platforms.dingtalk.adapter as module

    monkeypatch.setattr(module, "DINGTALK_STREAM_AVAILABLE", True)
    monkeypatch.setattr(module, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(module, "_DINGTALK_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "dingtalk_stream", object())
    monkeypatch.setattr(module, "ChatbotMessage", object())
    monkeypatch.setattr(module, "CallbackMessage", object())
    monkeypatch.setattr(module, "AckMessage", object())
    monkeypatch.setattr(module, "httpx", object())
    monkeypatch.setattr(module, "CARD_SDK_AVAILABLE", True)
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stale")),
    )

    assert module.ensure_dingtalk_deps() is False
    assert module.DINGTALK_STREAM_AVAILABLE is True
    assert module.HTTPX_AVAILABLE is True
    assert module.dingtalk_stream is not None
    assert module.httpx is not None
    assert module.CARD_SDK_AVAILABLE is True
    assert module._DINGTALK_ACTIVE_CHECK_FAILED is True
    assert module.dingtalk_deps_present() is False


def test_dingtalk_failed_active_check_does_not_break_existing_adapter(monkeypatch):
    import httpx
    import plugins.platforms.dingtalk.adapter as module
    from gateway.config import PlatformConfig

    class _FailingHTTP:
        async def post(self, *_args, **_kwargs):
            raise RuntimeError("transport failed")

    monkeypatch.setattr(module, "DINGTALK_STREAM_AVAILABLE", True)
    monkeypatch.setattr(module, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(module, "_DINGTALK_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "dingtalk_stream", object())
    monkeypatch.setattr(module, "httpx", httpx)
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stale")),
    )

    adapter = module.DingTalkAdapter(
        PlatformConfig(extra={"client_id": "id", "client_secret": "secret"})
    )
    adapter._http_client = _FailingHTTP()
    adapter._session_webhooks["chat"] = (
        "https://oapi.dingtalk.com/session",
        10**15,
    )

    assert module.ensure_dingtalk_deps() is False
    result = asyncio.run(adapter.send("chat", "hello"))
    assert result.success is False
    assert "transport failed" in (result.error or "")


def test_telegram_active_hook_preserves_live_bindings_on_repair_failure(monkeypatch):
    import plugins.platforms.telegram.adapter as module

    monkeypatch.setattr(module, "TELEGRAM_AVAILABLE", True)
    monkeypatch.setattr(module, "_TELEGRAM_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "Update", object())
    monkeypatch.setattr(module, "Bot", object())
    monkeypatch.setattr(module, "HTTPXRequest", object())
    monkeypatch.setattr(
        "tools.lazy_deps.ensure",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("stale")),
    )

    assert module.check_telegram_requirements() is False
    assert module.TELEGRAM_AVAILABLE is True
    assert module.Update is not module.Any
    assert module.HTTPXRequest is not module.Any
    assert module._TELEGRAM_ACTIVE_CHECK_FAILED is True
    assert module.telegram_deps_present() is False


def test_telegram_active_hook_preserves_on_non_import_error(monkeypatch):
    import builtins
    import plugins.platforms.telegram.adapter as module

    monkeypatch.setattr(module, "TELEGRAM_AVAILABLE", True)
    monkeypatch.setattr(module, "_TELEGRAM_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "Update", object())
    original_import = builtins.__import__

    def fail_sdk_import(name, *args, **kwargs):
        if name == "telegram" or name.startswith("telegram."):
            raise RuntimeError("broken SDK initializer")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_sdk_import)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)

    assert module.check_telegram_requirements() is False
    assert module.TELEGRAM_AVAILABLE is True
    assert module.Update is not module.Any
    assert module._TELEGRAM_ACTIVE_CHECK_FAILED is True


def test_wecom_active_hook_preserves_live_bindings_on_repair_failure(monkeypatch):
    import plugins.platforms.wecom.callback_adapter as module

    monkeypatch.setattr(module, "DEFUSEDXML_AVAILABLE", True)
    monkeypatch.setattr(module, "AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr(module, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(module, "_WECOM_CALLBACK_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "ET", object())
    monkeypatch.setattr(module, "web", object())
    monkeypatch.setattr(module, "httpx", object())
    monkeypatch.setattr("tools.lazy_deps.ensure_and_bind", lambda *args, **kwargs: False)

    assert module.ensure_wecom_callback_requirements() is False
    assert module.DEFUSEDXML_AVAILABLE is True
    assert module.AIOHTTP_AVAILABLE is True
    assert module.HTTPX_AVAILABLE is True
    assert module.ET is not None
    assert module.web is not None
    assert module.httpx is not None
    assert module._WECOM_CALLBACK_ACTIVE_CHECK_FAILED is True
    assert module.check_wecom_callback_requirements() is False


def test_wecom_callback_active_check_recovers_after_a_failed_attempt(monkeypatch):
    import plugins.platforms.wecom.callback_adapter as module

    monkeypatch.setattr(module, "DEFUSEDXML_AVAILABLE", True)
    monkeypatch.setattr(module, "AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr(module, "HTTPX_AVAILABLE", True)
    monkeypatch.setattr(module, "_WECOM_CALLBACK_ACTIVE_CHECK_FAILED", True)
    monkeypatch.setattr(
        "tools.lazy_deps.ensure_and_bind",
        lambda *_args, **_kwargs: True,
    )

    assert module.ensure_wecom_callback_requirements() is True
    assert module._WECOM_CALLBACK_ACTIVE_CHECK_FAILED is False
    assert module.check_wecom_callback_requirements() is True


def test_feishu_active_hook_preserves_on_non_import_error(monkeypatch):
    import builtins
    import plugins.platforms.feishu.adapter as module

    monkeypatch.setattr(module, "FEISHU_AVAILABLE", True)
    monkeypatch.setattr(module, "FEISHU_WEBHOOK_AVAILABLE", True)
    monkeypatch.setattr(module, "FEISHU_WEBSOCKET_AVAILABLE", True)
    monkeypatch.setattr(module, "_FEISHU_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "aiohttp", object())
    original_import = builtins.__import__

    def fail_transport_import(name, *args, **kwargs):
        if name == "aiohttp" or name.startswith("aiohttp."):
            raise RuntimeError("broken transport initializer")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_transport_import)
    monkeypatch.setattr("tools.lazy_deps.ensure", lambda *_args, **_kwargs: None)

    assert module.check_feishu_requirements() is False
    assert module.FEISHU_AVAILABLE is True
    assert module.FEISHU_WEBHOOK_AVAILABLE is True
    assert module.FEISHU_WEBSOCKET_AVAILABLE is True
    assert module._FEISHU_ACTIVE_CHECK_FAILED is True
    assert module.feishu_deps_present() is False


def test_teams_active_hook_preserves_on_non_import_error(monkeypatch):
    import plugins.platforms.teams.adapter as module

    monkeypatch.setattr(module, "AIOHTTP_AVAILABLE", True)
    monkeypatch.setattr(module, "TEAMS_SDK_AVAILABLE", True)
    monkeypatch.setattr(module, "_TEAMS_ACTIVE_CHECK_FAILED", False)
    monkeypatch.setattr(module, "App", object())

    def fail_bind(*_args, **_kwargs):
        raise RuntimeError("broken SDK initializer")

    monkeypatch.setattr("tools.lazy_deps.ensure_and_bind", fail_bind)

    assert module.check_teams_requirements() is False
    assert module.AIOHTTP_AVAILABLE is True
    assert module.TEAMS_SDK_AVAILABLE is True
    assert module.App is not None
    assert module._TEAMS_ACTIVE_CHECK_FAILED is True
    assert module.check_requirements() is False


def test_teams_active_check_timeout_does_not_wait_for_another_worker(monkeypatch):
    import plugins.platforms.teams.adapter as module

    assert module._TEAMS_ACTIVE_CHECK_LOCK.acquire(timeout=0.1)
    try:
        with pytest.raises(module._TeamsActiveCheckBusy):
            module.check_teams_requirements(timeout=0.01)
    finally:
        module._TEAMS_ACTIVE_CHECK_LOCK.release()


def test_dingtalk_card_repair_is_coalesced_across_threads(monkeypatch):
    import plugins.platforms.dingtalk.adapter as module

    calls = []
    monkeypatch.setattr(module, "CARD_SDK_AVAILABLE", False)
    monkeypatch.setattr(module, "_CARD_DEPS_REPAIR_RESULT", None)
    monkeypatch.setattr(module, "_load_optional_card_sdk", lambda: False)

    def fake_ensure(*_args, **_kwargs):
        calls.append(True)
        time.sleep(0.05)

    monkeypatch.setattr("tools.lazy_deps.ensure", fake_ensure)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: module.ensure_dingtalk_card_deps(), range(2)))

    assert results == [False, False]
    assert calls == [True]
