from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import pytest

from scripts.canary import production_successor_rebind_owner_runtime as publisher
from scripts.canary import (
    production_successor_rebind_owner_runtime_preexec as preexec,
)


REVISION = "a" * 40
SOURCE_TREE = "b" * 40
STAGE_C_TERMINAL = "c" * 64
BUILDER_RECEIPT = "d" * 64
WHEEL = "e" * 64


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _write(path: Path, raw: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    path.chmod(mode)


@dataclass
class StagedFixture:
    release_base: Path
    publication_root: Path
    staging_base: Path
    staging_root: Path
    module: Path
    module_relative: str
    module_raw: bytes
    staging: dict[str, Any]
    retained_descriptor: int | None

    @property
    def final_root(self) -> Path:
        return self.release_base / REVISION

    def promotion_arguments(self) -> dict[str, Any]:
        uid = os.geteuid()
        gid = os.getegid()
        return {
            "revision": REVISION,
            "release_base": self.release_base,
            "publication_root": self.publication_root,
            "builder_uid": uid,
            "builder_gid": gid,
            "root_uid": uid,
            "root_gid": gid,
            "expected_staging_publication_sha256": self.staging["publication_sha256"],
            "expected_source_tree_oid": SOURCE_TREE,
            "expected_stage_c_builder_terminal_receipt_sha256": (STAGE_C_TERMINAL),
            "expected_owner_runtime_builder_receipt_sha256": BUILDER_RECEIPT,
            "expected_owner_runtime_wheel_sha256": WHEEL,
            "expected_staging_manifest_sha256": self.staging["manifest_sha256"],
            "expected_staging_attestation_sha256": self.staging["attestation_sha256"],
            "expected_staging_tree_sha256": self.staging["tree_sha256"],
            "expected_staging_interpreter_sha256": self.staging["interpreter_sha256"],
            "expected_staging_pyvenv_cfg_sha256": self.staging["pyvenv_cfg_sha256"],
            "rename_noreplace": _rename_noreplace_for_test,
        }


def _rename_noreplace_for_test(source: Path, target: Path) -> None:
    if target.exists() or target.is_symlink():
        raise FileExistsError(target)
    source.rename(target)


def _staged_fixture(
    root: Path,
    *,
    module_raw: bytes = b"trusted owner runtime module\n",
    retained_descriptor: bool = False,
) -> StagedFixture:
    uid = os.geteuid()
    gid = os.getegid()
    release_base = root / "runtime"
    publication_root = root / "publications"
    release_base.mkdir(parents=True, mode=0o755)
    release_base.chmod(0o755)
    staging_base = release_base / f".{REVISION}.builder-staging"
    staging_base.mkdir(mode=0o700)
    staging_base.chmod(0o700)
    staging_root = staging_base / REVISION
    staging_root.mkdir(mode=0o755)
    interpreter = staging_root / "venv/bin/python"
    pyvenv = staging_root / "venv/pyvenv.cfg"
    site_packages = staging_root / "venv/lib/python3.11/site-packages"
    module_relative = (
        "venv/lib/python3.11/site-packages/gateway/production_owner_runtime.py"
    )
    module = staging_root / module_relative
    _write(interpreter, b"exact copied python\n", 0o555)
    _write(pyvenv, b"home = /sealed/python\n", 0o444)
    _write(module, module_raw, 0o644 if retained_descriptor else 0o444)
    descriptor = os.open(module, os.O_RDWR) if retained_descriptor else None
    if descriptor is not None:
        module.chmod(0o444)
    for current, directories, _files in os.walk(
        staging_root,
        topdown=False,
        followlinks=False,
    ):
        for name in directories:
            (Path(current) / name).chmod(0o555)
    staging_root.chmod(0o555)
    entries, total = preexec._collect(staging_root, uid=uid, gid=gid)  # noqa: SLF001
    required = {
        "gateway.production_owner_runtime": {
            "origin": str(module),
            "relative_path": module_relative,
            "sha256": _sha(module_raw),
        }
    }
    unsigned_manifest = {
        "schema": preexec.MANIFEST_SCHEMA,
        "revision": REVISION,
        "artifact_root": str(staging_root),
        "python_version": preexec.PYTHON_VERSION,
        "interpreter": {
            "path": str(interpreter),
            "realpath": str(interpreter),
            "mode": "0555",
            "size": interpreter.stat().st_size,
            "sha256": _sha(interpreter.read_bytes()),
        },
        "pyvenv_cfg": {
            "path": str(pyvenv),
            "mode": "0444",
            "size": pyvenv.stat().st_size,
            "sha256": _sha(pyvenv.read_bytes()),
        },
        "site_packages": str(site_packages),
        "sys_path": [str(site_packages)],
        "required_modules": required,
        "entries": entries,
        "entry_count": len(entries),
        "tree_bytes": total,
        "tree_sha256": _sha(preexec._canonical(entries)),  # noqa: SLF001
        "root_uid": uid,
        "root_gid": gid,
        "root_mode": "0555",
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    manifest = {
        **unsigned_manifest,
        "manifest_sha256": _sha(preexec._canonical(unsigned_manifest)),  # noqa: SLF001
    }
    manifest_path = staging_root / preexec.MANIFEST_NAME
    staging_root.chmod(0o755)
    _write(manifest_path, preexec._canonical(manifest) + b"\n", 0o444)  # noqa: SLF001
    staging_root.chmod(0o555)
    attestation = publisher._attestation(manifest)  # noqa: SLF001
    unsigned_staging = {
        "schema": publisher.STAGING_SCHEMA,
        "release_revision": REVISION,
        "source_tree_oid": SOURCE_TREE,
        "stage_c_builder_terminal_receipt_sha256": STAGE_C_TERMINAL,
        "staging_root": str(staging_root),
        "manifest_sha256": manifest["manifest_sha256"],
        "attestation_sha256": attestation["attestation_sha256"],
        "tree_sha256": manifest["tree_sha256"],
        "interpreter_sha256": manifest["interpreter"]["sha256"],
        "pyvenv_cfg_sha256": manifest["pyvenv_cfg"]["sha256"],
        "owner_runtime_builder_receipt_sha256": BUILDER_RECEIPT,
        "owner_runtime_wheel_sha256": WHEEL,
        "builder_unprivileged": True,
        "root_build_performed": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    staging = publisher.validate_staging_publication(
        {
            **unsigned_staging,
            "publication_sha256": publisher._digest(unsigned_staging),  # noqa: SLF001
        },
        revision=REVISION,
        staging_root=staging_root,
    )
    _write(
        staging_base / publisher.STAGING_NAME,
        publisher._canonical(staging) + b"\n",  # noqa: SLF001
        0o444,
    )
    return StagedFixture(
        release_base=release_base,
        publication_root=publication_root,
        staging_base=staging_base,
        staging_root=staging_root,
        module=module,
        module_relative=module_relative,
        module_raw=module_raw,
        staging=staging,
        retained_descriptor=descriptor,
    )


def test_root_promotion_copies_into_fresh_tree_and_retained_fd_cannot_mutate_it(
    tmp_path: Path,
) -> None:
    staged = _staged_fixture(tmp_path, retained_descriptor=True)
    try:
        publication = publisher.promote_staged_for_test(**staged.promotion_arguments())
        final_module = staged.final_root / staged.module_relative
        assert final_module.read_bytes() == staged.module_raw
        assert publication["source_tree_oid"] == SOURCE_TREE
        assert (
            publication["staging_publication_sha256"]
            == staged.staging["publication_sha256"]
        )
        assert publication["owner_runtime_wheel_sha256"] == WHEEL

        assert staged.retained_descriptor is not None
        os.lseek(staged.retained_descriptor, 0, os.SEEK_SET)
        os.write(staged.retained_descriptor, b"hostile")
        os.fsync(staged.retained_descriptor)

        assert staged.module.read_bytes() != staged.module_raw
        assert final_module.read_bytes() == staged.module_raw
        assert (staged.final_root.stat().st_mode & 0o777) == 0o555
    finally:
        if staged.retained_descriptor is not None:
            os.close(staged.retained_descriptor)


@pytest.mark.parametrize("mutation", ("write", "swap"))
def test_source_mutation_during_copy_never_reaches_final_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
) -> None:
    staged = _staged_fixture(tmp_path, retained_descriptor=True)
    original = publisher._copy_tree_exact  # noqa: SLF001

    def mutate_after_copy(**kwargs: Any) -> None:
        original(**kwargs)
        if mutation == "write":
            assert staged.retained_descriptor is not None
            os.lseek(staged.retained_descriptor, 0, os.SEEK_SET)
            os.write(staged.retained_descriptor, b"changed")
            os.fsync(staged.retained_descriptor)
        else:
            staged.module.parent.chmod(0o755)
            staged.module.unlink()
            _write(staged.module, b"swapped malicious module\n", 0o444)
            staged.module.parent.chmod(0o555)

    monkeypatch.setattr(publisher, "_copy_tree_exact", mutate_after_copy)
    try:
        with pytest.raises(
            preexec.SuccessorRuntimePreExecError,
            match="successor_runtime_preexec_",
        ):
            publisher.promote_staged_for_test(**staged.promotion_arguments())
        assert not staged.final_root.exists()
    finally:
        if staged.retained_descriptor is not None:
            os.close(staged.retained_descriptor)


def test_self_consistent_malicious_staging_without_external_digest_is_rejected(
    tmp_path: Path,
) -> None:
    trusted = _staged_fixture(tmp_path / "trusted")
    malicious = _staged_fixture(
        tmp_path / "malicious",
        module_raw=b"self-consistent attacker-selected owner runtime\n",
    )
    arguments = malicious.promotion_arguments()
    trusted_authority = trusted.promotion_arguments()
    for name in tuple(arguments):
        if name.startswith("expected_"):
            arguments[name] = trusted_authority[name]

    with pytest.raises(
        publisher.SuccessorRebindOwnerRuntimeError,
        match="staging_authority_invalid",
    ):
        publisher.promote_staged_for_test(**arguments)

    assert not malicious.final_root.exists()
    assert not (malicious.release_base / f".{REVISION}.root-copy-incomplete").exists()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("expected_source_tree_oid", "f" * 40),
        ("expected_stage_c_builder_terminal_receipt_sha256", "0" * 64),
        ("expected_owner_runtime_builder_receipt_sha256", "1" * 64),
        ("expected_owner_runtime_wheel_sha256", "2" * 64),
        ("expected_staging_manifest_sha256", "3" * 64),
        ("expected_staging_tree_sha256", "4" * 64),
        ("expected_staging_interpreter_sha256", "5" * 64),
        ("expected_staging_attestation_sha256", "6" * 64),
    ),
)
def test_every_external_authority_mismatch_fails_before_copy(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    staged = _staged_fixture(tmp_path)
    arguments = staged.promotion_arguments()
    arguments[field] = value
    with pytest.raises(
        publisher.SuccessorRebindOwnerRuntimeError,
        match="staging_authority_invalid",
    ):
        publisher.promote_staged_for_test(**arguments)
    assert not (staged.release_base / f".{REVISION}.root-copy-incomplete").exists()


def test_dynamic_site_path_and_manifest_mode_tamper_fail_before_copy(
    tmp_path: Path,
) -> None:
    staged = _staged_fixture(tmp_path / "pth")
    site_packages = staged.staging_root / "venv/lib/python3.11/site-packages"
    site_packages.chmod(0o755)
    _write(site_packages / "hostile.pth", b"import hostile\n", 0o444)
    site_packages.chmod(0o555)
    with pytest.raises(
        preexec.SuccessorRuntimePreExecError,
        match="successor_runtime_preexec_",
    ):
        publisher.promote_staged_for_test(**staged.promotion_arguments())
    assert not staged.final_root.exists()

    staged = _staged_fixture(tmp_path / "manifest")
    manifest = staged.staging_root / preexec.MANIFEST_NAME
    manifest.chmod(0o644)
    with pytest.raises(
        preexec.SuccessorRuntimePreExecError,
        match="successor_runtime_preexec_",
    ):
        publisher.promote_staged_for_test(**staged.promotion_arguments())
    assert not staged.final_root.exists()


def test_atomic_no_replace_race_preserves_foreign_target(tmp_path: Path) -> None:
    staged = _staged_fixture(tmp_path)
    marker = b"foreign target created during race\n"

    def lose_race(_source: Path, target: Path) -> None:
        target.mkdir(mode=0o700)
        (target / "foreign").write_bytes(marker)
        raise FileExistsError(target)

    arguments = staged.promotion_arguments()
    arguments["rename_noreplace"] = lose_race
    with pytest.raises(
        publisher.SuccessorRebindOwnerRuntimeError,
        match="promotion_failed",
    ):
        publisher.promote_staged_for_test(**arguments)
    assert (staged.final_root / "foreign").read_bytes() == marker
