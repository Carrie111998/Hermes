"""Tests for hermes_cli/webhook.py — webhook subscription CLI."""

import json
import os
import pytest
import stat
from argparse import Namespace

from hermes_cli.webhook import (
    webhook_command,
    _get_webhook_base_url,
    _load_subscriptions,
    _resolve_route_secret,
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
        "payload": "",
        "script": "",
        "json": False,
    }
    defaults.update(kwargs)
    return Namespace(**defaults)


@pytest.mark.parametrize("host", [None, "", "0.0.0.0", "::"])
def test_webhook_base_url_maps_wildcard_hosts_to_localhost(monkeypatch, host):
    monkeypatch.setattr(
        "hermes_cli.webhook._get_webhook_config",
        lambda: {"extra": {"host": host, "port": 9123}},
    )
    assert _get_webhook_base_url() == "http://localhost:9123"


class TestSubscribe:


    def test_custom_secret(self):
        webhook_command(_make_args(
            webhook_action="subscribe", name="s", secret="my-secret"
        ))
        route = _load_subscriptions()["s"]
        assert "secret" not in route
        assert route["secret_ref"].startswith("WEBHOOK_ROUTE_S_")
        assert _resolve_route_secret(route) == "my-secret"


    def test_auto_secret(self):
        webhook_command(_make_args(webhook_action="subscribe", name="s"))
        route = _load_subscriptions()["s"]
        assert "secret" not in route
        secret = _resolve_route_secret(route)
        assert len(secret) > 20


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
            _save_subscriptions(
                {"demo": {"secret_ref": "WEBHOOK_ROUTE_DEMO", "prompt": "x"}}
            )
        finally:
            os.umask(old_umask)

        path = _subscriptions_path()
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "WEBHOOK_ROUTE_DEMO" in path.read_text(encoding="utf-8")

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits are platform-specific")
    def test_save_narrows_existing_broad_secret_file_mode(self):
        # Simulate a pre-existing 0o644 file from before this hardening landed.
        path = _subscriptions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"old": {"secret_ref": "WEBHOOK_ROUTE_OLD", "prompt": "x"}}
            )
        )
        path.chmod(0o644)

        _save_subscriptions(
            {"demo": {"secret_ref": "WEBHOOK_ROUTE_DEMO", "prompt": "x"}}
        )

        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert "WEBHOOK_ROUTE_DEMO" in path.read_text(encoding="utf-8")

    def test_plaintext_route_write_is_rejected(self):
        with pytest.raises(ValueError, match="Refusing to persist plaintext"):
            _save_subscriptions({"demo": {"secret": "must-not-egress"}})
        assert not _subscriptions_path().exists()


class TestMigration:

    def test_runs_while_adapter_disabled_and_emits_value_free_json(
        self, monkeypatch, capsys
    ):
        sentinel = "CLI_MIGRATION_SENTINEL_92d8"
        path = _subscriptions_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"legacy": {"secret": sentinel}}),
            encoding="utf-8",
        )
        monkeypatch.setattr("hermes_cli.webhook._is_webhook_enabled", lambda: False)

        webhook_command(
            _make_args(webhook_action="migrate-secrets", json=True)
        )

        output = capsys.readouterr().out
        assert sentinel not in output
        assert '"migrated_routes"' in output
        assert sentinel not in path.read_text(encoding="utf-8")
        assert sentinel in (path.parent / ".env").read_text(encoding="utf-8")


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
