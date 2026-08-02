from __future__ import annotations

import hashlib
import io
import os
import signal
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
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
ROOT = Path(__file__).parents[3]


@pytest.mark.parametrize("payload", (b"[]\n", b'"scalar"\n'))
def test_production_promote_rejects_non_object_json_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    payload: bytes,
) -> None:
    stdin = io.TextIOWrapper(io.BytesIO(payload), encoding="ascii")
    monkeypatch.setattr(sys, "stdin", stdin)

    assert publisher.production_main(("promote-runtime", REVISION)) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        '{"error_code":"successor_rebind_owner_runtime_foundation_failed","ok":false}\n'
    )


def test_production_builder_refuses_root_before_source_or_target_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(publisher.os, "geteuid", lambda: 0)
    monkeypatch.setattr(publisher.os, "getegid", lambda: 0)
    monkeypatch.setattr(
        publisher,
        "_require_exact_production_source",
        lambda **_kwargs: pytest.fail("source must not be read as root"),
    )

    with pytest.raises(
        publisher.SuccessorRebindOwnerRuntimeError,
        match="successor_rebind_owner_runtime_identity_invalid",
    ):
        publisher.build_runtime_as_dedicated_builder(
            REVISION,
            source_tree_oid=SOURCE_TREE,
            stage_c_builder_terminal_receipt_sha256=STAGE_C_TERMINAL,
        )


def test_production_entrypoint_refuses_simulated_root_builder_in_subprocess() -> None:
    expression = (
        "import sys;"
        f"sys.path.insert(0,{str(ROOT)!r});"
        "from scripts.canary import production_successor_rebind_owner_runtime as p;"
        "p.os.geteuid=lambda:0;"
        "p.os.getegid=lambda:0;"
        f"raise SystemExit(p.production_main(('build-runtime-as-dedicated-builder',"
        f"{REVISION!r},{SOURCE_TREE!r},{STAGE_C_TERMINAL!r})))"
    )
    completed = subprocess.run(
        (sys.executable, "-I", "-S", "-B", "-c", expression),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        env={"PATH": "/usr/bin:/bin", "HOME": "/nonexistent", "LC_ALL": "C"},
    )

    assert completed.returncode == 2
    assert completed.stdout == b""
    assert completed.stderr == (
        b'{"error_code":"successor_rebind_owner_runtime_foundation_failed",'
        b'"ok":false}\n'
    )
    assert b"Traceback" not in completed.stderr


