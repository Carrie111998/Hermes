"""Append-only production completion contract for Muncho releases.

Runtime code validates typed identity, integrity, and state transitions only.
Release-note and smoke text is rendered as opaque human-authored display data.
"""

from __future__ import annotations

import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

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
RESTART_ATTEMPT_SCHEMA = "muncho-production-release-restart-attempt.v1"
RESTART_ATTESTATION_SCHEMA = "muncho-production-release-restart-attestation.v1"
SMOKE_SCHEMA = "muncho-production-release-smoke.v1"
DRAFT_SCHEMA = "muncho-production-release-summary-draft.v1"
DELIVERY_ATTEMPT_SCHEMA = "muncho-production-release-summary-attempt.v1"
DELIVERY_SCHEMA = "muncho-production-release-summary-delivery.v1"
GATEWAY_DISCORD_REQUEST_SCHEMA = (
    "muncho-production-release-gateway-discord-request.v1"
)
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
_SYSTEMD_SERVICE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{1,126}\.service$")
_SYSTEMD_INVOCATION_ID = re.compile(r"^[0-9a-f]{32}$")
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


_RESTART_ATTEMPT_FIELDS = frozenset({
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "mapping_receipt_sha256",
    "service_name",
    "before_invocation_id",
    "planned_stop_marker_prepared",
    "prepared_at_utc",
})
_RESTART_ATTESTATION_FIELDS = frozenset({
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "mapping_receipt_sha256",
    "restart_attempt_receipt_sha256",
    "service_name",
    "before_invocation_id",
    "after_invocation_id",
    "planned_stop_marker_consumed",
    "exact_deployed_identity_confirmed",
    "production_health_smoke_passed",
    "attested_at_utc",
})


