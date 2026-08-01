"""Gateway tool-surface regressions for cronjob management."""

import asyncio

import pytest


def test_gateway_start_exposes_cronjob_tool_before_agent_construction(monkeypatch):
    """A real gateway process must advertise cronjob without external env setup."""
    monkeypatch.delenv("HERMES_GATEWAY_SESSION", raising=False)
    monkeypatch.delenv("HERMES_INTERACTIVE", raising=False)
    monkeypatch.delenv("HERMES_EXEC_ASK", raising=False)

    from gateway import code_skew
    from gateway.run import start_gateway
    from tools.cronjob_tools import check_cronjob_requirements

    class StopAfterGatewayContext(RuntimeError):
        pass

    def stop_after_gateway_context():
        raise StopAfterGatewayContext

    # Stop at the first startup side effect. The process-role marker must be
    # bound before this point so every later AIAgent sees the cronjob schema.
    monkeypatch.setattr(
        code_skew, "record_boot_fingerprint", stop_after_gateway_context
    )

    with pytest.raises(StopAfterGatewayContext):
        asyncio.run(start_gateway())

    assert check_cronjob_requirements() is True

    # Exercise the same registry-to-model schema path used by AIAgent rather
    # than stopping at the requirement predicate.
    from model_tools import get_tool_definitions

    definitions = get_tool_definitions(
        enabled_toolsets=["cronjob"],
        quiet_mode=False,
        skip_tool_search_assembly=True,
    )
    assert "cronjob" in {definition["function"]["name"] for definition in definitions}
