from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from scripts.canary import receipt_driven_release_gc as gc


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
    return gc.GCLayout(
        release_base=releases,
        source_base=sources,
        evidence_base=evidence,
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
    unsigned = {
        "schema": gc.STOPPED_RECEIPT_SCHEMA,
        "ok": ok,
        "state": state,
        "services_stopped_and_disabled": services_stopped_and_disabled,
        "release_revision": revision,
        "release_root": str(layout.release_base / revision),
        "receipt_path": str(path),
        "source": {
            "repository": gc.FORK_REPOSITORY,
            "root": str(layout.source_base / revision),
            "head_sha": revision,
            "tree_sha": _sha(900),
        },
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


def test_main_defaults_to_dry_run(monkeypatch, capsys):
    observed = {}

    def fake_plan(layout, *, production_sha):
        observed["layout"] = layout
        observed["production_sha"] = production_sha
        return {"schema": gc.PLAN_SCHEMA, "plan_sha256": "a" * 64}

    monkeypatch.setattr(gc, "build_plan", fake_plan)
    monkeypatch.setattr(
        gc,
        "apply_plan",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()),
    )

    assert gc.main(["--production-sha", _sha(1)]) == 0
    assert json.loads(capsys.readouterr().out)["schema"] == gc.PLAN_SCHEMA
    assert observed["production_sha"] == _sha(1)
