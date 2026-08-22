"""CLI /stop must hard-interrupt the running parent (and thus children)."""

from hermes_cli.cli_commands_mixin import CLICommandsMixin


class _Agent:
    def __init__(self):
        self.messages = []

    def hard_interrupt(self, message=None):
        self.messages.append(message)


class _Stub(CLICommandsMixin):
    def __init__(self, agent=None, running=False):
        self.agent = agent
        self._agent_running = running


def test_stop_interrupts_foreground_agent(monkeypatch, capsys):
    from tools import process_registry as pr

    monkeypatch.setattr(pr.process_registry, "list_sessions", lambda: [])
    monkeypatch.setattr(
        "tools.async_delegation.active_count",
        lambda: 0,
        raising=False,
    )
    called = {}

    def _interrupt_all(reason="/stop"):
        called["reason"] = reason
        return 0

    monkeypatch.setattr(
        "tools.async_delegation.interrupt_all",
        _interrupt_all,
        raising=False,
    )
    agent = _Agent()
    stub = _Stub(agent=agent, running=True)
    stub._handle_stop_command()
    assert agent.messages == ["/stop"]
    assert called.get("reason") == "/stop"
    out = capsys.readouterr().out
    assert "Interrupted the running agent" in out


def test_stop_with_nothing_running_is_quiet(monkeypatch, capsys):
    from tools import process_registry as pr

    monkeypatch.setattr(pr.process_registry, "list_sessions", lambda: [])
    stub = _Stub(agent=None, running=False)
    stub._handle_stop_command()
    out = capsys.readouterr().out
    assert "No running background processes." in out
