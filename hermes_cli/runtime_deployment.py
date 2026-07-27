"""Fail-closed deployment contract for unattended objective processing."""

from __future__ import annotations

import os
import sqlite3
import time
from typing import Any, Mapping

from hermes_cli import objective_worker


HOST_ROLES = {
    "gateway": frozenset({"gateway-objective-runtime"}),
    "standalone": frozenset({"objective-runtime"}),
    "either": frozenset({"gateway-objective-runtime", "objective-runtime"}),
}


class RuntimeDeploymentError(RuntimeError):
    pass


def selected_host(charter: Mapping[str, Any]) -> str | None:
    """Return an explicit host, or None for legacy/unconfigured charters."""
    value = charter.get("runtime_host")
    if value is None:
        return None
    host = str(value).strip()
    if host not in HOST_ROLES:
        raise RuntimeDeploymentError(
            "agentic.runtime_host must be gateway, standalone, or either"
        )
    return host


def validate_worker_role(charter: Mapping[str, Any], role: str) -> None:
    host = selected_host(charter)
    if host is None:
        return
    if role not in HOST_ROLES[host]:
        raise RuntimeDeploymentError(
            f"runtime host {host!r} does not admit worker role {role!r}"
        )


def assert_current_runtime(
    conn: sqlite3.Connection,
    charter: Mapping[str, Any],
) -> None:
    """Require a live registration for this exact process and selected host."""
    host = selected_host(charter)
    if host is None:
        return
    objective_worker.ensure_schema(conn)
    from hermes_cli import organization_db

    ceo = organization_db.active_ceo(conn)
    organization_id = (
        str(ceo["organization_id"]) if ceo is not None else "__unscoped__"
    )
    roles = HOST_ROLES[host]
    stale_after = max(
        60, int(charter.get("runtime_interval_seconds", 15) or 15) * 3
    )
    placeholders = ",".join("?" for _ in roles)
    row = conn.execute(
        f"""SELECT id FROM objective_workers
             WHERE status='running' AND pid=? AND organization_id=?
               AND heartbeat_at>=?
               AND role IN ({placeholders})
             ORDER BY started_at DESC,id DESC LIMIT 1""",
        (
            os.getpid(),
            organization_id,
            int(time.time()) - stale_after,
            *sorted(roles),
        ),
    ).fetchone()
    if row is None:
        raise RuntimeDeploymentError(
            "objective cycle is not running inside the selected supervised host"
        )


def posture(
    conn: sqlite3.Connection,
    charter: Mapping[str, Any],
    *,
    stale_after_seconds: int = 60,
) -> dict[str, Any]:
    """Report configured host and observed matching worker health."""
    host = selected_host(charter)
    from hermes_cli import organization_db

    ceo = organization_db.active_ceo(conn)
    organization_id = (
        str(ceo["organization_id"]) if ceo is not None else "__unscoped__"
    )
    workers = objective_worker.worker_health(
        conn, stale_after_seconds=stale_after_seconds
    )
    roles = HOST_ROLES.get(host, frozenset())
    matching = [
        worker
        for worker in workers
        if worker["role"] in roles
        and str(worker["organization_id"]) == organization_id
    ]
    healthy = [worker for worker in matching if worker["healthy"]]
    return {
        "configured": host is not None,
        "selected_host": host,
        "expected_roles": sorted(roles),
        "ready": bool(host is not None and healthy),
        "healthy_worker_ids": [str(worker["id"]) for worker in healthy],
        "matching_workers": matching[:20],
        "worker_limit": 20,
    }
