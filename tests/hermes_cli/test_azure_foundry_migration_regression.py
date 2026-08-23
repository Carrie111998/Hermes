"""Update/migration regression for Azure Foundry Entra routing."""

from __future__ import annotations

from pathlib import Path

import yaml


def test_migration_preserves_entra_route_and_unrelated_configuration(
    monkeypatch, tmp_path
):
    """A schema version bump must retain the accepted routing semantics."""
    hermes_home = tmp_path / "hermes"
    hermes_home.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "_config_version": 37,
                "model": {
                    "provider": "azure-foundry",
                    "default": "deployment-a",
                    "auth_mode": "entra_id",
                    "base_url": "https://accepted.example.invalid/models/v1",
                    "api_mode": "codex_responses",
                },
                "unrelated_extension": {"preserved": "yes"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    (hermes_home / ".env").write_text("", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))

    from hermes_cli.config import DEFAULT_CONFIG, migrate_config

    migrate_config(interactive=False, quiet=True)
    migrated = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    assert migrated["_config_version"] == DEFAULT_CONFIG["_config_version"]
    assert migrated["model"] == {
        "provider": "azure-foundry",
        "default": "deployment-a",
        "auth_mode": "entra_id",
        "base_url": "https://accepted.example.invalid/models/v1",
        "api_mode": "codex_responses",
    }
    assert "fallback" not in migrated
    assert migrated["unrelated_extension"] == {"preserved": "yes"}
