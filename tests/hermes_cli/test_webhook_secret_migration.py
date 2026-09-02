"""Behavior contracts for reference-only webhook secret migration."""
from __future__ import annotations

import json
import threading
import time

import pytest
import yaml

from hermes_cli import webhook_secrets
from hermes_cli.migrations.webhook_secret_refs import (
    WebhookSecretMigrationError,
    migrate_webhook_config,
    migrate_webhook_routes,
)


SENTINEL = "WR_SENTINEL_WEBHOOK_SECRET_7f39d8"


def test_pre_switch_failure_preserves_exact_route_source(tmp_path):
    source = tmp_path / "webhook_subscriptions.json"
    original = json.dumps({"alerts": {"secret": SENTINEL}}, indent=2)
    source.write_text(original, encoding="utf-8")

    with pytest.raises(WebhookSecretMigrationError, match="source left untouched"):
        migrate_webhook_routes(
            source,
            store=lambda _ref, _value: (_ for _ in ()).throw(OSError(SENTINEL)),
        )

    assert source.read_text(encoding="utf-8") == original


def test_routes_and_differing_backup_versions_are_all_scrubbed(tmp_path):
    source = tmp_path / "webhook_subscriptions.json"
    backup = tmp_path / "webhook_subscriptions.json.bak"
    source.write_text(
        json.dumps({"alerts": {"secret": SENTINEL, "prompt": "new"}}),
        encoding="utf-8",
    )
    backup.write_text(
        json.dumps({"alerts": {"secret": "older-secret", "prompt": "old"}}),
        encoding="utf-8",
    )
    stored = {}

    result = migrate_webhook_routes(
        source,
        store=stored.__setitem__,
        resolve=stored.get,
        backup_paths=(backup,),
    )

    current = json.loads(source.read_text(encoding="utf-8"))["alerts"]
    prior = json.loads(backup.read_text(encoding="utf-8"))["alerts"]
    assert "secret" not in current
    assert "secret" not in prior
    assert current["secret_ref"] != prior["secret_ref"]
    assert stored[current["secret_ref"]] == SENTINEL
    assert stored[prior["secret_ref"]] == "older-secret"
    assert SENTINEL not in json.dumps(result)
    assert "older-secret" not in json.dumps(result)


def test_generated_route_references_do_not_collide_after_normalization(tmp_path):
    source = tmp_path / "webhook_subscriptions.json"
    source.write_text(
        json.dumps(
            {
                "a-b": {"secret": "first-secret"},
                "a_b": {"secret": "second-secret"},
            }
        ),
        encoding="utf-8",
    )
    stored = {}

    migrate_webhook_routes(source, store=stored.__setitem__, resolve=stored.get)

    routes = json.loads(source.read_text(encoding="utf-8"))
    assert routes["a-b"]["secret_ref"] != routes["a_b"]["secret_ref"]
    assert len(stored) == 2


def test_conflicting_explicit_reference_fails_before_any_store(tmp_path):
    source = tmp_path / "webhook_subscriptions.json"
    original = json.dumps(
        {
            "one": {"secret_ref": "WEBHOOK_SHARED", "secret": "first"},
            "two": {"secret_ref": "WEBHOOK_SHARED", "secret": "second"},
        }
    )
    source.write_text(original, encoding="utf-8")
    writes = []

    with pytest.raises(WebhookSecretMigrationError, match="conflicting"):
        migrate_webhook_routes(
            source,
            store=lambda ref, value: writes.append((ref, value)),
            resolve=lambda _ref: None,
        )

    assert writes == []
    assert source.read_text(encoding="utf-8") == original


