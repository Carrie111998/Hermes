"""Shared subagent-model selection semantics for every Hermes surface.

The override is profile-scoped and persisted only as
``delegation.model`` / ``delegation.provider``.  Model/provider resolution is
not reimplemented here: all typed and picker selections go through the same
``model_switch.switch_model`` pipeline as ``/model`` so aliases, configured
providers, credentials, catalog validation, and provider-specific
normalization cannot drift between surfaces.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass(frozen=True)
class SubagentModelStatus:
    """Persisted subagent override; no model means inherit the parent."""

    model: Optional[str]
    provider: Optional[str]
    inherits_parent: bool


def _status_from_config(config: dict[str, Any]) -> SubagentModelStatus:
    delegation = config.get("delegation")
    if not isinstance(delegation, dict):
        delegation = {}
    model = str(delegation.get("model") or "").strip() or None
    provider = str(delegation.get("provider") or "").strip() or None
    # A provider without a model is not a usable override. Treat legacy or
    # partially-written config as inheritance instead of constructing a child
    # with an empty model.
    if not model:
        return SubagentModelStatus(None, None, True)
    return SubagentModelStatus(model, provider, False)


def get_subagent_model_status() -> SubagentModelStatus:
    from hermes_cli.config import load_config

    return _status_from_config(load_config())


def _persist_override(model: Optional[str], provider: Optional[str]) -> SubagentModelStatus:
    """Atomically persist (or clear) the two-key delegation override."""

    from hermes_cli.config import load_config, save_config

    config = load_config()
    delegation = config.get("delegation")
    if not isinstance(delegation, dict):
        delegation = {}
    else:
        delegation = dict(delegation)

    if model:
        delegation["model"] = str(model).strip()
        if provider:
            delegation["provider"] = str(provider).strip()
        else:
            delegation.pop("provider", None)
    else:
        delegation.pop("model", None)
        delegation.pop("provider", None)

    if delegation:
        config["delegation"] = delegation
    else:
        config.pop("delegation", None)
    save_config(config)
    return _status_from_config(config)


def persist_subagent_switch_result(result: Any) -> SubagentModelStatus:
    """Commit a successful shared ``ModelSwitchResult`` to delegation config."""

    if not getattr(result, "success", False):
        message = str(getattr(result, "error_message", "") or "Invalid subagent model")
        raise ValueError(message)
    model = str(getattr(result, "new_model", "") or "").strip()
    provider = str(getattr(result, "target_provider", "") or "").strip()
    if not model:
        raise ValueError("Model selection resolved to an empty model")
    return _persist_override(model, provider or None)


def resolve_subagent_model(model: str, *, provider: Optional[str] = None):
    """Resolve and validate through the canonical model-switch pipeline."""

    raw_model = str(model or "").strip()
    if not raw_model:
        raise ValueError("model is required")

    from hermes_cli.inventory import load_picker_context
    from hermes_cli.model_switch import switch_model

    context = load_picker_context()
    result = switch_model(
        raw_input=raw_model,
        current_provider=context.current_provider,
        current_model=context.current_model,
        current_base_url=context.current_base_url,
        current_api_key="",
        is_global=False,
        explicit_provider=str(provider or "").strip(),
        user_providers=context.user_providers,
        custom_providers=context.custom_providers,
    )
    if not result.success:
        raise ValueError(result.error_message or "Invalid subagent model")
    return result


def set_subagent_model(model: str, *, provider: Optional[str] = None) -> SubagentModelStatus:
    """Resolve, validate, normalize, then persist a subagent override."""

    return persist_subagent_switch_result(resolve_subagent_model(model, provider=provider))


def reset_subagent_model() -> SubagentModelStatus:
    """Remove only model/provider; preserve every other delegation setting."""

    return _persist_override(None, None)


def list_subagent_picker_providers(*, refresh: bool = False) -> list[dict[str, Any]]:
    """Return the same authenticated provider/model inventory as model pickers."""

    if refresh:
        try:
            from hermes_cli.models import clear_provider_models_cache

            clear_provider_models_cache()
        except Exception:
            pass

    from hermes_cli.inventory import build_models_payload, load_picker_context

    context = load_picker_context()
    return list(
        build_models_payload(
            context,
            probe_custom_providers=refresh,
            probe_current_custom_provider=not refresh,
        ).get("providers")
        or []
    )


def _canonical_picker_provider(model_config: Any, full_config: dict[str, Any]) -> str:
    """Return the runtime-addressable provider selected by the full picker.

    The manual custom-endpoint flow temporarily writes ``model.provider=custom``
    plus ``model.base_url`` and then saves a named ``custom_providers`` entry.
    Delegation must persist that entry's canonical ``custom:<name>`` slug; bare
    ``custom`` is ambiguous when more than one endpoint exists.
    """

    if not isinstance(model_config, dict):
        return ""
    provider = str(model_config.get("provider") or "").strip()
    if provider != "custom":
        return provider

    from hermes_cli.config import get_compatible_custom_providers
    from hermes_cli.providers import custom_provider_slug
    from hermes_cli.route_identity import normalize_route_base_url

    selected_url = normalize_route_base_url(model_config.get("base_url"))
    if not selected_url:
        raise ValueError("Custom model selection did not persist an endpoint URL")
    for entry in get_compatible_custom_providers(full_config):
        if not isinstance(entry, dict):
            continue
        if normalize_route_base_url(entry.get("base_url")) != selected_url:
            continue
        identity = str(entry.get("provider_key") or entry.get("name") or "").strip()
        if identity:
            return custom_provider_slug(identity)
    raise ValueError(
        "Custom endpoint was selected but no matching saved custom provider was found"
    )


def select_subagent_model_interactively(
    *, refresh: bool = False
) -> Optional[SubagentModelStatus]:
    """Run the complete ``hermes model`` flow for the delegation target.

    Provider logins, credentials, custom-provider additions, and auxiliary
    configuration are deliberately retained.  The active primary model route
    and auth provider are restored before the confirmed selection is committed
    under ``delegation.*``.
    """

    import copy

    from hermes_cli.auth import capture_model_selection
    from hermes_cli.config import load_config
    from hermes_cli.fallback_cmd import (
        _restore_auth_active_provider,
        _restore_model_cfg,
        _snapshot_auth_active_provider,
    )
    from hermes_cli.main import select_provider_and_model

    if refresh:
        try:
            from hermes_cli.models import clear_provider_models_cache

            clear_provider_models_cache()
        except Exception:
            pass

    before_config = load_config()
    model_before = copy.deepcopy(before_config.get("model"))
    active_provider_before = _snapshot_auth_active_provider()
    selected_config: Optional[dict[str, Any]] = None
    selections: list[str] = []

    print()
    print("  Select the provider + model to use for subagents.")
    print("  This is the full `hermes model` setup flow: provider login and custom")
    print("  provider additions are kept; your active primary model is unchanged.")
    print()

    try:
        with capture_model_selection() as selections:
            select_provider_and_model()
        if selections:
            selected_config = copy.deepcopy(load_config())
    finally:
        _restore_model_cfg(model_before)
        _restore_auth_active_provider(active_provider_before)

    if not selections or selected_config is None:
        return None

    model_config = selected_config.get("model")
    selected_model = selections[-1]
    selected_provider = _canonical_picker_provider(model_config, selected_config)
    return set_subagent_model(selected_model, provider=selected_provider or None)
