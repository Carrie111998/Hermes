"""CommandCode provider profile.

CommandCode provides a unified API that fronts 20+ models from DeepSeek, Qwen,
Kimi, GLM, MiniMax, StepFun, Xiaomi Mimo, Google Gemini, and OpenAI GPT — all
accessible through either OpenAI-compatible chat completions or Anthropic
Messages endpoints from a single base URL and API key.

Two provider profiles are registered:

``commandcode``
    ``api_mode=chat_completions`` — standard OpenAI-compatible endpoint.
    Model prefix: ``deepseek/deepseek-v4-pro``, ``Qwen/Qwen3.7-Max``, etc.

``commandcode-anthropic``
    ``api_mode=anthropic_messages`` — Anthropic Messages API-compatible.
    Model names: ``claude-sonnet-4-6``, ``claude-opus-4-7``,
    ``claude-haiku-4-5-20251001``.

Both use the same ``COMMANDCODE_API_KEY`` env var and
``https://api.commandcode.ai/provider/v1`` base URL.  The
``commandcode-anthropic`` profile relies on ``agent/anthropic_adapter.py``
recognizing the ``api.commandcode.ai`` hostname for Bearer auth (the
CommandCode /anthropic endpoint uses ``Authorization: Bearer``, not
Anthropic's native ``x-api-key`` header).
"""

from __future__ import annotations

import json
import logging
import math
import os
import urllib.request
from datetime import datetime, timezone
from typing import Any

from providers import register_provider
from providers.base import ProviderProfile, _profile_user_agent

logger = logging.getLogger(__name__)

# ── Shared constants ──────────────────────────────────────────────────────────
_COMMANDCODE_BASE = "https://api.commandcode.ai/provider/v1"
_COMMANDCODE_MODELS_URL = f"{_COMMANDCODE_BASE}/models"
_COMMANDCODE_BILLING_BASE = "https://api.commandcode.ai/internal/billing"
_COMMANDCODE_CREDITS_URL = f"{_COMMANDCODE_BILLING_BASE}/credits"
_COMMANDCODE_SUBSCRIPTIONS_URL = f"{_COMMANDCODE_BILLING_BASE}/subscriptions"
_COMMANDCODE_SESSION_COOKIES = (
    "__Secure-commandcode_prod_.session_token",
    "commandcode_prod_.session_token",
    "__Host-commandcode_prod_.session_token",
    "__Host-better-auth.session_token",
    "__Secure-better-auth.session_token",
    "better-auth.session_token",
)
_COMMANDCODE_PLANS: dict[str, tuple[str, float]] = {
    "individual-go": ("Go", 10.0),
    "individual-goat": ("GOAT", 70.0),
    "individual-pro": ("Pro", 30.0),
    "individual-max": ("Max", 150.0),
    "individual-ultra": ("Ultra", 300.0),
}
# Both profiles authenticate with the same key; each carries its own base-URL
# override var so each renders its own card on the desktop Keys tab (rows are
# keyed by env var, and the shared API key attributes to the first profile).
_COMMANDCODE_ENV = ("COMMANDCODE_API_KEY", "COMMANDCODE_BASE_URL")
_COMMANDCODE_ANTHROPIC_ENV = ("COMMANDCODE_API_KEY", "COMMANDCODE_ANTHROPIC_BASE_URL")


def _fetch_commandcode_models(
    timeout: float = 10.0,
) -> list[str] | None:
    """Fetch the live model list from the CommandCode /models endpoint.

    Returns a flat list of model IDs or None on failure.
    No auth required — the public models endpoint is open.
    """
    try:
        req = urllib.request.Request(_COMMANDCODE_MODELS_URL)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", _profile_user_agent())
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        # Response shape: {"object": "list", "data": [{"id": "..."}, ...]}
        return [
            m["id"]
            for m in data.get("data", [])
            if isinstance(m, dict) and "id" in m
        ]
    except Exception as exc:
        logger.debug("fetch_models(commandcode): %s", exc)
        return None


