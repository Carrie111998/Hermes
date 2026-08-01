from __future__ import annotations

import fcntl
import hashlib
import inspect
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Mapping

import pytest

from scripts.canary import production_release_builder_phase as phase
from scripts.canary import production_release_builder_runtime as builder
from scripts.canary import production_release_candidate_promoter as promoter
from tests.scripts.canary import test_production_release_builder_phase as build_test


REVISION = build_test.REVISION
ROOT = Path(__file__).resolve().parents[3]

EXPECTED_BUILDER_UNIT = b"""[Unit]
Description=Muncho pinned release builder boundary (%i)
Documentation=man:systemd.exec(5)

[Service]
Type=oneshot
User=muncho-release-builder
Group=muncho-release-builder
SupplementaryGroups=
ExecStart=/usr/libexec/muncho-release-builder-phase --request /var/lib/muncho-release-updates/%i/input/request.json
WorkingDirectory=/var/lib/muncho-release-updates/%i/output
Environment=HOME=/nonexistent
Environment=PATH=/usr/bin:/bin
Environment=LANG=C.UTF-8
Environment=TZ=UTC
Environment=PYTHONNOUSERSITE=1
Environment=PYTHONDONTWRITEBYTECODE=1
UnsetEnvironment=PYTHONPATH PYTHONHOME PYTHONINSPECT PYTHONSTARTUP PYTHONWARNINGS PYTHONBREAKPOINT PYTHONUSERBASE VIRTUAL_ENV
UnsetEnvironment=PIP_CONFIG_FILE PIP_INDEX_URL PIP_EXTRA_INDEX_URL UV_CONFIG_FILE UV_INDEX_URL UV_EXTRA_INDEX_URL
UnsetEnvironment=LD_PRELOAD LD_LIBRARY_PATH DYLD_INSERT_LIBRARIES DYLD_LIBRARY_PATH
UnsetEnvironment=SSLKEYLOGFILE SSL_CERT_FILE SSL_CERT_DIR REQUESTS_CA_BUNDLE CURL_CA_BUNDLE OPENSSL_CONF OPENSSL_MODULES
UnsetEnvironment=HTTP_PROXY HTTPS_PROXY ALL_PROXY NO_PROXY http_proxy https_proxy all_proxy no_proxy
UnsetEnvironment=OPENAI_API_KEY ANTHROPIC_API_KEY DISCORD_BOT_TOKEN GH_TOKEN GITHUB_TOKEN GOOGLE_APPLICATION_CREDENTIALS
UMask=0077
TimeoutStartSec=3600s
TimeoutStopSec=30s
KillMode=control-group
SendSIGKILL=yes
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
PrivateDevices=yes
PrivateNetwork=yes
PrivateTmp=yes
ProtectClock=yes
ProtectControlGroups=yes
ProtectHome=yes
ProtectHostname=yes
ProtectKernelLogs=yes
ProtectKernelModules=yes
ProtectKernelTunables=yes
ProtectProc=invisible
ProtectSystem=strict
ProcSubset=pid
RemoveIPC=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RestrictAddressFamilies=AF_UNIX
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@mount @privileged @resources @reboot @swap
IPAddressDeny=any
ReadOnlyPaths=/usr/lib/muncho-release-updater
ReadOnlyPaths=/run/lock/muncho-release-builder-promotion.lock
ReadOnlyPaths=/var/lib/muncho-release-updates/%i/input
ReadWritePaths=/var/lib/muncho-release-updates/%i/output
InaccessiblePaths=-/opt/adventico-ai-platform/hermes-home/.env
InaccessiblePaths=-/opt/adventico-ai-platform/hermes-home/auth.json
InaccessiblePaths=-/var/lib/hermes-gateway/.hermes/auth.json
InaccessiblePaths=-/etc/muncho/discord-connector-credentials
InaccessiblePaths=-/etc/muncho/discord-edge-credentials
InaccessiblePaths=-/etc/muncho/mac-ops-edge-credentials
InaccessiblePaths=-/run/credentials
"""
EXPECTED_BUILDER_WRAPPER = b"""#!/bin/sh
set -eu

exec /usr/bin/flock --exclusive --no-fork \\
  /run/lock/muncho-release-builder-promotion.lock \\
  /usr/bin/python3 -B -I -c \\
  'import runpy,sys; sys.path.insert(0,"/usr/lib/muncho-release-updater"); sys.argv=sys.argv[1:]; runpy.run_module("scripts.canary.production_release_builder_phase",run_name="__main__")' \\
  muncho-release-builder-phase "$@"
"""
EXPECTED_BUILDER_TMPFILES = (
    b"d /var/lib/muncho-release-updates 0755 root root -\n"
    b"f /run/lock/muncho-release-builder-promotion.lock "
    b"0440 root muncho-release-builder -\n"
)


