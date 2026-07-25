"""Regression coverage for proposal-first Home Assistant group management."""


def test_group_preview_requires_complete_definition_and_hides_legacy_tool():
    from tools.homeassistant_config_tool import HA_PREVIEW_CONFIG_SCHEMA
    from toolsets import TOOLSETS

    parameters = HA_PREVIEW_CONFIG_SCHEMA["parameters"]

    assert parameters["required"] == [
        "resource_type",
        "resource_id",
        "operation",
        "definition",
    ]
    assert parameters["properties"]["operation"]["enum"] == ["create", "update"]

    homeassistant_tools = TOOLSETS["homeassistant"]["tools"]
    assert "ha_preview_config" in homeassistant_tools
    assert "ha_apply_config" in homeassistant_tools
    assert "ha_rollback_config" in homeassistant_tools
    assert "ha_manage_config" not in homeassistant_tools
