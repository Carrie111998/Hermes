"""Gateway runtime-metadata footer.

Renders a compact footer showing runtime state (model, context %, cwd) and
appends it to the FINAL message of an agent turn when enabled.  Off by default
to keep replies minimal.

Config (``~/.hermes/config.yaml``)::

    display:
      runtime_footer:
        enabled: true                       # off by default
        fields: [model, context_pct, cwd]   # order shown; drop any to hide
      context_meter:
        footer_floor: 0.70                  # always-on meter kicks in here

Per-platform overrides live under ``display.platforms.<platform>.runtime_footer``.
Users can toggle the global setting with ``/footer on|off`` from both the CLI
and any gateway platform.

Even with the manual footer off, an always-on meter footer surfaces once
context pressure crosses ``context_meter.footer_floor`` — the fraction of the
way to auto-compaction (default 70%) — so a silent compaction never sneaks up.
See ``build_meter_footer``.

The footer is appended to the final response text in ``gateway/run.py`` right
before returning the response to the adapter send path — so it only lands on
the final message a user sees, not on tool-progress updates or streaming
partials.  When streaming is on and the final text has already been delivered
piecemeal, the footer is sent as a separate trailing message via
``send_trailing_footer()``.
"""

from __future__ import annotations

import os
from typing import Any, Iterable, Optional

_DEFAULT_FIELDS: tuple[str, ...] = ("model", "context_pct", "cwd")
# Fields for the always-on footer that appears once context pressure crosses
# the floor even when the manual footer is off. Headlines the compaction meter.
_METER_FIELDS: tuple[str, ...] = ("model", "compaction", "cwd")
_SEP = " · "

# Fraction of the way to auto-compaction at which the reply footer starts
# showing on its own (even with the manual footer off). Tunable via
# ``display.context_meter.footer_floor``. 0.70 = surface at 70% of the way to
# the ~50%-of-window compaction point (i.e. ~35% of the raw window).
_DEFAULT_FOOTER_FLOOR = 0.70


def compaction_percent(context_tokens: int, threshold_tokens: Optional[int]) -> Optional[int]:
    """Return usage as a % of the compaction threshold, or None if unknown.

    100 means auto-compaction fires now. Can exceed 100 (compaction is checked
    after a send, so usage briefly overshoots the trigger).
    """
    if threshold_tokens and threshold_tokens > 0 and context_tokens >= 0:
        return max(0, round((context_tokens / threshold_tokens) * 100))
    return None


def _compaction_marker(pct: int) -> str:
    """Render the compaction-pressure field: an emoji tier plus the percentage."""
    if pct >= 100:
        emoji = "🔴"
    elif pct >= 85:
        emoji = "🟠"
    else:
        emoji = "🟡"
    return f"{emoji} {pct}% to compaction"


def resolve_meter_config(user_config: dict[str, Any] | None) -> dict[str, Any]:
    """Resolve the always-on context-meter footer config.

    ``display.context_meter.footer_floor`` — fraction (0-1) of the way to
    auto-compaction at which the footer surfaces on its own. Values outside
    (0, 1] fall back to the default so a stray 0/negative can't either spam
    every reply or silence the meter entirely.
    """
    floor = _DEFAULT_FOOTER_FLOOR
    cfg = (user_config or {}).get("display") or {}
    meter = cfg.get("context_meter")
    if isinstance(meter, dict) and "footer_floor" in meter:
        try:
            candidate = float(meter["footer_floor"])
            if 0.0 < candidate <= 1.0:
                floor = candidate
        except (TypeError, ValueError):
            pass
    return {"footer_floor": floor}


def _home_relative_cwd(cwd: str) -> str:
    """Return *cwd* with ``$HOME`` collapsed to ``~``.  Empty string if unset."""
    if not cwd:
        return ""
    try:
        home = os.path.expanduser("~")
        p = os.path.abspath(cwd)
        if home and (p == home or p.startswith(home + os.sep)):
            return "~" + p[len(home):]
        return p
    except Exception:
        return cwd


def _model_short(model: Optional[str]) -> str:
    """Drop ``vendor/`` prefix for readability (``openai/gpt-5.4`` → ``gpt-5.4``)."""
    if not model:
        return ""
    return model.rsplit("/", 1)[-1]


