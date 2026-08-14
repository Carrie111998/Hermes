"""Shared feature-parity runtime for embedded and standalone Kanban dispatch."""
from __future__ import annotations

import time
from typing import Any, Mapping

from hermes_cli.dispatcher_authority import require_dispatcher_lease


def _load_kanban_config() -> dict[str, Any]:
    try:
        from hermes_cli.config import load_config

        cfg = load_config()
        return dict(cfg.get("kanban", {})) if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _positive(value: object) -> int | None:
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def run_dispatcher_tick(lease, *, config: Mapping[str, Any] | None = None):
    """Enumerate every active board and perform one authorized tick on each."""
    require_dispatcher_lease(lease, "run_dispatcher_tick")
    from hermes_cli import kanban_db as kb

    cfg = dict(config) if config is not None else _load_kanban_config()
    try:
        boards = kb.list_boards(include_archived=False)
    except Exception:
        boards = [kb.read_board_metadata(kb.DEFAULT_BOARD)]
    results = []
    for entry in boards:
        slug = entry.get("slug") or kb.DEFAULT_BOARD
        conn = kb.connect(board=slug)
        try:
            results.append(
                (
                    slug,
                    kb.dispatch_once_authorized(
                        lease,
                        conn,
                        board=slug,
                        max_spawn=_positive(cfg.get("max_spawn")),
                        max_in_progress=_positive(cfg.get("max_in_progress")),
                        failure_limit=_positive(cfg.get("failure_limit"))
                        or kb.DEFAULT_SPAWN_FAILURE_LIMIT,
                        stale_timeout_seconds=int(cfg.get("dispatch_stale_timeout_seconds", 0) or 0),
                        default_assignee=(str(cfg.get("default_assignee") or "").strip() or None),
                        max_in_progress_per_profile=_positive(
                            cfg.get("max_in_progress_per_profile")
                        ),
                        reconcile_orphans=bool(cfg.get("reconcile_orphans", True)),
                    ),
                )
            )
        finally:
            conn.close()
    return results


def run_foreground_dispatcher(*, lease, interval: float = 60.0, once: bool = False) -> int:
    """Run until interrupted while retaining the validated lifetime lease."""
    require_dispatcher_lease(lease, "run_foreground_dispatcher")
    while True:
        run_dispatcher_tick(lease)
        if once:
            return 0
        try:
            time.sleep(max(1.0, interval))
        except KeyboardInterrupt:
            return 0
