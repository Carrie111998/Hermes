"""Contract tests for the native-Anthropic interrupt-test gate."""

from __future__ import annotations

import sys
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
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "no-managed-scope"))
    monkeypatch.delenv("HERMES_INFERENCE_PROVIDER", raising=False)
    return root


def test_legacy_root_native_provider_is_effective_and_fails(hermes_root, monkeypatch):
    """Legacy root keys select native Anthropic after canonical normalization."""
    _write_config(
        hermes_root,
        "provider: anthropic\n"
        "model: claude-sonnet-4.6\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL


def test_env_expanded_native_primary_is_effective_and_fails(hermes_root, monkeypatch):
    """Provider selection sees config expansion before testing the native route."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: ${D2_C03_NATIVE_PRIMARY}\n"
        "  default: claude-sonnet-4.6\n",
    )
    monkeypatch.setenv("D2_C03_NATIVE_PRIMARY", "claude")
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL


def test_env_expanded_native_fallback_is_effective_and_fails(hermes_root, monkeypatch):
    """Fallback provider entries receive the same config expansion treatment."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n"
        "fallback_providers:\n"
        "  - provider: ${D2_C03_NATIVE_FALLBACK}\n"
        "    model: claude-sonnet-4.6\n",
    )
    monkeypatch.setenv("D2_C03_NATIVE_FALLBACK", "claude-code")
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL


@pytest.mark.parametrize(
    ("managed_config", "expected_source"),
    [
        (
            "model:\n"
            "  provider: anthropic\n"
            "  default: claude-sonnet-4.6\n",
            "primary",
        ),
        (
            "fallback_providers:\n"
            "  - provider: anthropic\n"
            "    model: claude-sonnet-4.6\n",
            "fallback",
        ),
    ],
)
def test_explicit_managed_scope_native_route_fails(
    hermes_root, monkeypatch, managed_config, expected_source
):
    """An explicit managed overlay is part of every profile's effective route."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
    )
    managed_dir = hermes_root.parent / "managed-scope"
    managed_dir.mkdir()
    _write_config(managed_dir, managed_config)
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert gate.find_native_anthropic_selections()[0].source == expected_source


def test_inference_provider_env_fallback_is_effective_and_fails(hermes_root, monkeypatch):
    """The runtime provider override applies only after no config provider exists."""
    _write_config(
        hermes_root,
        "model:\n"
        "  default: claude-sonnet-4.6\n",
    )
    monkeypatch.setenv("HERMES_INFERENCE_PROVIDER", "claude-code")
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL


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


def test_profile_enumeration_excludes_default_hidden_and_invalid_directories(hermes_root, monkeypatch):
    """The gate uses the same active-profile taxonomy as the gateway."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n",
    )
    profiles_root = hermes_root / "profiles"
    for name in ("default", ".quarantine", "not a valid profile"):
        profile = profiles_root / name
        profile.mkdir(parents=True)
        _write_config(
            profile,
            "model:\n"
            "  provider: anthropic\n"
            "  default: claude-sonnet-4.6\n",
        )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.SKIP


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink semantics")
def test_profile_enumeration_follows_a_valid_named_symlink(hermes_root):
    """A valid named profile symlink is intentionally followed, like production."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: anthropic/claude-sonnet-4.6\n",
    )
    target = hermes_root.parent / "linked-profile-target"
    target.mkdir()
    _write_config(
        target,
        "model:\n"
        "  provider: anthropic\n"
        "  default: claude-sonnet-4.6\n",
    )
    link = hermes_root / "profiles" / "operator"
    link.parent.mkdir()
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink unavailable: {exc}")

    selections = gate.find_native_anthropic_selections()

    assert [(selection.scope, selection.source) for selection in selections] == [
        ("profile:operator", "primary"),
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


@pytest.mark.parametrize(
    ("source", "route_provider", "provider_key", "disabled_value"),
    [
        ("primary", "anthropic", "anthropic", "false"),
        ("primary", "claude", "anthropic", '"false"'),
        ("primary", "claude-code", "claude-code", '"off"'),
        ("fallback", "anthropic", "anthropic", "false"),
        ("fallback", "claude", "anthropic", '"0"'),
        ("fallback", "claude-code", "claude-code", '"no"'),
    ],
)
def test_disabled_native_primary_or_fallback_skips(
    hermes_root, monkeypatch, source, route_provider, provider_key, disabled_value
):
    """Canonical and alias provider blocks use production enabled semantics."""
    route = (
        "model:\n"
        f"  provider: {route_provider}\n"
        "  default: claude-sonnet-4.6\n"
        if source == "primary"
        else (
            "model:\n"
            "  provider: openrouter\n"
            "  default: openai/gpt-5.6\n"
            "fallback_providers:\n"
            f"  - provider: {route_provider}\n"
            "    model: claude-sonnet-4.6\n"
        )
    )
    _write_config(
        hermes_root,
        route
        + "providers:\n"
        + f"  {provider_key}:\n"
        + f"    enabled: {disabled_value}\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.SKIP


@pytest.mark.parametrize("provider", ["anthropic", "claude", "claude-code"])
def test_enabled_native_alias_never_skips(hermes_root, monkeypatch, provider):
    """An enabled native provider stays an active route regardless of spelling."""
    _write_config(
        hermes_root,
        "model:\n"
        f"  provider: {provider}\n"
        "  default: claude-sonnet-4.6\n"
        "providers:\n"
        "  anthropic:\n"
        "    enabled: true\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL


def test_malformed_config_fails_closed_without_secret_diagnostic(hermes_root, monkeypatch):
    """A parse failure cannot turn an unknown native route into a skip."""
    secret = "d2-c03-malformed-secret"
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: [anthropic\n"
        f"  api_key: {secret}\n",
    )
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "could not safely inspect active Hermes configuration" in decision.reason
    assert secret not in decision.reason
    with pytest.raises(gate.NativeAnthropicGateInspectionError) as inspection:
        gate.find_native_anthropic_selections()
    assert inspection.value.__cause__ is None
    assert secret not in str(inspection.value)

    with pytest.raises(pytest.fail.Exception) as failure:
        gate.enforce_native_anthropic_test_gate()
    assert secret not in str(failure.value)


def test_unreadable_config_fails_closed_without_secret_diagnostic(hermes_root, monkeypatch):
    """An I/O error uses the same non-secret, fail-closed gate decision."""
    config_path = hermes_root / "config.yaml"
    secret = "d2-c03-unreadable-secret"
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        f"  api_key: {secret}\n",
    )
    import builtins

    real_open = builtins.open

    def deny_gate_config(path, *args, **kwargs):
        if Path(path) == config_path:
            raise PermissionError(secret)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny_gate_config)
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "could not safely inspect active Hermes configuration" in decision.reason
    assert secret not in decision.reason


def test_unreadable_profiles_root_fails_closed_without_secret_diagnostic(hermes_root, monkeypatch):
    """Canonical profile enumeration errors must not become a false skip."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
    )
    profiles_root = hermes_root / "profiles"
    profiles_root.mkdir()
    secret = "d2-c03-profiles-secret"
    real_iterdir = Path.iterdir

    def deny_profiles_root(path):
        if path == profiles_root:
            raise PermissionError(secret)
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", deny_profiles_root)
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "could not safely inspect active Hermes configuration" in decision.reason
    assert secret not in decision.reason


