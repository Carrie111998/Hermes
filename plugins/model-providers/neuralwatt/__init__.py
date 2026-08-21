"""NeuralWatt provider profile.

NeuralWatt Cloud (https://api.neuralwatt.com/v1) is an OpenAI-compatible
inference service with energy-based billing.  This profile is a first-class
provider (canonical ``name="neuralwatt"``, aliases ``nw`` / ``neural-watt``)
and carries the per-model reasoning, context, max-output, capability and
pricing contracts that NeuralWatt publishes on ``GET /v1/models``.

Live-verified catalog: 2026-08-21 (22 model ids incl. variants).

Key quirks handled here:

  - ``reasoning_effort`` is a TOP-LEVEL api_kwarg (the OpenAI-compatible wire
    format GLM/vLLM/NeuralWatt expect) — never ``extra_body.reasoning``.
  - Per-model effort vocabularies differ sharply: flash defaults to thinking
    OFF, pro defaults to ``low``, glm-5.2 to ``max``, qwen-3.8-27b natively
    supports ``xhigh``, qwen3.6-35b only has ``high``/``none``, and the
    kimi-k2.7 family has NO reasoning-effort field at all
    (``capabilities.reasoning_effort: false``).  When the catalog entry is in
    the static table we honor that contract exactly; unknown ids fall back to
    a gentle live-lookup with the widest ladder as a final fallback.
  - ``glm-5.2`` family does NOT support JSON mode (``json_mode: false``).
  - Variant suffixes: ``-fast`` (server reasoning default OFF), ``-flex``
    (Flex Tier), ``-short`` (200k ctx, capped output), stackable
    (``glm-5.2-short-fast-flex``).  Canary ids and the HuggingFace slug are
    usable when requested explicitly but excluded from picker defaults.
  - ``deepseek-v4-pro`` and ``qwen-3.8-27b`` are PREVIEW models — marked in
    the contract and surfaced via :meth:`is_preview`, so surfaces that want
    will badge or gate them can.
    - Session routing fingerprint: NeuralWatt groups sessions/families by
    ``X-NW-Conversation-ID`` header, else the OpenAI ``user`` field, else a
    content-derived anchor.  We emit the Hermes session id as the OpenAI
    ``user`` field so every turn in a conversation lands in one group and
    the usage analytics surfaces (sessions / families) can roll sub-agent
    runs up per root session.
    - Energy cost: NeuralWatt bills on energy (``accounting_method: energy``);
    every response carries an ``energy`` object (joules/kWh/carbon) and the
    usage endpoints expose per-session/per-request cost + energy.  Fetched
    via :meth:`NeuralWattProfile.fetch_quota` / :meth:`fetch_usage` /
    :meth:`fetch_sessions` / :meth:`fetch_session_detail` /
    :meth:`fetch_session_families` / :meth:`fetch_requests`.

NeuralWatt's structured error contract (the retryable ``internal_routing_error``
400, and 429/503 status classes that the generic classifier already maps
correctly by status) is handled in :func:`agent.error_classifier._classify_400`
via the same provider-specific branch pattern used for ``xai-oauth``.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile

logger = logging.getLogger(__name__)

#: How long a live /v1/models contract refresh stays valid (seconds).
_CONTRACT_TTL_SECONDS = 6 * 3600

#: Model ids that exist in the catalog but must never become picker defaults
#: or fallbacks (internal canary pins and vendor slugs).
_EXCLUDE_FROM_DEFAULTS = {
    "deepseek-v4-flash-0731-canary",
    "deepseek-ai/deepseek-v4-flash",
}

#: Preview / opt-in models (portal shows "Request access").
_PREVIEW_MODELS = {"deepseek-v4-pro", "qwen-3.8-27b"}

# NeuralWatt model contract — 2026-08-21, verified against the live
# authenticated /v1/models catalog.  ``supported_efforts`` = the distinct
# levels the model actually serves (strongest first); ``effort_aliases`` =
# accepted-value → served level (only non-trivial mappings listed);
# ``default_effort`` / ``default_enabled`` = server behavior when we omit
# reasoning_effort.  ``max_context_length`` / ``max_output_tokens`` mirror
# metadata.limits; ``json_mode`` / ``vision`` mirror capabilities;
# ``pricing_usd_per_m`` = (input, cached_input, output).
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
        "vision": False,
        "json_mode": True,
        "pricing_usd_per_m": (0.14, 0.028, 0.28),
    },
    "deepseek-v4-flash-flex": {
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
        "vision": False,
        "json_mode": True,
        "pricing_usd_per_m": (0.14, 0.028, 0.28),
    },
    "deepseek-v4-pro": {
        "supported_efforts": ("max", "high", "low", "none"),
        "effort_aliases": {"xhigh": "high", "medium": "low", "minimal": "low"},
        "default_effort": "low",
        "default_enabled": True,
        "max_context_length": 1_048_560,
        "max_output_tokens": 393_216,
        "vision": False,
        "json_mode": True,
        "pricing_usd_per_m": (1.00, 0.10, 3.00),
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
        "vision": False,
        "json_mode": False,
        "pricing_usd_per_m": (1.45, 0.145, 4.50),
    },
    "glm-5.2-fast": {
        "supported_efforts": ("max", "high", "none"),
        "effort_aliases": {
            "xhigh": "max",
            "medium": "high",
            "low": "high",
            "minimal": "none",
        },
        "default_effort": "none",
        "default_enabled": False,
        "max_context_length": 1_048_560,
        "max_output_tokens": None,
        "vision": False,
        "json_mode": False,
        "pricing_usd_per_m": (1.45, 0.145, 4.50),
    },
    "glm-5.2-flex": {
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
        "max_output_tokens": None,
        "vision": False,
        "json_mode": False,
        "pricing_usd_per_m": (1.45, 0.145, 4.50),
    },
    "glm-5.2-short": {
        "supported_efforts": ("max", "high", "none"),
        "effort_aliases": {
            "xhigh": "max",
            "medium": "high",
            "low": "high",
            "minimal": "none",
        },
        "default_effort": "high",
        "default_enabled": True,
        "max_context_length": 199_984,
        "max_output_tokens": 32_000,
        "vision": False,
        "json_mode": False,
        "pricing_usd_per_m": (1.45, 0.145, 4.50),
    },
    "glm-5.2-short-fast": {
        "supported_efforts": ("max", "high", "none"),
        "effort_aliases": {
            "xhigh": "max",
            "medium": "high",
            "low": "high",
            "minimal": "none",
        },
        "default_effort": "none",
        "default_enabled": False,
        "max_context_length": 199_984,
        "max_output_tokens": 32_000,
        "vision": False,
        "json_mode": False,
        "pricing_usd_per_m": (1.45, 0.145, 4.50),
    },
    "glm-5.2-short-flex": {
        "supported_efforts": ("max", "high", "none"),
        "effort_aliases": {
            "xhigh": "max",
            "medium": "high",
            "low": "high",
            "minimal": "none",
        },
        "default_effort": "high",
        "default_enabled": True,
        "max_context_length": 199_984,
        "max_output_tokens": 32_000,
        "vision": False,
        "json_mode": False,
        "pricing_usd_per_m": (1.45, 0.145, 4.50),
    },
    "glm-5.2-short-fast-flex": {
        "supported_efforts": ("max", "high", "none"),
        "effort_aliases": {
            "xhigh": "max",
            "medium": "high",
            "low": "high",
            "minimal": "none",
        },
        "default_effort": "none",
        "default_enabled": False,
        "max_context_length": 199_984,
        "max_output_tokens": 32_000,
        "vision": False,
        "json_mode": False,
        "pricing_usd_per_m": (1.45, 0.145, 4.50),
    },
    "gemma-4-31b": {
        "supported_efforts": ("max", "none"),
        "effort_aliases": {
            "xhigh": "max",
            "high": "max",
            "medium": "max",
            "low": "max",
            "minimal": "max",
        },
        "default_effort": "none",
        "default_enabled": False,
        "max_context_length": 262_128,
        "max_output_tokens": 16_384,
        "vision": True,
        "json_mode": True,
        "pricing_usd_per_m": (0.144, 0.0144, 0.42),
    },
    "kimi-k2.7-code": {
        "supported_efforts": (),
        "effort_aliases": {},
        "reasoning_effort": False,
        "default_effort": None,
        "default_enabled": None,
        "max_context_length": 262_128,
        "max_output_tokens": None,
        "vision": True,
        "json_mode": True,
        "pricing_usd_per_m": (0.95, 0.095, 4.00),
    },
    "kimi-k2.7-code-fast": {
        "supported_efforts": (),
        "effort_aliases": {},
        "reasoning_effort": False,
        "default_effort": None,
        "default_enabled": None,
        "max_context_length": 262_128,
        "max_output_tokens": None,
        "vision": True,
        "json_mode": True,
        "pricing_usd_per_m": (0.95, 0.095, 4.00),
    },
    "kimi-k2.7-code-flex": {
        "supported_efforts": (),
        "effort_aliases": {},
        "reasoning_effort": False,
        "default_effort": None,
        "default_enabled": None,
        "max_context_length": 262_128,
        "max_output_tokens": None,
        "vision": True,
        "json_mode": True,
        "pricing_usd_per_m": (0.95, 0.095, 4.00),
    },
    "kimi-k3": {
        "supported_efforts": ("max", "high", "low", "none"),
        "effort_aliases": {"xhigh": "max", "medium": "high", "minimal": "low"},
        "default_effort": "max",
        "default_enabled": True,
        "max_context_length": 1_048_560,
        "max_output_tokens": None,
        "vision": True,
        "json_mode": True,
        "pricing_usd_per_m": (3.00, 0.30, 15.00),
    },
    "kimi-k3-fast": {
        "supported_efforts": ("none",),
        "effort_aliases": {},
        "default_effort": "none",
        "default_enabled": False,
        "max_context_length": 1_048_560,
        "max_output_tokens": None,
        "vision": True,
        "json_mode": True,
        "pricing_usd_per_m": (3.00, 0.30, 15.00),
    },
    "kimi-k3-flex": {
        "supported_efforts": ("max", "high", "low", "none"),
        "effort_aliases": {"xhigh": "max", "medium": "high", "minimal": "low"},
        "default_effort": "max",
        "default_enabled": True,
        "max_context_length": 1_048_560,
        "max_output_tokens": None,
        "vision": True,
        "json_mode": True,
        "pricing_usd_per_m": (3.00, 0.30, 15.00),
    },
    "qwen-3.8-27b": {
        # The only NeuralWatt model serving ``xhigh`` natively.
        "supported_efforts": ("xhigh", "medium", "low", "none"),
        "effort_aliases": {"max": "xhigh", "high": "xhigh", "minimal": "low"},
        "default_effort": "xhigh",
        "default_enabled": True,
        "max_context_length": 262_128,
        "max_output_tokens": 65_536,
        "vision": True,
        "json_mode": True,
        "pricing_usd_per_m": (0.45, 0.25, 3.20),
    },
    "qwen3.6-35b": {
        "supported_efforts": ("high", "none"),
        "effort_aliases": {},
        "default_effort": "high",
        "default_enabled": True,
        "max_context_length": 131_056,
        "max_output_tokens": None,
        "vision": True,
        "json_mode": True,
        "pricing_usd_per_m": (0.29, 0.029, 1.15),
    },
    "qwen3.6-35b-fast": {
        "supported_efforts": ("none",),
        "effort_aliases": {},
        "default_effort": "none",
        "default_enabled": False,
        "max_context_length": 131_056,
        "max_output_tokens": None,
        "vision": True,
        "json_mode": True,
        "pricing_usd_per_m": (0.29, 0.029, 1.15),
    },
}


def _model_base_id(model: str | None) -> str:
    """Return the canonical NeuralWatt model id for contract lookup.

    Strips vendor prefixes (``deepseek-ai/DeepSeek-V4-Flash`` →
    ``deepseek-v4-flash``) and case, and folds the internal canary pin onto
    its stable parent so it stays usable without carrying its own row.
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
    if lower.endswith("-0731-canary"):
        candidate = lower.removesuffix("-0731-canary")
        if candidate in _NEURALWATT_STATIC_CONTRACT:
            return candidate
    return lower


