from argparse import Namespace


def test_tools_summary_bypasses_interactive_tty_guard(monkeypatch):
    import hermes_cli.main as main_mod
    import hermes_cli.tools_config as tools_config

    called = []

    def fail_tty_guard(_command_name):
        raise AssertionError("summary mode must not require a TTY")

    def fake_tools_command(args):
        called.append(args)

    monkeypatch.setattr(main_mod, "_require_tty", fail_tty_guard)
    monkeypatch.setattr(tools_config, "tools_command", fake_tools_command)

    args = Namespace(tools_action=None, summary=True)
    main_mod.cmd_tools(args)

    assert called == [args]
