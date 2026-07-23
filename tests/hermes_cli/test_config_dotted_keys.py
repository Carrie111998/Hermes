"""Tests for double-quote-aware dotted config-key addressing (`_split_dotted_key`).

A key segment that legitimately contains a dot — every real model id does
(``gpt-5.6-sol``) — must be addressable so ``model_routes.<model-id>`` can be
written, read and removed from the CLI. Unquoted keys must keep tokenizing
exactly as ``str.split(".")`` did.
"""

import os
from unittest.mock import patch

import pytest

from hermes_cli.config import (
    _split_dotted_key,
    _get_nested,
    _unset_nested,
    _default_value_for_key,
    set_config_value,
    get_config_value,
    unset_config_value,
)


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path):
    """Point HERMES_HOME at a temp dir so tests never touch real config."""
    (tmp_path / ".env").touch()
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        yield tmp_path


# ---------------------------------------------------------------------------
# Tokenizer
# ---------------------------------------------------------------------------

class TestSplitDottedKey:
    def test_unquoted_matches_str_split(self):
        assert _split_dotted_key("a.b.c") == ["a", "b", "c"]

    def test_quoted_span_kept_literal(self):
        assert _split_dotted_key('a."b.c".d') == ["a", "b.c", "d"]

    def test_model_id_route_path(self):
        assert _split_dotted_key(
            'platforms.api_server.extra.model_routes."gpt-5.6-sol".model'
        ) == [
            "platforms", "api_server", "extra", "model_routes",
            "gpt-5.6-sol", "model",
        ]

    def test_numeric_list_segment_preserved(self):
        # The #17876 list-navigation guard must be unaffected.
        assert _split_dotted_key("custom_providers.0.api_key") == [
            "custom_providers", "0", "api_key",
        ]

    def test_unterminated_quote_raises(self):
        with pytest.raises(ValueError):
            _split_dotted_key('a."b')


# ---------------------------------------------------------------------------
# Round trip through set/get/unset on a dotted model-id leaf
# ---------------------------------------------------------------------------

class TestDottedRouteRoundTrip:
    KEY = 'platforms.api_server.extra.model_routes."gpt-5.6-sol".model'

    def test_set_get_unset_addresses_single_key(self, capsys):
        set_config_value(self.KEY, "openai/gpt-5.6")
        capsys.readouterr()

        from hermes_cli.config import load_config
        routes = (
            load_config()
            .get("platforms", {})
            .get("api_server", {})
            .get("extra", {})
            .get("model_routes", {})
        )
        # The route lands under the SINGLE key "gpt-5.6-sol", not a nested
        # "gpt-5" -> "6-sol" mangling.
        assert routes == {"gpt-5.6-sol": {"model": "openai/gpt-5.6"}}

        get_config_value(self.KEY)
        assert "openai/gpt-5.6" in capsys.readouterr().out

        unset_config_value(self.KEY)
        capsys.readouterr()
        routes_after = (
            load_config()
            .get("platforms", {})
            .get("api_server", {})
            .get("extra", {})
            .get("model_routes", {})
        )
        # Unset removes the addressed leaf (`.model`), and hermes' existing
        # empty-container cleanup then collapses the route entry and its now
        # empty ancestors — the same behaviour `config unset` already has for
        # any other key. Quoting changes only how the path is addressed.
        assert routes_after == {}

    def test_get_nested_directly(self):
        cfg = {"model_routes": {"gpt-5.6-sol": {"model": "x"}}}
        assert _get_nested(cfg, 'model_routes."gpt-5.6-sol".model') == "x"

    def test_unset_nested_directly(self):
        cfg = {"model_routes": {"gpt-5.6-sol": {"model": "x"}}}
        assert _unset_nested(cfg, 'model_routes."gpt-5.6-sol"') is True
        # The quoted segment is removed, then the existing empty-container
        # cleanup drops the emptied `model_routes` parent.
        assert cfg == {}


# ---------------------------------------------------------------------------
# A quoted model-id leaf must not be coerced away from a string
# ---------------------------------------------------------------------------

def test_quoted_key_does_not_mis_coerce_default_lookup():
    # A quoted model-id path misses DEFAULT_CONFIG, so the default lookup
    # returns None and the value keeps its historical best-effort coercion —
    # a model-id string stays a string.
    key = 'platforms.api_server.extra.model_routes."gpt-5.6-sol".model'
    assert _default_value_for_key(key) is None


# ---------------------------------------------------------------------------
# Malformed key is rejected cleanly (no traceback, non-zero exit)
# ---------------------------------------------------------------------------

class TestMalformedKeyGuard:
    def test_set_exits_on_unterminated_quote(self):
        with pytest.raises(SystemExit) as exc:
            set_config_value('model_routes."gpt-5.6-sol', "x")
        assert exc.value.code == 1

    def test_get_exits_on_unterminated_quote(self):
        with pytest.raises(SystemExit) as exc:
            get_config_value('model_routes."gpt-5.6-sol')
        assert exc.value.code == 1

    def test_unset_exits_on_unterminated_quote(self):
        with pytest.raises(SystemExit) as exc:
            unset_config_value('model_routes."gpt-5.6-sol')
        assert exc.value.code == 1
