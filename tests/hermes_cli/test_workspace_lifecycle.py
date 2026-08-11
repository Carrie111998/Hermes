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
    classify,
    compare_post_cleanup_baseline,
    collect_inventory,
    import_dry_run,
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
    receipt = registry.receipt("one", "import", {"a": 1})
    assert registry.receipt("one", "import", {"a": 1}) == receipt
    assert registry.show_receipt(receipt)["payload"] == {"a": 1}
    with pytest.raises(RuntimeError, match="immutable"):
        registry.receipt("one", "import", {"a": 2}, receipt_hash=receipt)


def test_idempotency_key_refuses_different_workspace_identity(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    registry.create_or_get(workspace_id="one", idempotency_key="same", evidence=_evidence(tmp_path))
    with pytest.raises(RuntimeError, match="idempotency conflict"):
        registry.create_or_get(
            workspace_id="two", idempotency_key="same",
            evidence=_evidence(tmp_path, canonical_path=str(tmp_path / "other")),
        )


def test_interrupted_preparing_is_reconciled_to_blocked_review(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    conn = registry.open()
    conn.execute(
        "INSERT INTO workspaces (id, idempotency_key, canonical_path, repo_common_dir, state, disposition, reasons, evidence_hash, record) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("stuck", "stuck-key", "/tmp/stuck", "/tmp/common", "preparing", "blocked_review", '["preparing"]', "hash", "{}"),
    )
    conn.close()
    # A registry reopen performs crash recovery before serving records.
    assert registry.get("stuck")["state"] == "blocked_review"
    assert "interrupted_preparing" in registry.get("stuck")["reasons"]


def test_receipts_reject_unknown_workspace_ids(tmp_path):
    registry = Registry(tmp_path / "registry.sqlite3")
    with pytest.raises(RuntimeError, match="orphan receipt"):
        registry.receipt("unknown", "closeout", {"outcome": "dry_run"})


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


def test_baseline_comparison_is_observation_only_and_reports_each_missing_row(tmp_path):
    baseline = tmp_path / "baseline.md"
    rows = [f"- `{'a' * 40}` `{tmp_path / f'legacy-{i}'}`" for i in range(54)]
    manifest = [f"- `{tmp_path / f'legacy-{i}'}`" for i in range(54)]
    baseline.write_text("\n".join(rows + ["", "## `evidence_update_pr_or_nonterminal` (54)", ""] + manifest) + "\n", encoding="utf-8")
    before = baseline.read_bytes()

    report = compare_post_cleanup_baseline(tmp_path / "missing-repo", baseline)

    assert report["dry_run"] is True
    assert report["expected_registrations"] == 54
    assert len(report["comparisons"]) == 54
    assert all(item["differences"] == ["missing_registration"] for item in report["comparisons"])
    assert baseline.read_bytes() == before