# Live contract cache — refreshed from GET /v1/models for ids NOT in the
# static table (the catalog accretes models and changes defaults between
# releases; static rows stay deterministic for the ones we ship).
_live_contract_cache: dict[str, dict[str, Any]] = {}
_live_contract_fetched_at: float = 0.0
_live_contract_lock = threading.Lock()


def _refresh_live_contract(base_url: str) -> dict[str, dict[str, Any]]:
    with _live_contract_lock:
        global _live_contract_fetched_at
        if (
            _live_contract_cache
            and (time.time() - _live_contract_fetched_at) < _CONTRACT_TTL_SECONDS
        ):
            return _live_contract_cache
        try:
            import json
            import urllib.request

            req = urllib.request.Request(base_url.rstrip("/") + "/models")
            req.add_header("Accept", "application/json")
            req.add_header("User-Agent", "hermes-cli/neuralwatt")
            import hermes_cli.urllib_security as sec  # defer import

            with sec.open_credentialed_url(req, timeout=8.0) as resp:
                data = json.loads(resp.read().decode())
            items = data.get("data", []) if isinstance(data, dict) else data
            for m in items:
                if not isinstance(m, dict) or "id" not in m:
                    continue
                mid = str(m["id"]).lower()
                md = m.get("metadata") or {}
                reasoning = md.get("reasoning") or {}
                limits = md.get("limits") or {}
                caps = md.get("capabilities") or {}
                pricing = md.get("pricing") or {}
                _live_contract_cache[mid] = {
                    "supported_efforts": tuple(reasoning.get("supported_efforts") or [])
                    or (),
                    "effort_aliases": {
                        str(k): str(v)
                        for k, v in (reasoning.get("effort_aliases") or {}).items()
                    },
                    "default_effort": reasoning.get("default_effort"),
                    "default_enabled": reasoning.get("default_enabled"),
                    "max_context_length": int(limits.get("max_context_length") or 0)
                    or None,
                    "max_output_tokens": int(limits.get("max_output_tokens") or 0)
                    or None,
                    "vision": bool(caps.get("vision")),
                    "json_mode": bool(caps.get("json_mode")),
                    "reasoning_effort": bool(caps.get("reasoning_effort")),
                    "pricing_usd_per_m": (
                        float(pricing.get("input_per_million") or 0) or None,
                        float(pricing.get("cached_input_per_million") or 0) or None,
                        float(pricing.get("output_per_million") or 0) or None,
                    ),
                }
            _live_contract_fetched_at = time.time()
        except Exception as exc:
            logger.debug("neuralwatt live contract refresh failed: %s", exc)
        return dict(_live_contract_cache)


