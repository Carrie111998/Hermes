"""Focused regression: the root gateway ``/kanban`` handler arms the
ephemeral control-plane capability around ``run_slash`` — and only around it.

The capability (a process-local ContextVar in ``gateway.status``) is one of
the two gates privileged board mutations require; the other is retained
gateway-runtime-lock ownership. A leaked capability would let unrelated
in-process code paths mint continuation evidence, so these tests pin the
exact arming boundary.
"""

import asyncio
from types import SimpleNamespace

from gateway import status
from gateway.slash_commands import GatewaySlashCommandsMixin


def _event(text: str) -> SimpleNamespace:
    return SimpleNamespace(
        text=text,
        source=SimpleNamespace(
            platform="test",
            chat_id="",
            thread_id="",
            user_id="",
        ),
    )


def test_kanban_handler_arms_control_plane_around_run_slash(
    tmp_path, monkeypatch
):
    seen = {}

    def fake_run_slash(_text):
        seen["active_inside"] = status.gateway_control_plane_active()
        return "ok"

    monkeypatch.setattr("hermes_cli.kanban.run_slash", fake_run_slash)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert status.acquire_gateway_runtime_lock() is True
    handler = GatewaySlashCommandsMixin()
    handler._gateway_control_plane_context = (
        status._claim_gateway_control_plane_context()
    )

    try:
        output = asyncio.run(handler._handle_kanban_command(_event("/kanban list")))
    finally:
        status.release_gateway_runtime_lock()

    assert output == "ok"
    assert seen["active_inside"] is True
    # The capability is reset on handler exit and never leaks.
    assert status.gateway_control_plane_active() is False


def test_kanban_handler_capability_does_not_leak_on_error(
    tmp_path, monkeypatch
):
    def boom(_text):
        raise RuntimeError("kaput")

    monkeypatch.setattr("hermes_cli.kanban.run_slash", boom)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert status.acquire_gateway_runtime_lock() is True
    handler = GatewaySlashCommandsMixin()
    handler._gateway_control_plane_context = (
        status._claim_gateway_control_plane_context()
    )

    try:
        output = asyncio.run(handler._handle_kanban_command(_event("/kanban list")))
    finally:
        status.release_gateway_runtime_lock()

    assert isinstance(output, str)
    assert status.gateway_control_plane_active() is False


def test_kanban_handler_capability_not_armed_outside_dispatch(monkeypatch):
    """Plain callers of run_slash (CLI process, worker processes) never see
    the capability — only the gateway handler arms it."""
    import hermes_cli.kanban

    seen = {}

    def spy(text):
        seen["active_inside"] = status.gateway_control_plane_active()
        return "ok"

    monkeypatch.setattr(hermes_cli.kanban, "run_slash", spy)
    # Direct call without the gateway handler wrapper:
    assert hermes_cli.kanban.run_slash("list") == "ok"
    assert seen["active_inside"] is False
