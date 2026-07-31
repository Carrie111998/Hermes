from __future__ import annotations

import hashlib
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Mapping

import pytest

from scripts.canary import production_release_builder_runtime as runtime


REVISION = "a" * 40
BUILDER_UID = 29104
BUILDER_GID = 29104


def _git_oid(raw: bytes, algorithm: str = "sha1") -> str:
    digest = hashlib.new(algorithm)
    digest.update(f"blob {len(raw)}\0".encode("ascii"))
    digest.update(raw)
    return digest.hexdigest()


def _git_tree_oid(entries: list[tuple[str, int, str]]) -> str:
    root: dict[bytes, object] = {}
    for path, mode, object_id in entries:
        node = root
        parts = [part.encode() for part in path.split("/")]
        for part in parts[:-1]:
            node = node.setdefault(part, {})  # type: ignore[assignment]
            assert isinstance(node, dict)
        node[parts[-1]] = (mode, object_id)

    def digest(node: dict[bytes, object]) -> str:
        records: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            if isinstance(value, dict):
                mode = b"40000"
                object_id = digest(value)
                key = name + b"/"
            else:
                assert isinstance(value, tuple)
                mode = f"{value[0]:o}".encode()
                object_id = value[1]
                key = name
            records.append((key, mode + b" " + name + b"\0" + bytes.fromhex(object_id)))
        payload = b"".join(record for _key, record in sorted(records))
        result = hashlib.sha1()
        result.update(f"tree {len(payload)}\0".encode())
        result.update(payload)
        return result.hexdigest()

    return digest(root)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _gid(path: Path) -> int:
    return os.lstat(path).st_gid


def _empty_xattrs(_descriptor: int) -> tuple[str, ...]:
    return ()


def _identities() -> runtime.ReleaseIdentities:
    return runtime.ReleaseIdentities(
        builder_uid=BUILDER_UID,
        builder_gid=BUILDER_GID,
        reserved_runtime_uids=(31001, 31002),
        reserved_runtime_gids=(32001, 32002, 32003),
    )


def _systemd_properties(fragment: Path, *, cgroup: str) -> Mapping[str, Any]:
    return {
        "Id": "muncho-release-builder@tx-1.service",
        "FragmentPath": str(fragment),
        "DropInPaths": "",
        "LoadState": "loaded",
        "ActiveState": "inactive",
        "SubState": "dead",
        "MainPID": "0",
        "ExecMainPID": "0",
        "Result": "success",
        "ExecMainCode": "exited",
        "ExecMainStatus": "0",
        "InvocationID": "1" * 32,
        "ControlGroup": cgroup,
    }