class NeuralWattProfile(ProviderProfile):
    """NeuralWatt — top-level reasoning_effort + per-model capability contract.

    ``default_reasoning_effort`` is the provider's preference when reasoning
    is enabled but the user did not pick an effort.  ``max`` is served by the
    widest set of strong models (flash/glm/kimi-k3/gemma), so it is the
    unambiguous default — notably it overrides pro's *server* default of
    ``low`` so the model always thinks hard unless asked otherwise.  Models
    that do not serve ``max`` at all fall back to their own server default
    (qwen3.6-35b → high, qwen-3.8-27b → xhigh).
    """

    #: Effort level sent when reasoning is enabled but no effort is given.
    #: Overrides the per-model server default (pro: low, flash: none).
    default_reasoning_effort: str = "max"

    def _contract_for(self, model: str | None) -> dict[str, Any] | None:
        base = _model_base_id(model)
        contract = _NEURALWATT_STATIC_CONTRACT.get(base)
        if contract is not None:
            return contract
        if not base:
            return None
        live = _refresh_live_contract(self.base_url)
        return live.get(base)

    @staticmethod
    def is_preview(model: str | None) -> bool:
        """True for preview models (deepseek-v4-pro, qwen-3.8-27b)."""
        return _model_base_id(model) in _PREVIEW_MODELS

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
        ``none`` sentinel; with reasoning enabled but no effort chosen we
        send our provider default (``max`` where served, else the model's
        server default).  Models with no effort capability at all (the
        kimi-k2.7 family) never get the field.  The field is omitted only
        when there is no reasoning config at all.
        """
        extra_body: dict[str, Any] = {}
        top_level: dict[str, Any] = {}

        # Session routing fingerprint (Phase C): sessions/families analytics
        # group by the OpenAI ``user`` field when present.  Emit the Hermes
        # session id (opaque hex — no PII) as a short stable suffix.
        session_id = str(context.get("session_id") or "").strip()
        if session_id:
            top_level["user"] = f"hermes-{session_id[-32:]}"

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
            # No reasoning-effort capability (kimi-k2.7 family) — the backend
            # has a fixed thinking level and would reject the field.
            if contract.get("reasoning_effort") is False or (
                not supported and "reasoning_effort" not in contract
            ):
                return extra_body, top_level
            aliases = contract["effort_aliases"] or {}
            if effort in aliases and aliases[effort] in supported:
                effort = aliases[effort]
            if not supported:
                # Unknown vocabulary for a live-discovered model — fall
                # through to the generic ladder below.
                pass
            elif enabled is False or effort == "none":
                if "none" in supported:
                    top_level["reasoning_effort"] = "none"
                    return extra_body, top_level
            elif effort and effort in supported:
                top_level["reasoning_effort"] = effort
            elif effort and effort != "none":
                clamped = clamp_effort(effort, supported, aliases) or effort
                top_level["reasoning_effort"] = clamped
            elif not effort and enabled is not False:
                # Reasoning enabled but no effort chosen → provider default
                # (max when served) else model's server default.
                if "max" in supported:
                    top_level["reasoning_effort"] = "max"
                elif contract.get("default_effort") in supported:
                    top_level["reasoning_effort"] = contract["default_effort"]
            if supported:
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

        ``deepseek-v4-pro`` caps at 393,216, ``glm-5.2-short`` at 32,000,
        ``gemma-4-31b`` at 16,384; models publishing no cap use the profile
        floor (65,536) rather than inventing one.
        """
        contract = self._contract_for(model)
        if contract is not None and contract.get("max_output_tokens"):
            return int(contract["max_output_tokens"])
        return self.default_max_tokens

    def default_vision_model(self) -> str | None:
        return None  # Hermes default vision routes elsewhere; neuralwatt vision opt-in

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Live model list from ``GET /v1/models`` (public catalog path)."""
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
        """Fetch account balance / usage / limits from ``GET /v1/quota``."""
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
        """Fetch energy usage from ``GET /v1/usage/energy``."""
        return self._api_get(
            f"/usage/energy?period_days={int(period_days)}",
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

    def fetch_sessions(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any] | None:
        """Recent sessions from ``GET /v1/usage/sessions`` (limit/offset)."""
        return self._api_get(
            f"/usage/sessions?limit={int(limit)}&offset={int(offset)}",
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

    def fetch_session_detail(
        self,
        session_id: str,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any] | None:
        """Per-turn detail + optimization flags from ``GET /v1/usage/sessions/{id}``."""
        return self._api_get(
            f"/usage/sessions/{session_id}",
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

    def fetch_session_families(
        self,
        *,
        limit: int = 20,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any] | None:
        """Routing-fingerprint families from ``GET /v1/usage/sessions/families``."""
        return self._api_get(
            f"/usage/sessions/families?limit={int(limit)}",
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
        )

    def fetch_requests(
        self,
        *,
        limit: int = 20,
        cursor: str | None = None,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 20.0,
    ) -> dict[str, Any] | None:
        """Cursor-paginated per-request rows from ``GET /v1/usage/requests``."""
        cursor_q = f"&cursor={cursor}" if cursor else ""
        return self._api_get(
            f"/usage/requests?limit={int(limit)}{cursor_q}",
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
    # Decision 2026-08-21 (user): fallback chain flash → pro → glm-5.2.
    # pro's preview status only removed it from fallbacks previously; the
    # user moved it back into the chain, so it deliberately appears here.
    fallback_models=(
        "deepseek-v4-flash",
        "deepseek-v4-pro",
        "glm-5.2",
    ),
    base_url="https://api.neuralwatt.com/v1",
    default_aux_model="deepseek-v4-flash",
    default_max_tokens=65_536,
)

register_provider(neuralwatt)