def test_unstatable_profiles_root_fails_closed_without_secret_diagnostic(
    hermes_root, monkeypatch
):
    """A permission error hidden by Path.is_dir() still must not become a skip."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
    )
    profiles_root = hermes_root / "profiles"
    profiles_root.mkdir()
    secret = "d2-c03-unstatable-profiles-secret"
    real_stat = Path.stat

    def deny_profiles_stat(path, *args, **kwargs):
        if path == profiles_root:
            raise PermissionError(secret)
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", deny_profiles_stat)
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert "could not safely inspect active Hermes configuration" in decision.reason
    assert secret not in decision.reason


def test_original_interrupt_test_honors_controlled_gate_states(monkeypatch):
    """The unchanged target obeys run, skip, and fail decisions before its body."""
    from unittest.mock import MagicMock

    from agent import anthropic_adapter
    from tests.run_agent.test_run_agent import TestAnthropicInterruptHandler

    # The controlled RUN state must execute the original behavioral assertions
    # without installing the optional SDK; its request client is mocked below.
    monkeypatch.setattr(anthropic_adapter, "build_anthropic_client", MagicMock())
    target = TestAnthropicInterruptHandler().test_interruptible_anthropic_interrupt_never_closes_shared_client

    monkeypatch.setattr(gate, "enforce_native_anthropic_test_gate", lambda: None)
    target()

    monkeypatch.setattr(
        gate,
        "enforce_native_anthropic_test_gate",
        lambda: pytest.skip("controlled native gate skip"),
    )
    with pytest.raises(pytest.skip.Exception, match="controlled native gate skip"):
        target()

    monkeypatch.setattr(
        gate,
        "enforce_native_anthropic_test_gate",
        lambda: pytest.fail("controlled native gate fail"),
    )
    with pytest.raises(pytest.fail.Exception, match="controlled native gate fail"):
        target()
