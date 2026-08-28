"""Focused behavior contracts for sharded webhook CLI management."""

from __future__ import annotations

import argparse
import json
import os
from argparse import Namespace

import pytest

from hermes_cli.subcommands.webhook import build_webhook_parser
from hermes_cli.webhook import (
    ConcurrentWebhookUpdateError,
    _load_subscriptions,
    _save_subscriptions,
    _subscriptions_path,
    webhook_command,
)


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_cli.webhook._is_webhook_enabled", lambda: True)


def _args(**overrides) -> Namespace:
    values = {
        "webhook_action": "subscribe",
        "name": "events",
        "prompt": "",
        "events": "",
        "provider": "generic",
        "signature_mode": "generic_v2",
        "route_profile": "",
        "profile": "",
        "description": "",
        "skills": "",
        "deliver": "log",
        "deliver_chat_id": "",
        "secret": "test-secret",
        "secret_fd": None,
        "deliver_only": False,
        "script": "",
        "replace": False,
        "json": False,
        "payload": "",
    }
    values.update(overrides)
    return Namespace(**values)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="hermes")
    build_webhook_parser(
        parser.add_subparsers(dest="command"),
        cmd_webhook=webhook_command,
    )
    return parser


def test_parser_exposes_management_profile_json_replace_and_secret_fd():
    parsed = _parser().parse_args([
        "webhook",
        "subscribe",
        "events",
        "--profile",
        "ops",
        "--replace",
        "--json",
        "--secret-fd",
        "3",
    ])

    assert parsed.profile == "ops"
    assert parsed.replace is True
    assert parsed.json is True
    assert parsed.secret_fd == 3


def test_parser_rejects_secret_and_secret_fd_without_echoing_value(capsys):
    sentinel = "never-echo-this-secret"
    with pytest.raises(SystemExit):
        _parser().parse_args([
            "webhook",
            "subscribe",
            "events",
            "--secret",
            sentinel,
            "--secret-fd",
            "3",
        ])

    assert sentinel not in capsys.readouterr().err


def test_subscribe_conflict_requires_replace_and_preserves_existing(capsys):
    webhook_command(_args(secret="first-secret"))
    capsys.readouterr()

    webhook_command(_args(secret="second-secret"))

    output = capsys.readouterr().out
    assert "already exists" in output
    assert "second-secret" not in output
    assert _load_subscriptions()["events"]["secret"] == "first-secret"

    webhook_command(_args(secret="second-secret", replace=True))
    assert _load_subscriptions()["events"]["secret"] == "second-secret"


def test_secret_fd_is_bounded_trimmed_left_open_and_never_echoed(tmp_path, capsys):
    sentinel = "fd-secret-value"
    path = tmp_path / "secret"
    path.write_text(sentinel + "\n", encoding="utf-8")
    with path.open("rb") as stream:
        webhook_command(_args(name="fd-route", secret="", secret_fd=stream.fileno()))
        os.fstat(stream.fileno())

    output = capsys.readouterr().out
    assert sentinel not in output
    assert _load_subscriptions()["fd-route"]["secret"] == sentinel


def test_secret_fd_accepts_4096_bytes_and_rejects_4097(tmp_path, capsys):
    maximum = tmp_path / "maximum"
    maximum.write_bytes(b"x" * 4096)
    with maximum.open("rb") as stream:
        webhook_command(_args(name="maximum", secret="", secret_fd=stream.fileno()))
    assert len(_load_subscriptions()["maximum"]["secret"]) == 4096
    assert "x" * 32 not in capsys.readouterr().out

    oversize = tmp_path / "oversize"
    oversize.write_bytes(b"y" * 4097)
    with oversize.open("rb") as stream:
        webhook_command(_args(name="oversize", secret="", secret_fd=stream.fileno()))
    assert "oversize" not in _load_subscriptions()
    assert "y" * 32 not in capsys.readouterr().out


def test_subscribe_list_and_show_json_are_secret_safe(capsys):
    sentinel = "json-secret-sentinel"
    webhook_command(_args(secret=sentinel, json=True))
    created = json.loads(capsys.readouterr().out)
    assert created["name"] == "events"
    assert created["secret_set"] is True
    assert created["secret_masked"] == "***"
    assert sentinel not in json.dumps(created)

    webhook_command(_args(webhook_action="list", json=True))
    listed = json.loads(capsys.readouterr().out)
    assert listed[0]["name"] == "events"
    assert sentinel not in json.dumps(listed)

    webhook_command(_args(webhook_action="show", json=True))
    shown = json.loads(capsys.readouterr().out)
    assert shown["name"] == "events"
    assert sentinel not in json.dumps(shown)


