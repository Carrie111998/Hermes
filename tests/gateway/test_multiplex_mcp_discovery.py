from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionSource


def _event(profile: str) -> MessageEvent:
    return MessageEvent(
        text="/reload-mcp",
        message_id="m1",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            user_id="u1",
            chat_id="c1",
            chat_type="dm",
            profile=profile,
        ),
    )


@pytest.mark.asyncio
async def test_gateway_boot_discovers_mcp_for_every_multiplex_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gateway.run as gateway_run
    from hermes_constants import get_hermes_home
    from tools import mcp_tool

    homes = []
    for name in ("default", "worker"):
        home = tmp_path / name
        home.mkdir()
        (home / "config.yaml").write_text(
            f"mcp_servers:\n  {name}-server:\n    command: fake\n",
            encoding="utf-8",
        )
        homes.append((name, home))

    seen: list[tuple[Path, str]] = []

    def fake_discover() -> list[str]:
        home = get_hermes_home()
        seen.append((home, threading.current_thread().name))
        return [home.name]

    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist=None: homes,
    )
    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", fake_discover)

    await gateway_run._discover_gateway_mcp_tools(
        GatewayConfig(multiplex_profiles=True)
    )

    assert [home for home, _thread in seen] == [home for _name, home in homes]
    assert all(thread != threading.current_thread().name for _home, thread in seen)


@pytest.mark.asyncio
async def test_reload_mcp_keeps_requesting_profile_scope_in_executor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.run import GatewayRunner
    from hermes_constants import get_hermes_home
    from tools import mcp_tool

    worker_home = tmp_path / "profiles" / "worker"
    worker_home.mkdir(parents=True)
    (worker_home / "config.yaml").write_text(
        "mcp_servers:\n  worker-server:\n    command: fake\n",
        encoding="utf-8",
    )

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        multiplex_profiles=True,
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
    )
    runner._resolve_profile_home_for_source = MagicMock(return_value=worker_home)
    runner._agent_cache = {}
    runner._agent_cache_lock = None
    runner._async_session_store = SimpleNamespace(
        get_or_create_session=MagicMock(side_effect=RuntimeError("skip transcript")),
    )

    seen: list[tuple[str, Path]] = []

    def fake_shutdown(*, scope=None) -> None:
        seen.append(("shutdown", get_hermes_home()))

    def fake_discover() -> list[str]:
        seen.append(("discover", get_hermes_home()))
        return []

    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", fake_shutdown)
    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", fake_discover)

    result = await runner._execute_mcp_reload(_event("worker"))

    assert "failed" not in result.lower()
    assert seen == [("shutdown", worker_home), ("discover", worker_home)]
    runner._resolve_profile_home_for_source.assert_called_once()


@pytest.mark.asyncio
async def test_reload_mcp_refreshes_only_requesting_profile_agents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from tools import mcp_tool

    worker_home = tmp_path / "profiles" / "worker"
    worker_home.mkdir(parents=True)

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._agent_cache_lock = threading.Lock()
    worker_agent = SimpleNamespace(
        tools=[], valid_tool_names=set(), enabled_toolsets=None, disabled_toolsets=None
    )
    other_agent = SimpleNamespace(
        tools=[{"type": "function", "function": {"name": "other_only"}}],
        valid_tool_names={"other_only"},
        enabled_toolsets=None,
        disabled_toolsets=None,
    )
    runner._agent_cache = OrderedDict(
        {
            "agent:worker:telegram:dm:c1": (worker_agent, "sig-worker"),
            "agent:other:telegram:dm:c1": (other_agent, "sig-other"),
        }
    )
    runner._async_session_store = SimpleNamespace(
        get_or_create_session=MagicMock(side_effect=RuntimeError("skip transcript")),
    )

    monkeypatch.setattr(mcp_tool, "shutdown_mcp_servers", lambda *, scope=None: None)
    monkeypatch.setattr(mcp_tool, "discover_mcp_tools", lambda: ["worker_tool"])
    monkeypatch.setattr(
        "model_tools.get_tool_definitions",
        lambda **kwargs: [
            {"type": "function", "function": {"name": "worker_tool"}}
        ],
    )

    with _profile_runtime_scope(worker_home):
        await runner._execute_mcp_reload(_event("worker"))

    assert worker_agent.valid_tool_names == {"worker_tool"}
    assert other_agent.valid_tool_names == {"other_only"}
