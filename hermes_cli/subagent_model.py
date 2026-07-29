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


def select_subagent_model_interactively(
    *, refresh: bool = False
) -> Optional[SubagentModelStatus]:
    """Run the shell CLI picker using Hermes' dependency-free curses UI."""

    from hermes_cli.curses_ui import curses_radiolist

    providers = [
        row for row in list_subagent_picker_providers(refresh=refresh) if row.get("slug")
    ]
    status = get_subagent_model_status()
    provider_labels = [
        "Inherit parent model (reset override)",
        *[
            f"{row.get('name') or row.get('slug')} ({len(row.get('models') or [])} models)"
            for row in providers
        ],
    ]
    selected_provider_index = 0
    if not status.inherits_parent:
        selected_provider_index = next(
            (
                index
                for index, row in enumerate(providers, start=1)
                if str(row.get("slug") or "") == status.provider
            ),
            0,
        )

    provider_index = curses_radiolist(
        "Select subagent provider:",
        provider_labels,
        selected=selected_provider_index,
        cancel_returns=-1,
        searchable=True,
        search_labels=provider_labels,
    )
    if provider_index < 0:
        return None
    if provider_index == 0:
        return reset_subagent_model()

    row = providers[provider_index - 1]
    selected_provider = str(row.get("slug") or "")
    models = [str(model) for model in row.get("models") or [] if str(model).strip()]
    if not models:
        raise ValueError(f"No authenticated models available for provider '{selected_provider}'")

    selected_model_index = 0
    if status.provider == selected_provider and status.model in models:
        selected_model_index = models.index(status.model)
    model_index = curses_radiolist(
        "Select subagent model:",
        models,
        selected=selected_model_index,
        cancel_returns=-1,
        searchable=True,
        search_labels=models,
    )
    if model_index < 0:
        return None
    return set_subagent_model(models[model_index], provider=selected_provider)
