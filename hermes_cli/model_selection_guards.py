"""Unified selection-time guard registry for model switching surfaces.

Hermes has multiple model-selection surfaces (CLI picker, TUI, dashboard,
gateway ``/model``, Telegram/Discord pickers, TUI-gateway RPC). Each of them
previously imported ``model_cost_guard.expensive_model_warning`` directly, so
every new guard class (e.g. the data-training-tier guard) had to be wired into
every surface by hand — and inevitably missed some.

This module is the single evaluation point: ``selection_warnings()`` runs every
registered guard and returns the warnings that fired. Surfaces render the
result with their own confirm UX (stdin prompt, modal, inline keyboard,
``confirm_required`` JSON) — that half stays per-surface; the *evaluation* half
lives here. Adding a guard to ``_GUARDS`` makes it appear on every surface at
once.

Guard modules (``model_cost_guard``, ``model_data_policy_guard``) keep their
public APIs — existing tests and mock patch points remain valid; this module
only aggregates them.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional

from agent.models_dev import ModelInfo


@dataclass(frozen=True)
class SelectionWarning:
    """A selection-time warning a surface must confirm before applying."""

    kind: str  # "cost" | "data_policy" | "context_cache" | future guard kinds
    title: str
    model: str
    provider: str
    message: str


@dataclass(frozen=True)
class SelectionContext:
    """Live-session facts a surface can thread into the guard registry.

    Guards that only need the target model (cost, data-policy) ignore this.
    Guards about the *switch itself* (context-cache) need to know how much
    conversation is at stake and what model the session is currently on.
    Surfaces without a live agent (setup wizard, dashboard scope assignment)
    simply omit it — session-dependent guards then stay silent.
    """

    context_tokens: Optional[int] = None
    current_model: Optional[str] = None


def selection_context_for_agent(agent: object) -> Optional[SelectionContext]:
    """Build a :class:`SelectionContext` from a live ``AIAgent``.

    Uses the compressor's measured ``last_prompt_tokens`` (what the provider
    actually billed on the latest turn) and falls back to the session prompt
    counter. Returns ``None`` when no live size is known — the context-cache
    guard then stays silent rather than guessing.
    """
    if agent is None:
        return None
    tokens = 0
    try:
        cc = getattr(agent, "context_compressor", None)
        tokens = int(getattr(cc, "last_prompt_tokens", 0) or 0) if cc else 0
        if tokens <= 0:
            tokens = int(getattr(agent, "session_prompt_tokens", 0) or 0)
    except Exception:
        tokens = 0
    if tokens <= 0:
        return None
    return SelectionContext(
        context_tokens=tokens,
        current_model=(getattr(agent, "model", "") or "") or None,
    )


def _cost_guard(
    model_name: str,
    provider: Optional[str],
    base_url: Optional[str],
    api_key: Optional[str],
    model_info: Optional[ModelInfo],
    ctx: Optional[SelectionContext] = None,
) -> Optional[SelectionWarning]:
    from hermes_cli.model_cost_guard import expensive_model_warning

    warning = expensive_model_warning(
        model_name,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model_info=model_info,
    )
    if warning is None:
        return None
    # Duck-typed access: tests (and future guard payloads) may supply objects
    # carrying only ``.message``.
    return SelectionWarning(
        kind="cost",
        title="Expensive Model Warning",
        model=getattr(warning, "model", model_name),
        provider=getattr(warning, "provider", provider or ""),
        message=warning.message,
    )


def _data_policy_guard(
    model_name: str,
    provider: Optional[str],
    base_url: Optional[str],
    api_key: Optional[str],
    model_info: Optional[ModelInfo],
    ctx: Optional[SelectionContext] = None,
) -> Optional[SelectionWarning]:
    from hermes_cli.model_data_policy_guard import data_training_warning

    warning = data_training_warning(
        model_name,
        provider=provider,
        base_url=base_url,
    )
    if warning is None:
        return None
    return SelectionWarning(
        kind="data_policy",
        title="Data-Training Tier Warning",
        model=getattr(warning, "model", model_name),
        provider=getattr(warning, "provider", provider or ""),
        message=warning.message,
    )


# Default context-token threshold above which a mid-session model switch asks
# for confirmation (the next call after a switch re-reads the whole context
# uncached — providers key prompt caches per model). Mirrors deepagents'
# `warnings.model_switch_token_threshold` (langchain-ai/deepagents#5829).
DEFAULT_CONTEXT_CACHE_SWITCH_THRESHOLD = 100_000


def _context_cache_threshold() -> int:
    """Resolve the confirm threshold from config.yaml (0 disables)."""
    try:
        from hermes_cli.config import load_config

        cfg = load_config() or {}
        model_cfg = cfg.get("model", {})
        if isinstance(model_cfg, dict):
            raw = model_cfg.get("switch_context_confirm_tokens")
            if raw is not None:
                return max(0, int(raw))
    except Exception:
        pass
    return DEFAULT_CONTEXT_CACHE_SWITCH_THRESHOLD


def _context_cache_guard(
    model_name: str,
    provider: Optional[str],
    base_url: Optional[str],
    api_key: Optional[str],
    model_info: Optional[ModelInfo],
    ctx: Optional[SelectionContext] = None,
) -> Optional[SelectionWarning]:
    """Confirm mid-session switches that abandon a large cached context.

    Providers key prompt caches per model, so the first call after a switch
    re-reads the entire conversation at full input price. On a large session
    that is real money and easy to trigger by accident from a picker. Fires
    only when the surface supplied live-session facts (``ctx``) showing the
    active context exceeds the configured threshold; sessions below it, empty
    sessions, and no-op re-selects of the current model stay silent.
    """
    if ctx is None or not ctx.context_tokens:
        return None
    target = (model_name or "").strip()
    current = (ctx.current_model or "").strip()
    if not target or (current and target == current):
        return None  # same-model re-select keeps the cache warm
    threshold = _context_cache_threshold()
    if threshold <= 0 or int(ctx.context_tokens) < threshold:
        return None
    tokens = int(ctx.context_tokens)
    lines = [
        "!!! LARGE CONTEXT MODEL SWITCH !!!",
        "",
        f"This session holds ~{tokens:,} tokens of context.",
        f"Switching to {target} makes the next reply re-read all of it "
        "uncached (providers key prompt caches per model) — a one-time "
        "full-price input cost.",
        "",
        "Threshold: model.switch_context_confirm_tokens "
        f"(currently {threshold:,}; 0 disables this check).",
        "Confirm only if you intend to switch now.",
    ]
    return SelectionWarning(
        kind="context_cache",
        title="Large Context Switch Warning",
        model=target,
        provider=(provider or "").strip(),
        message="\n".join(lines),
    )


# Registry, evaluated in order. Add new guard classes here — never at the
# individual surfaces.
_GUARDS = (
    _cost_guard,
    _data_policy_guard,
    _context_cache_guard,
)


def selection_warnings(
    model_name: str,
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model_info: Optional[ModelInfo] = None,
    include_kinds: Optional[Iterable[str]] = None,
    selection_context: Optional[SelectionContext] = None,
) -> List[SelectionWarning]:
    """Run every registered selection guard and return the warnings that fired.

    Returns an empty list in the common case (no guard fired). Callers should
    run this after model resolution so aliases / provider-specific ids have
    settled, then surface the messages as a confirm step. ``include_kinds``
    optionally restricts which guard kinds run (e.g. auth.py's picker only runs
    the cost guard when a provider is known, but always runs the data-policy
    guard).

    A misbehaving guard must never break model selection: individual guard
    exceptions are swallowed.
    """
    wanted = set(include_kinds) if include_kinds is not None else None
    results: List[SelectionWarning] = []
    for guard in _GUARDS:
        try:
            warning = guard(
                model_name, provider, base_url, api_key, model_info,
                selection_context,
            )
        except TypeError:
            # Back-compat: externally patched 5-arg guards (tests, plugins).
            try:
                warning = guard(model_name, provider, base_url, api_key, model_info)
            except Exception:
                continue
        except Exception:
            continue
        if warning is None:
            continue
        if wanted is not None and warning.kind not in wanted:
            continue
        results.append(warning)
    return results


def combined_message(warnings: List[SelectionWarning]) -> str:
    """Join multiple warnings into one confirm-prompt body.

    Surfaces that show a single confirm dialog use this when more than one
    guard fires (rare) — one prompt showing both blocks beats two sequential
    prompts.
    """
    return "\n\n".join(w.message for w in warnings)


def combined_selection_warning(
    model_name: str,
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
    api_key: Optional[str] = None,
    model_info: Optional[ModelInfo] = None,
    selection_context: Optional[SelectionContext] = None,
) -> Optional[SelectionWarning]:
    """Drop-in replacement for ``expensive_model_warning`` call sites.

    Returns ``None`` when no guard fired, a single :class:`SelectionWarning`
    when exactly one fired, or a merged warning (``kind="multiple"``) whose
    ``message`` stacks every fired guard. Surfaces that render one confirm
    dialog with ``warning.message`` can switch to this without reshaping their
    control flow.
    """
    warnings = selection_warnings(
        model_name,
        provider=provider,
        base_url=base_url,
        api_key=api_key,
        model_info=model_info,
        selection_context=selection_context,
    )
    if not warnings:
        return None
    if len(warnings) == 1:
        return warnings[0]
    return SelectionWarning(
        kind="multiple",
        title="Model Selection Warning",
        model=warnings[0].model,
        provider=warnings[0].provider,
        message=combined_message(warnings),
    )
