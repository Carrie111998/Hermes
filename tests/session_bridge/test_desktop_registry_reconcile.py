from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from session_bridge.desktop_registry import (
    DESKTOP_REGISTRY_GROUPING_VERSION,
    RegistryBaseline,
    RegistryMutationConflict,
    RegistryScanError,
    apply_registry_mutation,
    build_registry_sync_plan,
    canonical_group_value,
    scan_desktop_registry_roots,
    verify_registry_sync_plan,
)


def _write_record(
    root: Path,
    session_id: str,
    *,
    mtime_ns: int,
    **fields: object,
) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{session_id}.json"
    record = {
        "sessionId": session_id,
        "title": "Original",
        "isArchived": False,
        "lastActivityAt": 1000,
        **fields,
    }
    path.write_text(json.dumps(record), encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _scan(*roots: Path):
    return scan_desktop_registry_roots(roots)


def _root_id(scan, root: Path) -> str:
    expected = str(root.resolve()).casefold()
    return next(
        root_id
        for root_id, observation in scan.roots.items()
        if observation.canonical_path.casefold() == expected
    )


def test_grouping_registry_has_an_explicit_version_and_every_live_field() -> None:
    assert DESKTOP_REGISTRY_GROUPING_VERSION == 1
    record = {
        "sessionId": "local_one",
        "title": "Title",
        "isArchived": False,
        "lastActivityAt": 1,
        "lastFocusedAt": 1,
        "completedTurns": 3,
        "worktreeName": "tree",
        "worktreePath": "C:/tree",
        "sourceBranch": "main",
        "writtenBranches": ["topic"],
        "error": "failed",
        "errorAt": 2,
        "errorCategory": "runtime",
        "priorErrorMark": {"x": 1},
        "prNumber": 1,
        "prRepository": "owner/repo",
        "prState": "open",
        "prUrl": "https://example.test/pr/1",
        "permissionMode": "default",
        "chromePermissionMode": "default",
        "alwaysAllowedReasons": [],
        "sessionPermissionUpdates": [],
        "sessionSettings": {},
        "remoteMcpServersConfig": {},
        "enabledMcpTools": [],
        "backgroundTaskSuggestions": [],
        "resolvedBackgroundTaskSuggestions": [],
        "pendingSystemReminder": None,
    }

    values = canonical_group_value(record)

    assert "identity" not in values
    assert values["worktree"]
    assert values["error-state"]
    assert values["pull-request"]
    assert values["permissions"]
    assert values["mcp"]
    assert values["background-tasks"]
    assert values["field:title"]
    assert values["field:isArchived"]


def test_scan_rejects_filename_session_identity_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "a"
    path = _write_record(root, "local_one", mtime_ns=1)
    record = json.loads(path.read_text(encoding="utf-8"))
    record["sessionId"] = "local_other"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(RegistryScanError, match="identity"):
        _scan(root)


@pytest.mark.parametrize(
    ("raw", "message"),
    (
        ('{"sessionId":"local_one","title":"a","title":"b"}', "duplicate"),
        ('{"sessionId":"local_one","value":NaN}', "constant"),
        ('["local_one"]', "not an object"),
    ),
)
def test_scan_rejects_non_strict_json(
    tmp_path: Path, raw: str, message: str
) -> None:
    root = tmp_path / "a"
    root.mkdir()
    (root / "local_one.json").write_text(raw, encoding="utf-8")

    with pytest.raises(RegistryScanError, match=message):
        _scan(root)


def test_scan_rejects_duplicate_resolved_roots(tmp_path: Path) -> None:
    root = tmp_path / "a"
    root.mkdir()

    with pytest.raises(RegistryScanError, match="duplicate resolved"):
        _scan(root, root)


def test_cli_session_id_collision_does_not_merge_filenames(tmp_path: Path) -> None:
    roots = [tmp_path / name for name in ("a", "b", "c")]
    for index, root in enumerate(roots):
        _write_record(
            root,
            "local_one",
            mtime_ns=10 + index,
            cliSessionId="shared-cli",
        )
        _write_record(
            root,
            "local_two",
            mtime_ns=20 + index,
            cliSessionId="shared-cli",
        )

    plan = build_registry_sync_plan(_scan(*roots), baselines=())

    assert set(plan.records) == {"local_one.json", "local_two.json"}
    assert not plan.conflicts


def test_bootstrap_chooses_unique_newest_record_per_group_not_whole_record(
    tmp_path: Path,
) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(
        a,
        "local_one",
        mtime_ns=100,
        title="Old title",
        isArchived=True,
        destinationOnly="preserve-a",
    )
    _write_record(b, "local_one", mtime_ns=300, title="New title", isArchived=False)
    _write_record(c, "local_one", mtime_ns=200, title="Middle", isArchived=True)

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())
    record = plan.records["local_one.json"]

    assert json.loads(record.desired_groups["field:title"])["value"] == "New title"
    assert json.loads(record.desired_groups["field:isArchived"])["value"] is False
    assert len(record.mutations) == 2
    conflict = next(
        item
        for item in plan.conflicts
        if item.group_name == "unknown:destinationOnly"
    )
    assert conflict.reason == "unknown_field_unclassified"


