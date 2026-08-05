"""Tests for `hermes chat --query-file`.

The flag reads the single query from a file instead of `-q/--query`.
It exists for very large prompts (e.g. harness-dispatch runs) that would
exceed the kernel's per-argv limit (MAX_ARG_STRLEN ~128KiB) when passed
inline on the command line.
"""

import sys
import types

import pytest


def _build_chat_args(argv):
    """Parse argv with the real parser; returns the parsed namespace."""
    from hermes_cli._parser import build_top_level_parser

    parser, _subparsers, _chat_parser = build_top_level_parser()
    return parser.parse_args(argv)


def _run_cmd_chat(monkeypatch, args, captured):
    """Invoke cmd_chat with heavy side effects stubbed.

    Mirrors the pattern in tests/hermes_cli/test_argparse_flag_propagation.py
    (fake `cli` module + banner + skills_sync + provider/kanban stubs) so the
    test exercises the real cmd_chat query handling without a full runtime.
    """
    import hermes_cli.main as main_mod

    fake_cli = types.ModuleType("cli")

    def fake_main(**kwargs):
        captured.update(kwargs)

    setattr(fake_cli, "main", fake_main)
    fake_banner = types.ModuleType("hermes_cli.banner")
    setattr(fake_banner, "prefetch_update_check", lambda: None)
    fake_skills_sync = types.ModuleType("tools.skills_sync")
    setattr(fake_skills_sync, "sync_skills", lambda quiet=True: None)

    monkeypatch.setitem(sys.modules, "cli", fake_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.banner", fake_banner)
    monkeypatch.setitem(sys.modules, "tools.skills_sync", fake_skills_sync)
    monkeypatch.setattr(main_mod, "_has_any_provider_configured", lambda: True)
    monkeypatch.setattr(main_mod, "_pin_kanban_board_env", lambda: None)

    main_mod.cmd_chat(args)


class TestQueryFileParsing:
    """Parser-level behaviour."""

    def test_parser_accepts_query_file(self):
        args = _build_chat_args(["chat", "--query-file", "/tmp/query.txt"])
        assert args.query_file == "/tmp/query.txt"
        assert args.query is None

    def test_parser_accepts_query_and_query_file_together(self):
        # The parser accepts both flags; cmd_chat rejects the combination.
        args = _build_chat_args(
            ["chat", "-q", "inline", "--query-file", "/tmp/query.txt"]
        )
        assert args.query == "inline"
        assert args.query_file == "/tmp/query.txt"

    def test_parent_parser_does_not_accept_query_file(self):
        # The flag belongs to the chat subparser, not the root parser.
        from hermes_cli._parser import build_top_level_parser

        parser, _subparsers, _chat_parser = build_top_level_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["--query-file", "/tmp/query.txt", "chat"])


