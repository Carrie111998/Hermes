"""The CLI ``/learn`` command must run the deterministic preflight."""

import queue
import sys
import types

from hermes_cli.cli_commands_mixin import CLICommandsMixin


def test_cli_learn_uses_checkpointed_request_builder(monkeypatch):
    entrypoint = types.ModuleType("agent.learn_entrypoint")
    entrypoint.build_learn_request = lambda request: f"checkpointed:{request}"
    monkeypatch.setitem(sys.modules, "agent.learn_entrypoint", entrypoint)

    handler = object.__new__(CLICommandsMixin)
    handler._pending_input = queue.Queue()
    handler._handle_learn_command("/learn /tmp/large-book.pdf")

    assert handler._pending_input.get_nowait() == "checkpointed:/tmp/large-book.pdf"