def test_production_source_requires_exact_clean_revision_and_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "library/pending/source"
    source.mkdir(parents=True)

    def git(*arguments: str) -> str:
        completed = subprocess.run(
            ("/usr/bin/git", *arguments),
            cwd=source,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
            },
        )
        return completed.stdout.strip()

    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    (source / "tracked.txt").write_text("exact\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "exact source")
    revision = git("rev-parse", "HEAD")
    tree = git("rev-parse", "HEAD^{tree}")
    selected = tmp_path / "library" / revision / "source"
    selected.parent.mkdir(parents=True)
    source.rename(selected)

    original_lstat = publisher.os.lstat

    def root_owned_lstat(path: os.PathLike[str] | str) -> SimpleNamespace:
        state = original_lstat(path)
        return SimpleNamespace(
            st_mode=stat.S_IFDIR | stat.S_IMODE(state.st_mode),
            st_uid=0,
            st_gid=0,
        )

    monkeypatch.setattr(publisher.os, "lstat", root_owned_lstat)
    monkeypatch.setattr(publisher, "REVISION_LIBRARY_BASE", tmp_path / "library")

    assert (
        publisher._require_exact_production_source(  # noqa: SLF001
            revision=revision,
            source_tree_oid=tree,
        )
        == selected
    )

    (selected / "foreign.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(
        publisher.SuccessorRebindOwnerRuntimeError,
        match="successor_rebind_owner_runtime_source_invalid",
    ):
        publisher._require_exact_production_source(  # noqa: SLF001
            revision=revision,
            source_tree_oid=tree,
        )


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
        "launch_authority_sha256": "0" * 64,
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


def test_retry_recovers_crash_after_rename_before_publication(tmp_path: Path) -> None:
    staged = _staged_fixture(tmp_path)
    arguments = staged.promotion_arguments()

    def crash_after_rename() -> None:
        raise RuntimeError("simulated process crash after durable rename")

    arguments["after_rename"] = crash_after_rename
    with pytest.raises(RuntimeError, match="simulated process crash"):
        publisher.promote_staged_for_test(**arguments)

    publication_path = staged.publication_root / f"{REVISION}.json"
    intent_path = staged.publication_root / f"{REVISION}.promotion-intent.json"
    assert staged.final_root.is_dir()
    assert intent_path.is_file()
    assert not publication_path.exists()

    arguments.pop("after_rename")
    publication = publisher.promote_staged_for_test(**arguments)

    assert publication_path.is_file()
    assert (
        publication["publication_sha256"]
        == publisher.validate_publication(
            publisher._decode_exact(  # noqa: SLF001
                publication_path,
                uid=os.geteuid(),
                gid=os.getegid(),
            ),
            revision=REVISION,
            release_base=staged.release_base,
        )["publication_sha256"]
    )


def test_recovery_refuses_final_tree_changed_after_crash(tmp_path: Path) -> None:
    staged = _staged_fixture(tmp_path)
    arguments = staged.promotion_arguments()

    def crash_after_rename() -> None:
        raise RuntimeError("simulated process crash after durable rename")

    arguments["after_rename"] = crash_after_rename
    with pytest.raises(RuntimeError, match="simulated process crash"):
        publisher.promote_staged_for_test(**arguments)

    final_module = staged.final_root / staged.module_relative
    final_module.chmod(0o644)
    final_module.write_bytes(b"different post-crash tree\n")
    final_module.chmod(0o444)
    arguments.pop("after_rename")
    with pytest.raises(
        preexec.SuccessorRuntimePreExecError,
        match="successor_runtime_preexec_",
    ):
        publisher.promote_staged_for_test(**arguments)
    assert not (staged.publication_root / f"{REVISION}.json").exists()


@pytest.mark.parametrize("crash_point", ("pending", "linked"))
def test_exact_publication_replays_both_durable_crash_points(
    tmp_path: Path,
    crash_point: str,
) -> None:
    parent = tmp_path / "publication"
    parent.mkdir(mode=0o700)
    path = parent / "exact.json"
    value = {"schema": "exact.test.v1", "secret_material_recorded": False}
    raw = publisher._canonical(value) + b"\n"  # noqa: SLF001
    pending = path.with_name(
        f".{path.name}.{hashlib.sha256(raw).hexdigest()}.pending"
    )

    def crash() -> None:
        raise KeyboardInterrupt

    callbacks = (
        {"after_pending_fsync": crash}
        if crash_point == "pending"
        else {"after_final_link": crash}
    )
    with pytest.raises(KeyboardInterrupt):
        publisher._write_exact(  # noqa: SLF001
            path,
            value,
            uid=os.geteuid(),
            gid=os.getegid(),
            **callbacks,
        )

    assert pending.read_bytes() == raw
    assert (path.exists()) is (crash_point == "linked")
    publisher._write_exact(  # noqa: SLF001
        path,
        value,
        uid=os.geteuid(),
        gid=os.getegid(),
    )

    assert path.read_bytes() == raw
    assert path.stat().st_mode & 0o777 == 0o444
    assert path.stat().st_nlink == 1
    assert not pending.exists()


def test_exact_publication_recovers_immutable_prefix_but_preserves_foreign_pending(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "publication"
    parent.mkdir(mode=0o700)
    path = parent / "exact.json"
    value = {"schema": "exact.test.v1", "secret_material_recorded": False}
    raw = publisher._canonical(value) + b"\n"  # noqa: SLF001
    digest = hashlib.sha256(raw).hexdigest()
    pending = path.with_name(f".{path.name}.{digest}.pending")
    pending.write_bytes(raw[: len(raw) // 2])
    pending.chmod(0o444)

    publisher._write_exact(  # noqa: SLF001
        path,
        value,
        uid=os.geteuid(),
        gid=os.getegid(),
    )
    assert path.read_bytes() == raw
    assert not pending.exists()

    other_path = parent / "other.json"
    foreign = other_path.with_name(f".{other_path.name}.{'f' * 64}.pending")
    foreign.write_bytes(b"foreign\n")
    foreign.chmod(0o444)
    with pytest.raises(
        publisher.SuccessorRebindOwnerRuntimeError,
        match="publication_conflict",
    ):
        publisher._write_exact(  # noqa: SLF001
            other_path,
            value,
            uid=os.geteuid(),
            gid=os.getegid(),
        )
    assert foreign.read_bytes() == b"foreign\n"
    assert not other_path.exists()


def test_retry_recovers_intent_bound_partial_root_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged_fixture(tmp_path)
    arguments = staged.promotion_arguments()
    real_copy = publisher._copy_tree_exact  # noqa: SLF001
    crashed = False

    def crash_with_exact_prefix(**kwargs: Any) -> None:
        nonlocal crashed
        destination = kwargs["destination"]
        source = kwargs["source"]
        destination.mkdir(mode=0o700)
        relative = Path("venv/bin/python")
        target = destination / relative
        target.parent.mkdir(parents=True, mode=0o700)
        for directory in (destination / "venv", target.parent):
            directory.chmod(0o700)
        raw = (source / relative).read_bytes()
        target.write_bytes(raw[: max(1, len(raw) // 2)])
        target.chmod(0o600)
        crashed = True
        raise KeyboardInterrupt

    monkeypatch.setattr(publisher, "_copy_tree_exact", crash_with_exact_prefix)
    with pytest.raises(KeyboardInterrupt):
        publisher.promote_staged_for_test(**arguments)

    incomplete = staged.release_base / f".{REVISION}.root-copy-incomplete"
    intent = staged.publication_root / f"{REVISION}.promotion-intent.json"
    assert crashed is True
    assert intent.is_file()
    assert incomplete.is_dir()
    assert (incomplete / "venv/bin/python").stat().st_mode & 0o777 == 0o600

    monkeypatch.setattr(publisher, "_copy_tree_exact", real_copy)
    monkeypatch.setattr(publisher, "_rename_noreplace", _rename_noreplace_for_test)
    publication = publisher.promote_staged_for_test(**arguments)

    assert publication["publication_sha256"]
    assert staged.final_root.is_dir()
    assert not incomplete.exists()
    assert not tuple(
        staged.release_base.glob(f".{REVISION}.*.root-copy-quarantine")
    )


@pytest.mark.parametrize(
    "shape",
    ("distinct-pair", "final-outside-hardlink", "pending-outside-hardlink"),
)
def test_root_copy_quarantine_preserves_ambiguous_manifest_transactions(
    tmp_path: Path,
    shape: str,
) -> None:
    staged = _staged_fixture(tmp_path)
    uid = os.geteuid()
    gid = os.getegid()
    staged_manifest = preexec.verify_staged(
        root=staged.staging_root,
        revision=REVISION,
        expected_manifest_sha256=staged.staging["manifest_sha256"],
        expected_tree_sha256=staged.staging["tree_sha256"],
        expected_interpreter_sha256=staged.staging["interpreter_sha256"],
        expected_attestation_sha256=staged.staging["attestation_sha256"],
        uid=uid,
        gid=gid,
    )
    selected = staged.release_base / ".ambiguous-root-copy"
    quarantine = staged.release_base / ".quarantine"
    selected.mkdir(mode=0o700)
    rebased = publisher._rebase_manifest(  # noqa: SLF001
        staged_manifest,
        staging_root=staged.staging_root,
        final_root=staged.final_root,
        root_uid=uid,
        root_gid=gid,
    )
    manifest_raw = publisher._canonical(rebased) + b"\n"  # noqa: SLF001
    digest = hashlib.sha256(manifest_raw).hexdigest()
    final = selected / preexec.MANIFEST_NAME
    pending = selected / f".{preexec.MANIFEST_NAME}.{digest}.pending"
    final.write_bytes(manifest_raw)
    final.chmod(0o444)
    pending.write_bytes(manifest_raw)
    pending.chmod(0o444)
    outside: Path | None = None
    if shape == "final-outside-hardlink":
        pending.unlink()
        outside = staged.release_base / "outside-final-alias"
        outside.hardlink_to(final)
    elif shape == "pending-outside-hardlink":
        final.unlink()
        outside = staged.release_base / "outside-pending-alias"
        outside.hardlink_to(pending)

    with pytest.raises(
        publisher.SuccessorRebindOwnerRuntimeError,
        match="incomplete_conflict",
    ):
        publisher._quarantine_recoverable_root_copy(  # noqa: SLF001
            selected=selected,
            quarantine=quarantine,
            staging_root=staged.staging_root,
            staged_manifest=staged_manifest,
            rebased_manifest=rebased,
            root_uid=uid,
            root_gid=gid,
        )

    assert selected.is_dir()
    assert not quarantine.exists()
    if outside is not None:
        assert outside.read_bytes() == manifest_raw


def test_root_copy_quarantine_accepts_only_linked_full_manifest_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    staged = _staged_fixture(tmp_path)
    uid = os.geteuid()
    gid = os.getegid()
    staged_manifest = preexec.verify_staged(
        root=staged.staging_root,
        revision=REVISION,
        expected_manifest_sha256=staged.staging["manifest_sha256"],
        expected_tree_sha256=staged.staging["tree_sha256"],
        expected_interpreter_sha256=staged.staging["interpreter_sha256"],
        expected_attestation_sha256=staged.staging["attestation_sha256"],
        uid=uid,
        gid=gid,
    )
    selected = staged.release_base / ".linked-root-copy"
    quarantine = staged.release_base / ".quarantine"
    selected.mkdir(mode=0o700)
    rebased = publisher._rebase_manifest(  # noqa: SLF001
        staged_manifest,
        staging_root=staged.staging_root,
        final_root=staged.final_root,
        root_uid=uid,
        root_gid=gid,
    )
    manifest_raw = publisher._canonical(rebased) + b"\n"  # noqa: SLF001
    digest = hashlib.sha256(manifest_raw).hexdigest()
    pending = selected / f".{preexec.MANIFEST_NAME}.{digest}.pending"
    final = selected / preexec.MANIFEST_NAME
    pending.write_bytes(manifest_raw)
    pending.chmod(0o444)
    final.hardlink_to(pending)
    monkeypatch.setattr(publisher, "_rename_noreplace", _rename_noreplace_for_test)

    publisher._quarantine_recoverable_root_copy(  # noqa: SLF001
        selected=selected,
        quarantine=quarantine,
        staging_root=staged.staging_root,
        staged_manifest=staged_manifest,
        rebased_manifest=rebased,
        root_uid=uid,
        root_gid=gid,
    )

    assert not selected.exists()
    assert not quarantine.exists()


def test_exact_publication_serializes_real_competing_processes(
    tmp_path: Path,
) -> None:
    parent = tmp_path / "publication"
    parent.mkdir(mode=0o700)
    path = parent / "exact.json"
    script = (
        "import os,sys;"
        "from pathlib import Path;"
        "from scripts.canary.production_successor_rebind_owner_runtime "
        "import _write_exact;"
        "_write_exact(Path(sys.argv[1]),"
        "{'schema':'exact.test.v1','secret_material_recorded':False},"
        "uid=os.geteuid(),gid=os.getegid())"
    )
    processes = [
        subprocess.Popen(
            (sys.executable, "-B", "-c", script, str(path)),
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        for _ in range(4)
    ]
    completed = [process.communicate(timeout=30) for process in processes]

    assert [process.returncode for process in processes] == [0, 0, 0, 0], completed
    value = {"schema": "exact.test.v1", "secret_material_recorded": False}
    assert path.read_bytes() == publisher._canonical(value) + b"\n"  # noqa: SLF001
    assert path.stat().st_nlink == 1
    assert not tuple(parent.glob(f".{path.name}.*.pending"))


@pytest.mark.parametrize(
    ("kill_hook", "pending_mode"),
    (
        ("before_pending_finalize", 0o600),
        ("before_pending_fsync", 0o444),
    ),
)
def test_exact_publication_recovers_real_sigkill_and_fsyncs_inode_before_link(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kill_hook: str,
    pending_mode: int,
) -> None:
    parent = tmp_path / "publication"
    parent.mkdir(mode=0o700)
    path = parent / "exact.json"
    value = {"schema": "exact.test.v1", "secret_material_recorded": False}
    raw = publisher._canonical(value) + b"\n"  # noqa: SLF001
    pending = path.with_name(
        f".{path.name}.{hashlib.sha256(raw).hexdigest()}.pending"
    )
    script = (
        "import os,signal,sys;"
        "from pathlib import Path;"
        "from scripts.canary.production_successor_rebind_owner_runtime "
        "import _write_exact;"
        "kill=lambda:os.kill(os.getpid(),signal.SIGKILL);"
        "_write_exact(Path(sys.argv[1]),"
        "{'schema':'exact.test.v1','secret_material_recorded':False},"
        "uid=os.geteuid(),gid=os.getegid(),**{sys.argv[2]:kill})"
    )
    completed = subprocess.run(
        (sys.executable, "-B", "-c", script, str(path), kill_hook),
        cwd=ROOT,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )

    assert completed.returncode == -signal.SIGKILL
    assert not path.exists()
    assert pending.read_bytes() == raw
    assert pending.stat().st_mode & 0o777 == pending_mode
    pending_inode = pending.stat().st_ino
    real_fsync = publisher.os.fsync
    fsynced_inodes: list[int] = []

    def observe_fsync(descriptor: int) -> None:
        fsynced_inodes.append(os.fstat(descriptor).st_ino)
        real_fsync(descriptor)

    monkeypatch.setattr(publisher.os, "fsync", observe_fsync)
    publisher._write_exact(  # noqa: SLF001
        path,
        value,
        uid=os.geteuid(),
        gid=os.getegid(),
    )

    assert pending_inode in fsynced_inodes
    assert path.read_bytes() == raw
    assert path.stat().st_mode & 0o777 == 0o444
    assert path.stat().st_nlink == 1
    assert not pending.exists()