def _process_free_evidence(
    tmp_path: Path,
    *,
    create_cgroup: bool = False,
    observation_only: bool = False,
    xattr_reader=_empty_xattrs,
) -> Mapping[str, Any]:
    tmp_path.mkdir(parents=True)
    fragment = tmp_path / "muncho-release-builder@.service"
    fragment.write_bytes(b"[Service]\nType=oneshot\n")
    fragment.chmod(0o444)
    wrapper = tmp_path / "muncho-release-builder-phase"
    wrapper.write_bytes(b"#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o555)
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    cgroup_root.mkdir()
    (cgroup_root / "system.slice").mkdir()
    proc_root.mkdir()
    control_group = "/system.slice/muncho-release-builder@tx-1.service"
    if create_cgroup:
        service_cgroup = cgroup_root / control_group.removeprefix("/")
        service_cgroup.mkdir(parents=True)
        (service_cgroup / "cgroup.procs").write_bytes(b"")
    observation = runtime.validate_process_free_evidence(
        _systemd_properties(fragment, cgroup=control_group),
        expected_unit="muncho-release-builder@tx-1.service",
        expected_fragment=fragment,
        expected_fragment_sha256=_sha256(fragment),
        expected_wrapper=wrapper,
        expected_wrapper_sha256=_sha256(wrapper),
        expected_control_group=control_group,
        builder_uid=BUILDER_UID,
        builder_gid=BUILDER_GID,
        cgroup_root=cgroup_root,
        proc_root=proc_root,
        authority_uid=os.geteuid(),
        authority_gid=_gid(fragment),
        xattr_reader=xattr_reader,
    )
    if observation_only:
        return observation
    return runtime.build_process_free_evidence_set(
        observation,
        observation,
        builder_uid=BUILDER_UID,
        builder_gid=BUILDER_GID,
    )


def test_process_free_evidence_accepts_revision_qualified_builder_unit(
    tmp_path: Path,
) -> None:
    fragment = tmp_path / "muncho-release-builder-v2@.service"
    fragment.write_bytes(b"[Service]\nType=oneshot\n")
    fragment.chmod(0o444)
    wrapper = tmp_path / "muncho-release-foundation-exec-v2"
    wrapper.write_bytes(b"#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o555)
    cgroup_root = tmp_path / "cgroup"
    (cgroup_root / "system.slice").mkdir(parents=True)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    unit = "muncho-release-builder-v2@" + "a" * 40 + ".service"
    control_group = f"/system.slice/{unit}"
    properties = {
        **_systemd_properties(fragment, cgroup=control_group),
        "Id": unit,
    }

    evidence = runtime.validate_process_free_evidence(
        properties,
        expected_unit=unit,
        expected_fragment=fragment,
        expected_fragment_sha256=_sha256(fragment),
        expected_wrapper=wrapper,
        expected_wrapper_sha256=_sha256(wrapper),
        expected_control_group=control_group,
        builder_uid=BUILDER_UID,
        builder_gid=BUILDER_GID,
        cgroup_root=cgroup_root,
        proc_root=proc_root,
        authority_uid=os.geteuid(),
        authority_gid=_gid(fragment),
        xattr_reader=_empty_xattrs,
    )

    assert evidence["unit"] == unit


def test_parse_and_materialize_exact_git_tree(tmp_path: Path) -> None:
    blobs = {
        "bin/run.py": b"#!/usr/bin/env python3\nprint('ok')\n",
        "pkg/data.txt": b"payload\n",
    }
    objects = tmp_path / "objects"
    objects.mkdir()
    records: list[bytes] = []
    by_oid: dict[str, Path] = {}
    for path, raw in sorted(blobs.items()):
        object_id = _git_oid(raw)
        object_path = objects / object_id
        object_path.write_bytes(raw)
        object_path.chmod(0o600)
        by_oid[object_id] = object_path
        mode = b"100755" if path == "bin/run.py" else b"100644"
        records.append(mode + b" blob " + object_id.encode() + b"\t" + path.encode())
    entries = runtime.parse_git_tree(b"\x00".join(records) + b"\x00")
    source_tree_oid = _git_tree_oid([
        (entry.path, entry.mode, entry.object_id) for entry in entries
    ])

    def open_blob(
        entry: runtime.GitTreeEntry,
    ) -> runtime.HeldRegularFile:
        return runtime.open_held_regular(
            by_oid[entry.object_id],
            expected_uid=os.geteuid(),
            expected_gid=_gid(by_oid[entry.object_id]),
            allowed_modes=frozenset({0o600}),
            maximum_bytes=1024,
        )

    result = runtime.materialize_git_tree(
        entries,
        tmp_path / "source",
        revision=REVISION,
        source_tree_oid=source_tree_oid,
        open_blob=open_blob,
        destination_uid=os.geteuid(),
        destination_gid=_gid(tmp_path),
        parent_uid=os.geteuid(),
        parent_gid=_gid(tmp_path),
        _xattr_reader=_empty_xattrs,
    )

    assert result["source_revision"] == REVISION
    assert result["source_tree_oid"] == source_tree_oid
    assert result["entry_count"] == 2
    assert (tmp_path / "source/pkg/data.txt").read_bytes() == blobs["pkg/data.txt"]
    assert (tmp_path / "source/bin/run.py").read_bytes() == blobs["bin/run.py"]
    assert stat.S_IMODE((tmp_path / "source").stat().st_mode) == 0o555
    assert stat.S_IMODE((tmp_path / "source/pkg/data.txt").stat().st_mode) == 0o444
    assert stat.S_IMODE((tmp_path / "source/bin/run.py").stat().st_mode) == 0o555


@pytest.mark.parametrize(
    "record",
    [
        b"120000 blob " + b"1" * 40 + b"\tlink\x00",
        b"160000 commit " + b"1" * 40 + b"\tsubmodule\x00",
        b"100644 blob " + b"1" * 40 + b"\t../escape\x00",
        b"100644 blob " + b"1" * 40 + b"\t.git/config\x00",
        b"100644 blob " + b"1" * 40 + b"\ta\\b\x00",
    ],
)
def test_git_tree_rejects_non_regular_or_unsafe_entries(record: bytes) -> None:
    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="git_(tree|path)_invalid",
    ):
        runtime.parse_git_tree(record)


def test_git_tree_rejects_file_directory_prefix_collision() -> None:
    raw = (
        b"100644 blob "
        + b"1" * 40
        + b"\ta\x00"
        + b"100644 blob "
        + b"2" * 40
        + b"\ta/b\x00"
    )

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="git_tree_invalid",
    ):
        runtime.parse_git_tree(raw)


