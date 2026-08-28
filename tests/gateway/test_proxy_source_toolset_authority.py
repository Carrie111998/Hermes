"""Proxy delegation must preserve per-source toolset authority."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _SourceToolsetAdapter:
    def __init__(self, toolsets, events):
        self._toolsets = toolsets
        self._events = events

    def toolsets_for_source(self, source):
        self._events.append(("authority", source.chat_id))
        if isinstance(self._toolsets, Exception):
            raise self._toolsets
        return self._toolsets


class _ExactSourceToolsetAdapter:
    def __init__(self, exact, events, *, legacy=None):
        self._exact = exact
        self._events = events
        self._legacy = legacy

    def resolved_toolsets_for_source(self, source):
        self._events.append(("exact", source.chat_id))
        if isinstance(self._exact, Exception):
            raise self._exact
        return self._exact

    def toolsets_for_source(self, source):
        self._events.append(("legacy", source.chat_id))
        if isinstance(self._legacy, Exception):
            raise self._legacy
        return self._legacy


def _webhook_source() -> SessionSource:
    return SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:default:deploy:github:trace-1",
        chat_name="webhook/deploy",
        chat_type="webhook",
        user_id="webhook:deploy",
        user_name="deploy",
    )


def _proxy_runner(adapter) -> GatewayRunner:
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WEBHOOK: adapter}
    runner._get_proxy_url = lambda: "http://proxy.invalid:8642"
    return runner


@pytest.mark.asyncio
async def test_admitted_webhook_toolsets_cannot_be_bypassed_by_proxy(
    monkeypatch,
):
    events = []
    admitted_toolsets = ["web", "discord_admin"]
    adapter = _SourceToolsetAdapter(admitted_toolsets, events)
    runner = _proxy_runner(adapter)
    source = _webhook_source()
    user_config = {"platform_toolsets": {"webhook": ["terminal"]}}
    resolved_configs = []

    def resolve_toolsets(config, platform_key):
        resolved_configs.append((deepcopy(config), platform_key))
        events.append(("validated", tuple(config["platform_toolsets"][platform_key])))
        # Model the normal resolver dropping a platform-restricted toolset.
        return {"web"}

    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: deepcopy(user_config),
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        resolve_toolsets,
    )
    runner._run_agent_via_proxy = AsyncMock()

    result = await runner._run_agent_inner(
        message="deploy",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
    )

    assert events == [
        ("authority", source.chat_id),
        ("validated", tuple(admitted_toolsets)),
    ]
    assert resolved_configs == [
        (
            {"platform_toolsets": {"webhook": admitted_toolsets}},
            "webhook",
        )
    ]
    assert user_config == {"platform_toolsets": {"webhook": ["terminal"]}}
    runner._run_agent_via_proxy.assert_not_awaited()
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["api_calls"] == 0
    assert result["failure_reason"] == "proxy_source_toolsets_unsupported"


@pytest.mark.asyncio
async def test_explicit_empty_source_toolsets_are_deny_all_and_block_proxy(
    monkeypatch,
):
    events = []
    adapter = _SourceToolsetAdapter([], events)
    runner = _proxy_runner(adapter)
    source = _webhook_source()

    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"platform_toolsets": {"webhook": ["terminal"]}},
    )

    def resolve_toolsets(config, platform_key):
        events.append(("validated", tuple(config["platform_toolsets"][platform_key])))
        return set()

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        resolve_toolsets,
    )
    runner._run_agent_via_proxy = AsyncMock()

    result = await runner._run_agent_inner(
        message="deploy",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
    )

    assert events == [("authority", source.chat_id)]
    runner._run_agent_via_proxy.assert_not_awaited()
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["api_calls"] == 0
    assert result["failure_reason"] == "proxy_source_toolsets_unsupported"


def test_exact_admission_grant_bypasses_mutable_toolset_resolution(monkeypatch):
    events = []
    adapter = _ExactSourceToolsetAdapter(
        ["web", "newly_removed_plugin", "web"],
        events,
        legacy=AssertionError("legacy authority must not replace the durable grant"),
    )
    runner = _proxy_runner(adapter)
    source = _webhook_source()

    def reject_live_resolution(_config, _platform_key):
        raise AssertionError("durable grants must not be re-resolved")

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        reject_live_resolution,
    )

    resolved, has_override, failed = runner._resolve_toolset_authority_for_source(
        {"platform_toolsets": {"webhook": ["terminal"]}},
        source,
        "webhook",
    )

    assert resolved == ["web", "newly_removed_plugin"]
    assert has_override is True
    assert failed is False
    assert events == [("exact", source.chat_id)]


@pytest.mark.asyncio
async def test_exact_nonempty_grant_blocks_proxy_without_live_resolution(monkeypatch):
    events = []
    adapter = _ExactSourceToolsetAdapter(
        ["web", "durable_plugin"],
        events,
        legacy=AssertionError("legacy authority must not replace the durable grant"),
    )
    runner = _proxy_runner(adapter)
    source = _webhook_source()
    runner._run_agent_via_proxy = AsyncMock()

    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"platform_toolsets": {"webhook": ["terminal"]}},
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("durable grants must not be re-resolved")
        ),
    )

    result = await runner._run_agent_inner(
        message="deploy",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
    )

    assert events == [("exact", source.chat_id)]
    runner._run_agent_via_proxy.assert_not_awaited()
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["failure_reason"] == "proxy_source_toolsets_unsupported"


@pytest.mark.parametrize(
    "malformed",
    [
        pytest.param(("web",), id="tuple"),
        pytest.param(["web", ""], id="blank-name"),
        pytest.param(["web", 7], id="non-string"),
    ],
)
def test_malformed_exact_grant_fails_closed_without_legacy_fallback(
    malformed,
    monkeypatch,
):
    events = []
    adapter = _ExactSourceToolsetAdapter(
        malformed,
        events,
        legacy=["terminal"],
    )
    runner = _proxy_runner(adapter)
    source = _webhook_source()

    def reject_live_resolution(_config, _platform_key):
        raise AssertionError("malformed exact authority must fail before fallback")

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        reject_live_resolution,
    )

    resolved, has_override, failed = runner._resolve_toolset_authority_for_source(
        {"platform_toolsets": {"webhook": ["terminal"]}},
        source,
        "webhook",
    )

    assert resolved == []
    assert has_override is True
    assert failed is True
    assert events == [("exact", source.chat_id)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed",
    [pytest.param(("web",), id="tuple"), pytest.param(["web", 7], id="non-string")],
)
async def test_malformed_exact_grant_blocks_full_proxy_path(malformed, monkeypatch):
    events = []
    adapter = _ExactSourceToolsetAdapter(malformed, events, legacy=["terminal"])
    runner = _proxy_runner(adapter)
    source = _webhook_source()
    runner._run_agent_via_proxy = AsyncMock()

    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"platform_toolsets": {"webhook": ["terminal"]}},
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("malformed authority must fail before live resolution")
        ),
    )

    result = await runner._run_agent_inner(
        message="deploy",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
    )

    assert events == [("exact", source.chat_id)]
    runner._run_agent_via_proxy.assert_not_awaited()
    assert result["failure_reason"] == "proxy_source_toolset_resolution_failed"


@pytest.mark.asyncio
async def test_exact_empty_grant_blocks_proxy_without_live_resolution(monkeypatch):
    events = []
    adapter = _ExactSourceToolsetAdapter(
        [],
        events,
        legacy=AssertionError("legacy authority must not replace deny-all"),
    )
    runner = _proxy_runner(adapter)
    source = _webhook_source()
    runner._run_agent_via_proxy = AsyncMock()

    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"platform_toolsets": {"webhook": ["terminal"]}},
    )

    def reject_live_resolution(_config, _platform_key):
        raise AssertionError("deny-all must not be re-resolved")

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        reject_live_resolution,
    )

    result = await runner._run_agent_inner(
        message="deploy",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
    )

    assert events == [("exact", source.chat_id)]
    runner._run_agent_via_proxy.assert_not_awaited()
    assert result["failure_reason"] == "proxy_source_toolsets_unsupported"


@pytest.mark.asyncio
async def test_proxy_delegation_remains_available_without_source_override(
    monkeypatch,
):
    events = []
    adapter = _SourceToolsetAdapter(None, events)
    runner = _proxy_runner(adapter)
    source = _webhook_source()
    expected = {
        "final_response": "remote",
        "messages": [],
        "api_calls": 1,
        "tools": [],
    }

    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"platform_toolsets": {"webhook": ["web"]}},
    )

    def resolve_toolsets(config, platform_key):
        events.append(("validated", tuple(config["platform_toolsets"][platform_key])))
        return {"web"}

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        resolve_toolsets,
    )

    async def run_proxy(**_kwargs):
        events.append(("proxy", source.chat_id))
        return expected

    runner._run_agent_via_proxy = AsyncMock(side_effect=run_proxy)

    result = await runner._run_agent_inner(
        message="deploy",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
    )

    assert result is expected
    assert events == [
        ("authority", source.chat_id),
        ("validated", ("web",)),
        ("proxy", source.chat_id),
    ]
    runner._run_agent_via_proxy.assert_awaited_once()


@pytest.mark.asyncio
async def test_proxy_fails_closed_when_source_authority_resolution_errors(
    monkeypatch,
):
    events = []
    adapter = _SourceToolsetAdapter(RuntimeError("snapshot unavailable"), events)
    runner = _proxy_runner(adapter)
    source = _webhook_source()

    monkeypatch.setattr(
        "gateway.run._load_gateway_config",
        lambda: {"platform_toolsets": {"webhook": ["web"]}},
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda _config, _platform_key: {"web"},
    )
    runner._run_agent_via_proxy = AsyncMock()

    result = await runner._run_agent_inner(
        message="deploy",
        context_prompt="",
        history=[],
        source=source,
        session_id="session-1",
    )

    assert events == [("authority", source.chat_id)]
    runner._run_agent_via_proxy.assert_not_awaited()
    assert result["failed"] is True
    assert result["completed"] is False
    assert result["api_calls"] == 0
    assert result["failure_reason"] == "proxy_source_toolset_resolution_failed"


def test_local_toolsets_fail_closed_when_exact_authority_resolution_errors(
    monkeypatch,
):
    events = []
    adapter = _ExactSourceToolsetAdapter(
        RuntimeError("durable grant unavailable"),
        events,
        legacy=["terminal"],
    )
    runner = _proxy_runner(adapter)
    source = _webhook_source()

    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda _config, _platform_key: {"terminal"},
    )

    resolved = runner._resolve_enabled_toolsets_for_source(
        {"platform_toolsets": {"webhook": ["terminal"]}},
        source,
        "webhook",
    )

    assert resolved == []
    assert events == [("exact", source.chat_id)]


@pytest.mark.asyncio
async def test_webhook_recovery_delegates_to_primary_adapter_with_trigger():
    adapter = SimpleNamespace(recover_pending_operations=AsyncMock(return_value=3))
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WEBHOOK: adapter}

    recovered = await runner._recover_webhook_operations(trigger="reconnect:webhook")

    assert recovered == 3
    adapter.recover_pending_operations.assert_awaited_once_with(
        trigger="reconnect:webhook"
    )


@pytest.mark.asyncio
async def test_webhook_recovery_is_noop_without_recovery_capability():
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WEBHOOK: object()}

    assert await runner._recover_webhook_operations(trigger="startup") == 0


@pytest.mark.asyncio
async def test_webhook_recovery_failure_does_not_break_gateway_reconnect():
    adapter = SimpleNamespace(
        recover_pending_operations=AsyncMock(
            side_effect=RuntimeError("ledger unavailable")
        )
    )
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.WEBHOOK: adapter}

    assert await runner._recover_webhook_operations(trigger="reconnect:webhook") == 0
    adapter.recover_pending_operations.assert_awaited_once_with(
        trigger="reconnect:webhook"
    )