def test_config_and_config_backup_are_scrubbed(tmp_path):
    source = tmp_path / "config.yaml"
    backup = tmp_path / "config.yaml.bak"
    config = {
        "platforms": {
            "webhook": {
                "enabled": True,
                "extra": {
                    "secret": SENTINEL,
                    "routes": {"alerts": {"secret_value": "route-secret"}},
                },
            }
        }
    }
    source.write_text(yaml.safe_dump(config), encoding="utf-8")
    backup.write_text(yaml.safe_dump(config), encoding="utf-8")
    stored = {}

    result = migrate_webhook_config(
        source,
        store=stored.__setitem__,
        resolve=stored.get,
        backup_paths=(backup,),
    )

    for path in (source, backup):
        text = path.read_text(encoding="utf-8")
        assert SENTINEL not in text
        assert "route-secret" not in text
        assert "secret_ref:" in text
    assert SENTINEL not in json.dumps(result)
    assert "route-secret" not in json.dumps(result)


def test_config_migrates_historical_top_level_routes_and_removes_empty_fields(
    tmp_path,
):
    source = tmp_path / "config.yaml"
    config = {
        "platforms": {
            "webhook": {
                "secret": "",
                "secret_value": None,
                "routes": {
                    "historical": {"secret": SENTINEL},
                    "empty": {"secret": "", "secret_value": None},
                },
                "extra": {"secret": ""},
            }
        }
    }
    source.write_text(yaml.safe_dump(config), encoding="utf-8")
    stored = {}

    result = migrate_webhook_config(
        source,
        store=stored.__setitem__,
        resolve=stored.get,
    )

    migrated = yaml.safe_load(source.read_text(encoding="utf-8"))
    webhook = migrated["platforms"]["webhook"]
    assert "secret" not in webhook
    assert "secret_value" not in webhook
    assert "secret" not in webhook["extra"]
    assert "secret" not in webhook["routes"]["empty"]
    assert "secret_value" not in webhook["routes"]["empty"]
    ref = webhook["routes"]["historical"]["secret_ref"]
    assert stored[ref] == SENTINEL
    assert result["migrated"] is True


def test_v40_config_migration_evacuates_static_and_dynamic_secrets(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "_config_version": 39,
                "platforms": {
                    "webhook": {
                        "enabled": True,
                        "extra": {
                            "routes": {"shared": {"secret": SENTINEL}}
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    routes_path = tmp_path / "webhook_subscriptions.json"
    routes_path.write_text(
        json.dumps({"shared": {"secret": "dynamic-secret"}}),
        encoding="utf-8",
    )

    from hermes_cli.config import migrate_config

    result = migrate_config(interactive=False, quiet=True)

    config_text = config_path.read_text(encoding="utf-8")
    routes_text = routes_path.read_text(encoding="utf-8")
    env_text = (tmp_path / ".env").read_text(encoding="utf-8")
    assert SENTINEL not in config_text
    assert "dynamic-secret" not in routes_text
    assert "secret_ref" in config_text
    assert "secret_ref" in routes_text
    assert SENTINEL in env_text
    assert "dynamic-secret" in env_text
    migrated_config = yaml.safe_load(config_text)
    static_ref = migrated_config["platforms"]["webhook"]["extra"]["routes"][
        "shared"
    ]["secret_ref"]
    dynamic_ref = json.loads(routes_text)["shared"]["secret_ref"]
    assert static_ref != dynamic_ref
    assert yaml.safe_load(config_text)["_config_version"] == 40
    assert result["env_added"]


def test_kernel_lock_serializes_threads_without_stale_steal(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    active = 0
    maximum = 0
    guard = threading.Lock()

    def fake_save(_key, _value):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.08)
        with guard:
            active -= 1

    monkeypatch.setattr("hermes_cli.config.save_env_value", fake_save)
    monkeypatch.setattr(
        "hermes_cli.config.get_env_value_prefer_dotenv",
        lambda ref: f"value-{ref.rsplit('_', 1)[-1]}",
    )
    threads = [
        threading.Thread(
            target=webhook_secrets.store_webhook_secret,
            args=(f"WEBHOOK_ROUTE_{index}", f"value-{index}"),
        )
        for index in range(4)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert maximum == 1
