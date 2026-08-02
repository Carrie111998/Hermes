"""Model routing layer for IYARI agents: configuration, not code.

``select_model`` translates a named profile (``cheap``/``main``/``premium``)
from ``config/model_config.yaml`` into the exact shapes the codebase's
*existing* runtime already consumes:

  * ``provider`` / ``model`` -> ``AIAgent(provider=..., model=...)``.
  * ``fallback_chain`` -> ``AIAgent(fallback_model=[...])``, which
    ``agent_init.py`` copies onto ``agent._fallback_chain``.

This module deliberately does **not** implement retry, backoff, or
timeout/rate-limit handling itself. That mechanism already exists and is
extensively tested: ``agent/error_classifier.py`` (``FailoverReason``) and
``agent/chat_completion_helpers.py`` (``try_activate_fallback``), wired into
every turn via ``agent/conversation_loop.py``. Reimplementing it here would
duplicate a production-hardened system and risk the two disagreeing. This
module's only job is to get the *right data* into that mechanism's hands.

Fase 1 scope: explicit profile selection only. The caller picks
``cheap``/``main``/``premium`` -- there is no automatic complexity-based
classifier yet (future work).
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

import yaml

_DEFAULT_MODEL_CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "model_config.yaml",
)


def load_model_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Load and parse ``config/model_config.yaml``.

    Unlike the meta-prompt loader, this raises on failure rather than
    degrading silently: a missing or malformed model config means we do not
    know which model to call, which is a hard stop, not a "slightly weaker
    prompt" situation.

    Raises:
        FileNotFoundError: the config file does not exist.
        ValueError: the file is not valid YAML, or does not parse to a
            mapping with a ``profiles`` section.
    """
    resolved = path or _DEFAULT_MODEL_CONFIG_PATH
    try:
        with open(resolved, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"model_config.yaml not found at {resolved!r}") from None
    except yaml.YAMLError as exc:
        raise ValueError(f"model_config.yaml at {resolved!r} is not valid YAML: {exc}") from exc

    if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
        raise ValueError(
            f"model_config.yaml at {resolved!r} must be a mapping with a 'profiles' section"
        )
    return data


def _normalize_fallback_chain(raw_chain: Any) -> List[Dict[str, str]]:
    """Reduce a profile's fallback_chain entries to {provider, model} dicts.

    Silently drops malformed entries (missing provider/model) rather than
    raising -- a bad fallback entry should not block using the profile's
    primary model, it just means one less safety net. This mirrors
    ``agent_init.py``'s own filtering of ``fallback_providers`` entries.
    """
    if not isinstance(raw_chain, list):
        return []
    normalized = []
    for entry in raw_chain:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").strip()
        model = str(entry.get("model") or "").strip()
        if provider and model:
            normalized.append({"provider": provider, "model": model})
    return normalized


def select_model(profile: str, model_config: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a named profile into a ready-to-use model/fallback spec.

    Args:
        profile: one of the keys under ``model_config["profiles"]``
            (``cheap``/``main``/``premium`` in Fase 1 -- the caller chooses
            explicitly, there is no automatic classifier yet).
        model_config: the dict returned by :func:`load_model_config`.

    Returns:
        A dict with:
          * ``agent_id`` -- from the top-level config, for logging/routing.
          * ``profile`` -- echoes the requested profile name.
          * ``provider`` / ``model`` -- the primary backend to call.
          * ``max_tokens`` / ``timeout`` / ``max_retries`` -- per-profile
            operational limits (``None`` if unset in the config).
          * ``fallback_model`` -- a list of ``{"provider", "model"}`` dicts
            in the exact shape ``AIAgent(fallback_model=...)`` expects, so
            it can be passed straight through without adaptation. Empty
            list if the profile has no fallback_chain.

    Raises:
        KeyError: ``profile`` is not defined in ``model_config``.
        ValueError: the profile entry is missing ``provider`` or ``model``.
    """
    profiles = model_config.get("profiles") or {}
    entry = profiles.get(profile)
    if not isinstance(entry, dict):
        available = sorted(p for p in profiles if isinstance(p, str))
        raise KeyError(f"Unknown model profile {profile!r}. Available profiles: {available}")

    provider = str(entry.get("provider") or "").strip()
    model = str(entry.get("model") or "").strip()
    if not provider or not model:
        raise ValueError(f"Profile {profile!r} is missing 'provider' and/or 'model'")

    return {
        "agent_id": model_config.get("agent_id"),
        "profile": profile,
        "provider": provider,
        "model": model,
        "max_tokens": entry.get("max_tokens"),
        "timeout": entry.get("timeout"),
        "max_retries": entry.get("max_retries"),
        "fallback_model": _normalize_fallback_chain(entry.get("fallback_chain")),
    }


__all__ = ["load_model_config", "select_model"]
