#!/usr/bin/python3
"""Fixed guest boundary for the production boot-disk growth transaction.

The module exposes two operations only: collect the exact root-device facts and
grow the exact ``/dev/sda1`` ext4 filesystem online.  Callers cannot provide a
command, device, partition, filesystem, mount point, or size.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

REQUEST_SCHEMA = "muncho-production-storage-growth-guest-request.v1"
RESPONSE_SCHEMA = "muncho-production-storage-growth-guest-response.v1"
READINESS_SCHEMA = "muncho-production-storage-growth-guest-readiness.v1"
INSTALLATION_SCHEMA = "muncho-production-storage-growth-guest-installation.v1"
ROOT_SOURCE = "/dev/sda1"
ROOT_PARENT = "/dev/sda"
ROOT_PARTITION_NUMBER = 1
ROOT_FILESYSTEM = "ext4"
MOUNTPOINT = "/"
TARGET_SIZE_GB = 100
MINIMUM_POSTFLIGHT_FILESYSTEM_BYTES = 104_000_000_000
ENTRYPOINT = Path("/usr/local/lib/muncho/production-storage-growth-guest")
INSTALLATION_RECEIPT = Path(
    "/var/lib/muncho-production-storage-growth-guest/installation.json"
)
SUDOERS_PATH = Path("/etc/sudoers.d/muncho-production-storage-growth")
INTERPRETER = Path("/usr/bin/python3")

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_REQUEST_FIELDS = frozenset({"schema", "operation", "document", "request_sha256"})
_INSTALLATION_FIELDS = frozenset({
    "schema",
    "release_sha",
    "entrypoint",
    "entrypoint_sha256",
    "installer_sha256",
    "interpreter_path",
    "interpreter_resolved_path",
    "interpreter_sha256",
    "installed_at_unix",
    "sudoers_path",
    "sudoers_installed",
    "installation_receipt_sha256",
})


class ProductionStorageGuestError(RuntimeError):
    """Stable, secret-free guest-boundary failure."""


Runner = Callable[[Sequence[str]], bytes]


def _reject_number(_value: str) -> None:
    raise ProductionStorageGuestError("production_storage_guest_json_invalid")


def _strict_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProductionStorageGuestError(
                "production_storage_guest_json_invalid"
            )
        result[key] = value
    return result


def _canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeError):
        raise ProductionStorageGuestError(
            "production_storage_guest_json_invalid"
        ) from None


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json_bytes(value)).hexdigest()


def _decode_canonical_json(raw: bytes) -> Any:
    if not isinstance(raw, bytes) or not raw or len(raw) > 64 * 1024:
        raise ProductionStorageGuestError("production_storage_guest_json_invalid")
    try:
        decoded = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_strict_pairs,
            parse_float=_reject_number,
            parse_constant=_reject_number,
        )
    except (json.JSONDecodeError, UnicodeError):
        raise ProductionStorageGuestError(
            "production_storage_guest_json_invalid"
        ) from None
    if _canonical_json_bytes(decoded) != raw:
        raise ProductionStorageGuestError("production_storage_guest_json_invalid")
    return decoded


def _default_runner(argv: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            list(argv),
            check=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )
    except (OSError, subprocess.SubprocessError):
        raise ProductionStorageGuestError(
            "production_storage_guest_command_failed"
        ) from None
    return completed.stdout


def _positive_int(value: Any) -> int:
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        raise ProductionStorageGuestError(
            "production_storage_guest_observation_invalid"
        ) from None
    if parsed < 0:
        raise ProductionStorageGuestError(
            "production_storage_guest_observation_invalid"
        )
    return parsed


class FixedProductionStorageGuest:
    """Exact root observation and growth; no caller-selected shell surface."""

    def __init__(
        self,
        *,
        runner: Runner = _default_runner,
        entrypoint: Path = ENTRYPOINT,
        installation_receipt: Path = INSTALLATION_RECEIPT,
        sudoers_path: Path = SUDOERS_PATH,
        interpreter: Path = INTERPRETER,
        expected_uid: int = 0,
        expected_gid: int = 0,
        expected_interpreter_uid: int = 0,
        expected_interpreter_gid: int = 0,
    ) -> None:
        if (
            not callable(runner)
            or not all(
                isinstance(value, Path)
                for value in (
                    entrypoint,
                    installation_receipt,
                    sudoers_path,
                    interpreter,
                )
            )
            or type(expected_uid) is not int
            or expected_uid < 0
            or type(expected_gid) is not int
            or expected_gid < 0
            or type(expected_interpreter_uid) is not int
            or expected_interpreter_uid < 0
            or type(expected_interpreter_gid) is not int
            or expected_interpreter_gid < 0
        ):
            raise ProductionStorageGuestError(
                "production_storage_guest_configuration_invalid"
            )
        self._runner = runner
        self._entrypoint = entrypoint
        self._installation_receipt = installation_receipt
        self._sudoers_path = sudoers_path
        self._interpreter = interpreter
        self._expected_uid = expected_uid
        self._expected_gid = expected_gid
        self._expected_interpreter_uid = expected_interpreter_uid
        self._expected_interpreter_gid = expected_interpreter_gid

    def readiness(self) -> Mapping[str, Any]:
        try:
            entrypoint = self._entrypoint.lstat()
            receipt = self._installation_receipt.lstat()
            raw = self._installation_receipt.read_bytes()
            if not raw.endswith(b"\n") or raw.endswith(b"\n\n"):
                raise ProductionStorageGuestError(
                    "production_storage_guest_readiness_invalid"
                )
            installed = _decode_canonical_json(raw[:-1])
            source_sha256 = hashlib.sha256(self._entrypoint.read_bytes()).hexdigest()
            interpreter_resolved = Path(
                os.path.realpath(self._interpreter, strict=True)
            )
            interpreter = interpreter_resolved.lstat()
            interpreter_sha256 = hashlib.sha256(
                interpreter_resolved.read_bytes()
            ).hexdigest()
        except (OSError, ProductionStorageGuestError):
            raise ProductionStorageGuestError(
                "production_storage_guest_readiness_invalid"
            ) from None
        try:
            self._sudoers_path.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            raise ProductionStorageGuestError(
                "production_storage_guest_readiness_invalid"
            ) from None
        else:
            raise ProductionStorageGuestError(
                "production_storage_guest_readiness_invalid"
            )
        if (
            not isinstance(installed, Mapping)
            or set(installed) != _INSTALLATION_FIELDS
            or installed.get("schema") != INSTALLATION_SCHEMA
            or _SHA40.fullmatch(str(installed.get("release_sha") or "")) is None
            or installed.get("entrypoint") != str(ENTRYPOINT)
            or installed.get("entrypoint_sha256") != source_sha256
            or _SHA256.fullmatch(
                str(installed.get("installer_sha256") or "")
            )
            is None
            or installed.get("interpreter_path") != str(INTERPRETER)
            or installed.get("interpreter_resolved_path")
            != str(interpreter_resolved)
            or installed.get("interpreter_sha256") != interpreter_sha256
            or type(installed.get("installed_at_unix")) is not int
            or installed["installed_at_unix"] <= 0
            or installed.get("sudoers_path") != str(SUDOERS_PATH)
            or installed.get("sudoers_installed") is not False
            or installed.get("installation_receipt_sha256")
            != _sha256_json({
                name: item
                for name, item in installed.items()
                if name != "installation_receipt_sha256"
            })
            or not stat.S_ISREG(entrypoint.st_mode)
            or stat.S_IMODE(entrypoint.st_mode) != 0o755
            or entrypoint.st_uid != self._expected_uid
            or entrypoint.st_gid != self._expected_gid
            or entrypoint.st_nlink != 1
            or not stat.S_ISREG(receipt.st_mode)
            or stat.S_IMODE(receipt.st_mode) != 0o600
            or receipt.st_uid != self._expected_uid
            or receipt.st_gid != self._expected_gid
            or receipt.st_nlink != 1
            or not stat.S_ISREG(interpreter.st_mode)
            or stat.S_IMODE(interpreter.st_mode) & 0o022
            or interpreter.st_uid != self._expected_interpreter_uid
            or interpreter.st_gid != self._expected_interpreter_gid
        ):
            raise ProductionStorageGuestError(
                "production_storage_guest_readiness_invalid"
            )
        unsigned = {
            "schema": READINESS_SCHEMA,
            "release_sha": installed["release_sha"],
            "entrypoint": str(ENTRYPOINT),
            "entrypoint_sha256": source_sha256,
            "entrypoint_uid": entrypoint.st_uid,
            "entrypoint_gid": entrypoint.st_gid,
            "entrypoint_mode": "0755",
            "entrypoint_link_count": entrypoint.st_nlink,
            "installer_sha256": installed["installer_sha256"],
            "interpreter_path": str(INTERPRETER),
            "interpreter_resolved_path": str(interpreter_resolved),
            "interpreter_sha256": interpreter_sha256,
            "installation_receipt_sha256": installed[
                "installation_receipt_sha256"
            ],
            "sudoers_path": str(SUDOERS_PATH),
            "sudoers_required": False,
            "sudoers_absent": True,
            "root_transport_required": True,
            "ready": True,
        }
        return {**unsigned, "readiness_sha256": _sha256_json(unsigned)}

    def observe(self) -> Mapping[str, Any]:
        try:
            boot_id = (
                Path("/proc/sys/kernel/random/boot_id")
                .read_text(encoding="ascii")
                .strip()
            )
            root = json.loads(
                self._runner((
                    "/usr/bin/findmnt",
                    "--json",
                    "--bytes",
                    "--output",
                    "SOURCE,FSTYPE,TARGET,AVAIL,SIZE",
                    MOUNTPOINT,
                )).decode("utf-8", errors="strict")
            )
            block = json.loads(
                self._runner((
                    "/usr/bin/lsblk",
                    "--json",
                    "--bytes",
                    "--output",
                    "PATH,PKNAME,PARTN,SIZE",
                    ROOT_SOURCE,
                )).decode("utf-8", errors="strict")
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise ProductionStorageGuestError(
                "production_storage_guest_observation_invalid"
            ) from None
        filesystems = root.get("filesystems") if isinstance(root, Mapping) else None
        devices = block.get("blockdevices") if isinstance(block, Mapping) else None
        if (
            not isinstance(filesystems, list)
            or len(filesystems) != 1
            or not isinstance(filesystems[0], Mapping)
            or not isinstance(devices, list)
            or len(devices) != 1
            or not isinstance(devices[0], Mapping)
        ):
            raise ProductionStorageGuestError(
                "production_storage_guest_observation_invalid"
            )
        filesystem = filesystems[0]
        device = devices[0]
        parent = str(device.get("pkname", ""))
        if parent and not parent.startswith("/dev/"):
            parent = f"/dev/{parent}"
        if (
            filesystem.get("source") != ROOT_SOURCE
            or filesystem.get("fstype") != ROOT_FILESYSTEM
            or filesystem.get("target") != MOUNTPOINT
            or device.get("path") != ROOT_SOURCE
            or parent != ROOT_PARENT
            or _positive_int(device.get("partn")) != ROOT_PARTITION_NUMBER
        ):
            raise ProductionStorageGuestError(
                "production_storage_guest_identity_invalid"
            )
        disk_size = _positive_int(
            self._runner(("/usr/sbin/blockdev", "--getsize64", ROOT_PARENT)).decode(
                "ascii", errors="strict"
            )
        )
        partition_size = _positive_int(device.get("size"))
        filesystem_size = _positive_int(filesystem.get("size"))
        available = _positive_int(filesystem.get("avail"))
        if not 0 <= available <= filesystem_size <= partition_size <= disk_size:
            raise ProductionStorageGuestError(
                "production_storage_guest_observation_invalid"
            )
        return {
            "boot_id": boot_id,
            "root_source": ROOT_SOURCE,
            "root_parent": ROOT_PARENT,
            "root_partition_number": ROOT_PARTITION_NUMBER,
            "root_filesystem": ROOT_FILESYSTEM,
            "mountpoint": MOUNTPOINT,
            "disk_size_bytes": disk_size,
            "partition_size_bytes": partition_size,
            "filesystem_size_bytes": filesystem_size,
            "available_bytes": available,
        }

    def grow(self, *, idempotency_key_sha256: str) -> Mapping[str, Any]:
        if (
            not isinstance(idempotency_key_sha256, str)
            or _SHA256.fullmatch(idempotency_key_sha256) is None
        ):
            raise ProductionStorageGuestError(
                "production_storage_guest_idempotency_key_invalid"
            )
        before = self.observe()
        if before["disk_size_bytes"] < TARGET_SIZE_GB * 1024**3:
            raise ProductionStorageGuestError(
                "production_storage_guest_provider_resize_not_visible"
            )
        if (
            before["partition_size_bytes"]
            < MINIMUM_POSTFLIGHT_FILESYSTEM_BYTES
        ):
            self._runner((
                "/usr/bin/growpart",
                ROOT_PARENT,
                str(ROOT_PARTITION_NUMBER),
            ))
            after_partition = self.observe()
            if (
                after_partition["boot_id"] != before["boot_id"]
                or after_partition["partition_size_bytes"]
                < MINIMUM_POSTFLIGHT_FILESYSTEM_BYTES
            ):
                raise ProductionStorageGuestError(
                    "production_storage_guest_partition_growth_incomplete"
                )
        else:
            after_partition = before
        if (
            after_partition["filesystem_size_bytes"]
            < MINIMUM_POSTFLIGHT_FILESYSTEM_BYTES
        ):
            self._runner((
                "/usr/sbin/resize2fs",
                ROOT_SOURCE,
            ))
        after = self.observe()
        if (
            after["boot_id"] != before["boot_id"]
            or after["partition_size_bytes"]
            < MINIMUM_POSTFLIGHT_FILESYSTEM_BYTES
            or after["filesystem_size_bytes"]
            < MINIMUM_POSTFLIGHT_FILESYSTEM_BYTES
        ):
            raise ProductionStorageGuestError(
                "production_storage_guest_growth_incomplete"
            )
        return {
            "completed": True,
            "idempotency_key_sha256": idempotency_key_sha256,
            "guest": after,
        }


def _validate_request(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _REQUEST_FIELDS:
        raise ProductionStorageGuestError("production_storage_guest_request_invalid")
    unsigned = {name: item for name, item in value.items() if name != "request_sha256"}
    if (
        value.get("schema") != REQUEST_SCHEMA
        or value.get("operation") not in {"readiness", "observe", "grow"}
        or not isinstance(value.get("document"), Mapping)
        or value.get("request_sha256") != _sha256_json(unsigned)
    ):
        raise ProductionStorageGuestError("production_storage_guest_request_invalid")
    document = value["document"]
    if value["operation"] in {"readiness", "observe"} and document:
        raise ProductionStorageGuestError("production_storage_guest_request_invalid")
    if value["operation"] == "grow" and set(document) != {"idempotency_key_sha256"}:
        raise ProductionStorageGuestError("production_storage_guest_request_invalid")
    return value


def main() -> int:
    try:
        raw = sys.stdin.buffer.read(16_385)
        if not raw or len(raw) > 16_384:
            raise ProductionStorageGuestError(
                "production_storage_guest_request_invalid"
            )
        request = _validate_request(_decode_canonical_json(raw))
        guest = FixedProductionStorageGuest()
        if request["operation"] == "readiness":
            result = guest.readiness()
        elif request["operation"] == "observe":
            result = guest.observe()
        else:
            result = guest.grow(
                idempotency_key_sha256=request["document"]["idempotency_key_sha256"]
            )
        unsigned = {
            "schema": RESPONSE_SCHEMA,
            "operation": request["operation"],
            "ok": True,
            "document": result,
        }
        response = {**unsigned, "response_sha256": _sha256_json(unsigned)}
        sys.stdout.buffer.write(_canonical_json_bytes(response))
        sys.stdout.buffer.flush()
        return 0
    except ProductionStorageGuestError:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "FixedProductionStorageGuest",
    "ProductionStorageGuestError",
    "ENTRYPOINT",
    "INSTALLATION_RECEIPT",
    "INSTALLATION_SCHEMA",
    "READINESS_SCHEMA",
    "REQUEST_SCHEMA",
    "RESPONSE_SCHEMA",
    "SUDOERS_PATH",
]
