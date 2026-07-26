#!/usr/bin/env python3
"""Opt-in live reconciliation of estimator, provider cost, and ledger rows.

This is deliberately not a pytest test. It makes five real paid OpenRouter
calls and must only be run manually with ``HERMES_RECONCILE=1``.
"""

from __future__ import annotations

import json
import os
from decimal import Decimal


MODEL_ROUTES = (
    ("OpenAI 5.6 Sol", "openai/gpt-5.6-sol"),
    ("Anthropic Opus 5", "anthropic/claude-opus-5"),
    ("Kimi K3", "moonshotai/kimi-k3"),
    ("GLM 5.2", "z-ai/glm-5.2"),
    ("Anthropic Sonnet 5", "anthropic/claude-sonnet-5"),
)
MAX_DRIFT = Decimal("0.05")
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


def _decimal(value) -> Decimal:
    return Decimal(str(value))


def _relative_drift(actual: Decimal, expected: Decimal) -> Decimal:
    if expected == 0:
        return Decimal("0") if actual == 0 else Decimal("Infinity")
    return abs(actual - expected) / abs(expected)


def _published_cost(pricing: dict, usage) -> Decimal:
    """Compute USD independently from OpenRouter's published per-token rates."""
    return (
        Decimal(usage.input_tokens) * _decimal(pricing.get("prompt", 0))
        + Decimal(usage.output_tokens)
        * _decimal(pricing.get("completion", 0))
        + Decimal(usage.cache_read_tokens)
        * _decimal(pricing.get("input_cache_read", 0))
        + Decimal(usage.cache_write_tokens)
        * _decimal(pricing.get("input_cache_write", 0))
        + Decimal(usage.request_count) * _decimal(pricing.get("request", 0))
    )


def main() -> int:
    if os.environ.get("HERMES_RECONCILE") != "1":
        raise SystemExit(
            "Refusing paid calls: set HERMES_RECONCILE=1 to run reconciliation"
        )
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY is required for reconciliation")

    # Imports follow the opt-in gate so an accidental invocation cannot even
    # initialize the runtime stores, let alone contact a provider.
    from agent.auxiliary_client import call_llm
    from agent.usage_pricing import estimate_usage_cost, normalize_usage
    from hermes_cli.cost import config as cost_config
    from hermes_cli.cost import ledger
    import httpx

    catalog_response = httpx.get(
        f"{OPENROUTER_BASE_URL}/models",
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=30,
    )
    catalog_response.raise_for_status()
    catalog = {
        str(item["id"]): item.get("pricing", {})
        for item in catalog_response.json().get("data", [])
        if item.get("id")
    }

    results = []
    failures = []
    for label, model in MODEL_ROUTES:
        before = ledger.connect()
        try:
            row = before.execute(
                "SELECT COALESCE(MAX(id), 0) AS max_id FROM cost_ledger"
            ).fetchone()
            prior_id = int(row["max_id"])
        finally:
            before.close()

        response = call_llm(
            task="reconcile_estimator",
            provider="openrouter",
            model=model,
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
            messages=[
                {
                    "role": "user",
                    "content": "Reply with exactly: ok",
                }
            ],
            temperature=0,
            max_tokens=8,
            timeout=120,
        )
        usage = normalize_usage(response.usage, provider="openrouter")
        response_model = str(getattr(response, "model", "") or model)
        estimate = estimate_usage_cost(
            response_model,
            usage,
            provider="openrouter",
            base_url=OPENROUTER_BASE_URL,
            api_key=api_key,
        )
        reported_raw = getattr(response.usage, "cost", None)
        if reported_raw is None:
            raise RuntimeError(f"{label}: OpenRouter response omitted usage.cost")
        if estimate.amount_usd is None:
            raise RuntimeError(f"{label}: Hermes estimator has no price")
        pricing = catalog.get(response_model) or catalog.get(model)
        if not pricing:
            raise RuntimeError(
                f"{label}: model missing from OpenRouter published catalog"
            )
        manual_usd = _published_cost(pricing, usage)
        manual_aud = (
            manual_usd
            * (Decimal("1") + _decimal(cost_config.OPENROUTER_SURCHARGE))
            * _decimal(cost_config.FX_RATE)
        )

        request_id = str(getattr(response, "id", "") or "")
        after = ledger.connect()
        try:
            ledger_row = after.execute(
                """
                SELECT id, request_id, model_slug, usd_amount, aud_amount,
                       fx_rate, surcharge_applied, raw_response_meta
                  FROM cost_ledger
                 WHERE id > ?
                   AND (? = '' OR request_id = ?)
                 ORDER BY id DESC
                 LIMIT 1
                """,
                (prior_id, request_id, request_id),
            ).fetchone()
        finally:
            after.close()
        if ledger_row is None:
            raise RuntimeError(f"{label}: real provider path wrote no ledger row")

        reported = _decimal(reported_raw)
        estimated = _decimal(estimate.amount_usd)
        recorded = _decimal(ledger_row["usd_amount"])
        ledger_aud = _decimal(ledger_row["aud_amount"])
        estimator_drift = _relative_drift(estimated, manual_usd)
        provider_drift = _relative_drift(reported, manual_usd)
        ledger_drift = _relative_drift(ledger_aud, manual_aud)
        result = {
            "label": label,
            "requested_model": model,
            "response_model": response_model,
            "request_id": request_id,
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "provider_reported_usd": str(reported),
            "published_price_manual_usd": str(manual_usd),
            "published_price_manual_aud": str(manual_aud),
            "estimated_usd": str(estimated),
            "ledger_usd": str(recorded),
            "ledger_aud": str(ledger_aud),
            "estimator_drift": str(estimator_drift),
            "provider_drift": str(provider_drift),
            "ledger_drift": str(ledger_drift),
            "pricing_source": estimate.source,
            "pricing_version": estimate.pricing_version,
        }
        results.append(result)
        if (
            estimator_drift >= MAX_DRIFT
            or provider_drift >= MAX_DRIFT
            or ledger_drift >= MAX_DRIFT
        ):
            failures.append(result)

    print(json.dumps(results, indent=2, sort_keys=True))
    if failures:
        raise SystemExit(
            f"reconciliation failed: {len(failures)} model(s) had >=5% drift"
        )
    print("PASS: estimator and ledger drift were below 5% for every model")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
