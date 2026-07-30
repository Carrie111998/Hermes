from copy import deepcopy

from hermes_cli.config import DEFAULT_CONFIG


def test_turn_routing_is_additive_inert_and_separate_from_legacy_smart_routing():
    config = deepcopy(DEFAULT_CONFIG)

    assert config["routing"]["mode"] == "off"
    assert config["routing"]["budget"]["grok_weekly_limit"] == 0
    config["routing"]["mode"] = "auto"

    assert DEFAULT_CONFIG["routing"]["mode"] == "off"
    assert "smart_model_routing" not in DEFAULT_CONFIG