def test_materialization_rejects_blob_path_swap(tmp_path: Path) -> None:
    raw = b"trusted blob"
    object_id = _git_oid(raw)
    source = tmp_path / object_id
    source.write_bytes(raw)
    source.chmod(0o600)
    entries = runtime.parse_git_tree(
        b"100644 blob " + object_id.encode() + b"\tpayload.bin\x00"
    )
    source_tree_oid = _git_tree_oid([
        (entry.path, entry.mode, entry.object_id) for entry in entries
    ])

    @contextmanager
    def swapped_blob(
        _entry: runtime.GitTreeEntry,
    ) -> Any:
        held = runtime.open_held_regular(
            source,
            expected_uid=os.geteuid(),
            expected_gid=_gid(source),
            allowed_modes=frozenset({0o600}),
            maximum_bytes=1024,
        )
        source.rename(tmp_path / "original")
        source.write_bytes(b"replacement")
        source.chmod(0o600)
        try:
            yield held
        finally:
            held.close()

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="path_binding_changed",
    ):
        runtime.materialize_git_tree(
            entries,
            tmp_path / "source",
            revision=REVISION,
            source_tree_oid=source_tree_oid,
            open_blob=swapped_blob,
            destination_uid=os.geteuid(),
            destination_gid=_gid(tmp_path),
            parent_uid=os.geteuid(),
            parent_gid=_gid(tmp_path),
            _xattr_reader=_empty_xattrs,
        )


@pytest.mark.parametrize("variant", ["subset", "mixed"])
def test_materialization_rejects_listing_not_bound_to_source_tree_oid(
    tmp_path: Path,
    variant: str,
) -> None:
    object_ids = [_git_oid(b"one"), _git_oid(b"two"), _git_oid(b"mixed")]
    full = runtime.parse_git_tree(
        b"100644 blob "
        + object_ids[0].encode()
        + b"\ta\x00"
        + b"100644 blob "
        + object_ids[1].encode()
        + b"\tb\x00"
    )
    source_tree_oid = _git_tree_oid([
        (entry.path, entry.mode, entry.object_id) for entry in full
    ])
    if variant == "subset":
        supplied = full[:1]
    else:
        supplied = runtime.parse_git_tree(
            b"100644 blob "
            + object_ids[0].encode()
            + b"\ta\x00"
            + b"100644 blob "
            + object_ids[2].encode()
            + b"\tb\x00"
        )

    def must_not_open(_entry: runtime.GitTreeEntry) -> runtime.HeldRegularFile:
        raise AssertionError("tree mismatch must be rejected before blob access")

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="git_tree_oid_mismatch",
    ):
        runtime.materialize_git_tree(
            supplied,
            tmp_path / "source",
            revision=REVISION,
            source_tree_oid=source_tree_oid,
            open_blob=must_not_open,
            destination_uid=os.geteuid(),
            destination_gid=_gid(tmp_path),
            parent_uid=os.geteuid(),
            parent_gid=_gid(tmp_path),
            _xattr_reader=_empty_xattrs,
        )
    assert not (tmp_path / "source").exists()


def test_held_file_rejects_path_swap(tmp_path: Path) -> None:
    source = tmp_path / "artifact.whl"
    source.write_bytes(b"trusted")
    source.chmod(0o600)
    held = runtime.open_held_regular(
        source,
        expected_uid=os.geteuid(),
        expected_gid=_gid(source),
        allowed_modes=frozenset({0o600}),
        maximum_bytes=1024,
    )
    try:
        source.rename(tmp_path / "original.whl")
        source.write_bytes(b"replacement")
        source.chmod(0o600)

        with pytest.raises(
            runtime.ProductionReleaseBuilderError,
            match="path_binding_changed",
        ):
            held.assert_stable()
    finally:
        held.close()


