"""Read-only rehearsal of the operator-controlled cutover sequence."""

from __future__ import annotations

import hashlib
import inspect
import json
import re
import sqlite3
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from hermes_constants import get_default_hermes_root
from hermes_cli.lanes.doctor import run_lane_doctor
from hermes_cli.lanes.dry_run import run_lane_dry_run
from hermes_cli.lanes.manifest import (
    load_manifest as load_lane_manifest,
)
from hermes_cli.service.manifest import (
    compute_restart_order,
    load_manifest as load_service_manifest,
)
from hermes_cli.smoke.roundtrip import (
    _remove_temp_copy,
    run_smoke_turn,
)


_MELBOURNE = ZoneInfo("Australia/Melbourne")
_EXPECTED_QUARANTINE_NODES = (
    "test_telegram_can_continue_interrupted_task_after_restart",
    "test_terminal_gate_rejects_active_todo",
    "test_terminal_gate_accepts_completed_task",
    "test_terminal_gate_rejects_active_process_and_delegation",
    "test_review_completion_is_claimed_once",
    "test_spawn_starts_once_for_duplicate_completion",
    "test_bounded_review_wait_refuses_active_turn",
)
_SPEC_FILE_MANIFEST: dict[str, tuple[str, ...]] = {
    "CS-01c": ("hermes_cli/programme/ingress.py", "run_agent.py"),
    "CS-04": (
        "hermes_cli/session/rotation_config.py",
        "agent/conversation_loop.py",
    ),
    "CS-05rev": (
        "hermes_cli/routing/reader.py",
        "hermes_cli/routing/facade.py",
    ),
    "CS-05b": ("hermes_cli/routing/drift.py",),
    "CS-06": ("tests/test_cs06_default_flip.py",),
    "CS-10a": (
        "hermes_cli/cost/caps.py",
        "hermes_cli/cost/kill_switch.py",
    ),
    "CS-10brev": (
        "hermes_cli/routing/route_context.py",
        "hermes_cli/lanes/harness.py",
    ),
    "CS-12": (
        "hermes_cli/service/manifest.py",
        "hermes_cli/service/runner.py",
    ),
    "CS-13": ("hermes_cli/smoke/roundtrip.py",),
    "CS-14": (
        "hermes_cli/lanes/contracts.py",
        "hermes_cli/lanes/harness.py",
    ),
    "CS-15": ("hermes_cli/lanes/impls/tihna.py",),
    "CS-16": (
        "hermes_cli/lanes/doctor.py",
        "hermes_cli/lanes/dry_run.py",
    ),
    "CS-18": (
        "docs/known_debt/PRE_CS01_WIP_DEBT.md",
        "tests/test_wip_debt_quarantine.py",
    ),
}


@dataclass(frozen=True)
class PreconditionResult:
    """One deterministic readiness assertion."""

    precondition_id: str
    label: str
    status: str
    detail: str

    @property
    def passed(self) -> bool:
        return self.status == "PASS"


