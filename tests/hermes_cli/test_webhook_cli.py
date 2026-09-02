"""Tests for hermes_cli/webhook.py — webhook subscription CLI."""

import argparse
import json
import os
import pytest
import stat
from argparse import Namespace

from hermes_cli.subcommands.webhook import build_webhook_parser
from hermes_cli.webhook import (
    webhook_command,
    _get_webhook_base_url,
    _load_subscriptions,
    _save_subscriptions,
    _subscriptions_path,
)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    # Default: webhooks enabled (most tests need this)
    monkeypatch.setattr(
        "hermes_cli.webhook._is_webhook_enabled", lambda: True
    )


def _make_args(**kwargs):
    defaults = {
        "webhook_action": None,
        "name": "",
        "prompt": "",
        "events": "",
        "description": "",
        "skills": "",
        "deliver": "log",
        "deliver_chat_id": "",
        "secret": "",
        "secret_fd": None,
        "payload": "",
        "script": "",
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


def _webhook_parser():
    parser = argparse.ArgumentParser(prog="hermes")
    build_webhook_parser(
        parser.add_subparsers(dest="command"), cmd_webhook=webhook_command
    )
    return parser


@pytest.mark.parametrize("host", [None, "", "0.0.0.0", "::"])
def test_webhook_base_url_maps_wildcard_hosts_to_localhost(monkeypatch, host):
    monkeypatch.setattr(
        "hermes_cli.webhook._get_webhook_config",
        lambda: {"extra": {"host": host, "port": 9123}},
    )
    assert _get_webhook_base_url() == "http://localhost:9123"


class TestSubscribe:


    def test_custom_secret_is_not_echoed(self, capsys):
        secret = "legacy-argv-secret"
        webhook_command(_make_args(
            webhook_action="subscribe", name="s", secret=secret
        ))
        assert _load_subscriptions()["s"]["secret"] == secret
        assert secret not in capsys.readouterr().out


    def test_auto_secret_remains_default_and_is_not_echoed(self, capsys):
        webhook_command(_make_args(webhook_action="subscribe", name="s"))
        secret = _load_subscriptions()["s"]["secret"]
        assert len(secret) > 20
        assert secret not in capsys.readouterr().out

    def test_secret_fd_success_strips_trailing_newline_and_keeps_fd_open(self, capsys):
        read_fd, write_fd = os.pipe()
        secret = b"fd-provided-value"
        try:
            os.write(write_fd, secret + b"\n")
            os.close(write_fd)
            write_fd = -1

            webhook_command(
                _make_args(webhook_action="subscribe", name="fd-route", secret_fd=read_fd)
            )

            assert _load_subscriptions()["fd-route"]["secret"] == secret.decode()
            assert secret.decode() not in capsys.readouterr().out
            os.fstat(read_fd)
        finally:
            if write_fd >= 0:
                os.close(write_fd)
            os.close(read_fd)

    def test_secret_and_secret_fd_are_mutually_exclusive(self, capsys):
        secret = "mutual-exclusion-value"
        with pytest.raises(SystemExit):
            _webhook_parser().parse_args(
                ["webhook", "subscribe", "route", "--secret", secret, "--secret-fd", "3"]
            )
        error = capsys.readouterr().err
        assert "not allowed with argument" in error
        assert secret not in error

    @pytest.mark.parametrize("value", ["-1", "+1", " 1", "1.0", "not-an-integer"])
    def test_secret_fd_rejects_invalid_values(self, value, capsys):
        with pytest.raises(SystemExit):
            _webhook_parser().parse_args(
                ["webhook", "subscribe", "route", "--secret-fd", value]
            )
        assert "--secret-fd" in capsys.readouterr().err

    def test_secret_fd_rejects_closed_fd_without_persisting(self, capsys):
        read_fd, write_fd = os.pipe()
        os.close(read_fd)
        os.close(write_fd)

        webhook_command(
            _make_args(webhook_action="subscribe", name="closed", secret_fd=read_fd)
        )

        assert "closed" not in _load_subscriptions()
        assert capsys.readouterr().out == "Error: Could not read --secret-fd.\n"

    def test_secret_fd_rejects_out_of_platform_range_without_traceback(self, capsys):
        webhook_command(
            _make_args(
                webhook_action="subscribe",
                name="out-of-range",
                secret_fd=1 << 63,
            )
        )

        assert "out-of-range" not in _load_subscriptions()
        assert capsys.readouterr().out == "Error: Could not read --secret-fd.\n"

    def test_secret_fd_rejects_oversize_without_echoing_input(self, tmp_path, capsys):
        secret_file = tmp_path / "oversize-secret"
        secret_file.write_bytes(b"x" * 4097)
        with secret_file.open("rb") as fh:
            webhook_command(
                _make_args(webhook_action="subscribe", name="oversize", secret_fd=fh.fileno())
            )

        assert "oversize" not in _load_subscriptions()
        assert "x" * 32 not in capsys.readouterr().out

    def test_secret_fd_accepts_4096_byte_limit(self, tmp_path, capsys):
        secret = "x" * 4096
        secret_file = tmp_path / "maximum-size-secret"
        secret_file.write_text(secret, encoding="utf-8")
        with secret_file.open("rb") as fh:
            webhook_command(
                _make_args(webhook_action="subscribe", name="maximum", secret_fd=fh.fileno())
            )

        assert _load_subscriptions()["maximum"]["secret"] == secret
        assert secret not in capsys.readouterr().out

    def test_secret_fd_rejects_non_utf8_without_echoing_input(self, tmp_path, capsys):
        secret_file = tmp_path / "malformed-secret"
        secret_file.write_bytes(b"prefix-\xff-suffix")
        with secret_file.open("rb") as fh:
            webhook_command(
                _make_args(webhook_action="subscribe", name="malformed", secret_fd=fh.fileno())
            )

        assert "malformed" not in _load_subscriptions()
        assert "prefix" not in capsys.readouterr().out

    @pytest.mark.parametrize("contents", [b"", b"\n", b" \n\t"])
    def test_secret_fd_rejects_empty_normalized_secret(self, tmp_path, contents, capsys):
        secret_file = tmp_path / "empty-secret"
        secret_file.write_bytes(contents)
        with secret_file.open("rb") as fh:
            webhook_command(
                _make_args(webhook_action="subscribe", name="empty", secret_fd=fh.fileno())
            )

        assert "empty" not in _load_subscriptions()
        assert "is empty after trimming" in capsys.readouterr().out

    def test_secret_fd_call_path_keeps_secret_out_of_argv(self, tmp_path, capsys):
        secret = b"not-present-in-argv"
        secret_file = tmp_path / "argv-free-secret"
        secret_file.write_bytes(secret)
        with secret_file.open("rb") as fh:
            argv = ["webhook", "subscribe", "argv-free", "--secret-fd", str(fh.fileno())]
            assert secret.decode() not in argv
            args = _webhook_parser().parse_args(argv)
            args.func(args)

        assert _load_subscriptions()["argv-free"]["secret"] == secret.decode()
        assert secret.decode() not in capsys.readouterr().out


class TestList:

    def test_with_entries(self, capsys):
        webhook_command(_make_args(webhook_action="subscribe", name="a"))
        webhook_command(_make_args(webhook_action="subscribe", name="b"))
        capsys.readouterr()  # clear
        webhook_command(_make_args(webhook_action="list"))
        out = capsys.readouterr().out
        assert "2 webhook" in out
        assert "a" in out
        assert "b" in out


class TestRemove:


    def test_selective_remove(self):
        webhook_command(_make_args(webhook_action="subscribe", name="keep"))
        webhook_command(_make_args(webhook_action="subscribe", name="drop"))
        webhook_command(_make_args(webhook_action="remove", name="drop"))
        subs = _load_subscriptions()
        assert "keep" in subs
        assert "drop" not in subs


class TestPersistence:

    def test_corrupted_file(self):
        path = _subscriptions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("broken{{{")
        assert _load_subscriptions() == {}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are platform-specific")
    def test_save_creates_secret_file_owner_only_under_permissive_umask(self):
        old_umask = os.umask(0o022)
        try:
            _save_subscriptions({"demo": {"secret": "TOPSECRET", "prompt": "x"}})
        finally:
            os.umask(old_umask)

        path = _subscriptions_path()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "TOPSECRET" in path.read_text(encoding="utf-8")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are platform-specific")
    def test_save_narrows_existing_broad_secret_file_mode(self):
        # Simulate a pre-existing 0o644 file from before this hardening landed.
        path = _subscriptions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"old": {"secret": "stale", "prompt": "x"}}))
        path.chmod(0o644)

        _save_subscriptions({"demo": {"secret": "FRESH", "prompt": "x"}})

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "FRESH" in path.read_text(encoding="utf-8")


class TestWebhookEnabledGate:

    def test_blocks_list_when_disabled(self, capsys, monkeypatch):
        monkeypatch.setattr("hermes_cli.webhook._is_webhook_enabled", lambda: False)
        webhook_command(_make_args(webhook_action="list"))
        out = capsys.readouterr().out
        assert "not enabled" in out.lower()

    def test_allows_when_enabled(self, capsys):
        # _is_webhook_enabled already patched to True by autouse fixture
        webhook_command(_make_args(webhook_action="subscribe", name="allowed"))
        out = capsys.readouterr().out
        assert "Created" in out
        assert "allowed" in _load_subscriptions()

    def test_real_check_disabled(self, monkeypatch):
        monkeypatch.setattr(
            "hermes_cli.webhook._get_webhook_config",
            lambda: {},
        )
        monkeypatch.setattr(
            "hermes_cli.webhook._is_webhook_enabled",
            lambda: bool({}.get("enabled")),
        )
        import hermes_cli.webhook as wh_mod
        assert wh_mod._is_webhook_enabled() is False

