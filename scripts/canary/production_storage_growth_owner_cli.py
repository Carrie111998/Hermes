#!/usr/bin/env python3
"""Fixed owner CLI for exact production boot-storage growth.

All structured inputs arrive as one strict canonical stdin frame.  The CLI has
no path, account, project, zone, instance, disk, command, device, filesystem,
or target-size option.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Mapping, Sequence


_DIRECT_ENTRYPOINT_RELATIVE = Path(
    "source/scripts/canary/production_storage_growth_owner_cli.py"
)
_OWNER_SUPPORT_ROOT = re.compile(r"^owner-support-([0-9a-f]{40})$")
_OWNER_SUPPORT_MAX_ENTRIES = 50_000
_OWNER_SUPPORT_MAX_BYTES = 512 * 1024 * 1024


def _activate_direct_owner_support() -> str:
    if (
        sys.flags.isolated != 1
        or sys.flags.no_site != 1
        or not sys.dont_write_bytecode
    ):
        raise RuntimeError("production_storage_owner_direct_isolation_required")
    module_path = Path(__file__)
    invoked_path = Path(sys.argv[0])
    if (
        not module_path.is_absolute()
        or not invoked_path.is_absolute()
        or module_path != invoked_path
        or ".." in module_path.parts
    ):
        raise RuntimeError("production_storage_owner_direct_path_invalid")
    try:
        source_root = module_path.parents[2]
        support_root = module_path.parents[3]
    except IndexError:
        raise RuntimeError("production_storage_owner_direct_path_invalid") from None
    match = _OWNER_SUPPORT_ROOT.fullmatch(support_root.name)
    if (
        match is None
        or module_path != support_root / _DIRECT_ENTRYPOINT_RELATIVE
        or not support_root.is_absolute()
    ):
        raise RuntimeError("production_storage_owner_direct_path_invalid")
    site_root = support_root / "site-packages"
    try:
        if os.path.realpath(module_path, strict=True) != str(module_path):
            raise RuntimeError("production_storage_owner_direct_path_invalid")
    except OSError:
        raise RuntimeError("production_storage_owner_direct_path_invalid") from None
    pending = [support_root]
    entries = 0
    total_bytes = 0
    root_children: set[str] | None = None
    while pending:
        path = pending.pop()
        try:
            metadata = path.lstat()
        except OSError:
            raise RuntimeError("production_storage_owner_direct_tree_invalid") from None
        if metadata.st_uid != os.getuid():  # windows-footgun: ok
            raise RuntimeError("production_storage_owner_direct_tree_invalid")
        if stat.S_ISDIR(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) != 0o500:
                raise RuntimeError("production_storage_owner_direct_tree_invalid")
            try:
                children = tuple(path.iterdir())
            except OSError:
                raise RuntimeError(
                    "production_storage_owner_direct_tree_invalid"
                ) from None
            if path == support_root:
                root_children = {item.name for item in children}
            pending.extend(children)
        elif stat.S_ISREG(metadata.st_mode):
            if stat.S_IMODE(metadata.st_mode) != 0o400 or metadata.st_nlink != 1:
                raise RuntimeError("production_storage_owner_direct_tree_invalid")
            total_bytes += metadata.st_size
        else:
            raise RuntimeError("production_storage_owner_direct_tree_invalid")
        entries += 1
        if entries > _OWNER_SUPPORT_MAX_ENTRIES or total_bytes > _OWNER_SUPPORT_MAX_BYTES:
            raise RuntimeError("production_storage_owner_direct_tree_invalid")
    if root_children != {"owner-support.json", "source", "site-packages"}:
        raise RuntimeError("production_storage_owner_direct_tree_invalid")
    for required in (
        source_root / "scripts/__init__.py",
        source_root / "scripts/canary/__init__.py",
        module_path,
    ):
        try:
            metadata = required.lstat()
        except OSError:
            raise RuntimeError("production_storage_owner_direct_tree_invalid") from None
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_IMODE(metadata.st_mode) != 0o400
            or metadata.st_nlink != 1
        ):
            raise RuntimeError("production_storage_owner_direct_tree_invalid")
    standard_paths = tuple(sys.path)
    if any(
        not isinstance(item, str)
        or not item
        or not os.path.isabs(item)
        or "site-packages" in Path(item).parts
        or "dist-packages" in Path(item).parts
        for item in standard_paths
    ):
        raise RuntimeError("production_storage_owner_direct_sys_path_invalid")
    sys.path[:] = [str(source_root), str(site_root), *standard_paths]
    return match.group(1)


_DIRECT_RELEASE_SHA = _activate_direct_owner_support() if __package__ is None else None

from scripts.canary import full_canary_owner_launcher as owner
from scripts.canary import passkey_v2_protocol as protocol
from scripts.canary import production_storage_growth_installer as installer


FRAME_SCHEMA = "muncho-production-storage-growth-owner-cli-frame.v1"
RESPONSE_SCHEMA = "muncho-production-storage-growth-owner-cli-response.v1"
FAILURE_SCHEMA = "muncho-production-storage-growth-owner-cli-failure.v1"
MAXIMUM_FRAME_BYTES = 2 * 1024 * 1024
_RELEASE = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FRAME_FIELDS = frozenset({"schema", "operation", "document", "frame_sha256"})
_OPERATIONS = frozenset({
    "build-plan",
    "install-owner-state",
    "install-guest",
    "preflight",
    "request",
    "apply-or-recover",
})


class ProductionStorageOwnerCliError(RuntimeError):
    """Stable, secret-free fixed CLI failure."""


def _read_frame(operation: str) -> Mapping[str, Any]:
    raw = sys.stdin.buffer.read(MAXIMUM_FRAME_BYTES + 1)
    try:
        frame = protocol.decode_canonical_json(raw)
    except protocol.PasskeyV2ProtocolError:
        raise ProductionStorageOwnerCliError(
            "production_storage_owner_cli_frame_invalid"
        ) from None
    unsigned = {
        name: item for name, item in frame.items() if name != "frame_sha256"
    } if isinstance(frame, Mapping) else {}
    if (
        not isinstance(frame, Mapping)
        or set(frame) != _FRAME_FIELDS
        or frame.get("schema") != FRAME_SCHEMA
        or frame.get("operation") != operation
        or not isinstance(frame.get("document"), Mapping)
        or frame.get("frame_sha256") != protocol.sha256_json(unsigned)
    ):
        raise ProductionStorageOwnerCliError(
            "production_storage_owner_cli_frame_invalid"
        )
    document = frame["document"]
    expected_fields = {
        "build-plan": frozenset(),
        "install-owner-state": frozenset(),
        "install-guest": frozenset(),
        "preflight": frozenset({"growth_plan"}),
        "request": frozenset({"growth_plan", "authorization_nonce_sha256"}),
        "apply-or-recover": frozenset({
            "growth_plan",
            "request_id",
            "consume_attempt_id",
            "external_iam_receipt",
        }),
    }[operation]
    if set(document) != expected_fields:
        raise ProductionStorageOwnerCliError(
            "production_storage_owner_cli_frame_invalid"
        )
    for name in (
        "authorization_nonce_sha256",
        "request_id",
        "consume_attempt_id",
    ):
        if name in document and (
            not isinstance(document[name], str)
            or _SHA256.fullmatch(document[name]) is None
        ):
            raise ProductionStorageOwnerCliError(
                "production_storage_owner_cli_frame_invalid"
            )
    return document


def _build_route(release_sha: str) -> tuple[Any, Any]:
    runtime = owner.TrustedGcloudExecutable(release_sha=release_sha)
    owner.activate_trusted_owner_support(runtime, release_sha=release_sha)
    owner.require_trusted_owner_support_activation(
        runtime,
        release_sha=release_sha,
    )
    configuration = owner.PinnedGcloudConfiguration()
    identity = owner.GcloudOwnerAccessToken(
        gcloud_executable=runtime,
        gcloud_configuration=configuration,
    )
    route = owner.build_exact_production_storage_growth_owner_route(
        release_sha=release_sha,
        owner_identity=identity,
        gcloud_executable=runtime,
        gcloud_configuration=configuration,
    )
    return route, runtime


def _runtime_artifacts(release_sha: str) -> tuple[Any, Mapping[str, Any]]:
    runtime = owner.TrustedGcloudExecutable(release_sha=release_sha)
    owner.activate_trusted_owner_support(runtime, release_sha=release_sha)
    owner.require_trusted_owner_support_activation(
        runtime,
        release_sha=release_sha,
    )
    artifacts = owner.observe_exact_production_storage_runtime_artifacts(
        release_sha=release_sha,
        trusted_runtime=runtime,
    )
    return runtime, artifacts


def _revalidate_runtime(runtime: Any, release_sha: str) -> None:
    owner.require_trusted_owner_support_activation(
        runtime,
        release_sha=release_sha,
    )
    owner.observe_exact_production_storage_runtime_artifacts(
        release_sha=release_sha,
        trusted_runtime=runtime,
    )


def _emit(value: Mapping[str, Any]) -> None:
    sys.stdout.buffer.write(protocol.canonical_json_bytes(value) + b"\n")
    sys.stdout.buffer.flush()


def _install_owner_state_privileged(
    release_sha: str,
    binding: Mapping[str, Any],
    *,
    runner: Any = subprocess.run,
) -> Mapping[str, Any]:
    installer_path = Path(installer.__file__).resolve(strict=True)
    command = (
        "/usr/bin/sudo",
        "--non-interactive",
        "--",
        "/usr/bin/python3",
        "-I",
        "-S",
        "-B",
        str(installer_path),
        "install-owner-state",
        "--release-sha",
        release_sha,
    )
    try:
        completed = runner(
            command,
            input=installer.canonical_json_bytes({
                "sealed_artifact_binding": binding,
            }),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            env={
                "HOME": os.path.expanduser("~"),
                "LANG": "C",
                "LC_ALL": "C",
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            },
            timeout=120,
            check=False,
        )
        raw = completed.stdout
        if (
            completed.returncode != 0
            or not raw.endswith(b"\n")
            or raw.endswith(b"\n\n")
        ):
            raise ProductionStorageOwnerCliError(
                "production_storage_owner_privileged_install_failed"
            )
        value = installer.decode_canonical_json(raw[:-1])
    except (OSError, subprocess.SubprocessError):
        raise ProductionStorageOwnerCliError(
            "production_storage_owner_privileged_install_failed"
        ) from None
    if (
        not isinstance(value, Mapping)
        or value.get("schema") != installer.OWNER_READINESS_SCHEMA
        or value.get("release_sha") != release_sha
        or value.get("sealed_artifact_binding_sha256")
        != binding["binding_sha256"]
        or value.get("owner_support_manifest_sha256")
        != binding["owner_support_manifest_sha256"]
        or value.get("ready") is not True
    ):
        raise ProductionStorageOwnerCliError(
            "production_storage_owner_privileged_install_failed"
        )
    return dict(value)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(allow_abbrev=False)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("operation", choices=sorted(_OPERATIONS))
    arguments = parser.parse_args(argv)
    release_sha = arguments.release_sha
    operation = arguments.operation
    try:
        if _RELEASE.fullmatch(release_sha or "") is None:
            raise ProductionStorageOwnerCliError(
                "production_storage_owner_cli_release_invalid"
            )
        if _DIRECT_RELEASE_SHA is not None and release_sha != _DIRECT_RELEASE_SHA:
            raise ProductionStorageOwnerCliError(
                "production_storage_owner_cli_release_invalid"
            )
        document = _read_frame(operation)
        if operation == "build-plan":
            route, runtime = _build_route(release_sha)
            source_preflight = route.collect_source_preflight()
            result = owner.build_exact_production_storage_growth_plan(
                release_sha=release_sha,
                source_preflight=source_preflight,
                trusted_runtime=runtime,
                now_unix=int(time.time()),
            )
            _revalidate_runtime(runtime, release_sha)
        elif operation == "install-owner-state":
            runtime, artifacts = _runtime_artifacts(release_sha)
            binding = installer.build_owner_artifact_binding(
                release_sha,
                artifacts,
            )
            result = _install_owner_state_privileged(release_sha, binding)
            _revalidate_runtime(runtime, release_sha)
        else:
            route, runtime = _build_route(release_sha)
            if operation == "install-guest":
                result = route.install_guest_prerequisite()
            elif operation == "preflight":
                result = route.preflight(
                    growth_plan=document["growth_plan"],
                )
            elif operation == "request":
                result = route.request(
                    growth_plan=document["growth_plan"],
                    authorization_nonce_sha256=document[
                        "authorization_nonce_sha256"
                    ],
                )
            else:
                result = route.apply_or_recover(
                    growth_plan=document["growth_plan"],
                    request_id=document["request_id"],
                    consume_attempt_id=document["consume_attempt_id"],
                    external_iam_receipt=document[
                        "external_iam_receipt"
                    ],
                )
            _revalidate_runtime(runtime, release_sha)
        unsigned = {
            "schema": RESPONSE_SCHEMA,
            "operation": operation,
            "release_sha": release_sha,
            "result": result,
            "caller_selected_paths_allowed": False,
            "caller_selected_commands_allowed": False,
            "caller_selected_targets_allowed": False,
        }
        _emit({**unsigned, "response_sha256": protocol.sha256_json(unsigned)})
        return 0
    except BaseException as error:
        code = (
            error.args[0]
            if len(error.args) == 1
            and isinstance(error.args[0], str)
            and re.fullmatch(r"[a-z0-9_]{1,96}", error.args[0]) is not None
            else "production_storage_owner_cli_failed"
        )
        unsigned = {
            "schema": FAILURE_SCHEMA,
            "operation": operation,
            "release_sha": release_sha,
            "ok": False,
            "error_code": code,
        }
        _emit({**unsigned, "response_sha256": protocol.sha256_json(unsigned)})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FAILURE_SCHEMA",
    "FRAME_SCHEMA",
    "ProductionStorageOwnerCliError",
    "RESPONSE_SCHEMA",
    "main",
]
