from __future__ import annotations

import hashlib
import json
import os
import tomllib
from pathlib import Path

import pytest

from scripts.canary import package_production_owner_runtime as package


REVISION = "a" * 40
ROOT = Path(__file__).parents[3]


class NewBuildObserved(RuntimeError):
    pass


def _spec(tmp_path: Path) -> package.OwnerRuntimeBuildSpec:
    source = tmp_path / "source"
    release = tmp_path / "releases"
    source.mkdir()
    release.mkdir()
    uv = tmp_path / "uv"
    git = tmp_path / "git"
    uv.touch()
    git.touch()
    return package.OwnerRuntimeBuildSpec(
        revision=REVISION,
        source_root=source,
        release_base=release,
        uv_executable=uv,
        git_executable=git,
    )


def test_build_argv_is_frozen_noneditable_and_hash_constrained(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    commands = package.build_commands(
        spec,
        managed_python=spec.python_root / "cpython/bin/python3.11",
    )

    assert commands[0][1:3] == ("-I", "-B")
    assert "-S" not in commands[0]
    assert "--copies" in commands[0]
    assert "--frozen" in commands[1]
    assert "--no-editable" in commands[1]
    assert "--no-dev" in commands[1]
    assert "--no-install-project" in commands[1]
    assert "--require-hashes" in commands[2]
    assert "--force-pep517" in commands[2]


def test_runtime_argv_uses_site_imports_but_rejects_ambient_python(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    command = package.runtime_command(
        spec,
        "run",
        "author-freeze",
        "--revision",
        REVISION,
    )

    assert command[:4] == (
        str(spec.interpreter),
        "-I",
        "-B",
        "-m",
    )
    assert command[4:8] == (
        "gateway.production_owner_runtime",
        "--revision",
        REVISION,
        "run",
    )
    assert "-S" not in command
    assert command[8:] == (
        "--",
        "author-freeze",
        "--revision",
        REVISION,
    )


def test_package_metadata_includes_owner_launchers_and_operational_rails() -> None:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text())
    included = set(metadata["tool"]["setuptools"]["packages"]["find"]["include"])

    assert {"scripts", "scripts.canary", "scripts.canary.*"} <= included
    assert {"ops", "ops.muncho", "ops.muncho.*"} <= included


def test_spec_rejects_release_nested_inside_source(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    spec = package.OwnerRuntimeBuildSpec(
        revision=REVISION,
        source_root=source,
        release_base=source / "releases",
        uv_executable=tmp_path / "uv",
        git_executable=tmp_path / "git",
    )

    with pytest.raises(
        package.ProductionOwnerRuntimePackagingError,
        match="spec_invalid",
    ):
        spec.validate()


def test_build_passes_owner_role_only_to_the_sealed_wheel_step(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    observed_environments: list[tuple[tuple[str, ...], dict[str, str]]] = []

    class BuildObserved(RuntimeError):
        pass

    def fake_run(
        argv: tuple[str, ...],
        *,
        spec: package.OwnerRuntimeBuildSpec,
        cwd: Path | None = None,
        extra_environment: dict[str, str] | None = None,
        timeout: int = package._COMMAND_TIMEOUT,
    ) -> bytes:
        del cwd, timeout
        environment = dict(extra_environment or {})
        observed_environments.append((tuple(argv), environment))
        if tuple(argv[:2]) == (str(spec.uv_executable), "python") and (
            "find" in argv
        ):
            return (
                str(spec.python_root / "cpython/bin/python3.11") + "\n"
            ).encode()
        if tuple(argv[:2]) == (str(spec.uv_executable), "build"):
            raise BuildObserved
        return b""

    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)
    monkeypatch.setattr(package, "_run", fake_run)

    with pytest.raises(BuildObserved):
        package.build_owner_runtime(spec)

    guarded = [
        environment
        for argv, environment in observed_environments
        if tuple(argv[:2]) == (str(spec.uv_executable), "build")
    ]
    assert guarded == [
        {"HERMES_SEALED_RELEASE_BUILD": "owner-runtime-v1"}
    ]
    assert all(
        "HERMES_SEALED_RELEASE_BUILD" not in environment
        for argv, environment in observed_environments
        if tuple(argv[:2]) != (str(spec.uv_executable), "build")
    )


def _quarantined_releases(
    spec: package.OwnerRuntimeBuildSpec,
) -> list[Path]:
    return sorted(
        spec.release_base.glob(
            f".{spec.revision}.owner-runtime-quarantine-*/release"
        )
    )


def _observe_fresh_build(
    monkeypatch: pytest.MonkeyPatch,
    *,
    label: str = "fresh",
) -> None:
    def observe(spec: package.OwnerRuntimeBuildSpec) -> dict[str, object]:
        assert spec.incomplete_marker.read_text(encoding="ascii") == (
            spec.revision + "\n"
        )
        spec.release_root.mkdir(mode=0o700)
        (spec.release_root / "attempt").write_text(label, encoding="ascii")
        raise NewBuildObserved

    monkeypatch.setattr(package, "_build_new_release", observe)


def test_legacy_incomplete_release_is_quarantined_and_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.release_root.mkdir()
    spec.legacy_incomplete_marker.write_text(
        spec.revision + "\n",
        encoding="ascii",
    )
    (spec.release_root / "partial").write_text("legacy", encoding="ascii")
    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)
    monkeypatch.setattr(
        package,
        "_existing_release",
        lambda _spec: pytest.fail("incomplete release was treated as reusable"),
    )
    _observe_fresh_build(monkeypatch)

    with pytest.raises(NewBuildObserved):
        package.build_owner_runtime(spec)

    quarantined = _quarantined_releases(spec)
    assert len(quarantined) == 1
    assert (quarantined[0] / "partial").read_text(encoding="ascii") == "legacy"
    assert (
        quarantined[0] / spec.legacy_incomplete_marker.name
    ).read_text(encoding="ascii") == spec.revision + "\n"
    assert spec.release_root.joinpath("attempt").read_text(encoding="ascii") == (
        "fresh"
    )
    assert spec.incomplete_marker.exists()


def test_sibling_incomplete_state_quarantines_unattested_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.release_root.mkdir()
    (spec.release_root / "partial").write_text("unattested", encoding="ascii")
    spec.incomplete_marker.write_text(spec.revision + "\n", encoding="ascii")
    existing_calls = 0

    def reject_existing(
        _spec: package.OwnerRuntimeBuildSpec,
    ) -> dict[str, object]:
        nonlocal existing_calls
        existing_calls += 1
        raise package.ProductionOwnerRuntimePackagingError(
            "production_owner_runtime_package_command_failed"
        )

    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)
    monkeypatch.setattr(package, "_existing_release", reject_existing)
    _observe_fresh_build(monkeypatch)

    with pytest.raises(NewBuildObserved):
        package.build_owner_runtime(spec)

    assert existing_calls == 1
    quarantined = _quarantined_releases(spec)
    assert len(quarantined) == 1
    assert (quarantined[0] / "partial").read_text(encoding="ascii") == (
        "unattested"
    )
    assert spec.incomplete_marker.exists()


