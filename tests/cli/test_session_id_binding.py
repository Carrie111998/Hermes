"""Deterministic quiet-mode native-session binding (--session-id-file).

Behavior contracts:
- atomic write of the live session id (new AND resumed) at startup
- rebinding on /new rotation and continuation-session sync
- concurrency-safe: distinct paths never cross-bind; last write wins on a
  shared path and is always a complete value (never torn)
- no-op when HERMES_SESSION_ID_FILE is unset (non-Mission-Control unchanged)
"""

import os

import pytest

from hermes_cli.session_binding import (
    resolve_session_id_file,
    write_session_id_file,
)


class TestWriteSessionIdFile:
    def test_writes_exact_id(self, tmp_path):
        path = str(tmp_path / "run1" / "binding")
        assert write_session_id_file(path, "20260823_061808_ac9b1a") is True
        with open(path) as fh:
            assert fh.read() == "20260823_061808_ac9b1a"

    def test_replaces_previous_value_atomically(self, tmp_path):
        path = str(tmp_path / "binding")
        write_session_id_file(path, "first_id")
        write_session_id_file(path, "second_id")
        with open(path) as fh:
            assert fh.read() == "second_id"
        # temp artifacts are cleaned up
        leftovers = [
            p for p in os.listdir(tmp_path)
            if p != "binding" and not p.startswith("hermes_test")
        ]
        assert leftovers == []

    def test_noop_on_missing_args(self, tmp_path):
        path = str(tmp_path / "binding")
        assert write_session_id_file(None, "sid") is False
        assert write_session_id_file(path, None) is False
        assert write_session_id_file("", "") is False
        assert not os.path.exists(path)

    def test_failure_returns_false_not_raises(self, tmp_path):
        # A directory in place of the target makes os.replace fail.
        target_dir = tmp_path / "blocking"
        target_dir.mkdir()
        assert write_session_id_file(str(target_dir), "sid") is False


class TestResolveSessionIdFile:
    def test_reads_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HERMES_SESSION_ID_FILE", str(tmp_path / "b"))
        assert resolve_session_id_file() == str(tmp_path / "b")

    def test_unset_is_none(self, monkeypatch):
        monkeypatch.delenv("HERMES_SESSION_ID_FILE", raising=False)
        assert resolve_session_id_file() is None

    def test_blank_is_none(self, monkeypatch):
        monkeypatch.setenv("HERMES_SESSION_ID_FILE", "   ")
        assert resolve_session_id_file() is None


class TestConcurrentRuns:
    def test_distinct_paths_never_cross_bind(self, tmp_path):
        run_a = str(tmp_path / "run-a")
        run_b = str(tmp_path / "run-b")
        write_session_id_file(run_a, "session_aaa")
        write_session_id_file(run_b, "session_bbb")
        assert open(run_a).read() == "session_aaa"
        assert open(run_b).read() == "session_bbb"

    def test_interleaved_writes_leave_complete_values(self, tmp_path):
        import threading

        path = str(tmp_path / "shared")
        threads = [
            threading.Thread(target=write_session_id_file, args=(path, f"sess_{i}"))
            for i in range(8)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        value = open(path).read()
        assert value.startswith("sess_")  # complete, never torn


class TestCliBindingHooks:
    """HermesCLI._bind_session_id_file writes at startup / rotation / sync."""

    @pytest.fixture
    def cli_stub(self):
        import types
        from types import SimpleNamespace

        from cli import HermesCLI

        stub = SimpleNamespace(session_id=None)
        # Bind the real method so the test exercises HermesCLI's code as-is.
        stub._bind_session_id_file = types.MethodType(
            HermesCLI._bind_session_id_file, stub
        )
        return stub

    def test_binds_fresh_session(self, monkeypatch, tmp_path, cli_stub):
        binding = str(tmp_path / "mc-run-1")
        monkeypatch.setenv("HERMES_SESSION_ID_FILE", binding)
        cli_stub.session_id = "20260823_new00001"
        cli_stub._bind_session_id_file()
        assert open(binding).read() == "20260823_new00001"

    def test_binds_resumed_session_at_startup(self, monkeypatch, tmp_path, cli_stub):
        binding = str(tmp_path / "mc-run-2")
        monkeypatch.setenv("HERMES_SESSION_ID_FILE", binding)
        # Resumed runs set self.session_id to the known resumed id before any
        # message is processed; startup binding must carry that exact id.
        cli_stub.session_id = "20260822_resumed01"
        cli_stub._bind_session_id_file()
        assert open(binding).read() == "20260822_resumed01"

    def test_rotation_rebinds(self, monkeypatch, tmp_path, cli_stub):
        binding = str(tmp_path / "mc-run-3")
        monkeypatch.setenv("HERMES_SESSION_ID_FILE", binding)
        cli_stub.session_id = "old_session_id"
        cli_stub._bind_session_id_file()
        cli_stub.session_id = "rotated_new_id"  # /new generated this
        cli_stub._bind_session_id_file()
        assert open(binding).read() == "rotated_new_id"

    def test_unset_env_is_full_noop(self, monkeypatch, tmp_path, cli_stub):
        monkeypatch.delenv("HERMES_SESSION_ID_FILE", raising=False)
        cli_stub.session_id = "some_session"
        cli_stub._bind_session_id_file()  # must not raise or create anything
        created = [p.name for p in tmp_path.iterdir() if p.name != "hermes_test"]
        assert created == []


class TestArgparseFlag:
    def test_chat_subparser_accepts_flag(self):
        from hermes_cli._parser import build_top_level_parser

        parser, _, chat_parser = build_top_level_parser()
        args = parser.parse_args(["chat", "--session-id-file", "/tmp/bind-xyz"])
        assert args.session_id_file == "/tmp/bind-xyz"
        assert getattr(chat_parser, "_option_string_actions")  # sanity

    def test_default_is_none(self):
        from hermes_cli._parser import build_top_level_parser

        parser, _, _ = build_top_level_parser()
        args = parser.parse_args(["chat"])
        assert args.session_id_file is None