@dataclass
class Fixture:
    build: build_test.Fixture
    terminal: Mapping[str, Any]
    roots: promoter.PromoterRoots
    identities: builder.ReleaseIdentities
    systemd_properties: Mapping[str, Any]
    fragment_sha256: str
    wrapper_sha256: str
    source_builder_uid: int
    source_builder_gid: int
    interlock_gid: int
    staging_gid: int

    @property
    def final(self) -> Path:
        return (
            self.roots.release_parent
            / f"hermes-agent-{REVISION[:12]}"
        )

    @property
    def hidden(self) -> Path:
        return (
            self.roots.release_parent
            / f".muncho-release-staging-{REVISION}"
        )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    rotation_stager: bool = False,
) -> Fixture:
    build = build_test._fixture(tmp_path, monkeypatch)
    if rotation_stager:
        build_test._rewrite_request(
            build,
            schema=phase.UNIT_INPUT_ROTATION_STAGER_REQUEST_SCHEMA,
            purpose=phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE,
            entrypoint_relative_path=(
                phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
            ),
        )
    terminal = phase._run_builder_phase_for_test(
        build.request_path,
        production=False,
        job_root=build.job_root,
        identity_resolver=build_test.Resolver(),
        command_runner=build.runner,
        effective_uid=phase.BUILDER_UID,
        effective_gid=phase.BUILDER_GID,
        test_authority_uid=os.lstat(build.input_root).st_uid,
        test_authority_gid=os.lstat(build.input_root).st_gid,
        test_physical_builder_uid=os.lstat(build.output_root).st_uid,
        test_physical_builder_gid=os.lstat(build.output_root).st_gid,
        test_xattr_reader=lambda _descriptor: (),
    )
    release_parent = tmp_path / "releases"
    release_parent.mkdir(mode=0o755)
    os.chown(release_parent, -1, os.getegid())
    fragment = tmp_path / "muncho-release-builder@.service"
    fragment.write_bytes(b"[Service]\nExecStart=/fixed/builder\n")
    fragment.chmod(0o444)
    wrapper = tmp_path / "muncho-release-builder-phase"
    wrapper.write_bytes(b"#!/bin/sh\nexec /fixed/builder \"$@\"\n")
    wrapper.chmod(0o555)
    promotion_interlock = tmp_path / "promotion-interlock.lock"
    promotion_interlock.write_bytes(b"")
    promotion_interlock.chmod(0o440)
    cgroup_root = tmp_path / "cgroup"
    (cgroup_root / "system.slice").mkdir(parents=True)
    proc_root = tmp_path / "proc"
    proc_root.mkdir()
    roots = promoter.PromoterRoots(
        job_root=build.job_root,
        release_parent=release_parent,
        builder_unit_fragment=fragment,
        builder_wrapper=wrapper,
        promotion_interlock=promotion_interlock,
        cgroup_root=cgroup_root,
        proc_root=proc_root,
    )
    unit = f"muncho-release-builder@{REVISION}.service"
    systemd_properties = {
        "Id": unit,
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
        "ControlGroup": f"/system.slice/{unit}",
    }
    identities = builder.ReleaseIdentities(
        builder_uid=phase.BUILDER_UID,
        builder_gid=phase.BUILDER_GID,
        reserved_runtime_uids=(31001,),
        reserved_runtime_gids=(32001,),
    )
    release_gid = os.lstat(release_parent).st_gid
    candidate_state = os.lstat(
        build.output_root / phase.CANDIDATE_NAME
    )
    staging_gid = next(
        (
            group
            for group in os.getgroups()
            if group != release_gid
        ),
        release_gid,
    )
    return Fixture(
        build=build,
        terminal=terminal,
        roots=roots,
        identities=identities,
        systemd_properties=systemd_properties,
        fragment_sha256=_sha256(fragment),
        wrapper_sha256=_sha256(wrapper),
        source_builder_uid=candidate_state.st_uid,
        source_builder_gid=candidate_state.st_gid,
        interlock_gid=os.lstat(promotion_interlock).st_gid,
        staging_gid=staging_gid,
    )


