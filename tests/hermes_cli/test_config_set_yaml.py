"""Tests for set_config_value YAML parse fallback.

Verifies that multi-line YAML list/dict values are stored as proper YAML
structures, not quoted strings.  Before the fix, argparse delivered a str
and the coercion block only handled bool/int/float, so YAML collections
were written verbatim as strings.
"""

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


def _read_config_yaml(tmp_path):
    config_path = tmp_path / "config.yaml"
    if not config_path.exists():
        return {}
    return yaml.safe_load(config_path.read_text()) or {}


# ---------------------------------------------------------------------------
# Bug-injection proof: multi-line YAML list must be stored as a list
# ---------------------------------------------------------------------------

class TestYamlListParse:
    """Multi-line YAML list input must be stored as a YAML list, not a string."""

    def test_multiline_yaml_list_stored_as_list(self, _isolated_hermes_home):
        """The primary bug: custom_providers written as multi-line YAML."""
        yaml_value = (
            "- name: oneapi\n"
            "  type: openai\n"
            "  base_url: https://oneapi.example.com\n"
            "  api_key_env: ONEAPI_API_KEY\n"
        )
        set_config_value("custom_providers", yaml_value, force=True)

        saved = _read_config_yaml(_isolated_hermes_home)
        assert isinstance(saved["custom_providers"], list), (
            f"Expected list, got {type(saved['custom_providers']).__name__}: "
            f"{saved['custom_providers']!r}"
        )
        assert len(saved["custom_providers"]) == 1
        entry = saved["custom_providers"][0]
        assert entry["name"] == "oneapi"
        assert entry["type"] == "openai"
        assert entry["base_url"] == "https://oneapi.example.com"

    def test_multiline_yaml_list_multiple_entries(self, _isolated_hermes_home):
        yaml_value = (
            "- name: alpha\n"
            "  base_url: https://a.example.com\n"
            "- name: beta\n"
            "  base_url: https://b.example.com\n"
        )
        set_config_value("custom_providers", yaml_value, force=True)

        saved = _read_config_yaml(_isolated_hermes_home)
        assert isinstance(saved["custom_providers"], list)
        assert len(saved["custom_providers"]) == 2
        assert saved["custom_providers"][0]["name"] == "alpha"
        assert saved["custom_providers"][1]["name"] == "beta"


# ---------------------------------------------------------------------------
# Regression: existing scalar coercion still works
# ---------------------------------------------------------------------------

class TestScalarCoercionRegression:
    """Scalar values must still be coerced exactly as before."""

    @pytest.mark.parametrize("value,expected", [
        ("true", True),
        ("false", False),
        ("yes", True),
        ("no", False),
        ("on", True),
        ("off", False),
    ])
    def test_bool_coercion(self, _isolated_hermes_home, value, expected):
        set_config_value("terminal.persistent_shell", value)
        saved = _read_config_yaml(_isolated_hermes_home)
        assert saved["terminal"]["persistent_shell"] is expected

    def test_int_coercion(self, _isolated_hermes_home):
        set_config_value("approvals.timeout", "30")
        saved = _read_config_yaml(_isolated_hermes_home)
        assert saved["approvals"]["timeout"] == 30
        assert isinstance(saved["approvals"]["timeout"], int)

    def test_float_coercion(self, _isolated_hermes_home):
        set_config_value("agent.temperature", "0.7")
        saved = _read_config_yaml(_isolated_hermes_home)
        assert saved["agent"]["temperature"] == 0.7
        assert isinstance(saved["agent"]["temperature"], float)

    def test_plain_string_not_affected(self, _isolated_hermes_home):
        """A plain string that doesn't look like YAML must stay a string."""
        set_config_value("model.default", "gpt-4o")
        saved = _read_config_yaml(_isolated_hermes_home)
        assert saved["model"]["default"] == "gpt-4o"
        assert isinstance(saved["model"]["default"], str)

    def test_string_typed_enum_not_coerced(self, _isolated_hermes_home):
        """approvals.mode is string-typed in DEFAULT_CONFIG -- must stay str."""
        set_config_value("approvals.mode", "off")
        saved = _read_config_yaml(_isolated_hermes_home)
        assert saved["approvals"]["mode"] == "off"
        assert isinstance(saved["approvals"]["mode"], str)


# ---------------------------------------------------------------------------
# Edge case: single-line dict
# ---------------------------------------------------------------------------

class TestSingleLineDict:
    def test_single_line_dict_parses(self, _isolated_hermes_home):
        set_config_value("custom.enabled", "{enabled: true, name: test}", force=True)
        saved = _read_config_yaml(_isolated_hermes_home)
        assert isinstance(saved["custom"]["enabled"], dict)
        assert saved["custom"]["enabled"]["enabled"] is True
        assert saved["custom"]["enabled"]["name"] == "test"

    def test_single_line_list_parses(self, _isolated_hermes_home):
        set_config_value("custom.tags", "[alpha, beta, gamma]", force=True)
        saved = _read_config_yaml(_isolated_hermes_home)
        assert isinstance(saved["custom"]["tags"], list)
        assert saved["custom"]["tags"] == ["alpha", "beta", "gamma"]


# ---------------------------------------------------------------------------
# Edge case: value with newline that is NOT valid YAML keeps original string
# ---------------------------------------------------------------------------

class TestInvalidYamlFallback:
    def test_invalid_yaml_keeps_original_string(self, _isolated_hermes_home):
        """A value with a newline that fails YAML parse must stay a string."""
        # Tab-indented content that yaml.safe_load rejects as a syntax error
        # is hard to construct reliably; instead use a value that yaml parses
        # back to a plain str -- the fallback must not adopt it as a complex
        # type.
        set_config_value("custom.note", "just a\nplain string", force=True)
        saved = _read_config_yaml(_isolated_hermes_home)
        assert isinstance(saved["custom"]["note"], str)
        assert "plain string" in saved["custom"]["note"]

    def test_yaml_error_keeps_original_string(self, _isolated_hermes_home):
        """A value that raises YAMLError must fall back to the original str."""
        # Unmatched flow-mapping bracket triggers a YAMLError.
        set_config_value("custom.broken", "{a: 1", force=True)
        saved = _read_config_yaml(_isolated_hermes_home)
        # Falls back to the raw string because yaml.safe_load raises.
        assert isinstance(saved["custom"]["broken"], str)
        assert saved["custom"]["broken"] == "{a: 1"