def test_bootstrap_equal_newest_same_value_is_safe(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old")
    _write_record(b, "local_one", mtime_ns=300, title="Winner")
    _write_record(c, "local_one", mtime_ns=300, title="Winner")

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())

    assert not plan.conflicts
    desired = plan.records["local_one.json"].desired_groups["field:title"]
    assert json.loads(desired)["value"] == "Winner"


def test_bootstrap_equal_newest_different_value_quarantines_without_tiebreak(
    tmp_path: Path,
) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old")
    _write_record(b, "local_one", mtime_ns=300, title="B")
    _write_record(c, "local_one", mtime_ns=300, title="C")

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())

    conflict = next(item for item in plan.conflicts if item.group_name == "field:title")
    assert conflict.reason == "bootstrap_newest_tie"
    assert "field:title" not in plan.records["local_one.json"].desired_groups
    assert all(
        "title" not in mutation.changed_fields
        for mutation in plan.records["local_one.json"].mutations
    )


def test_bootstrap_treats_absent_as_distinct_from_null(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, promptSuggestion="old")
    _write_record(b, "local_one", mtime_ns=300)
    _write_record(c, "local_one", mtime_ns=200, promptSuggestion=None)

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())
    desired = json.loads(
        plan.records["local_one.json"].desired_groups["field:promptSuggestion"]
    )

    assert desired == {"state": "absent"}
    assert any(
        mutation.changed_fields.get("promptSuggestion") == {"state": "absent"}
        for mutation in plan.records["local_one.json"].mutations
    )


def test_steady_state_propagates_manual_archive_then_unarchive(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for root in (a, b, c):
        _write_record(root, "local_one", mtime_ns=100, isArchived=False)
    initial_scan = _scan(a, b, c)
    initial_plan = build_registry_sync_plan(initial_scan, baselines=())
    baseline = initial_plan.proposed_baselines

    path = b / "local_one.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["isArchived"] = True
    path.write_text(json.dumps(record), encoding="utf-8")
    os.utime(path, ns=(200, 200))

    archived_plan = build_registry_sync_plan(_scan(a, b, c), baselines=baseline)
    archived_value = archived_plan.records["local_one.json"].desired_groups[
        "field:isArchived"
    ]
    assert json.loads(archived_value)["value"] is True

    archived_baseline = archived_plan.proposed_baselines
    for root in (a, c):
        other = root / "local_one.json"
        current = json.loads(other.read_text(encoding="utf-8"))
        current["isArchived"] = True
        other.write_text(json.dumps(current), encoding="utf-8")
    unarchived = c / "local_one.json"
    current = json.loads(unarchived.read_text(encoding="utf-8"))
    current["isArchived"] = False
    unarchived.write_text(json.dumps(current), encoding="utf-8")

    unarchived_plan = build_registry_sync_plan(
        _scan(a, b, c), baselines=archived_baseline
    )

    value = unarchived_plan.records["local_one.json"].desired_groups[
        "field:isArchived"
    ]
    assert json.loads(value)["value"] is False
    assert not unarchived_plan.conflicts


def test_steady_state_preserves_unrelated_destination_change(
    tmp_path: Path,
) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for root in (a, b, c):
        _write_record(root, "local_one", mtime_ns=100, title="Base", isStarred=False)
    initial = build_registry_sync_plan(_scan(a, b, c), baselines=())

    source = a / "local_one.json"
    source_record = json.loads(source.read_text(encoding="utf-8"))
    source_record["title"] = "Propagated"
    source.write_text(json.dumps(source_record), encoding="utf-8")
    destination = b / "local_one.json"
    destination_record = json.loads(destination.read_text(encoding="utf-8"))
    destination_record["isStarred"] = True
    destination.write_text(json.dumps(destination_record), encoding="utf-8")

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=initial.proposed_baselines)
    patch_b = next(
        mutation
        for mutation in plan.records["local_one.json"].mutations
        if mutation.root_id == _root_id(plan.scan, b)
    )

    assert json.loads(patch_b.after_bytes)["title"] == "Propagated"
    assert json.loads(patch_b.after_bytes)["isStarred"] is True


