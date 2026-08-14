"""Fail-closed contract tests for workspace lifecycle authority."""
from __future__ import annotations

import json
import multiprocessing
from pathlib import Path

import pytest

from hermes_cli.workspace_lifecycle import (
    Disposition,
    Evidence,
    Registry,
    WorkspaceState,
    build_closeout_manifest,
    classify,
    collect_inventory,
    import_dry_run,
    manager_registry_path,
    remove_exact_path,
)


def _evidence(tmp_path: Path, **overrides) -> Evidence:
    values = dict(
        canonical_path=str(tmp_path / "workspace"),
        repo_common_dir=str(tmp_path / "common"),
        head="a" * 40,
        branch="feature/test",
        status="clean",
        reachable="proven",
        pr="terminal",
        manager_created=True,
        terminal=True,
        lock="none",
        remote_fetch_age_seconds=0,
        mount_state="verified",
        device_state="verified",
        observation_provenance="verified_adapter",
    )
    values.update(overrides)
    return Evidence(**values)


def _receipt_payload(evidence: Evidence, **overrides) -> dict:
    payload = {
        "canonical_path": evidence.canonical_path,
        "repo_common_dir": evidence.repo_common_dir,
        "head": evidence.head,
        "physical_before_bytes": 0,
        "physical_after_bytes": 0,
        "predicate_evidence_hashes": [evidence.observation_hash],
        "recovery_statement": "retain and require owner review after any failure",
        "outcome": "dry_run",
        "error": None,
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("overrides", "disposition", "reason"),
    [
        ({"status": "unreadable"}, Disposition.BLOCKED_REVIEW, "unreadable_status"),
        ({"status": "timeout"}, Disposition.BLOCKED_REVIEW, "unreadable_status"),
        ({"status": "dirty"}, Disposition.BLOCKED_REVIEW, "working_tree_changes"),
        ({"status": "untracked"}, Disposition.BLOCKED_REVIEW, "working_tree_changes"),
        ({"reachable": "local_only"}, Disposition.BLOCKED_REVIEW, "unproven_reachability"),
        ({"pr": "unknown"}, Disposition.BLOCKED_REVIEW, "pr_not_proven_terminal"),
        ({"live_process": True}, Disposition.BLOCKED_REVIEW, "live_process_or_open_handle"),
        ({"hold": True}, Disposition.BLOCKED_REVIEW, "preservation_hold"),
        ({"nested": True}, Disposition.BLOCKED_REVIEW, "nested_workspace"),
        ({"manager_created": False}, Disposition.BLOCKED_REVIEW, "unmanaged_or_unverified_observation"),
    ],
)
def test_classifier_never_allows_unknown_or_risky_evidence(tmp_path, overrides, disposition, reason):
    decision = classify(_evidence(tmp_path, **overrides))
    assert decision.disposition is disposition
    assert reason in decision.reasons


def test_classifier_never_grants_removal_authority_from_caller_bools(tmp_path):
    decision = classify(_evidence(tmp_path))
    assert decision.disposition is Disposition.BLOCKED_REVIEW
    assert decision.state is WorkspaceState.BLOCKED_REVIEW
    assert "manager_removal_authority_unavailable" in decision.reasons


@pytest.mark.parametrize("age, expected_reason", [
    (3_600, None),
    (3_601, "remote_fetch_not_verified"),
    (-1, "remote_fetch_not_verified"),
])
def test_remote_fetch_freshness_has_a_conservative_bounded_window(tmp_path, age, expected_reason):
    decision = classify(_evidence(tmp_path, remote_fetch_age_seconds=age))
    if expected_reason:
        assert expected_reason in decision.reasons
    else:
        assert expected_reason not in decision.reasons


def test_generated_outputs_require_creation_time_declaration(tmp_path):
    decision = classify(_evidence(tmp_path, status="dirty", generated_outputs=("dist/app.js",)))
    assert decision.disposition is Disposition.BLOCKED_REVIEW
    assert "working_tree_changes" in decision.reasons
    assert "undeclared_generated_output" in decision.reasons


