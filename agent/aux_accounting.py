"""Ambient session-accounting context for auxiliary LLM calls.

Auxiliary calls (vision, compression, title generation, web_extract,
session_search, ...) funnel through ``agent.auxiliary_client`` which has no
session handle — so their token usage was historically discarded, leaving
dashboard analytics blind to aux model spend (issue #23270).

Instead of threading ``session_db``/``session_id`` parameters through every
aux call site, the agent loop publishes them here (mirroring the Nous Portal
conversation context in ``agent.portal_tags``) and the auxiliary client
records usage at its single response-validation chokepoint.

ContextVar semantics give us the right isolation for free:

* concurrent agents in one process (gateway sessions, delegate subagents)
  never see each other's accounting context;
* worker threads spawned via ``tools.thread_context.propagate_context_to_thread``
  (MoA fan-out, background review) inherit the parent turn's context;
* asyncio tasks inherit the context of the code that created them.

MoA reference/aggregator slots are explicitly EXCLUDED from recording:
``agent/conversation_loop.py`` already folds MoA advisor usage and cost into
the main loop's ``update_token_counts`` delta, so recording them here would
double-count (see ``_EXCLUDED_TASKS``).
"""

from __future__ import annotations

import logging
from contextvars import ContextVar
from typing import Any, Optional

logger = logging.getLogger(__name__)

# (session_db, session_id, task_id, lane, profile, route) for the active turn,
# or None outside one.
_accounting: ContextVar[Optional[tuple]] = ContextVar(
    "aux_accounting_context", default=None
)

# Aux tasks whose usage is already accounted by the main loop — recording
# them here would double-count. MoA advisor/aggregator usage is folded into
# conversation_loop's update_token_counts delta (tokens AND cost).
_EXCLUDED_TASKS = frozenset({"moa_reference", "moa_aggregator"})


def _cost_vendor(provider: Optional[str], base_url: Optional[str]) -> Optional[str]:
    from utils import base_url_host_matches

    provider_name = str(provider or "").strip().lower()
    if provider_name == "openai-codex":
        return "openai-codex"
    if provider_name == "openrouter" or base_url_host_matches(
        base_url or "", "openrouter.ai"
    ):
        return "openrouter"
    if provider_name == "anthropic" or base_url_host_matches(
        base_url or "", "api.anthropic.com"
    ):
        return "anthropic"
    if provider_name in {"openai", "openai-api"} or base_url_host_matches(
        base_url or "", "api.openai.com"
    ):
        return "openai"
    return None


def _record_aux_cost_gate_call(
    response: Any,
    task: Optional[str],
    *,
    provider: Optional[str],
    base_url: Optional[str],
) -> None:
    """Synchronously ledger one auxiliary call for CS-02 vendor routes."""
    vendor = _cost_vendor(provider, base_url)
    if vendor is None:
        return

    from agent.usage_pricing import estimate_usage_cost, normalize_usage
    from hermes_cli.cost.gate_integration import on_call_complete

    raw_usage = getattr(response, "usage", None)
    usage = normalize_usage(raw_usage, provider=provider)
    model = str(getattr(response, "model", "") or "") or "unknown"
    cost = estimate_usage_cost(
        model,
        usage,
        provider=provider,
        base_url=base_url,
    )
    reported_cost = getattr(raw_usage, "cost", None)
    try:
        usd_amount = (
            float(reported_cost)
            if vendor == "openrouter" and reported_cost is not None
            else float(cost.amount_usd)
            if cost.amount_usd is not None
            else 0.0
        )
    except (TypeError, ValueError):
        usd_amount = float(cost.amount_usd) if cost.amount_usd is not None else 0.0

    model_slug = model if "/" in model else f"{vendor}/{model}"
    ctx = _accounting.get()
    task_id = ctx[2] if ctx is not None and len(ctx) > 2 else None
    profile = ctx[4] if ctx is not None and len(ctx) > 4 else None
    route = ctx[5] if ctx is not None and len(ctx) > 5 else None
    session_id = ctx[1] if ctx is not None and len(ctx) > 1 else None
    if task_id is None:
        logger.warning(
            "Cost gate task unavailable for auxiliary call %s; "
            "using nullable task and platform fallback",
            task or "unknown",
        )
    on_call_complete(
        task_id=task_id,
        lane="platform",
        vendor=vendor,
        model_slug=model_slug,
        attempt_number=None,
        rung_id=None,
        escalation=False,
        input_tokens=usage.input_tokens,
        output_tokens=usage.output_tokens,
        cached_input_tokens=usage.cache_read_tokens + usage.cache_write_tokens,
        usd_amount=usd_amount,
        latency_ms=None,
        request_id=getattr(response, "id", None),
        raw_response_meta={
            "auxiliary_task": task,
            "cost_status": (
                "actual"
                if vendor == "openrouter" and reported_cost is not None
                else cost.status
            ),
            "cost_source": (
                "provider_response"
                if vendor == "openrouter" and reported_cost is not None
                else cost.source
            ),
            "pricing_version": cost.pricing_version,
        },
        profile=profile,
        route=route,
        session_id=session_id,
    )


