"""Contract tests for the native-Anthropic interrupt-test gate."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import cast

import pytest

from tests.run_agent import anthropic_test_gate as gate


def _write_config(home: Path, text: str) -> None:
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _write_mapping(home: Path, config: dict) -> None:
    import yaml

    _write_config(home, yaml.safe_dump(config, sort_keys=False))


def _route_config(source: str, provider: str, provider_blocks: dict) -> dict:
    config = {
        "model": {
            "provider": provider if source == "primary" else "openrouter",
            "default": "claude-sonnet-4.6" if source == "primary" else "openai/gpt-5.6",
        },
        "providers": provider_blocks,
    }
    if source == "fallback":
        config["fallback_providers"] = [
            {"provider": provider, "model": "claude-sonnet-4.6"}
        ]
    return config


def _probe_runtime_route(monkeypatch, config: dict, source: str, raw_provider: str) -> dict:
    """Exercise runtime's raw enabled lookup without resolving credentials."""
    from hermes_cli import config as config_module
    from hermes_cli import runtime_provider

    monkeypatch.setattr(config_module, "load_config", lambda: config)
    monkeypatch.setattr(runtime_provider, "_get_model_config", lambda: config["model"])
    monkeypatch.setattr(
        runtime_provider,
        "_resolve_named_custom_runtime",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        runtime_provider,
        "_resolve_explicit_runtime",
        lambda *, provider, requested_provider, **_kwargs: {
            "provider": provider,
            "requested_provider": requested_provider,
            "source": "test",
        },
    )
    return runtime_provider.resolve_runtime_provider(
        requested=None if source == "primary" else raw_provider,
        explicit_api_key="x",
    )


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
    monkeypatch.delenv("HERMES_MANAGED_DIR", raising=False)
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