def test_update_preserves_unknown_fields_and_canonical_delivery():
    webhook_command(_args())
    path = _subscriptions_path()
    persisted = json.loads(path.read_text(encoding="utf-8"))
    persisted["events"]["future_policy"] = {"mode": "strict"}
    path.write_text(json.dumps(persisted), encoding="utf-8")

    webhook_command(
        _args(
            webhook_action="update",
            prompt="new prompt",
            events="deploy,build",
            deliver="slack",
            deliver_chat_id="C123",
        )
    )

    route = _load_subscriptions()["events"]
    assert route["prompt"] == "new prompt"
    assert route["events"] == ["deploy", "build"]
    assert route["future_policy"] == {"mode": "strict"}
    assert route["deliveries"][0] == {"target": "slack", "chat_id": "C123"}


def test_enable_disable_rotate_show_and_remove_lifecycle(capsys):
    webhook_command(_args(secret="old-secret"))
    capsys.readouterr()

    webhook_command(_args(webhook_action="disable"))
    assert _load_subscriptions()["events"]["enabled"] is False
    webhook_command(_args(webhook_action="enable"))
    assert _load_subscriptions()["events"]["enabled"] is True

    capsys.readouterr()
    webhook_command(_args(webhook_action="rotate-secret"))
    rotated_output = capsys.readouterr().out
    new_secret = _load_subscriptions()["events"]["secret"]
    assert new_secret != "old-secret"
    assert new_secret in rotated_output

    webhook_command(_args(webhook_action="show"))
    shown = capsys.readouterr().out
    assert new_secret not in shown
    assert "Secret:   ***" in shown

    webhook_command(_args(webhook_action="remove"))
    assert "events" not in _load_subscriptions()


def test_profile_selection_shards_storage_and_urls(capsys):
    webhook_command(_args(profile="ops"))

    output = capsys.readouterr().out
    assert "/p/ops/webhooks/events" in output
    assert "events" not in _load_subscriptions()
    assert _load_subscriptions("ops")["events"]["profile"] == "ops"
    assert _subscriptions_path("ops").parent.name == "ops"

    webhook_command(_args(webhook_action="remove", profile="ops"))
    assert "events" not in _load_subscriptions("ops")


def test_corrupt_store_fails_closed_without_consuming_secret_fd(tmp_path, capsys):
    corrupt = b"{broken-store"
    path = _subscriptions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(corrupt)
    secret_path = tmp_path / "secret-input"
    secret_path.write_text("unconsumed", encoding="utf-8")

    with secret_path.open("rb") as stream:
        webhook_command(_args(secret="", secret_fd=stream.fileno()))
        assert stream.tell() == 0

    output = capsys.readouterr().out
    assert "No changes were made" in output
    assert path.read_bytes() == corrupt


def _compat_route(secret: str, *, prompt: str = "") -> dict:
    return {
        "profile": "default",
        "provider": "generic",
        "signature_mode": "generic_v2",
        "secret": secret,
        "prompt": prompt,
        "deliver": "log",
        "deliver_extra": {},
    }


def test_compat_snapshots_merge_independent_concurrent_additions():
    first = _load_subscriptions()
    second = _load_subscriptions()
    first["alpha"] = _compat_route("alpha-secret")
    second["beta"] = _compat_route("beta-secret")

    _save_subscriptions(first)
    _save_subscriptions(second)

    assert set(_load_subscriptions()) == {"alpha", "beta"}


def test_compat_snapshot_rejects_divergent_same_route_update():
    initial = _load_subscriptions()
    initial["shared"] = _compat_route("shared-secret", prompt="before")
    _save_subscriptions(initial)
    first = _load_subscriptions()
    second = _load_subscriptions()
    first["shared"]["prompt"] = "writer-one"
    second["shared"]["prompt"] = "writer-two"

    _save_subscriptions(first)
    with pytest.raises(ConcurrentWebhookUpdateError, match="shared"):
        _save_subscriptions(second)

    assert _load_subscriptions()["shared"]["prompt"] == "writer-one"


def test_compat_snapshot_delete_preserves_unrelated_concurrent_addition():
    initial = _load_subscriptions()
    initial["old"] = _compat_route("old-secret")
    _save_subscriptions(initial)
    deleter = _load_subscriptions()
    adder = _load_subscriptions()
    del deleter["old"]
    adder["new"] = _compat_route("new-secret")

    _save_subscriptions(adder)
    _save_subscriptions(deleter)

    assert set(_load_subscriptions()) == {"new"}