def test_held_file_rejects_one_byte_mutation(tmp_path: Path) -> None:
    source = tmp_path / "artifact.whl"
    source.write_bytes(b"trusted")
    source.chmod(0o600)
    held = runtime.open_held_regular(
        source,
        expected_uid=os.geteuid(),
        expected_gid=_gid(source),
        allowed_modes=frozenset({0o600}),
        maximum_bytes=1024,
    )
    try:
        source.write_bytes(b"trusXed")
        with pytest.raises(
            runtime.ProductionReleaseBuilderError,
            match="file_changed",
        ):
            held.assert_stable()
    finally:
        held.close()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "mode"])
def test_held_file_rejects_unsafe_inode_shapes(
    tmp_path: Path,
    kind: str,
) -> None:
    trusted = tmp_path / "trusted"
    trusted.write_bytes(b"payload")
    trusted.chmod(0o600)
    candidate = tmp_path / "candidate"
    if kind == "symlink":
        candidate.symlink_to(trusted)
    elif kind == "hardlink":
        os.link(trusted, candidate)
    elif kind == "fifo":
        os.mkfifo(candidate, 0o600)
    else:
        candidate.write_bytes(b"payload")
        candidate.chmod(0o666)

    with pytest.raises(runtime.ProductionReleaseBuilderError):
        runtime.open_held_regular(
            candidate,
            expected_uid=os.geteuid(),
            expected_gid=_gid(candidate),
            allowed_modes=frozenset({0o600}),
            maximum_bytes=1024,
        )


def test_wheel_is_copied_from_held_inode_and_sealed(tmp_path: Path) -> None:
    source = tmp_path / "hermes_agent-1-py3-none-any.whl"
    source.write_bytes(b"wheel-bytes")
    source.chmod(0o600)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)

    result = runtime.retain_verified_wheel(
        source,
        artifacts,
        expected_sha256=_sha256(source),
        builder_uid=os.geteuid(),
        builder_gid=_gid(source),
        destination_uid=os.geteuid(),
        destination_gid=_gid(artifacts),
    )

    retained = artifacts / source.name
    assert result["sha256"] == _sha256(retained)
    assert retained.read_bytes() == b"wheel-bytes"
    assert stat.S_IMODE(retained.stat().st_mode) == 0o444
    assert retained.stat().st_ino != source.stat().st_ino


def test_wheel_copy_rejects_path_swap_after_open(tmp_path: Path) -> None:
    source = tmp_path / "hermes_agent-1-py3-none-any.whl"
    source.write_bytes(b"trusted")
    source.chmod(0o600)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)
    held = runtime.open_held_regular(
        source,
        expected_uid=os.geteuid(),
        expected_gid=_gid(source),
        allowed_modes=frozenset({0o600}),
        maximum_bytes=1024,
    )
    directory = os.open(artifacts, os.O_RDONLY | os.O_DIRECTORY)
    try:
        source.rename(tmp_path / "original.whl")
        source.write_bytes(b"replacement")
        source.chmod(0o600)
        with pytest.raises(
            runtime.ProductionReleaseBuilderError,
            match="path_binding_changed",
        ):
            runtime._copy_held_to_directory(
                held,
                directory,
                "retained.whl",
                mode=0o444,
                destination_uid=os.geteuid(),
                destination_gid=_gid(artifacts),
            )
    finally:
        os.close(directory)
        held.close()
    assert not (artifacts / "retained.whl").exists()


def test_wheel_rejects_wrong_digest_and_hardlink(tmp_path: Path) -> None:
    source = tmp_path / "hermes_agent-1-py3-none-any.whl"
    source.write_bytes(b"wheel")
    source.chmod(0o600)
    alias = tmp_path / "hermes_agent-2-py3-none-any.whl"
    os.link(source, alias)
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir(mode=0o700)

    with pytest.raises(runtime.ProductionReleaseBuilderError):
        runtime.retain_verified_wheel(
            alias,
            artifacts,
            expected_sha256=hashlib.sha256(b"wheem").hexdigest(),
            builder_uid=os.geteuid(),
            builder_gid=_gid(alias),
            destination_uid=os.geteuid(),
            destination_gid=_gid(artifacts),
        )


