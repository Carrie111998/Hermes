"""Provider attribution requires consent from the active Hermes profile."""

from __future__ import annotations

import pytest
import yaml

from hermes_cli import config, managed_scope
from hermes_cli.usage_attribution import usage_attribution_enabled
from hermes_constants import reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
    token = set_hermes_home_override(home)
    managed_scope.invalidate_managed_cache()
    try:
        yield home
    finally:
        reset_hermes_home_override(token)
        managed_scope.invalidate_managed_cache()


def _write_config(home, data):
    (home / "config.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def _policy(enabled):
    return {"telemetry": {"usage_attribution": {"enabled": enabled}}}


def test_usage_attribution_is_disabled_by_default(profile):
    assert config.DEFAULT_CONFIG["telemetry"]["usage_attribution"]["enabled"] is False
    assert usage_attribution_enabled() is False


@pytest.mark.parametrize("value", [False, None, 0, 1, "true", "false", [], {}])
def test_only_a_yaml_boolean_true_is_consent(profile, value):
    _write_config(profile, _policy(value))

    assert usage_attribution_enabled() is False


@pytest.mark.parametrize(
    "data",
    [{}, [], {"telemetry": None}, {"telemetry": True},
     {"telemetry": {"usage_attribution": True}}],
)
def test_malformed_policy_is_disabled(profile, data):
    _write_config(profile, data)

    assert usage_attribution_enabled() is False


def test_config_edits_are_seen_without_restarting_the_policy_reader(profile):
    _write_config(profile, _policy(True))
    assert usage_attribution_enabled() is True

    _write_config(profile, _policy(False))
    assert usage_attribution_enabled() is False

    (profile / "config.yaml").write_text("telemetry: [\n", encoding="utf-8")
    assert usage_attribution_enabled() is False


def test_config_read_error_fails_closed(profile, monkeypatch):
    def unreadable():
        raise OSError("unreadable test config")

    monkeypatch.setattr(config, "read_raw_config_readonly", unreadable)

    assert usage_attribution_enabled() is False


def test_consent_does_not_leak_between_profiles(profile, tmp_path):
    _write_config(profile, _policy(True))
    other = tmp_path / "other-profile"
    other.mkdir()
    _write_config(other, _policy(False))

    assert usage_attribution_enabled() is True
    token = set_hermes_home_override(other)
    try:
        assert usage_attribution_enabled() is False
    finally:
        reset_hermes_home_override(token)
    assert usage_attribution_enabled() is True


@pytest.mark.parametrize(
    ("profile_enabled", "managed_enabled"),
    [(None, True), (False, True), (True, False)],
)
def test_managed_overlay_cannot_supply_or_replace_consent(
    profile, tmp_path, monkeypatch, profile_enabled, managed_enabled,
):
    managed = tmp_path / "managed"
    managed.mkdir()
    _write_config(profile, {} if profile_enabled is None else _policy(profile_enabled))
    _write_config(managed, _policy(managed_enabled))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    config._LOAD_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()

    effective = config.load_config_readonly()
    assert effective["telemetry"]["usage_attribution"]["enabled"] is managed_enabled
    assert usage_attribution_enabled() is (profile_enabled is True)


@pytest.mark.parametrize("enabled", [False, True])
def test_setup_keeps_attribution_and_local_metrics_independent(
    profile, monkeypatch, enabled,
):
    from hermes_cli.setup import setup_telemetry

    data = {
        "telemetry": {
            "shared_metrics": {"enabled": True},
            "usage_attribution": {"enabled": not enabled},
            "unrelated_setting": "preserved",
        },
    }
    questions = []
    answers = iter([True, enabled])

    def answer(question, default):
        questions.append((question, default))
        return next(answers)

    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", answer)
    setup_telemetry(data)
    config.save_config(data)

    assert len(questions) == 2
    assert questions[1] == ("Allow provider usage attribution?", not enabled)
    assert data["telemetry"]["shared_metrics"]["enabled"] is True
    assert data["telemetry"]["unrelated_setting"] == "preserved"
    assert usage_attribution_enabled() is enabled


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("has_extra_menus", [False, True])
def test_tools_menu_persists_attribution_choice(
    profile, monkeypatch, enabled, has_extra_menus,
):
    from hermes_cli import tools_config

    data = _policy(not enabled)
    data["telemetry"]["shared_metrics"] = {"enabled": True}
    if has_extra_menus:
        data["mcp_servers"] = {"test": {"command": "unused"}}
    _write_config(profile, data)

    platforms = ["cli", "telegram"] if has_extra_menus else ["cli"]
    monkeypatch.setattr(tools_config, "_get_enabled_platforms", lambda: platforms)
    monkeypatch.setattr(tools_config, "_get_platform_tools", lambda *a, **kw: set())
    monkeypatch.setattr(tools_config, "_get_effective_configurable_toolsets", lambda: [])
    choices = iter(["Configure provider usage attribution", "Done"])
    monkeypatch.setattr(
        tools_config, "_prompt_choice",
        lambda _question, options, default: options.index(next(choices)),
    )
    monkeypatch.setattr("hermes_cli.setup.prompt_yes_no", lambda _q, default: enabled)

    tools_config.tools_command()

    assert usage_attribution_enabled() is enabled
    saved = config.read_raw_config_readonly()
    assert saved["telemetry"]["shared_metrics"]["enabled"] is True