def test_steady_state_equal_concurrent_changes_converge(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for root in (a, b, c):
        _write_record(root, "local_one", mtime_ns=100, title="Base")
    initial = build_registry_sync_plan(_scan(a, b, c), baselines=())

    for root in (a, b):
        path = root / "local_one.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["title"] = "Same update"
        path.write_text(json.dumps(record), encoding="utf-8")

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=initial.proposed_baselines)

    assert not plan.conflicts
    desired = plan.records["local_one.json"].desired_groups["field:title"]
    assert json.loads(desired)["value"] == "Same update"
    assert len(plan.records["local_one.json"].mutations) == 1


def test_steady_state_merges_disjoint_replica_changes(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for root in (a, b, c):
        _write_record(root, "local_one", mtime_ns=100, title="Base", isStarred=False)
    initial = build_registry_sync_plan(_scan(a, b, c), baselines=())

    a_record = json.loads((a / "local_one.json").read_text(encoding="utf-8"))
    a_record["title"] = "Changed title"
    (a / "local_one.json").write_text(json.dumps(a_record), encoding="utf-8")
    b_record = json.loads((b / "local_one.json").read_text(encoding="utf-8"))
    b_record["isStarred"] = True
    (b / "local_one.json").write_text(json.dumps(b_record), encoding="utf-8")

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=initial.proposed_baselines)
    desired = plan.records["local_one.json"].desired_groups

    assert json.loads(desired["field:title"])["value"] == "Changed title"
    assert json.loads(desired["field:isStarred"])["value"] is True
    assert not plan.conflicts


def test_new_unknown_key_after_baseline_is_quarantined_and_preserved(
    tmp_path: Path,
) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for root in (a, b, c):
        _write_record(root, "local_one", mtime_ns=100)
    initial = build_registry_sync_plan(_scan(a, b, c), baselines=())

    for root, value in ((a, "from-a"), (b, "from-b")):
        path = root / "local_one.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["futureDesktopField"] = value
        path.write_text(json.dumps(record), encoding="utf-8")

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=initial.proposed_baselines)

    conflict = next(
        item
        for item in plan.conflicts
        if item.group_name == "unknown:futureDesktopField"
    )
    assert conflict.reason == "unknown_field_unclassified"
    assert "unknown:futureDesktopField" not in plan.records[
        "local_one.json"
    ].desired_groups
    assert not plan.records["local_one.json"].mutations
    assert "futureDesktopField" not in json.loads(
        (c / "local_one.json").read_text(encoding="utf-8")
    )
    assert json.loads((a / "local_one.json").read_text(encoding="utf-8"))[
        "futureDesktopField"
    ] == "from-a"
    assert json.loads((b / "local_one.json").read_text(encoding="utf-8"))[
        "futureDesktopField"
    ] == "from-b"


def test_steady_state_protected_linkage_change_is_quarantined(
    tmp_path: Path,
) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for root in (a, b, c):
        _write_record(root, "local_one", mtime_ns=100, cliSessionId="cli-base")
    initial = build_registry_sync_plan(_scan(a, b, c), baselines=())

    path = a / "local_one.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["cliSessionId"] = "cli-new"
    path.write_text(json.dumps(record), encoding="utf-8")

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=initial.proposed_baselines)

    conflict = next(
        item for item in plan.conflicts if item.group_name == "protected:cliSessionId"
    )
    assert conflict.reason == "protected_linkage_divergence"
    assert "protected:cliSessionId" not in plan.records["local_one.json"].desired_groups


def test_steady_state_conflicting_same_group_changes_are_quarantined(
    tmp_path: Path,
) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for root in (a, b, c):
        _write_record(root, "local_one", mtime_ns=100, title="Base")
    initial = build_registry_sync_plan(_scan(a, b, c), baselines=())

    for root, title in ((a, "A"), (b, "B")):
        path = root / "local_one.json"
        record = json.loads(path.read_text(encoding="utf-8"))
        record["title"] = title
        path.write_text(json.dumps(record), encoding="utf-8")

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=initial.proposed_baselines)

    conflict = next(item for item in plan.conflicts if item.group_name == "field:title")
    assert conflict.reason == "concurrent_divergence"
    assert "field:title" not in plan.records["local_one.json"].desired_groups