def test_stale_incomplete_state_reuses_valid_release(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.release_root.mkdir()
    (spec.release_root / "sealed").write_text("release", encoding="ascii")
    spec.incomplete_marker.write_text(spec.revision + "\n", encoding="ascii")
    receipt = {"runtime_reused": True, "receipt": "valid"}
    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)
    monkeypatch.setattr(package, "_existing_release", lambda _spec: receipt)
    monkeypatch.setattr(
        package,
        "_build_new_release",
        lambda _spec: pytest.fail("valid stale-state release was rebuilt"),
    )

    assert package.build_owner_runtime(spec) == receipt
    assert not spec.incomplete_marker.exists()
    assert spec.release_root.joinpath("sealed").read_text(encoding="ascii") == (
        "release"
    )
    assert _quarantined_releases(spec) == []


def test_invalid_unmarked_release_fails_closed_without_quarantine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.release_root.mkdir()
    sentinel = spec.release_root / "sealed"
    sentinel.write_text("do-not-replace", encoding="ascii")
    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)
    monkeypatch.setattr(
        package,
        "_existing_release",
        lambda _spec: (_ for _ in ()).throw(
            package.ProductionOwnerRuntimePackagingError(
                "production_owner_runtime_package_command_failed"
            )
        ),
    )

    with pytest.raises(
        package.ProductionOwnerRuntimePackagingError,
        match="command_failed",
    ):
        package.build_owner_runtime(spec)

    assert sentinel.read_text(encoding="ascii") == "do-not-replace"
    assert not spec.incomplete_marker.exists()
    assert _quarantined_releases(spec) == []