def _commandcode_cookie_header(raw: str | None) -> str | None:
    """Extract only a recognized session cookie from a pasted Cookie header."""
    text = str(raw or "").strip()
    if not text or any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in text):
        return None
    if "=" not in text and ";" not in text:
        return f"__Secure-better-auth.session_token={text}"
    pairs: dict[str, tuple[str, str]] = {}
    for chunk in text.split(";"):
        name, separator, value = chunk.strip().partition("=")
        if separator and name.strip() and value.strip():
            pairs[name.strip().lower()] = (name.strip(), value.strip())
    for expected in _COMMANDCODE_SESSION_COOKIES:
        match = pairs.get(expected.lower())
        if match:
            return f"{match[0]}={match[1]}"
    return None


def _commandcode_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _commandcode_datetime(value: Any) -> datetime | None:
    number = _commandcode_number(value)
    if number is not None and number > 0:
        if number > 10_000_000_000:
            number /= 1000
        return datetime.fromtimestamp(number, tz=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _commandcode_get_json(url: str, cookie_header: str, timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(url)
    req.add_header("Cookie", cookie_header)
    req.add_header("Accept", "application/json, text/plain, */*")
    req.add_header("Origin", "https://commandcode.ai")
    req.add_header("Referer", "https://commandcode.ai/")
    req.add_header("User-Agent", _profile_user_agent())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read().decode())
    if not isinstance(payload, dict):
        raise ValueError("CommandCode billing response must be an object")
    return payload


def _commandcode_usage_window(value: Any, label: str):
    from agent.account_usage import AccountUsageWindow

    if not isinstance(value, dict):
        return None
    cap = _commandcode_number(value.get("cap"))
    used = _commandcode_number(value.get("used"))
    if cap is None or cap <= 0 or used is None or used < 0:
        return None
    return AccountUsageWindow(
        label=label,
        used_percent=max(0.0, min(100.0, used / cap * 100.0)),
        reset_at=_commandcode_datetime(value.get("resetAt")),
        detail=f"${used:.2f} of ${cap:.2f} used",
    )


def _fetch_commandcode_account_usage(timeout: float = 15.0):
    from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow

    cookie_header = _commandcode_cookie_header(os.getenv("COMMANDCODE_SESSION_COOKIE"))
    if not cookie_header:
        return None
    credits_timeout = max(0.5, min(timeout, 7.0))
    credits_payload = _commandcode_get_json(
        _COMMANDCODE_CREDITS_URL, cookie_header, credits_timeout
    )
    credits = credits_payload.get("credits")
    if not isinstance(credits, dict):
        return None
    monthly_remaining = _commandcode_number(credits.get("monthlyCredits"))
    if monthly_remaining is None or monthly_remaining < 0:
        return None

    subscription: dict[str, Any] = {}
    subscription_timeout = min(2.0, max(0.0, timeout - credits_timeout))
    if subscription_timeout > 0:
        try:
            sub_payload = _commandcode_get_json(
                _COMMANDCODE_SUBSCRIPTIONS_URL, cookie_header, subscription_timeout
            )
            if sub_payload.get("success") is True and isinstance(sub_payload.get("data"), dict):
                subscription = sub_payload["data"]
        except Exception:
            logger.debug("CommandCode subscription enrichment failed", exc_info=True)

    windows: list[AccountUsageWindow] = []
    limits = credits_payload.get("windowLimits")
    if not isinstance(limits, dict):
        limits = credits.get("windowLimits")
    if isinstance(limits, dict):
        for key, label in (("fiveHour", "5-hour limit"), ("weekly", "Weekly limit")):
            window = _commandcode_usage_window(limits.get(key), label)
            if window is not None:
                windows.append(window)

    plan_id = str(subscription.get("planId") or "").strip().lower()
    plan = _COMMANDCODE_PLANS.get(plan_id)
    period_end = _commandcode_datetime(subscription.get("currentPeriodEnd"))
    if plan and 0 <= monthly_remaining <= plan[1]:
        monthly_used = plan[1] - monthly_remaining
        windows.append(
            AccountUsageWindow(
                label="Monthly credits",
                used_percent=monthly_used / plan[1] * 100.0,
                reset_at=period_end,
                detail=f"${monthly_remaining:.2f} of ${plan[1]:.2f} remaining",
            )
        )

    details: list[str] = []
    if not plan:
        details.append(f"Monthly credits remaining: ${monthly_remaining:.2f}")
    purchased = _commandcode_number(credits.get("purchasedCredits"))
    if purchased is not None and purchased > 0:
        details.append(f"Purchased credits: ${purchased:.2f}")
    return AccountUsageSnapshot(
        provider="commandcode",
        source="billing_cookie_api",
        fetched_at=datetime.now(timezone.utc),
        title="CommandCode limits",
        plan=plan[0] if plan else None,
        windows=tuple(windows),
        details=tuple(details),
    )


# ── Chat Completions profile ──────────────────────────────────────────────────

class CommandCodeProfile(ProviderProfile):
    """CommandCode — OpenAI-compatible chat completions endpoint."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Fetch from the public CommandCode /models endpoint."""
        return _fetch_commandcode_models(timeout=timeout)

    def fetch_account_usage(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
    ):
        return _fetch_commandcode_account_usage(timeout=timeout)


commandcode = CommandCodeProfile(
    name="commandcode",
    aliases=("commandcode-chat",),
    api_mode="chat_completions",
    env_vars=_COMMANDCODE_ENV,
    display_name="CommandCode",
    description="CommandCode — 20+ models via OpenAI-compatible API",
    signup_url="https://commandcode.ai/",
    base_url=_COMMANDCODE_BASE,
    models_url=_COMMANDCODE_MODELS_URL,
    fallback_models=(
        "deepseek/deepseek-v4-pro",
        "deepseek/deepseek-v4-flash",
        "Qwen/Qwen3.7-Max",
        "Qwen/Qwen3.6-Plus",
        "moonshotai/Kimi-K2.6",
        "zai-org/GLM-5.1",
        "MiniMaxAI/MiniMax-M2.7",
        "stepfun/Step-3.5-Flash",
        "xiaomi/mimo-v2.5-pro",
        "google/gemini-3.5-flash",
        "gpt-5.5",
    ),
    default_aux_model="deepseek/deepseek-v4-flash",
)


# ── Anthropic Messages profile ────────────────────────────────────────────────

class CommandCodeAnthropicProfile(ProviderProfile):
    """CommandCode — Anthropic Messages API-compatible endpoint.

    Uses Bearer auth (same API key), not Anthropic's native x-api-key header.
    ``agent/anthropic_adapter.py`` must recognize ``api.commandcode.ai``
    as a Bearer-auth domain for this to work.
    """

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Fetch from the public CommandCode /models endpoint.

        Filter to Anthropic-family models only (claude-*).
        """
        all_models = _fetch_commandcode_models(timeout=timeout)
        if all_models is None:
            return None
        return [m for m in all_models if m.startswith("claude-")]

    def fetch_account_usage(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 15.0,
    ):
        return _fetch_commandcode_account_usage(timeout=timeout)


commandcode_anthropic = CommandCodeAnthropicProfile(
    name="commandcode-anthropic",
    aliases=("commandcode-claude",),
    api_mode="anthropic_messages",
    env_vars=_COMMANDCODE_ANTHROPIC_ENV,
    display_name="CommandCode (Anthropic)",
    description="CommandCode — Claude models via Anthropic Messages API",
    signup_url="https://commandcode.ai/",
    base_url=_COMMANDCODE_BASE,
    models_url=_COMMANDCODE_MODELS_URL,
    fallback_models=(
        "claude-sonnet-4-6",
        "claude-opus-4-7",
        "claude-haiku-4-5-20251001",
    ),
    default_aux_model="claude-haiku-4-5-20251001",
)


# ── Registration ──────────────────────────────────────────────────────────────
register_provider(commandcode)
register_provider(commandcode_anthropic)