def test_missing_replica_is_create_only_from_composite(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old")
    _write_record(b, "local_one", mtime_ns=300, title="Newest")
    c.mkdir()

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())
    record = plan.records["local_one.json"]

    assert len(record.mutations) == 2
    create = next(mutation for mutation in record.mutations if mutation.operation == "create")
    assert create.root_id == _root_id(plan.scan, c)
    assert json.loads(create.after_bytes)["title"] == "Newest"


def test_missing_replica_is_not_created_when_any_group_conflicts(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=300, title="A")
    _write_record(b, "local_one", mtime_ns=300, title="B")
    c.mkdir()

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())

    assert plan.conflicts
    assert all(
        mutation.operation != "create"
        for mutation in plan.records["local_one.json"].mutations
    )


def test_protected_cli_session_id_divergence_is_quarantined(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, cliSessionId="cli-a")
    _write_record(b, "local_one", mtime_ns=300, cliSessionId="cli-b")
    _write_record(c, "local_one", mtime_ns=200, cliSessionId="cli-a")

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())

    conflict = next(
        item for item in plan.conflicts if item.group_name == "protected:cliSessionId"
    )
    assert conflict.reason == "protected_linkage_divergence"
    assert "protected:cliSessionId" not in plan.records["local_one.json"].desired_groups


def test_missing_cli_session_id_is_distinct_protected_linkage_value(
    tmp_path: Path,
) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, cliSessionId="cli-a")
    _write_record(b, "local_one", mtime_ns=300)
    _write_record(c, "local_one", mtime_ns=200, cliSessionId="cli-a")

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())

    conflict = next(
        item for item in plan.conflicts if item.group_name == "protected:cliSessionId"
    )
    assert conflict.reason == "protected_linkage_divergence"


def test_unknown_future_key_is_quarantined_without_bootstrap_winner(
    tmp_path: Path,
) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, futureDesktopField={"old": True})
    _write_record(b, "local_one", mtime_ns=300, futureDesktopField={"new": [1, 2]})
    _write_record(c, "local_one", mtime_ns=200)

    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())

    conflict = next(
        item
        for item in plan.conflicts
        if item.group_name == "unknown:futureDesktopField"
    )
    assert conflict.reason == "unknown_field_unclassified"
    assert set(conflict.candidates) == {
        _root_id(plan.scan, a),
        _root_id(plan.scan, b),
        _root_id(plan.scan, c),
    }
    assert "unknown:futureDesktopField" not in plan.records[
        "local_one.json"
    ].desired_groups
    assert not plan.records["local_one.json"].mutations


def test_apply_patch_requires_exact_expected_before_hash(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old")
    _write_record(b, "local_one", mtime_ns=300, title="Winner")
    _write_record(c, "local_one", mtime_ns=200, title="Middle")
    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())
    mutation = next(
        item
        for item in plan.records["local_one.json"].mutations
        if item.root_id == _root_id(plan.scan, a)
    )
    path = a / "local_one.json"
    raced = json.loads(path.read_text(encoding="utf-8"))
    raced["isStarred"] = True
    path.write_text(json.dumps(raced), encoding="utf-8")

    with pytest.raises(RegistryMutationConflict, match="expected hash"):
        apply_registry_mutation(plan.scan, mutation)

    assert json.loads(path.read_text(encoding="utf-8"))["title"] == "Old"
    assert json.loads(path.read_text(encoding="utf-8"))["isStarred"] is True


def test_apply_patch_preserves_fresh_unrelated_fields_when_hash_matches(
    tmp_path: Path,
) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old", isStarred=True)
    _write_record(b, "local_one", mtime_ns=300, title="Winner", isStarred=True)
    _write_record(c, "local_one", mtime_ns=200, title="Middle", isStarred=True)
    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())
    mutation = next(
        item
        for item in plan.records["local_one.json"].mutations
        if item.root_id == _root_id(plan.scan, a)
    )

    result = apply_registry_mutation(plan.scan, mutation)

    assert result.applied
    record = json.loads((a / "local_one.json").read_text(encoding="utf-8"))
    assert record["title"] == "Winner"
    assert record["isStarred"] is True


def test_apply_create_is_create_only(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old")
    _write_record(b, "local_one", mtime_ns=300, title="Winner")
    c.mkdir()
    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())
    mutation = next(
        item
        for item in plan.records["local_one.json"].mutations
        if item.operation == "create"
    )
    destination = c / "local_one.json"
    destination.write_text(
        json.dumps({"sessionId": "local_one", "title": "Desktop won"}),
        encoding="utf-8",
    )

    with pytest.raises(RegistryMutationConflict, match="already exists"):
        apply_registry_mutation(plan.scan, mutation)

    assert json.loads(destination.read_text(encoding="utf-8"))["title"] == "Desktop won"