def _promote(
    fixture: Fixture,
    *,
    binding: promoter._PromotionBinding = (
        promoter._RELEASE_UPDATER_PROMOTION_BINDING
    ),
    checkpoint: Callable[[str], None] | None = None,
    rename_no_replace: Callable[[Path, Path], None] | None = None,
    xattr_reader=None,
    process_uid=None,
    systemd_reader: Callable[[str], Mapping[str, Any]] | None = None,
) -> Mapping[str, Any]:
    return promoter._promote_candidate_for_test(
        revision=REVISION,
        expected_builder_terminal_receipt_sha256=str(
            fixture.terminal["receipt_sha256"]
        ),
        roots=fixture.roots,
        binding=binding,
        production=False,
        checkpoint=checkpoint,
        rename_no_replace=rename_no_replace,
        test_authority_uid=os.lstat(
            fixture.build.input_root
        ).st_uid,
        test_authority_gid=os.lstat(
            fixture.build.input_root
        ).st_gid,
        test_interlock_gid=fixture.interlock_gid,
        test_source_builder_uid=fixture.source_builder_uid,
        test_source_builder_gid=fixture.source_builder_gid,
        test_staging_uid=os.geteuid(),
        test_staging_gid=fixture.staging_gid,
        test_publication_uid=os.lstat(
            fixture.roots.release_parent
        ).st_uid,
        test_publication_gid=os.lstat(
            fixture.roots.release_parent
        ).st_gid,
        test_xattr_reader=(
            (lambda _descriptor: ())
            if xattr_reader is None
            else xattr_reader
        ),
        test_process_uid=(
            (lambda _path, _state: 0)
            if process_uid is None
            else process_uid
        ),
        test_identities=fixture.identities,
        test_systemd_reader=(
            (lambda _unit: dict(fixture.systemd_properties))
            if systemd_reader is None
            else systemd_reader
        ),
        test_fragment_sha256=fixture.fragment_sha256,
        test_wrapper_sha256=fixture.wrapper_sha256,
    )


def test_input_descriptor_capacity_is_raised_for_large_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    limits = [1024, 1_048_576]
    updates: list[tuple[int, tuple[int, int]]] = []

    def getrlimit(kind: int) -> tuple[int, int]:
        assert kind == promoter.resource.RLIMIT_NOFILE
        return limits[0], limits[1]

    def setrlimit(kind: int, value: tuple[int, int]) -> None:
        assert kind == promoter.resource.RLIMIT_NOFILE
        updates.append((kind, value))
        limits[:] = value

    monkeypatch.setattr(promoter.resource, "getrlimit", getrlimit)
    monkeypatch.setattr(promoter.resource, "setrlimit", setrlimit)

    promoter._reserve_input_descriptor_capacity(
        source_blob_count=8_676,
        runtime_wheel_count=4,
    )

    assert updates == [
        (
            promoter.resource.RLIMIT_NOFILE,
            (8_744, 1_048_576),
        )
    ]


def test_input_descriptor_capacity_fails_before_opening_when_hard_limit_is_low(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        promoter.resource,
        "getrlimit",
        lambda _kind: (1024, 4096),
    )
    called = False

    def setrlimit(_kind: int, _value: tuple[int, int]) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr(promoter.resource, "setrlimit", setrlimit)

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="descriptor_capacity_insufficient",
    ):
        promoter._reserve_input_descriptor_capacity(
            source_blob_count=8_676,
            runtime_wheel_count=4,
        )

    assert called is False


def _rewrite_canonical(path: Path, value: Mapping[str, Any]) -> None:
    path.chmod(0o644)
    path.write_bytes(promoter.canonical_bytes(value) + b"\n")
    path.chmod(0o444)


def _self_hash(value: Mapping[str, Any], field: str) -> dict[str, Any]:
    unsigned = {key: item for key, item in value.items() if key != field}
    return {
        **unsigned,
        field: promoter.sha256_bytes(promoter.canonical_bytes(unsigned)),
    }


def test_cross_filesystem_safe_promotion_has_every_durable_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    checkpoints: list[str] = []
    rename_calls: list[tuple[Path, Path]] = []

    def same_filesystem_rename(source: Path, destination: Path) -> None:
        rename_calls.append((source, destination))
        assert source.parent == fixture.roots.release_parent
        assert destination.parent == fixture.roots.release_parent
        os.rename(source, destination)

    result = _promote(
        fixture,
        checkpoint=checkpoints.append,
        rename_no_replace=same_filesystem_rename,
    )

    assert promoter.validate_promotion_result(result) == result
    assert result["builder_terminal_receipt_sha256"] == fixture.terminal[
        "receipt_sha256"
    ]
    assert (
        result["candidate_seal_receipt_sha256"]
        != result["builder_terminal_receipt_sha256"]
    )
    assert rename_calls == [(fixture.hidden, fixture.final)]
    assert not fixture.hidden.exists()
    assert stat.S_IMODE(fixture.final.stat().st_mode) == 0o555
    assert (fixture.final / phase.TERMINAL_RECEIPT_NAME).is_file()
    assert (fixture.final / builder.MANIFEST_NAME).is_file()
    assert (fixture.final / builder.RECEIPT_NAME).is_file()
    assert checkpoints == [
        "builder_process_free_initial",
        "hidden_staging_created",
        "hidden_staging_fsynced",
        "renamed_final_no_replace",
        "root_publisher_modes_prepared",
        "promotion_interlock_acquired",
        "builder_process_free_final",
        "root_publication_manifest_written",
        "root_publication_terminal_receipt_written",
        "completed",
    ]

    assert _promote(fixture) == result


