"""Kimi / Moonshot provider profiles.

Kimi has dual endpoints:
  - sk-kimi-* keys → api.kimi.com/coding (Anthropic Messages API)
  - legacy keys → api.moonshot.ai/v1 (OpenAI chat completions)

This module covers the chat_completions path (/v1 endpoint).
"""

from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

from hermes_cli import __version__ as _HERMES_VERSION
from providers import register_provider
from providers.base import OMIT_TEMPERATURE, ProviderProfile


def _is_confirmed_kimi_coding_url(base_url: str) -> bool:
    """Return True only for Kimi Code's canonical HTTPS API surfaces."""
    try:
        parsed = urlparse(base_url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme.lower() == "https"
        and (parsed.hostname or "").lower() == "api.kimi.com"
        and port in (None, 443)
        and parsed.username is None
        and parsed.password is None
        and parsed.path.rstrip("/") in {"/coding", "/coding/v1"}
        and not parsed.query
        and not parsed.fragment
    )



KIMI_CODE_USAGE_BASE_URL = "https://api.kimi.com/coding/v1"

_KIMI_TIME_UNIT_SECONDS = {
    "TIME_UNIT_SECOND": 1,
    "TIME_UNIT_MINUTE": 60,
    "TIME_UNIT_HOUR": 3600,
    "TIME_UNIT_DAY": 86400,
}


