"""A failed dashboard bind must reach the Hermes log files, not just stderr.

uvicorn logs its own ``[Errno 10048]/[Errno 98] error while attempting to bind``
through the ``uvicorn.error`` logger, which uvicorn configures with
``propagate: False``. Nothing on that logger reaches the root handlers, so the
bind failure lands on stderr and in NO Hermes log file.

Measured during the 2026-08-12 dashboard incident: while a second dashboard was
plainly printing that error to stderr, grepping for ``10048|error while
attempting to bind`` returned **0 hits** across agent.log, gui.log, errors.log,
agent-dashboard.log and errors-dashboard.log. An operator reading the logs saw a
dashboard that simply vanished with no recorded reason.

``start_server`` must therefore log the failure itself, on a Hermes logger that
propagates to root.
"""
import socket

import pytest


@pytest.fixture
def held_port():
    """Bind and hold a real loopback port for the duration of the test."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(8)
    try:
        yield sock.getsockname()[1]
    finally:
        sock.close()


class TestBindFailureIsLogged:
    def test_bind_conflict_logs_an_error_to_a_propagating_logger(
        self, held_port, caplog, monkeypatch
    ):
        from hermes_cli import web_server

        # The keepalive spawns a background thread and does network work; it is
        # irrelevant to the bind and must not slow or flake this test.
        monkeypatch.setattr(
            "hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive",
            lambda *a, **k: None,
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(SystemExit):
                web_server.start_server(
                    host="127.0.0.1",
                    port=held_port,
                    open_browser=False,
                )

        messages = [
            r.getMessage() for r in caplog.records
            if r.levelname == "ERROR" and not r.name.startswith("uvicorn")
        ]
        assert messages, (
            "bind failure produced no ERROR on a non-uvicorn logger; it would "
            "never reach agent.log/gui.log"
        )
        assert any(str(held_port) in m for m in messages), messages
