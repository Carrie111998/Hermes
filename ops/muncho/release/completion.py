"""Append-only production completion contract for Muncho releases.

Runtime code validates typed identity, integrity, and state transitions only.
Release-note and smoke text is rendered as opaque human-authored display data.
"""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .metadata import (
    ReleaseBundle,
    ReleaseMetadataError,
    SemVer,
    canonical_bytes,
    require_exact_release_sha,
    require_summary_text,
    sha256_bytes,
)


MAPPING_SCHEMA = "muncho-release-version-sha-mapping.v1"
SMOKE_SCHEMA = "muncho-production-release-smoke.v1"
DRAFT_SCHEMA = "muncho-production-release-summary-draft.v1"
DELIVERY_ATTEMPT_SCHEMA = "muncho-production-release-summary-attempt.v1"
DELIVERY_SCHEMA = "muncho-production-release-summary-delivery.v1"
COMPLETION_SCHEMA = "muncho-production-release-completion.v1"
STATUS_SCHEMA = "muncho-production-release-status.v1"
HEALTH_SCHEMA = "muncho-production-release-health.v1"

MAX_STATE_BYTES = 256 * 1024
MAX_SUMMARY_BYTES = 8 * 1024
MAX_CONFIG_BYTES = 2 * 1024 * 1024

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SNOWFLAKE = re.compile(r"^[1-9][0-9]{14,21}$")
_TASK_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_MESSAGE_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_TIMESTAMP = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$")


class ReleaseCompletionError(ReleaseMetadataError):
    """Stable fail-closed error at the release completion boundary."""


