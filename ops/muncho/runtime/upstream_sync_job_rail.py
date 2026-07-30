#!/usr/bin/env python3
"""Digest-bound 3-hour mechanical rail for Muncho and SkyAI upstream sync.

The sync service receives only a dedicated GitHub credential.  It runs two
exact reviewed jobs, writes a sanitized public report, and never invokes a
model, Discord, merge, deploy, gateway restart, frontend, PBX, or public
upstream mutation path.  A separate daily reporter service owns delivery.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping, Sequence


RAIL_SCHEMA = "muncho-dual-upstream-sync-rail.v1"
MANIFEST_SCHEMA = "muncho-dual-upstream-sync-package.v1"
RUN_RECEIPT_SCHEMA = "muncho-dual-upstream-sync-receipt.v1"
PUBLIC_REPORT_SCHEMA = "muncho-dual-upstream-sync-public.v1"
MUNCHO_JOB_ID = "muncho_fork_upstream_sync_pr"
SKYAI_JOB_ID = "skyai_upstream_sync_pr"
JOB_IDS = (MUNCHO_JOB_ID, SKYAI_JOB_ID)
SYNC_SERVICE_UNIT = "muncho-dual-upstream-sync.service"
SYNC_TIMER_UNIT = "muncho-dual-upstream-sync.timer"
REPORT_SERVICE_UNIT = "muncho-dual-upstream-sync-report.service"
REPORT_TIMER_UNIT = "muncho-dual-upstream-sync-report.timer"
SYNC_USER = "muncho-dual-sync"
SYNC_GROUP = "muncho-dual-sync"
REPORT_USER = "ai-platform-brain"
REPORT_GROUP = "ai-platform-brain"
STATE_DIRECTORY_NAME = "muncho-dual-upstream-sync"
REPORT_STATE_DIRECTORY_NAME = "muncho-dual-upstream-sync-report"
LOGS_DIRECTORY_NAME = "muncho-dual-upstream-sync"
STATE_ROOT = Path("/var/lib") / STATE_DIRECTORY_NAME
REPORT_STATE_ROOT = Path("/var/lib") / REPORT_STATE_DIRECTORY_NAME
PUBLIC_REPORT_ROOT = Path("/var/log") / LOGS_DIRECTORY_NAME
PRIVATE_PUBLIC_REPORT_ROOT = Path("/var/log/private") / LOGS_DIRECTORY_NAME
REPORT_VIEW_DIRECTORY_NAME = "muncho-dual-upstream-sync-report-view"
REPORT_VIEW_ROOT = Path("/run") / REPORT_VIEW_DIRECTORY_NAME
RUNTIME_ROOT = Path("/run") / STATE_DIRECTORY_NAME
PACKAGE_ROOT = Path(
    "/var/lib/muncho-production-legacy-cutover/staged/dual-upstream-sync-rail"
)
CREDENTIAL_NAME = "github-token"
CREDENTIAL_SOURCE = Path("/etc/muncho/fork-auto-sync/github-token")
GH_PATH = Path("/usr/bin/gh")
GIT_PATH = Path("/usr/bin/git")
RELEASES_ROOT = Path("/opt/adventico-ai-platform/hermes-agent-releases")
HERMES_HOME = Path("/opt/adventico-ai-platform/hermes-home")
SYSTEMD_STUB_RESOLV_CONF = Path("/run/systemd/resolve/stub-resolv.conf")
SYSTEMD_UPLINK_RESOLV_CONF = Path("/run/systemd/resolve/resolv.conf")
SOURCE_MARKER_RELATIVE = Path(".codex-source-commit")
RAIL_RELATIVE = Path("ops/muncho/runtime/upstream_sync_job_rail.py")
MUNCHO_ROUTINE_RELATIVE = Path(
    "ops/muncho/runtime/fork_upstream_auto_sync_pr_routine.py"
)
HARDENING_RELATIVE = Path("ops/muncho/runtime/auto_sync_hardening.py")
SKYAI_ROUTINE_RELATIVE = Path(
    "ops/muncho/runtime/skyai_upstream_sync_pr_routine.py"
)
REPORTER_RELATIVE = Path(
    "ops/muncho/runtime/upstream_sync_discord_reporter.py"
)
DISCORD_CHANNEL_ID = "1504852355588423801"
RUN_TIMEOUT_SECONDS = 45 * 60
MAX_CAPTURE_BYTES = 8 * 1024 * 1024
MAX_PUBLIC_REPORTS = 80
EXIT_PASS = 0
EXIT_PARTIAL = 2
EXIT_BLOCKED = 3
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_]{20,4096}$")
_INVOCATION = re.compile(r"^[0-9a-f]{32}$")
_SAFE_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_PR_URL = re.compile(r"^https://github\.com/lomliev/hermes-agent/pull/[0-9]+$")


class DualSyncRailError(RuntimeError):
    """Stable package or launcher failure."""


@dataclass(frozen=True)
class RailPackage:
    revision: str
    release_root: Path
    sender_revision: str
    sender_release_root: Path
    source_digests: Mapping[str, str]
    host_binary_digests: Mapping[str, str]
    artifacts: Mapping[str, bytes]
    manifest_bytes: bytes
    manifest_sha256: str


@dataclass(frozen=True)
class ChildResult:
    job_id: str
    returncode: int | None
    timed_out: bool
    stdout_bytes: int
    stdout_sha256: str
    stderr_bytes: int
    stderr_sha256: str
    report: Mapping[str, Any] | None


def now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DualSyncRailError("dual_sync_json_not_canonical") from exc


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def read_regular(
    path: Path,
    *,
    maximum: int,
    expected_mode: int | None = None,
) -> bytes:
    flags = (
        os.O_RDONLY
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise DualSyncRailError("dual_sync_file_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= maximum
            or (
                expected_mode is not None
                and stat.S_IMODE(before.st_mode) != expected_mode
            )
        ):
            raise DualSyncRailError("dual_sync_file_metadata_invalid")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    identity = lambda item: (
        item.st_dev,
        item.st_ino,
        item.st_mode,
        item.st_uid,
        item.st_gid,
        item.st_nlink,
        item.st_size,
        item.st_mtime_ns,
        item.st_ctime_ns,
    )
    value = b"".join(chunks)
    if len(value) != before.st_size or identity(before) != identity(after):
        raise DualSyncRailError("dual_sync_file_changed_while_reading")
    return value


def digest_file(path: Path, *, maximum: int = 128 * 1024 * 1024) -> str:
    return sha256(read_regular(path, maximum=maximum))


def validate_digest(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise DualSyncRailError(f"{label}_invalid")
    return value


def release_root(revision: str) -> Path:
    if not isinstance(revision, str) or _SHA40.fullmatch(revision) is None:
        raise DualSyncRailError("dual_sync_revision_invalid")
    return RELEASES_ROOT / f"hermes-agent-{revision[:12]}"


def exact_revision_marker(revision: str) -> bytes:
    """Return the one canonical on-disk framing for a release revision."""

    if not isinstance(revision, str) or _SHA40.fullmatch(revision) is None:
        raise DualSyncRailError("dual_sync_revision_invalid")
    return revision.encode("ascii", errors="strict") + b"\n"


def source_paths(release: Path) -> dict[str, Path]:
    return {
        "rail": release / RAIL_RELATIVE,
        "muncho_routine": release / MUNCHO_ROUTINE_RELATIVE,
        "hardening": release / HARDENING_RELATIVE,
        "skyai_routine": release / SKYAI_ROUTINE_RELATIVE,
        "reporter": release / REPORTER_RELATIVE,
    }


def host_binary_fact(path: Path) -> str:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DualSyncRailError("dual_sync_host_binary_unavailable") from exc
    mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or mode & 0o022
        or not mode & 0o111
    ):
        raise DualSyncRailError("dual_sync_host_binary_metadata_invalid")
    return digest_file(path)


def validate_credential_metadata(path: Path = CREDENTIAL_SOURCE) -> None:
    try:
        metadata = path.lstat()
    except OSError as exc:
        raise DualSyncRailError("dual_sync_github_credential_unavailable") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o400
    ):
        raise DualSyncRailError("dual_sync_github_credential_metadata_invalid")


def _digest_arguments(
    source_digests: Mapping[str, str],
    binary_digests: Mapping[str, str],
) -> str:
    ordered = (
        ("rail-sha256", source_digests["rail"]),
        ("muncho-routine-sha256", source_digests["muncho_routine"]),
        ("hardening-sha256", source_digests["hardening"]),
        ("skyai-routine-sha256", source_digests["skyai_routine"]),
        ("reporter-sha256", source_digests["reporter"]),
        ("gh-sha256", binary_digests[str(GH_PATH)]),
        ("git-sha256", binary_digests[str(GIT_PATH)]),
    )
    return " ".join(f"--{name} {value}" for name, value in ordered)


def render_sync_service(
    *,
    revision: str,
    release: Path,
    source_digests: Mapping[str, str],
    binary_digests: Mapping[str, str],
) -> bytes:
    interpreter = release / ".venv/bin/python"
    arguments = _digest_arguments(source_digests, binary_digests)
    lines = [
        "# Exact release-addressed dual upstream-sync rail; do not edit.",
        f"# ReleaseRevision={revision}",
        "[Unit]",
        "Description=Muncho and SkyAI fork-only upstream sync mechanical rail",
        "Wants=network-online.target",
        "After=network-online.target",
        f"AssertPathExists={interpreter}",
        f"AssertPathExists={release / RAIL_RELATIVE}",
        f"AssertPathExists={release / MUNCHO_ROUTINE_RELATIVE}",
        f"AssertPathExists={release / HARDENING_RELATIVE}",
        f"AssertPathExists={release / SKYAI_ROUTINE_RELATIVE}",
        f"AssertPathExists={CREDENTIAL_SOURCE}",
        f"AssertPathExists={SYSTEMD_STUB_RESOLV_CONF}",
        "",
        "[Service]",
        "Type=oneshot",
        "DynamicUser=yes",
        f"User={SYNC_USER}",
        f"Group={SYNC_GROUP}",
        f"LoadCredential={CREDENTIAL_NAME}:{CREDENTIAL_SOURCE}",
        f"StateDirectory={STATE_DIRECTORY_NAME}",
        "StateDirectoryMode=0700",
        f"RuntimeDirectory={STATE_DIRECTORY_NAME}",
        "RuntimeDirectoryMode=0700",
        "RuntimeDirectoryPreserve=no",
        f"LogsDirectory={LOGS_DIRECTORY_NAME}",
        "LogsDirectoryMode=0755",
        "WorkingDirectory=/",
        f"Environment=HOME={STATE_ROOT}",
        "Environment=LANG=C.UTF-8",
        "Environment=LC_ALL=C.UTF-8",
        "Environment=PATH=/usr/bin:/bin",
        "Environment=TZ=UTC",
        (
            f"ExecStart={interpreter} -I -S -B {release / RAIL_RELATIVE} "
            f"run-all --revision {revision} {arguments}"
        ),
        "TimeoutStartSec=6000s",
        "TimeoutStopSec=30s",
        "KillMode=mixed",
        "LimitCORE=0",
        "UMask=0022",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "PrivateDevices=yes",
        "PrivateTmp=yes",
        "ProtectClock=yes",
        "ProtectControlGroups=yes",
        "ProtectHome=yes",
        "ProtectHostname=yes",
        "ProtectKernelLogs=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelTunables=yes",
        "ProtectProc=invisible",
        "ProtectSystem=strict",
        "ProcSubset=pid",
        "RemoveIPC=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "SystemCallArchitectures=native",
        "IPAddressDeny=169.254.169.254/32",
        (
            f"BindReadOnlyPaths={SYSTEMD_STUB_RESOLV_CONF}:"
            f"{SYSTEMD_UPLINK_RESOLV_CONF}"
        ),
        f"ReadOnlyPaths={release}",
        "ReadOnlyPaths=/usr/bin/git",
        "ReadOnlyPaths=/usr/bin/gh",
        f"ReadWritePaths={STATE_ROOT}",
        f"ReadWritePaths={RUNTIME_ROOT}",
        f"ReadWritePaths={PUBLIC_REPORT_ROOT}",
        f"InaccessiblePaths=-{HERMES_HOME}",
        "InaccessiblePaths=-/opt/adventico-ai-platform/canonical-brain",
        "InaccessiblePaths=-/etc/muncho/discord-connector-credentials",
        "InaccessiblePaths=-/etc/muncho/discord-edge-credentials",
        "InaccessiblePaths=-/run/credentials/hermes-cloud-gateway.service",
        "StandardOutput=null",
        "StandardError=journal",
    ]
    result = ("\n".join(lines) + "\n").encode("utf-8")
    validate_sync_service(result, revision=revision, release=release)
    return result


def render_sync_timer() -> bytes:
    result = (
        "\n".join(
            [
                "[Unit]",
                "Description=Schedule Muncho and SkyAI upstream sync every 3 hours",
                "",
                "[Timer]",
                f"Unit={SYNC_SERVICE_UNIT}",
                "OnActiveSec=30m",
                "OnUnitActiveSec=3h",
                "AccuracySec=1m",
                "RandomizedDelaySec=5m",
                "Persistent=false",
                "",
                "[Install]",
                "WantedBy=timers.target",
            ]
        )
        + "\n"
    ).encode("utf-8")
    validate_sync_timer(result)
    return result


def render_report_service(
    *,
    release: Path,
    sender_release: Path,
    sender_python_sha256: str,
) -> bytes:
    interpreter = release / ".venv/bin/python"
    reporter = release / REPORTER_RELATIVE
    sender_python = sender_release / ".venv/bin/python"
    lines = [
        "# Separate daily reporter; no GitHub credential is loaded.",
        "[Unit]",
        "Description=Send sanitized Muncho and SkyAI sync report to internal channel",
        "Wants=network-online.target",
        "After=network-online.target",
        f"AssertPathExists={interpreter}",
        f"AssertPathExists={reporter}",
        f"AssertPathExists={sender_python}",
        f"AssertPathExists={PRIVATE_PUBLIC_REPORT_ROOT}",
        f"AssertPathExists={SYSTEMD_STUB_RESOLV_CONF}",
        "",
        "[Service]",
        "Type=oneshot",
        f"User={REPORT_USER}",
        f"Group={REPORT_GROUP}",
        f"WorkingDirectory={sender_release}",
        f"StateDirectory={REPORT_STATE_DIRECTORY_NAME}",
        "StateDirectoryMode=0700",
        f"RuntimeDirectory={REPORT_VIEW_DIRECTORY_NAME}",
        "RuntimeDirectoryMode=0700",
        f"Environment=HOME={HERMES_HOME}",
        f"Environment=HERMES_HOME={HERMES_HOME}",
        "Environment=LANG=C.UTF-8",
        "Environment=LC_ALL=C.UTF-8",
        "Environment=PATH=/usr/bin:/bin",
        "Environment=TZ=Europe/Sofia",
        (
            f"ExecStart={interpreter} -I -B {reporter} "
            f"--public-report-dir {REPORT_VIEW_ROOT} "
            f"--state-dir {REPORT_STATE_ROOT} "
            f"--channel-id {DISCORD_CHANNEL_ID} "
            f"--sender-python {sender_python} "
            f"--sender-python-sha256 {sender_python_sha256} "
            "--timezone Europe/Sofia --window-hours 24"
        ),
        "TimeoutStartSec=120s",
        "LimitCORE=0",
        "UMask=0077",
        "NoNewPrivileges=yes",
        "CapabilityBoundingSet=",
        "AmbientCapabilities=",
        "LockPersonality=yes",
        "MemoryDenyWriteExecute=yes",
        "PrivateDevices=yes",
        "PrivateTmp=yes",
        "ProtectClock=yes",
        "ProtectControlGroups=yes",
        "ProtectHome=yes",
        "ProtectHostname=yes",
        "ProtectKernelLogs=yes",
        "ProtectKernelModules=yes",
        "ProtectKernelTunables=yes",
        "ProtectProc=invisible",
        "ProtectSystem=strict",
        "ProcSubset=pid",
        "RemoveIPC=yes",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "RestrictNamespaces=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "SystemCallArchitectures=native",
        "IPAddressDeny=169.254.169.254/32",
        (
            f"BindReadOnlyPaths={SYSTEMD_STUB_RESOLV_CONF}:"
            f"{SYSTEMD_UPLINK_RESOLV_CONF}"
        ),
        (
            f"BindReadOnlyPaths={PRIVATE_PUBLIC_REPORT_ROOT}:"
            f"{REPORT_VIEW_ROOT}"
        ),
        f"ReadOnlyPaths={release}",
        f"ReadOnlyPaths={sender_release}",
        f"ReadOnlyPaths={HERMES_HOME}",
        f"ReadOnlyPaths={REPORT_VIEW_ROOT}",
        f"ReadWritePaths={REPORT_STATE_ROOT}",
        f"InaccessiblePaths=-{STATE_ROOT}",
        f"InaccessiblePaths=-{CREDENTIAL_SOURCE.parent}",
        "StandardOutput=null",
        "StandardError=journal",
    ]
    result = ("\n".join(lines) + "\n").encode("utf-8")
    validate_report_service(
        result,
        release=release,
        sender_release=sender_release,
        sender_python_sha256=sender_python_sha256,
    )
    return result


def render_report_timer() -> bytes:
    result = (
        "\n".join(
            [
                "[Unit]",
                "Description=Schedule daily Muncho and SkyAI sync report",
                "",
                "[Timer]",
                f"Unit={REPORT_SERVICE_UNIT}",
                "OnCalendar=*-*-* 08:00:00 Europe/Sofia",
                "AccuracySec=1m",
                "RandomizedDelaySec=5m",
                "Persistent=true",
                "",
                "[Install]",
                "WantedBy=timers.target",
            ]
        )
        + "\n"
    ).encode("utf-8")
    validate_report_timer(result)
    return result


def validate_sync_service(value: bytes, *, revision: str, release: Path) -> None:
    text = value.decode("utf-8", errors="strict")
    required = (
        f"# ReleaseRevision={revision}\n",
        "DynamicUser=yes\n",
        f"LoadCredential={CREDENTIAL_NAME}:{CREDENTIAL_SOURCE}\n",
        f"StateDirectory={STATE_DIRECTORY_NAME}\n",
        f"LogsDirectory={LOGS_DIRECTORY_NAME}\n",
        "NoNewPrivileges=yes\n",
        "ProtectSystem=strict\n",
        "IPAddressDeny=169.254.169.254/32\n",
        (
            f"BindReadOnlyPaths={SYSTEMD_STUB_RESOLV_CONF}:"
            f"{SYSTEMD_UPLINK_RESOLV_CONF}\n"
        ),
        "StandardOutput=null\n",
        f"{release / RAIL_RELATIVE} run-all ",
    )
    forbidden = (
        "EnvironmentFile=",
        "PassEnvironment=",
        "Restart=",
        "OnFailure=",
        "OPENAI_API_KEY",
        "DISCORD_BOT_TOKEN",
        "HERMES_HOME=",
        "AUTO_MERGE_DEPLOY_APPROVED",
        "muncho-auto-deploy-release",
    )
    if (
        not text.endswith("\n")
        or any(text.count(item) != 1 for item in required)
        or any(item in text for item in forbidden)
    ):
        raise DualSyncRailError("dual_sync_service_unit_invalid")


def validate_sync_timer(value: bytes) -> None:
    text = value.decode("utf-8", errors="strict")
    required = (
        f"Unit={SYNC_SERVICE_UNIT}\n",
        "OnActiveSec=30m\n",
        "OnUnitActiveSec=3h\n",
        "Persistent=false\n",
    )
    if any(text.count(item) != 1 for item in required) or "OnCalendar=" in text:
        raise DualSyncRailError("dual_sync_timer_unit_invalid")


def validate_report_service(
    value: bytes,
    *,
    release: Path,
    sender_release: Path,
    sender_python_sha256: str,
) -> None:
    text = value.decode("utf-8", errors="strict")
    required = (
        f"User={REPORT_USER}\n",
        f"WorkingDirectory={sender_release}\n",
        f"StateDirectory={REPORT_STATE_DIRECTORY_NAME}\n",
        f"RuntimeDirectory={REPORT_VIEW_DIRECTORY_NAME}\n",
        f"ReadOnlyPaths={REPORT_VIEW_ROOT}\n",
        f"--public-report-dir {REPORT_VIEW_ROOT} ",
        f"InaccessiblePaths=-{STATE_ROOT}\n",
        f"--channel-id {DISCORD_CHANNEL_ID} ",
        f"--sender-python {sender_release / '.venv/bin/python'} ",
        f"--sender-python-sha256 {sender_python_sha256} ",
        "NoNewPrivileges=yes\n",
        "IPAddressDeny=169.254.169.254/32\n",
        (
            f"BindReadOnlyPaths={SYSTEMD_STUB_RESOLV_CONF}:"
            f"{SYSTEMD_UPLINK_RESOLV_CONF}\n"
        ),
        (
            f"BindReadOnlyPaths={PRIVATE_PUBLIC_REPORT_ROOT}:"
            f"{REPORT_VIEW_ROOT}\n"
        ),
    )
    forbidden = (
        "LoadCredential=",
        "GH_TOKEN",
        "GITHUB_TOKEN",
        str(CREDENTIAL_SOURCE),
        "AUTO_MERGE",
        "muncho-auto-deploy-release",
    )
    if (
        _SHA256.fullmatch(sender_python_sha256) is None
        or
        not text.endswith("\n")
        or any(text.count(item) != 1 for item in required)
        or any(item in text for item in forbidden)
    ):
        raise DualSyncRailError("dual_sync_report_service_unit_invalid")


def validate_report_timer(value: bytes) -> None:
    text = value.decode("utf-8", errors="strict")
    required = (
        f"Unit={REPORT_SERVICE_UNIT}\n",
        "OnCalendar=*-*-* 08:00:00 Europe/Sofia\n",
        "Persistent=true\n",
    )
    if any(text.count(item) != 1 for item in required):
        raise DualSyncRailError("dual_sync_report_timer_unit_invalid")


def build_package(revision: str, sender_revision: str) -> RailPackage:
    release = release_root(revision)
    try:
        resolved = release.resolve(strict=True)
    except OSError as exc:
        raise DualSyncRailError("dual_sync_release_unavailable") from exc
    if resolved != release:
        raise DualSyncRailError("dual_sync_release_not_final_address")
    marker = read_regular(release / SOURCE_MARKER_RELATIVE, maximum=128)
    if marker != exact_revision_marker(revision):
        raise DualSyncRailError("dual_sync_release_marker_mismatch")
    interpreter = release / ".venv/bin/python"
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise DualSyncRailError("dual_sync_interpreter_unavailable")
    validate_credential_metadata()
    sender_release = release_root(sender_revision)
    try:
        resolved_sender = sender_release.resolve(strict=True)
    except OSError as exc:
        raise DualSyncRailError("dual_sync_sender_release_unavailable") from exc
    if resolved_sender != sender_release:
        raise DualSyncRailError("dual_sync_sender_release_not_final_address")
    sender_marker = read_regular(
        sender_release / SOURCE_MARKER_RELATIVE,
        maximum=128,
    )
    if sender_marker != exact_revision_marker(sender_revision):
        raise DualSyncRailError("dual_sync_sender_release_marker_mismatch")
    sender_python = sender_release / ".venv/bin/python"
    if not sender_python.is_file() or not os.access(sender_python, os.X_OK):
        raise DualSyncRailError("dual_sync_sender_interpreter_unavailable")
    try:
        sender_python_target = sender_python.resolve(strict=True)
    except OSError as exc:
        raise DualSyncRailError(
            "dual_sync_sender_interpreter_unavailable"
        ) from exc
    sender_python_sha = digest_file(sender_python_target)
    paths = source_paths(release)
    source_digests = {name: digest_file(path) for name, path in paths.items()}
    binary_digests = {
        str(GH_PATH): host_binary_fact(GH_PATH),
        str(GIT_PATH): host_binary_fact(GIT_PATH),
    }
    artifacts = {
        SYNC_SERVICE_UNIT: render_sync_service(
            revision=revision,
            release=release,
            source_digests=source_digests,
            binary_digests=binary_digests,
        ),
        SYNC_TIMER_UNIT: render_sync_timer(),
        REPORT_SERVICE_UNIT: render_report_service(
            release=release,
            sender_release=sender_release,
            sender_python_sha256=sender_python_sha,
        ),
        REPORT_TIMER_UNIT: render_report_timer(),
    }
    jobs = [
        {
            "job_id": MUNCHO_JOB_ID,
            "routine": str(release / MUNCHO_ROUTINE_RELATIVE),
            "argv": ["--execute"],
            "fork_repository": "lomliev/hermes-agent",
            "base_branch": "main",
            "upstream_repository_read_only": "NousResearch/hermes-agent",
            "auto_merge_or_deploy_enabled": False,
        },
        {
            "job_id": SKYAI_JOB_ID,
            "routine": str(release / SKYAI_ROUTINE_RELATIVE),
            "argv": ["--execute"],
            "fork_repository": "lomliev/hermes-agent",
            "base_branch": "codex/skyai-v2-hermes-plugin-bootstrap",
            "upstream_repository_read_only": "NousResearch/hermes-agent",
            "auto_merge_or_deploy_enabled": False,
        },
    ]
    unsigned = {
        "schema": MANIFEST_SCHEMA,
        "rail_schema": RAIL_SCHEMA,
        "release_revision": revision,
        "release_root": str(release),
        "sender_revision": sender_revision,
        "sender_release_root": str(sender_release),
        "sender_interpreter_sha256": sender_python_sha,
        "jobs": jobs,
        "source_digests": source_digests,
        "host_binary_digests": binary_digests,
        "artifacts": {name: sha256(value) for name, value in artifacts.items()},
        "github_credential_path": str(CREDENTIAL_SOURCE),
        "github_credential_value_recorded": False,
        "sync_service_model_or_provider_dependency": False,
        "sync_service_discord_dependency": False,
        "reporter_github_credential_dependency": False,
        "package_installs_or_starts_units": False,
    }
    manifest_sha = sha256(canonical(unsigned))
    manifest = canonical({**unsigned, "manifest_sha256": manifest_sha}) + b"\n"
    return RailPackage(
        revision=revision,
        release_root=release,
        sender_revision=sender_revision,
        sender_release_root=sender_release,
        source_digests=source_digests,
        host_binary_digests=binary_digests,
        artifacts=artifacts,
        manifest_bytes=manifest,
        manifest_sha256=manifest_sha,
    )


def validate_manifest(
    manifest: Mapping[str, Any],
    *,
    revision: str,
    sender_revision: str,
) -> dict[str, Any]:
    if (
        not isinstance(manifest, Mapping)
        or manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("rail_schema") != RAIL_SCHEMA
        or manifest.get("release_revision") != revision
        or manifest.get("release_root") != str(release_root(revision))
        or manifest.get("sender_revision") != sender_revision
        or manifest.get("sender_release_root")
        != str(release_root(sender_revision))
        or not isinstance(manifest.get("sender_interpreter_sha256"), str)
        or _SHA256.fullmatch(manifest["sender_interpreter_sha256"]) is None
        or manifest.get("github_credential_value_recorded") is not False
        or manifest.get("sync_service_model_or_provider_dependency") is not False
        or manifest.get("sync_service_discord_dependency") is not False
        or manifest.get("reporter_github_credential_dependency") is not False
        or manifest.get("package_installs_or_starts_units") is not False
    ):
        raise DualSyncRailError("dual_sync_manifest_invalid")
    jobs = manifest.get("jobs")
    if (
        not isinstance(jobs, list)
        or [item.get("job_id") for item in jobs if isinstance(item, dict)]
        != list(JOB_IDS)
        or any(
            not isinstance(item, dict)
            or item.get("argv") != ["--execute"]
            or item.get("fork_repository") != "lomliev/hermes-agent"
            or item.get("upstream_repository_read_only")
            != "NousResearch/hermes-agent"
            or item.get("auto_merge_or_deploy_enabled") is not False
            for item in jobs
        )
    ):
        raise DualSyncRailError("dual_sync_manifest_invalid")
    recorded = manifest.get("manifest_sha256")
    if (
        not isinstance(recorded, str)
        or _SHA256.fullmatch(recorded) is None
        or sha256(
            canonical(
                {
                    key: value
                    for key, value in manifest.items()
                    if key != "manifest_sha256"
                }
            )
        )
        != recorded
    ):
        raise DualSyncRailError("dual_sync_manifest_invalid")
    return dict(manifest)


def write_artifact(path: Path, value: bytes, *, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, mode)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def stage_package(package: RailPackage, *, output_root: Path = PACKAGE_ROOT) -> None:
    manifest = json.loads(package.manifest_bytes.decode("ascii"))
    validate_manifest(
        manifest,
        revision=package.revision,
        sender_revision=package.sender_revision,
    )
    for name, value in package.artifacts.items():
        write_artifact(output_root / name, value, mode=0o444)
    write_artifact(output_root / "manifest.json", package.manifest_bytes, mode=0o444)


def verify_package(package: RailPackage, *, output_root: Path = PACKAGE_ROOT) -> None:
    for name, expected in {
        **package.artifacts,
        "manifest.json": package.manifest_bytes,
    }.items():
        path = output_root / name
        observed = read_regular(path, maximum=2 * 1024 * 1024)
        if observed != expected or stat.S_IMODE(path.stat().st_mode) != 0o444:
            raise DualSyncRailError("dual_sync_package_artifact_drifted")


def credential() -> str:
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if not directory or not os.path.isabs(directory):
        raise DualSyncRailError("dual_sync_credentials_directory_invalid")
    raw = read_regular(Path(directory) / CREDENTIAL_NAME, maximum=4096)
    value = raw.decode("ascii", errors="strict")
    if _TOKEN.fullmatch(value) is None:
        raise DualSyncRailError("dual_sync_github_credential_invalid")
    return value


def attest_release(args: argparse.Namespace) -> tuple[Path, Mapping[str, Path]]:
    release = release_root(args.revision)
    if release.resolve(strict=True) != release:
        raise DualSyncRailError("dual_sync_release_not_final_address")
    marker = read_regular(release / SOURCE_MARKER_RELATIVE, maximum=128)
    if marker != exact_revision_marker(args.revision):
        raise DualSyncRailError("dual_sync_release_marker_mismatch")
    paths = source_paths(release)
    expected = {
        "rail": args.rail_sha256,
        "muncho_routine": args.muncho_routine_sha256,
        "hardening": args.hardening_sha256,
        "skyai_routine": args.skyai_routine_sha256,
        "reporter": args.reporter_sha256,
    }
    for name, path in paths.items():
        if digest_file(path) != validate_digest(expected[name], f"{name}_sha256"):
            raise DualSyncRailError("dual_sync_source_digest_mismatch")
    if Path(__file__).resolve(strict=True) != paths["rail"]:
        raise DualSyncRailError("dual_sync_launcher_not_release_addressed")
    if host_binary_fact(GH_PATH) != validate_digest(args.gh_sha256, "gh_sha256"):
        raise DualSyncRailError("dual_sync_host_binary_digest_drifted")
    if host_binary_fact(GIT_PATH) != validate_digest(args.git_sha256, "git_sha256"):
        raise DualSyncRailError("dual_sync_host_binary_digest_drifted")
    return release, paths


def capture_digest(stream: BinaryIO) -> tuple[int, str]:
    stream.seek(0)
    digest = hashlib.sha256()
    total = 0
    while True:
        chunk = stream.read(64 * 1024)
        if not chunk:
            break
        total += len(chunk)
        digest.update(chunk)
        if total > MAX_CAPTURE_BYTES:
            raise DualSyncRailError("dual_sync_child_output_bound_exceeded")
    return total, digest.hexdigest()


def load_json(path: Path) -> Mapping[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(read_regular(path, maximum=4 * 1024 * 1024))
    except (DualSyncRailError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def no_worktree_cleanup_receipt() -> dict[str, int]:
    """Record that the rail performs no name-inferred destructive cleanup."""

    return {"removed": 0, "failed": 0}


def run_child(
    *,
    job_id: str,
    routine: Path,
    environment: Mapping[str, str],
    report_path: Path,
) -> ChildResult:
    # The child report is a run result, not a cache. Remove only this exact
    # rail-owned path before launch so a crash cannot replay a prior run's
    # apparently valid status.
    report_path.unlink(missing_ok=True)
    with tempfile.TemporaryFile(dir=RUNTIME_ROOT) as stdout, tempfile.TemporaryFile(
        dir=RUNTIME_ROOT
    ) as stderr:
        returncode: int | None = None
        timed_out = False
        try:
            completed = subprocess.run(
                [sys.executable, "-I", "-S", "-B", str(routine), "--execute"],
                cwd="/",
                env=dict(environment),
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=RUN_TIMEOUT_SECONDS,
            )
            returncode = completed.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
        stdout_bytes, stdout_sha = capture_digest(stdout)
        stderr_bytes, stderr_sha = capture_digest(stderr)
    return ChildResult(
        job_id=job_id,
        returncode=returncode,
        timed_out=timed_out,
        stdout_bytes=stdout_bytes,
        stdout_sha256=stdout_sha,
        stderr_bytes=stderr_bytes,
        stderr_sha256=stderr_sha,
        report=load_json(report_path),
    )


def safe_code(value: object, fallback: str = "unknown") -> str:
    if not isinstance(value, str):
        return fallback
    return value if _SAFE_CODE.fullmatch(value) else fallback


def safe_sha(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if _SHA40.fullmatch(value) else None


def safe_count(value: object) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def safe_pr(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value if _PR_URL.fullmatch(value) else None


def muncho_component(result: ChildResult) -> dict[str, Any]:
    report = result.report or {}
    outcome = safe_code(report.get("status"), "missing_report")
    blocked = report.get("blocked")
    if result.timed_out:
        status, blocker = "BLOCKED", "timeout"
    elif not report:
        status, blocker = "BLOCKED", "missing_report"
    elif type(blocked) is not bool:
        status, blocker = "BLOCKED", "invalid_blocked_field"
    elif (
        (blocked is True and result.returncode != 2)
        or (blocked is False and result.returncode != 0)
    ):
        status, blocker = "BLOCKED", "child_exit_status_mismatch"
    elif blocked is True:
        status, blocker = "BLOCKED", outcome
    elif outcome == "no_drift_no_action":
        status, blocker = "PASS", None
    else:
        status, blocker = "PARTIAL", None
    refs = report.get("fresh_refs")
    refs = refs if isinstance(refs, dict) else {}
    component = {
        "status": status,
        "outcome": outcome,
        "source_sha": safe_sha(refs.get("fork_main_ref")),
        "upstream_sha": safe_sha(refs.get("upstream_main_ref")),
        "ahead": safe_count(refs.get("ahead_by")),
        "behind": safe_count(refs.get("behind_by")),
        "pr_url": safe_pr(report.get("pr_url")),
        "blocker": blocker,
    }
    return component


def skyai_component(result: ChildResult) -> dict[str, Any]:
    report = result.report or {}
    exact_status = report.get("status")
    status_valid = (
        isinstance(exact_status, str)
        and exact_status in {"PASS", "PARTIAL", "BLOCKED"}
    )
    status = exact_status if status_valid else "BLOCKED"
    blocker_value = report.get("blocker")
    blocker = (
        safe_code(blocker_value, "invalid_blocker")
        if blocker_value is not None
        else None
    )
    if result.timed_out:
        status, blocker = "BLOCKED", "timeout"
    elif not report:
        status, blocker = "BLOCKED", "missing_report"
    elif not status_valid:
        status, blocker = "BLOCKED", "invalid_status"
    elif (
        (exact_status == "PASS" and result.returncode != 0)
        or (
            exact_status in {"PARTIAL", "BLOCKED"}
            and result.returncode != 2
        )
    ):
        status, blocker = "BLOCKED", "child_exit_status_mismatch"
    return {
        "status": status,
        "outcome": safe_code(report.get("outcome"), "missing_report"),
        "source_sha": safe_sha(report.get("source_sha")),
        "upstream_sha": safe_sha(report.get("upstream_sha")),
        "candidate_sha": safe_sha(report.get("candidate_sha")),
        "ahead": safe_count(report.get("head_ahead")),
        "behind": safe_count(report.get("head_behind")),
        "pr_url": safe_pr(report.get("pr_url")),
        "blocker": blocker,
    }


def aggregate_status(components: Sequence[Mapping[str, Any]]) -> str:
    priority = {"PASS": 0, "PARTIAL": 1, "BLOCKED": 2}
    statuses = [
        value if value in priority else "BLOCKED"
        for item in components
        for value in (item.get("status"),)
        if isinstance(value, str)
    ]
    if len(statuses) != len(components):
        return "BLOCKED"
    return max(
        statuses,
        key=priority.__getitem__,
    )


def render_public_markdown(report: Mapping[str, Any]) -> str:
    lines = [
        "# Muncho + SkyAI upstream sync",
        "",
        f"Status: {report['status']}",
        f"Time: {report['created_at_utc']}",
    ]
    for label, key in (("Muncho/Hermes", "muncho"), ("SkyAI", "skyai")):
        component = report[key]
        lines.extend(
            [
                "",
                f"## {label}",
                f"- status: {component['status']}",
                f"- outcome: {component['outcome']}",
                f"- source: {component['source_sha'] or '—'}",
                f"- upstream: {component['upstream_sha'] or '—'}",
                f"- ahead/behind: {component['ahead']} / {component['behind']}",
            ]
        )
        if component.get("blocker"):
            lines.append(f"- blocker: {component['blocker']}")
        if component.get("pr_url"):
            lines.append(f"- PR: {component['pr_url']}")
    lines.extend(
        [
            "",
            "Safety: no auto-merge, deploy, gateway restart, SkyAI runtime, "
            "frontend, PBX, model, or public-upstream mutation.",
        ]
    )
    return "\n".join(lines) + "\n"


def write_public_report(report: Mapping[str, Any]) -> str:
    PUBLIC_REPORT_ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
    os.chmod(PUBLIC_REPORT_ROOT, 0o755)
    stamp = str(report["created_at_utc"]).replace("-", "").replace(":", "")
    encoded = json.dumps(
        dict(report),
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8") + b"\n"
    markdown = render_public_markdown(report).encode("utf-8")
    for target, value in (
        (PUBLIC_REPORT_ROOT / f"report-{stamp}.json", encoded),
        (PUBLIC_REPORT_ROOT / "latest.json", encoded),
        (PUBLIC_REPORT_ROOT / "latest.md", markdown),
    ):
        write_artifact(target, value, mode=0o644)
    archives = sorted(PUBLIC_REPORT_ROOT.glob("report-*.json"))
    for old in archives[:-MAX_PUBLIC_REPORTS]:
        old.unlink(missing_ok=True)
    return sha256(encoded)


def write_private_receipt(receipt: Mapping[str, Any]) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_ROOT, 0o700)
    encoded = canonical(dict(receipt)) + b"\n"
    write_artifact(STATE_ROOT / "latest.json", encoded, mode=0o600)
    receipts = STATE_ROOT / "receipts"
    receipts.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(receipts, 0o700)
    write_artifact(
        receipts / f"{receipt['receipt_id']}.json",
        encoded,
        mode=0o600,
    )


def run_all(args: argparse.Namespace) -> int:
    release, paths = attest_release(args)
    token = credential()
    STATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    RUNTIME_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = RUNTIME_ROOT / "rail.lock"
    descriptor = os.open(
        lock_path,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0),
        0o600,
    )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        common = {
            "GH_HOST": "github.com",
            "GH_PROMPT_DISABLED": "1",
            "GH_TOKEN": token,
            "GIT_TERMINAL_PROMPT": "0",
            "HOME": str(STATE_ROOT),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PATH": "/usr/bin:/bin",
            "TZ": "UTC",
        }
        muncho_state = STATE_ROOT / "muncho-state"
        skyai_state = STATE_ROOT / "skyai-state"
        for path in (
            muncho_state,
            skyai_state,
            STATE_ROOT / "muncho-reports",
            STATE_ROOT / "muncho-worktrees",
            STATE_ROOT / "skyai-worktrees",
        ):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, 0o700)
        muncho_env = {
            **common,
            "FORK_UPSTREAM_AUTO_SYNC_EXECUTE_APPROVED": "1",
            "FORK_UPSTREAM_AUTO_SYNC_GH": str(GH_PATH),
            "FORK_UPSTREAM_AUTO_SYNC_REPORT_DIR": str(
                STATE_ROOT / "muncho-reports"
            ),
            "FORK_UPSTREAM_AUTO_SYNC_STATE_DIR": str(muncho_state),
            "FORK_UPSTREAM_AUTO_SYNC_WORKTREE_ROOT": str(
                STATE_ROOT / "muncho-worktrees"
            ),
        }
        skyai_env = {
            **common,
            "HERMES_PYTHON": sys.executable,
            "SKYAI_UPSTREAM_SYNC_EXECUTE_APPROVED": "1",
            "SKYAI_UPSTREAM_SYNC_GH": str(GH_PATH),
            "SKYAI_UPSTREAM_SYNC_STATE_DIR": str(skyai_state),
            "SKYAI_UPSTREAM_SYNC_WORKTREE_ROOT": str(
                STATE_ROOT / "skyai-worktrees"
            ),
        }
        muncho_result = run_child(
            job_id=MUNCHO_JOB_ID,
            routine=paths["muncho_routine"],
            environment=muncho_env,
            report_path=muncho_state / "auto-sync-pr-latest.json",
        )
        inter_job_cleanup = no_worktree_cleanup_receipt()
        skyai_result = run_child(
            job_id=SKYAI_JOB_ID,
            routine=paths["skyai_routine"],
            environment=skyai_env,
            report_path=skyai_state / "skyai-sync-latest.json",
        )
        muncho = muncho_component(muncho_result)
        skyai = skyai_component(skyai_result)
        created = now_utc()
        public = {
            "schema": PUBLIC_REPORT_SCHEMA,
            "created_at_utc": created,
            "status": aggregate_status((muncho, skyai)),
            "muncho": muncho,
            "skyai": skyai,
            "auto_merge": False,
            "deploy": False,
            "runtime_mutation": False,
            "provider_or_model_invoked": False,
            "discord_delivery_attempted": False,
            "secret_material_recorded": False,
        }
        public_sha = write_public_report(public)
        invocation = os.environ.get("INVOCATION_ID", "")
        if _INVOCATION.fullmatch(invocation) is None:
            invocation = uuid.uuid4().hex
        receipt_id = sha256(
            canonical(
                {
                    "invocation_id": invocation,
                    "revision": args.revision,
                    "created_at_utc": created,
                }
            )
        )
        children = []
        for result in (muncho_result, skyai_result):
            children.append(
                {
                    "job_id": result.job_id,
                    "returncode": result.returncode,
                    "timed_out": result.timed_out,
                    "stdout_bytes": result.stdout_bytes,
                    "stdout_sha256": result.stdout_sha256,
                    "stderr_bytes": result.stderr_bytes,
                    "stderr_sha256": result.stderr_sha256,
                    "content_recorded": False,
                }
            )
        receipt = {
            "schema": RUN_RECEIPT_SCHEMA,
            "rail_schema": RAIL_SCHEMA,
            "receipt_id": receipt_id,
            "created_at_utc": created,
            "release_revision": args.revision,
            "release_root": str(release),
            "status": public["status"],
            "children": children,
            "inter_job_cleanup": inter_job_cleanup,
            "public_report_sha256": public_sha,
            "provider_or_model_invoked": False,
            "discord_delivery_attempted": False,
            "auto_merge_or_deploy_approved": False,
            "secret_material_recorded": False,
        }
        write_private_receipt(receipt)
        return {
            "PASS": EXIT_PASS,
            "PARTIAL": EXIT_PARTIAL,
            "BLOCKED": EXIT_BLOCKED,
        }[public["status"]]
    finally:
        os.close(descriptor)


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)
    package = commands.add_parser("package")
    package.add_argument("--revision", required=True)
    package.add_argument("--sender-revision", required=True)
    package.add_argument("--output-root", type=Path, default=PACKAGE_ROOT)
    verify = commands.add_parser("verify-package")
    verify.add_argument("--revision", required=True)
    verify.add_argument("--sender-revision", required=True)
    verify.add_argument("--output-root", type=Path, default=PACKAGE_ROOT)
    run = commands.add_parser("run-all")
    run.add_argument("--revision", required=True)
    for name in (
        "rail_sha256",
        "muncho_routine_sha256",
        "hardening_sha256",
        "skyai_routine_sha256",
        "reporter_sha256",
        "gh_sha256",
        "git_sha256",
    ):
        run.add_argument(f"--{name.replace('_', '-')}", required=True)
    return root


def main() -> int:
    args = parser().parse_args()
    if args.command == "package":
        package = build_package(args.revision, args.sender_revision)
        stage_package(package, output_root=args.output_root)
        verify_package(package, output_root=args.output_root)
        return 0
    if args.command == "verify-package":
        package = build_package(args.revision, args.sender_revision)
        verify_package(package, output_root=args.output_root)
        return 0
    if args.command == "run-all":
        return run_all(args)
    raise DualSyncRailError("dual_sync_command_invalid")


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except DualSyncRailError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None


__all__ = [
    "DISCORD_CHANNEL_ID",
    "DualSyncRailError",
    "JOB_IDS",
    "MANIFEST_SCHEMA",
    "PUBLIC_REPORT_SCHEMA",
    "REPORT_SERVICE_UNIT",
    "REPORT_TIMER_UNIT",
    "RailPackage",
    "SYNC_SERVICE_UNIT",
    "SYNC_TIMER_UNIT",
    "aggregate_status",
    "build_package",
    "render_public_markdown",
    "stage_package",
    "validate_manifest",
    "verify_package",
]
