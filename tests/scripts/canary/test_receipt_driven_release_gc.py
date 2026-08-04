from __future__ import annotations

import hashlib
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from scripts.canary import receipt_driven_release_gc as gc


@pytest.fixture(autouse=True)
def _local_lifecycle(monkeypatch):
    monkeypatch.setattr(gc, "host_release_lifecycle_lock", lambda: nullcontext())
    if not sys.platform.startswith("linux"):
        monkeypatch.setattr(
            gc,
            "_rename_noreplace",
            lambda old_fd, old, new_fd, new: os.rename(
                old,
                new,
                src_dir_fd=old_fd,
                dst_dir_fd=new_fd,
            ),
        )


def _sha(index: int) -> str:
    return f"{index:040x}"


def _canonical(value: dict) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        + b"\n"
    )


def _layout(tmp_path: Path, **kwargs) -> gc.GCLayout:
    releases = tmp_path / "releases"
    sources = tmp_path / "sources"
    evidence = tmp_path / "evidence"
    releases.mkdir()
    sources.mkdir()
    evidence.mkdir()
    trusted_uid = kwargs.pop("trusted_uid", os.geteuid())
    trusted_gid = kwargs.pop("trusted_gid", os.getegid())
    validate_parent_chain = kwargs.pop("validate_parent_chain", False)
    return gc.GCLayout(
        release_base=releases,
        source_base=sources,
        evidence_base=evidence,
        trusted_uid=trusted_uid,
        trusted_gid=trusted_gid,
        validate_parent_chain=validate_parent_chain,
        **kwargs,
    )


def _terminal_receipt(
    layout: gc.GCLayout,
    revision: str,
    created_at_unix: int,
    *,
    state: str = "published_services_stopped",
    ok: bool = True,
    services_stopped_and_disabled: bool = True,
) -> Path:
    path = layout.evidence_base / revision / gc.RECEIPT_NAME
    path.parent.mkdir(parents=True)
    manifest_unsigned = {
        "schema": gc.RELEASE_MANIFEST_SCHEMA,
        "revision": revision,
        "artifact_root": str(layout.release_base / revision),
        "python_version": "3.11.15",
        "interpreter": str(layout.release_base / revision / "bin/python"),
        "writer_module": "gateway.canonical_writer",
        "writer_module_origin": str(layout.release_base / revision / "writer.py"),
        "gateway_module": "gateway.run",
        "gateway_module_origin": str(layout.release_base / revision / "gateway.py"),
        "entries": [],
    }
    manifest = {
        **manifest_unsigned,
        "artifact_sha256": hashlib.sha256(
            json.dumps(
                manifest_unsigned,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest(),
    }
    manifest_path = layout.release_base / revision / gc.MANIFEST_NAME
    manifest_raw = _canonical(manifest)
    if manifest_path.parent.is_dir():
        manifest_path.write_bytes(manifest_raw)
        manifest_path.chmod(0o400)
    unsigned = {
        "schema": gc.STOPPED_RECEIPT_SCHEMA,
        "ok": ok,
        "state": state,
        "services_stopped_and_disabled": services_stopped_and_disabled,
        "release_revision": revision,
        "release_root": str(layout.release_base / revision),
        "release_manifest_path": str(manifest_path),
        "release_manifest_file_sha256": hashlib.sha256(manifest_raw).hexdigest(),
        "release_artifact_sha256": manifest["artifact_sha256"],
        "receipt_path": str(path),
        "source": {
            "repository": gc.FORK_REPOSITORY,
            "root": str(layout.source_base / revision),
            "head_sha": revision,
            "tree_sha": _sha(900),
        },
        "service_state_before": [{"unit": "canary", "state": "absent"}],
        "service_state_after": [{"unit": "canary", "state": "absent"}],
        "host_identity_receipt_path": "/var/lib/muncho-canary/host.json",
        "host_identity_receipt_file_sha256": "d" * 64,
        "host_identity_receipt_sha256": "e" * 64,
        "created_at_unix": created_at_unix,
    }
    receipt = {**unsigned, "receipt_sha256": gc._sha256_json(unsigned)}
    path.write_bytes(_canonical(receipt))
    path.chmod(0o400)
    return path


def _pair(
    layout: gc.GCLayout,
    revision: str,
    created_at_unix: int,
    **receipt_kwargs,
) -> Path:
    release = layout.release_base / revision
    source = layout.source_base / revision
    release.mkdir()
    source.mkdir()
    (release / "artifact").write_text(revision, encoding="ascii")
    (source / "checkout").write_text(revision, encoding="ascii")
    return _terminal_receipt(
        layout,
        revision,
        created_at_unix,
        **receipt_kwargs,
    )


def _unit(plan: dict, revision: str) -> dict:
    return next(unit for unit in plan["units"] if unit["revision"] == revision)


def _protection_inventory(path: Path, *, current, previous, protected) -> Path:
    unsigned = {
        "schema": gc.PROTECTION_INVENTORY_SCHEMA,
        "current_links": [str(item) for item in current],
        "previous_links": [str(item) for item in previous],
        "protected_refs": [str(item) for item in protected],
    }
    value = {**unsigned, "inventory_sha256": gc._sha256_json(unsigned)}
    path.write_bytes(_canonical(value))
    return path


def test_plan_is_dry_run_and_retains_latest_three_terminal_releases(tmp_path):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 6)]
    for created, revision in enumerate(revisions, start=10):
        _pair(layout, revision, created)

    plan = gc.build_plan(layout, production_sha=_sha(99))

    assert plan["schema"] == gc.PLAN_SCHEMA
    assert plan["evidence_deletion_enabled"] is False
    assert plan["protected"]["newest_terminal_revisions"] == sorted(revisions[-3:])
    assert [_unit(plan, revision)["action"] for revision in revisions[:2]] == [
        "delete_pair",
        "delete_pair",
    ]
    assert all(
        "newest_terminal_retention" in _unit(plan, revision)["reasons"]
        for revision in revisions[-3:]
    )
    assert all((layout.release_base / revision).is_dir() for revision in revisions)
    assert all((layout.source_base / revision).is_dir() for revision in revisions)


