"""Hermes-to-Perseus/Plutus runtime usage bridge.

This module deliberately sits above provider transports. Hermes already
normalizes provider usage in ``conversation_loop``; Perseus already owns the
counterfactual render baseline. The bridge combines those two facts into one
real Plutus event without making provider transports know about billing.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


def build_usage_event(
    usage: Any,
    *,
    provider: str | None,
    model: str | None,
    baseline: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Map Hermes canonical usage plus a Perseus baseline to Plutus fields."""
    def integer(name: str) -> int:
        try:
            return max(0, int(getattr(usage, name, 0) or 0))
        except (TypeError, ValueError):
            return 0

    event: dict[str, Any] = {
        "provider": (provider or "unknown").strip().lower(),
        "model": model,
        "input_tokens": integer("input_tokens"),
        "output_tokens": integer("output_tokens"),
        "cache_read_tokens": integer("cache_read_tokens"),
        "reasoning_tokens": integer("reasoning_tokens"),
        "source": "hermes-provider-response",
    }
    if baseline:
        for key in ("baseline_input_tokens", "baseline_output_tokens"):
            value = baseline.get(key)
            if value is not None:
                try:
                    event[key] = max(0, int(value))
                except (TypeError, ValueError):
                    pass
        if baseline.get("source"):
            event["baseline_source"] = str(baseline["source"])
    return event


def load_perseus_config() -> dict[str, Any]:
    """Load the configured Perseus YAML without exposing secrets in logs."""
    root = Path(os.environ.get("PERSEUS_HOME", Path.home() / ".perseus"))
    path = Path(os.environ.get("PERSEUS_CONFIG_PATH", root / "config.yaml"))
    try:
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # metering is intentionally fail-open
        logger.warning("perseus metering config unavailable: %s", exc)
        return {}


def meter_normalized_usage(
    usage: Any,
    *,
    provider: str | None,
    model: str | None,
    cfg: Mapping[str, Any] | None = None,
    meter_fn: Callable[..., Any] | None = None,
    consume_baseline_fn: Callable[[], Mapping[str, Any] | None] | None = None,
) -> Any:
    """Record one real provider event, attaching the latest render baseline.

    The baseline is consumed exactly once. All failures are fail-open because
    an accounting outage must not break an agent response.
    """
    if usage is None:
        return None
    try:
        if cfg is None:
            cfg = load_perseus_config()
        if not isinstance(cfg, Mapping):
            return None
        if meter_fn is None or consume_baseline_fn is None:
            try:
                from perseus.metering import consume_context_baseline, meter_usage
            except (ImportError, ModuleNotFoundError):
                # The generated `perseus.py` artifact is a flat module, while
                # source/development installs expose `perseus.metering`.
                import perseus as perseus_runtime
                consume_context_baseline = getattr(
                    perseus_runtime, "consume_context_baseline"
                )
                meter_usage = getattr(perseus_runtime, "meter_usage")
            if meter_fn is None:
                meter_fn = meter_usage
            if consume_baseline_fn is None:
                consume_baseline_fn = consume_context_baseline
        baseline = consume_baseline_fn()
        event = build_usage_event(
            usage, provider=provider, model=model, baseline=baseline
        )
        kwargs = {
            "model": event["model"],
            "input_tokens": event["input_tokens"],
            "output_tokens": event["output_tokens"],
            "cache_read_tokens": event["cache_read_tokens"],
            "reasoning_tokens": event["reasoning_tokens"],
            "source": event["source"],
        }
        for key in ("baseline_input_tokens", "baseline_output_tokens"):
            if key in event:
                kwargs[key] = event[key]
        return meter_fn(cfg, event["provider"], **kwargs)
    except Exception as exc:
        logger.warning("provider usage metering dropped: %s", exc)
        return None
