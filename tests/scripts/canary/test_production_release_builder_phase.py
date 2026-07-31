from __future__ import annotations

import hashlib
import inspect
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import pytest

from scripts.canary import production_release_builder_phase as phase
from scripts.canary import production_release_builder_runtime as builder


REVISION = "a" * 40
WHEEL_NAME = "muncho_runtime-1.0-py3-none-any.whl"
WHEEL_BYTES = b"exact wheel archive bytes"


def _self_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    unsigned = dict(value)
    return {
        **unsigned,
        field: phase.sha256_bytes(phase.canonical_bytes(unsigned)),
    }


def _write_document(path: Path, value: Mapping[str, Any]) -> str:
    raw = phase.canonical_bytes(value) + b"\n"
    if path.exists():
        path.chmod(0o644)
    path.write_bytes(raw)
    path.chmod(0o444)
    return hashlib.sha256(raw).hexdigest()


def _blob_oid(raw: bytes) -> str:
    return hashlib.sha1(f"blob {len(raw)}\0".encode("ascii") + raw).hexdigest()


class Resolver:
    def __init__(self, *, alias: bool = False) -> None:
        self.alias = alias

    def user_by_name(self, _name: str) -> phase.IdentityRecord:
        return phase.IdentityRecord(
            phase.BUILDER_USER,
            phase.BUILDER_UID,
            phase.BUILDER_GID,
        )

    def user_by_uid(self, _uid: int) -> phase.IdentityRecord:
        return self.user_by_name(phase.BUILDER_USER)

    def group_by_name(self, _name: str) -> phase.IdentityRecord:
        return phase.IdentityRecord(phase.BUILDER_GROUP, phase.BUILDER_GID)

    def group_by_gid(self, _gid: int) -> phase.IdentityRecord:
        return self.group_by_name(phase.BUILDER_GROUP)

    def all_users(self) -> Sequence[phase.IdentityRecord]:
        records = [self.user_by_name(phase.BUILDER_USER)]
        if self.alias:
            records.append(
                phase.IdentityRecord(
                    "builder-alias",
                    phase.BUILDER_UID,
                    phase.BUILDER_GID,
                )
            )
        return tuple(records)

    def all_groups(self) -> Sequence[phase.IdentityRecord]:
        return (self.group_by_name(phase.BUILDER_GROUP),)


class FakeRunner:
    def __init__(
        self,
        *,
        wheel_to_swap: Path | None = None,
    ) -> None:
        self.calls: list[
            tuple[tuple[str, ...], Path, dict[str, str], tuple[int, ...]]
        ] = []
        self.wheel_to_swap = wheel_to_swap

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        pass_fds: tuple[int, ...],
    ) -> None:
        call = (tuple(argv), cwd, dict(env), pass_fds)
        self.calls.append(call)
        candidate = cwd / phase.CANDIDATE_NAME
        if argv[1] == "venv":
            venv_bin = candidate / ".venv" / "bin"
            venv_bin.mkdir(parents=True)
            interpreter = venv_bin / "python"
            interpreter.write_bytes(b"exact venv interpreter")
            interpreter.chmod(0o755)
            (venv_bin / "python3").symlink_to("python")
            (venv_bin / "python3.11").symlink_to("python")
            (candidate / ".venv" / "pyvenv.cfg").write_text(
                "include-system-site-packages = false\n",
                encoding="utf-8",
            )
            if self.wheel_to_swap is not None:
                self.wheel_to_swap.unlink()
                self.wheel_to_swap.write_bytes(b"swapped wheel")
                self.wheel_to_swap.chmod(0o444)
        elif argv[1:3] == ("pip", "install"):
            package = (
                candidate
                / ".venv"
                / "lib"
                / "python3.11"
                / "site-packages"
                / "muncho_runtime.py"
            )
            package.parent.mkdir(parents=True)
            package.write_text("VALUE = 1\n", encoding="utf-8")
        else:  # pragma: no cover - a contract regression makes this reachable
            raise AssertionError(argv)