def _require_systemd_service(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SYSTEMD_SERVICE.fullmatch(value) is None:
        raise ReleaseCompletionError(code)
    return value


def _require_invocation_id(value: Any, code: str) -> str:
    if not isinstance(value, str) or _SYSTEMD_INVOCATION_ID.fullmatch(value) is None:
        raise ReleaseCompletionError(code)
    return value


def validate_restart_attempt(value: Any) -> dict[str, Any]:
    code = "muncho_release_restart_attempt_invalid"
    raw = _unseal(
        value,
        schema=RESTART_ATTEMPT_SCHEMA,
        fields=_RESTART_ATTEMPT_FIELDS,
        code=code,
    )
    _validate_identity(raw, code)
    _require_digest(raw.get("mapping_receipt_sha256"), code)
    _require_systemd_service(raw.get("service_name"), code)
    _require_invocation_id(raw.get("before_invocation_id"), code)
    if raw.get("planned_stop_marker_prepared") is not True:
        raise ReleaseCompletionError(code)
    _require_timestamp(raw.get("prepared_at_utc"), code)
    return raw


def validate_restart_attestation(value: Any) -> dict[str, Any]:
    code = "muncho_release_restart_attestation_invalid"
    raw = _unseal(
        value,
        schema=RESTART_ATTESTATION_SCHEMA,
        fields=_RESTART_ATTESTATION_FIELDS,
        code=code,
    )
    _validate_identity(raw, code)
    _require_digest(raw.get("mapping_receipt_sha256"), code)
    _require_digest(raw.get("restart_attempt_receipt_sha256"), code)
    _require_systemd_service(raw.get("service_name"), code)
    before = _require_invocation_id(raw.get("before_invocation_id"), code)
    after = _require_invocation_id(raw.get("after_invocation_id"), code)
    if before == after or any(
        raw.get(name) is not True
        for name in (
            "planned_stop_marker_consumed",
            "exact_deployed_identity_confirmed",
            "production_health_smoke_passed",
        )
    ):
        raise ReleaseCompletionError(code)
    _require_timestamp(raw.get("attested_at_utc"), code)
    return raw


def _restart_paths(
    state: Path,
    *,
    version: str,
    release_sha: str,
) -> tuple[Path, Path]:
    suffix = _identity_suffix(version, release_sha)
    return (
        state / f"restart-attempt-{suffix}.json",
        state / f"restart-attestation-{suffix}.json",
    )


def prepare_restart_attestation(
    state_dir: Path,
    mapping: Mapping[str, Any],
    *,
    service_name: str,
    before_invocation_id: str,
    prepared_at: datetime | None = None,
) -> dict[str, Any]:
    """Persist pre-restart identity before the systemd mutation occurs."""

    state = _ensure_state_dir(state_dir)
    bound = validate_mapping_receipt(mapping)
    service_name = _require_systemd_service(
        service_name,
        "muncho_release_restart_attempt_invalid",
    )
    before_invocation_id = _require_invocation_id(
        before_invocation_id,
        "muncho_release_restart_attempt_invalid",
    )
    candidate = validate_restart_attempt(
        _seal(
            RESTART_ATTEMPT_SCHEMA,
            {
                "muncho_version": bound["muncho_version"],
                "release_sha": bound["release_sha"],
                "release_idempotency_key": bound["release_idempotency_key"],
                "mapping_receipt_sha256": bound["receipt_sha256"],
                "service_name": service_name,
                "before_invocation_id": before_invocation_id,
                "planned_stop_marker_prepared": True,
                "prepared_at_utc": utc_timestamp(prepared_at),
            },
        )
    )
    attempt_path, _attestation_path = _restart_paths(
        state,
        version=bound["muncho_version"],
        release_sha=bound["release_sha"],
    )
    created = _create(attempt_path, candidate)
    stored = validate_restart_attempt(_read(attempt_path))
    stable = _RESTART_ATTEMPT_FIELDS - {"prepared_at_utc"}
    if created and stored != candidate:
        raise ReleaseCompletionError("muncho_release_restart_attempt_changed")
    if not created and any(stored[name] != candidate[name] for name in stable):
        raise ReleaseCompletionError("muncho_release_restart_attempt_conflict")
    return stored


def complete_restart_attestation(
    state_dir: Path,
    mapping: Mapping[str, Any],
    *,
    service_name: str,
    after_invocation_id: str,
    attested_at: datetime | None = None,
) -> dict[str, Any]:
    """Record post-restart proof, or replay an existing immutable proof."""

    state = _ensure_state_dir(state_dir)
    bound = validate_mapping_receipt(mapping)
    service_name = _require_systemd_service(
        service_name,
        "muncho_release_restart_attestation_invalid",
    )
    after_invocation_id = _require_invocation_id(
        after_invocation_id,
        "muncho_release_restart_attestation_invalid",
    )
    attempt_path, attestation_path = _restart_paths(
        state,
        version=bound["muncho_version"],
        release_sha=bound["release_sha"],
    )
    existing = _read(attestation_path, missing_ok=True)
    if existing is not None:
        stored = validate_restart_attestation(existing)
        if (
            stored["mapping_receipt_sha256"] != bound["receipt_sha256"]
            or stored["service_name"] != service_name
            or stored["after_invocation_id"] != after_invocation_id
        ):
            raise ReleaseCompletionError(
                "muncho_release_restart_attestation_conflict"
            )
        return stored
    attempt = validate_restart_attempt(_read(attempt_path))
    if (
        attempt["mapping_receipt_sha256"] != bound["receipt_sha256"]
        or attempt["service_name"] != service_name
        or attempt["before_invocation_id"] == after_invocation_id
    ):
        raise ReleaseCompletionError("muncho_release_restart_attestation_invalid")
    candidate = validate_restart_attestation(
        _seal(
            RESTART_ATTESTATION_SCHEMA,
            {
                "muncho_version": bound["muncho_version"],
                "release_sha": bound["release_sha"],
                "release_idempotency_key": bound["release_idempotency_key"],
                "mapping_receipt_sha256": bound["receipt_sha256"],
                "restart_attempt_receipt_sha256": attempt["receipt_sha256"],
                "service_name": service_name,
                "before_invocation_id": attempt["before_invocation_id"],
                "after_invocation_id": after_invocation_id,
                "planned_stop_marker_consumed": True,
                "exact_deployed_identity_confirmed": True,
                "production_health_smoke_passed": True,
                "attested_at_utc": utc_timestamp(attested_at),
            },
        )
    )
    created = _create(attestation_path, candidate)
    stored = validate_restart_attestation(_read(attestation_path))
    stable = _RESTART_ATTESTATION_FIELDS - {"attested_at_utc"}
    if created and stored != candidate:
        raise ReleaseCompletionError("muncho_release_restart_attestation_changed")
    if not created and any(stored[name] != candidate[name] for name in stable):
        raise ReleaseCompletionError("muncho_release_restart_attestation_conflict")
    return stored


def require_restart_attestation(
    state_dir: Path,
    mapping: Mapping[str, Any],
) -> dict[str, Any]:
    """Load one complete restart chain bound to the release mapping."""

    state = _ensure_state_dir(state_dir)
    bound = validate_mapping_receipt(mapping)
    attempt_path, attestation_path = _restart_paths(
        state,
        version=bound["muncho_version"],
        release_sha=bound["release_sha"],
    )
    try:
        attempt = validate_restart_attempt(_read(attempt_path))
        attestation = validate_restart_attestation(_read(attestation_path))
    except ReleaseCompletionError as exc:
        if str(exc) == "muncho_release_state_record_missing":
            raise ReleaseCompletionError(
                "muncho_release_restart_attestation_required"
            ) from exc
        raise
    if (
        attempt["mapping_receipt_sha256"] != bound["receipt_sha256"]
        or attestation["mapping_receipt_sha256"] != bound["receipt_sha256"]
        or attestation["restart_attempt_receipt_sha256"]
        != attempt["receipt_sha256"]
        or attestation["service_name"] != attempt["service_name"]
        or attestation["before_invocation_id"]
        != attempt["before_invocation_id"]
    ):
        raise ReleaseCompletionError("muncho_release_restart_attestation_invalid")
    return attestation


_SMOKE_FIELDS = frozenset({
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "mapping_receipt_sha256",
    "restart_attestation_receipt_sha256",
    "checks",
    "all_required_checks_passed",
    "completed_at_utc",
})


def validate_smoke_receipt(value: Any) -> dict[str, Any]:
    code = "muncho_release_smoke_invalid"
    raw = _unseal(value, schema=SMOKE_SCHEMA, fields=_SMOKE_FIELDS, code=code)
    _validate_identity(raw, code)
    _require_digest(raw.get("mapping_receipt_sha256"), code)
    _require_digest(raw.get("restart_attestation_receipt_sha256"), code)
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
    restart_attestation: Mapping[str, Any],
    *,
    checks: Sequence[str],
    completed_at: datetime | None = None,
) -> dict[str, Any]:
    state = _ensure_state_dir(state_dir)
    bound = validate_mapping_receipt(mapping)
    restart = validate_restart_attestation(restart_attestation)
    _attempt_path, restart_path = _restart_paths(
        state,
        version=bound["muncho_version"],
        release_sha=bound["release_sha"],
    )
    if (
        restart["mapping_receipt_sha256"] != bound["receipt_sha256"]
        or validate_restart_attestation(_read(restart_path)) != restart
    ):
        raise ReleaseCompletionError("muncho_release_smoke_binding_invalid")
    candidate = validate_smoke_receipt(
        _seal(
            SMOKE_SCHEMA,
            {
                "muncho_version": bound["muncho_version"],
                "release_sha": bound["release_sha"],
                "release_idempotency_key": bound["release_idempotency_key"],
                "mapping_receipt_sha256": bound["receipt_sha256"],
                "restart_attestation_receipt_sha256": restart["receipt_sha256"],
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
        or stored["restart_attestation_receipt_sha256"]
        != candidate["restart_attestation_receipt_sha256"]
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
    if (
        len(summary) > 2_000
        or len(summary.encode("utf-8", errors="strict")) > MAX_SUMMARY_BYTES
    ):
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
        or len(summary) > 2_000
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
    if kind == "discord":
        request = validate_gateway_discord_request(
            _read(
                _gateway_discord_request_path(
                    state,
                    version=bound["muncho_version"],
                    release_sha=bound["release_sha"],
                )
            )
        )
        if (
            request["attempt_receipt_sha256"] != reserved["receipt_sha256"]
            or request["draft_receipt_sha256"] != bound["receipt_sha256"]
            or request["summary_sha256"] != bound["summary_sha256"]
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


_GATEWAY_DISCORD_REQUEST_FIELDS = frozenset({
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "attempt_receipt_sha256",
    "draft_receipt_sha256",
    "restart_attestation_receipt_sha256",
    "smoke_receipt_sha256",
    "summary_sha256",
    "summary",
    "guild_id",
    "channel_id",
    "target_type",
    "after_invocation_id",
    "queued_at_utc",
})


def validate_gateway_discord_request(value: Any) -> dict[str, Any]:
    code = "muncho_release_gateway_discord_request_invalid"
    raw = _unseal(
        value,
        schema=GATEWAY_DISCORD_REQUEST_SCHEMA,
        fields=_GATEWAY_DISCORD_REQUEST_FIELDS,
        code=code,
    )
    _validate_identity(raw, code)
    for name in (
        "attempt_receipt_sha256",
        "draft_receipt_sha256",
        "restart_attestation_receipt_sha256",
        "smoke_receipt_sha256",
        "summary_sha256",
    ):
        _require_digest(raw.get(name), code)
    summary = raw.get("summary")
    if (
        not isinstance(summary, str)
        or not summary
        or len(summary) > 2_000
        or len(summary.encode("utf-8")) > MAX_SUMMARY_BYTES
        or sha256_bytes(summary.encode("utf-8")) != raw["summary_sha256"]
    ):
        raise ReleaseCompletionError(code)
    if (
        _SNOWFLAKE.fullmatch(str(raw.get("guild_id", ""))) is None
        or _SNOWFLAKE.fullmatch(str(raw.get("channel_id", ""))) is None
        or raw.get("target_type") != "guild_channel"
    ):
        raise ReleaseCompletionError(code)
    _require_invocation_id(raw.get("after_invocation_id"), code)
    _require_timestamp(raw.get("queued_at_utc"), code)
    return raw


def _gateway_discord_request_binds(
    request: Mapping[str, Any],
    *,
    restart: Mapping[str, Any],
    smoke: Mapping[str, Any],
    draft: Mapping[str, Any],
    discord_attempt: Mapping[str, Any],
) -> bool:
    """Return whether one validated request belongs to the full release chain."""

    destination = draft["discord_destination"]
    identity = (
        draft["muncho_version"],
        draft["release_sha"],
        draft["release_idempotency_key"],
    )
    return (
        all(
            (
                item["muncho_version"],
                item["release_sha"],
                item["release_idempotency_key"],
            )
            == identity
            for item in (request, restart, smoke, draft, discord_attempt)
        )
        and request["attempt_receipt_sha256"]
        == discord_attempt["receipt_sha256"]
        and request["draft_receipt_sha256"] == draft["receipt_sha256"]
        and request["restart_attestation_receipt_sha256"]
        == restart["receipt_sha256"]
        and request["smoke_receipt_sha256"] == smoke["receipt_sha256"]
        and request["summary_sha256"] == draft["summary_sha256"]
        and request["summary"] == draft["summary"]
        and request["guild_id"] == destination["guild_id"]
        and request["channel_id"] == destination["channel_id"]
        and request["target_type"] == destination["target_type"]
        and request["after_invocation_id"] == restart["after_invocation_id"]
    )


def _gateway_discord_request_path(
    state: Path,
    *,
    version: str,
    release_sha: str,
) -> Path:
    suffix = _identity_suffix(version, release_sha)
    return state / f"gateway-discord-request-{suffix}.json"


def queue_gateway_discord_delivery(
    state_dir: Path,
    draft: Mapping[str, Any],
    attempt: Mapping[str, Any],
    *,
    queued_at: datetime | None = None,
) -> dict[str, Any]:
    """Queue one exact release summary for the live gateway relay edge.

    The gateway process already owns the authenticated Discord connector
    transport.  Keeping network dispatch there preserves the privileged
    writer/connector boundary; the release coordinator never receives the bot
    token and never falls back to a raw REST POST in production.
    """

    state = _ensure_state_dir(state_dir)
    bound = validate_summary_draft(draft)
    reserved = validate_delivery_attempt(attempt)
    attempt_path, _delivery_path = _delivery_paths(state, bound, "discord")
    destination = bound["discord_destination"]
    suffix = _identity_suffix(bound["muncho_version"], bound["release_sha"])
    smoke = validate_smoke_receipt(_read(state / f"smoke-{suffix}.json"))
    restart = validate_restart_attestation(
        _read(state / f"restart-attestation-{suffix}.json")
    )
    if (
        reserved["destination_kind"] != "discord"
        or reserved["destination_ref"] != destination["channel_id"]
        or reserved["draft_receipt_sha256"] != bound["receipt_sha256"]
        or reserved["summary_sha256"] != bound["summary_sha256"]
        or validate_delivery_attempt(_read(attempt_path)) != reserved
        or smoke["receipt_sha256"] != bound["smoke_receipt_sha256"]
        or smoke["restart_attestation_receipt_sha256"]
        != restart["receipt_sha256"]
    ):
        raise ReleaseCompletionError(
            "muncho_release_gateway_discord_request_binding_invalid"
        )
    candidate = validate_gateway_discord_request(
        _seal(
            GATEWAY_DISCORD_REQUEST_SCHEMA,
            {
                "muncho_version": bound["muncho_version"],
                "release_sha": bound["release_sha"],
                "release_idempotency_key": bound["release_idempotency_key"],
                "attempt_receipt_sha256": reserved["receipt_sha256"],
                "draft_receipt_sha256": bound["receipt_sha256"],
                "restart_attestation_receipt_sha256": restart["receipt_sha256"],
                "smoke_receipt_sha256": bound["smoke_receipt_sha256"],
                "summary_sha256": bound["summary_sha256"],
                "summary": bound["summary"],
                "guild_id": destination["guild_id"],
                "channel_id": destination["channel_id"],
                "target_type": destination["target_type"],
                "after_invocation_id": restart["after_invocation_id"],
                "queued_at_utc": utc_timestamp(queued_at),
            },
        )
    )
    path = _gateway_discord_request_path(
        state,
        version=bound["muncho_version"],
        release_sha=bound["release_sha"],
    )
    created = _create(path, candidate)
    stored = validate_gateway_discord_request(_read(path))
    stable = _GATEWAY_DISCORD_REQUEST_FIELDS - {"queued_at_utc"}
    if created and stored != candidate:
        raise ReleaseCompletionError("muncho_release_gateway_discord_request_changed")
    if not created and any(stored[name] != candidate[name] for name in stable):
        raise ReleaseCompletionError("muncho_release_gateway_discord_request_conflict")
    return stored


def pending_gateway_discord_deliveries(state_dir: Path) -> tuple[dict[str, Any], ...]:
    """Return validated gateway requests that have no durable delivery receipt."""

    state = _ensure_state_dir(state_dir)
    pending: list[dict[str, Any]] = []
    for path in sorted(state.glob("gateway-discord-request-*.json")):
        request = validate_gateway_discord_request(_read(path))
        status = release_status(
            state,
            version=request["muncho_version"],
            release_sha=request["release_sha"],
        )
        if (
            status["qualifying_restart_attested"] is not True
            or status["production_smoke_passed"] is not True
            or status["summary_rendered"] is not True
        ):
            raise ReleaseCompletionError(
                "muncho_release_gateway_discord_request_binding_invalid"
            )
        suffix = _identity_suffix(request["muncho_version"], request["release_sha"])
        delivered = _read(
            state / f"summary-discord-delivery-{suffix}.json",
            missing_ok=True,
        )
        if delivered is None:
            pending.append(request)
        else:
            receipt = validate_delivery_receipt(delivered)
            if (
                receipt["attempt_receipt_sha256"]
                != request["attempt_receipt_sha256"]
                or receipt["summary_sha256"] != request["summary_sha256"]
            ):
                raise ReleaseCompletionError(
                    "muncho_release_gateway_discord_delivery_conflict"
                )
    return tuple(pending)


def record_gateway_discord_delivery(
    state_dir: Path,
    request: Mapping[str, Any],
    *,
    message_id: str,
    published_at: datetime | None = None,
) -> dict[str, Any]:
    """Bind the connector's exact Discord message ID to the reserved attempt."""

    state = _ensure_state_dir(state_dir)
    bound_request = validate_gateway_discord_request(request)
    suffix = _identity_suffix(
        bound_request["muncho_version"], bound_request["release_sha"]
    )
    request_path = _gateway_discord_request_path(
        state,
        version=bound_request["muncho_version"],
        release_sha=bound_request["release_sha"],
    )
    if validate_gateway_discord_request(_read(request_path)) != bound_request:
        raise ReleaseCompletionError(
            "muncho_release_gateway_discord_request_binding_invalid"
        )
    draft = validate_summary_draft(_read(state / f"summary-draft-{suffix}.json"))
    attempt = validate_delivery_attempt(
        _read(state / f"summary-discord-attempt-{suffix}.json")
    )
    if (
        draft["receipt_sha256"] != bound_request["draft_receipt_sha256"]
        or attempt["receipt_sha256"] != bound_request["attempt_receipt_sha256"]
        or draft["summary_sha256"] != bound_request["summary_sha256"]
    ):
        raise ReleaseCompletionError(
            "muncho_release_gateway_discord_request_binding_invalid"
        )
    return record_reserved_summary_delivery(
        state,
        draft,
        attempt,
        message_ref=message_id,
        published_at=published_at,
    )


def deliver_discord_via_gateway_once(
    state_dir: Path,
    draft: Mapping[str, Any],
    *,
    timeout_seconds: float = 90.0,
    poll_interval_seconds: float = 0.25,
    reserved_at: datetime | None = None,
    queued_at: datetime | None = None,
) -> dict[str, Any]:
    """Queue and await one idempotent live-gateway Discord delivery."""

    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not 0 <= float(timeout_seconds) <= 300
        or isinstance(poll_interval_seconds, bool)
        or not isinstance(poll_interval_seconds, (int, float))
        or not 0.01 <= float(poll_interval_seconds) <= 5
    ):
        raise ReleaseCompletionError("muncho_release_gateway_wait_invalid")
    bound = validate_summary_draft(draft)
    attempt, created = reserve_summary_delivery(
        state_dir,
        bound,
        kind="discord",
        destination_ref=str(bound["discord_destination"]["channel_id"]),
        reserved_at=reserved_at,
    )
    if attempt.get("schema") == DELIVERY_SCHEMA:
        return validate_delivery_receipt(attempt)
    state = _ensure_state_dir(state_dir)
    request_path = _gateway_discord_request_path(
        state,
        version=bound["muncho_version"],
        release_sha=bound["release_sha"],
    )
    if created:
        queue_gateway_discord_delivery(
            state,
            bound,
            attempt,
            queued_at=queued_at,
        )
    else:
        request = _read(request_path, missing_ok=True)
        if request is None:
            # An attempt made by a different sender may already have reached
            # Discord.  Never reinterpret it as a safe connector-spool retry.
            raise ReleaseCompletionError(
                "muncho_release_discord_delivery_reconciliation_required"
            )
        queued = validate_gateway_discord_request(request)
        if (
            queued["attempt_receipt_sha256"] != attempt["receipt_sha256"]
            or queued["draft_receipt_sha256"] != bound["receipt_sha256"]
        ):
            raise ReleaseCompletionError(
                "muncho_release_gateway_discord_request_conflict"
            )

    _attempt_path, delivery_path = _delivery_paths(state, bound, "discord")
    deadline = time.monotonic() + float(timeout_seconds)
    while True:
        delivered = _read(delivery_path, missing_ok=True)
        if delivered is not None:
            receipt = validate_delivery_receipt(delivered)
            if (
                receipt["draft_receipt_sha256"] != bound["receipt_sha256"]
                or receipt["summary_sha256"] != bound["summary_sha256"]
            ):
                raise ReleaseCompletionError(
                    "muncho_release_gateway_discord_delivery_conflict"
                )
            return receipt
        if time.monotonic() >= deadline:
            raise ReleaseCompletionError(
                "muncho_release_discord_delivery_reconciliation_required"
            )
        time.sleep(float(poll_interval_seconds))


_COMPLETION_FIELDS = frozenset({
    "muncho_version",
    "release_sha",
    "release_idempotency_key",
    "mapping_receipt_sha256",
    "restart_attestation_receipt_sha256",
    "smoke_receipt_sha256",
    "draft_receipt_sha256",
    "summary_sha256",
    "gateway_discord_request_receipt_sha256",
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
    restart = require_restart_attestation(state, bound_mapping)
    bound_smoke = validate_smoke_receipt(smoke)
    bound_draft = validate_summary_draft(draft)
    suffix = _identity_suffix(
        bound_mapping["muncho_version"], bound_mapping["release_sha"]
    )
    gateway_request = validate_gateway_discord_request(
        _read(state / f"gateway-discord-request-{suffix}.json")
    )
    deliveries = {
        item["destination_kind"]: item
        for item in (
            validate_delivery_receipt(codex_delivery),
            validate_delivery_receipt(discord_delivery),
        )
    }
    discord_attempt = validate_delivery_attempt(
        _read(state / f"summary-discord-attempt-{suffix}.json")
    )
    if set(deliveries) != {"codex_task", "discord"} or (
        bound_mapping["receipt_sha256"] != bound_smoke["mapping_receipt_sha256"]
        or bound_smoke["restart_attestation_receipt_sha256"]
        != restart["receipt_sha256"]
        or bound_smoke["receipt_sha256"] != bound_draft["smoke_receipt_sha256"]
        or not _gateway_discord_request_binds(
            gateway_request,
            restart=restart,
            smoke=bound_smoke,
            draft=bound_draft,
            discord_attempt=discord_attempt,
        )
        or deliveries["discord"]["attempt_receipt_sha256"]
        != discord_attempt["receipt_sha256"]
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
                "restart_attestation_receipt_sha256": restart["receipt_sha256"],
                "smoke_receipt_sha256": bound_smoke["receipt_sha256"],
                "draft_receipt_sha256": bound_draft["receipt_sha256"],
                "summary_sha256": bound_draft["summary_sha256"],
                "gateway_discord_request_receipt_sha256": gateway_request[
                    "receipt_sha256"
                ],
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
    path = state / f"completion-{suffix}.json"
    created = _create(path, candidate)
    stored = validate_completion_receipt(_read(path))
    stable = _COMPLETION_FIELDS - {"completed_at_utc"}
    if created and stored != candidate:
        raise ReleaseCompletionError("muncho_release_completion_changed")
    if not created and any(stored[name] != candidate[name] for name in stable):
        raise ReleaseCompletionError("muncho_release_completion_conflict")
    return stored


def _load_release_completion_chain(
    state_dir: Path,
    *,
    version: str,
    release_sha: str,
) -> tuple[
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
    dict[str, Any],
]:
    state = _ensure_state_dir(state_dir)
    version = str(SemVer.parse(version))
    release_sha = require_exact_release_sha(release_sha)
    suffix = _identity_suffix(version, release_sha)
    mapping = validate_mapping_receipt(_read(state / f"mapping-v{version}.json"))
    restart = require_restart_attestation(state, mapping)
    smoke = validate_smoke_receipt(_read(state / f"smoke-{suffix}.json"))
    draft = validate_summary_draft(_read(state / f"summary-draft-{suffix}.json"))
    gateway_request = validate_gateway_discord_request(
        _read(state / f"gateway-discord-request-{suffix}.json")
    )
    discord_attempt = validate_delivery_attempt(
        _read(state / f"summary-discord-attempt-{suffix}.json")
    )
    discord = validate_delivery_receipt(
        _read(state / f"summary-discord-delivery-{suffix}.json")
    )
    expected_identity = (
        version,
        release_sha,
        release_idempotency_key(version, release_sha),
    )
    if (
        any(
            (
                record["muncho_version"],
                record["release_sha"],
                record["release_idempotency_key"],
            )
            != expected_identity
            for record in (
                mapping,
                restart,
                smoke,
                draft,
                gateway_request,
                discord_attempt,
                discord,
            )
        )
        or smoke["mapping_receipt_sha256"] != mapping["receipt_sha256"]
        or smoke["restart_attestation_receipt_sha256"]
        != restart["receipt_sha256"]
        or draft["smoke_receipt_sha256"] != smoke["receipt_sha256"]
        or not _gateway_discord_request_binds(
            gateway_request,
            restart=restart,
            smoke=smoke,
            draft=draft,
            discord_attempt=discord_attempt,
        )
        or discord["attempt_receipt_sha256"]
        != discord_attempt["receipt_sha256"]
        or discord["destination_kind"] != "discord"
        or discord["draft_receipt_sha256"] != draft["receipt_sha256"]
        or discord["summary_sha256"] != draft["summary_sha256"]
    ):
        raise ReleaseCompletionError("muncho_release_completion_binding_invalid")
    return mapping, smoke, draft, gateway_request, discord


def reserve_codex_task_summary(
    state_dir: Path,
    *,
    version: str,
    release_sha: str,
    task_id: str,
    reserved_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """Reserve the exact coordinator-task summary without claiming delivery."""

    _mapping, _smoke, draft, _gateway_request, _discord = (
        _load_release_completion_chain(
        state_dir,
        version=version,
        release_sha=release_sha,
        )
    )
    attempt, created = reserve_summary_delivery(
        state_dir,
        draft,
        kind="codex_task",
        destination_ref=task_id,
        reserved_at=reserved_at,
    )
    return draft, attempt, created


def record_codex_task_summary_and_finalize(
    state_dir: Path,
    *,
    version: str,
    release_sha: str,
    task_id: str,
    message_ref: str,
    summary_sha256: str,
    attempt_receipt_sha256: str,
    published_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Record an explicit coordinator acknowledgement, then finalize.

    This function performs no publication and never infers that the Codex task
    received a message.  The caller must supply the exact reserved attempt,
    summary digest, task ID, and returned task-message reference after the
    summary has actually been posted.
    """

    mapping, smoke, draft, _gateway_request, discord = (
        _load_release_completion_chain(
        state_dir,
        version=version,
        release_sha=release_sha,
        )
    )
    _require_digest(summary_sha256, "muncho_release_codex_ack_invalid")
    _require_digest(attempt_receipt_sha256, "muncho_release_codex_ack_invalid")
    if draft["summary_sha256"] != summary_sha256:
        raise ReleaseCompletionError("muncho_release_codex_summary_mismatch")
    attempt, _created = reserve_summary_delivery(
        state_dir,
        draft,
        kind="codex_task",
        destination_ref=task_id,
    )
    if attempt.get("schema") == DELIVERY_SCHEMA:
        codex = validate_delivery_receipt(attempt)
        if (
            codex["message_ref"] != message_ref
            or codex["attempt_receipt_sha256"] != attempt_receipt_sha256
            or codex["summary_sha256"] != summary_sha256
        ):
            raise ReleaseCompletionError("muncho_release_codex_ack_conflict")
    else:
        reserved = validate_delivery_attempt(attempt)
        if reserved["receipt_sha256"] != attempt_receipt_sha256:
            raise ReleaseCompletionError("muncho_release_codex_attempt_mismatch")
        codex = record_reserved_summary_delivery(
            state_dir,
            draft,
            reserved,
            message_ref=message_ref,
            published_at=published_at,
        )
    completion = finalize_release_completion(
        state_dir,
        mapping=mapping,
        smoke=smoke,
        draft=draft,
        codex_delivery=codex,
        discord_delivery=discord,
        completed_at=completed_at,
    )
    return codex, completion


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
        "restart_attempt": (
            state / f"restart-attempt-{suffix}.json",
            validate_restart_attempt,
        ),
        "restart": (
            state / f"restart-attestation-{suffix}.json",
            validate_restart_attestation,
        ),
        "smoke": (state / f"smoke-{suffix}.json", validate_smoke_receipt),
        "draft": (state / f"summary-draft-{suffix}.json", validate_summary_draft),
        "codex_attempt": (
            state / f"summary-codex_task-attempt-{suffix}.json",
            validate_delivery_attempt,
        ),
        "codex": (
            state / f"summary-codex_task-delivery-{suffix}.json",
            validate_delivery_receipt,
        ),
        "discord_attempt": (
            state / f"summary-discord-attempt-{suffix}.json",
            validate_delivery_attempt,
        ),
        "gateway_request": (
            state / f"gateway-discord-request-{suffix}.json",
            validate_gateway_discord_request,
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
    expected_key = release_idempotency_key(version, release_sha)
    for record in records.values():
        if record is not None and (
            record.get("muncho_version") != version
            or record.get("release_sha") != release_sha
            or record.get("release_idempotency_key") != expected_key
        ):
            raise ReleaseCompletionError("muncho_release_status_identity_mismatch")

    mapping = records["mapping"]
    restart_attempt = records["restart_attempt"]
    restart = records["restart"]
    smoke = records["smoke"]
    draft = records["draft"]
    codex_attempt = records["codex_attempt"]
    codex = records["codex"]
    discord_attempt = records["discord_attempt"]
    gateway_request = records["gateway_request"]
    discord = records["discord"]
    completion = records["completion"]
    if restart_attempt is not None and (
        mapping is None
        or restart_attempt["mapping_receipt_sha256"]
        != mapping["receipt_sha256"]
    ):
        raise ReleaseCompletionError("muncho_release_status_chain_invalid")
    if restart is not None and (
        mapping is None
        or restart_attempt is None
        or restart["mapping_receipt_sha256"] != mapping["receipt_sha256"]
        or restart["restart_attempt_receipt_sha256"]
        != restart_attempt["receipt_sha256"]
        or restart["service_name"] != restart_attempt["service_name"]
        or restart["before_invocation_id"]
        != restart_attempt["before_invocation_id"]
    ):
        raise ReleaseCompletionError("muncho_release_status_chain_invalid")
    if smoke is not None and (
        mapping is None
        or restart is None
        or smoke["mapping_receipt_sha256"] != mapping["receipt_sha256"]
        or smoke["restart_attestation_receipt_sha256"]
        != restart["receipt_sha256"]
    ):
        raise ReleaseCompletionError("muncho_release_status_chain_invalid")
    if draft is not None and (
        mapping is None
        or smoke is None
        or draft["mapping_receipt_sha256"] != mapping["receipt_sha256"]
        or draft["smoke_receipt_sha256"] != smoke["receipt_sha256"]
    ):
        raise ReleaseCompletionError("muncho_release_status_chain_invalid")
    for kind, attempt in (
        ("codex_task", codex_attempt),
        ("discord", discord_attempt),
    ):
        if attempt is not None and (
            draft is None
            or attempt["destination_kind"] != kind
            or attempt["draft_receipt_sha256"] != draft["receipt_sha256"]
            or attempt["summary_sha256"] != draft["summary_sha256"]
            or (
                kind == "discord"
                and attempt["destination_ref"]
                != draft["discord_destination"]["channel_id"]
            )
        ):
            raise ReleaseCompletionError("muncho_release_status_chain_invalid")
    for kind, attempt, delivery in (
        ("codex_task", codex_attempt, codex),
        ("discord", discord_attempt, discord),
    ):
        if delivery is not None and (
            draft is None
            or attempt is None
            or delivery["destination_kind"] != kind
            or delivery["attempt_receipt_sha256"] != attempt["receipt_sha256"]
            or delivery["draft_receipt_sha256"] != draft["receipt_sha256"]
            or delivery["summary_sha256"] != draft["summary_sha256"]
            or delivery["destination_ref"] != attempt["destination_ref"]
            or (kind == "discord" and gateway_request is None)
        ):
            raise ReleaseCompletionError("muncho_release_status_chain_invalid")
    if gateway_request is not None and (
        draft is None
        or restart is None
        or smoke is None
        or discord_attempt is None
        or not _gateway_discord_request_binds(
            gateway_request,
            restart=restart,
            smoke=smoke,
            draft=draft,
            discord_attempt=discord_attempt,
        )
    ):
        raise ReleaseCompletionError("muncho_release_status_chain_invalid")
    if completion is not None and (
        mapping is None
        or restart is None
        or smoke is None
        or draft is None
        or codex is None
        or gateway_request is None
        or discord is None
        or completion["mapping_receipt_sha256"] != mapping["receipt_sha256"]
        or completion["restart_attestation_receipt_sha256"]
        != restart["receipt_sha256"]
        or completion["smoke_receipt_sha256"] != smoke["receipt_sha256"]
        or completion["draft_receipt_sha256"] != draft["receipt_sha256"]
        or completion["summary_sha256"] != draft["summary_sha256"]
        or completion["gateway_discord_request_receipt_sha256"]
        != gateway_request["receipt_sha256"]
        or completion["codex_task_delivery_receipt_sha256"]
        != codex["receipt_sha256"]
        or completion["discord_delivery_receipt_sha256"]
        != discord["receipt_sha256"]
    ):
        raise ReleaseCompletionError("muncho_release_status_chain_invalid")
    if records["completion"]:
        phase = "complete"
    elif records["draft"]:
        phase = "summary_delivery_pending"
    elif records["smoke"]:
        phase = "summary_draft_pending"
    elif records["restart"]:
        phase = "production_smoke_pending"
    elif records["restart_attempt"]:
        phase = "restart_attestation_pending"
    elif records["mapping"]:
        phase = "qualifying_restart_pending"
    else:
        phase = "unreserved"
    return {
        "schema": STATUS_SCHEMA,
        "muncho_version": version,
        "release_sha": release_sha,
        "release_sha_short": release_sha[:8],
        "phase": phase,
        "version_sha_reserved": records["mapping"] is not None,
        "qualifying_restart_attested": records["restart"] is not None,
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
        "qualifying_restart_attested": status["qualifying_restart_attested"],
        "production_smoke_passed": status["production_smoke_passed"],
        "required_summaries_published": status["codex_task_summary_published"]
        and status["discord_summary_published"],
        "completion_receipt_sha256": status["completion_receipt_sha256"],
    }


__all__ = [
    "DELIVERY_SCHEMA",
    "ReleaseCompletionError",
    "build_mapping_receipt",
    "complete_restart_attestation",
    "deliver_discord_via_gateway_once",
    "finalize_release_completion",
    "load_current_production_config",
    "pending_gateway_discord_deliveries",
    "prepare_summary_draft",
    "prepare_restart_attestation",
    "queue_gateway_discord_delivery",
    "record_codex_task_summary_and_finalize",
    "record_gateway_discord_delivery",
    "record_production_smoke",
    "record_reserved_summary_delivery",
    "release_health",
    "release_status",
    "render_release_summary",
    "require_restart_attestation",
    "reserve_codex_task_summary",
    "reserve_release_mapping",
    "reserve_summary_delivery",
    "resolve_discord_destination",
    "validate_gateway_discord_request",
    "validate_restart_attestation",
    "validate_restart_attempt",
]
