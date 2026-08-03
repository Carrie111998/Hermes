"""Unit tests for the ``principle_distiller.enabled`` config switch.

Covers the DEFAULT_CONFIG entry, the ``principle_distiller_enabled()``
resolution order (env override -> config key -> default False), the
structure-validation warnings, and ``hermes config set`` key recognition
plus a real set/get round-trip on an isolated HERMES_HOME.

Contract (PRINCIPLE_INTEGRATION_DESIGN.md §5):
- ``HERMES_PRINCIPLE_DISTILLER`` = 1/true/yes/on (case-insensitive) enables;
  any other set value disables; unset falls through to the config key.
- ``principle_distiller.enabled`` defaults to False (feature off by default).
- Malformed sections degrade to False and never raise.
"""

from __future__ import annotations

import os

import pytest

from hermes_cli.config import (
    _validate_config_key,
    principle_distiller_enabled,
    set_config_value,
    validate_config_structure,
)
from hermes_cli.config_defaults import DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# DEFAULT_CONFIG
# ---------------------------------------------------------------------------


class TestDefaultConfig:
    def test_section_present_and_disabled_by_default(self):
        section = DEFAULT_CONFIG.get("principle_distiller")
        assert isinstance(section, dict)
        assert section.get("enabled") is False

    def test_key_is_known_to_config_set(self):
        # `hermes config set principle_distiller.enabled true` must not emit
        # the unknown-key warning (the parent task's CLI contract).
        is_known, suggestion = _validate_config_key("principle_distiller.enabled")
        assert is_known is True
        assert suggestion is None


# ---------------------------------------------------------------------------
# principle_distiller_enabled() resolution order
# ---------------------------------------------------------------------------


class TestPrincipleDistillerEnabled:
    def test_default_false_when_absent(self, monkeypatch):
        monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
        assert principle_distiller_enabled({"model": "x"}) is False

    def test_config_true(self, monkeypatch):
        monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
        assert (
            principle_distiller_enabled(
                {"principle_distiller": {"enabled": True}}
            )
            is True
        )

    def test_config_false_explicit(self, monkeypatch):
        monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
        assert (
            principle_distiller_enabled(
                {"principle_distiller": {"enabled": False}}
            )
            is False
        )

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "True", "yes", "on", " ON "])
    def test_env_truthy_enables_even_when_config_false(self, monkeypatch, value):
        monkeypatch.setenv("HERMES_PRINCIPLE_DISTILLER", value)
        assert principle_distiller_enabled({"principle_distiller": {"enabled": False}}) is True

    @pytest.mark.parametrize("value", ["0", "false", "off", "no", "banana", ""])
    def test_env_falsy_disables_even_when_config_true(self, monkeypatch, value):
        monkeypatch.setenv("HERMES_PRINCIPLE_DISTILLER", value)
        assert principle_distiller_enabled({"principle_distiller": {"enabled": True}}) is False

    def test_env_unset_falls_through_to_config(self, monkeypatch):
        monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
        assert (
            principle_distiller_enabled(
                {"principle_distiller": {"enabled": True}}
            )
            is True
        )

    def test_malformed_section_degrades_to_false(self, monkeypatch):
        monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
        assert principle_distiller_enabled({"principle_distiller": "nope"}) is False
        assert principle_distiller_enabled({"principle_distiller": 42}) is False
        assert principle_distiller_enabled({"principle_distiller": {"enabled": "yes"}}) is False

    def test_config_read_from_disk_when_omitted(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert principle_distiller_enabled() is False  # no config.yaml -> default


# ---------------------------------------------------------------------------
# validate_config_structure warnings
# ---------------------------------------------------------------------------


class TestValidationWarnings:
    def test_non_mapping_section_warns(self):
        issues = validate_config_structure({"principle_distiller": "enabled: true"})
        texts = " | ".join(i.message for i in issues)
        assert "principle_distiller must be a mapping" in texts

    def test_non_bool_enabled_warns(self):
        issues = validate_config_structure({"principle_distiller": {"enabled": "true"}})
        texts = " | ".join(i.message for i in issues)
        assert "principle_distiller.enabled must be a boolean" in texts

    def test_healthy_section_no_issue(self):
        issues = validate_config_structure({"principle_distiller": {"enabled": False}})
        assert not [i for i in issues if "principle_distiller" in i.message]


# ---------------------------------------------------------------------------
# hermes config set round-trip (isolated HERMES_HOME)
# ---------------------------------------------------------------------------


class TestConfigSetRoundTrip:
    def test_set_and_read_back(self, tmp_path, monkeypatch, capsys):
        monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        set_config_value("principle_distiller.enabled", "true")
        out = capsys.readouterr().out
        assert "principle_distiller.enabled" in out
        # recognized key -> no unknown-key warning (assert on the CLI's actual
        # warning text, not the bare word "unknown": the printed config path
        # can legitimately contain it, e.g. pytest-of-unknown on Windows)
        assert "not a recognized config key" not in out
        assert principle_distiller_enabled() is True

        set_config_value("principle_distiller.enabled", "false")
        assert principle_distiller_enabled() is False

    def test_missing_config_yaml_still_writes(self, tmp_path, monkeypatch):
        """set_config_value must work when no config.yaml exists yet."""
        monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        set_config_value("principle_distiller.enabled", "true")
        assert principle_distiller_enabled() is True
        cfg_path = tmp_path / "config.yaml"
        assert cfg_path.exists()
        assert "principle_distiller" in cfg_path.read_text(encoding="utf-8")

    @pytest.mark.parametrize("off_value", ["false", "off", "no"])
    def test_set_off_variants_coerce_to_bool_false(self, tmp_path, monkeypatch, capsys, off_value):
        """Every user-facing 'off' spelling of the switch writes a real YAML
        boolean False (never a truthy string) and keeps the distiller off."""
        monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        set_config_value("principle_distiller.enabled", off_value)
        capsys.readouterr()
        assert principle_distiller_enabled() is False
        cfg_path = tmp_path / "config.yaml"
        assert cfg_path.exists()
        # Coerced to a genuine bool, not stored as the raw string (a string
        # would trip the strict-bool validation warning on the next load).
        assert "enabled: false" in cfg_path.read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Never leak HERMES_PRINCIPLE_DISTILLER across tests in this module."""
    monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
    yield
    monkeypatch.delenv("HERMES_PRINCIPLE_DISTILLER", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