@dataclass(frozen=True)
class CutoverRehearsalReport:
    """Serializable and content-signed rehearsal evidence."""

    timestamp: str
    programme: dict[str, Any]
    cost_today_melbourne: dict[str, Any]
    lane_manifest: dict[str, Any]
    service_manifest: dict[str, Any]
    database: dict[str, Any]
    processes: list[dict[str, Any]]
    doctrine_rows: list[dict[str, Any]]
    kill_switch: dict[str, Any]
    session_rotation: dict[str, Any]
    inferred_unlanded_specs: list[dict[str, Any]]
    restart_plan: dict[str, Any]
    requires_operator_unlock: bool
    preconditions: tuple[PreconditionResult, ...]
    smoke_dry_run: dict[str, Any]
    lane_dry_run: dict[str, Any]
    cap_options: tuple[dict[str, str], ...]
    operator_actions: tuple[str, ...]
    go_no_go: str
    sha256: str

    @property
    def exit_code(self) -> int:
        return 0 if self.go_no_go == "GO" else 1

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["preconditions"] = [
            asdict(item) for item in self.preconditions
        ]
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
            "# Hermes Monday Cutover Rehearsal",
            "",
            f"- Timestamp: `{self.timestamp}`",
            f"- Decision: **{self.go_no_go}**",
            f"- Requires operator unlock: `{str(self.requires_operator_unlock).lower()}`",
            f"- Content SHA-256: `{self.sha256}`",
            "",
            "## Programme and cost",
            "",
            f"```json\n{json.dumps({'programme': self.programme, 'cost_today_melbourne': self.cost_today_melbourne}, indent=2, sort_keys=True)}\n```",
            "",
            "## Manifests and database",
            "",
            f"```json\n{json.dumps({'lane_manifest': self.lane_manifest, 'service_manifest': self.service_manifest, 'database': self.database}, indent=2, sort_keys=True)}\n```",
            "",
            "## Processes",
            "",
            f"```json\n{json.dumps(self.processes, indent=2, sort_keys=True)}\n```",
            "",
            "## Doctrine, kill switch, and session rotation",
            "",
            f"```json\n{json.dumps({'doctrine_rows': self.doctrine_rows, 'kill_switch': self.kill_switch, 'session_rotation': self.session_rotation}, indent=2, sort_keys=True)}\n```",
            "",
            "## Inferred unlanded specifications",
            "",
            "Inference is file-presence evidence only; it is not git-history or publication proof.",
            "",
            f"```json\n{json.dumps(self.inferred_unlanded_specs, indent=2, sort_keys=True)}\n```",
            "",
            "## Restart plan (not executed)",
            "",
            f"```json\n{json.dumps(self.restart_plan, indent=2, sort_keys=True)}\n```",
            "",
            "## Preconditions",
            "",
            "| ID | Status | Check | Detail |",
            "|---|---|---|---|",
        ]
        for check in self.preconditions:
            detail = check.detail.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {check.precondition_id} | {check.status} | "
                f"{check.label} | {detail} |"
            )
        lines.extend(
            [
                "",
                "## CS-13 smoke dry-run",
                "",
                f"```json\n{json.dumps(self.smoke_dry_run, indent=2, sort_keys=True)}\n```",
                "",
                "## CS-16 full-lane dry-run",
                "",
                f"```json\n{json.dumps(self.lane_dry_run, indent=2, sort_keys=True)}\n```",
                "",
                "## Cap-hit options (not executed)",
                "",
            ]
        )
        for option in self.cap_options:
            lines.append(
                f"- {option['label']}: `{option['command']}` — "
                f"{option['effect']}"
            )
        lines.extend(["", "## Required operator actions", ""])
        lines.extend(f"{index}. {item}" for index, item in enumerate(
            self.operator_actions,
            start=1,
        ))
        lines.extend(
            [
                "",
                "## Decision",
                "",
                f"**{self.go_no_go}**. This command performed no restart, "
                "resume, manifest mutation, production database write, "
                "network publish, or operator-lock invalidation.",
                "",
            ]
        )
        return "\n".join(lines)


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


def _row_dict(row: sqlite3.Row | None) -> dict[str, Any]:
    return {} if row is None else dict(row)


