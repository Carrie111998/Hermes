"""Strict source metadata for the fork-local Muncho release alias.

Only typed metadata, formatting, integrity, and exact version/SHA mappings
live here.  Free-form release-note text is carried as opaque display data; it
is never classified or used to choose behavior.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


METADATA_SCHEMA = "muncho-release-metadata.v1"
NOTES_SCHEMA = "muncho-release-notes.v1"
HISTORY_SCHEMA = "muncho-release-history.v1"
MAX_SOURCE_FILE_BYTES = 128 * 1024

PACKAGE_ROOT = Path(__file__).resolve().parent
DEFAULT_METADATA_PATH = PACKAGE_ROOT / "metadata.json"
DEFAULT_HISTORY_PATH = PACKAGE_ROOT / "history.json"

_SEMVER = re.compile(r"^(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})$")
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

# These are immutable historical facts.  Keeping them as a required prefix
# means a future append can never silently rewrite either pre-metadata release.
REQUIRED_HISTORY_PREFIX = (
    (
        "2.3.0",
        "62fbf327b3507a97a34807bf4834d35c396817de",
        "retrospective_baseline",
        False,
    ),
    (
        "2.3.1",
        "5564ec24a48d819e8ba0dd924bdb82ca5064ed4c",
        "retrospective_release",
        False,
    ),
)


class ReleaseMetadataError(RuntimeError):
    """Stable fail-closed error at the Muncho release metadata boundary."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ReleaseMetadataError("muncho_release_json_invalid") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _identity(value: os.stat_result) -> tuple[int, ...]:
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