def resolve_footer_config(
    user_config: dict[str, Any] | None,
    platform_key: str | None = None,
) -> dict[str, Any]:
    """Resolve effective runtime-footer config for *platform_key*.

    Merge order (later wins):
        1. Built-in defaults (enabled=False)
        2. ``display.runtime_footer``
        3. ``display.platforms.<platform_key>.runtime_footer``
    """
    resolved = {"enabled": False, "fields": list(_DEFAULT_FIELDS)}
    cfg = (user_config or {}).get("display") or {}

    global_cfg = cfg.get("runtime_footer")
    if isinstance(global_cfg, dict):
        if "enabled" in global_cfg:
            resolved["enabled"] = bool(global_cfg.get("enabled"))
        if isinstance(global_cfg.get("fields"), list) and global_cfg["fields"]:
            resolved["fields"] = [str(f) for f in global_cfg["fields"]]

    if platform_key:
        platforms = cfg.get("platforms") or {}
        plat_cfg = platforms.get(platform_key)
        if isinstance(plat_cfg, dict):
            plat_footer = plat_cfg.get("runtime_footer")
            if isinstance(plat_footer, dict):
                if "enabled" in plat_footer:
                    resolved["enabled"] = bool(plat_footer.get("enabled"))
                if isinstance(plat_footer.get("fields"), list) and plat_footer["fields"]:
                    resolved["fields"] = [str(f) for f in plat_footer["fields"]]

    return resolved


def format_runtime_footer(
    *,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    fields: Iterable[str] = _DEFAULT_FIELDS,
    threshold_tokens: Optional[int] = None,
) -> str:
    """Render the footer line, or return "" if no fields have data.

    Fields are skipped silently when their underlying data is missing — a
    partially-populated footer is better than a line with ``?%`` or empty slots.
    """
    parts: list[str] = []
    for field in fields:
        if field == "model":
            m = _model_short(model)
            if m:
                parts.append(m)
        elif field == "context_pct":
            if context_length and context_length > 0 and context_tokens >= 0:
                pct = max(0, min(100, round((context_tokens / context_length) * 100)))
                parts.append(f"{pct}%")
        elif field == "compaction":
            pct = compaction_percent(context_tokens, threshold_tokens)
            if pct is not None:
                parts.append(_compaction_marker(pct))
        elif field == "cwd":
            rel = _home_relative_cwd(cwd or os.environ.get("TERMINAL_CWD", ""))
            if rel:
                parts.append(rel)
        # Unknown field names are silently ignored.

    if not parts:
        return ""
    return _SEP.join(parts)


def build_footer_line(
    *,
    user_config: dict[str, Any] | None,
    platform_key: str | None,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    threshold_tokens: Optional[int] = None,
) -> str:
    """Manual runtime footer (``/footer on``). Empty when disabled or no data."""
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    return format_runtime_footer(
        model=model,
        context_tokens=context_tokens,
        context_length=context_length,
        cwd=cwd,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
        threshold_tokens=threshold_tokens,
    )


def build_meter_footer(
    *,
    user_config: dict[str, Any] | None,
    platform_key: str | None,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    threshold_tokens: Optional[int] = None,
    cwd: Optional[str] = None,
) -> str:
    """Top-level footer entry point used by gateway/run.py.

    Two ways a footer lands on a reply:
      1. The user turned it on with ``/footer on`` → the configured manual
         footer, unchanged. If context pressure is also past the floor, the
         compaction marker is appended so the warning rides along.
      2. The footer is off but context pressure has crossed
         ``display.context_meter.footer_floor`` (default 70% of the way to
         auto-compaction) → an always-on meter footer surfaces on its own so a
         silent compaction never sneaks up.

    Returns "" when neither applies. The caller appends this to the final
    reply, preserving one blank line of separation.
    """
    manual = build_footer_line(
        user_config=user_config,
        platform_key=platform_key,
        model=model,
        context_tokens=context_tokens,
        context_length=context_length,
        cwd=cwd,
        threshold_tokens=threshold_tokens,
    )

    pct = compaction_percent(context_tokens, threshold_tokens)
    floor_pct = resolve_meter_config(user_config)["footer_floor"] * 100
    past_floor = pct is not None and pct >= floor_pct

    if manual:
        # Manual footer already shows the compaction field → don't double it.
        if past_floor and "to compaction" not in manual:
            return f"{manual}{_SEP}{_compaction_marker(pct)}"
        return manual

    if past_floor:
        return format_runtime_footer(
            model=model,
            context_tokens=context_tokens,
            context_length=context_length,
            cwd=cwd,
            fields=_METER_FIELDS,
            threshold_tokens=threshold_tokens,
        )
    return ""
