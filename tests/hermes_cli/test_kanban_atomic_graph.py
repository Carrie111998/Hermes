"""Behavior contract for the host-owned weekly atomic Kanban graph facade."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_atomic_graph as atomic


CARD_NAMES = (
    "evidence",
    "implementation",
    "qa",
    "release_note",
    "release_compatibility",
    "recurrence_check",
)


def _digest(value: object) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _envelope(operation: str, **payload: object) -> dict[str, object]:
    material = {
        "schema_version": 1,
        "capability": atomic.CAPABILITY_ID,
        "capability_version": 1,
        "operation": operation,
        "board": atomic.TARGET_BOARD,
        **payload,
    }
    return {**material, "request_digest": _digest(material)}


def _report() -> dict[str, object]:
    material: dict[str, object] = {
        "schema_version": 1,
        "window": {"start": "2026-08-10T00:00:00Z", "end": "2026-08-17T00:00:00Z"},
        "records": [],
        "proposals": [],
        "scorecards": {"boards": [], "profiles": []},
    }
    return {**material, "digest": _digest(material)}


def _graph(report_digest: str, proposal: str = "proposal_aaaaaaaaaaaaaaaaaaaa") -> dict[str, Any]:
    prefix = f"weekly:2026-08-10T00:00:00Z:{proposal}"
    common = {
        "proposal_key": proposal,
        "failure_scope": "failure_scope_bbbbbbbbbbbbbbbbbbbb",
        "collector_report_digest": report_digest,
        "evidence_ids": ["evidence_cccccccccccccccccccc"],
        "expected_metric": "reduced recurrence in the next closed weekly window",
    }
    return {
        "kind": "improvement",
        "idempotency_key": f"bundle:{prefix}",
        "proposal_key": proposal,
        "expected_metric": "reduced recurrence in the next closed weekly window",
        "evidence": {"key": f"{prefix}:evidence", "title": "Weekly evidence/spec: scope", "assignee": "developer", "parents": [], "body": common},
        "implementation": {"key": f"{prefix}:implementation", "title": "Implement weekly hardening: scope", "assignee": "developer", "parents": [f"{prefix}:evidence"], "body": {**common, "routing_reason": "developer_first", "developer_failure_count": 0}},
        "qa": {"key": f"{prefix}:qa", "title": "Independent QA: scope", "assignee": "reviewer_qa", "parents": [f"{prefix}:implementation"], "body": common},
        "release_note": {"key": f"{prefix}:release-notes", "title": "Release note decision: scope", "assignee": "writer_docs", "parents": [f"{prefix}:qa"], "body": common},
        "release_compatibility": {"key": f"{prefix}:release-compatibility", "title": "Release/compatibility tail: scope", "assignee": "developer", "parents": [f"{prefix}:qa", f"{prefix}:release-notes"], "body": common},
        "recurrence_check": {"key": f"{prefix}:recurrence", "title": "Next-window recurrence check: scope", "assignee": "reviewer_qa", "parents": [f"{prefix}:release-compatibility"], "body": {**common, "window_start": "2026-08-17T00:00:00Z"}, "window_start": "2026-08-17T00:00:00Z"},
    }


def _capability(tmp_path: Path) -> tuple[atomic._AtomicGraphCapabilityV1, Path, Path]:
    board_db = tmp_path / "target.db"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = atomic._AtomicGraphCapabilityV1(
        db_path=board_db,
        live_profiles=lambda: ["default", "developer", "reviewer_qa", "writer_docs"],
        workspace_root=workspace,
    )
    return capability, board_db, workspace


def _prepare(capability: atomic._AtomicGraphCapabilityV1, workspace: Path) -> tuple[str, dict[str, Any]]:
    report = _report()
    digest = str(report["digest"])
    verify = capability.execute(
        envelope=_envelope("verify_collector_report", report=report, report_digest=digest),
        principal=atomic.REQUIRED_PRINCIPAL,
        provider=atomic.REQUIRED_PROVIDER,
    )
    assert verify == {"outcome": "confirmed_not_applied", "report_digest": digest}
    preflight = capability.execute(
        envelope=_envelope(
            "preflight",
            report_digest=digest,
            profiles=["developer", "reviewer_qa", "writer_docs"],
            workspace=str(workspace),
        ),
        principal=atomic.REQUIRED_PRINCIPAL,
        provider=atomic.REQUIRED_PROVIDER,
    )
    assert preflight == {"outcome": "confirmed_not_applied", "preflight": "confirmed"}
    return digest, _graph(digest)


def _execute(capability: atomic._AtomicGraphCapabilityV1, envelope: dict[str, object]) -> dict[str, Any]:
    return capability.execute(
        envelope=envelope,
        principal=atomic.REQUIRED_PRINCIPAL,
        provider=atomic.REQUIRED_PROVIDER,
    )


def test_factory_is_disabled_by_default_and_requires_the_pinned_board(monkeypatch, tmp_path):
    monkeypatch.setattr(atomic, "_configured_enabled", lambda: False)
    assert atomic.get_capability_v1() is None

    monkeypatch.setattr(atomic, "_configured_enabled", lambda: True)
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    assert atomic.get_capability_v1() is None


def test_facade_rejects_identity_and_malformed_envelopes_without_writes(tmp_path):
    capability, board_db, workspace = _capability(tmp_path)
    report = _report()
    valid = _envelope("verify_collector_report", report=report, report_digest=report["digest"])
    mutations = [
        ({**valid, "capability_version": 2}, atomic.REQUIRED_PRINCIPAL, atomic.REQUIRED_PROVIDER),
        ({**valid, "board": "other-board"}, atomic.REQUIRED_PRINCIPAL, atomic.REQUIRED_PROVIDER),
        ({**valid, "operation": "run_cli"}, atomic.REQUIRED_PRINCIPAL, atomic.REQUIRED_PROVIDER),
        ({**valid, "request_digest": "b" * 64}, atomic.REQUIRED_PRINCIPAL, atomic.REQUIRED_PROVIDER),
        (valid, "wrong-principal", atomic.REQUIRED_PROVIDER),
        (valid, atomic.REQUIRED_PRINCIPAL, "wrong-provider"),
    ]
    for envelope, principal, provider in mutations:
        result = capability.execute(envelope=envelope, principal=principal, provider=provider)
        assert result == {"outcome": "confirmed_not_applied", "error": {"code": "preflight_rejected"}}
    with sqlite3.connect(board_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM atomic_graph_receipts").fetchone()[0] == 0
    assert workspace.exists()


def test_verify_rejects_a_report_whose_digest_is_not_canonical(tmp_path):
    capability, board_db, _ = _capability(tmp_path)
    report = _report()
    report["window"] = {"start": "changed", "end": "changed"}
    result = _execute(
        capability,
        _envelope("verify_collector_report", report=report, report_digest=report["digest"]),
    )
    assert result == {"outcome": "confirmed_not_applied", "error": {"code": "preflight_rejected"}}
    with sqlite3.connect(board_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_apply_commits_six_cards_links_events_and_receipt_atomically(tmp_path):
    capability, board_db, workspace = _capability(tmp_path)
    report_digest, graph = _prepare(capability, workspace)
    key = "weekly-plan:" + "b" * 64
    apply = _envelope(
        "apply_weekly_graphs",
        report_digest=report_digest,
        profiles=["developer", "reviewer_qa", "writer_docs"],
        workspace=str(workspace),
        idempotency_key=key,
        graphs=[graph],
    )

    result = _execute(capability, apply)

    assert result["outcome"] == "confirmed_applied"
    receipt = result["receipt"]
    assert receipt["stable_bundle_keys"] == [graph["idempotency_key"]]
    assert receipt["graph_task_keys_by_bundle"] == {
        graph["idempotency_key"]: [graph[name]["key"] for name in CARD_NAMES]
    }
    assert receipt["created_task_ids"] == receipt["created_task_ids_by_bundle"][graph["idempotency_key"]]
    assert receipt["idempotent"] is False
    with sqlite3.connect(board_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM task_events WHERE kind = 'created'").fetchone()[0] == 6
        stored = conn.execute(
            "SELECT apply_request_digest, receipt_json FROM atomic_graph_receipts WHERE idempotency_key = ?",
            (key,),
        ).fetchone()
    assert stored[0] == apply["request_digest"]
    assert json.loads(stored[1]) == receipt


def test_exact_replay_is_a_noop_and_digest_collision_is_confirmed_not_applied(tmp_path):
    capability, board_db, workspace = _capability(tmp_path)
    report_digest, graph = _prepare(capability, workspace)
    key = "weekly-plan:" + "b" * 64
    apply = _envelope("apply_weekly_graphs", report_digest=report_digest, profiles=["developer", "reviewer_qa", "writer_docs"], workspace=str(workspace), idempotency_key=key, graphs=[graph])
    first = _execute(capability, apply)
    replay = _execute(capability, deepcopy(apply))
    changed: dict[str, Any] = deepcopy(apply)
    changed["graphs"][0]["evidence"]["title"] = "Different valid title"
    changed["request_digest"] = _digest({k: v for k, v in changed.items() if k != "request_digest"})
    conflict = _execute(capability, changed)

    assert replay == {"outcome": "confirmed_applied", "receipt": {**first["receipt"], "idempotent": True}}
    assert conflict == {"outcome": "confirmed_not_applied", "error": {"code": "idempotency_conflict"}}
    with sqlite3.connect(board_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 6
        assert conn.execute("SELECT COUNT(*) FROM atomic_graph_receipts").fetchone()[0] == 1


def test_reconcile_uses_original_key_and_apply_digest_after_response_loss(tmp_path):
    capability, _, workspace = _capability(tmp_path)
    report_digest, graph = _prepare(capability, workspace)
    key = "weekly-plan:" + "b" * 64
    apply = _envelope("apply_weekly_graphs", report_digest=report_digest, profiles=["developer", "reviewer_qa", "writer_docs"], workspace=str(workspace), idempotency_key=key, graphs=[graph])
    applied = _execute(capability, apply)

    reconcile = _execute(capability, _envelope("reconcile_weekly_graphs", idempotency_key=key, apply_request_digest=apply["request_digest"]))
    wrong_digest = _execute(capability, _envelope("reconcile_weekly_graphs", idempotency_key=key, apply_request_digest="c" * 64))
    missing = _execute(capability, _envelope("reconcile_weekly_graphs", idempotency_key="weekly-plan:" + "d" * 64, apply_request_digest="e" * 64))

    assert reconcile == {"outcome": "confirmed_applied", "receipt": {**applied["receipt"], "idempotent": True}}
    assert wrong_digest == {"outcome": "confirmed_not_applied", "error": {"code": "idempotency_conflict"}}
    assert missing == {"outcome": "confirmed_not_applied"}


def test_injected_crash_before_commit_rolls_back_every_graph_row(tmp_path, monkeypatch):
    capability, board_db, workspace = _capability(tmp_path)
    report_digest, graph = _prepare(capability, workspace)
    original = kb.create_task
    calls = 0

    def crash_on_fourth(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise RuntimeError("private crash marker")
        return original(*args, **kwargs)

    monkeypatch.setattr(kb, "create_task", crash_on_fourth)
    apply = _envelope("apply_weekly_graphs", report_digest=report_digest, profiles=["developer", "reviewer_qa", "writer_docs"], workspace=str(workspace), idempotency_key="weekly-plan:" + "b" * 64, graphs=[graph])
    result = _execute(capability, apply)

    assert result == {"outcome": "confirmed_not_applied", "error": {"code": "preflight_rejected"}}
    assert "private crash marker" not in json.dumps(result)
    with sqlite3.connect(board_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM atomic_graph_receipts").fetchone()[0] == 0


def test_preflight_checks_live_profiles_workspace_and_all_graph_keys_before_writes(tmp_path):
    capability, board_db, workspace = _capability(tmp_path)
    report_digest, graph = _prepare(capability, workspace)
    malformed = deepcopy(graph)
    malformed["qa"]["parents"] = ["unknown-key"]
    apply = _envelope("apply_weekly_graphs", report_digest=report_digest, profiles=["developer", "reviewer_qa", "writer_docs"], workspace=str(workspace), idempotency_key="weekly-plan:" + "b" * 64, graphs=[malformed])
    assert _execute(capability, apply) == {"outcome": "confirmed_not_applied", "error": {"code": "preflight_rejected"}}

    missing_workspace = _envelope("preflight", report_digest=report_digest, profiles=["developer", "reviewer_qa", "writer_docs"], workspace=str(tmp_path / "missing"))
    assert _execute(capability, missing_workspace) == {"outcome": "confirmed_not_applied", "error": {"code": "preflight_rejected"}}
    arbitrary_workspace = tmp_path / "other-existing-directory"
    arbitrary_workspace.mkdir()
    redirected = _envelope("preflight", report_digest=report_digest, profiles=["developer", "reviewer_qa", "writer_docs"], workspace=str(arbitrary_workspace))
    assert _execute(capability, redirected) == {"outcome": "confirmed_not_applied", "error": {"code": "preflight_rejected"}}
    with sqlite3.connect(board_db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0


def test_pinned_board_ignores_ambient_board_current_and_db_overrides(tmp_path, monkeypatch):
    target_dir = tmp_path / "kanban" / "boards" / atomic.TARGET_BOARD
    target_dir.mkdir(parents=True)
    (target_dir / "board.json").write_text("{}", encoding="utf-8")
    ambient_db = tmp_path / "ambient.db"
    monkeypatch.setattr(kb, "kanban_home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "other-board")
    monkeypatch.setenv("HERMES_KANBAN_DB", str(ambient_db))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    capability = atomic._AtomicGraphCapabilityV1(
        live_profiles=lambda: ["developer", "reviewer_qa", "writer_docs"],
        workspace_root=workspace,
    )
    report_digest, graph = _prepare(capability, workspace)
    apply = _envelope("apply_weekly_graphs", report_digest=report_digest, profiles=["developer", "reviewer_qa", "writer_docs"], workspace=str(workspace), idempotency_key="weekly-plan:" + "b" * 64, graphs=[graph])

    assert _execute(capability, apply)["outcome"] == "confirmed_applied"
    assert (target_dir / "kanban.db").exists()
    assert not ambient_db.exists()


def test_receipt_schema_upgrades_legacy_boards_and_concurrent_initialization(tmp_path):
    db_path = tmp_path / "legacy.db"
    initialized = kb.connect(db_path)
    initialized.execute("DROP TABLE atomic_graph_receipts")
    initialized.commit()
    initialized.close()
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    def initialize(_: int) -> None:
        connection = kb.connect(db_path)
        connection.close()

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(initialize, range(8)))

    with sqlite3.connect(db_path) as upgraded:
        columns = {row[1] for row in upgraded.execute("PRAGMA table_info(atomic_graph_receipts)")}
    assert columns == {"idempotency_key", "apply_request_digest", "receipt_json", "created_at"}
