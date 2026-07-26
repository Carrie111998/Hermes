"""Read-only diagnostics for registered business lanes."""

from __future__ import annotations

import ast
import importlib
import importlib.util
import inspect
import json
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

from hermes_constants import get_default_hermes_root
from hermes_cli.lanes.contracts import BusinessLane, LaneTask
from hermes_cli.lanes.errors import PublishDisabled
from hermes_cli.lanes.harness import LaneHarness
from hermes_cli.lanes.manifest import (
    LaneConfig,
    default_path,
    validate_manifest,
)

_HARNESS_SIGNATURES = {
    "find_task": ("external_id",),
    "list_tasks": ("status", "ingested_since"),
    "update_task": ("task", "payload", "status"),
    "check_rate_limit": ("window_kind", "increment"),
    "lint_draft": ("text",),
    "publish_with_ledger": (
        "task",
        "external_target",
        "payload",
        "side_effect_key",
        "publisher",
    ),
    "admit": ("task", "apply_rate_limits"),
    "call_llm": ("task", "prompt", "max_tokens", "purpose"),
    "enqueue_approval": ("task", "draft"),
    "record_metric": ("task", "metric_name", "value"),
}


@dataclass(frozen=True)
class LaneDoctorReport:
    lane_id: str
    success: bool
    registered: bool
    module_status: str
    protocol_satisfied: bool
    manifest: dict[str, Any] = field(default_factory=dict)
    doctrine: dict[str, Any] = field(default_factory=dict)
    rate_limits: dict[str, Any] = field(default_factory=dict)
    hygiene_checks: dict[str, bool] = field(default_factory=dict)
    harness_methods: dict[str, dict[str, Any]] = field(default_factory=dict)
    publish_disabled_guard: bool = False
    checks: dict[str, bool] = field(default_factory=dict)
    errors: tuple[str, ...] = ()

    @property
    def exit_code(self) -> int:
        return 0 if self.success else 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
        )


def _load_manifest(path: Path):
    return validate_manifest(
        yaml.safe_load(path.read_text(encoding="utf-8"))
    )


def _module_spec(module_name: str):
    try:
        return importlib.util.find_spec(module_name)
    except (ImportError, ModuleNotFoundError, ValueError):
        return None


def _called_names(tree: ast.AST) -> set[str]:
    return {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
    }


def _harness_calls(tree: ast.AST) -> list[str]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function = node.func
        if (
            isinstance(function, ast.Attribute)
            and isinstance(function.value, ast.Name)
            and function.value.id == "harness"
        ):
            calls.append(function.attr)
    return calls