def _read_canonical_json(path: Path, *, missing_ok: bool = False) -> Any:
    try:
        before = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        raise ReleaseMetadataError("muncho_release_metadata_missing") from None
    except OSError as exc:
        raise ReleaseMetadataError("muncho_release_metadata_unavailable") from exc
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or not 0 < before.st_size <= MAX_SOURCE_FILE_BYTES
    ):
        raise ReleaseMetadataError("muncho_release_metadata_file_invalid")
    try:
        raw = path.read_bytes()
        after = path.lstat()
    except OSError as exc:
        raise ReleaseMetadataError("muncho_release_metadata_unavailable") from exc
    if _identity(before) != _identity(after) or len(raw) != after.st_size:
        raise ReleaseMetadataError("muncho_release_metadata_changed")
    try:
        value = json.loads(raw.decode("ascii", errors="strict"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseMetadataError("muncho_release_metadata_json_invalid") from exc
    if raw != canonical_bytes(value) + b"\n":
        raise ReleaseMetadataError("muncho_release_metadata_not_canonical")
    return value


def _mapping(value: Any, fields: frozenset[str], code: str) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ReleaseMetadataError(code)
    return dict(value)


def _self_hashed(
    value: Any,
    *,
    fields: frozenset[str],
    digest_field: str,
    code: str,
) -> dict[str, Any]:
    raw = _mapping(value, fields, code)
    digest = raw[digest_field]
    unsigned = {key: item for key, item in raw.items() if key != digest_field}
    if (
        not isinstance(digest, str)
        or _SHA256.fullmatch(digest) is None
        or digest != sha256_bytes(canonical_bytes(unsigned))
    ):
        raise ReleaseMetadataError(code)
    return raw


def _display_text(value: Any, *, code: str, maximum: int = 240) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > maximum
        or len(value.encode("utf-8", errors="strict")) > maximum * 4
        or _CONTROL.search(value) is not None
    ):
        raise ReleaseMetadataError(code)
    return value


@dataclass(frozen=True, order=True)
class SemVer:
    major: int
    minor: int
    patch: int

    @classmethod
    def parse(cls, value: Any) -> "SemVer":
        if not isinstance(value, str):
            raise ReleaseMetadataError("muncho_release_version_invalid")
        match = _SEMVER.fullmatch(value)
        if match is None:
            raise ReleaseMetadataError("muncho_release_version_invalid")
        return cls(*(int(part) for part in match.groups()))

    def __str__(self) -> str:
        return f"{self.major}.{self.minor}.{self.patch}"


@dataclass(frozen=True)
class ReleaseNotes:
    changes: tuple[str, ...]
    known_limitations: tuple[str, ...]
    rollback_note: str | None


@dataclass(frozen=True)
class ReleaseMetadata:
    version: SemVer
    notes: ReleaseNotes
    metadata_sha256: str


@dataclass(frozen=True)
class ReleaseHistoryEntry:
    version: SemVer
    release_sha: str
    record_kind: str
    metadata_present_at_source: bool


@dataclass(frozen=True)
class ReleaseHistory:
    releases: tuple[ReleaseHistoryEntry, ...]
    history_sha256: str

    def by_version(self) -> dict[str, ReleaseHistoryEntry]:
        return {str(entry.version): entry for entry in self.releases}


@dataclass(frozen=True)
class ReleaseBundle:
    metadata: ReleaseMetadata
    history: ReleaseHistory


_NOTES_FIELDS = frozenset({"schema", "changes", "known_limitations", "rollback_note"})
_METADATA_FIELDS = frozenset({"schema", "version", "notes", "metadata_sha256"})
_HISTORY_ENTRY_FIELDS = frozenset({
    "version",
    "release_sha",
    "record_kind",
    "metadata_present_at_source",
})
_HISTORY_FIELDS = frozenset({"schema", "releases", "history_sha256"})
_RECORD_KINDS = frozenset({
    "retrospective_baseline",
    "retrospective_release",
    "source_release",
})


def validate_release_notes(value: Any) -> ReleaseNotes:
    raw = _mapping(value, _NOTES_FIELDS, "muncho_release_notes_invalid")
    changes = raw.get("changes")
    limitations = raw.get("known_limitations")
    rollback = raw.get("rollback_note")
    if (
        raw.get("schema") != NOTES_SCHEMA
        or not isinstance(changes, list)
        or not 3 <= len(changes) <= 6
        or not isinstance(limitations, list)
        or len(limitations) > 4
        or rollback is not None
        and not isinstance(rollback, str)
    ):
        raise ReleaseMetadataError("muncho_release_notes_invalid")
    checked_changes = tuple(
        _display_text(item, code="muncho_release_notes_invalid") for item in changes
    )
    checked_limitations = tuple(
        _display_text(item, code="muncho_release_notes_invalid") for item in limitations
    )
    checked_rollback = (
        None
        if rollback is None
        else _display_text(
            rollback,
            code="muncho_release_notes_invalid",
            maximum=360,
        )
    )
    if len(set(checked_changes)) != len(checked_changes) or len(
        set(checked_limitations)
    ) != len(checked_limitations):
        raise ReleaseMetadataError("muncho_release_notes_invalid")
    return ReleaseNotes(
        changes=checked_changes,
        known_limitations=checked_limitations,
        rollback_note=checked_rollback,
    )


def validate_release_metadata(value: Any) -> ReleaseMetadata:
    raw = _self_hashed(
        value,
        fields=_METADATA_FIELDS,
        digest_field="metadata_sha256",
        code="muncho_release_metadata_invalid",
    )
    if raw.get("schema") != METADATA_SCHEMA:
        raise ReleaseMetadataError("muncho_release_metadata_invalid")
    return ReleaseMetadata(
        version=SemVer.parse(raw.get("version")),
        notes=validate_release_notes(raw.get("notes")),
        metadata_sha256=raw["metadata_sha256"],
    )


def validate_release_history(value: Any) -> ReleaseHistory:
    raw = _self_hashed(
        value,
        fields=_HISTORY_FIELDS,
        digest_field="history_sha256",
        code="muncho_release_history_invalid",
    )
    releases = raw.get("releases")
    if raw.get("schema") != HISTORY_SCHEMA or not isinstance(releases, list):
        raise ReleaseMetadataError("muncho_release_history_invalid")
    checked: list[ReleaseHistoryEntry] = []
    for item in releases:
        entry = _mapping(
            item,
            _HISTORY_ENTRY_FIELDS,
            "muncho_release_history_invalid",
        )
        version = SemVer.parse(entry.get("version"))
        release_sha = entry.get("release_sha")
        record_kind = entry.get("record_kind")
        metadata_present = entry.get("metadata_present_at_source")
        if (
            not isinstance(release_sha, str)
            or _SHA40.fullmatch(release_sha) is None
            or record_kind not in _RECORD_KINDS
            or type(metadata_present) is not bool
            or record_kind.startswith("retrospective_")
            and metadata_present is not False
            or record_kind == "source_release"
            and metadata_present is not True
        ):
            raise ReleaseMetadataError("muncho_release_history_invalid")
        checked.append(
            ReleaseHistoryEntry(
                version=version,
                release_sha=release_sha,
                record_kind=record_kind,
                metadata_present_at_source=metadata_present,
            )
        )
    versions = [str(item.version) for item in checked]
    if len(versions) != len(set(versions)):
        raise ReleaseMetadataError("muncho_release_version_reused")
    if any(left.version >= right.version for left, right in zip(checked, checked[1:])):
        raise ReleaseMetadataError("muncho_release_history_not_append_only")
    observed_prefix = tuple(
        (
            str(entry.version),
            entry.release_sha,
            entry.record_kind,
            entry.metadata_present_at_source,
        )
        for entry in checked[: len(REQUIRED_HISTORY_PREFIX)]
    )
    if observed_prefix != REQUIRED_HISTORY_PREFIX:
        raise ReleaseMetadataError("muncho_release_history_baseline_changed")
    return ReleaseHistory(tuple(checked), raw["history_sha256"])


def _paths(release_root: Path | None) -> tuple[Path, Path]:
    if release_root is None:
        return DEFAULT_METADATA_PATH, DEFAULT_HISTORY_PATH
    root = Path(release_root)
    return (
        root / "ops/muncho/release/metadata.json",
        root / "ops/muncho/release/history.json",
    )


def load_release_bundle(release_root: Path | None = None) -> ReleaseBundle:
    metadata_path, history_path = _paths(release_root)
    metadata = validate_release_metadata(_read_canonical_json(metadata_path))
    history = validate_release_history(_read_canonical_json(history_path))
    if history.releases and metadata.version <= history.releases[-1].version:
        raise ReleaseMetadataError("muncho_release_current_version_not_new")
    return ReleaseBundle(metadata=metadata, history=history)


def load_optional_release_bundle(
    release_root: Path | None = None,
) -> ReleaseBundle | None:
    metadata_path, history_path = _paths(release_root)
    metadata_raw = _read_canonical_json(metadata_path, missing_ok=True)
    history_raw = _read_canonical_json(history_path, missing_ok=True)
    if metadata_raw is None and history_raw is None:
        return None
    if metadata_raw is None or history_raw is None:
        raise ReleaseMetadataError("muncho_release_metadata_incomplete")
    metadata = validate_release_metadata(metadata_raw)
    history = validate_release_history(history_raw)
    if history.releases and metadata.version <= history.releases[-1].version:
        raise ReleaseMetadataError("muncho_release_current_version_not_new")
    return ReleaseBundle(metadata=metadata, history=history)


def _read_exact_sha(path: Path) -> str | None:
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or not 0 < before.st_size <= 128
        ):
            return None
        raw = path.read_bytes()
        after = path.lstat()
        value = raw.decode("ascii", errors="strict").strip()
    except (OSError, UnicodeError):
        return None
    if _identity(before) != _identity(after) or len(raw) != after.st_size:
        return None
    return value if _SHA40.fullmatch(value) is not None else None