def test_apply_create_publishes_complete_record(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old")
    _write_record(b, "local_one", mtime_ns=300, title="Winner")
    c.mkdir()
    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())
    mutation = next(
        item
        for item in plan.records["local_one.json"].mutations
        if item.operation == "create"
    )

    result = apply_registry_mutation(plan.scan, mutation)

    assert result.applied
    assert json.loads((c / "local_one.json").read_text(encoding="utf-8"))[
        "title"
    ] == "Winner"
    assert not tuple(c.glob(".*.tmp-*"))


def test_verification_accepts_exact_intended_group_values(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old")
    _write_record(b, "local_one", mtime_ns=300, title="Winner")
    _write_record(c, "local_one", mtime_ns=200, title="Middle")
    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())

    for mutation in plan.records["local_one.json"].mutations:
        root = plan.scan.roots[mutation.root_id].path
        (root / mutation.filename).write_text(mutation.after_bytes, encoding="utf-8")

    verification = verify_registry_sync_plan(plan, _scan(a, b, c))

    assert verification.verified
    assert not verification.failures


def test_verification_rejects_concurrent_change_to_intended_group(
    tmp_path: Path,
) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old")
    _write_record(b, "local_one", mtime_ns=300, title="Winner")
    _write_record(c, "local_one", mtime_ns=200, title="Middle")
    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())

    for mutation in plan.records["local_one.json"].mutations:
        root = plan.scan.roots[mutation.root_id].path
        (root / mutation.filename).write_text(mutation.after_bytes, encoding="utf-8")
    path = a / "local_one.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["title"] = "Desktop raced"
    path.write_text(json.dumps(record), encoding="utf-8")

    verification = verify_registry_sync_plan(plan, _scan(a, b, c))

    assert not verification.verified
    failure = next(item for item in verification.failures if item.group_name == "field:title")
    assert failure.reason == "intended_value_mismatch"


def test_verification_requires_same_root_set(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for root in (a, b, c):
        _write_record(root, "local_one", mtime_ns=100)
    plan = build_registry_sync_plan(_scan(a, b, c), baselines=())
    extra = tmp_path / "d"
    _write_record(extra, "local_one", mtime_ns=100)

    with pytest.raises(ValueError, match="root set"):
        verify_registry_sync_plan(plan, _scan(a, b, c, extra))


def test_second_cycle_after_convergence_is_byte_idempotent(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old")
    _write_record(b, "local_one", mtime_ns=300, title="Winner")
    _write_record(c, "local_one", mtime_ns=200, title="Middle")
    first = build_registry_sync_plan(_scan(a, b, c), baselines=())
    for mutation in first.records["local_one.json"].mutations:
        root = first.scan.roots[mutation.root_id].path
        (root / mutation.filename).write_text(mutation.after_bytes, encoding="utf-8")

    second = build_registry_sync_plan(
        _scan(a, b, c), baselines=first.proposed_baselines
    )

    assert not second.records["local_one.json"].mutations
    assert not second.conflicts


def test_bootstrap_is_independent_of_root_arrival_order(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    _write_record(a, "local_one", mtime_ns=100, title="Old")
    _write_record(b, "local_one", mtime_ns=300, title="Winner")
    _write_record(c, "local_one", mtime_ns=200, title="Middle")

    forward = build_registry_sync_plan(_scan(a, b, c), baselines=())
    reverse = build_registry_sync_plan(_scan(c, b, a), baselines=())

    assert (
        forward.records["local_one.json"].desired_groups
        == reverse.records["local_one.json"].desired_groups
    )
    assert {
        (item.group_name, item.reason, tuple(sorted(item.candidates.values())))
        for item in forward.conflicts
    } == {
        (item.group_name, item.reason, tuple(sorted(item.candidates.values())))
        for item in reverse.conflicts
    }


def test_baseline_requires_every_root_and_group(tmp_path: Path) -> None:
    a, b, c = (tmp_path / name for name in ("a", "b", "c"))
    for root in (a, b, c):
        _write_record(root, "local_one", mtime_ns=100)
    scan = _scan(a, b, c)
    incomplete = (
        RegistryBaseline(
            filename="local_one.json",
            root_id=_root_id(scan, a),
            group_name="field:title",
            value_json='{"state":"present","value":"Original"}',
            revision=1,
        ),
    )

    with pytest.raises(ValueError, match="incomplete baseline"):
        build_registry_sync_plan(scan, baselines=incomplete)
