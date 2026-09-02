"""SIGUSR2 faulthandler dump must not chain to the default kill (#84373)."""

from __future__ import annotations

import inspect

from gateway.run import GatewayRunner, register_gateway_traceback_signal


def test_traceback_signal_does_not_chain_to_fatal_default(monkeypatch, tmp_path):
    observed = {}

    def register(signum, **kwargs):
        observed["signum"] = signum
        observed.update(kwargs)

    monkeypatch.setattr("gateway.run.faulthandler.register", register)

    with (tmp_path / "tracebacks.log").open("a") as output:
        register_gateway_traceback_signal(12, file=output)

    assert observed["signum"] == 12
    assert observed["all_threads"] is True
    assert observed["chain"] is False


def test_gateway_start_registers_via_the_nonfatal_helper():
    src = inspect.getsource(GatewayRunner.start)
    assert "register_gateway_traceback_signal" in src
