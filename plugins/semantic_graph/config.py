"""Runtime configuration for the semantic-graph plugin."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

from .models import DEFAULT_TOOLS

logger = logging.getLogger("hermes.plugins.semantic_graph")

PLUGIN_ID = "semantic-graph"
DB_FILENAME = "semantic_graph.db"
_AUTO_EXTRACT_ALLOWED = frozenset({"off", "explicit", "all"})

_warn_lock = threading.Lock()
_auto_extract_warned = False


@dataclass(frozen=True)
class SemanticGraphConfig:
    db_subdir: str = "semantic-graph"
    capture_turns: bool = True
    capture_tool_events: bool = False
    capture_subagents: bool = True
    auto_extract: str = "explicit"
    retrieval_enabled: bool = True
    retrieval_top_k: int = 8
    retrieval_max_chars: int = 3500
    min_recall_confidence: float = 0.60
    max_artifact_chars: int = 12000
    tool_result_preview_chars: int = 1000
    retention_days: int = 365
    recall_statuses: tuple[str, ...] = ("asserted", "accepted")
    full_tool_result_allowlist: frozenset[str] = field(default_factory=frozenset)
    tool_capture_denylist: frozenset[str] = field(
        default_factory=lambda: frozenset(DEFAULT_TOOLS)
    )

    def db_path(self) -> Path:
        return get_hermes_home() / self.db_subdir / DB_FILENAME

    def export_root(self) -> Path:
        return get_hermes_home() / self.db_subdir / "exports"


def _warn_auto_extract_once(raw: str) -> None:
    global _auto_extract_warned
    with _warn_lock:
        if _auto_extract_warned:
            return
        _auto_extract_warned = True
    logger.warning(
        "semantic-graph: unknown auto_extract=%r; falling back to 'explicit'",
        raw,
    )


def _coerce_bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if value is None:
        return default
    return bool(value)


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _raw_plugin_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config_readonly

        cfg = load_config_readonly() or {}
        entries = (cfg.get("plugins") or {}).get("entries") or {}
        entry = entries.get(PLUGIN_ID) or entries.get("semantic_graph") or {}
        if isinstance(entry, dict):
            nested = entry.get("config")
            return dict(nested) if isinstance(nested, dict) else dict(entry)
    except Exception:
        logger.debug("semantic-graph: config load failed; using defaults", exc_info=True)
    return {}


def load_config(overrides: dict[str, Any] | None = None) -> SemanticGraphConfig:
    raw = _raw_plugin_config()
    if overrides:
        raw = {**raw, **overrides}

    auto_raw = str(raw.get("auto_extract", "explicit") or "explicit").strip().lower()
    if auto_raw not in _AUTO_EXTRACT_ALLOWED:
        _warn_auto_extract_once(auto_raw)
        auto_raw = "explicit"

    recall = raw.get("recall_statuses") or ["asserted", "accepted"]
    if not isinstance(recall, (list, tuple)):
        recall = ["asserted", "accepted"]

    denylist = raw.get("tool_capture_denylist")
    if not isinstance(denylist, (list, tuple)) or not denylist:
        denylist = list(DEFAULT_TOOLS)
    # Always deny own tools to prevent recursive capture.
    denylist = set(str(x) for x in denylist) | set(DEFAULT_TOOLS)

    allowlist = raw.get("full_tool_result_allowlist") or []
    if not isinstance(allowlist, (list, tuple)):
        allowlist = []

    return SemanticGraphConfig(
        db_subdir=str(raw.get("db_subdir") or "semantic-graph"),
        capture_turns=_coerce_bool(raw.get("capture_turns"), True),
        capture_tool_events=_coerce_bool(raw.get("capture_tool_events"), False),
        capture_subagents=_coerce_bool(raw.get("capture_subagents"), True),
        auto_extract=auto_raw,
        retrieval_enabled=_coerce_bool(raw.get("retrieval_enabled"), True),
        retrieval_top_k=max(1, min(20, _coerce_int(raw.get("retrieval_top_k"), 8))),
        retrieval_max_chars=max(200, _coerce_int(raw.get("retrieval_max_chars"), 3500)),
        min_recall_confidence=max(
            0.0, min(1.0, _coerce_float(raw.get("min_recall_confidence"), 0.60))
        ),
        max_artifact_chars=max(500, _coerce_int(raw.get("max_artifact_chars"), 12000)),
        tool_result_preview_chars=max(
            64, _coerce_int(raw.get("tool_result_preview_chars"), 1000)
        ),
        retention_days=max(0, _coerce_int(raw.get("retention_days"), 365)),
        recall_statuses=tuple(str(x) for x in recall),
        full_tool_result_allowlist=frozenset(str(x) for x in allowlist),
        tool_capture_denylist=frozenset(denylist),
    )
