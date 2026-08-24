"""Regression tests for #60955: gateway must not freeze fallback_providers.

Cron reloads ``fallback_providers`` from disk on every job. The gateway used to
freeze ``self._fallback_model`` at process start, so a chain configured (or
edited) after ``hermes gateway`` was already running never reached messaging
sessions — even though cron in the same process fell back correctly.

These tests pin the reload + cached-agent apply helpers without driving the
full Feishu session path.
"""

from __future__ import annotations

import time
from types import SimpleNamespace


def test_refresh_fallback_model_rereads_config(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "fallback_providers:\n"
        "  - provider: deepseek\n"
        "    model: deepseek-v4-flash\n"
    )

    runner = SimpleNamespace(
        _fallback_model=None,
    )
    runner._load_fallback_model = GatewayRunner._load_fallback_model
    bound = GatewayRunner._refresh_fallback_model.__get__(runner)
    chain = bound()

    assert chain == [{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    assert runner._fallback_model == chain

    cfg.write_text(
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: anthropic/claude-sonnet-4.6\n"
    )
    updated = bound()
    assert updated == [
        {"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}
    ]
    assert runner._fallback_model == updated


def test_apply_fallback_chain_skips_while_cooldown_holds_fallback():
    """Do not clobber a live fallback activation during its cooldown window."""
    from gateway.run import GatewayRunner

    live = [{"provider": "deepseek", "model": "deepseek-v4-flash"}]
    agent = SimpleNamespace(
        _fallback_chain=live,
        _fallback_model=live[0],
        _fallback_index=1,
        _fallback_activated=True,
        _rate_limited_until=time.monotonic() + 30,
    )
    GatewayRunner._apply_fallback_chain_to_agent(
        agent,
        [{"provider": "openrouter", "model": "anthropic/claude-sonnet-4.6"}],
    )

    assert agent._fallback_chain == live
    assert agent._fallback_index == 1
    assert agent._fallback_activated is True


def test_refresh_composes_configured_default_for_session_override(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text(
        "fallback_providers:\n"
        "  - provider: openai-codex\n"
        "    model: gpt-5.6-luna\n"
    )
    runner = SimpleNamespace(_fallback_model=None)
    bound = GatewayRunner._refresh_fallback_model.__get__(runner)

    chain = bound(
        primary_route={"provider": "openrouter", "model": "override-model"},
        configured_default_route={"provider": "commandcode", "model": "model-a"},
    )

    assert chain == [
        {"provider": "commandcode", "model": "model-a"},
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
    ]


def test_refresh_preserves_last_known_default_on_transient_parse_failure(
    tmp_path, monkeypatch
):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "fallback_providers:\n"
        "  - provider: openai-codex\n"
        "    model: gpt-5.6-luna\n"
    )
    runner = SimpleNamespace(
        _fallback_model=None,
        _configured_default_route=None,
    )
    primary = {"provider": "openrouter", "model": "override-model"}
    configured_default = {"provider": "commandcode", "model": "model-a"}

    initial, initial_default = GatewayRunner._refresh_fallback_state.__get__(runner)(
        primary_route=primary,
        configured_default_route=configured_default,
    )
    cfg.write_text("model: [unterminated")
    retained, retained_default = GatewayRunner._refresh_fallback_state.__get__(runner)(
        primary_route=primary,
        configured_default_route=None,
    )

    assert initial == retained == [
        configured_default,
        {"provider": "openai-codex", "model": "gpt-5.6-luna"},
    ]
    assert initial_default == retained_default == configured_default


def test_refresh_clears_last_known_default_after_successful_removal(
    tmp_path, monkeypatch
):
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    cfg = tmp_path / "config.yaml"
    cfg.write_text("fallback_providers: []\n")
    configured_default = {"provider": "commandcode", "model": "model-a"}
    runner = SimpleNamespace(
        _fallback_model=None,
        _configured_default_route=configured_default,
    )
    chain, effective_default = GatewayRunner._refresh_fallback_state.__get__(runner)(
        primary_route={"provider": "openrouter", "model": "override-model"},
        configured_default_route=None,
    )

    assert chain is None
    assert effective_default is None


def test_refresh_state_isolated_by_multiplex_profile_home(tmp_path, monkeypatch):
    from gateway.run import GatewayRunner
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    homes = {name: tmp_path / name for name in ("profile-a", "profile-b")}
    for home in homes.values():
        home.mkdir()
    (homes["profile-a"] / "config.yaml").write_text(
        "fallback_providers:\n  - provider: fallback-a\n    model: rescue-a\n"
    )
    (homes["profile-b"] / "config.yaml").write_text(
        "fallback_providers:\n  - provider: fallback-b\n    model: rescue-b\n"
    )
    runner = SimpleNamespace(_fallback_model=None)
    refresh = GatewayRunner._refresh_fallback_state.__get__(runner)
    primary = {"provider": "override", "model": "session-model"}
    defaults = {
        "profile-a": {"provider": "provider-a", "model": "default-a"},
        "profile-b": {"provider": "provider-b", "model": "default-b"},
    }

    token = set_hermes_home_override(str(homes["profile-a"]))
    try:
        refresh(primary_route=primary, configured_default_route=defaults["profile-a"])
    finally:
        reset_hermes_home_override(token)
    token = set_hermes_home_override(str(homes["profile-b"]))
    try:
        chain_b, effective_b = refresh(
            primary_route=primary,
            configured_default_route=defaults["profile-b"],
        )
    finally:
        reset_hermes_home_override(token)

    (homes["profile-a"] / "config.yaml").write_text("model: [unterminated")
    token = set_hermes_home_override(str(homes["profile-a"]))
    try:
        chain_a, effective_a = refresh(
            primary_route=primary,
            configured_default_route=None,
        )
    finally:
        reset_hermes_home_override(token)

    assert effective_b == defaults["profile-b"]
    assert chain_b == [
        defaults["profile-b"],
        {"provider": "fallback-b", "model": "rescue-b"},
    ]
    assert effective_a == defaults["profile-a"]
    assert chain_a == [
        defaults["profile-a"],
        {"provider": "fallback-a", "model": "rescue-a"},
    ]


def test_load_fallback_model_static_unchanged_contract(tmp_path, monkeypatch):
    """_load_fallback_model remains a pure static reader used by refresh."""
    from gateway.run import GatewayRunner

    monkeypatch.setattr("gateway.run._hermes_home", tmp_path)
    (tmp_path / "config.yaml").write_text(
        "fallback_providers:\n"
        "  - provider: deepseek\n"
        "    model: deepseek-v4-flash\n"
        "fallback_model:\n"
        "  provider: nous\n"
        "  model: Hermes-4\n"
    )

    chain = GatewayRunner._load_fallback_model()
    assert chain == [
        {"provider": "deepseek", "model": "deepseek-v4-flash"},
        {"provider": "nous", "model": "Hermes-4"},
    ]
