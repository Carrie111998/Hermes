"""End-to-end egress gates for webhook credential material."""
from __future__ import annotations

import json

import pytest

from hermes_cli.config import (
    atomic_config_write,
    get_config_value,
    set_config_value,
)


SENTINEL = "WR_SENTINEL_WEBHOOK_SECRET_31c97a"


def test_atomic_config_guard_rejects_plaintext_and_preserves_existing_bytes(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    original = "display:\n  skin: default\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot be stored"):
        atomic_config_write(
            path,
            {
                "platforms": {
                    "webhook": {
                        "extra": {"routes": {"alerts": {"secret": SENTINEL}}}
                    }
                }
            },
            sort_keys=False,
        )

    assert path.read_text(encoding="utf-8") == original


def test_atomic_config_guard_rejects_empty_legacy_secret_fields(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    original = "{}\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot be stored"):
        atomic_config_write(
            path,
            {"platforms": {"webhook": {"extra": {"secret": ""}}}},
            sort_keys=False,
        )

    assert path.read_text(encoding="utf-8") == original


def test_config_set_rejects_plaintext_but_allows_reference(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot be stored"):
        set_config_value("platforms.webhook.extra.routes.alerts.secret", SENTINEL)

    set_config_value(
        "platforms.webhook.extra.routes.alerts.secret_ref",
        "WEBHOOK_ROUTE_ALERTS",
        force=True,
    )
    text = (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert SENTINEL not in text
    assert "WEBHOOK_ROUTE_ALERTS" in text


def test_webhook_route_env_values_are_masked_on_config_get(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("{}\n", encoding="utf-8")

    set_config_value("WEBHOOK_ROUTE_ALERTS", SENTINEL)
    capsys.readouterr()
    get_config_value("WEBHOOK_ROUTE_ALERTS")
    output = capsys.readouterr().out

    assert SENTINEL not in output
    assert "..." in output
    assert SENTINEL not in (tmp_path / "config.yaml").read_text(encoding="utf-8")
    assert SENTINEL in (tmp_path / ".env").read_text(encoding="utf-8")


def test_value_free_receipt_can_be_serialized_without_sentinel(tmp_path):
    from hermes_cli.migrations.webhook_secret_refs import migrate_webhook_routes

    source = tmp_path / "webhook_subscriptions.json"
    source.write_text(json.dumps({"alerts": {"secret": SENTINEL}}), encoding="utf-8")
    stored = {}
    receipt = migrate_webhook_routes(
        source,
        store=stored.__setitem__,
        resolve=stored.get,
    )
    assert SENTINEL not in json.dumps(receipt)
