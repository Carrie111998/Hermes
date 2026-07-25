"""Hybrid local/cloud model routing.

Route SIMPLE prompts to a cheap/local model and keep COMPLEX prompts on the
session's primary model (the ``"cloud"`` target). The classifier is pure
heuristics — prompt length, complexity keywords, code fences, and attachments —
so there is no extra model round-trip and no added latency or cost.

The decision is made per turn in ``HermesCLI._resolve_turn_agent_config``
(``hermes_cli/cli_agent_setup_mixin.py``). When the chosen target differs from
the session's primary model, the caller overrides the turn route and the
existing route-signature machinery swaps the live agent transparently — the
same seam ``/fast`` and MoA already use.

Design guarantees (mirroring ``agent.image_routing``):

* **No-op until configured.** If ``routing.local`` has no model (and, for
  keyless local servers, no base_url) the router returns ``"cloud"`` so
  everything runs on the primary model — zero behavior change for existing
  users.
* **Fail-safe.** Callers treat any exception here as "route to cloud/primary";
  the classifier itself never raises on malformed config (it coerces).
* **Deterministic.** Same prompt + config → same decision. Trivially testable.

This module is intentionally free of ``cli``/agent imports so it stays cheap to
import and easy to unit-test in isolation.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

# Route targets. ``"local"`` → the configured local endpoint; ``"cloud"`` → the
# session's primary model (the one selected via ``hermes model``).
LOCAL = "local"
CLOUD = "cloud"

# Default heuristic knobs — used when a value is missing/malformed in config.
# Kept in sync with the ``routing.complexity`` block in
# ``hermes_cli/config.py``'s DEFAULT_CONFIG.
_DEFAULT_MAX_PROMPT_CHARS = 1500
_DEFAULT_MAX_PROMPT_TOKENS = 400
_DEFAULT_CLOUD_KEYWORDS = (
    "refactor", "debug", "architect", "architecture", "analyze",
    "analyse", "prove", "design", "optimize", "optimise", "migrate",
    "security", "vulnerab", "trace", "root cause", "why does",
    "why is", "explain why", "step by step", "algorithm",
)

# A fenced code block: ``` (optionally with a language tag). Cheap to match.
_CODE_FENCE_RE = re.compile(r"```")


def _coerce_int(value: Any, default: int) -> int:
    """Best-effort positive int, else ``default``."""
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly.
        return default
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.strip().lower()
        if v in ("true", "yes", "on", "1"):
            return True
        if v in ("false", "no", "off", "0"):
            return False
    return default


def _coerce_keywords(value: Any) -> tuple[str, ...]:
    """Normalize a keyword list to lowercase, dropping blanks.

    Falls back to the built-in defaults when the config value is missing or not
    a list (a str is NOT treated as an iterable of chars).
    """
    if not isinstance(value, (list, tuple)):
        return _DEFAULT_CLOUD_KEYWORDS
    out = []
    for item in value:
        if isinstance(item, str):
            kw = item.strip().lower()
            if kw:
                out.append(kw)
    return tuple(out)


def _complexity_cfg(routing_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if isinstance(routing_cfg, dict):
        cx = routing_cfg.get("complexity")
        if isinstance(cx, dict):
            return cx
    return {}


def _estimate_tokens(text: str) -> int:
    """Crude token estimate: ~4 chars/token. Good enough for a threshold."""
    return (len(text) + 3) // 4


def is_routing_enabled(routing_cfg: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(routing_cfg, dict):
        return False
    return _coerce_bool(routing_cfg.get("enabled"), False)


def is_local_configured(routing_cfg: Optional[Dict[str, Any]]) -> bool:
    """True when ``routing.local`` names a usable endpoint.

    A local endpoint is usable once it has a model. A ``base_url`` alone (with
    no model) is not enough — local OpenAI-compatible servers still need a model
    name to send. This is the gate that keeps routing inert out of the box.
    """
    if not isinstance(routing_cfg, dict):
        return False
    local = routing_cfg.get("local")
    if not isinstance(local, dict):
        return False
    model = local.get("model")
    return isinstance(model, str) and bool(model.strip())


def classify_complexity(
    prompt: str,
    *,
    has_images: bool = False,
    routing_cfg: Optional[Dict[str, Any]] = None,
) -> str:
    """Return ``LOCAL`` for a simple prompt or ``CLOUD`` for a complex one.

    A prompt escalates to CLOUD when ANY trigger fires:
      * it exceeds ``max_prompt_chars`` or ``max_prompt_tokens``,
      * it contains a ``cloud_keywords`` substring,
      * it carries an image/file attachment (``escalate_on_images``),
      * it contains a fenced code block (``escalate_on_code_fence``).

    Otherwise it stays LOCAL. Never raises on bad config — every knob is coerced
    to a sane default.
    """
    text = prompt if isinstance(prompt, str) else ""
    cx = _complexity_cfg(routing_cfg)

    max_chars = _coerce_int(cx.get("max_prompt_chars"), _DEFAULT_MAX_PROMPT_CHARS)
    max_tokens = _coerce_int(cx.get("max_prompt_tokens"), _DEFAULT_MAX_PROMPT_TOKENS)
    keywords = _coerce_keywords(cx.get("cloud_keywords"))
    escalate_images = _coerce_bool(cx.get("escalate_on_images"), True)
    escalate_code = _coerce_bool(cx.get("escalate_on_code_fence"), True)

    if has_images and escalate_images:
        return CLOUD

    if len(text) > max_chars:
        return CLOUD

    if _estimate_tokens(text) > max_tokens:
        return CLOUD

    if escalate_code and _CODE_FENCE_RE.search(text):
        return CLOUD

    lowered = text.lower()
    for kw in keywords:
        if kw in lowered:
            return CLOUD

    return LOCAL


def decide_route(
    prompt: str,
    routing_cfg: Optional[Dict[str, Any]],
    *,
    has_images: bool = False,
    force: Optional[str] = None,
) -> str:
    """Resolve the effective route target for a turn.

    Args:
      prompt:      the user's text for this turn.
      routing_cfg: the ``routing`` config block (``config["routing"]``), or None.
      has_images:  whether the turn carries image/file attachments.
      force:       an explicit override from ``/local`` / ``/cloud`` — takes
                   precedence over everything except the "local unconfigured"
                   guard, which always wins so a forced-local turn can't send a
                   blank model.

    Returns ``LOCAL`` or ``CLOUD``. Returns ``CLOUD`` (→ primary model) whenever
    routing is disabled or the local endpoint isn't configured.
    """
    if not is_routing_enabled(routing_cfg):
        return CLOUD

    # Local must be configured for a local route to be possible. This guard
    # also overrides an explicit ``/local`` force so we never dispatch a turn
    # to an empty model.
    if not is_local_configured(routing_cfg):
        return CLOUD

    if force in (LOCAL, CLOUD):
        return force

    return classify_complexity(
        prompt,
        has_images=has_images,
        routing_cfg=routing_cfg,
    )
