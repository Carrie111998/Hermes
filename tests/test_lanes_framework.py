from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from hermes_cli.lanes import schema
from hermes_cli.lanes.contracts import LaneTask
from hermes_cli.lanes.errors import LaneModuleNotFound, LaneNotEnabledError
from hermes_cli.lanes.harness import LaneHarness
from hermes_cli.lanes.manifest import (
    LaneManifestError,
    load_manifest,
    validate_manifest,
)
from hermes_cli.lanes.registry import LaneRegistry


def _raw(*, enabled: bool = False, module: str = "missing.dayroute") -> dict:
    return {
        "schema_version": 1,
        "lanes": [
            {
                "lane_id": "dayroute",
                "enabled": enabled,
                "module": module,
                "approval_channel": "dashboard",
                "approval_timeout_hours": 24,
                "per_lane_daily_cost_cap_aud": 3.0,
                "per_lane_daily_task_cap": 50,
                "per_lane_hourly_ingest_cap": 20,
                "publish_enabled": False,
                "description": "test",
            }
        ],
    }


def _manifest(tmp_path: Path, **kwargs) -> Path:
    path = tmp_path / "lane_manifest.yaml"
    path.write_text(yaml.safe_dump(_raw(**kwargs)), encoding="utf-8")
    return path


def _task() -> LaneTask:
    return LaneTask(
        lane_id="dayroute",
        external_id="external-1",
        task_id="task-1",
        payload={"hello": "world"},
    )


def _patch_llm_plumbing(monkeypatch):
    import hermes_cli.lanes.harness as module

    seen = {}

    def route_for_turn(**kwargs):
        seen["route"] = kwargs
        return {
            "provider": "openai-codex",
            "model": "mock-model",
            "fallbacks": [],
            "decision_row_id": 17,
        }

    def caller(**kwargs):
        seen["env"] = json.loads(
            __import__("os").environ["HERMES_ROUTE_CONTEXT_JSON"]
        )
        return {
            "text": "draft",
            "provider": "openai-codex",
            "model": "mock-model",
        }

    monkeypatch.setattr(module, "route_for_turn", route_for_turn)
    monkeypatch.setattr(
        module.route_context,
        "flush_to_db",
        lambda **kwargs: seen.setdefault("flush", kwargs) or True,
    )
    monkeypatch.setattr(
        module,
        "record_call",
        lambda **kwargs: SimpleNamespace(id=21, aud_amount=0.0),
    )
    monkeypatch.setattr(module, "record_dispatch", lambda *a, **k: 22)
    monkeypatch.setattr(module, "record_verdict", lambda *a, **k: 23)
    monkeypatch.setattr(module.rate_limit, "enforce", lambda **kwargs: None)
    return seen, caller


def test_schemas_created_on_first_load(tmp_path):
    db = tmp_path / "kanban.db"
    schema.ensure_migrated(db)
    conn = sqlite3.connect(db)
    names = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    conn.close()
    assert {
        "lane_manifest_state",
        "lane_task",
        "lane_approval_queue",
        "lane_publish_log",
        "lane_rate_limit_state",
        "lane_metric",
    } <= names


def test_schemas_idempotent_reload(tmp_path):
    db = tmp_path / "kanban.db"
    schema.ensure_migrated(db)
    schema.ensure_migrated(db)
    conn = schema.connect(db)
    count = conn.execute(
        "SELECT COUNT(*) FROM lane_manifest_state"
    ).fetchone()[0]
    conn.close()
    assert count == 0


