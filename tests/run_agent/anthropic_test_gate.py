"""Read-only gate for the native-Anthropic interrupt behavior test.

The test module deliberately keeps this gate separate from the interrupt
assertions. It never imports the optional SDK or resolves credentials.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

import pytest

from hermes_cli.config import is_provider_enabled, load_effective_config_readonly
from hermes_cli.fallback_config import get_fallback_chain
from hermes_cli.provider_selection import resolve_requested_provider_from_model_config
from hermes_cli.providers import normalize_provider
from hermes_cli.profiles import profiles_to_serve


_CONFIG_INSPECTION_FAILURE_REASON = (
    "The native Anthropic test gate could not safely inspect active Hermes configuration. "
    "Repair configuration access before running this test."
)


class GateAction(str, Enum):
    """The action the native-Anthropic test gate must take."""

    RUN = "run"
    SKIP = "skip"
    FAIL = "fail"


@dataclass(frozen=True)
class GateDecision:
    """A gate result with an operator-readable explanation."""

    action: GateAction
    reason: str


@dataclass(frozen=True)
class NativeAnthropicSelection:
    """One explicit native-Anthropic configuration selection."""

    scope: str
    source: str
    fallback_index: int | None
    provider: str
    config_path: Path


class NativeAnthropicGateInspectionError(RuntimeError):
    """A configuration/profile read was unsafe to classify as an inactive route."""


def native_anthropic_sdk_available() -> bool:
    """Return whether the optional native Anthropic SDK is installed.

    ``find_spec`` only inspects import metadata: it does not import the SDK,
    invoke Hermes lazy-dependency installation, resolve credentials, or touch
    an auth pool.
    """
    return importlib.util.find_spec("anthropic") is not None


def _canonical_provider(provider: Any) -> str:
    """Use Hermes' provider identity taxonomy without loading provider metadata."""
    return normalize_provider(str(provider or ""))


def _is_native_anthropic_provider(provider: Any) -> bool:
    """Identify provider spellings whose canonical runtime route is Anthropic."""
    return _canonical_provider(provider) == "anthropic"


def _provider_enabled(config: dict[str, Any], provider: Any) -> bool:
    """Mirror runtime's raw-requested-provider enabled check exactly.

    ``resolve_runtime_provider`` looks up ``providers.<requested_provider>``
    before canonical resolution. The gate therefore must not scan equivalent
    alias keys: canonicalization is only for classifying the eventual route.
    """
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return True
    raw_name = str(provider or "").strip().lower()
    provider_config = providers.get(raw_name)
    return not (
        isinstance(provider_config, dict) and not is_provider_enabled(provider_config)
    )


def _read_native_selections(config_path: Path, scope: str) -> list[NativeAnthropicSelection]:
    """Inspect effective primary/fallback routing for one active profile home."""
    config = load_effective_config_readonly(config_path)
    selections: list[NativeAnthropicSelection] = []

    provider = resolve_requested_provider_from_model_config(config.get("model"))
    if _is_native_anthropic_provider(provider) and _provider_enabled(config, provider):
        selections.append(
            NativeAnthropicSelection(
                scope=scope,
                source="primary",
                fallback_index=None,
                provider=str(provider).strip(),
                config_path=config_path,
            )
        )

    for index, entry in enumerate(get_fallback_chain(config)):
        provider = entry.get("provider")
        if not _is_native_anthropic_provider(provider) or not _provider_enabled(config, provider):
            continue
        selections.append(
            NativeAnthropicSelection(
                scope=scope,
                source="fallback",
                fallback_index=index,
                provider=str(provider).strip(),
                config_path=config_path,
            )
        )

    return selections


def find_native_anthropic_selections() -> list[NativeAnthropicSelection]:
    """Inspect canonical active profiles without auth, provisioning, or network I/O.

    ``profiles_to_serve(multiplex=True)`` is the production active-profile
    taxonomy: it excludes ``profiles/default`` and invalid/hidden identifiers,
    and intentionally follows a valid named directory symlink because its
    canonical ``Path.is_dir()`` filter follows symlinks.
    """
    try:
        selections: list[NativeAnthropicSelection] = []
        for profile_name, profile_home in profiles_to_serve(multiplex=True):
            scope = "default" if profile_name == "default" else f"profile:{profile_name}"
            selections.extend(_read_native_selections(profile_home / "config.yaml", scope))
        return selections
    except Exception:
        raise NativeAnthropicGateInspectionError from None


def decide_native_anthropic_test_gate() -> GateDecision:
    """Choose whether the native-Anthropic interrupt test can run."""
    if native_anthropic_sdk_available():
        return GateDecision(GateAction.RUN, "Native Anthropic SDK is installed.")

    try:
        selections = find_native_anthropic_selections()
    except NativeAnthropicGateInspectionError:
        return GateDecision(GateAction.FAIL, _CONFIG_INSPECTION_FAILURE_REASON)
    if selections:
        selection = selections[0]
        return GateDecision(
            GateAction.FAIL,
            "The native Anthropic provider is selected by active Hermes configuration "
            f"({selection.scope} {selection.source}). Restore the supported Anthropic SDK "
            "before running this test.",
        )

    return GateDecision(
        GateAction.SKIP,
        "Native Anthropic SDK is not installed and no active Hermes configuration "
        "selects the native Anthropic provider.",
    )


def enforce_native_anthropic_test_gate(decision: GateDecision | None = None) -> None:
    """Skip or fail using a current decision or an immutable audited snapshot."""
    if decision is None:
        decision = decide_native_anthropic_test_gate()
    elif not (
        isinstance(decision, GateDecision)
        and isinstance(decision.action, GateAction)
        and isinstance(decision.reason, str)
    ):
        pytest.fail(_CONFIG_INSPECTION_FAILURE_REASON, pytrace=False)
    if decision.action is GateAction.SKIP:
        pytest.skip(decision.reason)
    if decision.action is GateAction.FAIL:
        pytest.fail(decision.reason, pytrace=False)