def set_accounting_context(
    session_db: Any,
    session_id: Optional[str],
    *,
    task_id: Optional[str] = None,
    lane: Optional[str] = None,
    profile: Optional[str] = None,
    route: Optional[str] = None,
):
    """Publish the active session's accounting handles for aux usage recording.

    Called by the agent loop at turn entry. Returns the ContextVar token so
    callers can ``reset_accounting_context(token)`` on turn exit. Publishing
    ``None`` handles (no DB / no session id) clears the context.
    """
    if session_db is None or not session_id:
        return _accounting.set(None)
    return _accounting.set(
        (session_db, session_id, task_id, lane, profile, route)
    )


def reset_accounting_context(token) -> None:
    """Restore the previous accounting context (pair with ``set_...``)."""
    try:
        _accounting.reset(token)
    except Exception:
        _accounting.set(None)


def get_accounting_context() -> Optional[tuple]:
    """Return session, task, lane, profile, and route for the active turn."""
    return _accounting.get()


def _is_perplexity_route(
    provider: Optional[str], base_url: Optional[str]
) -> bool:
    from utils import base_url_host_matches

    provider_name = str(provider or "").strip().lower()
    return provider_name == "perplexity" or base_url_host_matches(
        base_url or "", "api.perplexity.ai"
    )


def _record_perplexity_aux_cost(
    *,
    task_id: Optional[str],
    lane: Optional[str],
    profile: Optional[str],
    route: Optional[str],
    session_id: Optional[str],
    usage,
) -> None:
    if not task_id:
        logger.info(
            "Skipping Perplexity auxiliary cost row: task_id unavailable"
        )
        return
    from hermes_cli.cost.recorders import record_perplexity_call

    resolved_lane = str(lane or "platform").strip().lower() or "platform"
    try:
        record_perplexity_call(
            task_id=task_id,
            lane=resolved_lane,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            profile=profile,
            route=route,
            session_id=session_id,
        )
    except Exception as exc:
        from hermes_cli.cost.kill_switch import (
            KillSwitchTripped,
            PerTaskCapExceeded,
        )

        if isinstance(exc, (KillSwitchTripped, PerTaskCapExceeded)):
            raise
        logger.warning(
            "Perplexity auxiliary cost recording failed (non-fatal): %s: %s",
            type(exc).__name__,
            exc,
        )


