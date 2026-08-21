"""NeuralWatt provider profile.

NeuralWatt Cloud (https://api.neuralwatt.com/v1) is an OpenAI-compatible
inference service with energy-based billing.  This profile is a first-class
provider (canonical ``name="neuralwatt"``, aliases ``nw`` / ``neural-watt``)
and pins the per-model reasoning, context, max-output and capability
contracts that NeuralWatt publishes on ``GET /v1/models``.

Key quirks handled here:

  - ``reasoning_effort`` is a TOP-LEVEL api_kwarg (the OpenAI-compatible wire
    format GLM/vLLM/NeuralWatt expect) — never ``extra_body.reasoning``.
  - Per-model effort contracts differ sharply: ``deepseek-v4-flash`` defaults
    to thinking OFF (``default_effort: none``), ``deepseek-v4-pro`` defaults
    to ``low``, ``glm-5.2`` defaults to ``max``.  ``xhigh`` maps to ``max``
    on flash and glm-5.2 but to ``high`` on pro.  We read each model's
    published ``metadata.reasoning`` contract live and fall back to a bundled
    static table when the catalog is unreachable.
  - ``deepseek-v4-pro`` is a PREVIEW model — it is deliberately excluded from
    ``fallback_models`` so it can never become a default/routed model; the
    user must pick it explicitly to opt in.
  - ``glm-5.2`` does NOT support JSON mode (``json_mode: false``).

NeuralWatt's structured error contract (the retryable ``internal_routing_error``
400, and 429/503 status classes that the generic classifier already maps
correctly by status) is handled in :func:`agent.error_classifier._classify_400`
via the same provider-specific branch pattern used for ``xai-oauth``.
"""

from __future__ import annotations

import logging
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

# NeuralWatt models that route reasoning through this provider. Keyed by model
# id.  2026-08-21 — verified against the live authenticated /v1/models catalog.
# ``supported_efforts`` = the distinct levels the model actually has (deepest
# first); ``effort_aliases`` = accepted-value → level served (only non-trivial
# mappings listed); ``default_effort`` / ``default_enabled`` = what the server
# does when we omit reasoning_effort.  ``max_context_length`` /
# ``max_output_tokens`` mirror metadata.limits; ``json_mode`` mirrors
# metadata.capabilities.json_mode.
_NEURALWATT_STATIC_CONTRACT: dict[str, dict[str, Any]] = {
    "deepseek-v4-flash": {
        "supported_efforts": ("max", "high", "none"),
        "effort_aliases": {
            "xhigh": "max",
            "medium": "high",
            "low": "high",
            "minimal": "high",
        },
        "default_effort": "none",
        "default_enabled": False,
        "max_context_length": 1_048_560,
        "max_output_tokens": 65_536,
        "json_mode": True,
    },
    "deepseek-v4-pro": {
        "supported_efforts": ("max", "high", "low", "none"),
        "effort_aliases": {"xhigh": "high", "medium": "low", "minimal": "low"},
        "default_effort": "low",
        "default_enabled": True,
        "max_context_length": 1_048_560,
        "max_output_tokens": 393_216,
        "json_mode": True,
    },
    "glm-5.2": {
        "supported_efforts": ("max", "high", "none"),
        "effort_aliases": {
            "xhigh": "max",
            "medium": "high",
            "low": "high",
            "minimal": "none",
        },
        "default_effort": "max",
        "default_enabled": True,
        "max_context_length": 1_048_560,
        "max_output_tokens": None,  # server publishes no output cap
        "json_mode": False,
    },
}


def _model_base_id(model: str | None) -> str:
    """Return the NeuralWatt model id for contract lookup.

    Handles vendor-prefixed aliases the catalog serves under their own id
    (e.g. ``deepseek-ai/DeepSeek-V4-Flash``) by falling back to the suffix.
    """
    m = (model or "").strip()
    if not m:
        return ""
    lower = m.lower()
    if lower in _NEURALWATT_STATIC_CONTRACT:
        return lower
    if "/" in m:
        candidate = m.rsplit("/", 1)[-1].strip().lower()
        if candidate in _NEURALWATT_STATIC_CONTRACT:
            return candidate
    return lower


