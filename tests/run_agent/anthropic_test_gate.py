"""Read-only gate for the native-Anthropic interrupt behavior test.

The test module deliberately keeps this gate separate from the interrupt
assertions. It never imports the optional SDK or resolves credentials.
"""

from __future__ import annotations

import importlib
import importlib.machinery
import importlib.util
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from importlib import _bootstrap
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from hermes_cli.fallback_config import get_fallback_chain
from hermes_cli.provider_selection import resolve_requested_provider_from_model_config
from hermes_cli.providers import normalize_provider
from hermes_cli.profiles import profiles_to_serve
from tests.collection_environment import OriginalCollectionEnvironment


_SECRET_PROMPT_MODULE = "hermes_cli.secret_prompt"
_CONFIG_MODULE = "hermes_cli.config"
_MISSING_IMPORT_STATE = object()


@contextmanager
def _target_module_import_lock(module_name: str) -> Iterator[None]:
    """Hold CPython's real per-module lock used by imports of ``module_name``."""
    get_module_lock = getattr(_bootstrap, "_get_module_lock")
    lock = get_module_lock(module_name)
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


def _restore_module_entry(module_name: str, prior: object) -> None:
    if prior is _MISSING_IMPORT_STATE:
        sys.modules.pop(module_name, None)
    else:
        sys.modules[module_name] = prior  # type: ignore[assignment]


def _restore_parent_attribute(parent: ModuleType, name: str, prior: object) -> None:
    if prior is _MISSING_IMPORT_STATE:
        vars(parent).pop(name, None)
    else:
        setattr(parent, name, prior)


def _load_config_audit_helpers() -> tuple[Any, Any]:
    """Load strict config auditing without retaining its unused secret import."""
    import hermes_cli

    with _target_module_import_lock(_SECRET_PROMPT_MODULE):
        prior_module = sys.modules.get(_SECRET_PROMPT_MODULE, _MISSING_IMPORT_STATE)
        prior_parent = vars(hermes_cli).get(
            "secret_prompt", _MISSING_IMPORT_STATE
        )
        stub_spec: importlib.machinery.ModuleSpec | None = None
        try:
            config_preloaded = (
                _CONFIG_MODULE in sys.modules
                and sys.modules[_CONFIG_MODULE] is not None
            )
            if prior_module is _MISSING_IMPORT_STATE and not config_preloaded:
                stub = ModuleType(_SECRET_PROMPT_MODULE)
                stub_spec = importlib.machinery.ModuleSpec(
                    _SECRET_PROMPT_MODULE, loader=None
                )
                # _find_and_load checks this flag before its fast sys.modules
                # return. Keeping it true makes simultaneous real imports wait
                # on the same target lock instead of observing the audit stub.
                stub_spec._initializing = True  # type: ignore[attr-defined]
                stub.__spec__ = stub_spec

                def deferred_masked_secret_prompt(*args, **kwargs):
                    """Resolve and invoke the real prompt only if later requested."""
                    prompt_module = importlib.import_module(_SECRET_PROMPT_MODULE)
                    masked_secret_prompt = prompt_module.masked_secret_prompt
                    if masked_secret_prompt is deferred_masked_secret_prompt:
                        raise RuntimeError(
                            "secret prompt is unavailable during config audit import"
                        )
                    return masked_secret_prompt(*args, **kwargs)

                stub.masked_secret_prompt = deferred_masked_secret_prompt  # type: ignore[attr-defined]
                sys.modules[_SECRET_PROMPT_MODULE] = stub
                setattr(hermes_cli, "secret_prompt", stub)

            config_module = importlib.import_module(_CONFIG_MODULE)
            return (
                config_module.is_provider_enabled,
                config_module.load_effective_config_readonly,
            )
        finally:
            if stub_spec is not None:
                stub_spec._initializing = False  # type: ignore[attr-defined]
            _restore_module_entry(_SECRET_PROMPT_MODULE, prior_module)
            _restore_parent_attribute(hermes_cli, "secret_prompt", prior_parent)


is_provider_enabled, load_effective_config_readonly = _load_config_audit_helpers()


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


@contextmanager
def _inspect_original_collection_environment(
    snapshot: OriginalCollectionEnvironment | None,
) -> Iterator[None]:
    """Temporarily restore only native-home path inputs, then restore the sandbox."""
    if snapshot is None:
        yield
        return
    if not isinstance(snapshot, OriginalCollectionEnvironment):
        raise TypeError("invalid original collection environment")

    previous = {
        name: (name in os.environ, os.environ.get(name))
        for name in ("HERMES_HOME", "HOME")
    }
    try:
        isolated_custom_home = False
        if snapshot.home is not None:
            try:
                import pwd

                isolated_custom_home = (
                    Path(snapshot.home).resolve()
                    != Path(pwd.getpwuid(os.getuid()).pw_dir).resolve()
                )
            except Exception:
                isolated_custom_home = False
        if snapshot.hermes_home_was_set:
            if snapshot.hermes_home is None:
                raise ValueError("missing original HERMES_HOME")
            os.environ["HERMES_HOME"] = snapshot.hermes_home
        elif not isolated_custom_home:
            os.environ.pop("HERMES_HOME", None)

        if snapshot.home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = snapshot.home
        yield
    finally:
        for name, (was_set, value) in previous.items():
            if was_set:
                if value is None:
                    raise RuntimeError(f"missing saved {name}")
                os.environ[name] = value
            else:
                os.environ.pop(name, None)


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


def find_native_anthropic_selections(
    collection_environment: OriginalCollectionEnvironment | None = None,
) -> list[NativeAnthropicSelection]:
    """Inspect canonical active profiles without auth, provisioning, or network I/O.

    ``profiles_to_serve(multiplex=True)`` is the production active-profile
    taxonomy: it excludes ``profiles/default`` and invalid/hidden identifiers,
    and intentionally follows a valid named directory symlink because its
    canonical ``Path.is_dir()`` filter follows symlinks.
    """
    try:
        with _inspect_original_collection_environment(collection_environment):
            selections: list[NativeAnthropicSelection] = []
            for profile_name, profile_home in profiles_to_serve(multiplex=True):
                scope = (
                    "default" if profile_name == "default" else f"profile:{profile_name}"
                )
                selections.extend(
                    _read_native_selections(profile_home / "config.yaml", scope)
                )
            return selections
    except Exception:
        raise NativeAnthropicGateInspectionError from None


def decide_native_anthropic_test_gate(
    collection_environment: OriginalCollectionEnvironment | None = None,
) -> GateDecision:
    """Choose whether the native-Anthropic interrupt test can run."""
    if native_anthropic_sdk_available():
        return GateDecision(GateAction.RUN, "Native Anthropic SDK is installed.")

    try:
        selections = find_native_anthropic_selections(collection_environment)
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