@dataclass
class Fixture:
    job_root: Path
    request_path: Path
    input_root: Path
    output_root: Path
    python_path: Path
    source_manifest: dict[str, Any]
    runtime_manifest: dict[str, Any]
    request: dict[str, Any]
    runner: FakeRunner


def _fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Fixture:
    job_root = tmp_path / "jobs"
    input_root = job_root / REVISION / "input"
    output_root = job_root / REVISION / "output"
    blob_root = input_root / phase.SOURCE_BLOB_DIRECTORY_NAME
    wheel_root = input_root / phase.RUNTIME_WHEEL_DIRECTORY_NAME
    for directory in (input_root, output_root, blob_root, wheel_root):
        directory.mkdir(parents=True, exist_ok=True)
        directory.chmod(0o755 if directory != output_root else 0o700)

    source_files = {
        phase.ENTRYPOINT_RELATIVE_PATH: b"#!/usr/bin/env python3\nVALUE = 1\n",
        phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH: (
            b"#!/usr/bin/env python3\nROTATION = 1\n"
        ),
        "README.md": b"pinned source\n",
    }
    entry_records: list[tuple[str, str, bytes]] = []
    blob_records: dict[str, dict[str, Any]] = {}
    for relative, raw in source_files.items():
        oid = _blob_oid(raw)
        entry_records.append((relative, oid, raw))
        blob_path = blob_root / f"{oid}.blob"
        blob_path.write_bytes(raw)
        blob_path.chmod(0o444)
        blob_records[oid] = {
            "object_id": oid,
            "filename": blob_path.name,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "size": len(raw),
        }
    entry_records.sort(key=lambda item: item[0].encode("utf-8"))
    listing = b"".join(
        f"100644 blob {oid}\t{relative}".encode("utf-8") + b"\x00"
        for relative, oid, _raw in entry_records
    )
    entries = builder.parse_git_tree(listing)
    tree_oid = builder._reconstruct_git_tree_oid(entries)
    tree_path = input_root / phase.TREE_LISTING_NAME
    tree_path.write_bytes(listing)
    tree_path.chmod(0o444)
    source_unsigned: dict[str, Any] = {
        "schema": phase.SOURCE_V3_MANIFEST_SCHEMA,
        "release_revision": REVISION,
        "source_tree_oid": tree_oid,
        "object_format": "sha1",
        "tree_listing_name": phase.TREE_LISTING_NAME,
        "tree_listing_sha256": hashlib.sha256(listing).hexdigest(),
        "tree_listing_size": len(listing),
        "tree_entry_count": len(entries),
        "blob_directory_name": phase.SOURCE_BLOB_DIRECTORY_NAME,
        "blobs": [blob_records[oid] for oid in sorted(blob_records)],
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    source_manifest = _self_hash(source_unsigned, "manifest_sha256")
    source_digest = _write_document(
        input_root / phase.SOURCE_MANIFEST_NAME,
        source_manifest,
    )

    wheel_path = wheel_root / WHEEL_NAME
    wheel_path.write_bytes(WHEEL_BYTES)
    wheel_path.chmod(0o444)
    runtime_unsigned: dict[str, Any] = {
        "schema": phase.RUNTIME_DEPENDENCY_MANIFEST_SCHEMA,
        "release_revision": REVISION,
        "wheel_directory_name": phase.RUNTIME_WHEEL_DIRECTORY_NAME,
        "wheels": [
            {
                "filename": WHEEL_NAME,
                "sha256": hashlib.sha256(WHEEL_BYTES).hexdigest(),
                "size": len(WHEEL_BYTES),
            }
        ],
        "installation": dict(phase._INSTALLATION),
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    runtime_manifest = _self_hash(runtime_unsigned, "manifest_sha256")
    runtime_digest = _write_document(
        input_root / phase.RUNTIME_MANIFEST_NAME,
        runtime_manifest,
    )

    uv_path = input_root / phase.UV_NAME
    uv_path.write_bytes(b"held uv binary")
    uv_path.chmod(0o555)
    python_path = tmp_path / "usr" / "bin" / "python3.11"
    python_path.parent.mkdir(parents=True)
    python_path.write_bytes(b"held python binary")
    python_path.chmod(0o555)
    monkeypatch.setattr(
        phase,
        "_PYTHON_PATH",
        re.compile(f"^{re.escape(str(python_path))}$"),
    )

    request_unsigned: dict[str, Any] = {
        "schema": phase.REQUEST_SCHEMA,
        "job_id": REVISION,
        "release_revision": REVISION,
        "source_tree_oid": tree_oid,
        "source_v3_manifest_name": phase.SOURCE_MANIFEST_NAME,
        "source_v3_manifest_sha256": source_digest,
        "runtime_dependency_manifest_name": phase.RUNTIME_MANIFEST_NAME,
        "runtime_dependency_manifest_sha256": runtime_digest,
        "uv_name": phase.UV_NAME,
        "uv_sha256": hashlib.sha256(uv_path.read_bytes()).hexdigest(),
        "uv_size": uv_path.stat().st_size,
        "python_executable_path": str(python_path),
        "python_executable_sha256": hashlib.sha256(
            python_path.read_bytes()
        ).hexdigest(),
        "python_executable_size": python_path.stat().st_size,
        "candidate_name": phase.CANDIDATE_NAME,
        "interpreter_relative_path": phase.INTERPRETER_RELATIVE_PATH,
        "entrypoint_relative_path": phase.ENTRYPOINT_RELATIVE_PATH,
        "builder_identity": dict(phase._BUILDER_IDENTITY),
        "resume_policy": "reject-nonempty-output-requires-root-cleanup",
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    request = _self_hash(request_unsigned, "request_sha256")
    request_path = input_root / phase.REQUEST_NAME
    _write_document(request_path, request)
    runner = FakeRunner()
    return Fixture(
        job_root=job_root,
        request_path=request_path,
        input_root=input_root,
        output_root=output_root,
        python_path=python_path,
        source_manifest=source_manifest,
        runtime_manifest=runtime_manifest,
        request=request,
        runner=runner,
    )


def _run(
    fixture: Fixture,
    *,
    resolver: Resolver | None = None,
    runner: FakeRunner | None = None,
    checkpoint=None,
) -> Mapping[str, Any]:
    return phase._run_builder_phase_for_test(
        fixture.request_path,
        production=False,
        job_root=fixture.job_root,
        identity_resolver=Resolver() if resolver is None else resolver,
        command_runner=fixture.runner if runner is None else runner,
        effective_uid=phase.BUILDER_UID,
        effective_gid=phase.BUILDER_GID,
        test_authority_uid=os.lstat(fixture.input_root).st_uid,
        test_authority_gid=os.lstat(fixture.input_root).st_gid,
        test_physical_builder_uid=os.lstat(
            fixture.output_root
        ).st_uid,
        test_physical_builder_gid=os.lstat(
            fixture.output_root
        ).st_gid,
        test_xattr_reader=lambda _descriptor: (),
        checkpoint=checkpoint,
    )


def _rewrite_request(fixture: Fixture, **changes: Any) -> None:
    unsigned = {
        key: value for key, value in fixture.request.items() if key != "request_sha256"
    }
    unsigned.update(changes)
    fixture.request = _self_hash(unsigned, "request_sha256")
    _write_document(fixture.request_path, fixture.request)


def _rewrite_source_manifest(
    fixture: Fixture,
    **changes: Any,
) -> None:
    unsigned = {
        key: value
        for key, value in fixture.source_manifest.items()
        if key != "manifest_sha256"
    }
    unsigned.update(changes)
    fixture.source_manifest = _self_hash(unsigned, "manifest_sha256")
    digest = _write_document(
        fixture.input_root / phase.SOURCE_MANIFEST_NAME,
        fixture.source_manifest,
    )
    _rewrite_request(fixture, source_v3_manifest_sha256=digest)


def test_offline_build_is_exact_receipt_last_and_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    checkpoints: list[str] = []

    def denied_fchown(
        _descriptor: int,
        _uid: int,
        _gid: int,
    ) -> None:
        raise AssertionError(
            "the Debian 12 builder unit deny-lists @privileged/@chown"
        )

    monkeypatch.setattr(builder.os, "fchown", denied_fchown)
    receipt = _run(fixture, checkpoint=checkpoints.append)

    candidate = fixture.output_root / phase.CANDIDATE_NAME
    assert receipt["schema"] == phase.TERMINAL_RECEIPT_SCHEMA
    assert receipt["terminal"] is True
    assert (
        receipt["builder_request_sha256"]
        == hashlib.sha256(fixture.request_path.read_bytes()).hexdigest()
    )
    assert stat.S_IMODE(candidate.stat().st_mode) == 0o555
    assert (
        candidate / phase.TERMINAL_RECEIPT_NAME
    ).read_bytes() == phase.canonical_bytes(receipt) + b"\n"
    assert (
        candidate / phase.INTERPRETER_RELATIVE_PATH
    ).read_bytes() == fixture.python_path.read_bytes()
    payload = json.loads(
        (candidate / phase.PAYLOAD_MANIFEST_NAME).read_text(encoding="ascii")
    )
    assert phase.validate_payload_manifest(payload) == payload
    assert phase.validate_terminal_receipt(receipt) == receipt
    assert checkpoints[-3:] == [
        "payload_manifest_written",
        "terminal_receipt_written",
        "completed",
    ]
    assert len(fixture.runner.calls) == 2
    venv, install = fixture.runner.calls
    assert venv[0][0].startswith("/proc/self/fd/")
    assert venv[0][1:3] == ("venv", "--python")
    assert "--offline" in venv[0]
    assert "--no-index" in venv[0]
    assert "--no-python-downloads" in venv[0]
    assert install[0][1:3] == ("pip", "install")
    for flag in (
        "--offline",
        "--no-index",
        "--no-deps",
        "--only-binary",
        "--no-sources",
        "--exact",
        "--strict",
    ):
        assert flag in install[0]
    # Current production uv rejects --no-build together with --only-binary.
    # Explicit, verified wheel paths plus --only-binary=:all: and --no-sources
    # preserve the no-source-build contract without the incompatible flag.
    assert "--no-build" not in install[0]
    assert install[0][-1].endswith(WHEEL_NAME)
    assert set(venv[2]) == set(phase._command_environment())
    assert not {
        "PYTHONPATH",
        "PIP_INDEX_URL",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "OPENAI_API_KEY",
        "DISCORD_BOT_TOKEN",
    } & set(venv[2])
    assert venv[3] == install[3]
    assert len(venv[3]) == 1


def test_unit_input_rotation_stager_is_exactly_purpose_and_entrypoint_bound(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _rewrite_request(
        fixture,
        schema=phase.UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA,
        purpose=phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE,
        entrypoint_relative_path=(
            phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
        ),
    )

    receipt = _run(fixture)
    candidate_entrypoint = (
        fixture.output_root
        / phase.CANDIDATE_NAME
        / phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
    )

    assert (
        receipt["schema"]
        == phase.UNIT_INPUT_ROTATION_STAGER_TERMINAL_RECEIPT_SCHEMA
    )
    assert receipt["purpose"] == phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE
    assert (
        receipt["entrypoint_relative_path"]
        == phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
    )
    assert receipt["entrypoint_sha256"] == hashlib.sha256(
        candidate_entrypoint.read_bytes()
    ).hexdigest()
    assert phase.validate_terminal_receipt(receipt) == receipt


@pytest.mark.parametrize(
    ("purpose", "entrypoint"),
    [
        ("release-updater", phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH),
        (phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE, phase.ENTRYPOINT_RELATIVE_PATH),
    ],
)
def test_unit_input_rotation_stager_rejects_mixed_exact_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    purpose: str,
    entrypoint: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _rewrite_request(
        fixture,
        schema=phase.UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA,
        purpose=purpose,
        entrypoint_relative_path=entrypoint,
    )

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_request_invalid",
    ):
        _run(fixture)
    assert fixture.runner.calls == []
    assert list(fixture.output_root.iterdir()) == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("candidate_name", "../candidate"),
        ("entrypoint_relative_path", "../entrypoint.py"),
        ("source_v3_manifest_name", "../../source.json"),
    ],
)
def test_request_traversal_and_path_substitution_are_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _rewrite_request(fixture, **{field: value})

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_request_invalid",
    ):
        _run(fixture)