def test_registry_is_idempotent_and_receipts_are_immutable(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    evidence = _evidence(tmp_path)
    first = registry.create_or_get(workspace_id="one", idempotency_key="repo:base:intent", evidence=evidence)
    second = registry.create_or_get(workspace_id="two", idempotency_key="repo:base:intent", evidence=evidence)
    assert first["id"] == second["id"] == "one"
    payload = _receipt_payload(evidence)
    receipt = registry.receipt("one", "import", payload)
    assert registry.receipt("one", "import", payload) == receipt
    assert registry.show_receipt(receipt)["payload"] == payload
    with pytest.raises(RuntimeError, match="immutable"):
        registry.receipt("one", "import", _receipt_payload(evidence, error="different"), receipt_hash=receipt)


def test_idempotency_key_refuses_different_workspace_identity(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    registry.create_or_get(workspace_id="one", idempotency_key="same", evidence=_evidence(tmp_path))
    with pytest.raises(RuntimeError, match="idempotency conflict"):
        registry.create_or_get(
            workspace_id="two", idempotency_key="same",
            evidence=_evidence(tmp_path, canonical_path=str(tmp_path / "other")),
        )


def test_reservation_refuses_unmanaged_pre_existing_path(tmp_path):
    occupied = tmp_path / "occupied"
    occupied.mkdir()
    registry = Registry(tmp_path / "registry.sqlite3")

    with pytest.raises(RuntimeError, match="unmanaged pre-existing workspace collision"):
        registry.reserve(
            workspace_id="one",
            idempotency_key="one",
            evidence=_evidence(tmp_path, canonical_path=str(occupied)),
        )

    conn = registry.open()
    try:
        assert conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 0
    finally:
        conn.close()


def test_interrupted_preparing_is_reconciled_to_blocked_review(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    conn = registry.open()
    conn.execute(
        "INSERT INTO workspaces (id, idempotency_key, canonical_path, repo_common_dir, state, disposition, reasons, evidence_hash, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("stuck", "stuck-key", "/tmp/stuck", "/tmp/common", "preparing", "blocked_review", '["preparing"]', "hash", json.dumps({"reservation": {"pid": 999999, "process_started_at": -1, "reserved_at": 0}})),
    )
    conn.close()
    # Reconciliation is explicit and safely detects the mismatched owner identity.
    assert registry.get("stuck")["state"] == "preparing"
    assert registry.reconcile_preparing() == 1
    assert registry.get("stuck")["state"] == "blocked_review"
    assert "interrupted_preparing" in registry.get("stuck")["reasons"]


def test_receipts_reject_unknown_workspace_ids(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    with pytest.raises(RuntimeError, match="orphan receipt"):
        registry.receipt("unknown", "closeout", _receipt_payload(_evidence(tmp_path)))


def test_import_and_inventory_are_read_only_and_fail_closed(tmp_path):
    missing = tmp_path / "missing"
    before = sorted(tmp_path.iterdir())
    report = collect_inventory(missing)
    imported = import_dry_run(missing)
    assert report["disposition"] == "blocked_review"
    assert imported["dry_run"] is True
    assert imported["disposition"] == "blocked_review"
    assert sorted(tmp_path.iterdir()) == before


def test_remove_boundary_never_invokes_git_or_deletes(tmp_path):
    receipt = {"receipt_id": "r", "enable": True, "canonical_path": str(tmp_path)}
    with pytest.raises(RuntimeError, match="disabled in V1"):
        remove_exact_path(receipt)
    assert tmp_path.exists()


def test_serialized_inventory_is_stable(tmp_path):
    evidence = _evidence(tmp_path)
    assert evidence.canonical_json() == evidence.canonical_json()
    assert json.loads(evidence.canonical_json())["canonical_path"] == evidence.canonical_path


@pytest.mark.parametrize("field", ("status", "lock"))
def test_arbitrary_status_or_lock_value_can_never_be_removable(tmp_path, field):
    for value in ("", "future-value", "NONE", "0", "clean "):
        decision = classify(_evidence(tmp_path, **{field: value}))
        assert decision.disposition is not Disposition.REMOVABLE


def test_dirty_nested_or_unmanaged_is_never_labeled_preserved(tmp_path):
    for overrides in ({"status": "dirty"}, {"nested_paths": ("child",)}, {"manager_created": False}):
        assert classify(_evidence(tmp_path, **overrides)).state is not WorkspaceState.PRESERVED


def test_closeout_manifest_is_generic_hashed_and_never_authorizes_apply(tmp_path):
    report = build_closeout_manifest(tmp_path / "missing-repo")

    assert report["operation"] == "closeout_manifest"
    assert report["dry_run"] is True
    assert report["entries"] == []
    assert len(report["manifest_hash"]) == 64
    assert report["apply_available"] is False
    assert report["disposition"] == "blocked_review"


def _race_reservation(registry_path: str, root: str, queue) -> None:
    root_path = Path(root)
    result = Registry(registry_path).create_or_get(
        workspace_id=f"worker-{multiprocessing.current_process().pid}",
        idempotency_key="same-repo-base-intent", evidence=_evidence(root_path),
    )
    queue.put(result["id"])


def test_manager_registry_is_host_owned_not_repository_local(tmp_path):
    assert manager_registry_path(tmp_path) == tmp_path / "workspace-lifecycle" / "registry.sqlite3"
    registry = Registry()
    assert registry.path == manager_registry_path()


def test_schema_mismatch_and_missing_receipt_fields_fail_closed(tmp_path):
    path = tmp_path / "registry.sqlite3"
    conn = __import__("sqlite3").connect(path)
    conn.execute("CREATE TABLE workspace_schema (version INTEGER NOT NULL)")
    conn.execute("INSERT INTO workspace_schema VALUES (999)")
    conn.commit()
    conn.close()
    with pytest.raises(RuntimeError, match="unsupported workspace registry schema"):
        Registry(path).open()

    registry = Registry(tmp_path / "valid.sqlite3")
    evidence = _evidence(tmp_path)
    registry.create_or_get(workspace_id="one", idempotency_key="one", evidence=evidence)
    with pytest.raises(RuntimeError, match="missing required immutable fields"):
        registry.receipt("one", "closeout", {"outcome": "dry_run"})


def test_registry_backup_restores_only_to_fresh_authority(tmp_path):
    evidence = _evidence(tmp_path)
    source = Registry(tmp_path / "source.sqlite3")
    source.create_or_get(workspace_id="one", idempotency_key="one", evidence=evidence)
    backup = source.backup_to(tmp_path / "backup.sqlite3")
    recovered = Registry(tmp_path / "recovered.sqlite3")
    recovered.restore_from_backup(backup)
    assert recovered.get("one")["canonical_path"] == evidence.canonical_path
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        recovered.restore_from_backup(backup)


def test_preparing_crash_recovery_and_validated_transitions(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    evidence = _evidence(tmp_path)
    reserved = registry.reserve(workspace_id="one", idempotency_key="one", evidence=evidence)
    assert reserved["state"] == "preparing"
    # Ordinary observers do not mutate an owning process's live reservation.
    assert registry.get("one")["state"] == "preparing"
    # Simulate a dead/PID-reused owner, then use the explicit leased reconciler.
    conn = registry.open()
    record = json.loads(conn.execute("SELECT record FROM workspaces WHERE id='one'").fetchone()[0])
    record["reservation"]["process_started_at"] = -1
    conn.execute("UPDATE workspaces SET record=? WHERE id='one'", (json.dumps(record),))
    conn.close()
    assert registry.reconcile_preparing() == 1
    assert "interrupted_preparing" in registry.get("one")["reasons"]
    with pytest.raises(RuntimeError, match="invalid workspace state transition"):
        registry.transition("one", WorkspaceState.REMOVED, reason="no direct removal")


def test_generic_transition_api_can_only_tighten_to_blocked_review(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    evidence = _evidence(tmp_path)
    registry.reserve(workspace_id="one", idempotency_key="one", evidence=evidence)

    with pytest.raises(RuntimeError, match="invalid workspace state transition"):
        registry.transition(
            "one", WorkspaceState.ACTIVE, reason="caller cannot activate",
        )
    blocked = registry.transition(
        "one", WorkspaceState.BLOCKED_REVIEW, reason="fail closed",
    )
    assert blocked["state"] == "blocked_review"
    with pytest.raises(RuntimeError, match="invalid workspace state transition"):
        registry.transition(
            "one", WorkspaceState.PRESERVED, reason="caller cannot preserve",
        )

    conn = registry.open()
    try:
        conn.execute(
            "UPDATE workspaces SET state='active' WHERE id='one'",
        )
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="invalid workspace state transition"):
        registry.transition(
            "one", WorkspaceState.TERMINAL_PENDING,
            reason="caller cannot declare terminal",
        )


def test_receipt_requires_current_registered_evidence_and_identity(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    evidence = _evidence(tmp_path)
    registry.create_or_get(
        workspace_id="one", idempotency_key="one", evidence=evidence,
    )

    with pytest.raises(RuntimeError, match="predicate evidence"):
        registry.receipt(
            "one", "closeout",
            _receipt_payload(evidence, predicate_evidence_hashes=[]),
        )
    with pytest.raises(RuntimeError, match="unknown predicate evidence"):
        registry.receipt(
            "one", "closeout",
            _receipt_payload(
                evidence,
                predicate_evidence_hashes=[evidence.observation_hash, "f" * 64],
            ),
        )
    with pytest.raises(RuntimeError, match="canonical_path"):
        registry.receipt(
            "one", "closeout",
            _receipt_payload(evidence, canonical_path=str(tmp_path / "other")),
        )
    with pytest.raises(RuntimeError, match="head does not match"):
        registry.receipt(
            "one", "closeout", _receipt_payload(evidence, head="b" * 40),
        )


def test_receipt_rejects_stale_or_foreign_workspace_evidence(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    first = _evidence(tmp_path)
    second = _evidence(tmp_path, head="b" * 40)
    foreign = _evidence(
        tmp_path,
        canonical_path=str(tmp_path / "foreign"),
        head="c" * 40,
    )
    registry.create_or_get(
        workspace_id="one", idempotency_key="one", evidence=first,
    )
    registry.create_or_get(
        workspace_id="two", idempotency_key="two", evidence=foreign,
    )
    conn = registry.open()
    try:
        Registry._append_observation(conn, "one", second)
        conn.execute(
            "UPDATE workspaces SET evidence_hash=? WHERE id='one'",
            (second.observation_hash,),
        )
    finally:
        conn.close()

    with pytest.raises(RuntimeError, match="current workspace evidence"):
        registry.receipt("one", "closeout", _receipt_payload(first))
    with pytest.raises(RuntimeError, match="belongs to another workspace"):
        registry.receipt(
            "one",
            "closeout",
            _receipt_payload(
                second,
                predicate_evidence_hashes=[
                    second.observation_hash,
                    foreign.observation_hash,
                ],
            ),
        )


def test_two_process_reservation_returns_one_idempotent_record(tmp_path):
    registry_path = str(tmp_path / "race.sqlite3")
    initialized = Registry(registry_path).open()
    initialized.close()
    queue = multiprocessing.Queue()
    workers = [multiprocessing.Process(target=_race_reservation, args=(registry_path, str(tmp_path), queue)) for _ in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(10)
        assert worker.exitcode == 0
    assert len({queue.get(timeout=2), queue.get(timeout=2)}) == 1
    registry = Registry(registry_path)
    conn = registry.open()
    try:
        assert conn.execute("SELECT COUNT(*) FROM workspaces").fetchone()[0] == 1
    finally:
        conn.close()


def test_nonce_bound_lease_heartbeat_and_foreign_lease_are_retain_only(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    registry.create_or_get(workspace_id="one", idempotency_key="one", evidence=_evidence(tmp_path))
    with registry.held_lease("one", reason="test") as lease:
        registry.heartbeat_lease("one", lease["nonce"])
        with pytest.raises(RuntimeError, match="nonce mismatch"):
            registry.heartbeat_lease("one", "foreign")
    conn = registry.open()
    try:
        conn.execute("INSERT INTO workspace_leases VALUES (?, ?, ?, ?, ?, ?)", ("one", "foreign", 999999, 1, 1, "foreign"))
    finally:
        conn.close()
    with pytest.raises(RuntimeError, match="stale or foreign lease"):
        with registry.held_lease("one", reason="recovery"):
            pass
    assert registry.get("one")["state"] == "blocked_review"
    conn = registry.open()
    try:
        assert conn.execute(
            "SELECT COUNT(*) FROM workspace_leases WHERE workspace_id='one'",
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_stale_lease_row_does_not_abort_remaining_reconciliation(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    first = _evidence(tmp_path, canonical_path=str(tmp_path / "first"))
    second = _evidence(tmp_path, canonical_path=str(tmp_path / "second"))
    registry.reserve(workspace_id="one", idempotency_key="one", evidence=first)
    registry.reserve(workspace_id="two", idempotency_key="two", evidence=second)

    conn = registry.open()
    try:
        for workspace_id in ("one", "two"):
            record = json.loads(conn.execute(
                "SELECT record FROM workspaces WHERE id=?", (workspace_id,),
            ).fetchone()[0])
            record["reservation"]["process_started_at"] = -1
            conn.execute(
                "UPDATE workspaces SET record=? WHERE id=?",
                (json.dumps(record), workspace_id),
            )
        conn.execute(
            "INSERT INTO workspace_leases VALUES (?, ?, ?, ?, ?, ?)",
            ("one", "stale", 999999, -1, 1, "crashed reconcile"),
        )
    finally:
        conn.close()

    assert registry.reconcile_preparing() == 1
    assert registry.get("one")["state"] == "blocked_review"
    assert registry.get("two")["state"] == "blocked_review"
    assert "interrupted_preparing" in registry.get("two")["reasons"]