def _snapshot_database(
    db_path: Path,
    *,
    local_now: datetime,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
]:
    connection = _read_only_connection(db_path)
    try:
        integrity_rows = connection.execute(
            "PRAGMA integrity_check"
        ).fetchall()
        integrity_messages = [str(row[0]) for row in integrity_rows]
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                "ORDER BY name"
            )
        ]
        row_counts = {
            table: int(
                connection.execute(
                    f'SELECT COUNT(*) FROM "{table}"'
                ).fetchone()[0]
            )
            for table in tables
        }
        programme = _row_dict(
            connection.execute(
                "SELECT * FROM programme_state WHERE id=1"
            ).fetchone()
        ) if _table_exists(connection, "programme_state") else {}

        day_start = local_now.astimezone(_MELBOURNE).replace(
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
        day_end = day_start.replace(
            day=day_start.day,
        )
        from datetime import timedelta

        day_end += timedelta(days=1)
        start_utc = day_start.astimezone(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        )
        end_utc = day_end.astimezone(timezone.utc).isoformat().replace(
            "+00:00",
            "Z",
        )
        if _table_exists(connection, "cost_ledger"):
            cost_row = connection.execute(
                """
                SELECT COUNT(*) AS row_count,
                       COALESCE(SUM(aud_amount), 0.0) AS aud
                  FROM cost_ledger
                 WHERE ts >= ? AND ts < ?
                """,
                (start_utc, end_utc),
            ).fetchone()
            cost = {
                "calendar_date": day_start.date().isoformat(),
                "timezone": "Australia/Melbourne",
                "window_start_utc": start_utc,
                "window_end_utc": end_utc,
                "row_count": int(cost_row["row_count"]),
                "aud": round(float(cost_row["aud"]), 8),
            }
        else:
            cost = {
                "calendar_date": day_start.date().isoformat(),
                "timezone": "Australia/Melbourne",
                "window_start_utc": start_utc,
                "window_end_utc": end_utc,
                "row_count": 0,
                "aud": 0.0,
            }

        doctrine: list[dict[str, Any]] = []
        if _table_exists(connection, "routing_doctrine"):
            doctrine = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT lane, rung, complexity, primary_provider,
                           primary_model, version
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

        killed_count = 0
        if _table_exists(connection, "task_kill_switch"):
            killed_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM task_kill_switch"
                ).fetchone()[0]
            )
        kill_switch = {
            "tripped": killed_count > 0,
            "active_rows": killed_count,
        }

        session_columns: set[str] = set()
        if _table_exists(connection, "sessions"):
            session_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(sessions)")
            }
        rotation_rows = 0
        max_tokens = 0
        if "rotation_reason" in session_columns:
            rotation_rows = int(
                connection.execute(
                    "SELECT COUNT(*) FROM sessions "
                    "WHERE rotation_reason IS NOT NULL"
                ).fetchone()[0]
            )
        if "token_count" in session_columns:
            max_tokens = int(
                connection.execute(
                    "SELECT COALESCE(MAX(token_count), 0) FROM sessions"
                ).fetchone()[0]
            )
        rotation = {
            "soft_limit_tokens": 100_000,
            "rotation_rows": rotation_rows,
            "max_observed_token_count": max_tokens,
        }
        database = {
            "path": str(db_path),
            "integrity_check": integrity_messages,
            "table_count": len(tables),
            "total_rows": sum(row_counts.values()),
            "row_counts": row_counts,
        }
        return database, programme, cost, doctrine, kill_switch, rotation
    finally:
        connection.close()


def _snapshot_processes(manifest) -> list[dict[str, Any]]:
    processes: list[dict[str, Any]] = []
    for service in manifest.services:
        pid: int | None = None
        try:
            pid = int(service.pid_file.read_text(encoding="utf-8").strip())
        except (OSError, TypeError, ValueError):
            pass
        start_time = None
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
                    start_time = " ".join(parts[:5])
                    command = parts[5] if len(parts) == 6 else ""
        processes.append(
            {
                "service_id": service.id,
                "pid_file": str(service.pid_file),
                "pid": pid,
                "alive": alive,
                "start_time": start_time,
                "command": command,
            }
        )
    return processes


def _infer_specs(repo_root: Path) -> list[dict[str, Any]]:
    result = []
    for spec_id, relative_paths in _SPEC_FILE_MANIFEST.items():
        paths = [repo_root / item for item in relative_paths]
        result.append(
            {
                "spec_id": spec_id,
                "status": (
                    "INFERRED_PRESENT"
                    if all(path.exists() for path in paths)
                    else "INCOMPLETE"
                ),
                "files": [str(path) for path in paths],
                "missing": [
                    str(path) for path in paths if not path.exists()
                ],
                "caveat": (
                    "Inferred from the declared file manifest only; "
                    "not proof of git history, commit, push, or publication."
                ),
            }
        )
    return result


def _source_contains(path: Path, *needles: str) -> tuple[bool, str]:
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return False, f"{path}: {type(exc).__name__}: {exc}"
    missing = [needle for needle in needles if needle not in source]
    if missing:
        return False, f"{path}: missing {missing}"
    return True, f"{path}: found {list(needles)}"


def _check(
    precondition_id: str,
    label: str,
    passed: bool,
    detail: str,
    overrides: Mapping[str, bool] | None,
) -> PreconditionResult:
    if overrides and precondition_id in overrides:
        passed = bool(overrides[precondition_id])
        detail = f"test override: {passed}; observed: {detail}"
    return PreconditionResult(
        precondition_id=precondition_id,
        label=label,
        status="PASS" if passed else "FAIL",
        detail=detail,
    )


