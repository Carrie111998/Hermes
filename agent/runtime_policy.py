"""P0 cost-leakage hotfix — READ_ONLY runtime policy + billable-provider guard.

Fail-fast enforcement, imported at the very top of ``execute_code`` and
``delegate_task`` so a READ_ONLY session can never initialize a model,
spawn a worker, or emit an API request.

A session is READ_ONLY when any of:
  * config key ``runtime_policy.mode == "read_only"``
  * env var ``HERMES_RUNTIME_POLICY=read_only``

The guard never retries and never falls back: it raises
:class:`RuntimePolicyError` immediately.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

READ_ONLY = "read_only"
NORMAL = "normal"

# Providers that meter per-request billing. Nous Portal (OAuth subscription)
# is NOT billable per-call; everything in this set costs money per request.
BILLABLE_PROVIDERS = frozenset(
    {
        "gemini",
        "google",           # alias resolved to gemini, kept for defense
        "openrouter",
        "anthropic",
        "openai-api",
        "openai",
        "xai",
        "xai-oauth",
        "deepseek",
        "kimi-coding",
        "kimi-coding-cn",
        "minimax-cn",
        "minimax-oauth",
        "zai",
        "glm",              # alias, defensive
        "mistral",
        "stepfun",
        "qwen-oauth",
        "alibaba-coding-plan",
    }
)

# The only provider allowed to win default/auto resolution for this profile.
DEFAULT_SAFE_PROVIDER = "nous"


class RuntimePolicyError(RuntimeError):
    """Raised immediately when a READ_ONLY session attempts a billable action."""

    def __init__(self, tool_name: str, detail: str = ""):
        self.tool_name = tool_name
        msg = (
            f"RuntimePolicyError: '{tool_name}' is blocked — session mode is "
            f"READ_ONLY. No model/provider was initialized and no API request "
            f"was made. This guard does not retry."
        )
        if detail:
            msg += f" ({detail})"
        super().__init__(msg)


def _config_policy_mode() -> Optional[str]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        mode = (cfg.get("runtime_policy") or {}).get("mode")
        return str(mode).strip().lower() if mode else None
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("Could not read runtime_policy.mode from config: %s", e)
        return None


def get_runtime_policy_mode() -> str:
    """Resolve the effective session policy mode (fail-open to NORMAL only on
    config-read failure; an explicit READ_ONLY signal always wins)."""
    env_mode = (os.environ.get("HERMES_RUNTIME_POLICY") or "").strip().lower()
    if env_mode in (READ_ONLY, "readonly"):
        return READ_ONLY
    cfg_mode = _config_policy_mode()
    if cfg_mode in (READ_ONLY, "readonly"):
        return READ_ONLY
    return NORMAL


def enforce_read_only(tool_name: str) -> None:
    """Raise :class:`RuntimePolicyError` NOW if the session is READ_ONLY.

    Must be called before any model/provider initialization, worker spawn,
    or network request. No retry, no fallback.
    """
    if get_runtime_policy_mode() == READ_ONLY:
        raise RuntimePolicyError(tool_name)


def is_billable_provider(provider: Optional[str]) -> bool:
    """True when ``provider`` belongs to the per-request-billable group."""
    p = (provider or "").strip().lower()
    if not p:
        return False
    if p in ("nous", "nous-portal"):
        return False
    return p in BILLABLE_PROVIDERS


def enforce_cost_guard(tool_name: str, provider: Optional[str]) -> None:
    """Billable-provider gate: block BEFORE execution, no retry."""
    if get_runtime_policy_mode() == READ_ONLY and is_billable_provider(provider):
        raise RuntimePolicyError(
            tool_name,
            f"billable provider '{provider}' cannot be used in READ_ONLY mode",
        )