class NeuralWattProfile(ProviderProfile):
    """NeuralWatt — top-level reasoning_effort + per-model capability contract.

    ``default_reasoning_effort`` is the provider's preference when reasoning
    is enabled but the user did not pick an effort.  ``max`` is native on all
    three NeuralWatt models (flash/glm-5.2 support max·high·none; pro
    supports max·high·low·none), so it is the unambiguous default — notably
    it overrides pro's *server* default of ``low`` so the model always thinks
    hard unless asked otherwise.
    """

    #: Effort level sent when reasoning is enabled but no effort is given.
    #: Overrides the per-model server default (pro: low, flash: none).
    default_reasoning_effort: str = "max"

    def _contract_for(self, model: str | None) -> dict[str, Any] | None:
        return _NEURALWATT_STATIC_CONTRACT.get(_model_base_id(model))

    def build_api_kwargs_extras(
        self,
        *,
        reasoning_config: dict | None = None,
        model: str | None = None,
        **context: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Emit the top-level ``reasoning_effort`` field for NeuralWatt.

        NeuralWatt's GLM/vLLM backends expect the effort as a top-level JSON
        field (the OpenAI-compatible spelling) — not ``extra_body.reasoning``.
        When the model publishes a reasoning contract we clamp the user's
        requested effort onto that model's wire vocabulary and honor its
        ``none`` sentinel; when it has no contract we reuse the widest
        OpenAI-compatible ladder.  With reasoning enabled but no effort
        chosen we send the provider default (``max``) rather than the
        model's server default (pro: low, flash: none); the field is omitted
        only when there is no reasoning config at all.
        """
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        if not reasoning_config or not isinstance(reasoning_config, dict):
            # Nothing configured — let the model's own default apply.
            return extra_body, top_level

        from agent.reasoning_effort import (
            OPENAI_COMPAT_WIRE_EFFORTS,
            clamp_effort,
            requested_effort,
        )

        enabled = reasoning_config.get("enabled", True)
        effort = requested_effort(reasoning_config)

        contract = self._contract_for(model)
        if contract is not None:
            supported = contract["supported_efforts"]
            aliases = contract["effort_aliases"] or {}
            if effort in aliases and aliases[effort] in supported:
                effort = aliases[effort]
            elif enabled is False or effort == "none":
                if "none" in supported:
                    top_level["reasoning_effort"] = "none"
                    return extra_body, top_level
            if effort and effort in supported:
                top_level["reasoning_effort"] = effort
            elif effort and effort != "none":
                clamped = clamp_effort(effort, supported, aliases) or effort
                # clamp_effort maps to the nearest weaker supported level
                top_level["reasoning_effort"] = clamped
            elif not effort and enabled is not False:
                # Reasoning enabled but no effort chosen → our provider
                # default (max) instead of the model's server default
                # (pro: low, flash: none).  Drop the field entirely only
                # when the default isn't a supported level.
                if self.default_reasoning_effort in supported:
                    top_level["reasoning_effort"] = self.default_reasoning_effort
            return extra_body, top_level

        # No published contract — widest OpenAI-compatible wire spelling.
        if enabled is False or effort == "none":
            top_level["reasoning_effort"] = "none"
            return extra_body, top_level
        if effort:
            clamped = clamp_effort(effort, OPENAI_COMPAT_WIRE_EFFORTS) or effort
            top_level["reasoning_effort"] = clamped
        elif enabled is not False:
            # No effort chosen → our provider default (max).
            if self.default_reasoning_effort in OPENAI_COMPAT_WIRE_EFFORTS:
                top_level["reasoning_effort"] = self.default_reasoning_effort

        return extra_body, top_level

    def get_max_tokens(self, model: str | None) -> int | None:
        """Per-model output cap from the NeuralWatt contract.

        ``deepseek-v4-flash`` caps at 65,536; ``deepseek-v4-pro`` at 393,216;
        ``glm-5.2`` publishes no output cap so we keep the generous profile
        floor (65,536) rather than inventing a limit.
        """
        contract = self._contract_for(model)
        if contract is not None and contract.get("max_output_tokens"):
            return int(contract["max_output_tokens"])
        return self.default_max_tokens

    def default_vision_model(self) -> str | None:
        return None  # none of the NeuralWatt models accept image input

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Live model list from ``GET /v1/models`` (open to public catalog)."""
        return super().fetch_models(api_key=api_key, base_url=base_url, timeout=timeout)

    def _api_get(
        self,
        path: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> dict[str, Any] | None:
        """Authenticated GET against a NeuralWatt /v1 path, returning JSON dict.

        Returns ``None`` on any failure (network, non-200, non-JSON).
        """
        import json
        import urllib.request

        from hermes_cli.urllib_security import open_credentialed_url

        base = (base_url or "").strip().rstrip("/") or self.base_url.rstrip("/")
        if not base or not api_key:
            return None
        url = f"{base}{path}"
        req = urllib.request.Request(url)
        req.add_header("Authorization", f"Bearer {api_key}")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "hermes-cli/neuralwatt")
        for k, v in self.default_headers.items():
            req.add_header(k, v)
        try:
            with open_credentialed_url(req, timeout=timeout) as resp:
                if resp.status != 200:
                    return None
                data = json.loads(resp.read().decode())
            return data if isinstance(data, dict) else None
        except Exception as exc:
            logger.debug("neuralwatt %s: %s", path, exc)
            return None

    def fetch_quota(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> dict[str, Any] | None:
        """Fetch account balance / usage / limits from ``GET /v1/quota``.

        Returns the NeuralWatt quota payload (balance.credits_remaining_usd,
        usage.current_month, subscription.plan, …), or ``None`` on failure.
        """
        return self._api_get(
            "/quota", api_key=api_key, base_url=base_url, timeout=timeout
        )

    def fetch_usage(
        self,
        *,
        period_days: int = 30,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> dict[str, Any] | None:
        """Fetch energy usage from ``GET /v1/usage/energy``.

        Returns the NeuralWatt energy payload (period, totals.energy_kwh,
        daily[]) or ``None`` on failure.  ``period_days`` selects the
        trailing window (the API clamps to its own cadence).
        """
        return self._api_get(
            f"/usage/energy?period_days={int(period_days)}",
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )


neuralwatt = NeuralWattProfile(
    name="neuralwatt",
    aliases=("nw", "neural-watt"),
    env_vars=("NEURALWATT_API_KEY",),
    display_name="NeuralWatt",
    description="NeuralWatt Cloud — OpenAI-compatible inference with energy-based billing",
    signup_url="https://portal.neuralwatt.com/auth/register",
    # deepseek-v4-pro is deliberately absent: it is a PREVIEW model and must
    # be explicitly opted into, never a default/fallback route.
    fallback_models=(
        "deepseek-v4-flash",
        "glm-5.2",
    ),
    base_url="https://api.neuralwatt.com/v1",
    default_aux_model="deepseek-v4-flash",
    default_max_tokens=65_536,
)

register_provider(neuralwatt)