def test_production_links_and_structured_refs_protect_exact_revisions(tmp_path):
    current = tmp_path / "current"
    previous = tmp_path / "previous"
    protected_ref = tmp_path / "pending-owner-cutover.json"
    layout = _layout(
        tmp_path,
        current_links=(current,),
        previous_links=(previous,),
        protected_refs=(protected_ref,),
    )
    production, current_sha, previous_sha, ref_sha, candidate = (
        _sha(index) for index in range(1, 6)
    )
    for index, revision in enumerate(
        (production, current_sha, previous_sha, ref_sha, candidate),
        start=1,
    ):
        _pair(layout, revision, index)
    current.symlink_to(layout.release_base / current_sha)
    previous.symlink_to(layout.source_base / previous_sha)
    protected_ref.write_bytes(
        _canonical({
            "nested": [
                {"identity": ref_sha},
                {"path": str(layout.release_base / ref_sha)},
            ]
        })
    )

    plan = gc.build_plan(layout, production_sha=production)

    assert "production_sha" in _unit(plan, production)["reasons"]
    assert "current_or_previous_target" in _unit(plan, current_sha)["reasons"]
    assert "current_or_previous_target" in _unit(plan, previous_sha)["reasons"]
    assert "pending_owner_or_cutover_ref" in _unit(plan, ref_sha)["reasons"]
    # This unit is also among the latest three; no protected identity is ever
    # made deletable merely because protection reasons overlap.
    assert _unit(plan, candidate)["action"] == "preserve"


