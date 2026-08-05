"""`hermes config set` must store list/dict literals as real lists/dicts.

Issue #57063 (half 2): ``set_config_value`` only coerces bool/int/float. A list
literal (e.g. ``hermes config set platform_toolsets.discord '["clarify","file"]'``)
is stored as a raw **string**, and every reader gated on ``isinstance(..., list)``
(``_get_platform_tools``, ``_get_enabled_set``, ``_get_disabled_set``) silently
ignores it and falls back to its default. The setting looks saved but never
takes effect — a silent-failure path that cost a LINE deployment weeks of a
zero-tool agent (#57063).
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest
import yaml

from hermes_cli.config import set_config_value


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path):
    """Point HERMES_HOME at a temp dir so tests never touch real config."""
    env_file = tmp_path / ".env"
    env_file.touch()
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        yield tmp_path


def _read_config_dict(tmp_path) -> dict:
    config_path = tmp_path / "config.yaml"
    if not config_path.exists():
        return {}
    return yaml.safe_load(config_path.read_text())


class TestListLiteralCoercion:
    """Values that look like YAML flow lists must land as real lists."""

    def test_platform_toolsets_list_literal_stored_as_list(
        self, _isolated_hermes_home
    ):
        set_config_value(
            "platform_toolsets.discord", '["clarify", "file", "web"]'
        )
        cfg = _read_config_dict(_isolated_hermes_home)
        saved = cfg.get("platform_toolsets", {}).get("discord")
        assert isinstance(saved, list), (
            f"platform_toolsets.discord stored as {type(saved).__name__}, "
            f"not list — readers gated on isinstance(..., list) will ignore it"
        )
        assert saved == ["clarify", "file", "web"]

    def test_plugins_enabled_list_literal(self, _isolated_hermes_home):
        set_config_value("plugins.enabled", '["spotify", "a2a-platform"]')
        cfg = _read_config_dict(_isolated_hermes_home)
        saved = cfg.get("plugins", {}).get("enabled")
        assert isinstance(saved, list)
        assert saved == ["spotify", "a2a-platform"]

    def test_single_quoted_list_literal(self, _isolated_hermes_home):
        set_config_value("platform_toolsets.telegram", "['terminal', 'file']")
        cfg = _read_config_dict(_isolated_hermes_home)
        saved = cfg.get("platform_toolsets", {}).get("telegram")
        assert isinstance(saved, list)
        assert saved == ["terminal", "file"]

    def test_yaml_flow_style_list_braces(self, _isolated_hermes_home):
        set_config_value("platform_toolsets.slack", "[terminal, file, web]")
        cfg = _read_config_dict(_isolated_hermes_home)
        saved = cfg.get("platform_toolsets", {}).get("slack")
        assert isinstance(saved, list)
        assert saved == ["terminal", "file", "web"]


class TestDictLiteralCoercion:
    """Values that look like YAML flow mappings must land as real dicts."""

    def test_mapping_literal_stored_as_dict(self, _isolated_hermes_home):
        set_config_value(
            "some.dict.key", '{"one": 1, "two": 2}'
        )
        cfg = _read_config_dict(_isolated_hermes_home)
        saved = cfg.get("some", {}).get("dict", {}).get("key")
        assert isinstance(saved, dict), (
            f"stored as {type(saved).__name__}, not dict"
        )
        assert saved == {"one": 1, "two": 2}


class TestCoercionSafety:
    """The coercion must not break existing scalar behavior or corrupt data."""

    @pytest.mark.parametrize(
        "value",
        [
            "gpt-4o",
            "off",
            "12345",
            "3.14",
            "hello [world]",
            "{not yaml",
            "[unterminated",
        ],
    )
    def test_non_literal_values_keep_legacy_behavior(
        self, _isolated_hermes_home, value
    ):
        """Scalars and malformed literals must keep the current string behavior
        (with a warning) — never crash, never silently corrupt."""
        set_config_value("model", value)
        cfg = _read_config_dict(_isolated_hermes_home)
        assert cfg.get("model") == value

    def test_bool_int_float_coercion_preserved(self, _isolated_hermes_home):
        set_config_value("some.flag", "true")
        set_config_value("some.count", "42")
        set_config_value("some.ratio", "0.5")
        cfg = _read_config_dict(_isolated_hermes_home)
        assert cfg["some"]["flag"] is True
        assert cfg["some"]["count"] == 42
        assert cfg["some"]["ratio"] == 0.5
