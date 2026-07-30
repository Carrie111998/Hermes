#!/usr/bin/env python3
"""Pure policy helpers for the fork-only upstream sync routine."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

try:  # Linux/macOS production path; harmless fallback keeps Windows imports safe.
    import fcntl
except ImportError:  # pragma: no cover - Windows does not run the Cloud helper.
    fcntl = None  # type: ignore[assignment]

SHA_RE = re.compile(r"^[0-9a-f]{40}$")
STATE_SCHEMA = "muncho-auto-sync-blocker-dedupe.v2"
CANDIDATE_MANIFEST_SCHEMA = "muncho-upstream-sync-candidate.v2"
CANDIDATE_TERMINAL_SCHEMA = "muncho-upstream-sync-candidate-terminal.v1"
DEFAULT_REPEAT_AFTER_SECONDS = 24 * 60 * 60
_DELIVERY_OBSERVATIONS = frozenset({"none", "confirmed", "failed"})
_CANDIDATE_MANIFEST_FIELDS = frozenset(
    {
        "schema",
        "phase",
        "candidate_id",
        "fork_repository",
        "upstream_repository",
        "base_ref",
        "upstream_ref",
        "branch",
        "pr_number",
        "head_sha",
        "base_sha",
        "upstream_sha",
        "created_at_utc",
        "manifest_sha256",
    }
)
_CANDIDATE_TERMINAL_FIELDS = frozenset(
    {
        "schema",
        "candidate_id",
        "published_manifest_sha256",
        "fork_repository",
        "base_ref",
        "branch",
        "pr_number",
        "head_sha",
        "terminal_state",
        "observed_base_sha",
        "base_contains_head_sha",
        "created_at_utc",
        "receipt_sha256",
    }
)


class CandidateManifestError(ValueError):
    """The private candidate identity record is missing or inconsistent."""


def _valid_sha(value: str | None) -> bool:
    return bool(value and SHA_RE.fullmatch(value))


def classify_stale_candidate(
    *,
    head_already_in_fork_main: bool,
    upstream_snapshot_sha: str | None,
    upstream_snapshot_in_fork_merge_base: bool,
    current_upstream_sha: str | None,
    current_upstream_contains_snapshot: bool,
) -> str | None:
    """Return a bounded terminal stale reason after exact manifest validation.

    A newer upstream ref never invalidates an open, exact-SHA review
    candidate. Replacing it on every scheduled run would restart conflict
    review and CI indefinitely on a fast-moving upstream. New commits remain
    factual tail drift and are picked up by the next candidate after the
    current one reaches a terminal state.

    The snapshot fields stay in this interface because callers already collect
    them as exact observations. They are deliberately not used as authority
    to close an active review candidate.
    """

    if head_already_in_fork_main:
        return "head_already_in_fork_main"
    if upstream_snapshot_in_fork_merge_base:
        return "upstream_snapshot_already_in_fork_merge_base"
    return None


def _candidate_manifest_digest(payload: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        dict(payload),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _require_manifest_string(
    name: str,
    value: Any,
    *,
    maximum: int,
) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > maximum
        or "\x00" in value
        or "\n" in value
        or "\r" in value
    ):
        raise CandidateManifestError(f"candidate_manifest_invalid_{name}")
    return value


def build_candidate_manifest(
    *,
    candidate_id: str,
    fork_repository: str,
    upstream_repository: str,
    base_ref: str,
    upstream_ref: str,
    branch: str,
    pr_number: int,
    head_sha: str,
    base_sha: str,
    upstream_sha: str,
    created_at_utc: str,
) -> dict[str, Any]:
    """Build the canonical private identity record for one candidate PR.

    Every identifier is preserved byte-for-byte.  Validation is limited to
    protocol shape; no title, body, branch prefix, or other authored text is
    interpreted as ownership evidence.
    """

    if (
        not isinstance(candidate_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", candidate_id) is None
    ):
        raise CandidateManifestError("candidate_manifest_invalid_candidate_id")
    exact_fork_repository = _require_manifest_string(
        "fork_repository", fork_repository, maximum=256
    )
    exact_upstream_repository = _require_manifest_string(
        "upstream_repository", upstream_repository, maximum=256
    )
    exact_base_ref = _require_manifest_string("base_ref", base_ref, maximum=256)
    exact_upstream_ref = _require_manifest_string(
        "upstream_ref", upstream_ref, maximum=256
    )
    exact_branch = _require_manifest_string("branch", branch, maximum=1024)
    if type(pr_number) is not int or pr_number <= 0:
        raise CandidateManifestError("candidate_manifest_invalid_pr_number")
    for name, value in (
        ("head_sha", head_sha),
        ("base_sha", base_sha),
        ("upstream_sha", upstream_sha),
    ):
        if not isinstance(value, str) or not _valid_sha(value):
            raise CandidateManifestError(f"candidate_manifest_invalid_{name}")
    exact_created_at = _require_manifest_string(
        "created_at_utc", created_at_utc, maximum=80
    )
    if _parse_utc(exact_created_at) is None:
        raise CandidateManifestError("candidate_manifest_invalid_created_at_utc")

    unsigned: dict[str, Any] = {
        "schema": CANDIDATE_MANIFEST_SCHEMA,
        "phase": "published",
        "candidate_id": candidate_id,
        "fork_repository": exact_fork_repository,
        "upstream_repository": exact_upstream_repository,
        "base_ref": exact_base_ref,
        "upstream_ref": exact_upstream_ref,
        "branch": exact_branch,
        "pr_number": pr_number,
        "head_sha": head_sha,
        "base_sha": base_sha,
        "upstream_sha": upstream_sha,
        "created_at_utc": exact_created_at,
    }
    return {
        **unsigned,
        "manifest_sha256": _candidate_manifest_digest(unsigned),
    }


def build_prepared_candidate_manifest(
    *,
    candidate_id: str,
    fork_repository: str,
    upstream_repository: str,
    base_ref: str,
    upstream_ref: str,
    branch: str,
    head_sha: str,
    base_sha: str,
    upstream_sha: str,
    created_at_utc: str,
) -> dict[str, Any]:
    """Build fail-closed state before the first fork mutation.

    A prepared state is not PR ownership: it deliberately has no PR number.
    It prevents another run from creating a lookalike or duplicate candidate
    after a crash between branch push, PR creation, and final publication.
    """

    published_shape = build_candidate_manifest(
        candidate_id=candidate_id,
        fork_repository=fork_repository,
        upstream_repository=upstream_repository,
        base_ref=base_ref,
        upstream_ref=upstream_ref,
        branch=branch,
        pr_number=1,
        head_sha=head_sha,
        base_sha=base_sha,
        upstream_sha=upstream_sha,
        created_at_utc=created_at_utc,
    )
    unsigned = {
        key: value
        for key, value in published_shape.items()
        if key != "manifest_sha256"
    }
    unsigned["phase"] = "prepared"
    unsigned["pr_number"] = None
    return {
        **unsigned,
        "manifest_sha256": _candidate_manifest_digest(unsigned),
    }


def publish_candidate_manifest(
    prepared: Mapping[str, Any],
    *,
    pr_number: int,
) -> dict[str, Any]:
    """Transition an exact prepared record to an exact published record."""

    exact = validate_candidate_manifest(prepared)
    if exact["phase"] != "prepared":
        raise CandidateManifestError("candidate_manifest_not_prepared")
    return build_candidate_manifest(
        candidate_id=exact["candidate_id"],
        fork_repository=exact["fork_repository"],
        upstream_repository=exact["upstream_repository"],
        base_ref=exact["base_ref"],
        upstream_ref=exact["upstream_ref"],
        branch=exact["branch"],
        pr_number=pr_number,
        head_sha=exact["head_sha"],
        base_sha=exact["base_sha"],
        upstream_sha=exact["upstream_sha"],
        created_at_utc=exact["created_at_utc"],
    )


def validate_candidate_manifest(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate an exact candidate manifest and its canonical digest."""

    if not isinstance(value, Mapping) or set(value) != _CANDIDATE_MANIFEST_FIELDS:
        raise CandidateManifestError("candidate_manifest_invalid_fields")
    if value.get("schema") != CANDIDATE_MANIFEST_SCHEMA:
        raise CandidateManifestError("candidate_manifest_invalid_schema")
    phase = value.get("phase")
    if phase not in {"prepared", "published"}:
        raise CandidateManifestError("candidate_manifest_invalid_phase")
    candidate_id = value.get("candidate_id")
    if (
        not isinstance(candidate_id, str)
        or re.fullmatch(r"[0-9a-f]{64}", candidate_id) is None
    ):
        raise CandidateManifestError("candidate_manifest_invalid_candidate_id")
    pr_number = value.get("pr_number")
    if phase == "prepared":
        if pr_number is not None:
            raise CandidateManifestError(
                "candidate_manifest_prepared_pr_number_must_be_null"
            )
        rebuilt = build_prepared_candidate_manifest(
            candidate_id=candidate_id,
            fork_repository=value.get("fork_repository"),
            upstream_repository=value.get("upstream_repository"),
            base_ref=value.get("base_ref"),
            upstream_ref=value.get("upstream_ref"),
            branch=value.get("branch"),
            head_sha=value.get("head_sha"),
            base_sha=value.get("base_sha"),
            upstream_sha=value.get("upstream_sha"),
            created_at_utc=value.get("created_at_utc"),
        )
    else:
        rebuilt = build_candidate_manifest(
            candidate_id=candidate_id,
            fork_repository=value.get("fork_repository"),
            upstream_repository=value.get("upstream_repository"),
            base_ref=value.get("base_ref"),
            upstream_ref=value.get("upstream_ref"),
            branch=value.get("branch"),
            pr_number=pr_number,
            head_sha=value.get("head_sha"),
            base_sha=value.get("base_sha"),
            upstream_sha=value.get("upstream_sha"),
            created_at_utc=value.get("created_at_utc"),
        )
    if value.get("manifest_sha256") != rebuilt["manifest_sha256"]:
        raise CandidateManifestError("candidate_manifest_digest_mismatch")
    return dict(value)