def test_reserved_source_root_is_rejected_before_runner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    entrypoint_oid = next(
        item["object_id"]
        for item in fixture.source_manifest["blobs"]
        if (fixture.input_root / phase.SOURCE_BLOB_DIRECTORY_NAME / item["filename"])
        .read_bytes()
        .startswith(b"#!")
    )
    listing = f"100644 blob {entrypoint_oid}\t.venv/escape.py".encode() + b"\x00"
    tree_path = fixture.input_root / phase.TREE_LISTING_NAME
    tree_path.chmod(0o644)
    tree_path.write_bytes(listing)
    tree_path.chmod(0o444)
    entries = builder.parse_git_tree(listing)
    _rewrite_source_manifest(
        fixture,
        source_tree_oid=builder._reconstruct_git_tree_oid(entries),
        tree_listing_sha256=hashlib.sha256(listing).hexdigest(),
        tree_listing_size=len(listing),
        tree_entry_count=1,
        blobs=[
            item
            for item in fixture.source_manifest["blobs"]
            if item["object_id"] == entrypoint_oid
        ],
    )
    _rewrite_request(
        fixture,
        source_tree_oid=fixture.source_manifest["source_tree_oid"],
    )

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_source_path_reserved",
    ):
        _run(fixture)
    assert fixture.runner.calls == []


