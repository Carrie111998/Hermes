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

    def test_missing_query_file_rejected(self, monkeypatch, capsys, tmp_path):
        missing = tmp_path / "does-not-exist.txt"
        args = _build_chat_args(["chat", "--query-file", str(missing)])
        with pytest.raises(SystemExit) as exc_info:
            _run_cmd_chat(monkeypatch, args, {})
        assert exc_info.value.code == 2
        assert "cannot read" in capsys.readouterr().out