def resolve_exact_release_sha(release_root: Path | None = None) -> str | None:
    """Resolve the immutable source identity without inventing a value.

    Production's exact ``.codex-source-commit`` marker wins.  Source installs
    fall back to live Git HEAD, and image installs may use the existing baked
    Hermes build SHA.  Missing evidence returns ``None`` for clean upstream
    compatibility; release mutation paths receive the SHA explicitly and
    validate it separately.
    """

    if release_root is not None:
        roots = (Path(release_root).resolve(),)
    else:
        # Source trees import from the repository root. Sealed production
        # imports from <release>/.venv/site-packages, so sys.prefix's parent
        # is the immutable release address carrying .codex-source-commit.
        roots = tuple(dict.fromkeys((PACKAGE_ROOT.parents[2], Path(sys.prefix).parent)))
    for root in roots:
        marker = _read_exact_sha(root / ".codex-source-commit")
        if marker is not None:
            return marker
    for root in roots:
        try:
            completed = subprocess.run(
                ("git", "rev-parse", "HEAD"),
                cwd=str(root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="strict",
                check=False,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            continue
        if completed.returncode == 0:
            value = (completed.stdout or "").strip()
            if _SHA40.fullmatch(value) is not None:
                return value
    try:
        from hermes_cli.build_info import get_build_sha

        baked = get_build_sha(short=0)
    except Exception:
        baked = None
    return baked if isinstance(baked, str) and _SHA40.fullmatch(baked) else None


def require_exact_release_sha(value: Any) -> str:
    if not isinstance(value, str) or _SHA40.fullmatch(value) is None:
        raise ReleaseMetadataError("muncho_release_sha_invalid")
    return value


def require_summary_text(value: Any, *, code: str) -> str:
    return _display_text(value, code=code, maximum=360)


__all__ = [
    "DEFAULT_HISTORY_PATH",
    "DEFAULT_METADATA_PATH",
    "HISTORY_SCHEMA",
    "METADATA_SCHEMA",
    "NOTES_SCHEMA",
    "REQUIRED_HISTORY_PREFIX",
    "ReleaseBundle",
    "ReleaseHistory",
    "ReleaseHistoryEntry",
    "ReleaseMetadata",
    "ReleaseMetadataError",
    "ReleaseNotes",
    "SemVer",
    "canonical_bytes",
    "load_optional_release_bundle",
    "load_release_bundle",
    "require_exact_release_sha",
    "require_summary_text",
    "resolve_exact_release_sha",
    "sha256_bytes",
    "validate_release_history",
    "validate_release_metadata",
    "validate_release_notes",
]