def test_public_promoter_has_no_authority_or_test_seams() -> None:
    assert tuple(inspect.signature(promoter.promote_candidate).parameters) == (
        "revision",
        "expected_builder_terminal_receipt_sha256",
    )
    assert tuple(
        inspect.signature(
            promoter.promote_rotation_stager_candidate
        ).parameters
    ) == (
        "revision",
        "expected_builder_terminal_receipt_sha256",
    )


def test_public_promoters_are_bound_to_disjoint_exact_receipt_purposes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[promoter._PromotionBinding] = []

    def capture(**kwargs: Any) -> Mapping[str, Any]:
        observed.append(kwargs["binding"])
        return {"binding": kwargs["binding"]}

    monkeypatch.setattr(promoter, "_promote_candidate_for_test", capture)

    updater = promoter.promote_candidate(
        revision=REVISION,
        expected_builder_terminal_receipt_sha256="1" * 64,
    )
    stager = promoter.promote_rotation_stager_candidate(
        revision=REVISION,
        expected_builder_terminal_receipt_sha256="2" * 64,
    )

    assert updater["binding"] is (
        promoter._RELEASE_UPDATER_PROMOTION_BINDING
    )
    assert stager["binding"] is (
        promoter._UNIT_INPUT_ROTATION_STAGER_PROMOTION_BINDING
    )
    assert observed == [
        promoter._RELEASE_UPDATER_PROMOTION_BINDING,
        promoter._UNIT_INPUT_ROTATION_STAGER_PROMOTION_BINDING,
    ]
    assert observed[0].request_purpose is None
    assert observed[0].terminal_receipt_purpose is None
    assert observed[1].request_purpose == (
        phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE
    )
    assert observed[1].terminal_receipt_purpose == (
        phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE
    )


def test_rotation_stager_is_published_only_through_stager_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch, rotation_stager=True)

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="candidate_promoter_request_purpose_invalid",
    ):
        _promote(fixture)
    assert not fixture.hidden.exists()
    assert not fixture.final.exists()

    result = _promote(
        fixture,
        binding=promoter._UNIT_INPUT_ROTATION_STAGER_PROMOTION_BINDING,
    )

    assert result["completed"] is True
    terminal = json.loads(
        (fixture.final / phase.TERMINAL_RECEIPT_NAME).read_text(
            encoding="ascii"
        )
    )
    assert terminal["schema"] == (
        phase.UNIT_INPUT_ROTATION_STAGER_TERMINAL_RECEIPT_SCHEMA
    )
    assert terminal["purpose"] == phase.UNIT_INPUT_ROTATION_STAGER_PURPOSE
    assert terminal["entrypoint_relative_path"] == (
        phase.UNIT_INPUT_ROTATION_STAGER_ENTRYPOINT_RELATIVE_PATH
    )
    assert fixture.final.is_dir()
    assert not fixture.final.is_symlink()


def test_updater_candidate_is_rejected_by_rotation_stager_binding(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="candidate_promoter_request_purpose_invalid",
    ):
        _promote(
            fixture,
            binding=(
                promoter._UNIT_INPUT_ROTATION_STAGER_PROMOTION_BINDING
            ),
        )

    assert not fixture.hidden.exists()
    assert not fixture.final.exists()


def test_production_builder_assets_and_embedded_digests_are_exact() -> None:
    unit = (
        ROOT
        / "ops/muncho/release-updater/muncho-release-builder@.service"
    ).read_bytes()
    wrapper = (
        ROOT
        / "ops/muncho/release-updater/muncho-release-builder-phase"
    ).read_bytes()
    tmpfiles = (
        ROOT
        / "ops/muncho/release-updater/muncho-release-builder.tmpfiles"
    ).read_bytes()

    assert unit == EXPECTED_BUILDER_UNIT
    assert wrapper == EXPECTED_BUILDER_WRAPPER
    assert tmpfiles == EXPECTED_BUILDER_TMPFILES
    assert hashlib.sha256(unit).hexdigest() == (
        promoter.PRODUCTION_BUILDER_UNIT_FRAGMENT_SHA256
    )
    assert hashlib.sha256(wrapper).hexdigest() == (
        promoter.PRODUCTION_BUILDER_WRAPPER_SHA256
    )


