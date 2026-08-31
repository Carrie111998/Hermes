"""Pure scan and reconciliation planning for replicated Desktop session records.

This module deliberately does not write registry files or durable state.  It turns a
complete, stable scan plus the last verified replica baselines into an immutable plan
that a single durable writer can stage, apply, and verify.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass, field as _dataclass_field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

DESKTOP_REGISTRY_GROUPING_VERSION = 1

_IDENTITY_FIELDS = frozenset({"sessionId"})
_PROTECTED_FIELDS = frozenset({"cliSessionId"})

_GROUP_FIELDS: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "worktree": (
            "worktreeName",
            "worktreePath",
            "sourceBranch",
            "branch",
            "writtenBranches",
            "keptDirtyWorktree",
        ),
        "error-state": ("error", "errorAt", "errorCategory", "priorErrorMark"),
        "pull-request": ("prNumber", "prRepository", "prState", "prUrl", "prs"),
        "permissions": (
            "permissionMode",
            "chromePermissionMode",
            "alwaysAllowedReasons",
            "sessionPermissionUpdates",
            "sessionSettings",
            "cuAllowedApps",
            "cuGrantFlags",
            "cuLastScreenshotDims",
            "cuSelectedDisplayId",
        ),
        "mcp": ("remoteMcpServersConfig", "enabledMcpTools"),
        "background-tasks": (
            "backgroundTaskSuggestions",
            "resolvedBackgroundTaskSuggestions",
            "pendingSystemReminder",
        ),
    }
)

# All fields observed in the three enrolled Desktop stores on 2026-08-30.  Keys not
# listed here are preserved in place but quarantined from convergence until their
# semantics are classified.  Adding a field to a correlated group changes conflict
# semantics and therefore requires a grouping-version bump.
_OBSERVED_FIELDS = frozenset(
    {
        "alwaysAllowedReasons",
        "armedWorkAtQuit",
        "autoArchiveExempt",
        "autoFixEnabled",
        "backgroundTaskSuggestions",
        "branch",
        "bridgeSessionIds",
        "chromePermissionMode",
        "chromeTabGroupId",
        "classifierSummaryEnabled",
        "cliSessionId",
        "completedTurns",
        "contextExceededCount",
        "createdAt",
        "cuAllowedApps",
        "cuGrantFlags",
        "cuLastScreenshotDims",
        "cuSelectedDisplayId",
        "cwd",
        "dispatchParentId",
        "effort",
        "enabledMcpTools",
        "error",
        "errorAt",
        "errorCategory",
        "forkedFromSessionId",
        "isArchived",
        "isStarred",
        "keptDirtyWorktree",
        "lastActivityAt",
        "lastFocusedAt",
        "lastSpawnRootDetected",
        "model",
        "originCwd",
        "pendingSystemReminder",
        "permissionMode",
        "planPath",
        "prNumber",
        "prRepository",
        "prState",
        "prUrl",
        "priorErrorMark",
        "promptSuggestion",
        "prs",
        "remoteControlAutoEligible",
        "remoteMcpServersConfig",
        "reportFindingsCard",
        "resolvedBackgroundTaskSuggestions",
        "scheduledTaskId",
        "sessionId",
        "sessionPermissionUpdates",
        "sessionSettings",
        "sourceBranch",
        "spawnSeed",
        "spawnedFrom",
        "spawnedFromEndNotified",
        "title",
        "titleSource",
        "transcriptUnavailable",
        "worktreeName",
        "worktreePath",
        "writtenBranches",
    }
)
_GROUPED_FIELDS = frozenset(field for fields in _GROUP_FIELDS.values() for field in fields)
_INDEPENDENT_FIELDS = _OBSERVED_FIELDS - _IDENTITY_FIELDS - _PROTECTED_FIELDS - _GROUPED_FIELDS

_ABSENT_JSON = '{"state":"absent"}'


class RegistryScanError(ValueError):
    """The enrolled roots could not produce complete, stable evidence."""


class RegistryMutationConflict(RuntimeError):
    """A staged mutation no longer matches current filesystem evidence."""


@dataclass(frozen=True)
class RegistryRootObservation:
    root_id: str
    path: Path
    canonical_path: str
    filenames: tuple[str, ...]


@dataclass(frozen=True)
class RegistryRecordObservation:
    root_id: str
    filename: str
    path: Path
    session_id: str
    exact_bytes: bytes
    byte_hash: str
    mtime_ns: int
    record: Mapping[str, Any]
    group_values: Mapping[str, str]


@dataclass(frozen=True)
class RegistryScan:
    roots: Mapping[str, RegistryRootObservation]
    records: Mapping[str, Mapping[str, RegistryRecordObservation]]


@dataclass
class RegistryScanCache:
    """Reuse observations for files whose stat evidence is unchanged.

    Keyed by normcased path; the value pairs the exact stat identity
    ``(st_dev, st_ino, st_size, st_mtime_ns)`` -- the same evidence tuple the
    strict stable read captures -- with the frozen observation it produced.
    A file matching all four is not re-read or re-canonicalized, which keeps a
    steady-state cycle's cost proportional to recent activity instead of store
    size. Entries for files no longer present are evicted after every scan.
    """

    entries: dict[
        str, tuple[tuple[int, int, int, int], RegistryRecordObservation]
    ] = _dataclass_field(default_factory=dict)


@dataclass(frozen=True)
class RegistryBaseline:
    filename: str
    root_id: str
    group_name: str
    value_json: str
    revision: int


@dataclass(frozen=True)
class RegistryConflict:
    filename: str
    group_name: str
    reason: str
    candidates: Mapping[str, str]


@dataclass(frozen=True)
class RegistryMutation:
    root_id: str
    filename: str
    operation: str
    expected_before_hash: str | None
    after_bytes: str
    changed_fields: Mapping[str, Mapping[str, Any]]


@dataclass(frozen=True)
class RegistryRecordPlan:
    filename: str
    session_id: str
    desired_groups: Mapping[str, str]
    mutations: tuple[RegistryMutation, ...]


@dataclass(frozen=True)
class RegistrySyncPlan:
    scan: RegistryScan
    records: Mapping[str, RegistryRecordPlan]
    conflicts: tuple[RegistryConflict, ...]
    proposed_baselines: tuple[RegistryBaseline, ...]


@dataclass(frozen=True)
class RegistryVerificationFailure:
    filename: str
    root_id: str
    group_name: str
    reason: str
    expected_value_json: str
    observed_value_json: str | None


@dataclass(frozen=True)
class RegistryVerification:
    verified: bool
    failures: tuple[RegistryVerificationFailure, ...]


@dataclass(frozen=True)
class RegistryMutationResult:
    applied: bool
    byte_hash: str


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _tagged_value(record: Mapping[str, Any], field: str) -> Mapping[str, Any]:
    if field not in record:
        return {"state": "absent"}
    return {"state": "present", "value": record[field]}


def _canonical_single_value(record: Mapping[str, Any], field: str) -> str:
    return _canonical_json(_tagged_value(record, field))


def _canonical_correlated_value(
    record: Mapping[str, Any], fields: Sequence[str]
) -> str:
    return _canonical_json(
        {
            "state": "group",
            "fields": {field: _tagged_value(record, field) for field in fields},
        }
    )


def canonical_group_value(record: Mapping[str, Any]) -> dict[str, str]:
    """Return canonical values for every known or present synchronizable group."""
    values = {
        group_name: _canonical_correlated_value(record, fields)
        for group_name, fields in _GROUP_FIELDS.items()
    }
    for field in sorted(_INDEPENDENT_FIELDS):
        values[f"field:{field}"] = _canonical_single_value(record, field)
    for field in sorted(_PROTECTED_FIELDS):
        values[f"protected:{field}"] = _canonical_single_value(record, field)

    known = _IDENTITY_FIELDS | _PROTECTED_FIELDS | _GROUPED_FIELDS | _INDEPENDENT_FIELDS
    for field in sorted(set(record) - known):
        values[f"unknown:{field}"] = _canonical_single_value(record, field)
    return values


def _strict_object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistryScanError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> None:
    raise RegistryScanError(f"invalid JSON constant: {value}")


def _parse_record(raw: bytes, path: Path) -> dict[str, Any]:
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise RegistryScanError(f"registry record is not UTF-8: {path}") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_strict_object_pairs,
            parse_constant=_reject_json_constant,
        )
    except RegistryScanError:
        raise
    except json.JSONDecodeError as exc:
        raise RegistryScanError(f"malformed registry JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RegistryScanError(f"registry record is not an object: {path}")
    return value


def _is_reparse_point(path: Path, file_stat: os.stat_result | None = None) -> bool:
    if path.is_symlink():
        return True
    file_stat = file_stat or path.stat(follow_symlinks=False)
    attributes = getattr(file_stat, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def _list_record_names(root: Path) -> tuple[str, ...]:
    try:
        entries = tuple(root.iterdir())
    except OSError as exc:
        raise RegistryScanError(f"registry root unreadable: {root}") from exc

    names: list[str] = []
    folded: dict[str, str] = {}
    for entry in entries:
        if not entry.name.startswith("local_") or entry.suffix != ".json":
            continue
        try:
            entry_stat = entry.stat(follow_symlinks=False)
        except OSError as exc:
            raise RegistryScanError(f"registry entry unreadable: {entry}") from exc
        if _is_reparse_point(entry, entry_stat) or not stat.S_ISREG(entry_stat.st_mode):
            raise RegistryScanError(f"registry entry is not a regular file: {entry}")
        folded_name = entry.name.casefold()
        prior = folded.get(folded_name)
        if prior is not None and prior != entry.name:
            raise RegistryScanError(
                f"registry filenames differ only by normalization: {prior}, {entry.name}"
            )
        folded[folded_name] = entry.name
        names.append(entry.name)
    return tuple(sorted(names))


def _stable_read(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        before = path.stat(follow_symlinks=False)
        if _is_reparse_point(path, before) or not stat.S_ISREG(before.st_mode):
            raise RegistryScanError(f"registry entry is not a regular file: {path}")
        raw = path.read_bytes()
        after = path.stat(follow_symlinks=False)
    except RegistryScanError:
        raise
    except OSError as exc:
        raise RegistryScanError(f"registry record changed or disappeared: {path}") from exc
    evidence_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
    evidence_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if evidence_before != evidence_after or len(raw) != after.st_size:
        raise RegistryScanError(f"registry record was unstable during read: {path}")
    return raw, after


def _root_identity(root: Path) -> tuple[str, Path]:
    try:
        resolved = root.resolve(strict=True)
        root_stat = root.stat(follow_symlinks=False)
    except OSError as exc:
        raise RegistryScanError(f"registry root unavailable: {root}") from exc
    if _is_reparse_point(root, root_stat) or not stat.S_ISDIR(root_stat.st_mode):
        raise RegistryScanError(f"registry root is not a regular directory: {root}")
    canonical = os.path.normcase(str(resolved))
    root_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]
    return root_id, resolved


def scan_desktop_registry_roots(
    roots: Iterable[Path], *, cache: RegistryScanCache | None = None
) -> RegistryScan:
    """Capture a complete stable scan of an explicitly enrolled replica set."""
    root_list = tuple(Path(root) for root in roots)
    if not root_list:
        raise RegistryScanError("no registry roots enrolled")

    root_observations: dict[str, RegistryRootObservation] = {}
    canonical_roots: dict[str, Path] = {}
    record_observations: dict[str, dict[str, RegistryRecordObservation]] = {}
    seen_cache_keys: set[str] = set()

    for root in root_list:
        root_id, resolved = _root_identity(root)
        canonical = os.path.normcase(str(resolved))
        prior = canonical_roots.get(canonical)
        if prior is not None:
            raise RegistryScanError(f"duplicate resolved registry roots: {prior}, {root}")
        canonical_roots[canonical] = root

        filenames = _list_record_names(resolved)
        root_observations[root_id] = RegistryRootObservation(
            root_id=root_id,
            path=resolved,
            canonical_path=str(resolved),
            filenames=filenames,
        )
        for filename in filenames:
            path = resolved / filename
            observation: RegistryRecordObservation | None = None
            cache_key = os.path.normcase(str(path))
            if cache is not None:
                seen_cache_keys.add(cache_key)
                cached = cache.entries.get(cache_key)
                if cached is not None:
                    try:
                        current = path.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise RegistryScanError(
                            f"registry record changed or disappeared: {path}"
                        ) from exc
                    identity = (
                        current.st_dev,
                        current.st_ino,
                        current.st_size,
                        current.st_mtime_ns,
                    )
                    stored_identity, stored_observation = cached
                    if (
                        identity == stored_identity
                        and stored_observation.root_id == root_id
                        and stored_observation.filename == filename
                    ):
                        observation = stored_observation
            if observation is None:
                raw, record_stat = _stable_read(path)
                record = _parse_record(raw, path)
                expected_session_id = filename[: -len(".json")]
                session_id = record.get("sessionId")
                if (
                    not isinstance(session_id, str)
                    or session_id != expected_session_id
                ):
                    raise RegistryScanError(f"registry identity mismatch: {path}")
                observation = RegistryRecordObservation(
                    root_id=root_id,
                    filename=filename,
                    path=path,
                    session_id=session_id,
                    exact_bytes=raw,
                    byte_hash=hashlib.sha256(raw).hexdigest(),
                    mtime_ns=record_stat.st_mtime_ns,
                    record=MappingProxyType(record),
                    group_values=MappingProxyType(canonical_group_value(record)),
                )
                if cache is not None:
                    cache.entries[cache_key] = (
                        (
                            record_stat.st_dev,
                            record_stat.st_ino,
                            record_stat.st_size,
                            record_stat.st_mtime_ns,
                        ),
                        observation,
                    )
            record_observations.setdefault(filename, {})[root_id] = observation

        if _list_record_names(resolved) != filenames:
            raise RegistryScanError(f"registry root membership changed during scan: {root}")

    if cache is not None:
        for stale_key in set(cache.entries) - seen_cache_keys:
            del cache.entries[stale_key]

    return RegistryScan(
        roots=MappingProxyType(root_observations),
        records=MappingProxyType(
            {
                filename: MappingProxyType(observations)
                for filename, observations in record_observations.items()
            }
        ),
    )


def _value_for_group(observation: RegistryRecordObservation, group_name: str) -> str:
    if group_name in observation.group_values:
        return observation.group_values[group_name]
    if group_name.startswith(("field:", "protected:", "unknown:")):
        return _ABSENT_JSON
    raise ValueError(f"observation lacks required group {group_name}")


def _fields_for_group(group_name: str) -> tuple[str, ...]:
    if group_name in _GROUP_FIELDS:
        return _GROUP_FIELDS[group_name]
    prefix, separator, field = group_name.partition(":")
    if separator and prefix in {"field", "protected"} and field:
        return (field,)
    raise ValueError(f"unknown registry group: {group_name}")


def _decoded_tag(value: str) -> Mapping[str, Any]:
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("canonical group value is not an object")
    return parsed


def _apply_group(record: dict[str, Any], group_name: str, value_json: str) -> None:
    value = _decoded_tag(value_json)
    if group_name in _GROUP_FIELDS:
        if value.get("state") != "group" or not isinstance(value.get("fields"), dict):
            raise ValueError(f"invalid correlated group value for {group_name}")
        fields = value["fields"]
        for field in _GROUP_FIELDS[group_name]:
            tagged = fields.get(field)
            if not isinstance(tagged, dict):
                raise ValueError(f"missing correlated field {field}")
            _apply_tagged_field(record, field, tagged)
        return
    field = _fields_for_group(group_name)[0]
    _apply_tagged_field(record, field, value)


def _apply_tagged_field(
    record: dict[str, Any], field: str, tagged: Mapping[str, Any]
) -> None:
    state = tagged.get("state")
    if state == "absent" and set(tagged) == {"state"}:
        record.pop(field, None)
        return
    if state == "present" and set(tagged) == {"state", "value"}:
        record[field] = tagged["value"]
        return
    raise ValueError(f"invalid tagged value for {field}")


def _changed_fields(group_name: str, value_json: str) -> dict[str, Mapping[str, Any]]:
    value = _decoded_tag(value_json)
    if group_name in _GROUP_FIELDS:
        fields = value.get("fields")
        if not isinstance(fields, dict):
            raise ValueError(f"invalid correlated group value for {group_name}")
        return {field: fields[field] for field in _GROUP_FIELDS[group_name]}
    return {_fields_for_group(group_name)[0]: value}


def _validate_baselines(
    scan: RegistryScan,
    baselines_by_record: Mapping[str, Mapping[str, Mapping[str, RegistryBaseline]]],
) -> None:
    expected_roots = set(scan.roots)
    for filename, groups in baselines_by_record.items():
        observations = scan.records.get(filename)
        if observations is None:
            raise ValueError(f"baseline references missing record {filename}")
        # A group entirely absent from the baselines was never accepted (for
        # example a standing quarantine); that is legitimate.  Only PARTIAL
        # root coverage is evidence of a torn or foreign write.
        for group_name, rows in groups.items():
            covered = set(rows)
            if covered != expected_roots:
                raise ValueError(
                    f"incomplete baseline for {filename} {group_name}: "
                    f"expected {len(expected_roots)} roots, found {len(covered)}"
                )


def _bootstrap_decision(
    filename: str,
    group_name: str,
    observations: Mapping[str, RegistryRecordObservation],
) -> tuple[str | None, RegistryConflict | None]:
    candidates = {
        root_id: _value_for_group(observation, group_name)
        for root_id, observation in observations.items()
    }
    if len(set(candidates.values())) == 1:
        return next(iter(candidates.values())), None

    newest_mtime = max(observation.mtime_ns for observation in observations.values())
    newest = {
        root_id: candidates[root_id]
        for root_id, observation in observations.items()
        if observation.mtime_ns == newest_mtime
    }
    if len(set(newest.values())) == 1:
        return next(iter(newest.values())), None
    return None, RegistryConflict(
        filename=filename,
        group_name=group_name,
        reason="bootstrap_newest_tie",
        candidates=MappingProxyType(newest),
    )


def _unaccepted_group_decision(
    filename: str,
    group_name: str,
    observations: Mapping[str, RegistryRecordObservation],
) -> tuple[str | None, RegistryConflict | None]:
    """Decide a group with no accepted baseline on a record that is past bootstrap.

    File mtimes were spent as evidence during the one-time bootstrap; they are
    never conflict-resolution authority afterwards.  The only accepted retry
    path for a standing quarantine is the observations collapsing to one value.
    """
    values = {
        root_id: _value_for_group(observation, group_name)
        for root_id, observation in observations.items()
    }
    if group_name.startswith("unknown:"):
        return None, RegistryConflict(
            filename=filename,
            group_name=group_name,
            reason="unknown_field_unclassified",
            candidates=MappingProxyType(values),
        )
    if len(set(values.values())) == 1:
        return next(iter(values.values())), None
    reason = (
        "protected_linkage_divergence"
        if group_name.startswith("protected:")
        else "unaccepted_group_divergence"
    )
    return None, RegistryConflict(
        filename=filename,
        group_name=group_name,
        reason=reason,
        candidates=MappingProxyType(values),
    )


def _steady_state_decision(
    filename: str,
    group_name: str,
    observations: Mapping[str, RegistryRecordObservation],
    baseline_rows: Mapping[str, RegistryBaseline],
) -> tuple[str | None, RegistryConflict | None]:
    changed: dict[str, str] = {}
    current: dict[str, str] = {}
    baseline_values = {baseline.value_json for baseline in baseline_rows.values()}

    if group_name.startswith("unknown:"):
        current = {
            root_id: _value_for_group(observation, group_name)
            for root_id, observation in observations.items()
        }
        return None, RegistryConflict(
            filename=filename,
            group_name=group_name,
            reason="unknown_field_unclassified",
            candidates=MappingProxyType(current),
        )

    # Missing replicas have no observation but retain a root-specific baseline.  Current
    # deltas are derived only from extant files; creation uses the accepted composite.
    for root_id, observation in observations.items():
        value = _value_for_group(observation, group_name)
        current[root_id] = value
        baseline = baseline_rows[root_id]
        if value != baseline.value_json:
            changed[root_id] = value

    if group_name.startswith("protected:"):
        if changed:
            return None, RegistryConflict(
                filename=filename,
                group_name=group_name,
                reason="protected_linkage_divergence",
                candidates=MappingProxyType(current),
            )
        if len(baseline_values) != 1:
            return None, RegistryConflict(
                filename=filename,
                group_name=group_name,
                reason="baseline_divergence",
                candidates=MappingProxyType(current),
            )
        return next(iter(baseline_values)), None

    if not changed:
        if len(baseline_values) != 1:
            return None, RegistryConflict(
                filename=filename,
                group_name=group_name,
                reason="baseline_divergence",
                candidates=MappingProxyType(current),
            )
        return next(iter(baseline_values)), None
    if len(set(changed.values())) == 1:
        return next(iter(changed.values())), None
    return None, RegistryConflict(
        filename=filename,
        group_name=group_name,
        reason="concurrent_divergence",
        candidates=MappingProxyType(changed),
    )


def build_registry_sync_plan(
    scan: RegistryScan, *, baselines: Iterable[RegistryBaseline]
) -> RegistrySyncPlan:
    """Plan field-level convergence without mutating files or durable state."""
    baselines_by_record: dict[str, dict[str, dict[str, RegistryBaseline]]] = {}
    for baseline in baselines:
        rows = baselines_by_record.setdefault(baseline.filename, {}).setdefault(
            baseline.group_name, {}
        )
        if baseline.root_id in rows:
            raise ValueError(
                "duplicate baseline: "
                f"{(baseline.filename, baseline.root_id, baseline.group_name)}"
            )
        rows[baseline.root_id] = baseline
    _validate_baselines(scan, baselines_by_record)

    record_plans: dict[str, RegistryRecordPlan] = {}
    conflicts: list[RegistryConflict] = []
    proposed: list[RegistryBaseline] = []

    for filename in sorted(scan.records):
        observations = scan.records[filename]
        session_ids = {observation.session_id for observation in observations.values()}
        if len(session_ids) != 1:
            raise ValueError(f"record group has divergent identities: {filename}")
        session_id = next(iter(session_ids))
        record_baselines = baselines_by_record.get(filename, {})
        groups = set().union(
            *(set(observation.group_values) for observation in observations.values())
        )
        groups.update(record_baselines)
        record_has_baseline = bool(record_baselines)
        desired: dict[str, str] = {}
        group_conflicts: list[RegistryConflict] = []

        for group_name in sorted(groups):
            group_baselines = record_baselines.get(group_name)
            if group_baselines is not None:
                decision, conflict = _steady_state_decision(
                    filename, group_name, observations, group_baselines
                )
            elif record_has_baseline:
                # The record is past bootstrap but this group was never
                # accepted (a standing quarantine, or a field newly classified
                # since).  Mtimes are no longer evidence for it.
                decision, conflict = _unaccepted_group_decision(
                    filename, group_name, observations
                )
            elif group_name.startswith(("protected:", "unknown:")):
                values = {
                    root_id: _value_for_group(observation, group_name)
                    for root_id, observation in observations.items()
                }
                if group_name.startswith("protected:") and len(set(values.values())) == 1:
                    decision, conflict = next(iter(values.values())), None
                else:
                    reason = (
                        "protected_linkage_divergence"
                        if group_name.startswith("protected:")
                        else "unknown_field_unclassified"
                    )
                    decision, conflict = None, RegistryConflict(
                        filename=filename,
                        group_name=group_name,
                        reason=reason,
                        candidates=MappingProxyType(values),
                    )
            else:
                decision, conflict = _bootstrap_decision(
                    filename, group_name, observations
                )
            if conflict is not None:
                conflicts.append(conflict)
                group_conflicts.append(conflict)
            elif decision is not None:
                desired[group_name] = decision

        mutations: list[RegistryMutation] = []
        for root_id, observation in observations.items():
            updated = dict(observation.record)
            changed_fields: dict[str, Mapping[str, Any]] = {}
            for group_name, value_json in desired.items():
                if _value_for_group(observation, group_name) == value_json:
                    continue
                _apply_group(updated, group_name, value_json)
                changed_fields.update(_changed_fields(group_name, value_json))
            if changed_fields:
                mutations.append(
                    RegistryMutation(
                        root_id=root_id,
                        filename=filename,
                        operation="patch",
                        expected_before_hash=observation.byte_hash,
                        after_bytes=_canonical_json(updated),
                        changed_fields=MappingProxyType(changed_fields),
                    )
                )

        missing_roots = set(scan.roots) - set(observations)
        if missing_roots and not group_conflicts:
            composite: dict[str, Any] = {"sessionId": session_id}
            for group_name, value_json in desired.items():
                _apply_group(composite, group_name, value_json)
            after_bytes = _canonical_json(composite)
            all_changed: dict[str, Mapping[str, Any]] = {}
            for group_name, value_json in desired.items():
                all_changed.update(_changed_fields(group_name, value_json))
            for root_id in sorted(missing_roots):
                mutations.append(
                    RegistryMutation(
                        root_id=root_id,
                        filename=filename,
                        operation="create",
                        expected_before_hash=None,
                        after_bytes=after_bytes,
                        changed_fields=MappingProxyType(dict(all_changed)),
                    )
                )

        for group_name, value_json in desired.items():
            prior = record_baselines.get(group_name, {})
            revision = max(
                (baseline.revision for baseline in prior.values()), default=0
            ) + 1
            for root_id in scan.roots:
                proposed.append(
                    RegistryBaseline(
                        filename=filename,
                        root_id=root_id,
                        group_name=group_name,
                        value_json=value_json,
                        revision=revision,
                    )
                )

        record_plans[filename] = RegistryRecordPlan(
            filename=filename,
            session_id=session_id,
            desired_groups=MappingProxyType(desired),
            mutations=tuple(mutations),
        )

    return RegistrySyncPlan(
        scan=scan,
        records=MappingProxyType(record_plans),
        conflicts=tuple(conflicts),
        proposed_baselines=tuple(proposed),
    )


def _mutation_path(scan: RegistryScan, mutation: RegistryMutation) -> Path:
    root = scan.roots.get(mutation.root_id)
    if root is None:
        raise RegistryMutationConflict("mutation root is not enrolled")
    if Path(mutation.filename).name != mutation.filename:
        raise RegistryMutationConflict("mutation filename is unsafe")
    return root.path / mutation.filename


def _temporary_mutation_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}")


def _publish_create_only(path: Path, data: bytes) -> None:
    temporary = _temporary_mutation_path(path)
    try:
        with temporary.open("xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as exc:
            raise RegistryMutationConflict(f"create target already exists: {path}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def apply_registry_mutation(
    scan: RegistryScan, mutation: RegistryMutation
) -> RegistryMutationResult:
    """Apply one already-durable mutation with strict filesystem preconditions.

    Callers must stage the immutable mutation in the reconciliation ledger before this
    function and verify the full replica set afterward.  This helper cannot make
    ``os.replace`` a filesystem CAS; it instead refuses any evidence mismatch observed
    immediately before replacement and leaves post-replace races to all-root verification.
    """
    path = _mutation_path(scan, mutation)
    after = mutation.after_bytes.encode("utf-8")
    after_hash = hashlib.sha256(after).hexdigest()

    if mutation.operation == "create":
        if mutation.expected_before_hash is not None:
            raise RegistryMutationConflict("create mutation has an expected before hash")
        _publish_create_only(path, after)
        if path.read_bytes() != after:
            raise RegistryMutationConflict("created record failed immediate verification")
        return RegistryMutationResult(applied=True, byte_hash=after_hash)

    if mutation.operation != "patch":
        raise RegistryMutationConflict(f"unsupported mutation operation: {mutation.operation}")
    if mutation.expected_before_hash is None:
        raise RegistryMutationConflict("patch mutation lacks an expected before hash")

    try:
        current = path.read_bytes()
    except OSError as exc:
        raise RegistryMutationConflict(f"patch target is unavailable: {path}") from exc
    current_hash = hashlib.sha256(current).hexdigest()
    if current_hash != mutation.expected_before_hash:
        raise RegistryMutationConflict(
            f"patch target does not match expected hash: {path}"
        )
    current_record = _parse_record(current, path)
    expected_session_id = mutation.filename[: -len(".json")]
    if current_record.get("sessionId") != expected_session_id:
        raise RegistryMutationConflict(f"patch target identity changed: {path}")

    temporary = _temporary_mutation_path(path)
    try:
        with temporary.open("xb") as handle:
            handle.write(after)
            handle.flush()
            os.fsync(handle.fileno())
        # Recheck immediately before replace.  This is deliberate best-effort optimistic
        # protection, not a claim that the filesystem provides compare-and-swap here.
        if path.read_bytes() != current:
            raise RegistryMutationConflict(f"patch target changed before replace: {path}")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    if path.read_bytes() != after:
        raise RegistryMutationConflict("patched record failed immediate verification")
    return RegistryMutationResult(applied=True, byte_hash=after_hash)


def verify_registry_sync_plan(
    plan: RegistrySyncPlan, observed: RegistryScan
) -> RegistryVerification:
    """Verify every accepted group value across the exact enrolled replica set."""
    if set(observed.roots) != set(plan.scan.roots):
        raise ValueError("registry root set changed before verification")
    for root_id in observed.roots:
        if (
            observed.roots[root_id].canonical_path.casefold()
            != plan.scan.roots[root_id].canonical_path.casefold()
        ):
            raise ValueError("registry root set changed before verification")

    failures: list[RegistryVerificationFailure] = []
    for filename, record_plan in plan.records.items():
        observations = observed.records.get(filename, {})
        for root_id in observed.roots:
            observation = observations.get(root_id)
            if observation is None:
                for group_name, expected in record_plan.desired_groups.items():
                    failures.append(
                        RegistryVerificationFailure(
                            filename=filename,
                            root_id=root_id,
                            group_name=group_name,
                            reason="replica_missing",
                            expected_value_json=expected,
                            observed_value_json=None,
                        )
                    )
                continue
            for group_name, expected in record_plan.desired_groups.items():
                actual = _value_for_group(observation, group_name)
                if actual != expected:
                    failures.append(
                        RegistryVerificationFailure(
                            filename=filename,
                            root_id=root_id,
                            group_name=group_name,
                            reason="intended_value_mismatch",
                            expected_value_json=expected,
                            observed_value_json=actual,
                        )
                    )
    return RegistryVerification(verified=not failures, failures=tuple(failures))
