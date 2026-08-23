"""Runtime override support for ``pre_llm_call`` plugin hooks.

A plugin may return ``{"runtime_override": {...}}`` from ``pre_llm_call`` to
proactively override the LLM API call parameters for the current turn:

    {"context": "recalled text...",            # existing behavior, unchanged
     "runtime_override": {
         "model": "gpt-5.6",
         "provider": "openai",
         "base_url": "https://api.openai.com/v1",
         "api_key": "sk-...",
         "api_mode": "chat_completions",
         "system_prompt": "You are ...",
     }}

Contract (mirrors the ``pre_failover_decision`` redirect contract):

* ``redirect`` is *error-driven*: it is applied by the retry/failover machinery
  only after an API call has failed.  ``runtime_override`` is *proactive*: it is
  applied before the first API call of the turn.  The two do not conflict —
  redirect rewrites identity on the failover path, runtime_override rewrites
  identity on the primary path.
* The override is ephemeral and turn-scoped: it lives on ``agent._runtime_override``,
  is re-resolved on every turn prologue, is never persisted to the session DB and
  is never injected into the user message / session history.
* Unsupported keys are logged with a one-line warning and ignored (never crash).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Keys a plugin may override.  Anything else is logged and ignored.
#: ``system_prompt`` is intentionally NOT supported: it is the prompt-cache
#: prefix (byte-stable for the life of a conversation), so overriding it would
#: invalidate the cache and drop the core instructions. Model/provider routing
#: does not need it; persona switching needs a separate cache-safe design.
RUNTIME_OVERRIDE_KEYS = frozenset(
    {"model", "provider", "base_url", "api_key", "api_mode"}
)

#: All supported keys are plain non-empty strings.
_STRING_KEYS = frozenset(
    {"model", "provider", "base_url", "api_key", "api_mode"}
)

#: Known wire protocols.  Unknown api_mode values are rejected (logged+ignored)
#: instead of being forwarded into the call path.
KNOWN_API_MODES = frozenset(
    {"chat_completions", "anthropic_messages", "codex_responses", "bedrock_converse"}
)


def validate_runtime_override(overrides: Any) -> Dict[str, str]:
    """Type-check + filter a plugin-provided ``runtime_override`` dict.

    Returns only the supported, correctly-typed keys.  Unsupported keys and
    wrong-typed values are logged with a one-line warning and dropped, so a
    misbehaving plugin can never crash the turn.
    """
    if not isinstance(overrides, dict):
        logger.warning(
            "pre_llm_call runtime_override ignored: expected dict, got %s",
            type(overrides).__name__,
        )
        return {}
    valid: Dict[str, str] = {}
    for key, value in overrides.items():
        if key not in RUNTIME_OVERRIDE_KEYS:
            logger.warning(
                "pre_llm_call runtime_override: unsupported key %r ignored "
                "(supported: %s)",
                key,
                ", ".join(sorted(RUNTIME_OVERRIDE_KEYS)),
            )
            continue
        if key in _STRING_KEYS and (not isinstance(value, str) or not value.strip()):
            logger.warning(
                "pre_llm_call runtime_override: key %r must be a non-empty "
                "string, ignored",
                key,
            )
            continue
        if key == "api_mode" and value not in KNOWN_API_MODES:
            logger.warning(
                "pre_llm_call runtime_override: unsupported api_mode %r "
                "(known: %s), ignored",
                value,
                ", ".join(sorted(KNOWN_API_MODES)),
            )
            continue
        valid[key] = value
    return valid


class _RuntimeOverrideScope:
    """Context manager that temporarily applies an override to ``agent``.

    Snapshots the runtime attributes the LLM call path reads (plus the derived
    client kwargs) and restores them on exit, so the override is active only
    for the wrapped API-attempt block.  ``base_url`` is a property whose setter
    refreshes ``_base_url_lower`` / ``_base_url_hostname``, so plain assignment
    keeps the derived host-matching state consistent.
    """

    _ATTRS = ("model", "provider", "api_mode", "api_key")
    _MISSING = object()

    def __init__(self, agent: Any, overrides: Dict[str, str]) -> None:
        self.agent = agent
        self.overrides = overrides
        self._snapshot: Dict[str, Any] = {}
        self._client_kwargs_snapshot: Optional[Dict[str, Any]] = None

    def __enter__(self) -> "_RuntimeOverrideScope":
        agent = self.agent
        for name in self._ATTRS:
            if name in self.overrides:
                self._snapshot[name] = getattr(agent, name, self._MISSING)
        if "base_url" in self.overrides:
            self._snapshot["base_url"] = getattr(agent, "base_url", self._MISSING)
        # _client_kwargs feeds the per-request OpenAI-wire client.  Snapshot a
        # shallow copy so in-place mutation is reversible.
        ck = getattr(agent, "_client_kwargs", None)
        if isinstance(ck, dict):
            self._client_kwargs_snapshot = dict(ck)
            self._snapshot["_client_kwargs"] = ck
        for name, ov_key in (
            ("_anthropic_api_key", "api_key"),
            ("_anthropic_base_url", "base_url"),
        ):
            if ov_key in self.overrides and hasattr(agent, name):
                self._snapshot[name] = getattr(agent, name, self._MISSING)

        ov = self.overrides
        for name in self._ATTRS:
            if name in ov:
                setattr(agent, name, ov[name])
        if "base_url" in ov:
            agent.base_url = str(ov["base_url"]).strip().rstrip("/")
        if "api_key" in ov and isinstance(ck, dict):
            ck["api_key"] = ov["api_key"]
        if "base_url" in ov and isinstance(ck, dict):
            ck["base_url"] = str(ov["base_url"]).strip().rstrip("/")
        if "_anthropic_api_key" in self._snapshot and "api_key" in ov:
            agent._anthropic_api_key = ov["api_key"]
        if "_anthropic_base_url" in self._snapshot and "base_url" in ov:
            agent._anthropic_base_url = str(ov["base_url"]).strip().rstrip("/")
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        agent = self.agent
        for name, value in self._snapshot.items():
            if name == "_client_kwargs":
                continue  # restored from the shallow copy below
            if value is self._MISSING:
                # Attribute did not exist before the override — don't
                # fabricate it (tests build bare agents via __new__).
                try:
                    delattr(agent, name)
                except Exception:  # noqa: BLE001
                    pass
                continue
            try:
                setattr(agent, name, value)
            except Exception:  # noqa: BLE001 — restore must never raise
                pass
        if self._client_kwargs_snapshot is not None:
            ck = getattr(agent, "_client_kwargs", None)
            if isinstance(ck, dict):
                ck.clear()
                ck.update(self._client_kwargs_snapshot)


def apply_runtime_override(agent: Any, overrides: Dict[str, str]) -> "_RuntimeOverrideScope":
    """Return a context manager that applies ``overrides`` to ``agent``."""
    return _RuntimeOverrideScope(agent, overrides)
