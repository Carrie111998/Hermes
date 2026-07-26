"""Validated service manifests, canonical hashing, and restart ordering."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import yaml

from hermes_constants import get_default_hermes_root
from hermes_cli.service import schema
from hermes_cli.sqlite_util import retrying_write_txn


_HEALTH_TYPES = frozenset({"http", "exec", "pid_alive"})
_TAG_PRIORITY = {
    "critical": 0,
    "gateway": 1,
    "server": 2,
    "ui": 3,
    "worker": 4,
    "mcp": 5,
}


class ManifestError(ValueError):
    """Raised when a manifest is structurally unsafe or inconsistent."""


@dataclass(frozen=True)
class ServiceSpec:
    """One validated service declaration."""

    id: str
    name: str
    pid_file: Path
    command: tuple[str, ...]
    working_dir: Path
    env: Mapping[str, str]
    health_check: Mapping[str, Any]
    drain_timeout_seconds: float
    start_timeout_seconds: float
    depends_on: tuple[str, ...]
    tags: tuple[str, ...]
    manifest_index: int

    @property
    def is_critical(self) -> bool:
        return "critical" in self.tags


@dataclass(frozen=True)
class Manifest:
    """A validated manifest and its stable canonical representation."""

    schema_version: int
    services: tuple[ServiceSpec, ...]
    operator_review_required: bool
    operator_review_note: str | None
    canonical_json: str
    manifest_hash: str

    def by_id(self) -> dict[str, ServiceSpec]:
        return {service.id: service for service in self.services}


def default_manifest_path() -> Path:
    """Return the shared operator manifest path."""
    return get_default_hermes_root() / "service_manifest.yaml"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(
        timespec="seconds"
    ).replace("+00:00", "Z")


def _required_mapping(
    value: Any,
    *,
    label: str,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ManifestError(f"{label} must be a mapping")
    return dict(value)


def _string_list(value: Any, *, label: str, nonempty: bool = False) -> tuple[str, ...]:
    if not isinstance(value, list) or any(
        not isinstance(item, str) or not item.strip() for item in value
    ):
        raise ManifestError(f"{label} must be a list of non-empty strings")
    result = tuple(item.strip() for item in value)
    if nonempty and not result:
        raise ManifestError(f"{label} must not be empty")
    return result


def _positive_seconds(value: Any, *, label: str) -> float:
    if isinstance(value, bool):
        raise ManifestError(f"{label} must be a positive number")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ManifestError(f"{label} must be a positive number") from exc
    if result <= 0:
        raise ManifestError(f"{label} must be a positive number")
    return result


def _validate_health(value: Any, *, service_id: str) -> dict[str, Any]:
    health = _required_mapping(
        value,
        label=f"service {service_id!r} health_check",
    )
    kind = health.get("type")
    if kind not in _HEALTH_TYPES:
        raise ManifestError(
            f"service {service_id!r} has unknown health check type {kind!r}"
        )
    health["timeout_seconds"] = _positive_seconds(
        health.get("timeout_seconds", 5),
        label=f"service {service_id!r} health timeout",
    )
    if kind == "http":
        url = health.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise ManifestError(
                f"service {service_id!r} HTTP health check requires a URL"
            )
        expected = health.get("expected_status", 200)
        if isinstance(expected, bool) or not isinstance(expected, int):
            raise ManifestError(
                f"service {service_id!r} expected_status must be an integer"
            )
        health["expected_status"] = expected
    elif kind == "exec":
        health["command"] = list(
            _string_list(
                health.get("command"),
                label=f"service {service_id!r} health command",
                nonempty=True,
            )
        )
        regex = health.get("expected_stdout_regex")
        if regex is not None and not isinstance(regex, str):
            raise ManifestError(
                f"service {service_id!r} health regex must be a string"
            )
    return health


def validate_manifest(raw: Any) -> Manifest:
    """Validate untrusted YAML/JSON data and return immutable service specs."""
    document = _required_mapping(raw, label="manifest")
    if document.get("schema_version") != 1:
        raise ManifestError("manifest schema_version must be 1")
    operator_review_required = document.get(
        "operator_review_required",
        False,
    )
    if not isinstance(operator_review_required, bool):
        raise ManifestError("operator_review_required must be boolean")
    operator_review_note = document.get("operator_review_note")
    if operator_review_note is not None and not isinstance(
        operator_review_note,
        str,
    ):
        raise ManifestError("operator_review_note must be a string")
    raw_services = document.get("services")
    if not isinstance(raw_services, list) or not raw_services:
        raise ManifestError("manifest services must be a non-empty list")

    services: list[ServiceSpec] = []
    seen: set[str] = set()
    canonical_services: list[dict[str, Any]] = []
    for index, item in enumerate(raw_services):
        entry = _required_mapping(item, label=f"service at index {index}")
        service_id = entry.get("id")
        if not isinstance(service_id, str) or not service_id.strip():
            raise ManifestError(f"service at index {index} requires an id")
        service_id = service_id.strip()
        if service_id in seen:
            raise ManifestError(f"duplicate service id: {service_id}")
        seen.add(service_id)

        name = entry.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ManifestError(f"service {service_id!r} requires a name")
        command = _string_list(
            entry.get("command"),
            label=f"service {service_id!r} command",
            nonempty=True,
        )
        pid_file_raw = entry.get("pid_file")
        working_dir_raw = entry.get("working_dir")
        if not isinstance(pid_file_raw, str) or not pid_file_raw:
            raise ManifestError(f"service {service_id!r} requires pid_file")
        if not isinstance(working_dir_raw, str) or not working_dir_raw:
            raise ManifestError(f"service {service_id!r} requires working_dir")
        pid_file = Path(pid_file_raw).expanduser()
        working_dir = Path(working_dir_raw).expanduser()
        if not pid_file.is_absolute() or not working_dir.is_absolute():
            raise ManifestError(
                f"service {service_id!r} paths must be absolute"
            )

        raw_env = _required_mapping(
            entry.get("env", {}),
            label=f"service {service_id!r} env",
        )
        if any(
            not isinstance(key, str) or not isinstance(value, str)
            for key, value in raw_env.items()
        ):
            raise ManifestError(
                f"service {service_id!r} env must map strings to strings"
            )
        depends_on = _string_list(
            entry.get("depends_on", []),
            label=f"service {service_id!r} depends_on",
        )
        tags = _string_list(
            entry.get("tags", []),
            label=f"service {service_id!r} tags",
        )
        health = _validate_health(
            entry.get("health_check"),
            service_id=service_id,
        )
        drain_timeout = _positive_seconds(
            entry.get("drain_timeout_seconds"),
            label=f"service {service_id!r} drain timeout",
        )
        start_timeout = _positive_seconds(
            entry.get("start_timeout_seconds"),
            label=f"service {service_id!r} start timeout",
        )

        canonical = {
            "id": service_id,
            "name": name.strip(),
            "pid_file": str(pid_file),
            "command": list(command),
            "working_dir": str(working_dir),
            "env": dict(raw_env),
            "health_check": health,
            "drain_timeout_seconds": drain_timeout,
            "start_timeout_seconds": start_timeout,
            "depends_on": list(depends_on),
            "tags": list(tags),
        }
        canonical_services.append(canonical)
        services.append(
            ServiceSpec(
                id=service_id,
                name=name.strip(),
                pid_file=pid_file,
                command=command,
                working_dir=working_dir,
                env=dict(raw_env),
                health_check=health,
                drain_timeout_seconds=drain_timeout,
                start_timeout_seconds=start_timeout,
                depends_on=depends_on,
                tags=tags,
                manifest_index=index,
            )
        )

    for service in services:
        missing = set(service.depends_on) - seen
        if missing:
            raise ManifestError(
                f"service {service.id!r} has unknown dependencies: "
                f"{sorted(missing)}"
            )
        if service.id in service.depends_on:
            raise ManifestError(
                f"service {service.id!r} cannot depend on itself"
            )

    canonical_json = json.dumps(
        {
            "schema_version": 1,
            "operator_review_required": operator_review_required,
            "operator_review_note": operator_review_note,
            "services": canonical_services,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    manifest_hash = hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
    manifest = Manifest(
        schema_version=1,
        services=tuple(services),
        operator_review_required=operator_review_required,
        operator_review_note=operator_review_note,
        canonical_json=canonical_json,
        manifest_hash=manifest_hash,
    )
    compute_restart_order(manifest)
    return manifest


def _priority(service: ServiceSpec) -> tuple[int, int, str]:
    tag_rank = min(
        (_TAG_PRIORITY.get(tag, len(_TAG_PRIORITY)) for tag in service.tags),
        default=len(_TAG_PRIORITY),
    )
    return tag_rank, service.manifest_index, service.id


def compute_restart_order(manifest: Manifest) -> list[ServiceSpec]:
    """Return dependency order with deterministic tag-priority tie breaking."""
    by_id = manifest.by_id()
    remaining = {
        service.id: set(service.depends_on)
        for service in manifest.services
    }
    ordered: list[ServiceSpec] = []
    while remaining:
        ready = [
            by_id[service_id]
            for service_id, dependencies in remaining.items()
            if not dependencies
        ]
        if not ready:
            cycle = ", ".join(sorted(remaining))
            raise ManifestError(
                f"service dependency cycle detected among: {cycle}"
            )
        current = min(ready, key=_priority)
        ordered.append(current)
        remaining.pop(current.id)
        for dependencies in remaining.values():
            dependencies.discard(current.id)
    return ordered


def merge_service_env(
    service: ServiceSpec,
    parent: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Merge a service's declared overrides onto a parent environment."""
    result = dict(os.environ if parent is None else parent)
    result.update(service.env)
    return result