def _kimi_window_label(window: dict) -> str:
    """Name a window from its declared length, not from a guessed period."""
    try:
        duration = int(window.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0
    seconds = duration * _KIMI_TIME_UNIT_SECONDS.get(str(window.get("timeUnit") or ""), 0)
    if seconds <= 0:
        return "window"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    return f"{max(1, seconds // 60)}m"


def _kimi_reset(value):
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


class KimiProfile(ProviderProfile):
    """Kimi/Moonshot — temperature omitted, thinking xor reasoning_effort."""

    def fetch_models(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ) -> list[str] | None:
        """Use Kimi Code's OpenAI-compatible surface for model discovery."""
        effective_base = (base_url or self.base_url or "").rstrip("/")
        confirmed_coding_endpoint = _is_confirmed_kimi_coding_url(effective_base)
        if confirmed_coding_endpoint and urlparse(effective_base).path.rstrip("/") == "/coding":
            effective_base += "/v1"
        models = super().fetch_models(
            api_key=api_key,
            base_url=effective_base or None,
            timeout=timeout,
        )
        if models is None or confirmed_coding_endpoint:
            return models
        return [model for model in models if model.strip().lower() != "k3"]

    def build_api_kwargs_extras(
        self, *, reasoning_config: dict | None = None, **context
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Kimi reasoning controls.

        Moonshot's wire shape treats ``extra_body.thinking`` (a binary toggle)
        and a top-level ``reasoning_effort`` as mutually exclusive — sending
        both is at best redundant and risks "cannot specify both 'thinking' and
        'reasoning_effort'" (HTTP 400). This mirrors the kimi-k2 handling on the
        opencode-go relay: send effort when one is requested, otherwise fall
        back to ``extra_body.thinking`` — never both.
        """
        extra_body = {}
        top_level = {}

        if not reasoning_config or not isinstance(reasoning_config, dict):
            # No config → thinking enabled, let the server pick the depth.
            # (Previously also sent reasoning_effort="medium", which paired
            # thinking + effort on every default call.)
            extra_body["thinking"] = {"type": "enabled"}
            return extra_body, top_level

        enabled = reasoning_config.get("enabled", True)
        if enabled is False:
            extra_body["thinking"] = {"type": "disabled"}
            return extra_body, top_level

        # Enabled: prefer an explicit effort; only fall back to extra_body
        # thinking when no recognized effort is requested.
        # K3's vocabulary (low/high/max, default high) and its documented
        # rounding (medium→high, xhigh→max) are declared in
        # agent.reasoning_effort — shared with the chat-completions
        # transport's Kimi path so both stay in sync.
        from agent.reasoning_effort import (
            KIMI_K3_EFFORTS,
            KIMI_K3_OVERRIDES,
            clamp_effort,
        )

        effort = (reasoning_config.get("effort") or "").strip().lower()
        if effort and effort != "none":
            k3_effort = clamp_effort(effort, KIMI_K3_EFFORTS, KIMI_K3_OVERRIDES)
        else:
            k3_effort = None
        if k3_effort in KIMI_K3_EFFORTS:
            top_level["reasoning_effort"] = k3_effort
        else:
            extra_body["thinking"] = {"type": "enabled"}

        return extra_body, top_level


    def fetch_usage(
        self,
        *,
        credential=None,
        base_url: str | None = None,
        timeout: float = 8.0,
    ):
        """Kimi Code plan quotas: a rolling window plus a longer one.

        ``GET {coding}/v1/usages``. The response labels a field ``used`` whose
        value tracks what is LEFT, while the sibling window calls the same
        quantity ``remaining`` — so nothing here derives a percentage from
        ``used``. Both figures are stored verbatim and the shared model derives
        a percentage only from ``limit`` + ``remaining``, which is unambiguous.

        Window length comes from the payload (``window.duration`` +
        ``timeUnit``), never from a guessed name: the second window's
        ``resetTime`` lands ~a day out, not a week, despite third-party docs
        calling it "weekly".
        """
        import httpx

        from agent.provider_usage_types import (
            UNIT_COUNT,
            ProviderUsage,
            UsageWindow,
            to_decimal,
        )

        token = str(getattr(credential, "access_token", "") or "").strip()
        if not token:
            return None

        base = str(base_url or getattr(credential, "base_url", "") or KIMI_CODE_USAGE_BASE_URL)
        base = base.rstrip("/")
        if not base.endswith("/v1"):
            base = base + "/v1"

        with httpx.Client(timeout=timeout) as client:
            response = client.get(
                f"{base}/usages",
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            response.raise_for_status()
            payload = response.json() or {}

        windows = []
        for entry in payload.get("limits") or ():
            if not isinstance(entry, dict):
                continue
            detail = entry.get("detail") or {}
            windows.append(
                UsageWindow(
                    label=_kimi_window_label(entry.get("window") or {}),
                    unit=UNIT_COUNT,
                    limit=to_decimal(detail.get("limit")),
                    remaining=to_decimal(detail.get("remaining")),
                    reset_at=_kimi_reset(detail.get("resetTime")),
                )
            )

        rolling = payload.get("usage")
        if isinstance(rolling, dict) and rolling:
            windows.append(
                UsageWindow(
                    label="period",
                    unit=UNIT_COUNT,
                    limit=to_decimal(rolling.get("limit")),
                    remaining=to_decimal(rolling.get("used")),
                    reset_at=_kimi_reset(rolling.get("resetTime")),
                )
            )

        membership = ((payload.get("user") or {}).get("membership") or {}).get("level")
        plan = str(membership or "").replace("LEVEL_", "").title() or None

        return ProviderUsage(
            provider="kimi-coding",
            display_name="Kimi Code",
            plan=plan,
            windows=tuple(windows),
        )


kimi = KimiProfile(
    name="kimi-coding",
    aliases=("kimi", "moonshot", "kimi-for-coding"),
    env_vars=("KIMI_API_KEY", "KIMI_CODING_API_KEY"),
    base_url="https://api.moonshot.ai/v1",
    fixed_temperature=OMIT_TEMPERATURE,
    default_max_tokens=32000,
    default_headers={
        "HTTP-Referer": "https://hermes-agent.nousresearch.com",
        "X-Title": "Hermes Agent",
        "User-Agent": f"HermesAgent/{_HERMES_VERSION}",
    },
    default_aux_model="kimi-k2-turbo-preview",
    # The short window is 5 hours; a minute of cache costs nothing and keeps
    # a burst of panel opens off the endpoint.
    usage_ttl=60,
)

kimi_cn = KimiProfile(
    name="kimi-coding-cn",
    aliases=("kimi-cn", "moonshot-cn"),
    env_vars=("KIMI_CN_API_KEY",),
    base_url="https://api.moonshot.cn/v1",
    fixed_temperature=OMIT_TEMPERATURE,
    default_max_tokens=32000,
    default_headers={
        "HTTP-Referer": "https://hermes-agent.nousresearch.com",
        "X-Title": "Hermes Agent",
        "User-Agent": f"HermesAgent/{_HERMES_VERSION}",
    },
    default_aux_model="kimi-k2-turbo-preview",
)

register_provider(kimi)
register_provider(kimi_cn)