def test_process_free_evidence_accepts_removed_and_recursively_empty_cgroup(
    tmp_path: Path,
) -> None:
    removed = _process_free_evidence(
        tmp_path / "removed",
        observation_only=True,
    )
    empty = _process_free_evidence(
        tmp_path / "empty",
        create_cgroup=True,
        observation_only=True,
    )

    assert removed["cgroup_status"] == "removed"
    assert empty["cgroup_status"] == "recursively-empty"
    assert empty["builder_uid_pids_before"] == []
    assert (
        runtime.validate_process_free_evidence_record(
            empty,
            builder_uid=BUILDER_UID,
            builder_gid=BUILDER_GID,
        )
        == empty
    )


def test_process_free_evidence_rejects_cgroup_pid(tmp_path: Path) -> None:
    root = tmp_path / "state"
    fragment = root / "unit.service"
    fragment.parent.mkdir()
    fragment.write_bytes(b"[Service]\n")
    fragment.chmod(0o444)
    wrapper = root / "builder-wrapper"
    wrapper.write_bytes(b"#!/bin/sh\n")
    wrapper.chmod(0o555)
    cgroup_root = root / "cgroup"
    proc_root = root / "proc"
    proc_root.mkdir()
    control_group = "/system.slice/muncho-release-builder@tx-1.service"
    service = cgroup_root / control_group.removeprefix("/")
    service.mkdir(parents=True)
    (service / "cgroup.procs").write_bytes(b"123\n")

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="cgroup_not_empty",
    ):
        runtime.validate_process_free_evidence(
            _systemd_properties(fragment, cgroup=control_group),
            expected_unit="muncho-release-builder@tx-1.service",
            expected_fragment=fragment,
            expected_fragment_sha256=_sha256(fragment),
            expected_wrapper=wrapper,
            expected_wrapper_sha256=_sha256(wrapper),
            expected_control_group=control_group,
            builder_uid=BUILDER_UID,
            builder_gid=BUILDER_GID,
            cgroup_root=cgroup_root,
            proc_root=proc_root,
            authority_uid=os.geteuid(),
            authority_gid=_gid(fragment),
            xattr_reader=_empty_xattrs,
        )


def test_process_free_evidence_rejects_any_builder_uid_process(
    tmp_path: Path,
) -> None:
    fragment = tmp_path / "unit.service"
    fragment.write_bytes(b"[Service]\n")
    fragment.chmod(0o444)
    wrapper = tmp_path / "builder-wrapper"
    wrapper.write_bytes(b"#!/bin/sh\n")
    wrapper.chmod(0o555)
    cgroup_root = tmp_path / "cgroup"
    proc_root = tmp_path / "proc"
    cgroup_root.mkdir()
    (cgroup_root / "system.slice").mkdir()
    proc_root.mkdir()
    (proc_root / "42").mkdir()
    control_group = "/system.slice/muncho-release-builder@tx-1.service"

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="uid_processes_present",
    ):
        runtime.validate_process_free_evidence(
            _systemd_properties(fragment, cgroup=control_group),
            expected_unit="muncho-release-builder@tx-1.service",
            expected_fragment=fragment,
            expected_fragment_sha256=_sha256(fragment),
            expected_wrapper=wrapper,
            expected_wrapper_sha256=_sha256(wrapper),
            expected_control_group=control_group,
            builder_uid=BUILDER_UID,
            builder_gid=BUILDER_GID,
            cgroup_root=cgroup_root,
            proc_root=proc_root,
            authority_uid=os.geteuid(),
            authority_gid=_gid(fragment),
            process_uid=lambda path, _state: BUILDER_UID if path.name == "42" else 0,
            xattr_reader=_empty_xattrs,
        )


def test_default_proc_reader_detects_builder_uid_from_status_not_dir_owner(
    tmp_path: Path,
) -> None:
    proc_root = tmp_path / "proc"
    process = proc_root / "42"
    process.mkdir(parents=True)
    (process / "status").write_bytes(
        b"Name:\tbuilder\nUid:\t29104\t29104\t29104\t29104\n"
    )

    assert runtime._builder_processes(
        proc_root,
        builder_uid=BUILDER_UID,
    ) == (42,)
    assert process.stat().st_uid != BUILDER_UID


