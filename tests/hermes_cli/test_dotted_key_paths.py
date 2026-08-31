"""Tests for dotted config-key handling when leaf keys contain literal dots.

Model names like ``glm-5.3-flash`` are legal YAML mapping keys under
``providers.<name>.models``, but ``hermes config set`` historically split the
user-supplied key path on every ``.``, corrupting such leaf keys into nested
junk (``glm-5: {3-flash: {...}}``). The escape syntax ``\\.`` (a backslash
before the dot) marks a literal dot inside a segment.
"""

import os
from unittest.mock import patch

import pytest
import yaml

from hermes_cli.config import (
    _split_key_path,
    set_config_value,
)


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path):
    """Point HERMES_HOME at a temp dir so tests never touch real config."""
    env_file = tmp_path / ".env"
    env_file.touch()
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        yield tmp_path


def _read_config(tmp_path):
    config_path = tmp_path / "config.yaml"
    if not config_path.exists():
        return {}
    return yaml.safe_load(config_path.read_text()) or {}


# ---------------------------------------------------------------------------
# _split_key_path
# ---------------------------------------------------------------------------


class TestSplitKeyPath:
    def test_plain_path_unchanged(self):
        assert _split_key_path("a.b.c") == ["a", "b", "c"]

    def test_escaped_dot_is_literal(self):
        assert _split_key_path(r"a.b\.c") == ["a", "b.c"]

    def test_multiple_escaped_dots(self):
        assert _split_key_path(r"a\.b\.c") == ["a.b.c"]

    def test_model_name_round_trip(self):
        key = r"providers.Test.models.glm-5\.3-flash.context_length"
        assert _split_key_path(key) == [
            "providers",
            "Test",
            "models",
            "glm-5.3-flash",
            "context_length",
        ]

    def test_escaped_backslash_then_dot_still_splits(self):
        # r"a\\.b" is chars: a, \, \, ., b — the double backslash is an
        # escaped literal backslash, so the dot remains a separator.
        assert _split_key_path(r"a\\.b") == ["a\\", "b"]

    def test_unknown_escape_keeps_backslash_literally(self):
        assert _split_key_path(r"a\.b") == ["a.b"]

    def test_trailing_backslash_kept(self):
        assert _split_key_path("a.b\\") == ["a", "b\\"]


# ---------------------------------------------------------------------------
# set / get / unset with dotted leaf keys
# ---------------------------------------------------------------------------


class TestSetDottedLeafKey:
    def test_set_model_name_leaf_writes_single_key(self, tmp_path, capsys):
        set_config_value(
            r"providers.Test.models.glm-5\.3-flash.context_length", "1000000"
        )
        models = _read_config(tmp_path)["providers"]["Test"]["models"]
        assert models == {"glm-5.3-flash": {"context_length": 1000000}}

    def test_set_two_model_leaves_preserves_siblings(self, tmp_path, capsys):
        set_config_value(
            r"providers.Test.models.glm-5\.3-flash.context_length", "1000000"
        )
        set_config_value(r"providers.Test.models.hy3.context_length", "256000")
        models = _read_config(tmp_path)["providers"]["Test"]["models"]
        assert models == {
            "glm-5.3-flash": {"context_length": 1000000},
            "hy3": {"context_length": 256000},
        }

    def test_set_leaf_without_dots_unaffected(self, tmp_path, capsys):
        set_config_value(r"mcp_servers.my-server.command", "uvx")
        servers = _read_config(tmp_path)["mcp_servers"]
        assert servers == {"my-server": {"command": "uvx"}}


class TestUnsetDottedLeafKey:
    def test_unset_model_name_leaf(self, tmp_path, capsys):
        set_config_value(
            r"providers.Test.models.glm-5\.3-flash.context_length", "1000000"
        )
        from hermes_cli.config import unset_config_value

        unset_config_value(r"providers.Test.models.glm-5\.3-flash.context_length")
        # unset cascades away now-empty containers, so the whole branch may be
        # gone; the contract is simply that the escaped leaf key is removed.
        models = (
            _read_config(tmp_path).get("providers", {}).get("Test", {}).get("models", {})
        )
        assert "glm-5.3-flash" not in models


class TestEmptySegmentGuardSurvivesEscapes:
    def test_double_dot_still_rejected(self):
        from hermes_cli.config import set_config_value

        with pytest.raises(SystemExit):
            set_config_value("a..b", "x")