@pytest.mark.parametrize(
    "unsafe_shape",
    (
        "root_symlink",
        "marker_symlink",
        "marker_hardlink",
        "marker_writable",
        "marker_wrong_content",
    ),
)
def test_recovery_rejects_unsafe_shapes_without_moving_evidence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    unsafe_shape: str,
) -> None:
    spec = _spec(tmp_path)
    if unsafe_shape == "root_symlink":
        evidence_root = tmp_path / "evidence"
        evidence_root.mkdir()
        spec.release_root.symlink_to(evidence_root, target_is_directory=True)
    else:
        spec.release_root.mkdir()
        evidence_root = spec.release_root
        marker = spec.legacy_incomplete_marker
        if unsafe_shape == "marker_symlink":
            target = tmp_path / "foreign-marker"
            target.write_text(spec.revision + "\n", encoding="ascii")
            marker.symlink_to(target)
        else:
            marker.write_text(spec.revision + "\n", encoding="ascii")
            if unsafe_shape == "marker_hardlink":
                os.link(marker, tmp_path / "marker-alias")
            elif unsafe_shape == "marker_writable":
                marker.chmod(0o666)
            else:
                marker.write_text("b" * 40 + "\n", encoding="ascii")
    sentinel = evidence_root / "partial"
    sentinel.write_text("preserve-me", encoding="ascii")
    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)
    monkeypatch.setattr(
        package,
        "_existing_release",
        lambda _spec: pytest.fail("unsafe release was treated as reusable"),
    )

    with pytest.raises(
        package.ProductionOwnerRuntimePackagingError,
        match="recovery_invalid",
    ):
        package.build_owner_runtime(spec)

    assert sentinel.read_text(encoding="ascii") == "preserve-me"
    assert os.path.lexists(spec.release_root)
    assert _quarantined_releases(spec) == []


def test_repeated_failures_never_clobber_prior_quarantine(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.release_root.mkdir()
    spec.legacy_incomplete_marker.write_text(
        spec.revision + "\n",
        encoding="ascii",
    )
    (spec.release_root / "attempt").write_text("legacy", encoding="ascii")
    attempts = 0

    def reject_existing(
        _spec: package.OwnerRuntimeBuildSpec,
    ) -> dict[str, object]:
        raise package.ProductionOwnerRuntimePackagingError(
            "production_owner_runtime_package_command_failed"
        )

    def fail_build(current: package.OwnerRuntimeBuildSpec) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        current.release_root.mkdir(mode=0o700)
        (current.release_root / "attempt").write_text(
            f"attempt-{attempts}",
            encoding="ascii",
        )
        raise package.ProductionOwnerRuntimePackagingError(
            "production_owner_runtime_package_command_failed"
        )

    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)
    monkeypatch.setattr(package, "_existing_release", reject_existing)
    monkeypatch.setattr(package, "_build_new_release", fail_build)

    for _ in range(2):
        with pytest.raises(
            package.ProductionOwnerRuntimePackagingError,
            match="command_failed",
        ):
            package.build_owner_runtime(spec)

    quarantined = _quarantined_releases(spec)
    assert len(quarantined) == 2
    assert {
        path.joinpath("attempt").read_text(encoding="ascii")
        for path in quarantined
    } == {"legacy", "attempt-1"}
    assert spec.release_root.joinpath("attempt").read_text(encoding="ascii") == (
        "attempt-2"
    )
    assert spec.incomplete_marker.exists()