def _imports(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def run_hygiene_checks(source_path: Path) -> dict[str, bool]:
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = _imports(tree)
    direct_calls = _called_names(tree)
    harness_calls = _harness_calls(tree)
    raw_llm_calls = {
        "call_llm",
        "async_call_llm",
        "completion",
        "responses",
    }
    return {
        "ingress_only_through_harness": (
            "hermes_cli.programme.ingress" not in imported
            and "admit_new_turn" not in direct_calls
        ),
        "sqlite_only_through_harness": (
            "hermes_cli.sqlite_util" not in imported
            and "retrying_write_txn" not in direct_calls
        ),
        "cost_only_through_harness": (
            "hermes_cli.cost.ledger" not in imported
            and "record_call" not in direct_calls
        ),
        "verdict_only_through_harness": (
            "hermes_cli.verdict" not in imported
            and "record_verdict" not in direct_calls
        ),
        "routing_only_through_harness": (
            "hermes_cli.routing.facade" not in imported
            and "route_for_turn" not in direct_calls
        ),
        "route_context_only_through_harness": (
            "HERMES_ROUTE_CONTEXT_JSON" not in source
            and "hermes_cli.routing.route_context" not in imported
        ),
        "llm_only_through_harness": (
            bool(harness_calls.count("call_llm"))
            and not raw_llm_calls.intersection(direct_calls)
        ),
        "publish_only_through_harness": (
            harness_calls.count("publish_with_ledger") == 1
            and "requests" not in imported
            and "subprocess" not in imported
        ),
    }


def _harness_surface() -> dict[str, dict[str, Any]]:
    results = {}
    for method_name, expected in _HARNESS_SIGNATURES.items():
        method = getattr(LaneHarness, method_name, None)
        present = callable(method)
        parameters: tuple[str, ...] = ()
        if present:
            parameters = tuple(
                name
                for name in inspect.signature(method).parameters
                if name != "self"
            )
        results[method_name] = {
            "present": present,
            "parameters": list(parameters),
            "signature_ok": present and parameters == expected,
        }
    return results


def _read_doctrine(
    *,
    lane_id: str,
    db_path: Path,
) -> dict[str, Any]:
    uri = f"file:{db_path}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            """SELECT primary_provider,primary_model,fallback_chain_json
                 FROM routing_doctrine
                WHERE lane=? AND version=1
                ORDER BY id LIMIT 1""",
            (lane_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        return {}
    return {
        "provider": str(row["primary_provider"]),
        "model": str(row["primary_model"]),
        "fallback_chain": json.loads(
            str(row["fallback_chain_json"] or "[]")
        ),
    }


def _manifest_report(config: LaneConfig) -> dict[str, Any]:
    publish_channel = (
        "local:file:tihna-digests"
        if config.lane_id == "tihna"
        else None
    )
    return {
        "enabled": config.enabled,
        "publish_enabled": config.publish_enabled,
        "daily_cost_cap_aud": config.per_lane_daily_cost_cap_aud,
        "approval_channel": config.approval_channel,
        "publish_channel": publish_channel,
    }


def _rate_limit_report(config: LaneConfig) -> dict[str, Any]:
    return {
        "daily_cost_cap_aud": config.per_lane_daily_cost_cap_aud,
        "daily_task_cap": config.per_lane_daily_task_cap,
        "hourly_ingest_cap": config.per_lane_hourly_ingest_cap,
    }


def _publish_disabled_guard(config: LaneConfig) -> bool:
    if config.publish_enabled:
        return False
    harness = object.__new__(LaneHarness)
    harness.dry_run = False
    harness.config = config
    harness.lane_id = config.lane_id
    try:
        harness.publish_with_ledger(
            task=LaneTask(
                lane_id=config.lane_id,
                external_id="doctor",
                task_id="doctor",
                id=1,
                payload={},
            ),
            external_target="doctor:test",
            payload={},
        )
    except PublishDisabled:
        return True
    return False


def run_lane_doctor(
    lane_id: str,
    *,
    manifest_path: str | Path | None = None,
    db_path: str | Path | None = None,
) -> LaneDoctorReport:
    normalized = str(lane_id).strip().lower()
    source_manifest = (
        Path(manifest_path).expanduser()
        if manifest_path is not None
        else default_path()
    )
    source_db = (
        Path(db_path).expanduser()
        if db_path is not None
        else get_default_hermes_root() / "kanban.db"
    )
    errors: list[str] = []
    manifest = _load_manifest(source_manifest)
    config = manifest.by_id().get(normalized)
    registered = config is not None
    if config is None:
        return LaneDoctorReport(
            lane_id=normalized,
            success=False,
            registered=False,
            module_status="UNKNOWN",
            protocol_satisfied=False,
            checks={"registered": False},
            errors=(f"unknown lane: {normalized}",),
        )

    spec = _module_spec(config.module)
    module_status = "RESOLVABLE" if spec is not None else "ABSENT"
    protocol_satisfied = False
    hygiene_checks: dict[str, bool] = {}
    if spec is None:
        errors.append(f"module_status=ABSENT: {config.module}")
    else:
        origin = getattr(spec, "origin", None)
        if not origin:
            errors.append(f"module has no source origin: {config.module}")
        else:
            try:
                hygiene_checks = run_hygiene_checks(Path(origin))
            except (OSError, SyntaxError) as exc:
                errors.append(
                    f"hygiene inspection failed: {type(exc).__name__}: {exc}"
                )
        try:
            module = importlib.import_module(config.module)
            factory = getattr(module, "build_lane", None)
            if not callable(factory):
                errors.append(
                    f"lane module has no build_lane(): {config.module}"
                )
            else:
                protocol_satisfied = isinstance(factory(), BusinessLane)
        except Exception as exc:
            errors.append(
                f"lane activation failed: {type(exc).__name__}: {exc}"
            )

    try:
        doctrine = _read_doctrine(
            lane_id=normalized,
            db_path=source_db,
        )
    except (OSError, sqlite3.Error, ValueError) as exc:
        doctrine = {}
        errors.append(
            f"doctrine read failed: {type(exc).__name__}: {exc}"
        )
    harness_methods = _harness_surface()
    publish_guard = _publish_disabled_guard(config)
    checks = {
        "registered": registered,
        "module_resolvable": module_status == "RESOLVABLE",
        "protocol_satisfied": protocol_satisfied,
        "doctrine_present": bool(doctrine),
        "all_hygiene_checks": (
            len(hygiene_checks) == 8 and all(hygiene_checks.values())
        ),
        "all_harness_methods": (
            len(harness_methods) == 10
            and all(
                result["signature_ok"]
                for result in harness_methods.values()
            )
        ),
        "publish_disabled_guard": publish_guard,
    }
    for name, passed in checks.items():
        if not passed and not any(name in error for error in errors):
            errors.append(f"check failed: {name}")
    success = all(checks.values())
    return LaneDoctorReport(
        lane_id=normalized,
        success=success,
        registered=registered,
        module_status=module_status,
        protocol_satisfied=protocol_satisfied,
        manifest=_manifest_report(config),
        doctrine=doctrine,
        rate_limits=_rate_limit_report(config),
        hygiene_checks=hygiene_checks,
        harness_methods=harness_methods,
        publish_disabled_guard=publish_guard,
        checks=checks,
        errors=tuple(errors),
    )


__all__ = [
    "LaneDoctorReport",
    "run_hygiene_checks",
    "run_lane_doctor",
]
