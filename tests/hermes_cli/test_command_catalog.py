from __future__ import annotations

import pytest

from hermes_cli.command_catalog import (
    SCHEMA_VERSION,
    build_command_catalog,
    resolve_catalog_command,
)


def test_catalog_is_deterministic_and_aliases_preserve_identity():
    first = build_command_catalog()
    second = build_command_catalog()

    assert first.schema_version == SCHEMA_VERSION
    assert first.revision == second.revision
    assert first.to_dict() == second.to_dict()

    new = resolve_catalog_command(first, "/new")
    reset = resolve_catalog_command(first, "/reset")
    assert new is not None
    assert reset is not None
    assert new.command_id == reset.command_id == "command.new"


def test_catalog_serializes_legacy_argument_and_busy_contract():
    catalog = build_command_catalog()
    model = resolve_catalog_command(catalog, "model")
    stop = resolve_catalog_command(catalog, "stop")

    assert model is not None
    assert model.to_dict()["argument_schema"]["hint"]
    assert model.busy_policy == "reject"
    assert stop is not None
    assert stop.busy_policy == "interrupt_then_dispatch"


def test_dynamic_contributions_are_fingerprinted_and_resolvable():
    catalog = build_command_catalog(
        contributions=(
            (
                "plugin.example",
                {
                    "command_id": "plugin.example.deploy",
                    "name": "deploy",
                    "aliases": ["ship"],
                    "description": "Deploy the current project",
                    "category": "Plugins",
                    "execution_owner": "plugin",
                    "handler_id": "plugin.example.deploy",
                },
            ),
        )
    )
    assert resolve_catalog_command(catalog, "ship").command_id == "plugin.example.deploy"
    assert catalog.revision != build_command_catalog().revision


def test_duplicate_alias_fails_closed():
    with pytest.raises(ValueError, match="collides"):
        build_command_catalog(
            contributions=(
                (
                    "plugin.example",
                    {
                        "command_id": "plugin.example.not-new",
                        "name": "not-new",
                        "aliases": ["new"],
                    },
                ),
            )
        )