def test_lane_task_unique_lane_id_external_id(tmp_path):
    db = tmp_path / "kanban.db"
    schema.ensure_migrated(db)
    conn = schema.connect(db)
    values = ("dayroute", "same", "2026-01-01T00:00:00Z", "{}")
    conn.execute(
        """INSERT INTO lane_task(
             lane_id,external_id,ingested_at,status,payload_json)
           VALUES(?,?,?,'ingested',?)""",
        values,
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO lane_task(
                 lane_id,external_id,ingested_at,status,payload_json)
               VALUES(?,?,?,'ingested',?)""",
            values,
        )
    conn.close()


def test_lane_task_status_check_constraint(tmp_path):
    db = tmp_path / "kanban.db"
    schema.ensure_migrated(db)
    conn = schema.connect(db)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            """INSERT INTO lane_task(
                 lane_id,external_id,ingested_at,status,payload_json)
               VALUES('dayroute','x','now','INVALID','{}')"""
        )
    conn.close()


def test_manifest_loads_valid_yaml(tmp_path):
    manifest = load_manifest(
        _manifest(tmp_path),
        db_path=tmp_path / "kanban.db",
    )
    assert manifest.lanes[0].lane_id == "dayroute"


def test_manifest_rejects_missing_schema_version():
    raw = _raw()
    raw.pop("schema_version")
    with pytest.raises(LaneManifestError):
        validate_manifest(raw)


def test_manifest_hash_stable_across_key_order():
    raw = _raw()
    reordered = {
        "lanes": [dict(reversed(list(raw["lanes"][0].items())))],
        "schema_version": 1,
    }
    assert (
        validate_manifest(raw).manifest_hash
        == validate_manifest(reordered).manifest_hash
    )


def test_manifest_state_active_row_unique(tmp_path):
    db = tmp_path / "kanban.db"
    path = _manifest(tmp_path)
    load_manifest(path, db_path=db)
    load_manifest(path, db_path=db)
    conn = schema.connect(db)
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM lane_manifest_state WHERE is_active=1"
        ).fetchone()[0]
        == 1
    )
    conn.close()


def test_manifest_hash_change_creates_new_row(tmp_path):
    db = tmp_path / "kanban.db"
    path = _manifest(tmp_path)
    load_manifest(path, db_path=db)
    raw = _raw()
    raw["lanes"][0]["description"] = "changed"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    load_manifest(path, db_path=db)
    conn = schema.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM lane_manifest_state"
    ).fetchone()[0] == 2
    conn.close()


def test_registry_loads_manifest_lanes(tmp_path):
    registry = LaneRegistry(
        manifest_path=_manifest(tmp_path),
        db_path=tmp_path / "kanban.db",
    )
    assert [lane.lane_id for lane in registry.list()] == ["dayroute"]


def test_registry_missing_module_returns_LaneModuleNotFound(tmp_path):
    registry = LaneRegistry(
        manifest_path=_manifest(tmp_path, enabled=True),
        db_path=tmp_path / "kanban.db",
    )
    with pytest.raises(LaneModuleNotFound):
        registry.activate("dayroute")


def test_registry_disabled_lane_returns_LaneNotEnabled(tmp_path):
    registry = LaneRegistry(
        manifest_path=_manifest(tmp_path),
        db_path=tmp_path / "kanban.db",
    )
    with pytest.raises(LaneNotEnabledError):
        registry.activate("dayroute")


def test_registry_reload_after_manifest_change(tmp_path):
    path = _manifest(tmp_path)
    registry = LaneRegistry(
        manifest_path=path,
        db_path=tmp_path / "kanban.db",
    )
    raw = _raw()
    raw["lanes"][0]["description"] = "new description"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    registry.reload()
    assert registry.config("dayroute").description == "new description"


def test_harness_admit_calls_CS_01c_admit_new_turn(tmp_path, monkeypatch):
    import hermes_cli.lanes.harness as module

    seen = {}
    monkeypatch.setattr(
        module,
        "admit_new_turn",
        lambda **kwargs: seen.update(kwargs),
    )
    monkeypatch.setattr(module.rate_limit, "enforce", lambda **kwargs: None)
    harness = LaneHarness(
        lane_id="dayroute",
        db_path=tmp_path / "kanban.db",
        manifest_path=_manifest(tmp_path),
    )
    harness.admit(task=_task())
    assert seen["route"] == "single"
    assert seen["task_id_hint"] == "task-1"


def test_harness_call_llm_uses_route_for_turn_with_lane_param(
    tmp_path, monkeypatch
):
    seen, caller = _patch_llm_plumbing(monkeypatch)
    harness = LaneHarness(
        lane_id="dayroute",
        db_path=tmp_path / "kanban.db",
        manifest_path=_manifest(tmp_path),
        llm_caller=caller,
    )
    harness.call_llm(
        task=_task(), prompt="hello", max_tokens=20, purpose="draft"
    )
    assert seen["route"]["lane"] == "dayroute"
    assert seen["route"]["use_doctrine_reader"] is True


def test_harness_call_llm_installs_HERMES_ROUTE_CONTEXT_JSON(
    tmp_path, monkeypatch
):
    seen, caller = _patch_llm_plumbing(monkeypatch)
    harness = LaneHarness(
        lane_id="dayroute",
        db_path=tmp_path / "kanban.db",
        manifest_path=_manifest(tmp_path),
        llm_caller=caller,
    )
    harness.call_llm(
        task=_task(), prompt="hello", max_tokens=20, purpose="draft"
    )
    assert seen["env"]["decision_row_id"] == 17
    assert seen["env"]["schema_version"] == 1


def test_harness_call_llm_flushes_route_context_at_end(
    tmp_path, monkeypatch
):
    seen, caller = _patch_llm_plumbing(monkeypatch)
    harness = LaneHarness(
        lane_id="dayroute",
        db_path=tmp_path / "kanban.db",
        manifest_path=_manifest(tmp_path),
        llm_caller=caller,
    )
    harness.call_llm(
        task=_task(), prompt="hello", max_tokens=20, purpose="draft"
    )
    assert seen["flush"]["chosen_provider"] == "openai-codex"


def test_harness_call_llm_writes_leaf_verdict_and_cost_ledger(tmp_path):
    from hermes_cli.programme.init import migrate

    db = tmp_path / "kanban.db"
    migrate(db)
    harness = LaneHarness(
        lane_id="dayroute",
        db_path=db,
        manifest_path=_manifest(tmp_path),
        llm_caller=lambda **kwargs: {
            "text": "draft",
            "provider": "openai-codex",
            "model": "gpt-test",
            "input_tokens": 4,
            "output_tokens": 5,
        },
    )
    result = harness.call_llm(
        task=_task(), prompt="hello", max_tokens=20, purpose="draft"
    )
    conn = schema.connect(db)
    assert conn.execute(
        "SELECT COUNT(*) FROM cost_ledger WHERE id=?",
        (result.cost_ledger_id,),
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM leaf_verdicts WHERE id=?",
        (result.verdict_id,),
    ).fetchone()[0] == 1
    conn.close()