def test_missing_nonterminal_invalid_and_incomplete_units_are_preserved(tmp_path):
    layout = _layout(tmp_path)
    no_receipt = _sha(1)
    nonterminal = _sha(2)
    invalid_digest = _sha(3)
    release_only = _sha(4)
    source_only = _sha(5)

    (layout.release_base / no_receipt).mkdir()
    (layout.source_base / no_receipt).mkdir()
    _pair(layout, nonterminal, 2, state="building")
    invalid_path = _pair(layout, invalid_digest, 3)
    invalid = json.loads(invalid_path.read_text(encoding="utf-8"))
    invalid["receipt_sha256"] = "f" * 64
    invalid_path.chmod(0o600)
    invalid_path.write_bytes(_canonical(invalid))
    invalid_path.chmod(0o400)
    (layout.release_base / release_only).mkdir()
    _terminal_receipt(layout, release_only, 4)
    (layout.source_base / source_only).mkdir()
    _terminal_receipt(layout, source_only, 5)

    plan = gc.build_plan(layout, production_sha=_sha(99))

    for revision in (no_receipt, nonterminal, invalid_digest):
        assert _unit(plan, revision)["action"] == "preserve"
        assert "receipt_absent_or_nonterminal" in _unit(plan, revision)["reasons"]
    for revision in (release_only, source_only):
        assert _unit(plan, revision)["action"] == "preserve"
        assert "release_source_pair_incomplete" in _unit(plan, revision)["reasons"]


def test_incomplete_or_invalid_newer_units_do_not_consume_retention_slots(tmp_path):
    layout = _layout(tmp_path)
    complete = [_sha(index) for index in range(1, 6)]
    for created, revision in enumerate(complete, start=1):
        _pair(layout, revision, created)
    release_only = _sha(20)
    (layout.release_base / release_only).mkdir()
    _terminal_receipt(layout, release_only, 100)
    invalid_newest = _sha(21)
    invalid_path = _pair(layout, invalid_newest, 101)
    invalid_value = json.loads(invalid_path.read_text(encoding="utf-8"))
    invalid_value["receipt_sha256"] = "f" * 64
    invalid_path.chmod(0o600)
    invalid_path.write_bytes(_canonical(invalid_value))
    invalid_path.chmod(0o400)

    plan = gc.build_plan(layout, production_sha=_sha(99))

    assert plan["protected"]["newest_complete_terminal_pairs"] == sorted(complete[-3:])
    assert [_unit(plan, revision)["action"] for revision in complete[:2]] == [
        "delete_pair",
        "delete_pair",
    ]
    assert _unit(plan, release_only)["action"] == "preserve"
    assert _unit(plan, invalid_newest)["action"] == "preserve"


def test_release_manifest_anchor_must_match_trusted_producer_receipt(tmp_path):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    tampered = revisions[-1]
    manifest_path = layout.release_base / tampered / gc.MANIFEST_NAME
    manifest_path.chmod(0o600)
    manifest_path.write_bytes(_canonical({"schema": "tampered"}))
    manifest_path.chmod(0o400)

    plan = gc.build_plan(layout, production_sha=_sha(99))

    unit = _unit(plan, tampered)
    assert unit["action"] == "preserve"
    assert "receipt_absent_or_nonterminal" in unit["reasons"]


def test_nested_mount_boundary_marks_unit_invalid_and_preserves_it(
    tmp_path, monkeypatch
):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    nested = layout.release_base / revisions[0] / "nested-mount"
    nested.mkdir()
    nested_inode = os.lstat(nested).st_ino
    original_mount_id = gc._mount_id

    def mount_id(fd):
        if os.fstat(fd).st_ino == nested_inode:
            return "test:nested-mount"
        return original_mount_id(fd)

    monkeypatch.setattr(gc, "_mount_id", mount_id)

    plan = gc.build_plan(layout, production_sha=_sha(99))

    unit = _unit(plan, revisions[0])
    assert unit["action"] == "preserve"
    assert "invalid_release_or_source_entry" in unit["reasons"]