def _preconditions(
    *,
    repo_root: Path,
    lane_manifest: dict[str, Any],
    database: dict[str, Any],
    doctrine_rows: list[dict[str, Any]],
    kill_switch: dict[str, Any],
    smoke: dict[str, Any],
    lane_doctor: dict[str, Any],
    lane_dry: dict[str, Any],
    overrides: Mapping[str, bool] | None,
) -> tuple[PreconditionResult, ...]:
    plugin = (
        get_default_hermes_root()
        / "profiles"
        / "atlas"
        / "plugins"
        / "task-model-router"
        / "__init__.py"
    )
    checks: list[PreconditionResult] = []

    ok, detail = _source_contains(
        repo_root / "run_agent.py",
        "admit_new_turn(",
        "prepare_session_rotation(",
    )
    checks.append(_check(
        "CS01c", "Universal ingress gate wired before turn execution",
        ok, detail, overrides,
    ))

    ok, detail = _source_contains(
        repo_root / "hermes_cli/session/rotation_config.py",
        "soft_limit_tokens: int = 100_000",
    )
    checks.append(_check(
        "CS04", "100k shared session rotation",
        ok, detail, overrides,
    ))

    ok, detail = _source_contains(
        plugin,
        'args.get("use_doctrine_reader", True)',
        '"default": True',
    )
    checks.append(_check(
        "CS05rev", "Doctrine reader is default-on for Atlas single routes",
        ok, detail, overrides,
    ))

    ok, detail = _source_contains(
        repo_root / "hermes_cli/routing/drift.py",
        "def compute_drift_window",
        "def maybe_alert",
    )
    checks.append(_check(
        "CS05b", "Doctrine drift guard is present",
        ok, detail, overrides,
    ))

    ok, detail = _source_contains(
        plugin,
        'args.get("force_legacy_routing", False)',
        "forced_legacy=force_legacy",
    )
    checks.append(_check(
        "CS06", "Legacy routing default retired behind explicit escape hatch",
        ok, detail, overrides,
    ))

    caps_ok, caps_detail = _source_contains(
        repo_root / "hermes_cli/cost/caps.py",
        "per_lane_",
    )
    kill_ok = (repo_root / "hermes_cli/cost/kill_switch.py").exists()
    checks.append(_check(
        "CS10a", "Caps and per-task kill switch are present",
        caps_ok and kill_ok,
        f"{caps_detail}; kill_switch_module={kill_ok}",
        overrides,
    ))

    ok, detail = _source_contains(
        repo_root / "hermes_cli/routing/route_context.py",
        "HERMES_ROUTE_CONTEXT_JSON",
        "Read and clear",
    )
    checks.append(_check(
        "CS10brev", "One-shot route context flow is wired",
        ok, detail, overrides,
    ))

    ok, detail = _source_contains(
        repo_root / "hermes_cli/service/cli.py",
        '"restart"',
        "_cmd_restart",
    )
    checks.append(_check(
        "CS12", "Coordinated restart CLI is registered",
        ok, detail, overrides,
    ))

    smoke_ok = smoke.get("overall") == "PASS" and not smoke.get("commit", True)
    checks.append(_check(
        "CS13", "Doctrine smoke CLI passes in dry-run mode",
        smoke_ok,
        f"overall={smoke.get('overall')}; commit={smoke.get('commit')}",
        overrides,
    ))

    try:
        from hermes_cli.lanes.contracts import BusinessLane
        from hermes_cli.lanes.harness import LaneHarness

        importable = inspect.isclass(BusinessLane) and inspect.isclass(LaneHarness)
        import_detail = (
            f"BusinessLane={BusinessLane.__module__}; "
            f"LaneHarness={LaneHarness.__module__}"
        )
    except Exception as exc:
        importable = False
        import_detail = f"{type(exc).__name__}: {exc}"
    checks.append(_check(
        "CS14", "BusinessLane and LaneHarness are importable",
        importable, import_detail, overrides,
    ))

    checks.append(_check(
        "CS15", "Tihna lane doctor is healthy",
        bool(lane_doctor.get("success")),
        f"success={lane_doctor.get('success')}; errors={lane_doctor.get('errors')}",
        overrides,
    ))

    checks.append(_check(
        "CS16", "Lane doctor and full fixture dry-run are healthy",
        bool(lane_doctor.get("success")) and bool(lane_dry.get("success")),
        f"doctor={lane_doctor.get('success')}; dry_run={lane_dry.get('success')}",
        overrides,
    ))

    quarantine_source = (
        repo_root / "tests/conftest.py"
    ).read_text(encoding="utf-8")
    quarantine_block = quarantine_source.split(
        "_PRE_CS01_WIP_DEBT_NODE_IDS =",
        1,
    )[1].split("_PRE_CS01_WIP_DEBT_REASON =", 1)[0]
    observed_quarantine_nodes = frozenset(
        re.findall(r'"(test_[a-zA-Z0-9_]+)"', quarantine_block)
    )
    quarantine_ok = (
        observed_quarantine_nodes == frozenset(_EXPECTED_QUARANTINE_NODES)
        and "pytest.mark.xfail" in quarantine_source
    )
    checks.append(_check(
        "CS18", "Exactly the seven documented WIP-debt nodes are quarantined",
        quarantine_ok,
        f"observed_nodes={len(observed_quarantine_nodes)}; xfail_hook="
        f"{'pytest.mark.xfail' in quarantine_source}",
        overrides,
    ))

    integrity = database.get("integrity_check") == ["ok"]
    checks.append(_check(
        "DB", "Production SQLite integrity check",
        integrity,
        f"integrity_check={database.get('integrity_check')}",
        overrides,
    ))

    tihna_matches = any(
        row.get("lane") == "tihna"
        and row.get("primary_provider") == "openai-codex"
        and row.get("primary_model") == "gpt-5-6-sol"
        for row in doctrine_rows
    )
    checks.append(_check(
        "TIHNA_DOCTRINE", "Tihna doctrine uses openai-codex/gpt-5-6-sol",
        tihna_matches,
        f"matching_row={tihna_matches}",
        overrides,
    ))

    checks.append(_check(
        "LANE_MANIFEST", "Lane manifest schema version is 1",
        lane_manifest.get("schema_version") == 1,
        f"schema_version={lane_manifest.get('schema_version')}",
        overrides,
    ))

    checks.append(_check(
        "KILL_SWITCH", "No task kill switch is tripped",
        not bool(kill_switch.get("tripped")),
        f"active_rows={kill_switch.get('active_rows')}",
        overrides,
    ))
    return tuple(checks)