def test_active_revision_lock_prevents_quarantining_live_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)

    with package._revision_lock(spec):
        with pytest.raises(
            package.ProductionOwnerRuntimePackagingError,
            match="build_in_progress",
        ):
            package.build_owner_runtime(spec)

    assert not os.path.lexists(spec.release_root)
    assert not spec.incomplete_marker.exists()


def test_revision_lock_rejects_symlink_without_following_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    target = tmp_path / "foreign-lock"
    target.touch(mode=0o600)
    spec.lock_path.symlink_to(target)
    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)

    with pytest.raises(
        package.ProductionOwnerRuntimePackagingError,
        match="lock_invalid",
    ):
        package.build_owner_runtime(spec)

    assert spec.lock_path.is_symlink()
    assert target.read_bytes() == b""
    assert not os.path.lexists(spec.release_root)


def test_revision_lock_does_not_relabel_build_filesystem_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)
    monkeypatch.setattr(
        package,
        "_build_new_release",
        lambda _spec: (_ for _ in ()).throw(OSError("build filesystem error")),
    )

    with pytest.raises(OSError, match="build filesystem error"):
        package.build_owner_runtime(spec)

    assert spec.incomplete_marker.exists()


def test_state_and_quarantine_directory_entries_are_synced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    synced: list[Path] = []
    monkeypatch.setattr(
        package,
        "_fsync_directory",
        lambda path: synced.append(path),
    )

    package._create_incomplete_marker(spec)
    spec.release_root.mkdir()
    quarantined = package._quarantine_incomplete_release(spec)
    spec.release_root.mkdir()
    package._remove_incomplete_marker(spec)

    assert synced == [
        spec.release_base,
        quarantined,
        quarantined.parent,
        spec.release_base,
        spec.release_root,
        spec.release_base,
    ]
    assert quarantined.exists()
    assert not spec.incomplete_marker.exists()


def test_quarantine_restores_sealed_mode_after_non_oserror(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.incomplete_marker.write_text(spec.revision + "\n", encoding="ascii")
    spec.release_root.mkdir(mode=0o555)
    monkeypatch.setattr(
        package.os,
        "rename",
        lambda _source, _destination: (_ for _ in ()).throw(NewBuildObserved),
    )

    with pytest.raises(NewBuildObserved):
        package._quarantine_incomplete_release(spec)

    assert spec.release_root.stat().st_mode & 0o777 == 0o555
    assert _quarantined_releases(spec) == []


def test_failed_new_release_keeps_state_until_a_retry_can_recover(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)

    def fail_after_sealing(
        current: package.OwnerRuntimeBuildSpec,
    ) -> dict[str, object]:
        assert current.incomplete_marker.exists()
        current.release_root.mkdir(mode=0o555)
        raise package.ProductionOwnerRuntimePackagingError(
            "production_owner_runtime_package_attestation_failed"
        )

    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)
    monkeypatch.setattr(package, "_build_new_release", fail_after_sealing)

    with pytest.raises(
        package.ProductionOwnerRuntimePackagingError,
        match="attestation_failed",
    ):
        package.build_owner_runtime(spec)

    assert spec.incomplete_marker.read_text(encoding="ascii") == (
        spec.revision + "\n"
    )
    assert spec.release_root.exists()

    monkeypatch.setattr(
        package,
        "_existing_release",
        lambda _spec: (_ for _ in ()).throw(
            package.ProductionOwnerRuntimePackagingError(
                "production_owner_runtime_package_command_failed"
            )
        ),
    )
    _observe_fresh_build(monkeypatch, label="retry")

    with pytest.raises(NewBuildObserved):
        package.build_owner_runtime(spec)

    quarantined = _quarantined_releases(spec)
    assert len(quarantined) == 1
    assert quarantined[0].stat().st_mode & 0o777 == 0o555
    assert spec.release_root.joinpath("attempt").read_text(encoding="ascii") == (
        "retry"
    )
    assert spec.incomplete_marker.exists()


