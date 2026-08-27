"""Interactive chat must refuse a non-TTY stdin instead of crashing.

prompt_toolkit's vt100 input registers stdin's fd with the asyncio selector
(``Application.run`` -> ``_attached_input`` -> ``loop.add_reader``), which a
redirected stdin cannot satisfy. Before the guard, ``nohup hermes chat`` / a
pipe / any non-interactive subprocess died with a bare

    OSError: [Errno 22] Invalid argument

out of ``selectors.control()`` -- after git worktree setup, full agent init and
the whole welcome banner had already run.
"""

import sys

import pytest

import cli as cli_mod
from cli import will_enter_interactive_repl


class TestWillEnterInteractiveRepl:
    """Only a bare invocation reaches prompt_toolkit."""

    def test_bare_invocation_is_interactive(self):
        assert will_enter_interactive_repl() is True

    @pytest.mark.parametrize(
        "kwargs",
        [
            pytest.param({"query": "hello"}, id="--query"),
            pytest.param({"q": "hello"}, id="-q-shorthand"),
            pytest.param({"image": "/tmp/cat.png"}, id="--image"),
            pytest.param({"list_tools": True}, id="--list-tools"),
            pytest.param({"list_toolsets": True}, id="--list-toolsets"),
            pytest.param({"gateway": True}, id="--gateway"),
        ],
    )
    def test_non_interactive_entry_points(self, kwargs):
        # Each of these returns before cli.run(), so a redirected stdin is
        # legitimate -- driving `hermes chat -q "..."` from a script, a cron
        # job or a CI step must keep working.
        assert will_enter_interactive_repl(**kwargs) is False

    def test_empty_string_query_is_not_a_one_shot(self):
        # Fire passes "" for an omitted value in some shells; an empty query is
        # not a query, so this stays interactive (and therefore still gated).
        assert will_enter_interactive_repl(query="") is True


class TestInteractiveTTYGuard:
    @pytest.fixture
    def non_tty_stdin(self, monkeypatch):
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

    def test_exits_with_code_1(self, non_tty_stdin):
        with pytest.raises(SystemExit) as exc:
            cli_mod.main()
        assert exc.value.code == 1

    def test_message_is_actionable_and_on_stderr(self, non_tty_stdin, capsys):
        with pytest.raises(SystemExit):
            cli_mod.main()
        captured = capsys.readouterr()
        assert "requires a terminal" in captured.err
        # Point the user at the supported non-interactive form.
        assert '-q "your prompt"' in captured.err
        assert "requires a terminal" not in captured.out

    def test_fires_before_any_worktree_or_agent_setup(self, non_tty_stdin, monkeypatch):
        # The whole point is to fail fast: nothing expensive or side-effecting
        # may run first. HermesCLI construction stands in for "agent init".
        def _fail(*_args, **_kwargs):
            raise AssertionError("guard ran too late — setup already started")

        monkeypatch.setattr(cli_mod, "HermesCLI", _fail)
        with pytest.raises(SystemExit) as exc:
            cli_mod.main()
        assert exc.value.code == 1


class TestBackstopGuard:
    """The REPL entry itself is guarded, not only the predicted branch.

    The early guard predicts which branch main() will take; this one sits on the
    actual REPL entry. A future flag that carves out a new non-interactive path,
    or a reordering of main()'s dispatch, must not be able to reach
    prompt_toolkit with a non-TTY stdin.
    """

    def test_repl_entry_is_guarded_even_if_the_early_check_is_bypassed(
        self, monkeypatch, capsys
    ):
        # Simulate exactly that regression: force the predicate to claim this
        # invocation is non-interactive, so the early guard does not fire.
        monkeypatch.setattr(cli_mod, "will_enter_interactive_repl", lambda **_kw: False)
        monkeypatch.setattr(sys.stdin, "isatty", lambda: False, raising=False)

        def _boom(*_a, **_k):
            raise AssertionError("reached cli.run() with a non-TTY stdin")

        monkeypatch.setattr(cli_mod.HermesCLI, "run", _boom, raising=False)
        with pytest.raises(SystemExit) as exc:
            cli_mod.main()
        assert exc.value.code == 1
        assert "requires a terminal" in capsys.readouterr().err