def test_systemd_collector_executes_only_the_exact_fixed_show(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unit = f"muncho-release-builder@{REVISION}.service"
    properties = {
        "Id": unit,
        "FragmentPath": str(promoter.PRODUCTION_BUILDER_UNIT_FRAGMENT),
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
        "ControlGroup": f"/system.slice/{unit}",
    }
    stdout = b"".join(
        f"{name}={properties[name]}\n".encode()
        for name in promoter._SYSTEMD_PROPERTY_NAMES
    )
    calls: list[tuple[tuple[str, ...], Mapping[str, Any]]] = []

    def run(argv, **kwargs):
        calls.append((tuple(argv), dict(kwargs)))
        return SimpleNamespace(returncode=0, stdout=stdout)

    monkeypatch.setattr(promoter.subprocess, "run", run)

    assert promoter._systemctl_show(unit) == properties
    assert calls == [
        (
            (
                "/usr/bin/systemctl",
                "show",
                "--no-pager",
                *(
                    f"--property={name}"
                    for name in promoter._SYSTEMD_PROPERTY_NAMES
                ),
                "--",
                unit,
            ),
            {
                "stdin": promoter.subprocess.DEVNULL,
                "stdout": promoter.subprocess.PIPE,
                "stderr": promoter.subprocess.DEVNULL,
                "cwd": "/",
                "env": {
                    "HOME": "/",
                    "LANG": "C",
                    "LC_ALL": "C",
                    "PATH": "/usr/bin:/bin",
                },
                "check": False,
                "timeout": 30,
            },
        )
    ]


def test_production_identities_reserve_exact_cutover_catalog_before_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(promoter.os, "geteuid", lambda: 0)

    def absent(_value: Any) -> Any:
        raise KeyError

    identities = promoter._derive_production_release_identities(
        user_lookup=absent,
        user_id_lookup=absent,
        group_lookup=absent,
        group_id_lookup=absent,
    )

    assert identities.reserved_runtime_uids == tuple(
        sorted(promoter._PRODUCTION_RUNTIME_UID_BY_NAME.values())
    )
    assert identities.reserved_runtime_gids == tuple(
        sorted(promoter._PRODUCTION_RUNTIME_GID_BY_NAME.values())
    )
    assert len(identities.reserved_runtime_uids) == 17
    assert len(identities.reserved_runtime_gids) == 28


def test_production_identity_reservation_rejects_uid_collision(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(promoter.os, "geteuid", lambda: 0)

    def absent(_value: Any) -> Any:
        raise KeyError

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="identity_contract_invalid",
    ):
        promoter._derive_production_release_identities(
            user_lookup=absent,
            user_id_lookup=lambda uid: SimpleNamespace(
                pw_name="collision",
                pw_uid=uid,
                pw_gid=uid,
            ),
            group_lookup=absent,
            group_id_lookup=absent,
        )


def test_rehashed_terminal_tamper_is_rejected_against_root_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    terminal_path = (
        fixture.build.output_root
        / phase.CANDIDATE_NAME
        / phase.TERMINAL_RECEIPT_NAME
    )
    terminal = json.loads(terminal_path.read_text(encoding="ascii"))
    terminal["uv_sha256"] = "9" * 64
    terminal = _self_hash(terminal, "receipt_sha256")
    _rewrite_canonical(terminal_path, terminal)
    fixture.terminal = terminal

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="candidate_binding_invalid",
    ):
        _promote(fixture)
    assert not fixture.hidden.exists()
    assert not fixture.final.exists()


def test_payload_byte_tamper_is_rejected_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    candidate = fixture.build.output_root / phase.CANDIDATE_NAME
    entrypoint = candidate / phase.ENTRYPOINT_RELATIVE_PATH
    candidate.chmod(0o755)
    entrypoint.parent.chmod(0o755)
    entrypoint.chmod(0o644)
    raw = bytearray(entrypoint.read_bytes())
    raw[-2] ^= 1
    entrypoint.write_bytes(raw)
    entrypoint.chmod(0o444)
    entrypoint.parent.chmod(0o555)
    candidate.chmod(0o555)

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="candidate_tree_invalid",
    ):
        _promote(fixture)
    assert not fixture.hidden.exists()


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo"])
def test_non_regular_or_hardlinked_terminal_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    candidate = fixture.build.output_root / phase.CANDIDATE_NAME
    terminal = candidate / phase.TERMINAL_RECEIPT_NAME
    external = tmp_path / "external-terminal"
    external.write_bytes(terminal.read_bytes())
    external.chmod(0o444)
    candidate.chmod(0o755)
    terminal.unlink()
    if kind == "symlink":
        terminal.symlink_to(external)
    elif kind == "hardlink":
        os.link(external, terminal)
    else:
        os.mkfifo(terminal, 0o444)
    candidate.chmod(0o555)

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
    ):
        _promote(fixture)
    assert not fixture.hidden.exists()