@pytest.mark.parametrize("target_name", ["fragment", "wrapper"])
def test_process_evidence_rejects_metadata_on_fixed_executed_assets(
    tmp_path: Path,
    target_name: str,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    fragment = evidence_root / "muncho-release-builder@.service"
    fragment.write_bytes(b"[Service]\nType=oneshot\n")
    fragment.chmod(0o444)
    wrapper = evidence_root / "muncho-release-builder-phase"
    wrapper.write_bytes(b"#!/bin/sh\nexit 0\n")
    wrapper.chmod(0o555)
    target = fragment if target_name == "fragment" else wrapper
    target_identity = (target.stat().st_dev, target.stat().st_ino)
    cgroup_root = evidence_root / "cgroup"
    proc_root = evidence_root / "proc"
    (cgroup_root / "system.slice").mkdir(parents=True)
    proc_root.mkdir()
    control_group = "/system.slice/muncho-release-builder@tx-1.service"

    def xattrs(descriptor: int) -> tuple[str, ...]:
        state = os.fstat(descriptor)
        if (state.st_dev, state.st_ino) == target_identity:
            return ("security.capability",)
        return ()

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="xattrs_present",
    ):
        runtime.validate_process_free_evidence(
            _systemd_properties(fragment, cgroup=control_group),
            expected_unit="muncho-release-builder@tx-1.service",
            expected_fragment=fragment,
            expected_fragment_sha256=_sha256(fragment),
            expected_wrapper=wrapper,
            expected_wrapper_sha256=_sha256(wrapper),
            expected_control_group=control_group,
            builder_uid=BUILDER_UID,
            builder_gid=BUILDER_GID,
            cgroup_root=cgroup_root,
            proc_root=proc_root,
            authority_uid=os.geteuid(),
            authority_gid=_gid(fragment),
            xattr_reader=xattrs,
        )


def test_process_evidence_rejects_systemd_drop_in_override(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    fragment = evidence_root / "unit.service"
    fragment.write_bytes(b"[Service]\n")
    fragment.chmod(0o444)
    wrapper = evidence_root / "wrapper"
    wrapper.write_bytes(b"#!/bin/sh\n")
    wrapper.chmod(0o555)
    cgroup_root = evidence_root / "cgroup"
    proc_root = evidence_root / "proc"
    (cgroup_root / "system.slice").mkdir(parents=True)
    proc_root.mkdir()
    control_group = "/system.slice/muncho-release-builder@tx-1.service"
    properties = {
        **_systemd_properties(fragment, cgroup=control_group),
        "DropInPaths": "/etc/systemd/system/override.conf",
    }

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="systemd_evidence_invalid",
    ):
        runtime.validate_process_free_evidence(
            properties,
            expected_unit="muncho-release-builder@tx-1.service",
            expected_fragment=fragment,
            expected_fragment_sha256=_sha256(fragment),
            expected_wrapper=wrapper,
            expected_wrapper_sha256=_sha256(wrapper),
            expected_control_group=control_group,
            builder_uid=BUILDER_UID,
            builder_gid=BUILDER_GID,
            cgroup_root=cgroup_root,
            proc_root=proc_root,
            authority_uid=os.geteuid(),
            authority_gid=_gid(fragment),
            xattr_reader=_empty_xattrs,
        )