def test_missing_update_entrypoint_fails_before_output_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    tree_path = fixture.input_root / phase.TREE_LISTING_NAME
    records = tree_path.read_bytes()[:-1].split(b"\x00")
    entrypoint_suffix = (
        b"\t" + phase.ENTRYPOINT_RELATIVE_PATH.encode("utf-8")
    )
    retained_records = [
        record for record in records if not record.endswith(entrypoint_suffix)
    ]
    assert len(retained_records) + 1 == len(records)
    listing = b"\x00".join(retained_records) + b"\x00"
    tree_path.chmod(0o644)
    tree_path.write_bytes(listing)
    tree_path.chmod(0o444)
    entries = builder.parse_git_tree(listing)
    retained_oids = {entry.object_id for entry in entries}
    removed = [
        item
        for item in fixture.source_manifest["blobs"]
        if item["object_id"] not in retained_oids
    ]
    assert len(removed) == 1
    (
        fixture.input_root
        / phase.SOURCE_BLOB_DIRECTORY_NAME
        / removed[0]["filename"]
    ).unlink()
    tree_oid = builder._reconstruct_git_tree_oid(entries)
    _rewrite_source_manifest(
        fixture,
        source_tree_oid=tree_oid,
        tree_listing_sha256=hashlib.sha256(listing).hexdigest(),
        tree_listing_size=len(listing),
        tree_entry_count=len(entries),
        blobs=[
            item
            for item in fixture.source_manifest["blobs"]
            if item["object_id"] in retained_oids
        ],
    )
    _rewrite_request(fixture, source_tree_oid=tree_oid)

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_entrypoint_missing",
    ):
        _run(fixture)

    assert fixture.runner.calls == []
    assert list(fixture.output_root.iterdir()) == []


