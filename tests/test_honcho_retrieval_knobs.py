"""Tests for the Honcho representation retrieval knobs.

Honcho splits its conclusion budget three ways, and only the semantic slice is
driven by the query. These knobs let a deployment collapse the budget onto that
slice. The load-bearing property is the last test in each class: leaving the
keys unset must send nothing, so upgrading changes no behaviour.
"""

import json

from plugins.memory.honcho.client import HonchoClientConfig
from plugins.memory.honcho.session import HonchoSessionManager
from plugins.memory.config_schema import (
    KIND_BOOL,
    KIND_NUMBER,
    get_provider_config_schema,
)


RETRIEVAL_KEYS = {
    "searchTopK",
    "searchMaxDistance",
    "maxConclusions",
    "includeMostFrequent",
}


def _write(tmp_path, host_block, root=None):
    config_path = tmp_path / "config.json"
    payload = {
        "apiKey": "test-api-key-12345",
        "hosts": {"hermes": {"workspace": "w", "aiPeer": "a", "peerName": "p",
                             **host_block}},
    }
    payload.update(root or {})
    config_path.write_text(json.dumps(payload))
    return HonchoClientConfig.from_global_config(host="hermes", config_path=config_path)


class TestRetrievalSchema:
    """The knobs are declared, so the setup wizard and config UI can reach them."""

    def test_keys_are_declared(self):
        provider = get_provider_config_schema("honcho")
        assert provider is not None
        assert RETRIEVAL_KEYS <= {field.key for field in provider.fields}

    def test_kinds(self):
        provider = get_provider_config_schema("honcho")
        assert provider is not None
        by_key = {f.key: f for f in provider.fields}
        assert by_key["searchTopK"].kind == KIND_NUMBER
        assert by_key["maxConclusions"].kind == KIND_NUMBER
        assert by_key["searchMaxDistance"].kind == KIND_NUMBER
        assert by_key["includeMostFrequent"].kind == KIND_BOOL

    def test_stay_out_of_the_inline_panel(self):
        """Tuning knobs belong in the modal, not the compact panel."""
        provider = get_provider_config_schema("honcho")
        assert provider is not None
        assert not (RETRIEVAL_KEYS & {f.key for f in provider.inline_fields()})


class TestRetrievalConfigParsing:
    def test_values_parse(self, tmp_path):
        cfg = _write(tmp_path, {
            "searchTopK": 12,
            "maxConclusions": 12,
            "searchMaxDistance": 0.65,
            "includeMostFrequent": False,
        })
        assert cfg.search_top_k == 12
        assert cfg.max_conclusions == 12
        assert cfg.search_max_distance == 0.65
        assert cfg.include_most_frequent is False

    def test_host_block_wins_over_root(self, tmp_path):
        cfg = _write(tmp_path, {"searchTopK": 6}, root={"searchTopK": 40})
        assert cfg.search_top_k == 6

    def test_root_applies_when_host_is_silent(self, tmp_path):
        cfg = _write(tmp_path, {}, root={"searchTopK": 40})
        assert cfg.search_top_k == 40

    def test_unparseable_values_fall_back_to_none(self, tmp_path):
        """A typo must not crash the provider at startup."""
        cfg = _write(tmp_path, {"searchTopK": "abc", "searchMaxDistance": "x"})
        assert cfg.search_top_k is None
        assert cfg.search_max_distance is None

    def test_unset_stays_none(self, tmp_path):
        cfg = _write(tmp_path, {})
        assert cfg.search_top_k is None
        assert cfg.search_max_distance is None
        assert cfg.max_conclusions is None
        assert cfg.include_most_frequent is None


class TestRetrievalKwargs:
    """What actually reaches peer.context() and peer.representation()."""

    def test_configured_values_are_forwarded(self, tmp_path):
        cfg = _write(tmp_path, {
            "searchTopK": 12,
            "maxConclusions": 12,
            "searchMaxDistance": 0.65,
        })
        mgr = HonchoSessionManager(config=cfg)
        assert mgr._retrieval_kwargs() == {
            "search_top_k": 12,
            "max_conclusions": 12,
            "search_max_distance": 0.65,
        }

    def test_false_is_forwarded_not_dropped(self, tmp_path):
        """include_most_frequent is tri-state: False differs from unset."""
        cfg = _write(tmp_path, {"includeMostFrequent": False})
        mgr = HonchoSessionManager(config=cfg)
        assert mgr._retrieval_kwargs() == {"include_most_frequent": False}

    def test_unset_sends_nothing(self, tmp_path):
        """The compatibility guarantee: no keys configured, no parameters sent."""
        cfg = _write(tmp_path, {})
        mgr = HonchoSessionManager(config=cfg)
        assert mgr._retrieval_kwargs() == {}

    def test_no_config_at_all_sends_nothing(self):
        mgr = HonchoSessionManager()
        assert mgr._retrieval_kwargs() == {}