def test_acl_or_capability_xattr_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="xattrs_or_acl_present",
    ):
        _promote(
            fixture,
            xattr_reader=lambda _descriptor: [
                "system.posix_acl_access"
            ],
        )
    assert not fixture.hidden.exists()


@pytest.mark.parametrize(
    "target_name",
    [
        "fragment",
        "wrapper",
        "interlock",
        "input_root",
        "request",
        "blob_root",
        "wheel_root",
        "wheel",
        "python",
        "release_parent",
    ],
)
def test_every_root_input_and_directory_rejects_extended_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    targets = {
        "fragment": fixture.roots.builder_unit_fragment,
        "wrapper": fixture.roots.builder_wrapper,
        "interlock": fixture.roots.promotion_interlock,
        "input_root": fixture.build.input_root,
        "request": fixture.build.request_path,
        "blob_root": (
            fixture.build.input_root
            / phase.SOURCE_BLOB_DIRECTORY_NAME
        ),
        "wheel_root": (
            fixture.build.input_root
            / phase.RUNTIME_WHEEL_DIRECTORY_NAME
        ),
        "wheel": (
            fixture.build.input_root
            / phase.RUNTIME_WHEEL_DIRECTORY_NAME
            / build_test.WHEEL_NAME
        ),
        "python": fixture.build.python_path,
        "release_parent": fixture.roots.release_parent,
    }
    state = targets[target_name].stat()
    target_identity = (state.st_dev, state.st_ino)

    def xattrs(descriptor: int) -> list[str]:
        observed = os.fstat(descriptor)
        if (observed.st_dev, observed.st_ino) == target_identity:
            return ["security.capability"]
        return []

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
    ):
        _promote(fixture, xattr_reader=xattrs)
    assert not fixture.hidden.exists()


def test_drop_in_override_is_rejected_before_candidate_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    fixture.systemd_properties = {
        **fixture.systemd_properties,
        "DropInPaths": "/etc/systemd/system/override.conf",
    }

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="builder_not_process_free",
    ):
        _promote(fixture)
    assert not fixture.hidden.exists()


def test_active_builder_uid_is_rejected_before_candidate_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    process = fixture.roots.proc_root / "42"
    process.mkdir()

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="builder_not_process_free",
    ):
        _promote(
            fixture,
            process_uid=lambda path, _state: (
                phase.BUILDER_UID if path.name == "42" else 0
            ),
        )
    assert not fixture.hidden.exists()


def test_changed_systemd_invocation_between_observations_blocks_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    observations = 0

    def systemd_reader(_unit: str) -> Mapping[str, Any]:
        nonlocal observations
        observations += 1
        return {
            **fixture.systemd_properties,
            "InvocationID": str(observations) * 32,
        }

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="builder_evidence_changed",
    ):
        _promote(fixture, systemd_reader=systemd_reader)
    assert observations == 2
    assert not (fixture.final / builder.MANIFEST_NAME).exists()