def test_apply_requires_current_exact_plan_digest_before_mutation(tmp_path):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions):
        _pair(layout, revision, created)
    plan = gc.build_plan(layout, production_sha=_sha(99))
    candidate = revisions[0]
    assert _unit(plan, candidate)["action"] == "delete_pair"

    with pytest.raises(PermissionError, match="does not match"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256="f" * 64,
            require_root_linux=False,
        )

    assert (layout.release_base / candidate).is_dir()
    assert (layout.source_base / candidate).is_dir()


def test_apply_fails_closed_before_posix_uid_lookup_off_linux(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gc.sys, "platform", "win32")
    monkeypatch.delattr(gc.os, "geteuid")

    with pytest.raises(PermissionError, match="requires root on Linux"):
        gc.apply_plan(
            gc.GCLayout(),
            production_sha="a" * 40,
            approved_plan_sha256="b" * 64,
        )


def test_apply_rejects_new_pending_ref_after_plan(tmp_path):
    protected_ref = tmp_path / "pending.json"
    protected_ref.write_bytes(_canonical({"revisions": []}))
    layout = _layout(tmp_path, protected_refs=(protected_ref,))
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions):
        _pair(layout, revision, created)
    candidate = revisions[0]
    plan = gc.build_plan(layout, production_sha=_sha(99))
    assert _unit(plan, candidate)["action"] == "delete_pair"
    protected_ref.write_bytes(_canonical({"revision": candidate}))

    with pytest.raises(PermissionError, match="does not match"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )

    assert (layout.release_base / candidate).is_dir()
    assert (layout.source_base / candidate).is_dir()


def test_integration_apply_deletes_only_release_source_unit_and_keeps_evidence(
    tmp_path,
):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 6)]
    receipts = {}
    for created, revision in enumerate(revisions, start=1):
        receipts[revision] = _pair(layout, revision, created)
    plan = gc.build_plan(layout, production_sha=_sha(99))
    candidates = [
        unit["revision"] for unit in plan["units"] if unit["action"] == "delete_pair"
    ]
    assert candidates == revisions[:2]

    result = gc.apply_plan(
        layout,
        production_sha=_sha(99),
        approved_plan_sha256=plan["plan_sha256"],
        require_root_linux=False,
    )

    assert result["ok"] is True
    assert result["removed_release_source_pairs"] == revisions[:2]
    assert result["evidence_deleted"] is False
    for revision in revisions[:2]:
        assert not (layout.release_base / revision).exists()
        assert not (layout.source_base / revision).exists()
        assert receipts[revision].is_file()
        assert hashlib.sha256(receipts[revision].read_bytes()).hexdigest()
    for revision in revisions[-3:]:
        assert (layout.release_base / revision).is_dir()
        assert (layout.source_base / revision).is_dir()
        assert receipts[revision].is_file()


