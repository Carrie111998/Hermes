"""Behavior tests for the session-scoped gateway SSH control plane."""

from __future__ import annotations

import asyncio

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.ssh_mode import GatewaySshModeMixin
from gateway.ssh_targets import SshTarget


SESSION_KEY = "agent:main:telegram:dm:chat-1:user-1"


class _Runner(GatewaySshModeMixin):
    def __init__(self) -> None:
        self.evicted: list[str] = []

    def _session_key_for_source(self, source) -> str:
        return SESSION_KEY

    def _evict_cached_agent(self, session_key: str) -> None:
        self.evicted.append(session_key)


def _event(text: str) -> MessageEvent:
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="user-1",
        user_name="Tester",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="message-1",
    )


def _target() -> SshTarget:
    return SshTarget(
        alias="build-box",
        host="build.example",
        user="runner",
        port=2222,
        identity_file="/keys/id_ed25519",
        cwd="/srv/project",
    )


def test_ssh_command_is_registered_and_wired_to_gateway():
    from gateway.run import GatewayRunner
    from hermes_cli.commands import (
        ACTIVE_SESSION_BYPASS_COMMANDS,
        is_interrupt_then_dispatch,
        resolve_command,
    )

    command = resolve_command("ssh")

    assert command is not None
    assert command.gateway_only is True
    assert set(command.subcommands) >= {
        "list",
        "status",
        "test",
        "use",
        "off",
        "local",
        "help",
    }
    assert "ssh" in ACTIVE_SESSION_BYPASS_COMMANDS
    assert is_interrupt_then_dispatch("ssh") is True
    assert issubclass(GatewayRunner, GatewaySshModeMixin)


def test_target_registry_is_explicit_and_redacts_identity_path(tmp_path):
    from gateway.ssh_targets import load_ssh_targets, render_ssh_targets

    registry = tmp_path / "targets.yaml"
    registry.write_text(
        """
ssh:
  targets:
    build-box:
      host: build.example
      user: runner
      port: 2222
      identity_file: /keys/id_ed25519
      cwd: /srv/project
""",
        encoding="utf-8",
    )

    targets = load_ssh_targets(registry)
    rendered = render_ssh_targets(targets, current_alias="build-box")

    assert targets == [_target()]
    assert "`build-box`" in rendered
    assert "(current)" in rendered
    assert "build.example" in rendered
    assert "/keys/id_ed25519" not in rendered
    assert "[REDACTED_PATH]" in rendered


def test_use_status_and_local_are_scoped_to_one_session(
    monkeypatch,
    tmp_path,
):
    import gateway.ssh_mode as ssh_mode
    from gateway.ssh_bindings import get_ssh_binding

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(ssh_mode, "load_ssh_targets", lambda: [_target()])
    runner = _Runner()

    enabled = asyncio.run(
        runner._handle_ssh_command(
            _event('/ssh use build-box --cwd "/srv/project with spaces"')
        )
    )
    binding = get_ssh_binding(SESSION_KEY)
    status = asyncio.run(runner._handle_ssh_command(_event("/ssh status")))
    disabled = asyncio.run(runner._handle_ssh_command(_event("/ssh local")))

    assert "SSH enabled for this session" in enabled
    assert binding is not None
    assert binding.alias == "build-box"
    assert binding.cwd == "/srv/project with spaces"
    assert "current backend: `build-box` (ssh)" in status
    assert "/keys/id_ed25519" not in status
    assert "[REDACTED_PATH]" in status
    assert "Current backend: `local`" in disabled
    assert get_ssh_binding(SESSION_KEY) is None
    assert runner.evicted == [SESSION_KEY, SESSION_KEY]


def test_unknown_or_invalid_target_never_changes_binding(
    monkeypatch,
    tmp_path,
):
    import gateway.ssh_mode as ssh_mode
    from gateway.ssh_bindings import get_ssh_binding

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner = _Runner()

    monkeypatch.setattr(ssh_mode, "load_ssh_targets", lambda: [])
    unknown = asyncio.run(
        runner._handle_ssh_command(_event("/ssh use missing"))
    )

    monkeypatch.setattr(
        ssh_mode,
        "load_ssh_targets",
        lambda: [SshTarget(alias="broken", host="build.example")],
    )
    invalid = asyncio.run(
        runner._handle_ssh_command(_event("/ssh use broken"))
    )

    assert "Unknown SSH target" in unknown
    assert "missing user" in invalid
    assert get_ssh_binding(SESSION_KEY) is None
    assert runner.evicted == []


def test_removed_or_invalid_bound_target_fails_closed(tmp_path):
    from gateway.ssh_bindings import (
        resolve_binding_task_overrides,
        set_ssh_binding,
    )

    bindings = tmp_path / "bindings.json"
    set_ssh_binding(
        SESSION_KEY,
        alias="build-box",
        path=bindings,
    )

    missing = resolve_binding_task_overrides(
        SESSION_KEY,
        targets=[],
        path=bindings,
    )
    invalid = resolve_binding_task_overrides(
        SESSION_KEY,
        targets=[SshTarget(alias="build-box", host="build.example")],
        path=bindings,
    )

    assert missing["env_type"] == "ssh"
    assert missing["ssh_host"] == ""
    assert "no longer configured" in missing["ssh_binding_error"]
    assert invalid["env_type"] == "ssh"
    assert invalid["ssh_host"] == ""
    assert "missing user" in invalid["ssh_binding_error"]
