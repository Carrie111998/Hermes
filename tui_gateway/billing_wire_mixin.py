"""Billing wire serializers (shard-plan s4 cluster c1) (moved verbatim from tui_gateway/server.py).

Function bodies are byte-identical to their pre-split server.py form; they
are rebound onto server.py's globals at install time — see method_ctx.py.
"""

import types

from typing import Optional

def _usage_payload(state) -> dict:
    """Best-effort shared usage model for the /topup + /subscription overlay bars.

    Only fetched when logged in; fail-open to {available:false} so the overview
    still renders if the account-info path is down.
    """
    if not getattr(state, "logged_in", False):
        return {"available": False}
    try:
        from agent.billing_usage import build_usage_model

        return _serialize_usage_model(build_usage_model())
    except Exception:
        return {"available": False}


def _serialize_usage_bar(bar) -> Optional[dict]:
    """Serialize a UsageBar (dollar magnitudes → display strings + fractions)."""
    if bar is None:
        return None
    from agent.billing_usage import _fmt_usd

    return {
        "kind": bar.kind,
        "remaining_display": _fmt_usd(bar.remaining_usd),
        "total_display": _fmt_usd(bar.total_usd),
        "spent_display": _fmt_usd(bar.spent_usd),
        "pct_used": bar.pct_used,
        "fill_fraction": bar.fill_fraction,
    }


def _serialize_usage_model(model) -> dict:
    """Serialize a UsageModel for the wire — the shared two-bar dollar view.

    Dollars-only (no 'credits'); fail-open shape mirrors the other billing RPCs
    ({ok, available:false} when logged out / unreachable).
    """
    from agent.billing_usage import _fmt_usd, format_renews

    if model is None or not getattr(model, "available", False):
        return {"ok": True, "available": False}

    return {
        "ok": True,
        "available": True,
        "status": model.status,
        "plan_name": model.plan_name,
        "renews_at": model.renews_at,
        "renews_display": getattr(model, "renews_display", None) or format_renews(model.renews_at),
        "subscription_remaining_display": (
            None if model.subscription_remaining_usd is None else _fmt_usd(model.subscription_remaining_usd)
        ),
        "topup_remaining_display": (
            None if model.topup_remaining_usd is None else _fmt_usd(model.topup_remaining_usd)
        ),
        "total_spendable_display": (
            None if model.total_spendable_usd is None else _fmt_usd(model.total_spendable_usd)
        ),
        "has_topup": model.has_topup,
        "plan_bar": _serialize_usage_bar(model.plan_bar),
        "topup_bar": _serialize_usage_bar(model.topup_bar),
    }


def _serialize_subscription_state(state) -> dict:
    """Serialize a SubscriptionState for the wire (Decimals → strings)."""
    from agent.billing_usage import format_renews
    from agent.billing_view import format_money

    def _s(value):
        return None if value is None else str(value)

    current = None
    if state.current is not None:
        c = state.current
        current = {
            "tier_id": c.tier_id,
            "tier_name": c.tier_name,
            "monthly_credits": _s(c.monthly_credits),
            "credits_remaining": _s(c.credits_remaining),
            "cycle_ends_at": c.cycle_ends_at,
            "pending_downgrade_tier_name": c.pending_downgrade_tier_name,
            "pending_downgrade_at": c.pending_downgrade_at,
            "pending_downgrade_display": format_renews(c.pending_downgrade_at),
            "cancel_at_period_end": c.cancel_at_period_end,
            "cancellation_effective_at": c.cancellation_effective_at,
            "cancellation_effective_display": format_renews(c.cancellation_effective_at),
        }
    # Selectable catalog for the in-terminal tier picker; price is pre-formatted
    # ($X / $X.YY) so the TUI renders it directly.
    tiers = [
        {
            "tier_id": t.tier_id,
            "name": t.name,
            "tier_order": t.tier_order,
            "dollars_per_month_display": format_money(t.dollars_per_month),
            "monthly_credits": _s(t.monthly_credits),
            "is_current": t.is_current,
            "is_enabled": t.is_enabled,
        }
        for t in state.tiers
    ]
    return {
        "ok": True,
        "logged_in": state.logged_in,
        "is_admin": state.is_admin,
        "can_change_plan": state.can_change_plan,
        "org_name": state.org_name,
        "org_id": state.org_id,
        "role": state.role,
        "context": state.context,
        "current": current,
        "tiers": tiers,
        "portal_url": state.portal_url,
        "error": state.error,
        # Shared dollar usage model (two-bar view) embedded so /subscription
        # renders the same bars as /usage from its single fetch. Built from the
        # separate account-info path (the only source with top-up dollars);
        # fail-open → {available:false}. Computed lazily so a logged-out state
        # adds no cost.
        "usage": _usage_payload(state),
    }


def _serialize_subscription_preview(p) -> dict:
    """Serialize a SubscriptionChangePreview for the wire (Decimal → string)."""
    return {
        "ok": True,
        "effect": p.effect,
        "reason": p.reason,
        "current_tier_id": p.current_tier_id,
        "current_tier_name": p.current_tier_name,
        "target_tier_id": p.target_tier_id,
        "target_tier_name": p.target_tier_name,
        "monthly_credits_delta": (
            None if p.monthly_credits_delta is None else str(p.monthly_credits_delta)
        ),
        "amount_due_now_cents": p.amount_due_now_cents,
        "effective_at": p.effective_at,
    }

def register(server) -> None:
    """Bind this module's serializers onto ``server``'s globals (see method_ctx.py)."""
    g = vars(server)
    for _fn in (
        _usage_payload,
        _serialize_usage_bar,
        _serialize_usage_model,
        _serialize_subscription_state,
        _serialize_subscription_preview,
    ):
        rebound = types.FunctionType(
            _fn.__code__, g, _fn.__name__, _fn.__defaults__, _fn.__closure__
        )
        rebound.__kwdefaults__ = _fn.__kwdefaults__
        rebound.__doc__ = _fn.__doc__
        rebound.__dict__.update(_fn.__dict__)
        g[_fn.__name__] = rebound