def test_apply_resumes_idempotent_physical_purge_after_crash(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    candidate = revisions[0]
    plan = gc.build_plan(layout, production_sha=_sha(99))
    assert _unit(plan, candidate)["action"] == "delete_pair"
    original_purge = gc._purge_tree_at
    calls = 0

    def crash_after_first_purge(root, name, expected_anchor):
        nonlocal calls
        original_purge(root, name, expected_anchor)
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated crash after first physical purge")

    monkeypatch.setattr(gc, "_purge_tree_at", crash_after_first_purge)
    with pytest.raises(RuntimeError, match="simulated crash"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )

    evidence = layout.evidence_base / candidate
    assert (evidence / gc.INTENT_NAME).is_file()
    assert (evidence / gc.LOGICAL_DELETE_NAME).is_file()
    assert not (evidence / gc.PURGE_NAME).exists()
    monkeypatch.setattr(gc, "_purge_tree_at", original_purge)

    result = gc.apply_plan(
        layout,
        production_sha=_sha(99),
        approved_plan_sha256=plan["plan_sha256"],
        require_root_linux=False,
    )

    assert result["removed_release_source_pairs"] == [candidate]
    assert not (layout.release_base / candidate).exists()
    assert not (layout.source_base / candidate).exists()
    assert (evidence / gc.PURGE_NAME).is_file()


def test_apply_resumes_after_crash_between_no_replace_tombstones(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    candidate = revisions[0]
    plan = gc.build_plan(layout, production_sha=_sha(99))
    original_rename = gc._rename_noreplace
    calls = 0

    def crash_on_second_rename(old_fd, old, new_fd, new):
        nonlocal calls
        if old == candidate:
            calls += 1
            if calls == 2:
                raise RuntimeError("simulated crash between tombstones")
        original_rename(old_fd, old, new_fd, new)

    monkeypatch.setattr(gc, "_rename_noreplace", crash_on_second_rename)
    with pytest.raises(RuntimeError, match="between tombstones"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )

    evidence = layout.evidence_base / candidate
    assert (evidence / gc.INTENT_NAME).is_file()
    assert not (evidence / gc.LOGICAL_DELETE_NAME).exists()
    monkeypatch.setattr(gc, "_rename_noreplace", original_rename)

    result = gc.apply_plan(
        layout,
        production_sha=_sha(99),
        approved_plan_sha256=plan["plan_sha256"],
        require_root_linux=False,
    )

    assert result["removed_release_source_pairs"] == [candidate]
    assert (evidence / gc.LOGICAL_DELETE_NAME).is_file()
    assert (evidence / gc.PURGE_NAME).is_file()


def test_apply_resumes_remaining_units_from_durable_plan_anchor(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 6)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    plan = gc.build_plan(layout, production_sha=_sha(99))
    candidates = [
        unit["revision"] for unit in plan["units"] if unit["action"] == "delete_pair"
    ]
    assert candidates == revisions[:2]
    original_apply = gc._apply_unit
    calls = 0

    def crash_after_first_complete(layout_arg, unit, approved):
        nonlocal calls
        original_apply(layout_arg, unit, approved)
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated crash after first unit")

    monkeypatch.setattr(gc, "_apply_unit", crash_after_first_complete)
    with pytest.raises(RuntimeError, match="after first unit"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )

    anchor = layout.evidence_base / gc._plan_anchor_name(plan["plan_sha256"])
    assert anchor.is_file()
    assert not (layout.release_base / candidates[0]).exists()
    assert (layout.release_base / candidates[1]).is_dir()
    monkeypatch.setattr(gc, "_apply_unit", original_apply)

    result = gc.apply_plan(
        layout,
        production_sha=_sha(99),
        approved_plan_sha256=plan["plan_sha256"],
        require_root_linux=False,
    )

    assert result["removed_release_source_pairs"] == candidates
    assert all(not (layout.release_base / item).exists() for item in candidates)
    assert all(not (layout.source_base / item).exists() for item in candidates)


def test_retry_distinguishes_complete_unit_and_protects_remaining_unit(
    tmp_path, monkeypatch
):
    protected_ref = tmp_path / "pending.json"
    protected_ref.write_bytes(_canonical({"revisions": []}))
    layout = _layout(tmp_path, protected_refs=(protected_ref,))
    revisions = [_sha(index) for index in range(1, 6)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    plan = gc.build_plan(layout, production_sha=_sha(99))
    candidates = [
        unit["revision"] for unit in plan["units"] if unit["action"] == "delete_pair"
    ]
    original_apply = gc._apply_unit
    calls = 0

    def crash_after_first_complete(layout_arg, unit, approved):
        nonlocal calls
        original_apply(layout_arg, unit, approved)
        calls += 1
        if calls == 1:
            raise RuntimeError("simulated crash after first unit")

    monkeypatch.setattr(gc, "_apply_unit", crash_after_first_complete)
    with pytest.raises(RuntimeError, match="after first unit"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )
    protected_ref.write_bytes(_canonical({"revisions": [candidates[1]]}))
    monkeypatch.setattr(gc, "_apply_unit", original_apply)

    with pytest.raises(RuntimeError, match="became protected"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )
    assert not (layout.release_base / candidates[0]).exists()
    assert (layout.evidence_base / candidates[0] / gc.PURGE_NAME).is_file()
    assert (layout.release_base / candidates[1]).is_dir()
    assert (layout.source_base / candidates[1]).is_dir()


def test_receipt_candidate_recovers_after_fsync_before_noreplace(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    plan = gc.build_plan(layout, production_sha=_sha(99))
    original_rename = gc._rename_noreplace
    crashed = False

    def crash_intent_publication(old_fd, old, new_fd, new):
        nonlocal crashed
        if new == gc.INTENT_NAME and not crashed:
            crashed = True
            raise RuntimeError("simulated crash before receipt rename")
        original_rename(old_fd, old, new_fd, new)

    monkeypatch.setattr(gc, "_rename_noreplace", crash_intent_publication)
    with pytest.raises(RuntimeError, match="before receipt rename"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )
    evidence = layout.evidence_base / revisions[0]
    assert not (evidence / gc.INTENT_NAME).exists()
    assert any(path.name.endswith(".candidate") for path in evidence.iterdir())

    monkeypatch.setattr(gc, "_rename_noreplace", original_rename)
    result = gc.apply_plan(
        layout,
        production_sha=_sha(99),
        approved_plan_sha256=plan["plan_sha256"],
        require_root_linux=False,
    )

    assert result["removed_release_source_pairs"] == [revisions[0]]
    assert (evidence / gc.INTENT_NAME).is_file()
    assert not any(path.name.endswith(".candidate") for path in evidence.iterdir())


def test_approved_plan_anchor_candidate_recovers_before_any_mutation(
    tmp_path, monkeypatch
):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    plan = gc.build_plan(layout, production_sha=_sha(99))
    anchor_name = gc._plan_anchor_name(plan["plan_sha256"])
    original_rename = gc._rename_noreplace
    crashed = False

    def crash_anchor_publication(old_fd, old, new_fd, new):
        nonlocal crashed
        if new == anchor_name and not crashed:
            crashed = True
            raise RuntimeError("simulated crash before plan-anchor rename")
        original_rename(old_fd, old, new_fd, new)

    monkeypatch.setattr(gc, "_rename_noreplace", crash_anchor_publication)
    with pytest.raises(RuntimeError, match="plan-anchor rename"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )
    assert not (layout.evidence_base / anchor_name).exists()
    assert (layout.release_base / revisions[0]).is_dir()

    monkeypatch.setattr(gc, "_rename_noreplace", original_rename)
    result = gc.apply_plan(
        layout,
        production_sha=_sha(99),
        approved_plan_sha256=plan["plan_sha256"],
        require_root_linux=False,
    )

    assert result["removed_release_source_pairs"] == [revisions[0]]
    assert (layout.evidence_base / anchor_name).is_file()


def test_mid_tree_unlink_crash_is_monotonic_and_resumable(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    candidate = revisions[0]
    nested = layout.source_base / candidate / "nested" / "deeper"
    nested.mkdir(parents=True)
    (nested / "one").write_text("one", encoding="ascii")
    (nested / "two").write_text("two", encoding="ascii")
    plan = gc.build_plan(layout, production_sha=_sha(99))
    original_unlink = gc.os.unlink
    crashed = False

    def crash_after_unlink(path, *args, **kwargs):
        nonlocal crashed
        original_unlink(path, *args, **kwargs)
        if path in {"one", "two"} and not crashed:
            crashed = True
            raise RuntimeError("simulated mid-tree crash")

    monkeypatch.setattr(gc.os, "unlink", crash_after_unlink)
    with pytest.raises(RuntimeError, match="mid-tree crash"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )
    monkeypatch.setattr(gc.os, "unlink", original_unlink)

    result = gc.apply_plan(
        layout,
        production_sha=_sha(99),
        approved_plan_sha256=plan["plan_sha256"],
        require_root_linux=False,
    )

    assert result["removed_release_source_pairs"] == [candidate]
    assert not (layout.source_base / candidate).exists()
    assert not (layout.release_base / candidate).exists()


def test_purge_unlinks_pinned_symlink_without_following_target(tmp_path):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    candidate = revisions[0]
    outside = tmp_path / "outside-must-survive"
    outside.write_text("survives", encoding="ascii")
    (layout.source_base / candidate / "outside-link").symlink_to(outside)
    plan = gc.build_plan(layout, production_sha=_sha(99))

    gc.apply_plan(
        layout,
        production_sha=_sha(99),
        approved_plan_sha256=plan["plan_sha256"],
        require_root_linux=False,
    )

    assert outside.read_text(encoding="ascii") == "survives"


def test_foreign_tombstone_swap_is_rejected_without_purging_it(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    candidate = revisions[0]
    plan = gc.build_plan(layout, production_sha=_sha(99))
    original_purge = gc._purge_tree_at

    def crash_before_purge(_root, _name, _anchor):
        raise RuntimeError("simulated crash before purge")

    monkeypatch.setattr(gc, "_purge_tree_at", crash_before_purge)
    with pytest.raises(RuntimeError, match="before purge"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )
    logical = json.loads(
        (layout.evidence_base / candidate / gc.LOGICAL_DELETE_NAME).read_text()
    )
    tombstone = layout.source_base / logical["source_tombstone"]
    saved = layout.source_base / ".saved-pinned-tombstone"
    tombstone.rename(saved)
    tombstone.mkdir()
    marker = tombstone / "foreign"
    marker.write_text("do not delete", encoding="ascii")
    monkeypatch.setattr(gc, "_purge_tree_at", original_purge)

    with pytest.raises(RuntimeError, match="identity changed"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )
    assert marker.read_text(encoding="ascii") == "do not delete"
    assert saved.is_dir()


def test_physical_receipt_is_not_written_until_exact_final_absence(
    tmp_path, monkeypatch
):
    layout = _layout(tmp_path)
    revisions = [_sha(index) for index in range(1, 5)]
    for created, revision in enumerate(revisions, start=1):
        _pair(layout, revision, created)
    candidate = revisions[0]
    plan = gc.build_plan(layout, production_sha=_sha(99))
    monkeypatch.setattr(gc, "_purge_tree_at", lambda *_args: None)

    with pytest.raises(RuntimeError, match="final absence"):
        gc.apply_plan(
            layout,
            production_sha=_sha(99),
            approved_plan_sha256=plan["plan_sha256"],
            require_root_linux=False,
        )
    assert not (layout.evidence_base / candidate / gc.PURGE_NAME).exists()


def test_root_swap_during_plan_is_detected(tmp_path, monkeypatch):
    layout = _layout(tmp_path)
    _pair(layout, _sha(1), 1)
    original_inventory = gc._inventory_root
    swapped = False

    def swap_after_inventory(root):
        nonlocal swapped
        result = original_inventory(root)
        if root.path == layout.release_base and not swapped:
            swapped = True
            saved = layout.release_base.with_name("releases-pinned")
            layout.release_base.rename(saved)
            layout.release_base.mkdir()
        return result

    monkeypatch.setattr(gc, "_inventory_root", swap_after_inventory)
    with pytest.raises(RuntimeError, match="identity changed while in use"):
        gc.build_plan(layout, production_sha=_sha(99))


def test_untrusted_writable_gc_root_is_rejected(tmp_path):
    layout = _layout(tmp_path)
    layout.release_base.chmod(0o777)

    with pytest.raises(PermissionError, match="exact trusted directory"):
        gc.build_plan(layout, production_sha=_sha(99))


def test_untrusted_parent_chain_is_rejected(tmp_path):
    layout = _layout(tmp_path, validate_parent_chain=True)

    with pytest.raises(PermissionError, match="parent chain is not trusted"):
        gc.build_plan(layout, production_sha=_sha(99))


def test_unknown_entries_are_reported_and_never_removed(tmp_path):
    layout = _layout(tmp_path)
    unknown_release = layout.release_base / "operator-note"
    unknown_source = layout.source_base / ".partial"
    unknown_release.mkdir()
    unknown_source.mkdir()

    plan = gc.build_plan(layout, production_sha=_sha(99))

    assert plan["unknown_entries"] == {
        "release_base": ["operator-note"],
        "source_base": [".partial"],
    }
    assert unknown_release.is_dir()
    assert unknown_source.is_dir()


def test_invalid_protection_artifact_fails_closed(tmp_path):
    protected_ref = tmp_path / "pending.json"
    protected_ref.write_text('{"revision":"' + _sha(1) + '"}', encoding="utf-8")
    layout = _layout(tmp_path, protected_refs=(protected_ref,))

    with pytest.raises(RuntimeError, match="not canonical JSON"):
        gc.build_plan(layout, production_sha=_sha(99))


def test_non_symlink_current_path_fails_closed(tmp_path):
    current = tmp_path / "current"
    current.write_text(_sha(1), encoding="ascii")
    layout = _layout(tmp_path, current_links=(current,))

    with pytest.raises(RuntimeError, match="not a symlink"):
        gc.build_plan(layout, production_sha=_sha(99))


def test_missing_listed_current_path_fails_closed(tmp_path):
    layout = _layout(tmp_path, current_links=(tmp_path / "missing-current",))

    with pytest.raises(FileNotFoundError):
        gc.build_plan(layout, production_sha=_sha(99))


def test_main_defaults_to_dry_run(monkeypatch, capsys):
    observed = {}

    def fake_plan(layout, *, production_sha):
        observed["layout"] = layout
        observed["production_sha"] = production_sha
        return {"schema": gc.PLAN_SCHEMA, "plan_sha256": "a" * 64}

    inventory_layout = gc.GCLayout(
        current_links=(Path("/current"),),
        previous_links=(Path("/previous"),),
        protected_refs=(Path("/protected.json"),),
    )
    monkeypatch.setattr(gc, "load_protection_inventory", lambda _path: inventory_layout)
    monkeypatch.setattr(gc, "build_plan", fake_plan)
    monkeypatch.setattr(
        gc,
        "apply_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
    )

    assert (
        gc.main([
            "--production-sha",
            _sha(1),
            "--protection-inventory",
            "/inventory.json",
        ])
        == 0
    )
    assert json.loads(capsys.readouterr().out)["schema"] == gc.PLAN_SCHEMA
    assert observed["production_sha"] == _sha(1)


def test_cli_fails_closed_when_protection_inventory_is_omitted():
    with pytest.raises(SystemExit):
        gc.main(["--production-sha", _sha(1)])


def test_protection_inventory_must_contain_every_nonempty_class(tmp_path):
    path = tmp_path / "inventory.json"
    unsigned = {
        "schema": gc.PROTECTION_INVENTORY_SCHEMA,
        "current_links": ["/current"],
        "previous_links": [],
        "protected_refs": ["/protected.json"],
    }
    path.write_bytes(
        _canonical({
            **unsigned,
            "inventory_sha256": gc._sha256_json(unsigned),
        })
    )

    with pytest.raises(RuntimeError, match="incomplete or invalid"):
        gc.load_protection_inventory(
            path,
            trusted_uid=os.geteuid(),
            trusted_gid=os.getegid(),
            validate_parent_chain=False,
        )