def utc_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        raise ReleaseCompletionError("muncho_release_time_invalid")
    return (
        current
        .astimezone(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _require_timestamp(value: Any, code: str) -> str:
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise ReleaseCompletionError(code)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseCompletionError(code) from exc
    return value


def _require_digest(value: Any, code: str, *, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReleaseCompletionError(code)
    return value


def _seal(schema: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    unsigned = {"schema": schema, **dict(payload)}
    return {**unsigned, "receipt_sha256": sha256_bytes(canonical_bytes(unsigned))}


def _unseal(
    value: Any,
    *,
    schema: str,
    fields: frozenset[str],
    code: str,
) -> dict[str, Any]:
    expected = fields | {"schema", "receipt_sha256"}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ReleaseCompletionError(code)
    raw = dict(value)
    receipt_sha = raw.pop("receipt_sha256")
    if (
        raw.get("schema") != schema
        or _require_digest(receipt_sha, code) != receipt_sha
        or sha256_bytes(canonical_bytes(raw)) != receipt_sha
    ):
        raise ReleaseCompletionError(code)
    return {**raw, "receipt_sha256": receipt_sha}


def release_idempotency_key(version: str, release_sha: str) -> str:
    return sha256_bytes(
        canonical_bytes({"muncho_version": version, "release_sha": release_sha})
    )


def _validate_identity(raw: Mapping[str, Any], code: str) -> tuple[str, str]:
    try:
        version = str(SemVer.parse(raw.get("muncho_version")))
        release_sha = require_exact_release_sha(raw.get("release_sha"))
    except ReleaseMetadataError as exc:
        raise ReleaseCompletionError(code) from exc
    if raw.get("release_idempotency_key") != release_idempotency_key(
        version, release_sha
    ):
        raise ReleaseCompletionError(code)
    return version, release_sha


def _file_identity(value: os.stat_result) -> tuple[int, ...]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_mode,
        value.st_uid,
        value.st_gid,
        value.st_nlink,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _ensure_state_dir(path: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ReleaseCompletionError("muncho_release_state_path_invalid")
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        reached = path.lstat()
    except OSError as exc:
        raise ReleaseCompletionError("muncho_release_state_unavailable") from exc
    if (
        not stat.S_ISDIR(reached.st_mode)
        or reached.st_uid != os.geteuid()
        or reached.st_gid != os.getegid()
        or stat.S_IMODE(reached.st_mode) != 0o700
    ):
        raise ReleaseCompletionError("muncho_release_state_directory_invalid")
    return path


def _read(path: Path, *, missing_ok: bool = False) -> dict[str, Any] | None:
    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ReleaseCompletionError("muncho_release_state_record_missing") from None
    except OSError as exc:
        raise ReleaseCompletionError("muncho_release_state_unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.geteuid()
        or before.st_gid != os.getegid()
        or stat.S_IMODE(before.st_mode) != 0o600
        or not 0 < before.st_size <= MAX_STATE_BYTES
    ):
        raise ReleaseCompletionError("muncho_release_state_record_invalid")
    try:
        raw = path.read_bytes()
        after = path.lstat()
        value = json.loads(raw.decode("ascii", errors="strict"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseCompletionError("muncho_release_state_record_invalid") from exc
    if (
        _file_identity(before) != _file_identity(after)
        or len(raw) != after.st_size
        or not isinstance(value, dict)
        or raw != canonical_bytes(value) + b"\n"
    ):
        raise ReleaseCompletionError("muncho_release_state_record_invalid")
    return value


def _create(path: Path, value: Mapping[str, Any]) -> bool:
    raw = canonical_bytes(dict(value)) + b"\n"
    if len(raw) > MAX_STATE_BYTES:
        raise ReleaseCompletionError("muncho_release_state_record_too_large")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        return False
    except OSError as exc:
        raise ReleaseCompletionError("muncho_release_state_unavailable") from exc
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(raw)
            stream.flush()
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    parent_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    return True


def _identity_suffix(version: str, release_sha: str) -> str:
    return f"v{version}-{release_idempotency_key(version, release_sha)[:20]}"


_MAPPING_FIELDS = frozenset({
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "metadata_present_at_source",
    "source_metadata_sha256",
    "source_history_sha256",
    "reserved_at_utc",
})


def validate_mapping_receipt(value: Any) -> dict[str, Any]:
    code = "muncho_release_mapping_invalid"
    raw = _unseal(value, schema=MAPPING_SCHEMA, fields=_MAPPING_FIELDS, code=code)
    _validate_identity(raw, code)
    metadata_sha = _require_digest(
        raw.get("source_metadata_sha256"), code, optional=True
    )
    if type(raw.get("metadata_present_at_source")) is not bool or raw[
        "metadata_present_at_source"
    ] != (metadata_sha is not None):
        raise ReleaseCompletionError(code)
    _require_digest(raw.get("source_history_sha256"), code)
    _require_timestamp(raw.get("reserved_at_utc"), code)
    return raw


def build_mapping_receipt(
    bundle: ReleaseBundle,
    *,
    version: str,
    release_sha: str,
    reserved_at: datetime | None = None,
) -> dict[str, Any]:
    version = str(SemVer.parse(version))
    release_sha = require_exact_release_sha(release_sha)
    history = bundle.history.by_version().get(version)
    if history is not None:
        if history.release_sha != release_sha:
            raise ReleaseCompletionError("muncho_release_version_reused")
        metadata_present, metadata_sha = history.metadata_present_at_source, None
    elif version == str(bundle.metadata.version):
        metadata_present, metadata_sha = True, bundle.metadata.metadata_sha256
    else:
        raise ReleaseCompletionError("muncho_release_version_unknown")
    return validate_mapping_receipt(
        _seal(
            MAPPING_SCHEMA,
            {
                "muncho_version": version,
                "release_sha": release_sha,
                "release_idempotency_key": release_idempotency_key(
                    version, release_sha
                ),
                "metadata_present_at_source": metadata_present,
                "source_metadata_sha256": metadata_sha,
                "source_history_sha256": bundle.history.history_sha256,
                "reserved_at_utc": utc_timestamp(reserved_at),
            },
        )
    )


def reserve_release_mapping(
    state_dir: Path,
    bundle: ReleaseBundle,
    *,
    version: str,
    release_sha: str,
    reserved_at: datetime | None = None,
) -> dict[str, Any]:
    state = _ensure_state_dir(state_dir)
    candidate = build_mapping_receipt(
        bundle, version=version, release_sha=release_sha, reserved_at=reserved_at
    )
    path = state / f"mapping-v{candidate['muncho_version']}.json"
    created = _create(path, candidate)
    stored = validate_mapping_receipt(_read(path))
    if stored["release_sha"] != candidate["release_sha"]:
        raise ReleaseCompletionError("muncho_release_version_reused")
    if created and stored != candidate:
        raise ReleaseCompletionError("muncho_release_mapping_changed")
    return stored


_SMOKE_FIELDS = frozenset({
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "mapping_receipt_sha256",
    "checks",
    "all_required_checks_passed",
    "completed_at_utc",
})


def validate_smoke_receipt(value: Any) -> dict[str, Any]:
    code = "muncho_release_smoke_invalid"
    raw = _unseal(value, schema=SMOKE_SCHEMA, fields=_SMOKE_FIELDS, code=code)
    _validate_identity(raw, code)
    _require_digest(raw.get("mapping_receipt_sha256"), code)
    checks = raw.get("checks")
    if not isinstance(checks, list) or not 1 <= len(checks) <= 12:
        raise ReleaseCompletionError(code)
    try:
        checked = [require_summary_text(item, code=code) for item in checks]
    except ReleaseMetadataError as exc:
        raise ReleaseCompletionError(code) from exc
    if checked != checks or len(checks) != len(set(checks)):
        raise ReleaseCompletionError(code)
    if raw.get("all_required_checks_passed") is not True:
        raise ReleaseCompletionError(code)
    _require_timestamp(raw.get("completed_at_utc"), code)
    return raw


def record_production_smoke(
    state_dir: Path,
    mapping: Mapping[str, Any],
    *,
    checks: Sequence[str],
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    state = _ensure_state_dir(state_dir)
    bound = validate_mapping_receipt(mapping)
    candidate = validate_smoke_receipt(
        _seal(
            SMOKE_SCHEMA,
            {
                "muncho_version": bound["muncho_version"],
                "release_sha": bound["release_sha"],
                "release_idempotency_key": bound["release_idempotency_key"],
                "mapping_receipt_sha256": bound["receipt_sha256"],
                "checks": list(checks),
                "all_required_checks_passed": True,
                "completed_at_utc": utc_timestamp(completed_at),
            },
        )
    )
    suffix = _identity_suffix(bound["muncho_version"], bound["release_sha"])
    path = state / f"smoke-{suffix}.json"
    created = _create(path, candidate)
    stored = validate_smoke_receipt(_read(path))
    if created and stored != candidate:
        raise ReleaseCompletionError("muncho_release_smoke_changed")
    if not created and (
        stored["mapping_receipt_sha256"] != candidate["mapping_receipt_sha256"]
        or stored["checks"] != candidate["checks"]
    ):
        raise ReleaseCompletionError("muncho_release_smoke_conflict")
    return stored


def resolve_discord_destination(config: Mapping[str, Any]) -> dict[str, str]:
    approvals = config.get("approvals") if isinstance(config, Mapping) else None
    value = (
        approvals.get("gateway_owner_escalation")
        if isinstance(approvals, Mapping)
        else None
    )
    if (
        not isinstance(value, Mapping)
        or value.get("enabled") is not True
        or value.get("owner_target_type") != "guild_channel"
        or _SNOWFLAKE.fullmatch(str(value.get("owner_guild_id", ""))) is None
        or _SNOWFLAKE.fullmatch(str(value.get("owner_channel_id", ""))) is None
    ):
        raise ReleaseCompletionError("muncho_release_destination_invalid")
    return {
        "platform": "discord",
        "guild_id": str(value["owner_guild_id"]),
        "channel_id": str(value["owner_channel_id"]),
        "target_type": "guild_channel",
        "config_source": "approvals.gateway_owner_escalation",
    }


def _validate_destination(value: Any) -> dict[str, str]:
    fields = {"platform", "guild_id", "channel_id", "target_type", "config_source"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ReleaseCompletionError("muncho_release_destination_invalid")
    destination = dict(value)
    if (
        destination.get("platform") != "discord"
        or destination.get("target_type") != "guild_channel"
        or destination.get("config_source") != "approvals.gateway_owner_escalation"
        or _SNOWFLAKE.fullmatch(str(destination.get("guild_id", ""))) is None
        or _SNOWFLAKE.fullmatch(str(destination.get("channel_id", ""))) is None
    ):
        raise ReleaseCompletionError("muncho_release_destination_invalid")
    return destination  # type: ignore[return-value]


def load_current_production_config(path: Path) -> dict[str, Any]:
    """Load the current typed config from an explicit path, never an env var."""

    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise ReleaseCompletionError("muncho_release_production_config_invalid")
    try:
        before = path.lstat()
        raw = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise ReleaseCompletionError(
            "muncho_release_production_config_unavailable"
        ) from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 < len(raw) <= MAX_CONFIG_BYTES
        or _file_identity(before) != _file_identity(after)
    ):
        raise ReleaseCompletionError("muncho_release_production_config_invalid")
    try:
        from gateway.production_model_sovereignty_runtime import (
            load_strict_production_config,
            validate_production_gateway_config,
        )

        config = load_strict_production_config(raw)
        validate_production_gateway_config(config)
        resolve_discord_destination(config)
    except Exception as exc:
        raise ReleaseCompletionError(
            "muncho_release_production_config_invalid"
        ) from exc
    return config


def render_release_summary(
    bundle: ReleaseBundle,
    *,
    release_sha: str,
    production_checks: Sequence[str],
) -> str:
    release_sha = require_exact_release_sha(release_sha)
    checks = [
        require_summary_text(item, code="muncho_release_summary_invalid")
        for item in production_checks
    ]
    if not 1 <= len(checks) <= 12 or len(checks) != len(set(checks)):
        raise ReleaseCompletionError("muncho_release_summary_invalid")
    notes = bundle.metadata.notes
    lines = [
        f"**Muncho v{bundle.metadata.version} — PROD release**",
        f"**Exact SHA:** `{release_sha}`",
        "**User-facing changes:**",
        *(f"- {item}" for item in notes.changes),
        "**Production checks / smokes:**",
        *(f"- ✅ {item}" for item in checks),
    ]
    if notes.known_limitations:
        lines.extend([
            "**Known limitations:**",
            *(f"- {x}" for x in notes.known_limitations),
        ])
    if notes.rollback_note is not None:
        lines.append(f"**Rollback:** {notes.rollback_note}")
    summary = "\n".join(lines)
    if len(summary.encode("utf-8", errors="strict")) > MAX_SUMMARY_BYTES:
        raise ReleaseCompletionError("muncho_release_summary_too_large")
    return summary


_DRAFT_FIELDS = frozenset({
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "mapping_receipt_sha256",
    "smoke_receipt_sha256",
    "source_metadata_sha256",
    "discord_destination",
    "summary",
    "summary_sha256",
    "created_at_utc",
})


def validate_summary_draft(value: Any) -> dict[str, Any]:
    code = "muncho_release_summary_draft_invalid"
    raw = _unseal(value, schema=DRAFT_SCHEMA, fields=_DRAFT_FIELDS, code=code)
    _validate_identity(raw, code)
    for name in (
        "mapping_receipt_sha256",
        "smoke_receipt_sha256",
        "source_metadata_sha256",
    ):
        _require_digest(raw.get(name), code)
    _validate_destination(raw.get("discord_destination"))
    summary = raw.get("summary")
    if (
        not isinstance(summary, str)
        or not 0 < len(summary.encode("utf-8", errors="strict")) <= MAX_SUMMARY_BYTES
        or raw.get("summary_sha256")
        != sha256_bytes(summary.encode("utf-8", errors="strict"))
    ):
        raise ReleaseCompletionError(code)
    _require_timestamp(raw.get("created_at_utc"), code)
    return raw


def prepare_summary_draft(
    state_dir: Path,
    bundle: ReleaseBundle,
    *,
    mapping: Mapping[str, Any],
    smoke: Mapping[str, Any],
    production_config: Mapping[str, Any],
    created_at: datetime | None = None,
) -> dict[str, Any]:
    state = _ensure_state_dir(state_dir)
    bound_mapping = validate_mapping_receipt(mapping)
    bound_smoke = validate_smoke_receipt(smoke)
    version = str(bundle.metadata.version)
    if (
        bound_mapping["muncho_version"] != version
        or bound_mapping["release_sha"] != bound_smoke["release_sha"]
        or bound_mapping["receipt_sha256"] != bound_smoke["mapping_receipt_sha256"]
        or bound_mapping["source_metadata_sha256"] != bundle.metadata.metadata_sha256
        or bound_mapping["metadata_present_at_source"] is not True
    ):
        raise ReleaseCompletionError("muncho_release_summary_binding_invalid")
    summary = render_release_summary(
        bundle,
        release_sha=bound_mapping["release_sha"],
        production_checks=bound_smoke["checks"],
    )
    candidate = validate_summary_draft(
        _seal(
            DRAFT_SCHEMA,
            {
                "muncho_version": version,
                "release_sha": bound_mapping["release_sha"],
                "release_idempotency_key": bound_mapping["release_idempotency_key"],
                "mapping_receipt_sha256": bound_mapping["receipt_sha256"],
                "smoke_receipt_sha256": bound_smoke["receipt_sha256"],
                "source_metadata_sha256": bundle.metadata.metadata_sha256,
                "discord_destination": resolve_discord_destination(production_config),
                "summary": summary,
                "summary_sha256": sha256_bytes(summary.encode("utf-8")),
                "created_at_utc": utc_timestamp(created_at),
            },
        )
    )
    suffix = _identity_suffix(version, bound_mapping["release_sha"])
    path = state / f"summary-draft-{suffix}.json"
    created = _create(path, candidate)
    stored = validate_summary_draft(_read(path))
    stable = _DRAFT_FIELDS - {"created_at_utc"}
    if created and stored != candidate:
        raise ReleaseCompletionError("muncho_release_summary_draft_changed")
    if not created and any(stored[name] != candidate[name] for name in stable):
        raise ReleaseCompletionError("muncho_release_summary_draft_conflict")
    return stored


_ATTEMPT_FIELDS = frozenset({
    "destination_kind",
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "draft_receipt_sha256",
    "summary_sha256",
    "destination_ref",
    "reserved_at_utc",
    "network_send_authorized",
})
_DELIVERY_FIELDS = frozenset({
    "destination_kind",
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "attempt_receipt_sha256",
    "draft_receipt_sha256",
    "summary_sha256",
    "destination_ref",
    "message_ref",
    "published_at_utc",
})


def _destination_ref(kind: str, value: Any) -> str:
    pattern = (
        _SNOWFLAKE if kind == "discord" else _TASK_ID if kind == "codex_task" else None
    )
    if (
        pattern is None
        or not isinstance(value, str)
        or pattern.fullmatch(value) is None
    ):
        raise ReleaseCompletionError("muncho_release_delivery_invalid")
    return value


def validate_delivery_attempt(value: Any) -> dict[str, Any]:
    code = "muncho_release_delivery_attempt_invalid"
    raw = _unseal(
        value, schema=DELIVERY_ATTEMPT_SCHEMA, fields=_ATTEMPT_FIELDS, code=code
    )
    _validate_identity(raw, code)
    _require_digest(raw.get("draft_receipt_sha256"), code)
    _require_digest(raw.get("summary_sha256"), code)
    _destination_ref(str(raw.get("destination_kind")), raw.get("destination_ref"))
    if raw.get("network_send_authorized") is not (
        raw.get("destination_kind") == "discord"
    ):
        raise ReleaseCompletionError(code)
    _require_timestamp(raw.get("reserved_at_utc"), code)
    return raw


def validate_delivery_receipt(value: Any) -> dict[str, Any]:
    code = "muncho_release_delivery_invalid"
    raw = _unseal(value, schema=DELIVERY_SCHEMA, fields=_DELIVERY_FIELDS, code=code)
    _validate_identity(raw, code)
    for name in (
        "attempt_receipt_sha256",
        "draft_receipt_sha256",
        "summary_sha256",
    ):
        _require_digest(raw.get(name), code)
    kind = str(raw.get("destination_kind"))
    _destination_ref(kind, raw.get("destination_ref"))
    message_pattern = _SNOWFLAKE if kind == "discord" else _MESSAGE_REF
    if (
        not isinstance(raw.get("message_ref"), str)
        or message_pattern.fullmatch(raw["message_ref"]) is None
    ):
        raise ReleaseCompletionError(code)
    _require_timestamp(raw.get("published_at_utc"), code)
    return raw


def _delivery_paths(
    state: Path, draft: Mapping[str, Any], kind: str
) -> tuple[Path, Path]:
    suffix = _identity_suffix(draft["muncho_version"], draft["release_sha"])
    return (
        state / f"summary-{kind}-attempt-{suffix}.json",
        state / f"summary-{kind}-delivery-{suffix}.json",
    )


def reserve_summary_delivery(
    state_dir: Path,
    draft: Mapping[str, Any],
    *,
    kind: str,
    destination_ref: str,
    reserved_at: datetime | None = None,
) -> tuple[dict[str, Any], bool]:
    state = _ensure_state_dir(state_dir)
    bound = validate_summary_draft(draft)
    destination_ref = _destination_ref(kind, destination_ref)
    attempt_path, delivery_path = _delivery_paths(state, bound, kind)
    delivered = _read(delivery_path, missing_ok=True)
    if delivered is not None:
        receipt = validate_delivery_receipt(delivered)
        if (
            receipt["destination_ref"] != destination_ref
            or receipt["draft_receipt_sha256"] != bound["receipt_sha256"]
        ):
            raise ReleaseCompletionError("muncho_release_delivery_conflict")
        return receipt, False
    candidate = validate_delivery_attempt(
        _seal(
            DELIVERY_ATTEMPT_SCHEMA,
            {
                "destination_kind": kind,
                "muncho_version": bound["muncho_version"],
                "release_sha": bound["release_sha"],
                "release_idempotency_key": bound["release_idempotency_key"],
                "draft_receipt_sha256": bound["receipt_sha256"],
                "summary_sha256": bound["summary_sha256"],
                "destination_ref": destination_ref,
                "reserved_at_utc": utc_timestamp(reserved_at),
                "network_send_authorized": kind == "discord",
            },
        )
    )
    created = _create(attempt_path, candidate)
    stored = validate_delivery_attempt(_read(attempt_path))
    if (
        stored["destination_ref"] != destination_ref
        or stored["draft_receipt_sha256"] != bound["receipt_sha256"]
    ):
        raise ReleaseCompletionError("muncho_release_delivery_conflict")
    return stored, created


def record_reserved_summary_delivery(
    state_dir: Path,
    draft: Mapping[str, Any],
    attempt: Mapping[str, Any],
    *,
    message_ref: str,
    published_at: datetime | None = None,
) -> dict[str, Any]:
    state = _ensure_state_dir(state_dir)
    bound = validate_summary_draft(draft)
    reserved = validate_delivery_attempt(attempt)
    kind = reserved["destination_kind"]
    attempt_path, delivery_path = _delivery_paths(state, bound, kind)
    if (
        reserved["draft_receipt_sha256"] != bound["receipt_sha256"]
        or reserved["summary_sha256"] != bound["summary_sha256"]
        or validate_delivery_attempt(_read(attempt_path)) != reserved
    ):
        raise ReleaseCompletionError("muncho_release_delivery_binding_invalid")
    candidate = validate_delivery_receipt(
        _seal(
            DELIVERY_SCHEMA,
            {
                "destination_kind": kind,
                "muncho_version": bound["muncho_version"],
                "release_sha": bound["release_sha"],
                "release_idempotency_key": bound["release_idempotency_key"],
                "attempt_receipt_sha256": reserved["receipt_sha256"],
                "draft_receipt_sha256": bound["receipt_sha256"],
                "summary_sha256": bound["summary_sha256"],
                "destination_ref": reserved["destination_ref"],
                "message_ref": str(message_ref),
                "published_at_utc": utc_timestamp(published_at),
            },
        )
    )
    created = _create(delivery_path, candidate)
    stored = validate_delivery_receipt(_read(delivery_path))
    stable = _DELIVERY_FIELDS - {"published_at_utc"}
    if created and stored != candidate:
        raise ReleaseCompletionError("muncho_release_delivery_changed")
    if not created and any(stored[name] != candidate[name] for name in stable):
        raise ReleaseCompletionError("muncho_release_delivery_conflict")
    return stored


DiscordSender = Callable[[str, str], Mapping[str, Any]]


def deliver_discord_once(
    state_dir: Path,
    draft: Mapping[str, Any],
    *,
    sender: DiscordSender,
    reserved_at: datetime | None = None,
    published_at: datetime | None = None,
) -> dict[str, Any]:
    bound = validate_summary_draft(draft)
    channel_id = str(bound["discord_destination"]["channel_id"])
    attempt, created = reserve_summary_delivery(
        state_dir,
        bound,
        kind="discord",
        destination_ref=channel_id,
        reserved_at=reserved_at,
    )
    if attempt.get("schema") == DELIVERY_SCHEMA:
        return validate_delivery_receipt(attempt)
    if not created:
        raise ReleaseCompletionError(
            "muncho_release_discord_delivery_reconciliation_required"
        )
    try:
        result = sender(bound["summary"], channel_id)
    except Exception as exc:
        raise ReleaseCompletionError(
            "muncho_release_discord_delivery_uncertain"
        ) from exc
    if not isinstance(result, Mapping) or result.get("success") is not True:
        raise ReleaseCompletionError("muncho_release_discord_delivery_failed")
    return record_reserved_summary_delivery(
        state_dir,
        bound,
        attempt,
        message_ref=str(result.get("message_id", "")),
        published_at=published_at,
    )


def hermes_send_discord(message: str, channel_id: str) -> Mapping[str, Any]:
    """Send one already-reserved summary through Hermes's existing sender."""

    if _SNOWFLAKE.fullmatch(channel_id) is None:
        raise ReleaseCompletionError("muncho_release_destination_invalid")
    try:
        completed = subprocess.run(
            (
                sys.executable,
                "-m",
                "hermes_cli.main",
                "send",
                "--to",
                f"discord:{channel_id}",
                "--json",
            ),
            input=message,
            capture_output=True,
            text=True,
            check=False,
            timeout=90,
        )
        result = json.loads(completed.stdout or "null")
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
        raise ReleaseCompletionError(
            "muncho_release_discord_delivery_uncertain"
        ) from exc
    if completed.returncode != 0 or not isinstance(result, Mapping):
        raise ReleaseCompletionError("muncho_release_discord_delivery_failed")
    return {
        "success": result.get("success") is True or result.get("ok") is True,
        "message_id": str(result.get("message_id", "")),
    }


_COMPLETION_FIELDS = frozenset({
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "mapping_receipt_sha256",
    "smoke_receipt_sha256",
    "draft_receipt_sha256",
    "summary_sha256",
    "codex_task_delivery_receipt_sha256",
    "discord_delivery_receipt_sha256",
    "production_smoke_passed",
    "required_summaries_published",
    "completed_at_utc",
})


def validate_completion_receipt(value: Any) -> dict[str, Any]:
    code = "muncho_release_completion_invalid"
    raw = _unseal(value, schema=COMPLETION_SCHEMA, fields=_COMPLETION_FIELDS, code=code)
    _validate_identity(raw, code)
    for name in _COMPLETION_FIELDS:
        if name.endswith("_sha256"):
            _require_digest(raw.get(name), code)
    if (
        raw.get("production_smoke_passed") is not True
        or raw.get("required_summaries_published") is not True
    ):
        raise ReleaseCompletionError(code)
    _require_timestamp(raw.get("completed_at_utc"), code)
    return raw


def finalize_release_completion(
    state_dir: Path,
    *,
    mapping: Mapping[str, Any],
    smoke: Mapping[str, Any],
    draft: Mapping[str, Any],
    codex_delivery: Mapping[str, Any],
    discord_delivery: Mapping[str, Any],
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    state = _ensure_state_dir(state_dir)
    bound_mapping = validate_mapping_receipt(mapping)
    bound_smoke = validate_smoke_receipt(smoke)
    bound_draft = validate_summary_draft(draft)
    deliveries = {
        item["destination_kind"]: item
        for item in (
            validate_delivery_receipt(codex_delivery),
            validate_delivery_receipt(discord_delivery),
        )
    }
    if set(deliveries) != {"codex_task", "discord"} or (
        bound_mapping["receipt_sha256"] != bound_smoke["mapping_receipt_sha256"]
        or bound_smoke["receipt_sha256"] != bound_draft["smoke_receipt_sha256"]
        or any(
            item["draft_receipt_sha256"] != bound_draft["receipt_sha256"]
            or item["summary_sha256"] != bound_draft["summary_sha256"]
            for item in deliveries.values()
        )
    ):
        raise ReleaseCompletionError("muncho_release_completion_binding_invalid")
    candidate = validate_completion_receipt(
        _seal(
            COMPLETION_SCHEMA,
            {
                "muncho_version": bound_mapping["muncho_version"],
                "release_sha": bound_mapping["release_sha"],
                "release_idempotency_key": bound_mapping["release_idempotency_key"],
                "mapping_receipt_sha256": bound_mapping["receipt_sha256"],
                "smoke_receipt_sha256": bound_smoke["receipt_sha256"],
                "draft_receipt_sha256": bound_draft["receipt_sha256"],
                "summary_sha256": bound_draft["summary_sha256"],
                "codex_task_delivery_receipt_sha256": deliveries["codex_task"][
                    "receipt_sha256"
                ],
                "discord_delivery_receipt_sha256": deliveries["discord"][
                    "receipt_sha256"
                ],
                "production_smoke_passed": True,
                "required_summaries_published": True,
                "completed_at_utc": utc_timestamp(completed_at),
            },
        )
    )
    suffix = _identity_suffix(
        bound_mapping["muncho_version"], bound_mapping["release_sha"]
    )
    path = state / f"completion-{suffix}.json"
    created = _create(path, candidate)
    stored = validate_completion_receipt(_read(path))
    stable = _COMPLETION_FIELDS - {"completed_at_utc"}
    if created and stored != candidate:
        raise ReleaseCompletionError("muncho_release_completion_changed")
    if not created and any(stored[name] != candidate[name] for name in stable):
        raise ReleaseCompletionError("muncho_release_completion_conflict")
    return stored


def release_status(
    state_dir: Path,
    *,
    version: str,
    release_sha: str,
) -> dict[str, Any]:
    state = _ensure_state_dir(state_dir)
    version = str(SemVer.parse(version))
    release_sha = require_exact_release_sha(release_sha)
    suffix = _identity_suffix(version, release_sha)
    specs = {
        "mapping": (state / f"mapping-v{version}.json", validate_mapping_receipt),
        "smoke": (state / f"smoke-{suffix}.json", validate_smoke_receipt),
        "draft": (state / f"summary-draft-{suffix}.json", validate_summary_draft),
        "codex": (
            state / f"summary-codex_task-delivery-{suffix}.json",
            validate_delivery_receipt,
        ),
        "discord": (
            state / f"summary-discord-delivery-{suffix}.json",
            validate_delivery_receipt,
        ),
        "completion": (
            state / f"completion-{suffix}.json",
            validate_completion_receipt,
        ),
    }
    records = {}
    for name, (path, validator) in specs.items():
        value = _read(path, missing_ok=True)
        records[name] = validator(value) if value is not None else None
    if (
        records["mapping"] is not None
        and records["mapping"]["release_sha"] != release_sha
    ):
        raise ReleaseCompletionError("muncho_release_version_reused")
    if records["completion"]:
        phase = "complete"
    elif records["draft"]:
        phase = "summary_delivery_pending"
    elif records["smoke"]:
        phase = "summary_draft_pending"
    elif records["mapping"]:
        phase = "production_smoke_pending"
    else:
        phase = "unreserved"
    return {
        "schema": STATUS_SCHEMA,
        "muncho_version": version,
        "release_sha": release_sha,
        "release_sha_short": release_sha[:8],
        "phase": phase,
        "version_sha_reserved": records["mapping"] is not None,
        "production_smoke_passed": records["smoke"] is not None,
        "summary_rendered": records["draft"] is not None,
        "codex_task_summary_published": records["codex"] is not None,
        "discord_summary_published": records["discord"] is not None,
        "complete": records["completion"] is not None,
        "completion_receipt_sha256": (
            records["completion"]["receipt_sha256"] if records["completion"] else None
        ),
    }


def release_health(
    state_dir: Path,
    *,
    version: str,
    release_sha: str,
) -> dict[str, Any]:
    status = release_status(state_dir, version=version, release_sha=release_sha)
    return {
        "schema": HEALTH_SCHEMA,
        "muncho_version": status["muncho_version"],
        "release_sha": status["release_sha"],
        "release_sha_short": status["release_sha_short"],
        "healthy": status["complete"],
        "production_smoke_passed": status["production_smoke_passed"],
        "required_summaries_published": status["codex_task_summary_published"]
        and status["discord_summary_published"],
        "completion_receipt_sha256": status["completion_receipt_sha256"],
    }


__all__ = [
    "ReleaseCompletionError",
    "build_mapping_receipt",
    "deliver_discord_once",
    "finalize_release_completion",
    "hermes_send_discord",
    "load_current_production_config",
    "prepare_summary_draft",
    "record_production_smoke",
    "record_reserved_summary_delivery",
    "release_health",
    "release_status",
    "render_release_summary",
    "reserve_release_mapping",
    "reserve_summary_delivery",
    "resolve_discord_destination",
]