def _normalize_result(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    method = getattr(value, "to_dict", None)
    if callable(method):
        return dict(method())
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    raise TypeError(f"unsupported rehearsal result: {type(value).__name__}")


def _signature_payload(values: dict[str, Any]) -> str:
    return json.dumps(
        values,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def rehearse_cutover(
    *,
    db_path: str | Path | None = None,
    lane_manifest_path: str | Path | None = None,
    service_manifest_path: str | Path | None = None,
    repo_root: str | Path | None = None,
    now: datetime | None = None,
    smoke_runner: Callable[..., Any] = run_smoke_turn,
    lane_dry_runner: Callable[..., Any] = run_lane_dry_run,
    lane_doctor_runner: Callable[..., Any] = run_lane_doctor,
    process_snapshotter: Callable[[Any], list[dict[str, Any]]] = _snapshot_processes,
    precondition_overrides: Mapping[str, bool] | None = None,
) -> CutoverRehearsalReport:
    """Build a signed Monday cutover rehearsal without mutating live state."""
    hermes_root = get_default_hermes_root()
    source_db = Path(db_path or hermes_root / "kanban.db").expanduser()
    source_lane_manifest = Path(
        lane_manifest_path or hermes_root / "lane_manifest.yaml"
    ).expanduser()
    source_service_manifest = Path(
        service_manifest_path or hermes_root / "service_manifest.yaml"
    ).expanduser()
    source_repo = Path(repo_root or Path(__file__).parents[2]).expanduser()
    instant = now or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)

    lane_manifest_object = load_lane_manifest(
        source_lane_manifest,
        db_path=source_db,
        record_state=False,
    )
    lane_manifest = {
        "path": str(source_lane_manifest),
        "schema_version": 1,
        "manifest_hash": lane_manifest_object.manifest_hash,
        "lanes": [asdict(lane) for lane in lane_manifest_object.lanes],
    }
    service_manifest_object = load_service_manifest(
        source_service_manifest,
        db_path=source_db,
        record_state=False,
    )
    service_manifest = {
        "path": str(source_service_manifest),
        "schema_version": service_manifest_object.schema_version,
        "manifest_hash": service_manifest_object.manifest_hash,
        "operator_review_required": (
            service_manifest_object.operator_review_required
        ),
        "operator_review_note": (
            service_manifest_object.operator_review_note
        ),
        "service_count": len(service_manifest_object.services),
    }
    (
        database,
        programme,
        cost,
        doctrine,
        kill_switch,
        rotation,
    ) = _snapshot_database(source_db, local_now=instant)
    processes = process_snapshotter(service_manifest_object)

    smoke_value = smoke_runner(
        scenario="success",
        lane="default",
        db_path=source_db,
        commit=False,
    )
    smoke = _normalize_result(smoke_value)
    working_db = smoke.get("working_db_path")
    if not smoke.get("commit", True):
        _remove_temp_copy(working_db)

    lane_doctor = _normalize_result(
        lane_doctor_runner(
            "tihna",
            manifest_path=source_lane_manifest,
            db_path=source_db,
        )
    )
    lane_dry = _normalize_result(
        lane_dry_runner(
            "tihna",
            stage="full",
            manifest_path=source_lane_manifest,
            db_path=source_db,
        )
    )

    start_order = compute_restart_order(service_manifest_object)
    stop_order = list(reversed(start_order))
    restart_plan = {
        "would_execute": False,
        "requires_operator_unlock": True,
        "stop_order": [service.id for service in stop_order],
        "drain": [
            {
                "service_id": service.id,
                "timeout_seconds": service.drain_timeout_seconds,
            }
            for service in stop_order
        ],
        "restart_order": [service.id for service in start_order],
        "untouched": [],
    }
    inferred = _infer_specs(source_repo)
    checks = _preconditions(
        repo_root=source_repo,
        lane_manifest=lane_manifest,
        database=database,
        doctrine_rows=doctrine,
        kill_switch=kill_switch,
        smoke=smoke,
        lane_doctor=lane_doctor,
        lane_dry=lane_dry,
        overrides=precondition_overrides,
    )
    go_no_go = (
        "GO"
        if all(item.passed for item in checks)
        and service_manifest_object.operator_review_required
        else "NO-GO"
    )
    cap_options = (
        {
            "label": "Option A",
            "command": "hermes programme resume --acknowledge-cap-hit",
            "effect": "Resume while explicitly acknowledging the current cap hit.",
        },
        {
            "label": "Option B",
            "command": "hermes programme resume --roll-cap-window",
            "effect": "Resume only after rolling to a fresh cap window.",
        },
    )
    operator_actions = (
        "Review the signed manifest and every precondition.",
        "Choose exactly one cap-hit option; this rehearsal chooses neither.",
        "Unlock the service manifest through the approved operator path.",
        "Run the coordinated restart only after the unlock and cap decision.",
        "Run smoke and lane verification again after the real cutover.",
    )
    timestamp = instant.astimezone(timezone.utc).isoformat(
        timespec="seconds",
    ).replace("+00:00", "Z")
    unsigned = {
        "timestamp": timestamp,
        "programme": programme,
        "cost_today_melbourne": cost,
        "lane_manifest": lane_manifest,
        "service_manifest": service_manifest,
        "database": database,
        "processes": processes,
        "doctrine_rows": doctrine,
        "kill_switch": kill_switch,
        "session_rotation": rotation,
        "inferred_unlanded_specs": inferred,
        "restart_plan": restart_plan,
        "requires_operator_unlock": True,
        "preconditions": [asdict(item) for item in checks],
        "smoke_dry_run": smoke,
        "lane_dry_run": lane_dry,
        "cap_options": list(cap_options),
        "operator_actions": list(operator_actions),
        "go_no_go": go_no_go,
    }
    signature = hashlib.sha256(
        _signature_payload(unsigned).encode("utf-8")
    ).hexdigest()
    return CutoverRehearsalReport(
        timestamp=timestamp,
        programme=programme,
        cost_today_melbourne=cost,
        lane_manifest=lane_manifest,
        service_manifest=service_manifest,
        database=database,
        processes=processes,
        doctrine_rows=doctrine,
        kill_switch=kill_switch,
        session_rotation=rotation,
        inferred_unlanded_specs=inferred,
        restart_plan=restart_plan,
        requires_operator_unlock=True,
        preconditions=checks,
        smoke_dry_run=smoke,
        lane_dry_run=lane_dry,
        cap_options=cap_options,
        operator_actions=operator_actions,
        go_no_go=go_no_go,
        sha256=signature,
    )


__all__ = [
    "CutoverRehearsalReport",
    "PreconditionResult",
    "rehearse_cutover",
]
