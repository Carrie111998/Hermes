from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from hermes_cli.service import schema
from hermes_cli.service.manifest import (
    ManifestError,
    compute_restart_order,
    load_manifest,
    merge_service_env,
    record_manifest_state,
    validate_manifest,
)


def _service(
    service_id: str = "alpha",
    *,
    depends_on: list[str] | None = None,
    tags: list[str] | None = None,
    health_type: str = "pid_alive",
) -> dict:
    health: dict = {"type": health_type, "timeout_seconds": 1}
    if health_type == "http":
        health.update(url="http://127.0.0.1:9/", expected_status=200)
    if health_type == "exec":
        health.update(command=["python", "-c", "print('ok')"])
    return {
        "id": service_id,
        "name": service_id.title(),
        "pid_file": f"/tmp/{service_id}.pid",
        "command": ["/usr/bin/true"],
        "working_dir": "/tmp",
        "env": {},
        "health_check": health,
        "drain_timeout_seconds": 1,
        "start_timeout_seconds": 1,
        "depends_on": depends_on or [],
        "tags": tags or [],
    }


def _document(*services: dict) -> dict:
    return {"schema_version": 1, "services": list(services)}


def _write(path: Path, document: dict) -> Path:
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def test_manifest_loads_valid_yaml(tmp_path: Path) -> None:
    path = _write(tmp_path / "services.yaml", _document(_service()))
    manifest = load_manifest(path, record_state=False)
    assert manifest.schema_version == 1
    assert [service.id for service in manifest.services] == ["alpha"]


def test_manifest_rejects_missing_schema_version() -> None:
    with pytest.raises(ManifestError, match="schema_version"):
        validate_manifest({"services": [_service()]})


def test_manifest_rejects_duplicate_service_ids() -> None:
    with pytest.raises(ManifestError, match="duplicate"):
        validate_manifest(_document(_service(), _service()))


def test_manifest_rejects_unknown_health_check_type() -> None:
    with pytest.raises(ManifestError, match="unknown health"):
        validate_manifest(
            _document(_service(health_type="carrier_pigeon"))
        )


def test_manifest_hash_is_stable_across_key_ordering() -> None:
    first = _document(_service())
    reordered_service = dict(reversed(list(_service().items())))
    second = {
        "services": [reordered_service],
        "schema_version": 1,
    }
    assert validate_manifest(first).manifest_hash == validate_manifest(
        second
    ).manifest_hash


def test_topsort_respects_depends_on() -> None:
    manifest = validate_manifest(
        _document(
            _service("child", depends_on=["parent"]),
            _service("parent"),
        )
    )
    assert [item.id for item in compute_restart_order(manifest)] == [
        "parent",
        "child",
    ]


def test_topsort_breaks_ties_by_tag_priority() -> None:
    manifest = validate_manifest(
        _document(
            _service("worker", tags=["worker"]),
            _service("gateway", tags=["gateway"]),
            _service("critical", tags=["critical"]),
        )
    )
    assert [item.id for item in compute_restart_order(manifest)] == [
        "critical",
        "gateway",
        "worker",
    ]


def test_topsort_detects_cycle_and_raises() -> None:
    with pytest.raises(ManifestError, match="cycle"):
        validate_manifest(
            _document(
                _service("alpha", depends_on=["beta"]),
                _service("beta", depends_on=["alpha"]),
            )
        )


def test_topsort_all_independent_services_stable_order() -> None:
    manifest = validate_manifest(
        _document(
            _service("zeta", tags=["worker"]),
            _service("alpha", tags=["worker"]),
            _service("middle", tags=["worker"]),
        )
    )
    assert [item.id for item in compute_restart_order(manifest)] == [
        "zeta",
        "alpha",
        "middle",
    ]


def test_manifest_state_row_created_on_first_load(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    manifest = validate_manifest(_document(_service()))
    row_id = record_manifest_state(manifest, db_path=db_path)
    conn = schema.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, version, is_active FROM service_manifest_state"
        ).fetchone()
    finally:
        conn.close()
    assert (row["id"], row["version"], row["is_active"]) == (
        row_id,
        1,
        1,
    )


def test_manifest_state_marks_only_one_active(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    first = validate_manifest(_document(_service("alpha")))
    second = validate_manifest(_document(_service("beta")))
    record_manifest_state(first, db_path=db_path)
    record_manifest_state(second, db_path=db_path)
    conn = schema.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT manifest_hash, is_active FROM service_manifest_state "
            "ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    assert [row["is_active"] for row in rows] == [0, 1]
    assert rows[1]["manifest_hash"] == second.manifest_hash


def test_manifest_state_hash_change_creates_new_row(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    first = validate_manifest(_document(_service("alpha")))
    changed = _service("alpha")
    changed["name"] = "Changed"
    second = validate_manifest(_document(changed))
    record_manifest_state(first, db_path=db_path)
    record_manifest_state(second, db_path=db_path)
    conn = schema.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM service_manifest_state"
        ).fetchone()["count"]
    finally:
        conn.close()
    assert count == 2


def test_manifest_state_hash_unchanged_reuses_active(tmp_path: Path) -> None:
    db_path = tmp_path / "kanban.db"
    manifest = validate_manifest(_document(_service()))
    first = record_manifest_state(manifest, db_path=db_path)
    second = record_manifest_state(manifest, db_path=db_path)
    conn = schema.connect(db_path)
    try:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM service_manifest_state"
        ).fetchone()["count"]
    finally:
        conn.close()
    assert first == second
    assert count == 1


def test_manifest_env_merge_with_parent() -> None:
    raw = _service()
    raw["env"] = {"SHARED": "service", "ONLY_SERVICE": "yes"}
    service = validate_manifest(_document(raw)).services[0]
    merged = merge_service_env(
        service,
        {"SHARED": "parent", "ONLY_PARENT": "yes"},
    )
    assert merged == {
        "SHARED": "service",
        "ONLY_PARENT": "yes",
        "ONLY_SERVICE": "yes",
    }
    assert json.loads(
        validate_manifest(_document(raw)).canonical_json
    )["schema_version"] == 1