class TestCmdChatQueryFile:
    """cmd_chat behaviour with the real parser wiring."""

    def test_query_file_content_becomes_query(self, monkeypatch, tmp_path):
        query_file = tmp_path / "query.txt"
        query_file.write_text("large hardening prompt body", encoding="utf-8")
        args = _build_chat_args(["chat", "--query-file", str(query_file)])
        captured = {}
        _run_cmd_chat(monkeypatch, args, captured)
        assert captured["query"] == "large hardening prompt body"

    def test_large_query_file_round_trips_untouched(self, monkeypatch, tmp_path):
        # Guards the actual regression: a prompt far above the kernel's
        # per-argv limit must survive the file round trip byte-for-byte.
        content = ("x" * 200_000) + "\nwith a tail marker\n"
        query_file = tmp_path / "big.txt"
        query_file.write_text(content, encoding="utf-8")
        args = _build_chat_args(["chat", "--query-file", str(query_file)])
        captured = {}
        _run_cmd_chat(monkeypatch, args, captured)
        assert captured["query"] == content

    def test_query_flag_unchanged_without_query_file(self, monkeypatch, capsys):
        args = _build_chat_args(["chat", "-q", "inline query"])
        captured = {}
        _run_cmd_chat(monkeypatch, args, captured)
        assert captured["query"] == "inline query"
        assert "query-file" not in capsys.readouterr().out

    def test_both_query_and_query_file_rejected(self, monkeypatch, capsys, tmp_path):
        query_file = tmp_path / "query.txt"
        query_file.write_text("x", encoding="utf-8")
        args = _build_chat_args(
            ["chat", "-q", "inline", "--query-file", str(query_file)]
        )
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_chat(monkeypatch, args, {})
        assert exc_info.value.code == 2
        assert "not both" in capsys.readouterr().out

    def test_empty_query_flag_and_query_file_rejected(
        self, monkeypatch, capsys, tmp_path
    ):
        # `-q ""` is still a supplied query; the file must not silently win.
        query_file = tmp_path / "query.txt"
        query_file.write_text("x", encoding="utf-8")
        args = _build_chat_args(
            ["chat", "-q", "", "--query-file", str(query_file)]
        )
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_chat(monkeypatch, args, {})
        assert exc_info.value.code == 2
        assert "not both" in capsys.readouterr().out

    def test_missing_query_file_rejected(self, monkeypatch, capsys, tmp_path):
        missing = tmp_path / "does-not-exist.txt"
        args = _build_chat_args(["chat", "--query-file", str(missing)])
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_chat(monkeypatch, args, {})
        assert exc_info.value.code == 2
        assert "cannot read" in capsys.readouterr().out

    def test_invalid_utf8_query_file_rejected(self, monkeypatch, capsys, tmp_path):
        query_file = tmp_path / "bad.txt"
        query_file.write_bytes(b"\xff\xfe\x00")
        args = _build_chat_args(["chat", "--query-file", str(query_file)])
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_chat(monkeypatch, args, {})
        assert exc_info.value.code == 2
        assert "not valid UTF-8" in capsys.readouterr().out

    def test_empty_query_file_rejected(self, monkeypatch, capsys, tmp_path):
        query_file = tmp_path / "empty.txt"
        query_file.write_text("", encoding="utf-8")
        args = _build_chat_args(["chat", "--query-file", str(query_file)])
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_chat(monkeypatch, args, {})
        assert exc_info.value.code == 2
        assert "is empty" in capsys.readouterr().out

    def test_whitespace_only_query_file_rejected(self, monkeypatch, capsys, tmp_path):
        query_file = tmp_path / "blank.txt"
        query_file.write_text(" \n\t ", encoding="utf-8")
        args = _build_chat_args(["chat", "--query-file", str(query_file)])
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_chat(monkeypatch, args, {})
        assert exc_info.value.code == 2
        assert "is empty" in capsys.readouterr().out

    def test_oversized_query_file_rejected(self, monkeypatch, capsys, tmp_path):
        import hermes_cli.main as main_mod

        monkeypatch.setattr(main_mod, "_MAX_QUERY_FILE_BYTES", 100)
        query_file = tmp_path / "huge.txt"
        query_file.write_text("x" * 150, encoding="utf-8")
        args = _build_chat_args(["chat", "--query-file", str(query_file)])
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_chat(monkeypatch, args, {})
        assert exc_info.value.code == 2
        assert "exceeds" in capsys.readouterr().out

    def test_multibyte_query_file_checked_in_bytes(
        self, monkeypatch, capsys, tmp_path
    ):
        # 60 x "é" is 120 bytes but only 60 characters: the byte cap must
        # reject it even though the character count is under the limit.
        import hermes_cli.main as main_mod

        monkeypatch.setattr(main_mod, "_MAX_QUERY_FILE_BYTES", 100)
        query_file = tmp_path / "wide.txt"
        query_file.write_text("é" * 60, encoding="utf-8")
        args = _build_chat_args(["chat", "--query-file", str(query_file)])
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_chat(monkeypatch, args, {})
        assert exc_info.value.code == 2
        assert "exceeds" in capsys.readouterr().out

    def test_query_file_with_resume(self, monkeypatch, tmp_path):
        # Query normalization runs before resume resolution; both must land.
        import hermes_cli.main as main_mod

        query_file = tmp_path / "query.txt"
        query_file.write_text("resume turn content", encoding="utf-8")

        fake_state = types.ModuleType("hermes_state")

        class _FakeSessionDB:
            def get_session(self, session_id):
                return None

        setattr(fake_state, "SessionDB", _FakeSessionDB)
        monkeypatch.setitem(sys.modules, "hermes_state", fake_state)
        monkeypatch.setattr(main_mod, "_resolve_session_by_name_or_id", lambda v: None)

        args = _build_chat_args(
            ["chat", "--resume", "sess-abc", "--query-file", str(query_file)]
        )
        captured = {}
        _run_cmd_chat(monkeypatch, args, captured)
        assert captured["query"] == "resume turn content"
        assert captured["resume"] == "sess-abc"


class TestTuiQueryEnv:
    """_apply_tui_query_env keeps large queries out of the TUI env."""

    def test_small_query_rides_env(self):
        import hermes_cli.main as main_mod

        env = {}
        ret = main_mod._apply_tui_query_env(env, "hi there", None)
        assert ret is None
        assert env.get("HERMES_TUI_QUERY") == "hi there"
        assert "HERMES_TUI_QUERY_FILE" not in env

    def test_large_query_rides_file(self):
        import os

        import hermes_cli.main as main_mod

        content = "y" * 150_000
        env = {}
        ret = main_mod._apply_tui_query_env(env, content, None)
        assert ret is not None
        assert "HERMES_TUI_QUERY" not in env
        assert env.get("HERMES_TUI_QUERY_FILE") == ret
        try:
            with open(ret, encoding="utf-8") as fh:
                assert fh.read() == content
        finally:
            os.unlink(ret)

    def test_explicit_query_file_passthrough(self, tmp_path):
        import os

        import hermes_cli.main as main_mod

        query_file = tmp_path / "query.txt"
        query_file.write_text("from file", encoding="utf-8")
        env = {}
        ret = main_mod._apply_tui_query_env(env, "from file", str(query_file))
        assert ret is None
        assert env.get("HERMES_TUI_QUERY_FILE") == os.path.abspath(str(query_file))
        assert "HERMES_TUI_QUERY" not in env

    def test_no_query_no_env(self):
        import hermes_cli.main as main_mod

        env = {}
        ret = main_mod._apply_tui_query_env(env, None, None)
        assert ret is None
        assert env == {}

    def test_stale_query_env_cleared(self):
        # A stale exported value must not win over the current invocation.
        import hermes_cli.main as main_mod

        env = {
            "HERMES_TUI_QUERY": "stale inline",
            "HERMES_TUI_QUERY_FILE": "/stale/path.txt",
        }
        ret = main_mod._apply_tui_query_env(env, "fresh", None)
        assert ret is None
        assert env.get("HERMES_TUI_QUERY") == "fresh"
        assert "HERMES_TUI_QUERY_FILE" not in env

    def test_stale_query_env_cleared_without_query(self):
        import hermes_cli.main as main_mod

        env = {
            "HERMES_TUI_QUERY": "stale inline",
            "HERMES_TUI_QUERY_FILE": "/stale/path.txt",
        }
        ret = main_mod._apply_tui_query_env(env, None, None)
        assert ret is None
        assert env == {}
