"""Signed, read-only verification for a freshly completed cutover."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shlex
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from hermes_constants import get_default_hermes_root
from hermes_cli.cost.config import GLOBAL_DAILY_CAP_AUD
from hermes_cli.cutover.rehearse import rehearse_cutover
from hermes_cli.lanes.dry_run import run_lane_dry_run
from hermes_cli.lanes.manifest import (
    load_manifest as load_lane_manifest,
)
from hermes_cli.routing import route_context
from hermes_cli.routing.bootstrap import DEFAULT_DOCTRINE_V1_PATH
from hermes_cli.service.manifest import (
    load_manifest as load_service_manifest,
)
from hermes_cli.smoke.roundtrip import (
    _remove_temp_copy,
    run_smoke_turn,
)


_MELBOURNE = ZoneInfo("Australia/Melbourne")
_EXPECTED_SERVICE_IDS = (
    "dashboard",
    "hermes_app",
    "hermes_server",
    "blender_mcp_watchdog_server",
    "tui_slash_worker",
    "blender_mcp_watchdog_tui",
    "atlas_gateway",
    "blender_mcp_watchdog_gateway",
)
_DOCTRINE_LANES = ("dayroute", "green_captains", "tihna")


@dataclass(frozen=True)
class VerificationCheck:
    """One independently actionable post-cutover assertion."""

    check_id: str
    label: str
    status: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


@dataclass(frozen=True)
class CutoverVerificationReport:
    """Serializable and content-signed post-cutover evidence."""

    verification_timestamp: str
    restart_not_before: str
    processes: list[dict[str, Any]]
    code_freshness: dict[str, Any]
    programme: dict[str, Any]
    programme_status: str
    smoke_dry_run: dict[str, Any]
    lane_dry_run: dict[str, Any]
    cutover_rehearsal: dict[str, Any]
    lane_manifest_audit: dict[str, Any]
    route_context: dict[str, Any]
    kill_switch: dict[str, Any]
    doctrine: dict[str, Any]
    cost_cap: dict[str, Any]
    checks: tuple[VerificationCheck, ...]
    overall_verdict: str
    recommended_next_action: str
    sha256: str

    @property
    def exit_code(self) -> int:
        return 0 if self.overall_verdict == "HEALTHY" else 1

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["checks"] = [asdict(item) for item in self.checks]
        return value

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )

    def to_markdown(self) -> str:
        lines = [
            "# Hermes Post-Cutover Verification",
            "",
            f"- Verification timestamp: `{self.verification_timestamp}`",
            f"- Restart not before: `{self.restart_not_before}`",
            f"- Overall verdict: **{self.overall_verdict}**",
            f"- Recommended next action: `{self.recommended_next_action}`",
            f"- Content SHA-256: `{self.sha256}`",
            "",
            "## Checks",
            "",
            "| ID | Status | Check | Detail |",
            "|---|---|---|---|",
        ]
        for check in self.checks:
            detail = check.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {check.check_id} | {check.status} | "
                f"{check.label} | {detail} |"
            )
        sections = (
            ("Processes", self.processes),
            ("Code freshness", self.code_freshness),
            (
                "Programme",
                {
                    "row": self.programme,
                    "status": self.programme_status,
                },
            ),
            ("CS-13 smoke dry-run", self.smoke_dry_run),
            ("CS-16 lane dry-run", self.lane_dry_run),
            ("CS-19 cutover rehearsal", self.cutover_rehearsal),
            ("Lane manifest audit", self.lane_manifest_audit),
            ("Synthetic route context", self.route_context),
            ("Kill switch", self.kill_switch),
            ("Doctrine drift", self.doctrine),
            ("Cost cap", self.cost_cap),
        )
        for heading, value in sections:
            lines.extend(
                [
                    "",
                    f"## {heading}",
                    "",
                    "```json",
                    json.dumps(
                        value,
                        ensure_ascii=False,
                        indent=2,
                        sort_keys=True,
                    ),
                    "```",
                ]
            )
        lines.extend(
            [
                "",
                "## Decision",
                "",
                f"**{self.overall_verdict}**. This command performed no "
                "restart, programme transition, production manifest "
                "mutation, production database write, lane enablement, "
                "publishing change, or real network/model/message call.",
                "",
            ]
        )
        return "\n".join(lines)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _aware_datetime(
    value: str | datetime | None,
    *,
    now: datetime,
) -> datetime:
    if value is None:
        return now - timedelta(minutes=30)
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise ValueError(
                "--restart-not-before must be an ISO-8601 timestamp"
            ) from exc
    if parsed.tzinfo is None:
        raise ValueError(
            "--restart-not-before must include a UTC offset or Z"
        )
    return parsed.astimezone(timezone.utc)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(
        f"file:{path}?mode=ro&immutable=1",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        is not None
    )


def _normalize(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    method = getattr(value, "to_dict", None)
    if callable(method):
        return dict(method())
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(
        f"unsupported cutover verification result: {type(value).__name__}"
    )


def _parse_ps_start(value: str) -> datetime | None:
    try:
        local = datetime.strptime(
            value,
            "%a %b %d %H:%M:%S %Y",
        ).replace(tzinfo=_MELBOURNE)
    except (TypeError, ValueError):
        return None
    return local.astimezone(timezone.utc)


def _resolve_entrypoint(command: str) -> tuple[str | None, str | None]:
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    if not parts:
        return None, None
    if "-m" in parts:
        index = parts.index("-m")
        if index + 1 >= len(parts):
            return None, None
        module = parts[index + 1]
        try:
            spec = importlib.util.find_spec(module)
        except (ImportError, ModuleNotFoundError, ValueError):
            spec = None
        return module, str(spec.origin) if spec and spec.origin else None
    executable = Path(parts[0]).expanduser()
    if "python" in executable.name.lower() and len(parts) > 1:
        candidate = parts[1]
        if not candidate.startswith("-"):
            path = Path(candidate).expanduser()
            return str(path), str(path) if path.is_absolute() else None
    return str(executable), str(executable) if executable.is_absolute() else None


def _snapshot_processes(
    manifest,
    *,
    now: datetime,
) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for service in manifest.services:
        pid: int | None = None
        try:
            pid = int(service.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, TypeError, ValueError):
            pass
        raw_start = None
        command = None
        alive = False
        if pid is not None and pid > 0:
            result = subprocess.run(
                [
                    "ps",
                    "-p",
                    str(pid),
                    "-o",
                    "lstart=",
                    "-o",
                    "command=",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            text = result.stdout.strip()
            alive = result.returncode == 0 and bool(text)
            if text:
                parts = text.split(None, 5)
                if len(parts) >= 5:
                    raw_start = " ".join(parts[:5])
                    command = parts[5] if len(parts) == 6 else ""
        started = _parse_ps_start(raw_start) if raw_start else None
        entrypoint, module_path = _resolve_entrypoint(command or "")
        age_seconds = (
            max(0.0, (now - started).total_seconds())
            if started is not None
            else None
        )
        processes.append(
            {
                "service_id": service.id,
                "expected_name": service.name,
                "pid_file": str(service.pid_file),
                "pid": pid,
                "alive": alive,
                "start_time": _iso(started) if started else None,
                "age_minutes": (
                    round(age_seconds / 60.0, 2)
                    if age_seconds is not None
                    else None
                ),
                "command": command,
                "entrypoint": entrypoint,
                "module_path": module_path,
            }
        )
    return processes


def _process_checks(
    processes: list[dict[str, Any]],
    *,
    restart_not_before: datetime,
) -> tuple[VerificationCheck, VerificationCheck]:
    by_id = {
        str(item.get("service_id")): item
        for item in processes
    }
    missing = [
        service_id
        for service_id in _EXPECTED_SERVICE_IDS
        if service_id not in by_id or not by_id[service_id].get("alive")
    ]
    presence = VerificationCheck(
        check_id="PROCESSES_PRESENT",
        label="All eight protected processes are running",
        status="PASS" if not missing else "FAIL",
        detail=(
            "all expected services alive"
            if not missing
            else f"missing or dead: {missing}"
        ),
    )
    stale: list[str] = []
    for service_id in _EXPECTED_SERVICE_IDS:
        item = by_id.get(service_id)
        raw = item.get("start_time") if item else None
        try:
            started = _aware_datetime(raw, now=restart_not_before)
        except (TypeError, ValueError):
            started = None
        if started is None or started <= restart_not_before:
            stale.append(service_id)
    fresh = VerificationCheck(
        check_id="PROCESSES_FRESH",
        label="All protected processes started after the cutover threshold",
        status="PASS" if not stale else "FAIL",
        detail=(
            f"all process starts are after {_iso(restart_not_before)}"
            if not stale
            else (
                f"not after {_iso(restart_not_before)}: {stale}"
            )
        ),
    )
    return presence, fresh


def _code_freshness(
    processes: list[dict[str, Any]],
    *,
    code_files: Mapping[str, Path],
) -> tuple[dict[str, Any], VerificationCheck]:
    starts: dict[str, datetime] = {}
    unresolved_entrypoints: list[str] = []
    for item in processes:
        service_id = str(item.get("service_id"))
        raw = item.get("start_time")
        if raw:
            try:
                starts[service_id] = _aware_datetime(
                    str(raw),
                    now=datetime.now(timezone.utc),
                )
            except ValueError:
                pass
        if not item.get("module_path"):
            unresolved_entrypoints.append(service_id)
    files: list[dict[str, Any]] = []
    violations: list[str] = []
    for label, path in code_files.items():
        exists = path.is_file()
        modified = (
            datetime.fromtimestamp(
                path.stat().st_mtime,
                tz=timezone.utc,
            )
            if exists
            else None
        )
        newer_than = [
            service_id
            for service_id, started in starts.items()
            if modified is not None and modified >= started
        ]
        if not exists:
            violations.append(f"{label}: missing")
        elif newer_than:
            violations.append(
                f"{label}: newer than {','.join(newer_than)}"
            )
        files.append(
            {
                "label": label,
                "path": str(path),
                "exists": exists,
                "mtime": _iso(modified) if modified else None,
                "newer_than_processes": newer_than,
            }
        )
    if len(starts) != len(_EXPECTED_SERVICE_IDS):
        violations.append(
            f"only {len(starts)}/{len(_EXPECTED_SERVICE_IDS)} "
            "process start times resolved"
        )
    if unresolved_entrypoints:
        violations.append(
            f"unresolved entrypoints: {unresolved_entrypoints}"
        )
    result = {
        "files": files,
        "resolved_process_start_times": len(starts),
        "unresolved_entrypoints": unresolved_entrypoints,
        "violations": violations,
    }
    check = VerificationCheck(
        check_id="CODE_LOADED",
        label="Processes started after all key CS code was written",
        status="PASS" if not violations else "FAIL",
        detail=(
            "all key file mtimes predate every process start"
            if not violations
            else "; ".join(violations)
        ),
    )
    return result, check


def _day_bounds(now: datetime) -> tuple[datetime, datetime]:
    local = now.astimezone(_MELBOURNE)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _database_snapshot(
    db_path: Path,
    *,
    now: datetime,
    lane_manifest,
) -> tuple[
    dict[str, Any],
    str,
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
]:
    connection = _read_only_connection(db_path)
    try:
        programme = (
            dict(
                connection.execute(
                    "SELECT * FROM programme_state WHERE id=1"
                ).fetchone()
            )
            if _table_exists(connection, "programme_state")
            else {}
        )
        state = str(programme.get("state") or "UNKNOWN")
        if state == "PAUSED":
            programme_status = (
                "PAUSED — resume with 'hermes programme resume ...' "
                "before enabling lanes"
            )
        elif state == "RUNNING":
            programme_status = (
                f"RUNNING — resumed/changed at "
                f"{programme.get('changed_at')}"
            )
        else:
            programme_status = f"{state} — investigate before enabling lanes"

        cutoff = _iso(now - timedelta(hours=24))
        audit_exists = _table_exists(
            connection,
            "lane_manifest_audit",
        )
        audit_rows = (
            [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT id, lane_id, action, previous_value, new_value,
                           actor, timestamp_utc, notes
                      FROM lane_manifest_audit
                     WHERE timestamp_utc >= ?
                     ORDER BY timestamp_utc DESC, id DESC
                    """,
                    (cutoff,),
                )
            ]
            if audit_exists
            else []
        )
        total_audit_rows = (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM lane_manifest_audit"
                ).fetchone()[0]
            )
            if audit_exists
            else 0
        )
        enabled_lanes = [
            {
                "lane_id": lane.lane_id,
                "enabled": lane.enabled,
                "publish_enabled": lane.publish_enabled,
                "most_recent_audit": next(
                    (
                        row
                        for row in audit_rows
                        if row["lane_id"] == lane.lane_id
                    ),
                    None,
                ),
            }
            for lane in lane_manifest.lanes
            if lane.enabled
        ]
        audit = {
            "table_exists": audit_exists,
            "total_rows": total_audit_rows,
            "window_start_utc": cutoff,
            "rows_last_24h": audit_rows,
            "enabled_lanes": enabled_lanes,
        }

        killed = (
            [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM task_kill_switch ORDER BY killed_ts"
                )
            ]
            if _table_exists(connection, "task_kill_switch")
            else []
        )
        kill_switch = {
            "active_rows": len(killed),
            "entries": killed,
        }

        doctrine_rows: list[dict[str, Any]] = []
        if _table_exists(connection, "routing_doctrine"):
            doctrine_rows = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT lane, rung, complexity, primary_provider,
                           primary_model, version, priority
                      FROM routing_doctrine
                     WHERE lane IN ('dayroute', 'green_captains', 'tihna')
                       AND version = (
                           SELECT active_version
                             FROM routing_doctrine_meta
                            WHERE singleton=1
                       )
                     ORDER BY lane, priority DESC, id
                    """
                )
            ]

        day_start, day_end = _day_bounds(now)
        start_utc = _iso(day_start)
        end_utc = _iso(day_end)
        gross = 0.0
        billable = 0.0
        rows = 0
        if _table_exists(connection, "cost_ledger"):
            cost_row = connection.execute(
                """
                SELECT COUNT(*) AS rows,
                       COALESCE(SUM(aud_amount), 0.0) AS gross,
                       COALESCE(SUM(
                         CASE
                           WHEN COALESCE(is_free_tier, 0) = 0
                            AND COALESCE(is_subscription_bridge, 0) = 0
                           THEN aud_amount ELSE 0
                         END
                       ), 0.0) AS billable
                  FROM cost_ledger
                 WHERE ts >= ? AND ts < ?
                """,
                (start_utc, end_utc),
            ).fetchone()
            rows = int(cost_row["rows"])
            gross = float(cost_row["gross"])
            billable = float(cost_row["billable"])
        cap = float(GLOBAL_DAILY_CAP_AUD)
        cost = {
            "timezone": "Australia/Melbourne",
            "window_start_utc": start_utc,
            "window_end_utc": end_utc,
            "row_count": rows,
            "gross_aud": round(gross, 8),
            "billable_aud": round(billable, 8),
            "daily_cap_aud": cap,
            "remaining_aud": round(max(0.0, cap - billable), 8),
            "within_10_percent": billable >= cap * 0.9,
        }
        return (
            programme,
            programme_status,
            audit,
            kill_switch,
            doctrine_rows,
            cost,
        )
    finally:
        connection.close()


def _doctrine_snapshot(
    rows: list[dict[str, Any]],
    *,
    seed_path: Path,
) -> tuple[dict[str, Any], VerificationCheck]:
    try:
        seed_payload = json.loads(seed_path.read_text(encoding="utf-8"))
        seed_rules = list(seed_payload.get("rules") or [])
    except (OSError, json.JSONDecodeError, AttributeError) as exc:
        seed_rules = []
        seed_error = f"{type(exc).__name__}: {exc}"
    else:
        seed_error = None
    expected = {
        str(rule.get("lane")): {
            "primary_provider": rule.get("primary_provider"),
            "primary_model": rule.get("primary_model"),
        }
        for rule in seed_rules
        if rule.get("lane") in _DOCTRINE_LANES
    }
    observed: dict[str, dict[str, Any]] = {}
    for row in rows:
        lane = str(row.get("lane"))
        if lane in _DOCTRINE_LANES and lane not in observed:
            observed[lane] = {
                "primary_provider": row.get("primary_provider"),
                "primary_model": row.get("primary_model"),
                "version": row.get("version"),
            }
    drift = [
        lane
        for lane in _DOCTRINE_LANES
        if expected.get(lane, {}).get("primary_provider")
        != observed.get(lane, {}).get("primary_provider")
        or expected.get(lane, {}).get("primary_model")
        != observed.get(lane, {}).get("primary_model")
        or observed.get(lane, {}).get("primary_provider")
        != "openai-codex"
        or observed.get(lane, {}).get("primary_model") != "gpt-5-6-sol"
    ]
    if seed_error:
        drift.append("seed_unreadable")
    result = {
        "seed_path": str(seed_path),
        "seed_error": seed_error,
        "expected": expected,
        "observed": observed,
        "drifted_lanes": drift,
    }
    check = VerificationCheck(
        check_id="DOCTRINE_DRIFT",
        label="Active lane doctrine matches the version-one seed",
        status="PASS" if not drift else "FAIL",
        detail=(
            "dayroute, green_captains, and tihna match "
            "openai-codex/gpt-5-6-sol"
            if not drift
            else f"drift or missing seed rows: {drift}"
        ),
    )
    return result, check


def _synthetic_route_context_check() -> dict[str, Any]:
    key = "HERMES_ROUTE_CONTEXT_JSON"
    previous = os.environ.get(key)
    payload = {
        "schema_version": 1,
        "decision_row_id": 21,
        "fallback_chain": [
            {
                "provider": "openrouter",
                "model": "synthetic-route-context",
            }
        ],
        "task_id": "cs21-synthetic",
    }
    try:
        route_context._reset_for_tests()
        os.environ[key] = json.dumps(payload, sort_keys=True)
        observed = route_context.get_route_context()
        cleared = key not in os.environ
        second_read = route_context.get_route_context()
        success = (
            observed == payload
            and second_read == payload
            and cleared
        )
        return {
            "success": success,
            "payload": payload,
            "observed": observed,
            "second_read_matches_cached": second_read == payload,
            "env_cleared_after_read": cleared,
        }
    finally:
        route_context._reset_for_tests()
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _smoke_snapshot(
    runner: Callable[..., Any],
    *,
    db_path: Path,
) -> tuple[dict[str, Any], VerificationCheck]:
    smoke_value = runner(
        scenario="success",
        lane="default",
        db_path=db_path,
        commit=False,
    )
    smoke = _normalize(smoke_value)
    try:
        stages = list(smoke.get("stages") or [])
        route_stage = next(
            (
                stage
                for stage in stages
                if stage.get("name") == "route_for_turn"
            ),
            {},
        )
        cost_stage = next(
            (
                stage
                for stage in stages
                if stage.get("name") == "cost_ledger"
            ),
            {},
        )
        route_details = dict(route_stage.get("details") or {})
        cost_details = dict(cost_stage.get("details") or {})
        healthy = (
            smoke.get("overall") == "PASS"
            and smoke.get("commit") is False
            and route_stage.get("outcome") == "success"
            and bool(route_details.get("chosen_provider"))
            and bool(route_details.get("chosen_model"))
            and cost_stage.get("outcome") == "success"
            and cost_details.get("aud") is not None
        )
        summary = {
            "overall": smoke.get("overall"),
            "commit": smoke.get("commit"),
            "route_outcome": route_stage.get("outcome"),
            "chosen_provider": route_details.get("chosen_provider"),
            "chosen_model": route_details.get("chosen_model"),
            "estimated_cost_aud": cost_details.get("aud"),
            "source_db_path": smoke.get("source_db_path"),
            "working_db_path": smoke.get("working_db_path"),
            "errors": smoke.get("errors") or [],
            "healthy_round_trip": healthy,
        }
        check = VerificationCheck(
            check_id="SMOKE_DRY_RUN",
            label="CS-13 doctrine smoke resolves without a live write",
            status="PASS" if healthy else "FAIL",
            detail=(
                f"provider={summary['chosen_provider']}; "
                f"model={summary['chosen_model']}; "
                f"estimated_aud={summary['estimated_cost_aud']}"
                if healthy
                else f"unhealthy smoke result: {summary}"
            ),
        )
        return summary, check
    finally:
        if smoke.get("commit") is False:
            _remove_temp_copy(smoke.get("working_db_path"))


def _lane_dry_snapshot(
    runner: Callable[..., Any],
    *,
    lane_manifest_path: Path,
    db_path: Path,
) -> tuple[dict[str, Any], VerificationCheck]:
    value = runner(
        "tihna",
        stage="full",
        manifest_path=lane_manifest_path,
        db_path=db_path,
    )
    result = _normalize(value)
    summary = {
        "lane_id": result.get("lane_id"),
        "stage": result.get("stage"),
        "success": result.get("success"),
        "ingested": result.get("ingested"),
        "classified": result.get("classified"),
        "drafted": result.get("drafted"),
        "error": result.get("error"),
        "fixture_feed_used": result.get("fixture_feed_used"),
        "fake_llm_used": result.get("fake_llm_used"),
        "kanban_writes": result.get("kanban_writes"),
        "cost_ledger_writes": result.get("cost_ledger_writes"),
        "side_effect_writes": result.get("side_effect_writes"),
    }
    healthy = bool(result.get("success")) and not result.get("error")
    check = VerificationCheck(
        check_id="LANE_DRY_RUN",
        label="CS-16 Tihna full fixture dry-run succeeds",
        status="PASS" if healthy else "FAIL",
        detail=(
            f"ingested={summary['ingested']}; "
            f"classified={summary['classified']}; "
            f"drafted={summary['drafted']}"
            if healthy
            else f"lane dry-run failed: {summary['error']}"
        ),
    )
    return summary, check


def _rehearsal_snapshot(
    runner: Callable[..., Any],
    *,
    db_path: Path,
    lane_manifest_path: Path,
    service_manifest_path: Path,
    repo_root: Path,
) -> tuple[dict[str, Any], VerificationCheck]:
    value = runner(
        db_path=db_path,
        lane_manifest_path=lane_manifest_path,
        service_manifest_path=service_manifest_path,
        repo_root=repo_root,
    )
    result = _normalize(value)
    preconditions = list(result.get("preconditions") or [])
    failures = [
        item
        for item in preconditions
        if item.get("status") != "PASS"
    ]
    healthy = (
        result.get("go_no_go") == "GO"
        and len(preconditions) == 17
        and not failures
    )
    summary = {
        "go_no_go": result.get("go_no_go"),
        "sha256": result.get("sha256"),
        "precondition_count": len(preconditions),
        "precondition_failures": failures,
        "requires_operator_unlock": result.get(
            "requires_operator_unlock"
        ),
        "would_execute": (
            result.get("restart_plan") or {}
        ).get("would_execute"),
    }
    check = VerificationCheck(
        check_id="CUTOVER_REHEARSAL",
        label="CS-19 rehearsal remains GO with 17 passing checks",
        status="PASS" if healthy else "FAIL",
        detail=(
            f"GO; 17/17 PASS; sha256={summary['sha256']}"
            if healthy
            else (
                f"decision={summary['go_no_go']}; "
                f"count={summary['precondition_count']}; "
                f"failures={len(failures)}"
            )
        ),
    )
    return summary, check


def _check(
    check_id: str,
    label: str,
    passed: bool,
    detail: str,
) -> VerificationCheck:
    return VerificationCheck(
        check_id=check_id,
        label=label,
        status="PASS" if passed else "FAIL",
        detail=detail,
    )


def _signature_payload(values: dict[str, Any]) -> bytes:
    return json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def verify_cutover(
    *,
    restart_not_before: str | datetime | None = None,
    db_path: str | Path | None = None,
    lane_manifest_path: str | Path | None = None,
    service_manifest_path: str | Path | None = None,
    doctrine_seed_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    now: datetime | None = None,
    process_snapshotter: Callable[..., list[dict[str, Any]]] = (
        _snapshot_processes
    ),
    smoke_runner: Callable[..., Any] = run_smoke_turn,
    lane_dry_runner: Callable[..., Any] = run_lane_dry_run,
    rehearsal_runner: Callable[..., Any] = rehearse_cutover,
    route_context_checker: Callable[[], dict[str, Any]] = (
        _synthetic_route_context_check
    ),
    key_code_files: Mapping[str, str | Path] | None = None,
) -> CutoverVerificationReport:
    """Build a signed report without mutating production state."""
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    instant = instant.astimezone(timezone.utc)
    threshold = _aware_datetime(restart_not_before, now=instant)
    root = get_default_hermes_root()
    source_db = Path(db_path or root / "kanban.db").expanduser()
    source_lane_manifest = Path(
        lane_manifest_path or root / "lane_manifest.yaml"
    ).expanduser()
    source_service_manifest = Path(
        service_manifest_path or root / "service_manifest.yaml"
    ).expanduser()
    source_seed = Path(
        doctrine_seed_path or DEFAULT_DOCTRINE_V1_PATH
    ).expanduser()
    source_repo = Path(repo_root or Path(__file__).parents[2]).expanduser()
    code_files = {
        "CS-01c ingress gate (run_agent.py:6591)": (
            source_repo / "run_agent.py"
        ),
        "CS-10b-rev route flush (conversation_loop.py:6850)": (
            source_repo / "agent/conversation_loop.py"
        ),
        "CS-15 Tihna lane module": (
            source_repo / "hermes_cli/lanes/impls/tihna.py"
        ),
    }
    if key_code_files is not None:
        code_files = {
            str(label): Path(path).expanduser()
            for label, path in key_code_files.items()
        }

    service_manifest = load_service_manifest(
        source_service_manifest,
        db_path=source_db,
        record_state=False,
    )
    lane_manifest = load_lane_manifest(
        source_lane_manifest,
        db_path=source_db,
        record_state=False,
    )
    processes = process_snapshotter(service_manifest, now=instant)
    presence_check, freshness_check = _process_checks(
        processes,
        restart_not_before=threshold,
    )
    code_freshness, code_check = _code_freshness(
        processes,
        code_files=code_files,
    )
    (
        programme,
        programme_status,
        audit,
        kill_switch,
        doctrine_rows,
        cost_cap,
    ) = _database_snapshot(
        source_db,
        now=instant,
        lane_manifest=lane_manifest,
    )
    doctrine, doctrine_check = _doctrine_snapshot(
        doctrine_rows,
        seed_path=source_seed,
    )
    smoke, smoke_check = _smoke_snapshot(
        smoke_runner,
        db_path=source_db,
    )
    lane_dry, lane_check = _lane_dry_snapshot(
        lane_dry_runner,
        lane_manifest_path=source_lane_manifest,
        db_path=source_db,
    )
    rehearsal, rehearsal_check = _rehearsal_snapshot(
        rehearsal_runner,
        db_path=source_db,
        lane_manifest_path=source_lane_manifest,
        service_manifest_path=source_service_manifest,
        repo_root=source_repo,
    )
    route_context_result = route_context_checker()
    route_check = _check(
        "ROUTE_CONTEXT",
        "One-shot HERMES_ROUTE_CONTEXT_JSON propagation is intact",
        bool(route_context_result.get("success")),
        (
            "synthetic payload round-tripped and environment was cleared"
            if route_context_result.get("success")
            else f"route-context result: {route_context_result}"
        ),
    )
    programme_check = _check(
        "PROGRAMME_STATE",
        "Programme state is safe to inspect before lane enablement",
        programme.get("state") in {"PAUSED", "RUNNING"},
        programme_status,
    )
    audit_check = _check(
        "LANE_AUDIT",
        "Lane manifest audit table is readable",
        bool(audit.get("table_exists")),
        (
            f"{audit.get('total_rows')} total rows; "
            f"{len(audit.get('rows_last_24h') or [])} in last 24h; "
            f"enabled lanes={len(audit.get('enabled_lanes') or [])}"
        ),
    )
    kill_check = _check(
        "KILL_SWITCH",
        "No task kill switch is active",
        int(kill_switch.get("active_rows") or 0) == 0,
        f"active_rows={kill_switch.get('active_rows')}",
    )
    cost_check = _check(
        "COST_CAP",
        "Billable Melbourne-day spend is below 90% of cap",
        not bool(cost_cap.get("within_10_percent")),
        (
            f"billable_aud={cost_cap.get('billable_aud')}; "
            f"cap_aud={cost_cap.get('daily_cap_aud')}; "
            f"remaining_aud={cost_cap.get('remaining_aud')}"
        ),
    )
    checks = (
        presence_check,
        freshness_check,
        code_check,
        programme_check,
        smoke_check,
        lane_check,
        rehearsal_check,
        audit_check,
        route_check,
        kill_check,
        doctrine_check,
        cost_check,
    )
    first_failure = next(
        (item for item in checks if not item.passed),
        None,
    )
    overall_verdict = (
        "HEALTHY"
        if first_failure is None
        else f"ABORT — {first_failure.check_id}: {first_failure.detail}"
    )
    next_action = (
        "enable_tihna_lane"
        if first_failure is None
        else "investigate_and_do_not_enable_lanes"
    )
    timestamp = _iso(instant)
    unsigned = {
        "verification_timestamp": timestamp,
        "restart_not_before": _iso(threshold),
        "processes": processes,
        "code_freshness": code_freshness,
        "programme": programme,
        "programme_status": programme_status,
        "smoke_dry_run": smoke,
        "lane_dry_run": lane_dry,
        "cutover_rehearsal": rehearsal,
        "lane_manifest_audit": audit,
        "route_context": route_context_result,
        "kill_switch": kill_switch,
        "doctrine": doctrine,
        "cost_cap": cost_cap,
        "checks": [asdict(item) for item in checks],
        "overall_verdict": overall_verdict,
        "recommended_next_action": next_action,
    }
    signature = hashlib.sha256(_signature_payload(unsigned)).hexdigest()
    return CutoverVerificationReport(
        verification_timestamp=timestamp,
        restart_not_before=_iso(threshold),
        processes=processes,
        code_freshness=code_freshness,
        programme=programme,
        programme_status=programme_status,
        smoke_dry_run=smoke,
        lane_dry_run=lane_dry,
        cutover_rehearsal=rehearsal,
        lane_manifest_audit=audit,
        route_context=route_context_result,
        kill_switch=kill_switch,
        doctrine=doctrine,
        cost_cap=cost_cap,
        checks=checks,
        overall_verdict=overall_verdict,
        recommended_next_action=next_action,
        sha256=signature,
    )


__all__ = [
    "CutoverVerificationReport",
    "VerificationCheck",
    "verify_cutover",
]