def test_mixed_tree_listing_cannot_claim_the_authorized_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    original_tree_oid = fixture.request["source_tree_oid"]
    listing = (
        (fixture.input_root / phase.TREE_LISTING_NAME)
        .read_bytes()
        .replace(b"README.md", b"RENAMED.md")
    )
    tree_path = fixture.input_root / phase.TREE_LISTING_NAME
    tree_path.chmod(0o644)
    tree_path.write_bytes(listing)
    tree_path.chmod(0o444)
    _rewrite_source_manifest(
        fixture,
        tree_listing_sha256=hashlib.sha256(listing).hexdigest(),
        tree_listing_size=len(listing),
    )
    assert fixture.request["source_tree_oid"] == original_tree_oid

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_failed",
    ):
        _run(fixture)
    assert fixture.runner.calls == []


def test_root_wheel_swap_is_detected_even_after_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    wheel = fixture.input_root / phase.RUNTIME_WHEEL_DIRECTORY_NAME / WHEEL_NAME
    runner = FakeRunner(wheel_to_swap=wheel)

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_failed",
    ):
        _run(fixture, runner=runner)
    retained = (
        fixture.output_root
        / phase.CANDIDATE_NAME
        / phase.RETAINED_WHEEL_DIRECTORY_NAME
        / WHEEL_NAME
    )
    assert retained.read_bytes() == WHEEL_BYTES
    assert not (
        fixture.output_root / phase.CANDIDATE_NAME / phase.TERMINAL_RECEIPT_NAME
    ).exists()


