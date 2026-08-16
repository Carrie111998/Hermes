"""The slash worker feeds desktop chat bubbles, which render plain text — so
any ANSI a worker-routed command emits (e.g. /journey's own Rich Console) must
be stripped from the worker's return value."""

from __future__ import annotations


class _FakeCLI:
    console = None

    def process_command(self, cmd: str) -> None:
        import sys

        sys.stdout.write("\x1b[38;2;1;2;3mcolored\x1b[0m plain")


def test_run_strips_ansi_from_output():
    from tui_gateway import slash_worker

    out = slash_worker._run(_FakeCLI(), "/anything")

    assert "\x1b[" not in out
    assert out == "colored plain"


def test_run_refuses_skill_slash_instead_of_pending_input(monkeypatch):
    from tui_gateway import slash_worker

    monkeypatch.setattr(
        "agent.skill_commands.resolve_skill_slash",
        lambda token, **kwargs: "/ratchet-panel",
    )

    class _CLI:
        console = None

        def process_command(self, cmd: str) -> None:
            raise AssertionError(f"must not process_command skill {cmd!r}")

    try:
        slash_worker._run(_CLI(), "ratchet-panel")
    except RuntimeError as exc:
        assert "skill command" in str(exc)
        assert "ratchet-panel" in str(exc)
    else:
        raise AssertionError("expected RuntimeError for skill slash")
