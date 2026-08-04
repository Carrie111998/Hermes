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

from hermes_cli.config import read_user_config_raw
from hermes_cli.fallback_config import get_fallback_chain
from hermes_cli.profiles import _get_profiles_root
from hermes_constants import get_default_hermes_root


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


def native_anthropic_sdk_available() -> bool:
    """Return whether the optional native Anthropic SDK is installed.

    ``find_spec`` only inspects import metadata: it does not import the SDK,
    invoke Hermes lazy-dependency installation, resolve credentials, or touch
    an auth pool.
    """
    return importlib.util.find_spec("anthropic") is not None


def _is_native_anthropic_provider(provider: Any) -> bool:
    """Identify explicit provider spellings that source semantics route native."""
    return str(provider or "").strip().casefold() in {
        "anthropic",
        "claude",
        "claude-code",
    }


def _read_native_selections(config_path: Path, scope: str) -> list[NativeAnthropicSelection]:
    """Read explicit primary and fallback providers from one raw config file."""
    config = read_user_config_raw(config_path)
    selections: list[NativeAnthropicSelection] = []

    model = config.get("model")
    if isinstance(model, dict):
        provider = model.get("provider")
        if _is_native_anthropic_provider(provider):
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
        if not _is_native_anthropic_provider(provider):
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
    """Inspect default and active profile configs without loading auth state.

    The root and profiles directory come from canonical Hermes path helpers.
    Only direct children of the active ``profiles/`` directory count; retired
    or quarantined copies elsewhere are intentionally outside this taxonomy.
    """
    root = get_default_hermes_root()
    selections = _read_native_selections(root / "config.yaml", "default")

    profiles_root = _get_profiles_root()
    try:
        profile_dirs = sorted(
            (path for path in profiles_root.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )
    except FileNotFoundError:
        profile_dirs = []

    for profile_dir in profile_dirs:
        selections.extend(
            _read_native_selections(
                profile_dir / "config.yaml",
                f"profile:{profile_dir.name}",
            )
        )
    return selections


def decide_native_anthropic_test_gate() -> GateDecision:
    """Choose whether the native-Anthropic interrupt test can run."""
    if native_anthropic_sdk_available():
        return GateDecision(GateAction.RUN, "Native Anthropic SDK is installed.")

    selections = find_native_anthropic_selections()
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


def enforce_native_anthropic_test_gate() -> None:
    """Skip or fail the caller according to the read-only gate decision."""
    decision = decide_native_anthropic_test_gate()
    if decision.action is GateAction.SKIP:
        pytest.skip(decision.reason)
    if decision.action is GateAction.FAIL:
        pytest.fail(decision.reason, pytrace=False)
