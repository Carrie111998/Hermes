"""Regression tests for removal of the unwired ``smart_model_routing`` placeholder.

Issue: NousResearch#98835.
Branch: fix/config-dead-routing.

The setup wizard has long seeded ``smart_model_routing: {enabled: False}`` into
Blank-Slate configs and the structure-validator's ``_EXTRA_KNOWN_ROOT_KEYS``
allowlist accepted it on disk, but no runtime consumer ever reads the value.
This test suite pins three load-bearing contracts for the v39→v40 migration:

1. **Migration strips the key on first read** — users upgrading past the
   cutover never see the placeholder back on disk after a save/load cycle.
2. **Migration warning surfaces to ``results['warnings']``** — every caller
   that introspects the migration return value learns the dead key was
   removed, even when stdout is suppressed.
3. **Config structure validation fails closed** — when the key appears on
   disk (e.g. auto-migration disabled, hand-edited back), a warning surfaces
   instead of silently passing.
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
import yaml

from hermes_cli.config import (
    _EXTRA_KNOWN_ROOT_KEYS,
    _KNOWN_ROOT_KEYS,
    migrate_config,
    validate_config_structure,
)


def test_v40_migration_strips_smart_model_routing_block(tmp_path):
    """v39 config carrying the dead key upgrades to v40+ and loses the key."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "_config_version": 39,
                "smart_model_routing": {"enabled": False, "mode": "cost"},
                "model": {"provider": "openrouter"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        results = migrate_config(interactive=False, quiet=True)

    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    # Behavior contract: the run must advance past v40 (later migrations are
    # allowed to land later, but the v40 cutover must have run).
    assert raw["_config_version"] >= 40
    assert "smart_model_routing" not in raw
    # Model section is preserved — migration does not touch unrelated keys.
    assert raw["model"] == {"provider": "openrouter"}
    # Warning surfaces to callers even with quiet=True.
    assert any(
        "smart_model_routing" in warning for warning in results["warnings"]
    ), results["warnings"]


def test_v40_migration_noop_when_key_absent(tmp_path):
    """v40 migration is idempotent — running it twice is a no-op."""
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {"_config_version": 39, "model": {"provider": "openrouter"}},
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        results_first = migrate_config(interactive=False, quiet=True)
        results_second = migrate_config(interactive=False, quiet=True)

    # No smart_model_routing-specific warning either time.
    assert not any(
        "smart_model_routing" in warning
        for warning in results_first["warnings"]
    )
    assert not any(
        "smart_model_routing" in warning
        for warning in results_second["warnings"]
    )


def test_smart_model_routing_no_longer_in_known_root_keys():
    """The dead key must be removed from both allowlists — derivation,
    membership, and the comment trail."""
    from hermes_cli.config import DEFAULT_CONFIG

    assert "smart_model_routing" not in DEFAULT_CONFIG
    assert "smart_model_routing" not in _EXTRA_KNOWN_ROOT_KEYS
    assert "smart_model_routing" not in _KNOWN_ROOT_KEYS
    # _KNOWN_ROOT_KEYS invariant preserved after removal.
    assert _KNOWN_ROOT_KEYS == frozenset(DEFAULT_CONFIG.keys()) | _EXTRA_KNOWN_ROOT_KEYS


def test_validate_config_structure_warns_on_smart_model_routing():
    """Fail-closed: when the dead key appears in user config, validation
    surfaces a warning so the next ``hermes doctor`` run reports it."""
    issues = validate_config_structure(
        {"smart_model_routing": {"enabled": True}}
    )
    matched = [
        issue for issue in issues
        if issue.severity == "warning" and "smart_model_routing" in issue.message
    ]
    assert matched, (
        "validate_config_structure must surface a warning when "
        "smart_model_routing is present (issue #98835). Got: "
        f"{[i.message for i in issues]}"
    )
    # The hint must point at the actual replacement mechanism, not just say
    # "remove it" — model selection lives in `model:`, fallback_* and
    # `auxiliary:`; there is no separate routing flag.
    assert "model" in matched[0].hint
    assert "fallback" in matched[0].hint or "auxiliary" in matched[0].hint


def test_validate_config_structure_silent_for_unrelated_keys():
    """Removing smart_model_routing from the allowlist must not change
    behaviour for unrelated dead-key-style root entries — those still
    route through the existing provider-like misplaced-key warning."""
    issues = validate_config_structure({"MY_APP_TOKEN": "abc"})
    assert not any(
        "smart_model_routing" in issue.message for issue in issues
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])