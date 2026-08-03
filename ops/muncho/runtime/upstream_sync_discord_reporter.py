#!/usr/bin/env python3
"""Send one bounded daily Muncho + SkyAI sync report through Hermes.

The reporter reads only world-readable sanitized reports produced by the
mechanical sync service.  It has no GitHub credential and makes exactly one
delivery attempt per invocation.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
from typing import Any, Iterable, Iterator, Mapping, Sequence
from zoneinfo import ZoneInfo


REPORT_SCHEMA = "muncho-dual-upstream-sync-public.v1"
DELIVERY_SCHEMA = "muncho-dual-upstream-sync-discord-delivery.v2"
ATTEMPT_SCHEMA = "muncho-dual-upstream-sync-discord-attempt.v1"
OUTCOME_SCHEMA = "muncho-dual-upstream-sync-discord-outcome.v1"
REPORT_SET_SCHEMA = "muncho-dual-upstream-sync-report-set.v1"
DEFAULT_CHANNEL_ID = "1504852355588423801"
DEFAULT_TIMEZONE = "Europe/Sofia"
DEFAULT_WINDOW_HOURS = 24
MAX_MESSAGE_LENGTH = 1900
MAX_MESSAGE_BYTES = 16 * 1024
MAX_REPORT_FILES = 128
MAX_REPORT_COUNT = 32
MAX_REPORT_BYTES = 512 * 1024
MAX_REPORT_SET_BYTES = 2 * 1024 * 1024
MAX_STATE_RECEIPT_BYTES = 64 * 1024
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_PR_URL = re.compile(r"^https://github\.com/lomliev/hermes-agent/pull/[0-9]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_MESSAGE_ID = re.compile(r"^[0-9]{1,32}$")


class DeliveryStateError(RuntimeError):
    """Fail-closed reporter state or binding error."""


def canonical_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DeliveryStateError("delivery_state_not_canonical") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def utc_timestamp(value: datetime) -> str:
    return (
        value.astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def sofia_date(value: datetime) -> str:
    return value.astimezone(ZoneInfo(DEFAULT_TIMEZONE)).date().isoformat()


def safe_sha(value: object) -> str:
    text = str(value or "")
    return text[:10] if _SHA40.fullmatch(text) else "—"


def safe_count(value: object) -> str:
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return str(value)
    return "—"


def safe_code(value: object) -> str:
    text = str(value or "")
    return text if _CODE.fullmatch(text) else "unknown"


def safe_pr(value: object) -> str | None:
    text = str(value or "")
    return text if _PR_URL.fullmatch(text) else None


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_nlink,
        value.st_uid,
        value.st_gid,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def load_reports(
    public_dir: Path,
    *,
    now: datetime,
    window_hours: int,
) -> list[dict[str, Any]]:
    lower = now.astimezone(timezone.utc) - timedelta(hours=window_hours)
    reports: list[dict[str, Any]] = []
    paths = sorted(public_dir.glob("report-*.json"))
    if len(paths) > MAX_REPORT_FILES:
        raise DeliveryStateError("public_report_file_bound_exceeded")
    for path in paths:
        try:
            reached = path.lstat()
            if (
                not stat.S_ISREG(reached.st_mode)
                or reached.st_nlink != 1
                or reached.st_size > MAX_REPORT_BYTES
            ):
                continue
            raw = path.read_bytes()
            after = path.lstat()
            if (
                _file_identity(reached) != _file_identity(after)
                or len(raw) > MAX_REPORT_BYTES
            ):
                continue
            payload = json.loads(raw.decode("utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("schema") != REPORT_SCHEMA:
            continue
        created = parse_timestamp(payload.get("created_at_utc"))
        if created is None or not (lower < created <= now.astimezone(timezone.utc)):
            continue
        payload = dict(payload)
        payload["_created"] = created
        reports.append(payload)
    if len(reports) > MAX_REPORT_COUNT:
        raise DeliveryStateError("public_report_window_bound_exceeded")
    return sorted(reports, key=lambda item: item["_created"])


def report_set_sha256(reports: Sequence[Mapping[str, Any]]) -> str:
    if len(reports) > MAX_REPORT_COUNT:
        raise DeliveryStateError("public_report_window_bound_exceeded")
    entries: list[dict[str, str]] = []
    total = 0
    for report in reports:
        created = report.get("_created")
        if not isinstance(created, datetime):
            raise DeliveryStateError("public_report_created_time_invalid")
        payload = dict(report)
        payload.pop("_created", None)
        raw = canonical_bytes(payload)
        total += len(raw)
        if len(raw) > MAX_REPORT_BYTES or total > MAX_REPORT_SET_BYTES:
            raise DeliveryStateError("public_report_set_bound_exceeded")
        entries.append(
            {
                "created_at_utc": utc_timestamp(created),
                "report_sha256": sha256_bytes(raw),
            }
        )
    bounded = canonical_bytes({"schema": REPORT_SET_SCHEMA, "reports": entries})
    if len(bounded) > MAX_REPORT_SET_BYTES:
        raise DeliveryStateError("public_report_set_bound_exceeded")
    return sha256_bytes(bounded)


def overall_status(reports: Iterable[Mapping[str, Any]]) -> str:
    priority = {"PASS": 0, "PARTIAL": 1, "BLOCKED": 2}
    statuses = [
        str(report.get("status") or "").upper()
        for report in reports
        if str(report.get("status") or "").upper() in priority
    ]
    return max(statuses, key=priority.__getitem__) if statuses else "NO DATA"


def _component_line(label: str, component: Mapping[str, Any]) -> list[str]:
    status = str(component.get("status") or "BLOCKED").upper()
    if status not in {"PASS", "PARTIAL", "BLOCKED"}:
        status = "BLOCKED"
    icon = {"PASS": "✅", "PARTIAL": "⚠️", "BLOCKED": "⛔"}[status]
    source = safe_sha(component.get("source_sha"))
    upstream = safe_sha(component.get("upstream_sha"))
    outcome = safe_code(component.get("outcome"))
    lines = [
        (
            f"**{label}:** {icon} {status} · `{outcome}` · "
            f"`{source}` / `{upstream}` · "
            f"ahead {safe_count(component.get('ahead'))}, "
            f"behind {safe_count(component.get('behind'))}"
        )
    ]
    blocker = component.get("blocker")
    if blocker:
        lines.append(f"  blocker: `{safe_code(blocker)}`")
    pr_url = safe_pr(component.get("pr_url"))
    if pr_url:
        lines.append(f"  PR: {pr_url}")
    return lines


def format_daily_report(
    reports: Sequence[dict[str, Any]],
    *,
    now: datetime,
    timezone_name: str,
    window_hours: int,
) -> str:
    local_tz = ZoneInfo(timezone_name)
    start = (now - timedelta(hours=window_hours)).astimezone(local_tz)
    end = now.astimezone(local_tz)
    status = overall_status(reports)
    icon = {
        "PASS": "✅",
        "PARTIAL": "⚠️",
        "BLOCKED": "⛔",
        "NO DATA": "⚪",
    }[status]
    counts = Counter(str(item.get("status") or "").upper() for item in reports)
    lines = [
        "**Muncho + SkyAI upstream sync — дневен отчет**",
        f"**Статус:** {icon} {status}",
        f"**Период:** {start:%d.%m.%Y %H:%M} – {end:%d.%m.%Y %H:%M} ({timezone_name})",
        (
            f"**Изпълнения:** {len(reports)} "
            f"(PASS {counts['PASS']} · PARTIAL {counts['PARTIAL']} · "
            f"BLOCKED {counts['BLOCKED']})"
        ),
    ]
    if not reports:
        lines.extend(
            [
                "⚠️ Няма структуриран 3-часов sync отчет за периода.",
                "**Safety:** без auto-merge, deploy и runtime промени.",
            ]
        )
        return "\n".join(lines)

    latest = reports[-1]
    created = latest["_created"].astimezone(local_tz)
    lines.append(f"**Последно изпълнение:** {created:%d.%m.%Y %H:%M}")
    muncho = latest.get("muncho")
    skyai = latest.get("skyai")
    lines.extend(
        _component_line("Muncho/Hermes", muncho if isinstance(muncho, dict) else {})
    )
    lines.extend(_component_line("SkyAI", skyai if isinstance(skyai, dict) else {}))
    lines.append(
        "**Safety:** кандидат PR-и само във fork-а; без auto-merge, deploy, "
        "gateway restart, SkyAI runtime, frontend или PBX промени."
    )
    message = "\n".join(lines)
    if len(message) > MAX_MESSAGE_LENGTH:
        return message[: MAX_MESSAGE_LENGTH - 1].rstrip() + "…"
    return message


def delivery_succeeded(payload: object) -> bool:
    return isinstance(payload, dict) and (
        payload.get("ok") is True or payload.get("success") is True
    )


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(64 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def deliver_once(
    message: str,
    *,
    channel_id: str,
    sender_python: Path | None = None,
    sender_python_sha256: str | None = None,
    runner: Any = subprocess.run,
) -> dict[str, Any]:
    if not re.fullmatch(r"[0-9]{15,22}", channel_id):
        return {"status": "BLOCKED", "blocker": "invalid_discord_channel_id"}
    try:
        sender = sender_python or Path(sys.executable)
        if not sender.is_absolute() or not sender.is_file():
            return {"status": "BLOCKED", "blocker": "sender_python_unavailable"}
        if sender_python_sha256 is not None and (
            _SHA256.fullmatch(sender_python_sha256) is None
            or file_sha256(sender) != sender_python_sha256
        ):
            return {"status": "BLOCKED", "blocker": "sender_python_digest_drifted"}
        completed = runner(
            (
                str(sender),
                "-m",
                "hermes_cli.main",
                "send",
                "--to",
                f"discord:{channel_id}",
                "--json",
            ),
            input=message,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
            timeout=90,
        )
    except (OSError, subprocess.SubprocessError):
        return {"status": "BLOCKED", "blocker": "discord_delivery_error"}
    if completed.returncode != 0:
        return {
            "status": "BLOCKED",
            "blocker": "discord_delivery_failed",
            "returncode": completed.returncode,
        }
    try:
        payload = json.loads(completed.stdout or "null")
    except json.JSONDecodeError:
        return {"status": "BLOCKED", "blocker": "discord_delivery_invalid_result"}
    if not delivery_succeeded(payload):
        return {"status": "BLOCKED", "blocker": "discord_delivery_rejected"}
    result: dict[str, Any] = {"status": "PASS"}
    if isinstance(payload, dict) and payload.get("message_id"):
        result["message_id"] = str(payload["message_id"])
    return result


def _directory_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
    )


def _validate_controlled_parent_chain(path: Path, *, runtime_uid: int) -> None:
    current = path
    while True:
        reached = os.lstat(current)
        if (
            not stat.S_ISDIR(reached.st_mode)
            or stat.S_ISLNK(reached.st_mode)
            or reached.st_uid not in {0, runtime_uid}
            or stat.S_IMODE(reached.st_mode) & 0o022
        ):
            raise DeliveryStateError("delivery_state_parent_not_controlled")
        if current == current.parent:
            return
        current = current.parent


@contextmanager
def _open_state_dir(path: Path) -> Iterator[int]:
    if (
        not path.is_absolute()
        or ".." in path.parts
        or path != Path(os.path.normpath(str(path)))
    ):
        raise DeliveryStateError("delivery_state_path_invalid")
    runtime_uid = os.geteuid()
    _validate_controlled_parent_chain(path.parent, runtime_uid=runtime_uid)
    before = os.lstat(path)
    if (
        not stat.S_ISDIR(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_uid != runtime_uid
        or stat.S_IMODE(before.st_mode) != 0o700
    ):
        raise DeliveryStateError("delivery_state_directory_invalid")
    descriptor = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        opened = os.fstat(descriptor)
        reached = os.lstat(path)
        if (
            _directory_identity(opened) != _directory_identity(before)
            or _directory_identity(opened) != _directory_identity(reached)
        ):
            raise DeliveryStateError("delivery_state_directory_changed")
        yield descriptor
        reached = os.lstat(path)
        if _directory_identity(os.fstat(descriptor)) != _directory_identity(reached):
            raise DeliveryStateError("delivery_state_directory_changed")
    finally:
        os.close(descriptor)


def _read_receipt_at(parent_fd: int, name: str) -> dict[str, Any]:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise DeliveryStateError("delivery_state_receipt_absent") from exc
    parent = os.fstat(parent_fd)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != parent.st_uid
        or before.st_gid != parent.st_gid
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_size > MAX_STATE_RECEIPT_BYTES
    ):
        raise DeliveryStateError("delivery_state_receipt_invalid")
    descriptor = os.open(
        name,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        dir_fd=parent_fd,
    )
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before):
            raise DeliveryStateError("delivery_state_receipt_changed")
        chunks: list[bytes] = []
        remaining = MAX_STATE_RECEIPT_BYTES + 1
        while remaining:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_STATE_RECEIPT_BYTES:
            raise DeliveryStateError("delivery_state_receipt_invalid")
        reached = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if (
            _file_identity(opened) != _file_identity(reached)
            or len(raw) != reached.st_size
        ):
            raise DeliveryStateError("delivery_state_receipt_changed")
    finally:
        os.close(descriptor)
    try:
        value = json.loads(raw.decode("ascii", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DeliveryStateError("delivery_state_receipt_invalid") from exc
    if not isinstance(value, dict) or raw != canonical_bytes(value) + b"\n":
        raise DeliveryStateError("delivery_state_receipt_invalid")
    return value


def _create_receipt_at(parent_fd: int, name: str, value: Mapping[str, Any]) -> bool:
    raw = canonical_bytes(value) + b"\n"
    if len(raw) > MAX_STATE_RECEIPT_BYTES:
        raise DeliveryStateError("delivery_state_receipt_too_large")
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(name, flags, 0o600, dir_fd=parent_fd)
    except FileExistsError:
        return False
    try:
        parent = os.fstat(parent_fd)
        os.fchown(descriptor, parent.st_uid, parent.st_gid)
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(raw):
            written = os.write(descriptor, raw[offset:])
            if written <= 0:
                raise DeliveryStateError("delivery_state_receipt_write_stalled")
            offset += written
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.fsync(parent_fd)
    if _read_receipt_at(parent_fd, name) != dict(value):
        raise DeliveryStateError("delivery_state_receipt_changed")
    return True


def _validated_binding(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "sofia_date",
        "timezone",
        "channel_id",
        "window_hours",
        "report_count",
        "report_set_sha256",
        "message_sha256",
        "message_bytes",
    }:
        raise DeliveryStateError("delivery_binding_invalid")
    sofia_date = value.get("sofia_date")
    channel_id = value.get("channel_id")
    window_hours = value.get("window_hours")
    report_count = value.get("report_count")
    report_set_digest = value.get("report_set_sha256")
    message_digest = value.get("message_sha256")
    message_bytes = value.get("message_bytes")
    if (
        _DATE.fullmatch(str(sofia_date)) is None
        or value.get("timezone") != DEFAULT_TIMEZONE
        or re.fullmatch(r"[0-9]{15,22}", str(channel_id)) is None
        or type(window_hours) is not int
        or window_hours <= 0
        or type(report_count) is not int
        or not 0 <= report_count <= MAX_REPORT_COUNT
        or _SHA256.fullmatch(str(report_set_digest)) is None
        or _SHA256.fullmatch(str(message_digest)) is None
        or type(message_bytes) is not int
        or not 0 < message_bytes <= MAX_MESSAGE_BYTES
    ):
        raise DeliveryStateError("delivery_binding_invalid")
    return dict(value)


def delivery_binding(
    *,
    now: datetime,
    channel_id: str,
    window_hours: int,
    report_count: int,
    report_set_digest: str,
    message_digest: str,
    message_bytes: int,
) -> dict[str, Any]:
    return _validated_binding({
        "sofia_date": sofia_date(now),
        "timezone": DEFAULT_TIMEZONE,
        "channel_id": channel_id,
        "window_hours": window_hours,
        "report_count": report_count,
        "report_set_sha256": report_set_digest,
        "message_sha256": message_digest,
        "message_bytes": message_bytes,
    })


def _validate_attempt_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "schema",
        "binding",
        "binding_sha256",
        "reserved_at_utc",
        "attempts",
        "network_send_authorized",
        "secret_material_recorded",
        "attempt_receipt_sha256",
    }:
        raise DeliveryStateError("delivery_attempt_receipt_invalid")
    unsigned = dict(value)
    digest = unsigned.pop("attempt_receipt_sha256", None)
    binding = value.get("binding")
    reserved_at = parse_timestamp(value.get("reserved_at_utc"))
    if (
        value.get("schema") != ATTEMPT_SCHEMA
        or not isinstance(binding, dict)
        or _validated_binding(binding) != binding
        or value.get("binding_sha256") != sha256_bytes(canonical_bytes(binding))
        or reserved_at is None
        or sofia_date(reserved_at) != binding.get("sofia_date")
        or value.get("attempts") != 1
        or value.get("network_send_authorized") is not True
        or value.get("secret_material_recorded") is not False
        or _SHA256.fullmatch(str(digest)) is None
        or digest != sha256_bytes(canonical_bytes(unsigned))
    ):
        raise DeliveryStateError("delivery_attempt_receipt_invalid")
    return dict(value)


def reserve_delivery_attempt(
    state_dir: Path,
    *,
    binding: Mapping[str, Any],
    reserved_at: datetime,
) -> tuple[dict[str, Any], bool]:
    binding_value = _validated_binding(binding)
    binding_date = binding_value.get("sofia_date")
    if _DATE.fullmatch(str(binding_date)) is None:
        raise DeliveryStateError("delivery_binding_invalid")
    if binding_date != sofia_date(reserved_at):
        raise DeliveryStateError("delivery_binding_date_mismatch")
    unsigned = {
        "schema": ATTEMPT_SCHEMA,
        "binding": binding_value,
        "binding_sha256": sha256_bytes(canonical_bytes(binding_value)),
        "reserved_at_utc": utc_timestamp(reserved_at),
        "attempts": 1,
        "network_send_authorized": True,
        "secret_material_recorded": False,
    }
    receipt = {
        **unsigned,
        "attempt_receipt_sha256": sha256_bytes(canonical_bytes(unsigned)),
    }
    name = f"attempt-{binding_date}.json"
    with _open_state_dir(state_dir) as state_fd:
        created = _create_receipt_at(state_fd, name, receipt)
        stored = _validate_attempt_receipt(_read_receipt_at(state_fd, name))
    if stored["binding"] != binding_value:
        raise DeliveryStateError("daily_delivery_binding_conflict")
    return stored, created


def _validated_outcome_result(result: Mapping[str, Any]) -> dict[str, Any]:
    status = result.get("status")
    blocker = result.get("blocker")
    message_id = result.get("message_id")
    if status not in {"PASS", "BLOCKED"}:
        raise DeliveryStateError("delivery_outcome_status_invalid")
    if blocker is not None and _CODE.fullmatch(str(blocker)) is None:
        raise DeliveryStateError("delivery_outcome_blocker_invalid")
    if message_id is not None and _MESSAGE_ID.fullmatch(str(message_id)) is None:
        raise DeliveryStateError("delivery_outcome_message_id_invalid")
    return {
        "status": status,
        "blocker": str(blocker) if blocker is not None else None,
        "message_id": str(message_id) if message_id is not None else None,
    }


def _validate_outcome_receipt(value: Mapping[str, Any]) -> dict[str, Any]:
    if set(value) != {
        "schema",
        "attempt_receipt_sha256",
        "binding_sha256",
        "sofia_date",
        "completed_at_utc",
        "result",
        "attempts",
        "secret_material_recorded",
        "outcome_receipt_sha256",
    }:
        raise DeliveryStateError("delivery_outcome_receipt_invalid")
    unsigned = dict(value)
    digest = unsigned.pop("outcome_receipt_sha256", None)
    if (
        value.get("schema") != OUTCOME_SCHEMA
        or _SHA256.fullmatch(str(value.get("attempt_receipt_sha256"))) is None
        or _SHA256.fullmatch(str(value.get("binding_sha256"))) is None
        or _DATE.fullmatch(str(value.get("sofia_date"))) is None
        or parse_timestamp(value.get("completed_at_utc")) is None
        or not isinstance(value.get("result"), dict)
        or _validated_outcome_result(value["result"]) != value["result"]
        or value.get("attempts") != 1
        or value.get("secret_material_recorded") is not False
        or _SHA256.fullmatch(str(digest)) is None
        or digest != sha256_bytes(canonical_bytes(unsigned))
    ):
        raise DeliveryStateError("delivery_outcome_receipt_invalid")
    return dict(value)


def _replace_latest_at(parent_fd: int, value: Mapping[str, Any]) -> None:
    temporary = f".latest.{os.getpid()}.{value['outcome_receipt_sha256'][:16]}.tmp"
    if not _create_receipt_at(parent_fd, temporary, value):
        existing = _read_receipt_at(parent_fd, temporary)
        if existing != dict(value):
            raise DeliveryStateError("delivery_latest_candidate_collision")
    os.replace(
        temporary,
        "latest.json",
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
    )
    os.fsync(parent_fd)
    if _read_receipt_at(parent_fd, "latest.json") != dict(value):
        raise DeliveryStateError("delivery_latest_receipt_changed")


def write_terminal_outcome(
    state_dir: Path,
    *,
    attempt: Mapping[str, Any],
    result: Mapping[str, Any],
    completed_at: datetime,
) -> dict[str, Any]:
    attempt_value = _validate_attempt_receipt(attempt)
    binding = attempt_value["binding"]
    result_value = _validated_outcome_result(result)
    unsigned = {
        "schema": OUTCOME_SCHEMA,
        "attempt_receipt_sha256": attempt_value["attempt_receipt_sha256"],
        "binding_sha256": attempt_value["binding_sha256"],
        "sofia_date": binding["sofia_date"],
        "completed_at_utc": utc_timestamp(completed_at),
        "result": result_value,
        "attempts": 1,
        "secret_material_recorded": False,
    }
    outcome = {
        **unsigned,
        "outcome_receipt_sha256": sha256_bytes(canonical_bytes(unsigned)),
    }
    name = f"outcome-{binding['sofia_date']}.json"
    with _open_state_dir(state_dir) as state_fd:
        created = _create_receipt_at(state_fd, name, outcome)
        stored = _validate_outcome_receipt(_read_receipt_at(state_fd, name))
        if not created and stored != outcome:
            raise DeliveryStateError("delivery_outcome_receipt_collision")
        if (
            stored["attempt_receipt_sha256"]
            != attempt_value["attempt_receipt_sha256"]
            or stored["binding_sha256"] != attempt_value["binding_sha256"]
            or stored["sofia_date"] != binding["sofia_date"]
        ):
            raise DeliveryStateError("delivery_outcome_binding_invalid")
        _replace_latest_at(state_fd, stored)
    return stored


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-report-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--channel-id", default=DEFAULT_CHANNEL_ID)
    parser.add_argument("--sender-python", type=Path)
    parser.add_argument("--sender-python-sha256")
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--window-hours", type=int, default=DEFAULT_WINDOW_HOURS)
    parser.add_argument("--now")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.window_hours <= 0:
        raise SystemExit("window hours must be positive")
    try:
        ZoneInfo(args.timezone)
    except Exception as exc:
        raise SystemExit("unknown timezone") from exc
    now = parse_timestamp(args.now) if args.now else datetime.now(timezone.utc)
    if now is None:
        raise SystemExit("invalid --now")
    try:
        reports = load_reports(
            args.public_report_dir.resolve(),
            now=now,
            window_hours=args.window_hours,
        )
        message = format_daily_report(
            reports,
            now=now,
            timezone_name=args.timezone,
            window_hours=args.window_hours,
        )
        encoded_message = message.encode("utf-8", errors="strict")
        binding = delivery_binding(
            now=now,
            channel_id=args.channel_id,
            window_hours=args.window_hours,
            report_count=len(reports),
            report_set_digest=report_set_sha256(reports),
            message_digest=sha256_bytes(encoded_message),
            message_bytes=len(encoded_message),
        )
        attempt, created = reserve_delivery_attempt(
            args.state_dir,
            binding=binding,
            reserved_at=now,
        )
    except (DeliveryStateError, OSError) as exc:
        print(
            json.dumps(
                {
                    "schema": DELIVERY_SCHEMA,
                    "status": "BLOCKED",
                    "blocker": str(exc),
                    "attempts": 0,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 1

    if not created:
        print(
            json.dumps(
                {
                    "schema": DELIVERY_SCHEMA,
                    "status": "SKIPPED",
                    "blocker": "daily_delivery_attempt_already_reserved",
                    "sofia_date": binding["sofia_date"],
                    "binding_sha256": attempt["binding_sha256"],
                    "attempt_receipt_sha256": attempt["attempt_receipt_sha256"],
                    "report_count": len(reports),
                    "attempts": 0,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 0

    result = deliver_once(
        message,
        channel_id=args.channel_id,
        sender_python=args.sender_python,
        sender_python_sha256=args.sender_python_sha256,
    )
    try:
        outcome = write_terminal_outcome(
            args.state_dir,
            attempt=attempt,
            result=result,
            completed_at=datetime.now(timezone.utc),
        )
    except (DeliveryStateError, OSError) as exc:
        print(
            json.dumps(
                {
                    "schema": DELIVERY_SCHEMA,
                    "status": "BLOCKED",
                    "blocker": str(exc),
                    "sofia_date": binding["sofia_date"],
                    "binding_sha256": attempt["binding_sha256"],
                    "attempt_receipt_sha256": attempt["attempt_receipt_sha256"],
                    "attempts": 1,
                },
                ensure_ascii=True,
                sort_keys=True,
            )
        )
        return 1
    print(
        json.dumps(
            {
                "schema": DELIVERY_SCHEMA,
                "status": result["status"],
                "blocker": result.get("blocker"),
                "message_id": result.get("message_id"),
                "sofia_date": binding["sofia_date"],
                "binding_sha256": attempt["binding_sha256"],
                "attempt_receipt_sha256": attempt["attempt_receipt_sha256"],
                "outcome_receipt_sha256": outcome["outcome_receipt_sha256"],
                "report_count": len(reports),
                "attempts": 1,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
