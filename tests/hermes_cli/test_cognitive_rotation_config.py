"""Configuration contract tests for the cognitive-rotation guardrail."""

from hermes_cli.config import DEFAULT_CONFIG, load_config


EXPECTED_COGNITIVE_ROTATION_DEFAULTS = {
    "enabled": False,
    "mutation_budget": 20,
    "rotate_after_compaction": True,
    "lock_after_delegation": True,
}


def test_cognitive_rotation_defaults_to_opt_in_safe_values():
    agent_defaults = DEFAULT_CONFIG["agent"]
    assert isinstance(agent_defaults, dict)
    assert agent_defaults["cognitive_rotation"] == (
        EXPECTED_COGNITIVE_ROTATION_DEFAULTS
    )


def test_partial_cognitive_rotation_config_preserves_nested_defaults(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "agent:\n  cognitive_rotation:\n    enabled: true\n",
        encoding="utf-8",
    )

    config = load_config()

    assert config["agent"]["cognitive_rotation"] == {
        **EXPECTED_COGNITIVE_ROTATION_DEFAULTS,
        "enabled": True,
    }
