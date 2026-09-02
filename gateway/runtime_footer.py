"""Gateway runtime-metadata footer.

Renders a compact footer showing runtime state (model, context %, cwd) and
appends it to the FINAL message of an agent turn when enabled.  Off by default
to keep replies minimal.

Config (``~/.hermes/config.yaml``)::

    display:
      runtime_footer:
        enabled: true                       # off by default
        fields: [model, context_pct, cwd]   # order shown; drop any to hide

Available fields:
    model             — final active model, vendor prefix dropped (``gpt-5.4``)
    context_pct       — last-call context occupancy as a percent (``5%``)
    context_window    — last-call context used/total plus percent
                        (``ctx(last):123.0k/1.0M (12%)``)
    latency           — wall-clock duration of the turn (``22s``, ``1m05s``)
    cwd               — home-relative working dir (``~``)
    tokens_turn       — labelled non-cached turn usage
                        (``tokens(turn,uncached):15.9k in/1.2k out``)
    cache_hit         — provider-reported prompt cache hit ratio
                        (``cache(turn):87%``)
    reasoning_effort  — active model's request intent (``effort(req):max``)

``latency``, ``tokens_turn``, and ``reasoning_effort`` are opt-in: they are NOT
in the default field set, so a footer whose ``fields`` are unset renders
exactly as before.

Token fields are provider-reported accounting, not local estimates. Cached
input is excluded from ``tokens_turn`` but still occupies ``context_window``.
A turn whose provider reports usage for only some logical calls is labelled
``reported,partial``; a turn with no usable report omits token fields instead
of rendering a synthetic zero. ``reasoning_effort`` describes Hermes' request
intent for the active model, not reasoning tokens actually consumed. Cache and
context fields are omitted when the provider cannot prove those values.

Per-platform overrides live under ``display.platforms.<platform>.runtime_footer``.
Users can toggle the global setting with ``/footer on|off`` from both the CLI
and any gateway platform.

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
_SEP = " · "


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


def _format_latency(seconds: float) -> str:
    """Humanize a turn duration: ``<1s``, ``22s``, ``1m05s``."""
    if seconds < 1:
        return "<1s"
    total = int(round(seconds))
    if total < 60:
        return f"{total}s"
    m, sec = divmod(total, 60)
    return f"{m}m{sec:02d}s"


def _format_token_count(value: int) -> str:
    """Render a compact, stable token count for the footer."""
    if value >= 1_000_000:
        return f"{value / 1_000_000:.1f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)


def format_runtime_footer(
    *,
    model: Optional[str],
    context_tokens: int,
    context_length: Optional[int],
    cwd: Optional[str] = None,
    turn_seconds: Optional[float] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    token_usage_status: Optional[str] = None,
    cache_usage_status: Optional[str] = None,
    context_usage_status: Optional[str] = "reported",
    reasoning_effort: Optional[str] = None,
    fields: Iterable[str] = _DEFAULT_FIELDS,
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
            if (
                context_usage_status == "reported"
                and context_length
                and context_length > 0
                and context_tokens >= 0
            ):
                pct = max(0, min(100, round((context_tokens / context_length) * 100)))
                parts.append(f"{pct}%")
        elif field == "context_window":
            if (
                context_usage_status == "reported"
                and context_length
                and context_length > 0
                and context_tokens >= 0
            ):
                pct = max(0, min(100, round((context_tokens / context_length) * 100)))
                parts.append(
                    "ctx(last):"
                    f"{_format_token_count(context_tokens)}/"
                    f"{_format_token_count(context_length)} ({pct}%)"
                )
        elif field == "latency":
            # Wall-clock turn duration. Skipped when the caller supplied no
            # timing (call sites that don't measure) or the value is negative.
            if turn_seconds is not None and turn_seconds >= 0:
                parts.append(_format_latency(turn_seconds))
        elif field == "cwd":
            try:
                from tools.terminal_scope import terminal_env as _tenv
            except ImportError:
                env_cwd = os.environ.get("TERMINAL_CWD", "")
            else:
                env_cwd = _tenv("TERMINAL_CWD", "")
            rel = _home_relative_cwd(cwd or env_cwd)
            if rel:
                parts.append(rel)
        elif field == "tokens_turn":
            reported: list[str] = []
            if (
                isinstance(tokens_in, int)
                and not isinstance(tokens_in, bool)
                and tokens_in >= 0
            ):
                reported.append(f"{_format_token_count(tokens_in)} in")
            if (
                isinstance(tokens_out, int)
                and not isinstance(tokens_out, bool)
                and tokens_out >= 0
            ):
                reported.append(f"{_format_token_count(tokens_out)} out")
            if reported and token_usage_status in {"reported", "reported_partial"}:
                label = (
                    "tokens(turn,uncached,partial)"
                    if token_usage_status == "reported_partial"
                    else "tokens(turn,uncached)"
                )
                parts.append(f"{label}:{'/'.join(reported)}")
        elif field == "cache_hit":
            cache_buckets = (tokens_in, cache_read_tokens, cache_write_tokens)
            if (
                cache_usage_status in {"reported", "reported_partial"}
                and all(
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and value >= 0
                    for value in cache_buckets
                )
            ):
                prompt_tokens = sum(cache_buckets)
                if prompt_tokens > 0:
                    cache_pct = round((cache_read_tokens / prompt_tokens) * 100)
                    label = (
                        "cache(turn,partial)"
                        if cache_usage_status == "reported_partial"
                        else "cache(turn)"
                    )
                    parts.append(f"{label}:{cache_pct}%")
        elif field == "reasoning_effort":
            if reasoning_effort:
                parts.append(f"effort(req):{reasoning_effort}")
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
    turn_seconds: Optional[float] = None,
    tokens_in: Optional[int] = None,
    tokens_out: Optional[int] = None,
    cache_read_tokens: Optional[int] = None,
    cache_write_tokens: Optional[int] = None,
    token_usage_status: Optional[str] = None,
    cache_usage_status: Optional[str] = None,
    context_usage_status: Optional[str] = "reported",
    reasoning_effort: Optional[str] = None,
) -> str:
    """Top-level entry point used by gateway/run.py.

    Returns the footer text (empty string when disabled or no data).  Callers
    append this to the final response themselves, preserving a single blank
    line of separation.

    ``turn_seconds`` is the wall-clock duration of the agent run, measured by
    the caller with ``time.monotonic()``.  Callers that don't measure it leave
    it ``None`` and the ``latency`` field is skipped.
    """
    cfg = resolve_footer_config(user_config, platform_key)
    if not cfg.get("enabled"):
        return ""
    return format_runtime_footer(
        model=model,
        context_tokens=context_tokens,
        context_length=context_length,
        cwd=cwd,
        turn_seconds=turn_seconds,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
        token_usage_status=token_usage_status,
        cache_usage_status=cache_usage_status,
        context_usage_status=context_usage_status,
        reasoning_effort=reasoning_effort,
        fields=cfg.get("fields") or _DEFAULT_FIELDS,
    )