def test_latched_completed_v3_builder_promotes_without_a_live_process(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    unit = f"muncho-release-builder-v3@{REVISION}.service"
    fixture.roots = promoter.PromoterRoots(
        job_root=fixture.roots.job_root,
        release_parent=fixture.roots.release_parent,
        builder_unit_fragment=fixture.roots.builder_unit_fragment,
        builder_wrapper=fixture.roots.builder_wrapper,
        promotion_interlock=fixture.roots.promotion_interlock,
        builder_unit_prefix="muncho-release-builder-v3@",
        cgroup_root=fixture.roots.cgroup_root,
        proc_root=fixture.roots.proc_root,
    )
    fixture.systemd_properties = {
        **fixture.systemd_properties,
        "Id": unit,
        "ActiveState": "active",
        "SubState": "exited",
        "ExecMainPID": "526717",
        "ExecMainCode": "1",
        "ControlGroup": f"/system.slice/{unit}",
    }

    result = _promote(fixture)

    assert result["completed"] is True
    assert fixture.final.is_dir()
    evidence = json.loads(
        (fixture.final / builder.RECEIPT_NAME).read_text(encoding="utf-8")
    )["process_free_evidence"]
    assert evidence["initial"]["systemd_state"]["active"] == "active"
    assert evidence["final"]["systemd_state"]["sub"] == "exited"
    assert evidence["initial"]["systemd_state"]["exec_main_pid"] == 526717
    assert evidence["final"]["systemd_state"]["exec_main_code"] == "1"


def test_root_input_change_after_staging_blocks_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    wheel = (
        fixture.build.input_root
        / phase.RUNTIME_WHEEL_DIRECTORY_NAME
        / build_test.WHEEL_NAME
    )

    def checkpoint(name: str) -> None:
        if name != "promotion_interlock_acquired":
            return
        wheel.chmod(0o644)
        wheel.write_bytes(b"late root input mutation")
        wheel.chmod(0o444)

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="root_input_changed",
    ):
        _promote(fixture, checkpoint=checkpoint)
    assert not (fixture.final / builder.MANIFEST_NAME).exists()


def test_interlock_is_held_through_final_observation_and_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    checked: list[str] = []

    def checkpoint(name: str) -> None:
        if name not in {
            "promotion_interlock_acquired",
            "builder_process_free_final",
            "root_publication_manifest_written",
            "root_publication_terminal_receipt_written",
        }:
            return
        descriptor = os.open(fixture.roots.promotion_interlock, os.O_RDONLY)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(
                    descriptor,
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
        finally:
            os.close(descriptor)
        checked.append(name)

    result = _promote(fixture, checkpoint=checkpoint)

    assert result["completed"] is True
    assert checked == [
        "promotion_interlock_acquired",
        "builder_process_free_final",
        "root_publication_manifest_written",
        "root_publication_terminal_receipt_written",
    ]


def test_interlock_is_group_readable_but_never_world_accessible(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    mode = stat.S_IMODE(
        fixture.roots.promotion_interlock.stat().st_mode
    )

    assert mode == 0o440
    assert mode & 0o007 == 0
    assert mode & 0o222 == 0


def test_interlock_enter_failure_releases_lock_and_descriptor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    interlock_path = fixture.roots.promotion_interlock
    held = promoter._promotion_interlock(
        interlock_path,
        authority_uid=os.lstat(interlock_path).st_uid,
        builder_gid=fixture.interlock_gid,
        xattr_reader=lambda _descriptor: (),
    )
    original_path = interlock_path.with_name(
        "promotion-interlock-original.lock"
    )
    interlock_path.rename(original_path)

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="interlock_changed",
    ):
        held.__enter__()

    assert held.held._closed is True
    descriptor = os.open(original_path, os.O_RDONLY)
    try:
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX | fcntl.LOCK_NB,
        )
    finally:
        os.close(descriptor)


@pytest.mark.parametrize("attack", ["world_readable", "wrong_group"])
def test_interlock_rejects_permission_or_ownership_widening(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    attack: str,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    interlock = fixture.roots.promotion_interlock
    if attack == "world_readable":
        interlock.chmod(0o444)
    else:
        wrong_gid = next(
            (
                group
                for group in os.getgroups()
                if group != fixture.interlock_gid
            ),
            None,
        )
        if wrong_gid is None:
            pytest.skip("no alternate group available for ownership test")
        os.chown(interlock, -1, wrong_gid)

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="interlock_invalid",
    ):
        _promote(fixture)
    assert not (fixture.final / builder.RECEIPT_NAME).exists()


def test_published_evidence_is_complete_and_bound_in_both_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    result = _promote(fixture)
    manifest = json.loads(
        (fixture.final / builder.MANIFEST_NAME).read_text(
            encoding="ascii"
        )
    )
    receipt = json.loads(
        (fixture.final / builder.RECEIPT_NAME).read_text(
            encoding="ascii"
        )
    )
    evidence = manifest["process_free_evidence"]

    assert evidence == receipt["process_free_evidence"]
    assert evidence["schema"] == builder.PROCESS_FREE_EVIDENCE_SET_SCHEMA
    assert set(evidence) == {
        "schema",
        "initial",
        "final",
        "secret_material_recorded",
        "secret_digest_recorded",
        "evidence_sha256",
    }
    assert evidence["initial"]["wrapper_path"] == str(
        fixture.roots.builder_wrapper
    )
    assert evidence["final"]["fragment_path"] == str(
        fixture.roots.builder_unit_fragment
    )
    assert evidence["initial"]["drop_in_paths"] == []
    assert evidence["final"]["drop_in_paths"] == []
    assert evidence["evidence_sha256"] == manifest[
        "process_free_evidence_sha256"
    ]
    assert manifest["process_free_evidence_sha256"] == receipt[
        "process_free_evidence_sha256"
    ]
    assert receipt["process_free_evidence_sha256"] == result[
        "process_free_evidence_sha256"
    ]


