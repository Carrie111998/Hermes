"""Regression tests for #75684: routed write-approval slash commands."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
import yaml

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import MessageEvent
from gateway.session import SessionEntry, SessionSource, build_session_key


def _source() -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        user_id="u1",
        chat_id="c1",
        user_name="tester",
        chat_type="dm",
        profile="worker",
    )


def _event(text: str) -> MessageEvent:
    return MessageEvent(text=text, source=_source(), message_id="m1")


def _runner(*, multiplex: bool = True):
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        multiplex_profiles=multiplex,
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")},
    )
    adapter = MagicMock()
    adapter.send = AsyncMock()
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(
        emit=AsyncMock(), emit_collect=AsyncMock(return_value=[]), loaded_hooks=False
    )
    session_entry = SessionEntry(
        session_key=build_session_key(_source()),
        session_id="sess-1",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = session_entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.append_to_transcript = MagicMock()
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.update_session = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *args, **kwargs: None
    runner._emit_gateway_run_progress = AsyncMock()
    return runner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "handler_name"),
    [("/memory pending", "_handle_memory_command"), ("/skills pending", "_handle_skills_command")],
)
async def test_dispatch_enters_routed_profile_scope(
    tmp_path, monkeypatch, command, handler_name
):
    """Primary multiplex routes must scope slash handlers before storage access."""
    from hermes_constants import get_hermes_home

    default_home = tmp_path / "default"
    worker_home = tmp_path / "worker"
    default_home.mkdir()
    worker_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(default_home))

    runner = _runner()
    runner._resolve_profile_home_for_source = lambda _source: worker_home
    observed = []

    async def _capture(_event):
        observed.append(get_hermes_home())
        return "ok"

    setattr(runner, handler_name, _capture)
    assert await runner._handle_message(_event(command)) == "ok"
    assert observed == [worker_home]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("command", "handler_name", "section"),
    [
        ("/memory approval on", "_handle_memory_command", "memory"),
        ("/skills approval on", "_handle_skills_command", "skills"),
    ],
)
async def test_approval_toggle_writes_only_routed_profile_config(
    tmp_path, monkeypatch, command, handler_name, section
):
    """Handlers must not retain the gateway process's module-level home."""
    import gateway.run as gateway_run

    default_home = tmp_path / "default"
    worker_home = tmp_path / "worker"
    default_home.mkdir()
    worker_home.mkdir()
    for home in (default_home, worker_home):
        (home / "config.yaml").write_text(
            f"{section}:\n  write_approval: false\n", encoding="utf-8"
        )

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setattr(gateway_run, "_hermes_home", default_home)
    runner = _runner()

    with gateway_run._profile_runtime_scope(worker_home):
        result = await getattr(runner, handler_name)(_event(command))

    assert "set to 'on'" in result.lower()
    default_cfg = yaml.safe_load((default_home / "config.yaml").read_text(encoding="utf-8"))
    worker_cfg = yaml.safe_load((worker_home / "config.yaml").read_text(encoding="utf-8"))
    assert default_cfg[section]["write_approval"] is False
    assert worker_cfg[section]["write_approval"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("subsystem", ["memory", "skills"])
async def test_pending_list_reads_only_routed_profile_queue(
    tmp_path, monkeypatch, subsystem
):
    """End-to-end dispatch must not expose the default profile's pending writes."""
    import gateway.run as gateway_run
    from tools import write_approval as wa

    default_home = tmp_path / "default"
    worker_home = tmp_path / "worker"
    default_home.mkdir()
    worker_home.mkdir()
    config = "memory:\n  write_approval: true\nskills:\n  write_approval: true\n"
    for home in (default_home, worker_home):
        (home / "config.yaml").write_text(config, encoding="utf-8")

    monkeypatch.setenv("HERMES_HOME", str(default_home))
    monkeypatch.setattr(gateway_run, "_hermes_home", default_home)
    with gateway_run._profile_runtime_scope(default_home):
        wa.stage_write(subsystem, {}, summary="default-only", origin="foreground")
    with gateway_run._profile_runtime_scope(worker_home):
        wa.stage_write(subsystem, {}, summary="worker-only", origin="foreground")

    runner = _runner()
    runner._resolve_profile_home_for_source = lambda _source: worker_home
    result = await runner._handle_message(_event(f"/{subsystem} pending"))

    assert result is not None
    assert "worker-only" in result
    assert "default-only" not in result
