"""Fail-closed, create-only staging for paused cron-job manifests.

A manifest transaction validates every job before taking the registry lock, then
creates all missing jobs with one jobs.json write. Existing jobs must be exact
replays of the manifest-derived record (apart from their original creation
instant); collisions or drift abort without mutation.

Agent jobs must pin both ``provider`` and ``model``. This keeps replay
validation deterministic instead of depending on whatever provider/model is
configured when the manifest is replayed. No-agent jobs need no such pins.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, NoReturn, cast

from cron import jobs as cron_jobs
from hermes_time import now as _hermes_now

CONTRACT = "hermes-paused-cron-manifest.v1"
_SAFE_ID_RE = re.compile(r"[0-9a-f]{12}")
_TOP_LEVEL_KEYS = frozenset({"contract", "schema_version", "paused_reason", "jobs"})
_REQUIRED_JOB_KEYS = frozenset({"id", "name", "schedule"})
_JOB_KEYS = frozenset({
    "id",
    "name",
    "schedule",
    "prompt",
    "repeat",
    "deliver",
    "skills",
    "model",
    "provider",
    "base_url",
    "script",
    "context_from",
    "enabled_toolsets",
    "workdir",
    "no_agent",
    "attach_to_session",
    "monitor_script",
    "monitor_url",
    "reasoning_effort",
})
_OPTIONAL_TEXT_KEYS = frozenset({
    "deliver",
    "model",
    "provider",
    "base_url",
    "script",
    "workdir",
    "monitor_script",
    "monitor_url",
    "reasoning_effort",
})


class PausedManifestError(ValueError):
    """Fail-closed manifest or existing-registry mismatch."""


def _fail(code: str) -> NoReturn:
    raise PausedManifestError(code)


def _read_target_bytes(target: Path) -> bytes | None:
    try:
        return target.read_bytes()
    except FileNotFoundError:
        return None


def _required_ownership(target: Path, target_exists: bool) -> tuple[int, int]:
    source = target if target_exists else target.parent
    stat_result = source.stat()
    return stat_result.st_uid, stat_result.st_gid


def _preserve_ownership(fd: int, required: tuple[int, int]) -> os.stat_result:
    staged = os.fstat(fd)
    if (staged.st_uid, staged.st_gid) != required:
        fchown = getattr(os, "fchown", None)
        if fchown is None:
            _fail("jobs_registry_ownership_failed")
        try:
            fchown(fd, *required)
            staged = os.fstat(fd)
        except BaseException:
            _fail("jobs_registry_ownership_failed")
        if (staged.st_uid, staged.st_gid) != required:
            _fail("jobs_registry_ownership_failed")
    return staged


def _fsync_parent_directory(target: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_fd = os.open(target.parent, flags)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _resolved_registry_target() -> Path:
    logical_target = cron_jobs._current_cron_store().jobs_file
    return (
        Path(os.path.realpath(logical_target))
        if logical_target.is_symlink()
        else logical_target
    )


def _cleanup_staged_temp(tmp_path: str | None, staged: os.stat_result | None) -> None:
    """Remove only the still-present inode created by this transaction."""
    if tmp_path is None or staged is None:
        return
    try:
        residue = os.stat(tmp_path, follow_symlinks=False)
        if (residue.st_dev, residue.st_ino) == (staged.st_dev, staged.st_ino):
            os.unlink(tmp_path)
    except OSError:
        pass


def _commit_registry(records: list[dict[str, Any]]) -> None:
    """Commit one manifest transaction without any non-atomic fallback.

    The normal cron writer tolerates unusual filesystems by copying over the
    live target when ``replace(2)`` cannot cross a device boundary.  Manifest
    staging has a stricter contract. Resolve a jobs.json symlink first, create
    the temporary file beside that real target, fsync it, atomically replace,
    then fsync the target directory while the cross-process lock is still held.

    A failure proven by readback to have left the old bytes returns
    ``jobs_registry_commit_failed``. If the new bytes landed, or readback sees
    anything else, return ``jobs_registry_commit_uncertain``: callers must
    replay the same manifest, whose exact-record comparison safely converges
    without another write when the staged payload did land.
    """
    target = _resolved_registry_target()
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        old_payload = _read_target_bytes(target)
        ownership = _required_ownership(target, old_payload is not None)
    except OSError:
        _fail("jobs_registry_commit_failed")

    staged_payload = json.dumps(
        {"jobs": records, "updated_at": _hermes_now().isoformat()},
        indent=2,
        ensure_ascii=False,
    ).encode("utf-8")

    fd: int | None = None
    tmp_path: str | None = None
    staged_stat: os.stat_result | None = None
    replace_started = False
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent), suffix=".tmp", prefix=".jobs_manifest_"
        )
        staged_stat = os.fstat(fd)
        staged_stat = _preserve_ownership(fd, ownership)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(staged_payload)
            stream.flush()
            os.fsync(stream.fileno())
        cron_jobs._record_load_stamp(None)
        replace_started = True
        os.replace(tmp_path, target)
        _fsync_parent_directory(target)
    except BaseException as error:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _cleanup_staged_temp(tmp_path, staged_stat)
        if isinstance(error, PausedManifestError) and not replace_started:
            raise
        try:
            landed_payload = _read_target_bytes(target)
        except OSError:
            _fail("jobs_registry_commit_uncertain")
        if landed_payload == staged_payload:
            _fail("jobs_registry_commit_uncertain")
        if landed_payload == old_payload:
            _fail("jobs_registry_commit_failed")
        _fail("jobs_registry_commit_uncertain")


def _load_manifest(path: Path) -> tuple[dict[str, Any], str]:
    try:
        if not path.is_file() or path.is_symlink():
            _fail("manifest_not_regular")
        raw = path.read_bytes()
        manifest = json.loads(raw.decode("utf-8"))
    except PausedManifestError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        _fail("manifest_invalid_json")
    if not isinstance(manifest, dict) or set(manifest) != _TOP_LEVEL_KEYS:
        _fail("manifest_shape_invalid")
    manifest = cast(dict[str, Any], manifest)
    canonical = json.dumps(
        manifest, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return manifest, hashlib.sha256(canonical).hexdigest()


def _strict_optional_text(spec: dict[str, Any], key: str) -> None:
    if key not in spec or spec[key] is None:
        return
    value = spec[key]
    if not isinstance(value, str) or not value or value != value.strip():
        _fail(f"job_{key}_invalid")


def _strict_string_list(spec: dict[str, Any], key: str) -> None:
    if key not in spec or spec[key] is None:
        return
    values = spec[key]
    if not isinstance(values, list):
        _fail(f"job_{key}_invalid")
    normalized: list[str] = []
    for value in values:
        if not isinstance(value, str) or not value or value != value.strip():
            _fail(f"job_{key}_invalid")
        normalized.append(value)
    if len(normalized) != len(set(normalized)):
        _fail(f"job_{key}_invalid")


def _validate_job_spec(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        _fail("job_shape_invalid")
    keys = set(item)
    if not _REQUIRED_JOB_KEYS <= keys or not keys <= _JOB_KEYS:
        _fail("job_shape_invalid")
    spec = cast(dict[str, Any], item)

    job_id = spec["id"]
    if not isinstance(job_id, str) or _SAFE_ID_RE.fullmatch(job_id) is None:
        _fail("job_id_invalid")
    name = spec["name"]
    if not isinstance(name, str) or not name or name != name.strip() or len(name) > 200:
        _fail("job_name_invalid")
    schedule = spec["schedule"]
    if not isinstance(schedule, str) or not schedule or schedule != schedule.strip():
        _fail("job_schedule_invalid")
    try:
        cron_jobs.parse_schedule(schedule)
    except (TypeError, ValueError):
        _fail("job_schedule_invalid")

    if "prompt" in spec and not isinstance(spec["prompt"], str):
        _fail("job_prompt_invalid")
    repeat = spec.get("repeat")
    if repeat is not None and (type(repeat) is not int or repeat < 1):
        _fail("job_repeat_invalid")
    for key in _OPTIONAL_TEXT_KEYS:
        _strict_optional_text(spec, key)
    for key in ("skills", "enabled_toolsets"):
        _strict_string_list(spec, key)

    context_from = spec.get("context_from")
    if context_from is not None:
        if isinstance(context_from, str):
            if not context_from or context_from != context_from.strip():
                _fail("job_context_from_invalid")
        elif isinstance(context_from, list):
            _strict_string_list(spec, "context_from")
        else:
            _fail("job_context_from_invalid")

    if "no_agent" in spec and type(spec["no_agent"]) is not bool:
        _fail("job_no_agent_invalid")
    attach = spec.get("attach_to_session")
    if attach is not None and type(attach) is not bool:
        _fail("job_attach_to_session_invalid")

    no_agent = spec.get("no_agent", False)
    if not no_agent and (not spec.get("provider") or not spec.get("model")):
        _fail("job_provider_model_pins_required")
    return dict(spec)


def _validate_manifest(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    if (
        manifest.get("contract") != CONTRACT
        or type(manifest.get("schema_version")) is not int
        or manifest["schema_version"] != 1
    ):
        _fail("manifest_contract_invalid")
    reason = manifest.get("paused_reason")
    if (
        not isinstance(reason, str)
        or not reason
        or reason != reason.strip()
        or len(reason) > cron_jobs.MAX_PAUSED_REASON_LENGTH
    ):
        _fail("paused_reason_invalid")
    raw_specs = manifest.get("jobs")
    if not isinstance(raw_specs, list) or not raw_specs:
        _fail("jobs_invalid")

    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    validated: list[dict[str, Any]] = []
    for item in raw_specs:
        spec = _validate_job_spec(item)
        job_id = spec["id"]
        folded_name = spec["name"].casefold()
        if job_id in seen_ids:
            _fail("duplicate_job_id")
        if folded_name in seen_names:
            _fail("duplicate_job_name")
        seen_ids.add(job_id)
        seen_names.add(folded_name)
        validated.append(spec)
    return validated


def _expected_record(
    spec: dict[str, Any],
    *,
    paused_reason: str,
    created_at: str,
    validate_oneshot_eligibility: bool,
) -> dict[str, Any]:
    kwargs = dict(spec)
    job_id = kwargs.pop("id")
    try:
        relative_schedule_base = datetime.fromisoformat(
            created_at.replace("Z", "+00:00")
        )
    except ValueError:
        _fail("job_spec_invalid")
    try:
        return cron_jobs._build_job_record(
            **kwargs,
            initial_paused=True,
            paused_reason=paused_reason,
            job_id=job_id,
            created_at=created_at,
            validate_oneshot_eligibility=validate_oneshot_eligibility,
            relative_schedule_base=relative_schedule_base,
        )
    except (TypeError, ValueError):
        _fail("job_spec_invalid")


def _existing_matches(
    existing: dict[str, Any], spec: dict[str, Any], *, paused_reason: str
) -> bool:
    created_at = existing.get("created_at")
    if not isinstance(created_at, str) or existing.get("paused_at") != created_at:
        return False
    expected = _expected_record(
        spec,
        paused_reason=paused_reason,
        created_at=created_at,
        validate_oneshot_eligibility=False,
    )
    return existing == expected


def stage_paused_manifest(path: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Validate and atomically create every missing job in a paused state.

    The operation requires the jobs registry's OS cross-process lock. Its
    bounded outcomes are a durable registry write, a proven no-write failure,
    or ``jobs_registry_commit_uncertain`` when an atomic replace may have landed
    but its durability could not be established. Replay is the recovery
    operation. Replaying the same manifest is a no-op; target identity
    collisions and any stored-record drift are rejected.
    """
    if type(dry_run) is not bool:
        _fail("dry_run_must_be_boolean")
    manifest, manifest_sha256 = _load_manifest(Path(path))
    specs = _validate_manifest(manifest)
    paused_reason = cast(str, manifest["paused_reason"])

    with cron_jobs._jobs_lock(require_cross_process=True):
        current = cron_jobs._peek_jobs_unlocked()
        if current is None:
            _fail("jobs_registry_invalid")
        current = cast(list[dict[str, Any]], current)
        by_id: dict[str, dict[str, Any]] = {}
        by_name: dict[str, list[dict[str, Any]]] = {}
        for job in current:
            if not isinstance(job, dict):
                _fail("jobs_registry_invalid")
            job_id = job.get("id")
            if isinstance(job_id, str):
                if job_id in by_id:
                    _fail("existing_duplicate_job_id")
                by_id[job_id] = job
            name = job.get("name")
            if isinstance(name, str):
                by_name.setdefault(name.casefold(), []).append(job)

        missing: list[dict[str, Any]] = []
        staged_at = _hermes_now().isoformat()
        for spec in specs:
            id_match = by_id.get(spec["id"])
            name_matches = by_name.get(spec["name"].casefold(), [])
            if id_match is None:
                if name_matches:
                    _fail("existing_job_name_collision")
                missing.append(
                    _expected_record(
                        spec,
                        paused_reason=paused_reason,
                        created_at=staged_at,
                        validate_oneshot_eligibility=True,
                    )
                )
                continue
            if name_matches != [id_match]:
                _fail("existing_job_identity_mismatch")
            if not _existing_matches(id_match, spec, paused_reason=paused_reason):
                _fail("existing_job_drift")

        if missing and not dry_run:
            _commit_registry(current + missing)
        elif not missing and not dry_run:
            try:
                _fsync_parent_directory(_resolved_registry_target())
            except BaseException:
                _fail("jobs_registry_commit_uncertain")

    job_count = len(specs)
    return {
        "contract": CONTRACT,
        "manifest_sha256": manifest_sha256,
        "job_count": job_count,
        "matching_count": job_count - len(missing),
        "create_count": len(missing),
        "mutated": bool(missing) and not dry_run,
        "dry_run": dry_run,
        "job_ids": sorted(spec["id"] for spec in specs),
    }