def record_aux_usage(
    response: Any,
    task: Optional[str],
    *,
    provider: Optional[str] = None,
    base_url: Optional[str] = None,
) -> None:
    """Record an auxiliary response's token usage against the ambient session.

    Called from the auxiliary client's response-validation chokepoint. Strictly
    best-effort: any failure is swallowed (accounting must never break an aux
    call). No-ops when:

    * no accounting context is published (call is outside any agent turn),
    * the task is main-loop-accounted (MoA slots — see ``_EXCLUDED_TASKS``),
    * the response carries no usage object.

    The model is read from ``response.model`` (accurate even after the aux
    client's provider-fallback chains); *provider*/*base_url* reflect the
    originally-resolved route and are best-effort.
    """
    try:
        _record_aux_cost_gate_call(
            response,
            task,
            provider=provider,
            base_url=base_url,
        )

        if not task or task in _EXCLUDED_TASKS:
            return
        ctx = _accounting.get()
        if ctx is None:
            logger.info(
                "Skipping auxiliary verdict for %s: task_id unavailable",
                task,
            )
            return
        session_db, session_id = ctx[:2]
        task_id = ctx[2] if len(ctx) > 2 else None
        lane = ctx[3] if len(ctx) > 3 else None
        profile = ctx[4] if len(ctx) > 4 else None
        route = ctx[5] if len(ctx) > 5 else None
        raw_usage = getattr(response, "usage", None)
        if raw_usage is None:
            return

        from agent.usage_pricing import estimate_usage_cost, normalize_usage

        usage = normalize_usage(raw_usage, provider=provider)
        if not (
            usage.input_tokens or usage.output_tokens
            or usage.cache_read_tokens or usage.cache_write_tokens
            or usage.reasoning_tokens
        ):
            return

        model = str(getattr(response, "model", "") or "") or "unknown"
        estimated_cost = None
        try:
            cost = estimate_usage_cost(
                model, usage, provider=provider, base_url=base_url
            )
            if cost.amount_usd is not None:
                estimated_cost = float(cost.amount_usd)
        except Exception:
            logger.debug("Aux usage cost estimation failed", exc_info=True)

        session_db.record_auxiliary_usage(
            session_id,
            task,
            model=model,
            billing_provider=provider,
            billing_base_url=base_url,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
            reasoning_tokens=usage.reasoning_tokens,
            estimated_cost_usd=estimated_cost,
        )

        if _is_perplexity_route(provider, base_url):
            _record_perplexity_aux_cost(
                task_id=task_id,
                lane=lane,
                profile=profile,
                route=route,
                session_id=session_id,
                usage=usage,
            )

        if not task_id:
            logger.info(
                "Skipping auxiliary verdict for %s: task_id unavailable",
                task,
            )
            return

        from hermes_cli.verdict import (
            DispatchEnvelope,
            LeafVerdict,
            attempts_at_current_rung,
            last_cost_aud_for_task,
            record_dispatch,
            record_verdict,
        )
        from hermes_cli.verdict.types import canonical_strategy_hash

        rung_id = "r0_baseline"
        attempt_number = attempts_at_current_rung(task_id, rung_id) + 1
        strategy_payload = {
            "model": model,
            "mode": "single",
            "prompt_hash": canonical_strategy_hash(
                {"auxiliary_task": str(task)}
            ),
        }
        envelope = DispatchEnvelope(
            task_id=task_id,
            attempt_number=attempt_number,
            rung_id=rung_id,
            model_slug=model,
            mode="single",
            strategy_payload=strategy_payload,
            issued_by="auxiliary_client",
        )
        dispatch_id = record_dispatch(
            envelope,
            profile=profile,
            route=route,
            session_id=session_id,
        )
        record_verdict(
            LeafVerdict(
                task_id=task_id,
                attempt_number=attempt_number,
                rung_id=rung_id,
                dispatch_envelope_id=dispatch_id,
                model_used=model,
                outcome="success",
                confidence=1.0,
                strategy_hash=envelope.strategy_hash,
                cost_aud=last_cost_aud_for_task(task_id),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
                raw_meta={"auxiliary_task": task},
            ),
            profile=profile,
            route=route,
            session_id=session_id,
        )
    except Exception as exc:
        from hermes_cli.cost.kill_switch import (
            KillSwitchTripped,
            PerTaskCapExceeded,
        )

        if isinstance(exc, (KillSwitchTripped, PerTaskCapExceeded)):
            raise
        logger.debug("Aux usage recording failed (non-fatal)", exc_info=True)