def test_successful_new_release_clears_sibling_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    receipt = {"runtime_reused": False, "receipt": "new"}

    def succeed(current: package.OwnerRuntimeBuildSpec) -> dict[str, object]:
        assert current.incomplete_marker.exists()
        current.release_root.mkdir(mode=0o555)
        return receipt

    monkeypatch.setattr(package, "_verify_clean_source", lambda _spec: None)
    monkeypatch.setattr(package, "_build_new_release", succeed)

    assert package.build_owner_runtime(spec) == receipt
    assert not spec.incomplete_marker.exists()
    assert spec.release_root.exists()


@pytest.mark.parametrize("marker_kind", ("sibling", "legacy"))
def test_verify_rejects_every_incomplete_release_state(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    marker_kind: str,
) -> None:
    spec = _spec(tmp_path)
    spec.release_root.mkdir()
    marker = (
        spec.incomplete_marker
        if marker_kind == "sibling"
        else spec.legacy_incomplete_marker
    )
    marker.write_text(spec.revision + "\n", encoding="ascii")
    monkeypatch.setattr(
        package,
        "_existing_release",
        lambda _spec: pytest.fail("incomplete release was verified"),
    )

    with pytest.raises(
        package.ProductionOwnerRuntimePackagingError,
        match="release_unavailable",
    ):
        package.verify_owner_runtime(spec)


def _publication_receipt(
    spec: package.OwnerRuntimeBuildSpec,
) -> dict[str, object]:
    unsigned: dict[str, object] = {
        "schema": package.RECEIPT_SCHEMA,
        "release_revision": spec.revision,
        "release_root": str(spec.release_root),
        "manifest_sha256": "1" * 64,
        "attestation_sha256": "2" * 64,
        "interpreter_sha256": "3" * 64,
        "pyvenv_cfg_sha256": "4" * 64,
        "wheel_sha256": "5" * 64,
        "runtime_reused": False,
        "non_editable_install": True,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    canonical = json.dumps(
        unsigned,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return {
        **unsigned,
        "receipt_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def test_publication_receipt_has_one_exact_shape_for_build_and_reuse(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    receipt = _publication_receipt(spec)

    assert package.validate_publication_receipt(receipt, spec=spec) == receipt

    reused = _publication_receipt(spec)
    reused["runtime_reused"] = True
    unsigned = {
        name: value
        for name, value in reused.items()
        if name != "receipt_sha256"
    }
    canonical = json.dumps(
        unsigned,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    reused["receipt_sha256"] = hashlib.sha256(canonical).hexdigest()

    assert package.validate_publication_receipt(reused, spec=spec) == reused


@pytest.mark.parametrize("field", ["wheel_sha256", "receipt_sha256"])
def test_publication_receipt_rejects_missing_or_unbound_digest(
    tmp_path: Path,
    field: str,
) -> None:
    spec = _spec(tmp_path)
    receipt = _publication_receipt(spec)
    if field == "wheel_sha256":
        del receipt[field]
    else:
        receipt[field] = "f" * 64

    with pytest.raises(
        package.ProductionOwnerRuntimePackagingError,
        match="receipt_invalid",
    ):
        package.validate_publication_receipt(receipt, spec=spec)


def test_retained_wheel_requires_one_regular_non_symlink_artifact(
    tmp_path: Path,
) -> None:
    spec = _spec(tmp_path)
    spec.artifact_root.mkdir(parents=True)
    wheel = spec.artifact_root / "hermes_agent-1.0-py3-none-any.whl"
    wheel.write_bytes(b"sealed-wheel")

    assert package._retained_wheel(spec) == wheel

    (spec.artifact_root / "unexpected.txt").write_text("not a wheel")
    with pytest.raises(
        package.ProductionOwnerRuntimePackagingError,
        match="wheel_invalid",
    ):
        package._retained_wheel(spec)