def test_unknown_wheel_in_input_directory_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    extra = (
        fixture.input_root
        / phase.RUNTIME_WHEEL_DIRECTORY_NAME
        / "unknown-1.0-py3-none-any.whl"
    )
    extra.write_bytes(b"unknown")
    extra.chmod(0o444)

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_runtime_wheel_set_invalid",
    ):
        _run(fixture)
    assert fixture.runner.calls == []


def test_uid_alias_in_nss_is_rejected_before_inputs_are_opened(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_nss_identity_invalid",
    ):
        _run(fixture, resolver=Resolver(alias=True))
    assert fixture.runner.calls == []
    assert list(fixture.output_root.iterdir()) == []


def test_wrong_effective_uid_is_rejected_even_with_valid_nss(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_effective_identity_invalid",
    ):
        phase._run_builder_phase_for_test(
            fixture.request_path,
            production=False,
            job_root=fixture.job_root,
            identity_resolver=Resolver(),
            command_runner=fixture.runner,
            effective_uid=phase.BUILDER_UID + 1,
            effective_gid=phase.BUILDER_GID,
        )
    assert fixture.runner.calls == []


def test_public_builder_phase_has_no_authority_or_test_seams() -> None:
    assert tuple(inspect.signature(phase.run_builder_phase).parameters) == (
        "request_path",
    )


def test_terminal_receipt_validator_rejects_rehashed_semantic_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    receipt = dict(_run(fixture))
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    unsigned["terminal"] = False
    tampered = _self_hash(unsigned, "receipt_sha256")

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_terminal_receipt_invalid",
    ):
        phase.validate_terminal_receipt(tampered)


def test_rotation_stager_terminal_receipt_rejects_rehashed_purpose_tamper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    _rewrite_request(
        fixture,
        schema=phase.UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA,
        purpose=phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE,
        entrypoint_relative_path=(
            phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
        ),
    )
    receipt = dict(_run(fixture))
    unsigned = {
        key: value for key, value in receipt.items() if key != "receipt_sha256"
    }
    unsigned["purpose"] = "release-updater"
    tampered = _self_hash(unsigned, "receipt_sha256")

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_terminal_receipt_invalid",
    ):
        phase.validate_terminal_receipt(tampered)


def test_partial_output_is_nonresumable_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    partial = fixture.output_root / "partial"
    partial.write_text("crash evidence", encoding="utf-8")

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_output_not_empty",
    ):
        _run(fixture)
    assert partial.read_text(encoding="utf-8") == "crash evidence"
    assert fixture.runner.calls == []


def test_crash_after_payload_manifest_never_publishes_terminal_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    def checkpoint(name: str) -> None:
        if name == "payload_manifest_written":
            raise phase.ProductionReleaseBuilderPhaseError("injected_builder_crash")

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="injected_builder_crash",
    ):
        _run(fixture, checkpoint=checkpoint)
    candidate = fixture.output_root / phase.CANDIDATE_NAME
    assert (candidate / phase.PAYLOAD_MANIFEST_NAME).is_file()
    assert not (candidate / phase.TERMINAL_RECEIPT_NAME).exists()

    with pytest.raises(
        phase.ProductionReleaseBuilderPhaseError,
        match="release_builder_phase_output_not_empty",
    ):
        _run(fixture)
