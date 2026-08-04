"""Contract tests for the native-Anthropic interrupt-test gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.run_agent import anthropic_test_gate as gate


def _write_config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


@pytest.fixture
def hermes_root(tmp_path, monkeypatch) -> Path:
    """Use the real Hermes root/profile path helpers against a temp estate."""
    import hermes_constants

    root = tmp_path / "hermes-root"
    root.mkdir()
    monkeypatch.setattr(
        hermes_constants,
        "_get_platform_default_hermes_home",
        lambda: root,
    )
    monkeypatch.setenv("HERMES_HOME", str(root))
    return root


def test_sdk_absent_without_native_configuration_skips(hermes_root, monkeypatch):
    """A non-native Claude model route does not require the native SDK."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n"
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: anthropic/claude-opus-4.6\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.SKIP
    assert "Anthropic SDK is not installed" in decision.reason
    with pytest.raises(pytest.skip.Exception, match="no active Hermes configuration"):
        gate.enforce_native_anthropic_test_gate()


def test_sdk_absent_with_native_primary_configuration_fails(hermes_root, monkeypatch):
    """An explicit native primary must not be hidden behind a conditional skip."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "restore the supported anthropic sdk" in decision.reason.casefold()
    with pytest.raises(pytest.fail.Exception, match="native Anthropic provider"):
        gate.enforce_native_anthropic_test_gate()


def test_sdk_absent_with_native_fallback_configuration_fails(hermes_root, monkeypatch):
    """An explicit native fallback also requires the optional SDK."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: gpt-5.6-terra\n"
        "fallback_providers:\n"
        "  - provider: anthropic\n"
        "    model: claude-sonnet-4.6\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "fallback" in decision.reason
    with pytest.raises(
        pytest.fail.Exception,
        match="(?i)restore the supported anthropic sdk",
    ):
        gate.enforce_native_anthropic_test_gate()


def test_detects_native_anthropic_in_ordered_fallback_entries(hermes_root):
    """Fallback selection follows the shared chain's ordered provider entries."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openai-codex\n"
        "  default: gpt-5.6-terra\n"
        "fallback_providers:\n"
        "  - provider: openrouter\n"
        "    model: anthropic/claude-sonnet-4.6\n"
        "  - provider: anthropic\n"
        "    model: claude-sonnet-4.6\n"
        "  - provider: claude-code\n"
        "    model: claude-opus-4.6\n"
        "fallback_model:\n"
        "  provider: anthropic\n"
        "  model: claude-haiku-4.5\n",
    )

    selections = gate.find_native_anthropic_selections()

    assert [
        (selection.scope, selection.source, selection.fallback_index, selection.provider)
        for selection in selections
    ] == [
        ("default", "fallback", 1, "anthropic"),
        ("default", "fallback", 2, "claude-code"),
        ("default", "fallback", 3, "anthropic"),
    ]


def test_scans_active_profile_directories_but_ignores_retired_ones(hermes_root):
    """Only the canonical profiles root contributes to the active taxonomy."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n",
    )
    active_profile = hermes_root / "profiles" / "operator"
    active_profile.mkdir(parents=True)
    _write_config(
        active_profile,
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
    )
    retired_profile = hermes_root / "quarantine" / "operator"
    retired_profile.mkdir(parents=True)
    _write_config(
        retired_profile,
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
    )

    selections = gate.find_native_anthropic_selections()

    assert [(selection.scope, selection.source) for selection in selections] == [
        ("profile:operator", "primary"),
    ]


def test_sdk_present_runs_without_reading_configuration(monkeypatch):
    """The installed SDK is decisive and leaves configuration untouched."""
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: True)
    monkeypatch.setattr(
        gate,
        "find_native_anthropic_selections",
        lambda: pytest.fail("SDK-present gate must not scan configuration"),
    )

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.RUN


def test_probe_is_read_only_and_never_loads_a_credential_pool(hermes_root, monkeypatch):
    """The config diagnostic neither seeds files nor resolves provider auth."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
    )
    import agent.credential_pool as credential_pool

    monkeypatch.setattr(
        credential_pool,
        "load_pool",
        lambda *_args, **_kwargs: pytest.fail("gate must not load auth pools"),
    )
    before = {
        path.relative_to(hermes_root): path.read_bytes()
        for path in hermes_root.rglob("*")
        if path.is_file()
    }

    selections = gate.find_native_anthropic_selections()

    after = {
        path.relative_to(hermes_root): path.read_bytes()
        for path in hermes_root.rglob("*")
        if path.is_file()
    }
    assert selections[0].source == "primary"
    assert after == before
    assert not (hermes_root / "auth.json").exists()
    assert not (hermes_root / "SOUL.md").exists()