def test_process_evidence_set_binds_both_canonical_observations(
    tmp_path: Path,
) -> None:
    observation = _process_free_evidence(
        tmp_path / "evidence",
        observation_only=True,
    )
    evidence = runtime.build_process_free_evidence_set(
        observation,
        observation,
        builder_uid=BUILDER_UID,
        builder_gid=BUILDER_GID,
    )

    assert runtime.validate_process_free_evidence_set_record(
        evidence,
        builder_uid=BUILDER_UID,
        builder_gid=BUILDER_GID,
    ) == evidence
    tampered = {
        **evidence,
        "final": {
            **evidence["final"],
            "invocation_id": "2" * 32,
        },
    }
    tampered["final"] = {
        **tampered["final"],
        "evidence_sha256": runtime._sha256_bytes(
            runtime._canonical({
                name: item
                for name, item in tampered["final"].items()
                if name != "evidence_sha256"
            })
        ),
    }
    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="process_evidence_changed",
    ):
        runtime.build_process_free_evidence_set(
            evidence["initial"],
            tampered["final"],
            builder_uid=BUILDER_UID,
            builder_gid=BUILDER_GID,
        )


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "mode"])
def test_publication_rejects_unsafe_release_entry(
    tmp_path: Path,
    kind: str,
) -> None:
    case = tmp_path / kind
    case.mkdir()
    release = case / "release"
    release.mkdir(mode=0o700)
    trusted = case / "trusted"
    trusted.write_bytes(b"payload")
    trusted.chmod(0o600)
    candidate = release / "candidate"
    if kind == "symlink":
        candidate.symlink_to(trusted)
    elif kind == "hardlink":
        os.link(trusted, candidate)
    elif kind == "fifo":
        os.mkfifo(candidate, 0o600)
    else:
        candidate.write_bytes(b"payload")
        candidate.chmod(0o666)
    evidence = _process_free_evidence(case / "evidence")

    with pytest.raises(runtime.ProductionReleaseBuilderError):
        runtime._publish_release_filesystem(
            release,
            revision=REVISION,
            identities=_identities(),
            process_free_evidence=evidence,
            staging_uid=os.geteuid(),
            staging_gid=_gid(release),
            publication_uid=os.geteuid(),
            publication_gid=_gid(case),
            _xattr_reader=_empty_xattrs,
        )
    assert not (release / runtime.RECEIPT_NAME).exists()


def test_publication_writes_terminal_receipt_last_and_verifies_tree(
    tmp_path: Path,
) -> None:
    release = tmp_path / "release"
    (release / "pkg").mkdir(parents=True, mode=0o700)
    (release / "pkg/data.txt").write_bytes(b"payload")
    (release / "pkg/data.txt").chmod(0o600)
    executable = release / "run"
    executable.write_bytes(b"#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    evidence = _process_free_evidence(tmp_path / "evidence")
    checkpoints: list[str] = []

    def observe(name: str) -> None:
        checkpoints.append(name)
        if name == "manifest_written":
            assert (release / runtime.MANIFEST_NAME).is_file()
            assert not (release / runtime.RECEIPT_NAME).exists()
        elif name == "terminal_receipt_written":
            assert (release / runtime.MANIFEST_NAME).is_file()
            assert (release / runtime.RECEIPT_NAME).is_file()

    receipt = runtime._publish_release_filesystem(
        release,
        revision=REVISION,
        identities=_identities(),
        process_free_evidence=evidence,
        staging_uid=os.geteuid(),
        staging_gid=_gid(release),
        publication_uid=os.geteuid(),
        publication_gid=_gid(tmp_path),
        checkpoint=observe,
        _xattr_reader=_empty_xattrs,
    )

    assert checkpoints == ["manifest_written", "terminal_receipt_written"]
    assert receipt["terminal"] is True
    assert stat.S_IMODE(release.stat().st_mode) == 0o555
    assert stat.S_IMODE((release / "pkg").stat().st_mode) == 0o555
    assert stat.S_IMODE((release / "pkg/data.txt").stat().st_mode) == 0o444
    assert stat.S_IMODE(executable.stat().st_mode) == 0o555
    assert (
        runtime._verify_published_release_filesystem(
            release,
            revision=REVISION,
            expected_uid=os.geteuid(),
            expected_gid=_gid(release),
            require_logical_owner=False,
            _xattr_reader=_empty_xattrs,
        )
        == receipt
    )


def test_published_release_detects_one_byte_mutation(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    payload = release / "payload"
    payload.write_bytes(b"trusted")
    payload.chmod(0o600)
    evidence = _process_free_evidence(tmp_path / "evidence")
    runtime._publish_release_filesystem(
        release,
        revision=REVISION,
        identities=_identities(),
        process_free_evidence=evidence,
        staging_uid=os.geteuid(),
        staging_gid=_gid(release),
        publication_uid=os.geteuid(),
        publication_gid=_gid(tmp_path),
        _xattr_reader=_empty_xattrs,
    )
    release.chmod(0o755)
    payload.chmod(0o644)
    payload.write_bytes(b"trusXed")
    payload.chmod(0o444)
    release.chmod(0o555)

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="published_tree_changed",
    ):
        runtime._verify_published_release_filesystem(
            release,
            revision=REVISION,
            expected_uid=os.geteuid(),
            expected_gid=_gid(release),
            require_logical_owner=False,
            _xattr_reader=_empty_xattrs,
        )


@pytest.mark.parametrize(
    ("target_name", "attribute"),
    [
        (".", "system.posix_acl_access"),
        ("nested", "system.posix_acl_default"),
        ("nested/payload", "security.capability"),
    ],
)
def test_publication_rejects_any_extended_attribute(
    tmp_path: Path,
    target_name: str,
    attribute: str,
) -> None:
    release = tmp_path / "release"
    nested = release / "nested"
    nested.mkdir(parents=True, mode=0o700)
    payload = nested / "payload"
    payload.write_bytes(b"trusted")
    payload.chmod(0o600)
    target = release if target_name == "." else release / target_name
    target_inode = target.stat().st_ino
    evidence = _process_free_evidence(tmp_path / "evidence")

    def xattrs(descriptor: int) -> tuple[str, ...]:
        if os.fstat(descriptor).st_ino == target_inode:
            return (attribute,)
        return ()

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="xattrs_present",
    ):
        runtime._publish_release_filesystem(
            release,
            revision=REVISION,
            identities=_identities(),
            process_free_evidence=evidence,
            staging_uid=os.geteuid(),
            staging_gid=_gid(release),
            publication_uid=os.geteuid(),
            publication_gid=_gid(tmp_path),
            _xattr_reader=xattrs,
        )
    assert not (release / runtime.RECEIPT_NAME).exists()