def record_manifest_state(
    manifest: Manifest,
    *,
    db_path: str | Path | None = None,
    applied_by: str = "operator",
) -> int:
    """Record a changed manifest and keep exactly one active audit row."""
    schema.ensure_migrated(db_path)
    conn = schema.connect(db_path)
    try:
        with retrying_write_txn(conn):
            active = conn.execute(
                """
                SELECT id, manifest_hash
                  FROM service_manifest_state
                 WHERE is_active = 1
                """
            ).fetchone()
            if (
                active is not None
                and str(active["manifest_hash"]) == manifest.manifest_hash
            ):
                return int(active["id"])
            row = conn.execute(
                "SELECT COALESCE(MAX(version), 0) AS version "
                "FROM service_manifest_state"
            ).fetchone()
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE service_manifest_state SET is_active = 0 "
                "WHERE is_active = 1"
            )
            cursor = conn.execute(
                """
                INSERT INTO service_manifest_state (
                    version, applied_at, applied_by, manifest_hash,
                    manifest_json, is_active
                ) VALUES (?, ?, ?, ?, ?, 1)
                """,
                (
                    version,
                    _utc_now(),
                    str(applied_by),
                    manifest.manifest_hash,
                    manifest.canonical_json,
                ),
            )
            return int(cursor.lastrowid)
    finally:
        conn.close()


def load_manifest(
    path: str | Path | None = None,
    *,
    db_path: str | Path | None = None,
    record_state: bool = True,
    applied_by: str = "operator",
) -> Manifest:
    """Load, validate, hash, and optionally audit a YAML service manifest."""
    source = default_manifest_path() if path is None else Path(path).expanduser()
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ManifestError(f"cannot read service manifest {source}: {exc}") from exc
    except yaml.YAMLError as exc:
        raise ManifestError(f"invalid YAML in service manifest: {exc}") from exc
    manifest = validate_manifest(raw)
    if record_state:
        record_manifest_state(
            manifest,
            db_path=db_path,
            applied_by=applied_by,
        )
    return manifest


__all__ = [
    "Manifest",
    "ManifestError",
    "ServiceSpec",
    "compute_restart_order",
    "default_manifest_path",
    "load_manifest",
    "merge_service_env",
    "record_manifest_state",
    "validate_manifest",
]