@pytest.mark.parametrize("failure", ("malformed", "unreadable", "unstatable"))
def test_explicit_managed_scope_failure_fails_gate_without_secret_diagnostic(
    hermes_root, monkeypatch, failure
):
    """A selected managed scope cannot fail open during the audit."""
    _write_config(
        hermes_root,
        "model:\n"
        "  provider: openrouter\n"
        "  default: openai/gpt-5.6\n",
    )
    managed_dir = hermes_root.parent / "managed-scope"
    managed_dir.mkdir()
    managed_config = managed_dir / "config.yaml"
    secret = f"d2-c03-managed-{failure}-secret"
    if failure == "malformed":
        managed_config.write_text(
            "model: [anthropic\n" f"secret: {secret}\n",
            encoding="utf-8",
        )
    else:
        managed_config.write_text(
            "model:\n"
            "  provider: anthropic\n"
            "  default: claude-sonnet-4.6\n",
            encoding="utf-8",
        )
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed_dir))
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    if failure == "unreadable":
        import builtins

        real_open = builtins.open

        def deny_managed_config(path, *args, **kwargs):
            if Path(path) == managed_config:
                raise PermissionError(secret)
            return real_open(path, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", deny_managed_config)
    elif failure == "unstatable":
        real_stat = Path.stat

        def deny_managed_dir(path, *args, **kwargs):
            if path == managed_dir:
                raise PermissionError(secret)
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", deny_managed_dir)
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    decision = gate.decide_native_anthropic_test_gate()

    assert decision.action is gate.GateAction.FAIL
    assert decision.reason == gate._CONFIG_INSPECTION_FAILURE_REASON
    assert secret not in decision.reason


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


@pytest.mark.parametrize("source", ("primary", "fallback"))
@pytest.mark.parametrize(
    ("case", "raw_provider", "provider_blocks", "runtime_allows"),
    [
        pytest.param(
            "raw canonical disabled",
            "anthropic",
            {"anthropic": {"enabled": False}},
            False,
            id="raw-canonical-disabled",
        ),
        pytest.param(
            "raw canonical with alias-only disabled block",
            "anthropic",
            {"claude": {"enabled": False}},
            True,
            id="raw-canonical-alias-only",
        ),
        pytest.param(
            "raw claude alias disabled",
            "claude",
            {"claude": {"enabled": "false"}},
            False,
            id="raw-claude-disabled",
        ),
        pytest.param(
            "raw claude-code alias disabled",
            "claude-code",
            {"claude-code": {"enabled": "off"}},
            False,
            id="raw-claude-code-disabled",
        ),
        pytest.param(
            "raw claude with canonical-only disabled block",
            "claude",
            {"anthropic": {"enabled": False}},
            True,
            id="raw-claude-canonical-only",
        ),
        pytest.param(
            "raw claude-code with canonical-only disabled block",
            "claude-code",
            {"anthropic": {"enabled": False}},
            True,
            id="raw-claude-code-canonical-only",
        ),
        pytest.param(
            "raw claude with other-alias-only disabled block",
            "claude",
            {"claude-code": {"enabled": False}},
            True,
            id="raw-claude-other-alias-only",
        ),
        pytest.param(
            "raw claude-code with other-alias-only disabled block",
            "claude-code",
            {"claude": {"enabled": False}},
            True,
            id="raw-claude-code-other-alias-only",
        ),
        pytest.param(
            "raw disabled wins over canonical enabled",
            "claude",
            {"claude": {"enabled": False}, "anthropic": {"enabled": True}},
            False,
            id="raw-disabled-canonical-enabled",
        ),
        pytest.param(
            "raw enabled wins over canonical disabled",
            "claude",
            {"claude": {"enabled": True}, "anthropic": {"enabled": False}},
            True,
            id="raw-enabled-canonical-disabled",
        ),
        pytest.param(
            "raw claude-code disabled wins over canonical enabled",
            "claude-code",
            {"claude-code": {"enabled": False}, "anthropic": {"enabled": True}},
            False,
            id="raw-claude-code-disabled-canonical-enabled",
        ),
        pytest.param(
            "raw claude-code enabled wins over canonical disabled",
            "claude-code",
            {"claude-code": {"enabled": True}, "anthropic": {"enabled": False}},
            True,
            id="raw-claude-code-enabled-canonical-disabled",
        ),
    ],
)
def test_native_gate_matches_runtime_raw_requested_provider_enabled_lookup(
    hermes_root,
    monkeypatch,
    source,
    case,
    raw_provider,
    provider_blocks,
    runtime_allows,
):
    """The gate checks the same raw provider block as production runtime."""
    config = _route_config(source, raw_provider, provider_blocks)
    _write_mapping(hermes_root, config)
    monkeypatch.setattr(gate, "native_anthropic_sdk_available", lambda: False)

    if runtime_allows:
        runtime = _probe_runtime_route(monkeypatch, config, source, raw_provider)
        assert runtime["requested_provider"] == raw_provider, case
        assert runtime["provider"] == "anthropic", case
    else:
        with pytest.raises(ValueError, match=raw_provider):
            _probe_runtime_route(monkeypatch, config, source, raw_provider)

    decision = gate.decide_native_anthropic_test_gate()

    assert (decision.action is gate.GateAction.FAIL) is runtime_allows, case


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

    monkeypatch.setattr(gate, "enforce_native_anthropic_test_gate", lambda *_args, **_kwargs: None)
    target()

    monkeypatch.setattr(
        gate,
        "enforce_native_anthropic_test_gate",
        lambda *_args, **_kwargs: pytest.skip("controlled native gate skip"),
    )
    with pytest.raises(pytest.skip.Exception, match="controlled native gate skip"):
        target()

    monkeypatch.setattr(
        gate,
        "enforce_native_anthropic_test_gate",
        lambda *_args, **_kwargs: pytest.fail("controlled native gate fail"),
    )
    with pytest.raises(pytest.fail.Exception, match="controlled native gate fail"):
        target()


def test_interrupt_target_exposes_immutable_collection_gate_snapshot():
    """The real interrupt target receives a decision captured at import time."""
    from tests.run_agent import test_run_agent as interrupt_tests

    snapshot = interrupt_tests._NATIVE_ANTHROPIC_INTERRUPT_GATE_DECISION

    assert isinstance(snapshot, gate.GateDecision)
    assert snapshot == gate.GateDecision(snapshot.action, snapshot.reason)
    with pytest.raises(FrozenInstanceError):
        setattr(snapshot, "reason", "must remain immutable")


def test_gate_rejects_an_invalid_audit_snapshot_without_diagnostic_details():
    """Only a frozen GateDecision may bypass a recomputed audit."""
    with pytest.raises(pytest.fail.Exception, match="could not safely inspect"):
        gate.enforce_native_anthropic_test_gate(cast(gate.GateDecision, object()))


@pytest.mark.parametrize("estate", ("home-config", "provider-override"))
def test_pre_fixture_estate_snapshot_survives_real_hermetic_fixture(tmp_path, estate):
    """Collection captures native routing before the real autouse fixture mutates it."""
    if gate.native_anthropic_sdk_available():
        pytest.skip("pre-fixture regression requires the SDK to remain absent")

    project_root = Path(__file__).resolve().parents[2]
    pre_fixture_home = tmp_path / "pre-fixture-home"
    pre_fixture_home.mkdir()
    if estate == "home-config":
        _write_config(
            pre_fixture_home,
            "model:\n"
            "  provider: anthropic\n"
            "  default: claude-sonnet-4.6\n",
        )
    else:
        _write_config(
            pre_fixture_home,
            "model:\n"
            "  default: claude-sonnet-4.6\n",
        )
    managed_dir = tmp_path / "managed"
    managed_dir.mkdir()
    (managed_dir / "config.yaml").write_text("{}\n", encoding="utf-8")
    child_home = tmp_path / "child-home"
    child_home.mkdir()
    child_test = tmp_path / "test_prefixture_gate.py"
    child_test.write_text(
        "from tests.run_agent.test_run_agent import TestAnthropicInterruptHandler\n"
        "\n"
        "def test_collection_snapshot_is_enforced_after_fixture_mutation():\n"
        "    TestAnthropicInterruptHandler().test_interruptible_anthropic_interrupt_never_closes_shared_client()\n",
        encoding="utf-8",
    )
    child_env = {
        "PATH": os.environ["PATH"],
        "HOME": str(child_home),
        "HERMES_HOME": str(pre_fixture_home),
        "HERMES_MANAGED_DIR": str(managed_dir),
        "PYTHONHASHSEED": "0",
        "PYTHONPATH": str(project_root),
        "TZ": "UTC",
    }
    if estate == "provider-override":
        child_env["HERMES_INFERENCE_PROVIDER"] = "claude"

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "tests.conftest",
            str(child_test),
            "-q",
            "-rs",
        ],
        cwd=project_root,
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    child_output = result.stdout + result.stderr

    assert result.returncode == 1, child_output
    assert "native Anthropic provider is selected" in child_output
    assert "skipped" not in child_output.lower()