def test_idempotent_replay_needs_no_source_candidate_or_new_observation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    observations = 0

    def systemd_reader(_unit: str) -> Mapping[str, Any]:
        nonlocal observations
        observations += 1
        return dict(fixture.systemd_properties)

    first = _promote(fixture, systemd_reader=systemd_reader)
    source = fixture.build.output_root / phase.CANDIDATE_NAME
    removed = fixture.build.output_root / "removed-source-candidate"
    fixture.build.output_root.chmod(0o700)
    os.rename(source, removed)
    fixture.build.output_root.chmod(0o555)

    assert _promote(fixture, systemd_reader=systemd_reader) == first
    assert observations == 2


def test_root_wheel_input_tamper_is_rejected_before_candidate_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    wheel = (
        fixture.build.input_root
        / phase.RUNTIME_WHEEL_DIRECTORY_NAME
        / build_test.WHEEL_NAME
    )
    wheel.chmod(0o644)
    wheel.write_bytes(b"tampered root wheel")
    wheel.chmod(0o444)

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="root_input_invalid",
    ):
        _promote(fixture)
    assert not fixture.hidden.exists()


def test_partial_hidden_staging_requires_explicit_root_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    def crash(name: str) -> None:
        if name == "hidden_staging_created":
            raise promoter.ProductionReleaseCandidatePromoterError(
                "injected_crash"
            )

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="injected_crash",
    ):
        _promote(fixture, checkpoint=crash)
    assert fixture.hidden.exists()
    assert not fixture.final.exists()

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="cleanup_required",
    ):
        _promote(fixture)


def test_exact_fsynced_hidden_staging_resumes_without_recopied_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    def crash(name: str) -> None:
        if name == "hidden_staging_fsynced":
            raise promoter.ProductionReleaseCandidatePromoterError(
                "injected_crash"
            )

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="injected_crash",
    ):
        _promote(fixture, checkpoint=crash)
    assert fixture.hidden.exists()
    hidden_inode = fixture.hidden.stat().st_ino

    result = _promote(fixture)
    assert result["completed"] is True
    assert fixture.final.stat().st_ino == hidden_inode


def test_exact_renamed_staging_resumes_root_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    def crash(name: str) -> None:
        if name == "renamed_final_no_replace":
            raise promoter.ProductionReleaseCandidatePromoterError(
                "injected_crash"
            )

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="injected_crash",
    ):
        _promote(fixture, checkpoint=crash)
    assert fixture.final.exists()
    assert not (fixture.final / builder.MANIFEST_NAME).exists()

    result = _promote(fixture)
    assert result["completed"] is True
    assert (fixture.final / builder.RECEIPT_NAME).is_file()


def test_crash_after_publisher_mode_transition_requires_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    def crash(name: str) -> None:
        if name == "root_publisher_modes_prepared":
            raise promoter.ProductionReleaseCandidatePromoterError(
                "injected_crash"
            )

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="injected_crash",
    ):
        _promote(fixture, checkpoint=crash)
    assert fixture.final.exists()
    assert (
        stat.S_IMODE(
            (
                fixture.final / phase.INTERPRETER_RELATIVE_PATH
            ).stat().st_mode
        )
        == 0o755
    )
    assert not (fixture.final / builder.MANIFEST_NAME).exists()

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="cleanup_required",
    ):
        _promote(fixture)


def test_partial_root_publication_requires_explicit_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)

    def crash(name: str) -> None:
        if name == "root_publication_manifest_written":
            raise promoter.ProductionReleaseCandidatePromoterError(
                "injected_crash"
            )

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="injected_crash",
    ):
        _promote(fixture, checkpoint=crash)
    assert (fixture.final / builder.MANIFEST_NAME).is_file()
    assert not (fixture.final / builder.RECEIPT_NAME).exists()

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="cleanup_required",
    ):
        _promote(fixture)


def test_existing_destination_is_never_overwritten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _fixture(tmp_path, monkeypatch)
    fixture.final.mkdir(mode=0o755)
    sentinel = fixture.final / "sentinel"
    sentinel.write_bytes(b"do not overwrite")

    with pytest.raises(
        promoter.ProductionReleaseCandidatePromoterError,
        match="cleanup_required",
    ):
        _promote(fixture)
    assert sentinel.read_bytes() == b"do not overwrite"
