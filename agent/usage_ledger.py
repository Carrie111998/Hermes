"""Local usage ledger — tracks per-request token usage in a JSONL append-only log
and exposes rolling-window aggregates (5h, 7d) for the status bar and `/usage`.

Why local-only: MiniMax does not expose a usage/billing API. Anthropic and
OpenRouter do (see `account_usage.py`), and for those providers the fetcher
returns % directly. For MiniMax (and any provider without a usage endpoint)
we accumulate from per-response `usage` objects we already have.

Storage: `~/.hermes/usage_log.jsonl`, one JSON object per request:
    {"ts": <epoch_seconds>, "model": "MiniMax-M3", "input": 1234,
     "output": 567, "cache_read": 0, "cache_write": 0, "cost_usd": 0.001}

The file is rotated automatically when it exceeds MAX_BYTES (50 MB) — oldest
lines are dropped, since we only care about the last 7 days anyway.

Public API:
    record(usage_dict)              -> None
    snapshot(limits=None)           -> LedgerSnapshot
    reset_window(window: str)       -> None
    render_text(snapshot, ...)      -> list[str]
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# Rolling-window sizes, in seconds.
WINDOW_5H = 5 * 3600
WINDOW_7D = 7 * 24 * 3600
WINDOW_24H = 24 * 3600

# Max file size before rotation. 50 MB is ~250k requests — plenty for 7 days
# of normal use; older lines get dropped.
MAX_BYTES = 50 * 1024 * 1024

# When the file has more than this many lines after rotation, trim the oldest.
MAX_LINES_AFTER_ROTATE = 100_000


def _hermes_home() -> Path:
    """Resolve HERMES_HOME with sane fallback."""
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home)
    return Path.home() / ".hermes"


def _log_path() -> Path:
    return _hermes_home() / "usage_log.jsonl"


def _now_epoch() -> float:
    return time.time()


def _rotate_if_needed(path: Path) -> None:
    """Keep the JSONL small by trimming oldest lines when it grows too big."""
    try:
        if not path.exists():
            return
        if path.stat().st_size < MAX_BYTES:
            return
        # Read all lines, keep the newest MAX_LINES_AFTER_ROTATE.
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= MAX_LINES_AFTER_ROTATE:
            return
        keep = lines[-MAX_LINES_AFTER_ROTATE:]
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            f.writelines(keep)
        tmp.replace(path)
        logger.info("usage_ledger: rotated %s down to %d lines", path, len(keep))
    except Exception as exc:  # pragma: no cover
        logger.warning("usage_ledger: rotation failed: %s", exc)


def record(usage: dict[str, Any]) -> None:
    """Append a single request's usage to the ledger.

    `usage` is the same dict shape produced by `_get_usage()` in
    tui_gateway/server.py (subset of keys is fine — we only need
    input/output/cache_read/cache_write/cost_usd/model).
    """
    try:
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": _now_epoch(),
            "model": str(usage.get("model", "") or ""),
            "input": int(usage.get("input", 0) or 0),
            "output": int(usage.get("output", 0) or 0),
            "cache_read": int(usage.get("cache_read", 0) or 0),
            "cache_write": int(usage.get("cache_write", 0) or 0),
            "cost_usd": float(usage.get("cost_usd", 0) or 0),
        }
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        _rotate_if_needed(path)
    except Exception as exc:  # pragma: no cover
        # Never let ledger I/O break a chat response.
        logger.debug("usage_ledger: record failed: %s", exc)


def _read_all() -> list[dict[str, Any]]:
    """Read all ledger lines. Skips malformed lines without raising."""
    path = _log_path()
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:  # pragma: no cover
        logger.debug("usage_ledger: read failed: %s", exc)
    return out


@dataclass(frozen=True)
class UsageWindow:
    """Aggregated usage for one rolling window."""
    label: str
    seconds: int           # window size in seconds
    total_tokens: int      # input + output (cache counted in input)
    input_tokens: int
    output_tokens: int
    cost_usd: float
    request_count: int
    limit: Optional[float] = None         # configured limit (tokens or usd)
    limit_kind: Optional[str] = None      # "tokens" | "usd"
    used_percent: Optional[float] = None  # 0..100, or None if no limit
    resets_in_s: Optional[int] = None     # seconds until window slides off
    oldest_ts: Optional[float] = None     # oldest entry in the window

    @property
    def available(self) -> bool:
        return self.request_count > 0

    @property
    def display(self) -> str:
        """Short label for the status bar: '5h: 12%' or '5h: 1.2M'."""
        if not self.available:
            return f"{self.label}: —"
        if self.used_percent is not None:
            return f"{self.label}: {self.used_percent:.0f}%"
        if self.limit_kind == "usd" and self.cost_usd:
            return f"{self.label}: ${self.cost_usd:.2f}"
        # tokens only
        if self.total_tokens >= 1_000_000:
            return f"{self.label}: {self.total_tokens / 1_000_000:.1f}M"
        if self.total_tokens >= 1_000:
            return f"{self.label}: {self.total_tokens / 1_000:.1f}K"
        return f"{self.label}: {self.total_tokens}"


@dataclass(frozen=True)
class LedgerSnapshot:
    fetched_at: float
    session_id: str
    session_tokens: int
    session_input: int
    session_output: int
    session_cost_usd: float
    five_h: UsageWindow
    seven_d: UsageWindow
    twenty_four_h: UsageWindow

    def to_payload(self) -> dict[str, Any]:
        """Compact dict for the JSON-RPC usage payload."""
        return {
            "fetched_at": self.fetched_at,
            "session": {
                "tokens": self.session_tokens,
                "input": self.session_input,
                "output": self.session_output,
                "cost_usd": round(self.session_cost_usd, 6),
            },
            "five_h": _window_to_payload(self.five_h),
            "seven_d": _window_to_payload(self.seven_d),
            "twenty_four_h": _window_to_payload(self.twenty_four_h),
        }


def _window_to_payload(w: UsageWindow) -> dict[str, Any]:
    return {
        "label": w.label,
        "seconds": w.seconds,
        "available": w.available,
        "total_tokens": w.total_tokens,
        "input_tokens": w.input_tokens,
        "output_tokens": w.output_tokens,
        "cost_usd": round(w.cost_usd, 6),
        "request_count": w.request_count,
        "limit": w.limit,
        "limit_kind": w.limit_kind,
        "used_percent": (round(w.used_percent, 1) if w.used_percent is not None else None),
        "resets_in_s": w.resets_in_s,
        "oldest_ts": w.oldest_ts,
        "display": w.display,
    }


def _aggregate(
    rows: list[dict[str, Any]],
    *,
    label: str,
    window_s: int,
    now: float,
    limit: Optional[float] = None,
    limit_kind: Optional[str] = None,
) -> UsageWindow:
    """Sum rows whose ts falls within [now - window_s, now]."""
    cutoff = now - window_s
    total = inp = out = 0
    cost = 0.0
    count = 0
    oldest: Optional[float] = None
    for r in rows:
        ts = float(r.get("ts", 0) or 0)
        if ts < cutoff:
            continue
        total += int(r.get("input", 0) or 0) + int(r.get("output", 0) or 0)
        inp += int(r.get("input", 0) or 0)
        out += int(r.get("output", 0) or 0)
        cost += float(r.get("cost_usd", 0) or 0)
        count += 1
        if oldest is None or ts < oldest:
            oldest = ts
    used_pct: Optional[float] = None
    if limit is not None and limit > 0:
        if limit_kind == "usd":
            used_pct = min(999.0, (cost / limit) * 100.0)
        else:  # tokens (default)
            used_pct = min(999.0, (total / limit) * 100.0)
    resets_in: Optional[int] = None
    if oldest is not None:
        resets_in = max(0, int((oldest + window_s) - now))
    return UsageWindow(
        label=label,
        seconds=window_s,
        total_tokens=total,
        input_tokens=inp,
        output_tokens=out,
        cost_usd=cost,
        request_count=count,
        limit=limit,
        limit_kind=limit_kind,
        used_percent=used_pct,
        resets_in_s=resets_in,
        oldest_ts=oldest,
    )


def snapshot(
    session: Optional[dict[str, Any]] = None,
    *,
    limits: Optional[dict[str, Any]] = None,
) -> LedgerSnapshot:
    """Build a fresh snapshot.

    Args:
        session: current-session counters (from `_get_usage()`).
        limits: config-driven limits, e.g.
            {"5h": {"tokens": 1_000_000}, "7d": {"usd": 5.0}}
            Either of the keys may be missing. If both tokens and usd are
            given for one window, tokens wins (we render % when available).
    """
    now = _now_epoch()
    rows = _read_all()
    limits = limits or {}
    l5 = (limits.get("5h") or {})
    l7 = (limits.get("7d") or {})
    l24 = (limits.get("24h") or {})

    def _limit_tuple(d: dict[str, Any]) -> tuple[Optional[float], Optional[str]]:
        if "tokens" in d and d["tokens"]:
            return (float(d["tokens"]), "tokens")
        if "usd" in d and d["usd"]:
            return (float(d["usd"]), "usd")
        return (None, None)

    lim5, kind5 = _limit_tuple(l5)
    lim7, kind7 = _limit_tuple(l7)
    lim24, kind24 = _limit_tuple(l24)

    session = session or {}
    return LedgerSnapshot(
        fetched_at=now,
        session_id=str(session.get("session_id", "") or ""),
        session_tokens=int(session.get("total", 0) or 0),
        session_input=int(session.get("input", 0) or 0),
        session_output=int(session.get("output", 0) or 0),
        session_cost_usd=float(session.get("cost_usd", 0) or 0),
        five_h=_aggregate(rows, label="5h", window_s=WINDOW_5H, now=now,
                          limit=lim5, limit_kind=kind5),
        seven_d=_aggregate(rows, label="7d", window_s=WINDOW_7D, now=now,
                           limit=lim7, limit_kind=kind7),
        twenty_four_h=_aggregate(rows, label="24h", window_s=WINDOW_24H, now=now,
                                 limit=lim24, limit_kind=kind24),
    )


def render_text(snap: LedgerSnapshot, *, markdown: bool = False) -> list[str]:
    """Pretty-print for the `/usage` slash command / TUI panel."""
    bold = lambda s: f"**{s}**" if markdown else s
    lines = [f"📊 {bold('Local usage ledger')}"]
    if snap.session_tokens or snap.session_cost_usd:
        lines.append(
            f"Session: {snap.session_tokens} tok "
            f"(in {snap.session_input} / out {snap.session_output})"
        )
        if snap.session_cost_usd:
            lines.append(f"Session cost: ${snap.session_cost_usd:.4f}")
    for w in (snap.twenty_four_h, snap.five_h, snap.seven_d):
        if not w.available:
            continue
        suffix = ""
        if w.used_percent is not None:
            suffix = f"  ·  {w.used_percent:.0f}% of limit"
        cost = f"  ·  ${w.cost_usd:.4f}" if w.cost_usd else ""
        lines.append(
            f"{w.label}: {w.total_tokens} tok · {w.request_count} req{cost}{suffix}"
        )
        if w.resets_in_s is not None and w.oldest_ts is not None:
            mins = w.resets_in_s // 60
            if mins >= 60:
                hh, mm = divmod(mins, 60)
                rel = f"{hh}h {mm}m"
            else:
                rel = f"{mins}m"
            lines.append(f"  window oldest entry slides off in {rel}")
    return lines


def _load_limits_from_config(config: dict[str, Any]) -> dict[str, Any]:
    """Read `usage.limits` block from the Hermes config.yaml.

    Expected shape (all optional, all numbers):
        usage:
          limits:
            5h:  {tokens: 1_000_000}      # OR
                 {usd: 2.0}
            7d:  {tokens: 10_000_000}     # OR
                 {usd: 10.0}
            24h: {tokens: 5_000_000}
    """
    try:
        block = (config.get("usage") or {}).get("limits") or {}
        out: dict[str, Any] = {}
        for window_key in ("5h", "7d", "24h"):
            entry = block.get(window_key) or {}
            if not isinstance(entry, dict):
                continue
            t: dict[str, Any] = {}
            if "tokens" in entry and entry["tokens"] is not None:
                t["tokens"] = float(entry["tokens"])
            if "usd" in entry and entry["usd"] is not None:
                t["usd"] = float(entry["usd"])
            if t:
                out[window_key] = t
        return out
    except Exception:
        return {}