def load_candidate_manifest(path: Path) -> dict[str, Any]:
    """Load a private regular-file manifest without following symlinks."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError as exc:
        raise CandidateManifestError("candidate_manifest_missing") from exc
    except OSError as exc:
        raise CandidateManifestError("candidate_manifest_unreadable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > 16 * 1024
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.geteuid()
        ):
            raise CandidateManifestError("candidate_manifest_file_invalid")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as stream:
            descriptor = -1
            try:
                value = json.load(stream)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CandidateManifestError(
                    "candidate_manifest_json_invalid"
                ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return validate_candidate_manifest(value)


def write_candidate_manifest(path: Path, value: Mapping[str, Any]) -> None:
    """Atomically persist an already complete canonical private manifest."""

    manifest = validate_candidate_manifest(value)
    _atomic_write_private(path, manifest)


def clear_candidate_manifest(path: Path) -> None:
    """Remove the exact private manifest after terminal reconciliation."""

    try:
        path.unlink()
    except FileNotFoundError:
        return
    _fsync_directory(path.parent)


def candidate_ledger_path(path: Path) -> Path:
    """Return the private append-only ledger directory for one state pointer."""

    return path.with_name(f"{path.name}.ledger")


def _prepared_from_published(
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    exact = validate_candidate_manifest(manifest)
    return build_prepared_candidate_manifest(
        candidate_id=exact["candidate_id"],
        fork_repository=exact["fork_repository"],
        upstream_repository=exact["upstream_repository"],
        base_ref=exact["base_ref"],
        upstream_ref=exact["upstream_ref"],
        branch=exact["branch"],
        head_sha=exact["head_sha"],
        base_sha=exact["base_sha"],
        upstream_sha=exact["upstream_sha"],
        created_at_utc=exact["created_at_utc"],
    )


def _ledger_manifest_file(
    ledger: Path,
    manifest: Mapping[str, Any],
) -> Path:
    return ledger / (
        f"{manifest['candidate_id']}.{manifest['phase']}."
        f"{manifest['manifest_sha256']}.json"
    )


def _ledger_terminal_file(
    ledger: Path,
    receipt: Mapping[str, Any],
) -> Path:
    return ledger / (
        f"{receipt['candidate_id']}.terminal."
        f"{receipt['receipt_sha256']}.json"
    )


def _read_private_mapping(path: Path, *, maximum: int = 16 * 1024) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CandidateManifestError("candidate_ledger_entry_unreadable") from exc
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size > maximum
            or metadata.st_mode & 0o077
            or metadata.st_uid != os.geteuid()
        ):
            raise CandidateManifestError("candidate_ledger_entry_invalid")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as stream:
            descriptor = -1
            try:
                value = json.load(stream)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise CandidateManifestError(
                    "candidate_ledger_entry_json_invalid"
                ) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise CandidateManifestError("candidate_ledger_entry_invalid")
    return value


def _ensure_private_ledger_directory(path: Path) -> None:
    try:
        _ensure_private_directory(path)
    except RuntimeError as exc:
        raise CandidateManifestError(
            "candidate_ledger_directory_invalid"
        ) from exc


def _append_immutable_private(path: Path, value: Mapping[str, Any]) -> None:
    if os.path.lexists(path):
        if _read_private_mapping(path) != dict(value):
            raise CandidateManifestError("candidate_ledger_entry_mismatch")
        return
    _atomic_write_private(path, value)


def append_candidate_manifest(
    pointer: Path,
    value: Mapping[str, Any],
) -> dict[str, Any]:
    """Append exact candidate identity before updating the mutable pointer.

    The ledger is the recovery authority. A crash can therefore leave either
    the old pointer or no pointer at all without losing the exact candidate
    head/branch identity that preceded the first fork mutation.
    """

    manifest = validate_candidate_manifest(value)
    _ensure_private_directory(pointer.parent)
    ledger = candidate_ledger_path(pointer)
    _ensure_private_ledger_directory(ledger)
    if manifest["phase"] == "published":
        prepared = _prepared_from_published(manifest)
        _append_immutable_private(
            _ledger_manifest_file(ledger, prepared),
            prepared,
        )
    _append_immutable_private(
        _ledger_manifest_file(ledger, manifest),
        manifest,
    )
    write_candidate_manifest(pointer, manifest)
    return manifest


def build_candidate_terminal_receipt(
    manifest: Mapping[str, Any],
    *,
    observed_base_sha: str,
    created_at_utc: str,
) -> dict[str, Any]:
    """Bind exact MERGED proof before retiring an active ledger candidate."""

    exact = validate_candidate_manifest(manifest)
    if exact["phase"] != "published":
        raise CandidateManifestError("candidate_terminal_manifest_not_published")
    if not _valid_sha(observed_base_sha):
        raise CandidateManifestError("candidate_terminal_base_sha_invalid")
    if _parse_utc(created_at_utc) is None:
        raise CandidateManifestError("candidate_terminal_created_at_invalid")
    unsigned: dict[str, Any] = {
        "schema": CANDIDATE_TERMINAL_SCHEMA,
        "candidate_id": exact["candidate_id"],
        "published_manifest_sha256": exact["manifest_sha256"],
        "fork_repository": exact["fork_repository"],
        "base_ref": exact["base_ref"],
        "branch": exact["branch"],
        "pr_number": exact["pr_number"],
        "head_sha": exact["head_sha"],
        "terminal_state": "MERGED",
        "observed_base_sha": observed_base_sha,
        "base_contains_head_sha": True,
        "created_at_utc": created_at_utc,
    }
    return {
        **unsigned,
        "receipt_sha256": _candidate_manifest_digest(unsigned),
    }


def validate_candidate_terminal_receipt(
    value: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _CANDIDATE_TERMINAL_FIELDS:
        raise CandidateManifestError("candidate_terminal_invalid_fields")
    unsigned = {
        key: item for key, item in value.items() if key != "receipt_sha256"
    }
    if (
        value.get("schema") != CANDIDATE_TERMINAL_SCHEMA
        or re.fullmatch(r"[0-9a-f]{64}", str(value.get("candidate_id"))) is None
        or re.fullmatch(
            r"[0-9a-f]{64}",
            str(value.get("published_manifest_sha256")),
        )
        is None
        or type(value.get("pr_number")) is not int
        or value["pr_number"] <= 0
        or not _valid_sha(value.get("head_sha"))
        or not _valid_sha(value.get("observed_base_sha"))
        or value.get("terminal_state") != "MERGED"
        or value.get("base_contains_head_sha") is not True
        or _parse_utc(value.get("created_at_utc")) is None
        or value.get("receipt_sha256") != _candidate_manifest_digest(unsigned)
    ):
        raise CandidateManifestError("candidate_terminal_invalid")
    for name in ("fork_repository", "base_ref", "branch"):
        _require_manifest_string(name, value.get(name), maximum=1024)
    return dict(value)


def append_candidate_terminal_receipt(
    pointer: Path,
    manifest: Mapping[str, Any],
    *,
    observed_base_sha: str,
    created_at_utc: str,
) -> dict[str, Any]:
    exact = validate_candidate_manifest(manifest)
    receipt = build_candidate_terminal_receipt(
        exact,
        observed_base_sha=observed_base_sha,
        created_at_utc=created_at_utc,
    )
    ledger = candidate_ledger_path(pointer)
    _ensure_private_ledger_directory(ledger)
    published_path = _ledger_manifest_file(ledger, exact)
    if not os.path.lexists(published_path):
        raise CandidateManifestError("candidate_terminal_manifest_missing")
    if load_candidate_manifest(published_path) != exact:
        raise CandidateManifestError("candidate_terminal_manifest_mismatch")
    _append_immutable_private(
        _ledger_terminal_file(ledger, receipt),
        receipt,
    )
    clear_candidate_manifest(pointer)
    return receipt


def _validate_ledger_transition(
    prepared: Mapping[str, Any],
    published: Mapping[str, Any],
) -> None:
    expected = _prepared_from_published(published)
    if dict(prepared) != expected:
        raise CandidateManifestError("candidate_ledger_transition_invalid")


def recover_candidate_manifest(pointer: Path) -> dict[str, Any] | None:
    """Recover the sole active candidate from exact private ledger facts.

    Titles, bodies, labels, branch prefixes, and unrelated pull requests never
    participate. Corrupt or ambiguous ledger state blocks rather than guessing.
    """

    ledger = candidate_ledger_path(pointer)
    pointer_exists = os.path.lexists(pointer)
    if not os.path.lexists(ledger):
        if not pointer_exists:
            return None
        manifest = load_candidate_manifest(pointer)
        return append_candidate_manifest(pointer, manifest)
    _ensure_private_ledger_directory(ledger)

    manifests: dict[str, dict[str, dict[str, Any]]] = {}
    terminals: dict[str, dict[str, Any]] = {}
    for entry in sorted(ledger.iterdir(), key=lambda item: item.name):
        raw = _read_private_mapping(entry)
        if raw.get("schema") == CANDIDATE_MANIFEST_SCHEMA:
            manifest = validate_candidate_manifest(raw)
            expected_name = _ledger_manifest_file(ledger, manifest).name
            if entry.name != expected_name:
                raise CandidateManifestError("candidate_ledger_filename_invalid")
            phases = manifests.setdefault(manifest["candidate_id"], {})
            if manifest["phase"] in phases:
                raise CandidateManifestError("candidate_ledger_duplicate_phase")
            phases[manifest["phase"]] = manifest
        elif raw.get("schema") == CANDIDATE_TERMINAL_SCHEMA:
            receipt = validate_candidate_terminal_receipt(raw)
            expected_name = _ledger_terminal_file(ledger, receipt).name
            if entry.name != expected_name:
                raise CandidateManifestError("candidate_ledger_filename_invalid")
            if receipt["candidate_id"] in terminals:
                raise CandidateManifestError("candidate_ledger_duplicate_terminal")
            terminals[receipt["candidate_id"]] = receipt
        else:
            raise CandidateManifestError("candidate_ledger_schema_invalid")

    active: list[dict[str, Any]] = []
    terminal_manifests: list[dict[str, Any]] = []
    for candidate_id, phases in manifests.items():
        if set(phases) - {"prepared", "published"} or "prepared" not in phases:
            raise CandidateManifestError("candidate_ledger_phase_chain_invalid")
        prepared = phases["prepared"]
        published = phases.get("published")
        if published is not None:
            _validate_ledger_transition(prepared, published)
        terminal = terminals.get(candidate_id)
        if terminal is not None:
            if (
                published is None
                or terminal["published_manifest_sha256"]
                != published["manifest_sha256"]
                or terminal["fork_repository"] != published["fork_repository"]
                or terminal["base_ref"] != published["base_ref"]
                or terminal["branch"] != published["branch"]
                or terminal["pr_number"] != published["pr_number"]
                or terminal["head_sha"] != published["head_sha"]
            ):
                raise CandidateManifestError("candidate_ledger_terminal_mismatch")
            terminal_manifests.append(published)
        else:
            active.append(published or prepared)
    if set(terminals) - set(manifests):
        raise CandidateManifestError("candidate_ledger_orphan_terminal")
    if len(active) > 1:
        raise CandidateManifestError("candidate_ledger_multiple_active")

    if active:
        selected = active[0]
        if pointer_exists:
            current = load_candidate_manifest(pointer)
            if current != selected:
                if (
                    current.get("phase") == "prepared"
                    and selected.get("phase") == "published"
                ):
                    _validate_ledger_transition(current, selected)
                    # Crash-safe forward repair: the published ledger event was
                    # fsynced before the mutable pointer replacement. An exact
                    # prior prepared pointer is therefore advanced, never
                    # treated as an ambiguous second authority.
                    write_candidate_manifest(pointer, selected)
                else:
                    raise CandidateManifestError(
                        "candidate_pointer_ledger_mismatch"
                    )
        else:
            write_candidate_manifest(pointer, selected)
        return selected

    if pointer_exists:
        current = load_candidate_manifest(pointer)
        if current not in terminal_manifests:
            raise CandidateManifestError("candidate_pointer_without_active_ledger")
        clear_candidate_manifest(pointer)
    return None


@contextmanager
def candidate_manifest_lock(path: Path) -> Iterator[None]:
    """Serialize candidate discovery, creation, and manifest publication."""

    with _locked_state(path):
        yield


def blocker_fingerprint(
    *,
    status: str,
    pr_number: int | None,
    head_sha: str | None,
    blockers: Iterable[str],
    failed_checks: Iterable[Mapping[str, Any]],
) -> str:
    """Build a stable fingerprint without storing raw logs or PR bodies."""

    normalized_checks = sorted(
        {
            (
                row.get("name")[:160],
                row.get("conclusion")[:40],
            )
            for row in failed_checks
            if isinstance(row.get("name"), str)
            and isinstance(row.get("conclusion"), str)
        }
    )
    payload = {
        "status": status[:120] if isinstance(status, str) else "",
        "pr_number": pr_number if type(pr_number) is int else None,
        "head_sha": head_sha if _valid_sha(head_sha) else None,
        "blockers": sorted(
            {item[:120] for item in blockers if isinstance(item, str)}
        ),
        "failed_checks": normalized_checks,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("schema") != STATE_SCHEMA:
        return {}
    if type(data.get("active")) is not bool:
        return {}
    if data["active"] is False:
        return (
            data
            if set(data) == {"schema", "active", "cleared_at"}
            and _parse_utc(data.get("cleared_at")) is not None
            else {}
        )
    if set(data) != {
        "schema",
        "active",
        "fingerprint",
        "last_seen_at",
        "last_selected_for_delivery_at",
        "last_delivery_confirmed_at",
        "pending_delivery",
        "suppressed_runs",
    }:
        return {}
    fingerprint = data.get("fingerprint")
    selected_at = data.get("last_selected_for_delivery_at")
    confirmed_at = data.get("last_delivery_confirmed_at")
    suppressed_runs = data.get("suppressed_runs")
    pending = data.get("pending_delivery")
    if (
        not isinstance(fingerprint, str)
        or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None
        or _parse_utc(data.get("last_seen_at")) is None
        or _parse_utc(selected_at) is None
        or (confirmed_at is not None and _parse_utc(confirmed_at) is None)
        or type(suppressed_runs) is not int
        or suppressed_runs < 0
    ):
        return {}
    if pending is not None:
        if not isinstance(pending, dict) or set(pending) != {
            "fingerprint",
            "selected_at",
            "observed_previous_run_at",
        }:
            return {}
        baseline = pending.get("observed_previous_run_at")
        if (
            pending.get("fingerprint") != fingerprint
            or _parse_utc(pending.get("selected_at")) is None
            or (
                baseline is not None
                and (not isinstance(baseline, str) or not (0 < len(baseline) <= 80))
            )
        ):
            return {}
    return data


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        metadata = path.lstat()
    except OSError as exc:
        raise RuntimeError("invalid private state directory") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise RuntimeError("invalid private state directory")


@contextmanager
def _locked_state(path: Path) -> Iterator[None]:
    """Serialize the state read/modify/write cycle across cron processes."""

    _ensure_private_directory(path.parent)
    lock_path = path.with_name(f".{path.name}.lock")
    flags = (
        os.O_RDWR
        | os.O_CREAT
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != os.geteuid()
            or stat.S_IMODE(metadata.st_mode) & 0o077
        ):
            raise RuntimeError("invalid blocker state lock")
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _atomic_write_private(path: Path, payload: Mapping[str, Any]) -> None:
    _ensure_private_directory(path.parent)
    raw = (json.dumps(dict(payload), indent=2, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    tmp = path.with_name(
        f".{path.name}.{os.getpid()}.{os.urandom(8).hex()}.tmp"
    )
    descriptor = -1
    try:
        descriptor = os.open(tmp, flags, 0o600)
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        tmp.unlink(missing_ok=True)


def decide_blocker_delivery(
    state_path: Path,
    *,
    fingerprint: str,
    now: datetime | None = None,
    repeat_after_seconds: int = DEFAULT_REPEAT_AFTER_SECONDS,
    observed_previous_run_at: str | None = None,
    previous_delivery_status: str | None = None,
) -> dict[str, Any]:
    """Persist and return whether this run should print a blocker notification.

    ``emit`` means only "selected for downstream delivery".  A delivery is
    recorded as confirmed on the *next* cron invocation, after the scheduler's
    run-bound ``last_delivery_status`` / ``last_delivery_confirmed_at`` pair
    proves that every resolved target returned an explicit success receipt.
    This prevents state from claiming a Discord delivery merely because no
    transport exception was observed.
    """

    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ValueError("invalid blocker fingerprint")
    if repeat_after_seconds < 60:
        raise ValueError("repeat_after_seconds must be at least 60")
    if previous_delivery_status not in _DELIVERY_OBSERVATIONS | {None}:
        raise ValueError("invalid previous delivery status")
    if observed_previous_run_at is not None:
        if not isinstance(observed_previous_run_at, str) or not (
            0 < len(observed_previous_run_at) <= 80
        ):
            raise ValueError("invalid observed previous run timestamp")
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with _locked_state(state_path):
        state = _load_state(state_path)
        previous = state.get("fingerprint")
        pending = state.get("pending_delivery")
        if not isinstance(pending, dict):
            pending = None

        confirmed_at = state.get("last_delivery_confirmed_at")
        prior_delivery_failed = False
        prior_delivery_reconciled = False
        instrumented = previous_delivery_status in _DELIVERY_OBSERVATIONS
        if pending and pending.get("fingerprint") == previous:
            baseline = pending.get("observed_previous_run_at")
            completion_observed = (
                observed_previous_run_at is not None
                and observed_previous_run_at != baseline
            )
            if completion_observed and previous_delivery_status == "confirmed":
                confirmed_at = _iso(current)
                pending = None
                prior_delivery_reconciled = True
            elif completion_observed and previous_delivery_status == "failed":
                pending = None
                prior_delivery_failed = True
                prior_delivery_reconciled = True

        last_confirmed = _parse_utc(confirmed_at)
        last_selected = _parse_utc(state.get("last_selected_for_delivery_at"))
        confirmed_age = (
            (current - last_confirmed).total_seconds() if last_confirmed else None
        )
        selected_age = (
            (current - last_selected).total_seconds() if last_selected else None
        )

        if previous != fingerprint:
            emit, reason = True, "new_or_changed_blocker"
            suppressed_runs = 0
            confirmed_at = None
            pending = None
        elif prior_delivery_failed:
            emit, reason = True, "previous_delivery_failed_retry"
            suppressed_runs = int(state.get("suppressed_runs") or 0)
        elif pending is not None and instrumented:
            # The scheduler did not persist completion of the selected run.
            # Retry rather than inventing a delivery receipt.
            emit, reason = True, "previous_delivery_unconfirmed_retry"
            suppressed_runs = int(state.get("suppressed_runs") or 0)
        elif confirmed_age is not None and confirmed_age >= repeat_after_seconds:
            emit, reason = True, "repeat_window_elapsed"
            suppressed_runs = int(state.get("suppressed_runs") or 0)
        elif confirmed_age is not None:
            emit, reason = False, "unchanged_delivered_blocker_suppressed"
            suppressed_runs = int(state.get("suppressed_runs") or 0) + 1
        elif selected_age is None or selected_age >= repeat_after_seconds:
            emit, reason = True, "unconfirmed_repeat_window_elapsed"
            suppressed_runs = int(state.get("suppressed_runs") or 0)
        else:
            # Compatibility for a direct/manual invocation that has no generic
            # scheduler observation variables.  It suppresses noise but never
            # records the selection as a real platform delivery.
            emit, reason = False, "unchanged_selection_suppressed_unconfirmed"
            suppressed_runs = int(state.get("suppressed_runs") or 0) + 1

        selected_at = state.get("last_selected_for_delivery_at")
        if emit:
            selected_at = _iso(current)
            pending = {
                "fingerprint": fingerprint,
                "selected_at": selected_at,
                "observed_previous_run_at": observed_previous_run_at,
            }
        payload = {
            "schema": STATE_SCHEMA,
            "active": True,
            "fingerprint": fingerprint,
            "last_seen_at": _iso(current),
            "last_selected_for_delivery_at": selected_at,
            "last_delivery_confirmed_at": confirmed_at,
            "pending_delivery": pending,
            "suppressed_runs": suppressed_runs,
        }
        _atomic_write_private(state_path, payload)
        return {
            "emit": emit,
            "reason": reason,
            "suppressed_runs": suppressed_runs,
            "repeat_after_seconds": repeat_after_seconds,
            "delivery_confirmed_at": confirmed_at,
            "pending_delivery": pending is not None,
            "prior_delivery_reconciled": prior_delivery_reconciled,
        }


def clear_blocker_delivery_state(
    state_path: Path, *, now: datetime | None = None
) -> None:
    """Mark the blocker inactive so recurrence is treated as a new event."""

    if not state_path.exists():
        return
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    with _locked_state(state_path):
        _atomic_write_private(
            state_path,
            {"schema": STATE_SCHEMA, "active": False, "cleared_at": _iso(current)},
        )