def test_publication_rejects_real_xattr_where_supported(tmp_path: Path) -> None:
    if not hasattr(os, "setxattr") or not hasattr(os, "listxattr"):
        pytest.skip("host Python does not expose descriptor xattr inspection")
    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    payload = release / "payload"
    payload.write_bytes(b"trusted")
    payload.chmod(0o600)
    try:
        os.setxattr(payload, "user.muncho-test", b"present")
    except OSError:
        pytest.skip("test filesystem does not support user xattrs")
    evidence = _process_free_evidence(tmp_path / "evidence")

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="xattrs_present",
    ):
        runtime._publish_release_filesystem(
            release,
            revision=REVISION,
            identities=_identities(),
            process_free_evidence=evidence,
            staging_uid=os.geteuid(),
            staging_gid=_gid(release),
            publication_uid=os.geteuid(),
            publication_gid=_gid(tmp_path),
        )
    assert not (release / runtime.RECEIPT_NAME).exists()


def test_publication_fails_closed_without_xattr_inspection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = tmp_path / "release"
    release.mkdir(mode=0o700)
    payload = release / "payload"
    payload.write_bytes(b"trusted")
    payload.chmod(0o600)
    evidence = _process_free_evidence(tmp_path / "evidence")
    monkeypatch.delattr(os, "listxattr", raising=False)

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="xattr_inspection_unavailable",
    ):
        runtime._publish_release_filesystem(
            release,
            revision=REVISION,
            identities=_identities(),
            process_free_evidence=evidence,
            staging_uid=os.geteuid(),
            staging_gid=_gid(release),
            publication_uid=os.geteuid(),
            publication_gid=_gid(tmp_path),
        )
    assert not (release / runtime.RECEIPT_NAME).exists()


def test_identity_boundary_requires_distinct_real_root_builder_and_runtimes() -> None:
    assert runtime.validate_release_identities(_identities()) == _identities()

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="identity_contract_invalid",
    ):
        runtime.validate_release_identities(
            runtime.ReleaseIdentities(
                builder_uid=BUILDER_UID + 1,
                builder_gid=BUILDER_GID,
                reserved_runtime_uids=(31001,),
                reserved_runtime_gids=(32001,),
            )
        )

    with pytest.raises(
        runtime.ProductionReleaseBuilderError,
        match="identity_contract_invalid",
    ):
        runtime.validate_release_identities(
            runtime.ReleaseIdentities(
                builder_uid=BUILDER_UID,
                builder_gid=BUILDER_GID,
                reserved_runtime_uids=(BUILDER_UID,),
                reserved_runtime_gids=(32001,),
            )
        )


def test_arbitrary_root_publisher_is_not_public() -> None:
    assert not hasattr(runtime, "publish_release_as_root")
    assert "publish_release_as_root" not in runtime.__all__
