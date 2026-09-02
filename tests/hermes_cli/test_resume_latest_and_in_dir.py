"""Tests for `--resume latest` and `--in DIR` launch sugar.

`hermes --tui --resume latest --in ./dir` (and the classic-CLI equivalents)
resolve "latest" through the same workspace-scoped MRU lookup as `-c`, with
`--in` re-homing the process before any session resolution happens.
"""

from __future__ import annotations

from argparse import Namespace

import pytest


def _args(**overrides):
    base = {
        "cli": False,
        "continue_last": None,
        "in_dir": None,
        "model": None,
        "no_restore_cwd": False,
        "provider": None,
        "query": None,
        "resume": None,
        "safe_mode": False,
        "toolsets": None,
        "tui": True,
        "tui_dev": False,
        "worktree": False,
    }
    base.update(overrides)
    return Namespace(**base)


@pytest.fixture
def main_mod(monkeypatch):
    import hermes_cli.main as mod

    monkeypatch.setattr(mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(mod, "_sync_bundled_skills_for_startup", lambda: False)
    monkeypatch.setattr(mod, "_pin_kanban_board_env", lambda: None)
    return mod


@pytest.fixture
def launched(main_mod, monkeypatch):
    """Capture the _launch_tui call instead of exec'ing Node."""
    captured = {}

    def fake_launch(resume_session_id=None, **kwargs):
        captured["resume"] = resume_session_id
        captured.update(kwargs)
        raise SystemExit(0)

    monkeypatch.setattr(main_mod, "_launch_tui", fake_launch)
    return captured


# ---------------------------------------------------------------------------
# argparse surface
# ---------------------------------------------------------------------------


def test_top_level_parser_accepts_in_and_resume_latest():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()
    args = parser.parse_args(["--tui", "--resume", "latest", "--in", "./dir"])
    assert args.tui is True
    assert args.resume == "latest"
    assert args.in_dir == "./dir"


def test_chat_subparser_accepts_in_flag():
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat = build_top_level_parser()
    args = parser.parse_args(["chat", "--in", "/tmp", "--resume", "latest"])
    assert args.in_dir == "/tmp"
    assert args.resume == "latest"


def test_top_level_in_value_not_mistaken_for_subcommand(monkeypatch):
    # `hermes --in chat` — "chat" is the flag's value, not the subcommand.
    import sys

    import hermes_cli.main as mod

    monkeypatch.setattr(sys, "argv", ["hermes", "--in", "chat", "--resume", "latest"])
    assert mod._first_positional_argv() is None


# ---------------------------------------------------------------------------
# --resume latest resolution
# ---------------------------------------------------------------------------


def test_resume_latest_resolves_to_mru_session(main_mod, launched, monkeypatch):
    monkeypatch.setattr(
        main_mod, "_resolve_last_session", lambda source="cli": "20260807_120000_abc123"
    )
    # Keyword must NOT fall through to title resolution.
    monkeypatch.setattr(
        main_mod,
        "_resolve_session_by_name_or_id",
        lambda val: val if val != "latest" else pytest.fail("'latest' hit title resolution"),
    )

    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(_args(resume="latest"))
    assert exc.value.code == 0
    assert launched["resume"] == "20260807_120000_abc123"


def test_resume_latest_tui_falls_back_to_cli_source(main_mod, launched, monkeypatch):
    calls = []

    def fake_resolve(source="cli"):
        calls.append(source)
        return "cli_session_1" if source == "cli" else None

    monkeypatch.setattr(main_mod, "_resolve_last_session", fake_resolve)
    monkeypatch.setattr(main_mod, "_resolve_session_by_name_or_id", lambda v: v)

    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(_args(resume="latest"))
    assert exc.value.code == 0
    assert calls == ["tui", "cli"]
    assert launched["resume"] == "cli_session_1"


def test_resume_latest_is_case_insensitive(main_mod, launched, monkeypatch):
    monkeypatch.setattr(main_mod, "_resolve_last_session", lambda source="cli": "sess_1")
    monkeypatch.setattr(main_mod, "_resolve_session_by_name_or_id", lambda v: v)

    with pytest.raises(SystemExit):
        main_mod.cmd_chat(_args(resume="Latest"))
    assert launched["resume"] == "sess_1"


def test_resume_latest_no_sessions_exits_with_error(main_mod, monkeypatch, capsys):
    monkeypatch.setattr(main_mod, "_resolve_last_session", lambda source="cli": None)

    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(_args(resume="latest"))
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "No previous TUI session found" in out


def test_resume_real_id_untouched_by_latest_keyword(main_mod, launched, monkeypatch):
    monkeypatch.setattr(
        main_mod,
        "_resolve_last_session",
        lambda source="cli": pytest.fail("MRU lookup must not run for explicit IDs"),
    )
    monkeypatch.setattr(main_mod, "_resolve_session_by_name_or_id", lambda v: v)

    with pytest.raises(SystemExit):
        main_mod.cmd_chat(_args(resume="20260807_120000_abc123"))
    assert launched["resume"] == "20260807_120000_abc123"


# ---------------------------------------------------------------------------
# --in DIR
# ---------------------------------------------------------------------------


def test_in_dir_chdirs_before_session_resolution(main_mod, launched, monkeypatch, tmp_path):
    import os

    target = tmp_path / "projdir"
    target.mkdir()
    start = os.getcwd()
    seen_cwd = {}

    def fake_resolve(source="cli"):
        seen_cwd["at_resolve"] = os.getcwd()
        return "sess_scoped"

    monkeypatch.setattr(main_mod, "_resolve_last_session", fake_resolve)
    monkeypatch.setattr(main_mod, "_resolve_session_by_name_or_id", lambda v: v)

    try:
        with pytest.raises(SystemExit):
            main_mod.cmd_chat(_args(resume="latest", in_dir=str(target)))
    finally:
        os.chdir(start)

    assert seen_cwd["at_resolve"] == str(target.resolve())
    assert launched["resume"] == "sess_scoped"


def test_in_dir_sets_no_restore_cwd(main_mod, launched, monkeypatch, tmp_path):
    import os

    target = tmp_path / "pin-here"
    target.mkdir()
    start = os.getcwd()

    args = _args(resume=None, in_dir=str(target))
    try:
        with pytest.raises(SystemExit):
            main_mod.cmd_chat(args)
    finally:
        os.chdir(start)

    assert args.no_restore_cwd is True


def test_in_dir_missing_directory_exits(main_mod, monkeypatch, tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main_mod.cmd_chat(_args(in_dir=str(tmp_path / "nope")))
    assert exc.value.code == 1
    assert "--in directory not found" in capsys.readouterr().out


def test_in_dir_expands_user_home(main_mod, launched, monkeypatch, tmp_path):
    import os

    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    start = os.getcwd()

    try:
        with pytest.raises(SystemExit):
            main_mod.cmd_chat(_args(in_dir="~/proj"))
        assert os.getcwd() == str((home / "proj").resolve())
    finally:
        os.chdir(start)


# ---------------------------------------------------------------------------
# --in DIR for one-shot (-z)
# ---------------------------------------------------------------------------


@pytest.fixture
def oneshot_harness(main_mod, monkeypatch):
    """Drive _run_and_exit_oneshot without os._exit() or a real agent."""
    seen = {}

    def fake_run_oneshot(prompt, **kwargs):
        import os

        seen["cwd"] = os.getcwd()
        seen["prompt"] = prompt
        seen["kwargs"] = kwargs
        return 0

    # _run_and_exit_oneshot imports this lazily, inside the call.
    monkeypatch.setattr("hermes_cli.oneshot.run_oneshot", fake_run_oneshot)
    monkeypatch.setattr(main_mod, "_cleanup_oneshot_runtime", lambda: None)

    def fake_exit(rc):
        raise SystemExit(rc)

    monkeypatch.setattr(main_mod, "_exit_after_oneshot", fake_exit)
    return seen


def test_oneshot_in_dir_chdirs_before_agent_runs(main_mod, oneshot_harness, tmp_path):
    """`hermes -z ... --in WS` must run with WS as the process cwd.

    Regression: --in was consumed only by cmd_chat, so a one-shot parsed the
    flag and silently ignored it — "create SUMMARY.md" landed in the
    launcher's cwd (the repo root) instead of the assigned workspace.
    """
    import os

    target = tmp_path / "workspace"
    target.mkdir()
    start = os.getcwd()

    try:
        with pytest.raises(SystemExit) as exc:
            main_mod._run_and_exit_oneshot("create SUMMARY.md", in_dir=str(target))
    finally:
        os.chdir(start)

    assert exc.value.code == 0
    assert oneshot_harness["cwd"] == str(target.resolve())


def test_oneshot_without_in_dir_keeps_process_cwd(main_mod, oneshot_harness):
    import os

    start = os.getcwd()
    with pytest.raises(SystemExit) as exc:
        main_mod._run_and_exit_oneshot("hello")

    assert exc.value.code == 0
    assert oneshot_harness["cwd"] == start


def test_oneshot_in_dir_missing_directory_exits_1_on_stderr(
    main_mod, oneshot_harness, tmp_path, capsys
):
    """A bad --in fails the run, and the message must not pollute -z's stdout."""
    with pytest.raises(SystemExit) as exc:
        main_mod._run_and_exit_oneshot("x", in_dir=str(tmp_path / "nope"))

    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert "--in directory not found" in captured.err
    assert captured.out == ""
    assert "cwd" not in oneshot_harness  # the agent never ran


def test_every_oneshot_dispatch_site_forwards_in_dir():
    """Every `-z` dispatch site must forward --in.

    There are three (fast-CLI, Termux fast-CLI, full dispatch); a new one that
    forgets the kwarg silently reintroduces the wrong-workspace bug.
    """
    import inspect

    import hermes_cli.main as mod

    # Split on the call marker; a dispatch site is the one whose first
    # argument is ``args.oneshot`` (the helper's own def line is not).
    chunks = inspect.getsource(mod).split("_run_and_exit_oneshot(")[1:]
    sites = [c for c in chunks if c.lstrip().startswith("args.oneshot,")]
    assert sites, "no -z dispatch sites found"
    # The call ends at the first dedented closing paren.
    missing = [c for c in sites if "in_dir=" not in c.split("        )")[0]]
    assert not missing, f"{len(missing)} of {len(sites)} -z dispatch site(s) drop --in"
