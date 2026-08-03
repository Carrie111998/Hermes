#!/usr/bin/env python3
"""Receipt-driven garbage collection for stopped Muncho canary releases.

The default command is a read-only plan.  A release is eligible only when the
same exact Git revision exists below both fixed release/source roots and its
append-only stopped-release receipt is canonical, self-digest-bound, and says
that publication completed while every service remained stopped and disabled.

This module never deletes evidence.  It does not infer state from names,
prose, process output, or timestamps on release directories.  Protection is
derived only from exact SHA values, exact symlink targets, structured JSON
references, and the receipt's integer publication time.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, NoReturn, Sequence


PLAN_SCHEMA = "muncho-canary-receipt-driven-gc-plan.v1"
RESULT_SCHEMA = "muncho-canary-receipt-driven-gc-result.v1"
STOPPED_RECEIPT_SCHEMA = "muncho-canary-stopped-release-publication.v1"
FORK_REPOSITORY = "https://github.com/lomliev/hermes-agent.git"

DEFAULT_RELEASE_BASE = Path("/opt/muncho-canary-releases")
DEFAULT_SOURCE_BASE = Path("/opt/muncho-canary-source")
DEFAULT_EVIDENCE_BASE = Path("/var/lib/muncho-canary-release-evidence")
DEFAULT_LOCK_PATH = Path("/run/lock/muncho-canary-receipt-gc.lock")

RECEIPT_NAME = "stopped-release-publication.json"
TERMINAL_RETENTION_COUNT = 3
MAX_JSON_BYTES = 1024 * 1024
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class GCLayout:
    release_base: Path = DEFAULT_RELEASE_BASE
    source_base: Path = DEFAULT_SOURCE_BASE
    evidence_base: Path = DEFAULT_EVIDENCE_BASE
    current_links: tuple[Path, ...] = ()
    previous_links: tuple[Path, ...] = ()
    protected_refs: tuple[Path, ...] = ()

    def validate(self) -> None:
        roots = (self.release_base, self.source_base, self.evidence_base)
        if any(not path.is_absolute() for path in roots):
            raise ValueError("GC roots must be absolute")
        if len(set(roots)) != len(roots):
            raise ValueError("GC roots must be distinct")
        for path in (*self.current_links, *self.previous_links, *self.protected_refs):
            if not path.is_absolute():
                raise ValueError("GC protection paths must be absolute")


@dataclass(frozen=True)
class _TreeEntry:
    path: Path
    identity: tuple[int, ...]


@dataclass(frozen=True)
class _ReceiptState:
    status: str
    created_at_unix: int | None = None
    file_sha256: str | None = None


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256_json(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _require_sha(value: str, *, label: str) -> str:
    if not isinstance(value, str) or _SHA_RE.fullmatch(value) is None:
        raise ValueError(f"{label} must be one exact 40-character SHA")
    return value


def _stat_identity(value: os.stat_result) -> tuple[int, ...]:
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


def _require_real_directory(path: Path, *, label: str) -> os.stat_result:
    try:
        value = os.lstat(path)
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label} is absent") from exc
    if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
        raise RuntimeError(f"{label} is not a real directory")
    return value


def _inventory_root(
    path: Path,
    *,
    label: str,
) -> tuple[dict[str, _TreeEntry], dict[str, str], tuple[str, ...]]:
    root = _require_real_directory(path, label=label)
    valid: dict[str, _TreeEntry] = {}
    invalid: dict[str, str] = {}
    unknown: list[str] = []
    with os.scandir(path) as entries:
        for entry in entries:
            name = entry.name
            if _SHA_RE.fullmatch(name) is None:
                unknown.append(name)
                continue
            value = entry.stat(follow_symlinks=False)
            if (
                not stat.S_ISDIR(value.st_mode)
                or stat.S_ISLNK(value.st_mode)
                or value.st_dev != root.st_dev
            ):
                invalid[name] = "entry_not_real_same_filesystem_directory"
                continue
            valid[name] = _TreeEntry(path / name, _stat_identity(value))
    return valid, invalid, tuple(sorted(unknown))


def _read_stable_regular_file(path: Path, *, maximum_bytes: int) -> bytes:
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > maximum_bytes
    ):
        raise RuntimeError("protected JSON artifact is not an exact regular file")
    with path.open("rb") as handle:
        raw = handle.read(maximum_bytes + 1)
    after = os.lstat(path)
    if len(raw) > maximum_bytes or _stat_identity(before) != _stat_identity(after):
        raise RuntimeError("protected JSON artifact changed during read")
    return raw


def _decode_canonical_mapping(raw: bytes, *, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"{label} is not a JSON object")
    if raw != _canonical_bytes(value) + b"\n":
        raise RuntimeError(f"{label} is not canonical JSON")
    return value


def _receipt_state(layout: GCLayout, revision: str) -> _ReceiptState:
    receipt_path = layout.evidence_base / revision / RECEIPT_NAME
    try:
        raw = _read_stable_regular_file(receipt_path, maximum_bytes=MAX_JSON_BYTES)
        receipt = _decode_canonical_mapping(raw, label="stopped-release receipt")
    except (FileNotFoundError, RuntimeError, OSError):
        return _ReceiptState("absent_or_invalid")

    unsigned = dict(receipt)
    digest = unsigned.pop("receipt_sha256", None)
    source = receipt.get("source")
    created = receipt.get("created_at_unix")
    valid = (
        receipt.get("schema") == STOPPED_RECEIPT_SCHEMA
        and receipt.get("ok") is True
        and receipt.get("state") == "published_services_stopped"
        and receipt.get("services_stopped_and_disabled") is True
        and receipt.get("release_revision") == revision
        and receipt.get("release_root") == str(layout.release_base / revision)
        and receipt.get("receipt_path") == str(receipt_path)
        and isinstance(source, dict)
        and source.get("repository") == FORK_REPOSITORY
        and source.get("root") == str(layout.source_base / revision)
        and source.get("head_sha") == revision
        and _SHA_RE.fullmatch(str(source.get("tree_sha", ""))) is not None
        and type(created) is int
        and created >= 0
        and isinstance(digest, str)
        and _SHA256_RE.fullmatch(digest) is not None
        and digest == _sha256_json(unsigned)
    )
    if not valid:
        return _ReceiptState("absent_or_invalid")
    return _ReceiptState(
        "terminal",
        created_at_unix=created,
        file_sha256=hashlib.sha256(raw).hexdigest(),
    )


def _revision_from_target(layout: GCLayout, link: Path) -> str | None:
    try:
        value = os.lstat(link)
    except FileNotFoundError:
        return None
    if not stat.S_ISLNK(value.st_mode):
        raise RuntimeError("protected current/previous path is not a symlink")
    raw_target = os.readlink(link)
    if os.path.isabs(raw_target):
        target = Path(os.path.normpath(raw_target))
    else:
        target = Path(os.path.normpath(str(link.parent / raw_target)))
    after = os.lstat(link)
    if _stat_identity(value) != _stat_identity(after):
        raise RuntimeError("protected current/previous symlink changed during read")
    for base in (layout.release_base, layout.source_base):
        if target.parent == base and _SHA_RE.fullmatch(target.name) is not None:
            return target.name
    raise RuntimeError("protected current/previous symlink target is outside GC roots")


def _walk_exact_revisions(value: Any, layout: GCLayout) -> Iterable[str]:
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_exact_revisions(child, layout)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_exact_revisions(child, layout)
    elif isinstance(value, str):
        if _SHA_RE.fullmatch(value) is not None:
            yield value
            return
        candidate = Path(value)
        for base in (layout.release_base, layout.source_base):
            if (
                candidate.parent == base
                and _SHA_RE.fullmatch(candidate.name) is not None
            ):
                yield candidate.name
                return


def _protected_ref_revisions(
    layout: GCLayout,
) -> tuple[set[str], list[dict[str, Any]]]:
    revisions: set[str] = set()
    snapshots: list[dict[str, Any]] = []
    for path in layout.protected_refs:
        raw = _read_stable_regular_file(path, maximum_bytes=MAX_JSON_BYTES)
        value = _decode_canonical_mapping(raw, label="pending owner/cutover reference")
        found = sorted(set(_walk_exact_revisions(value, layout)))
        revisions.update(found)
        snapshots.append({
            "path": str(path),
            "file_sha256": hashlib.sha256(raw).hexdigest(),
            "revisions": found,
        })
    return revisions, snapshots


def _symlink_revisions(
    layout: GCLayout,
) -> tuple[set[str], list[dict[str, str]]]:
    revisions: set[str] = set()
    snapshots: list[dict[str, str]] = []
    for kind, paths in (
        ("current", layout.current_links),
        ("previous", layout.previous_links),
    ):
        for path in paths:
            revision = _revision_from_target(layout, path)
            if revision is None:
                continue
            revisions.add(revision)
            snapshots.append({"kind": kind, "path": str(path), "revision": revision})
    return revisions, snapshots


def build_plan(layout: GCLayout, *, production_sha: str) -> dict[str, Any]:
    """Return one deterministic, read-only GC plan."""

    layout.validate()
    production_sha = _require_sha(production_sha, label="production SHA")
    releases, invalid_releases, unknown_releases = _inventory_root(
        layout.release_base,
        label="canary release base",
    )
    sources, invalid_sources, unknown_sources = _inventory_root(
        layout.source_base,
        label="canary source base",
    )
    _require_real_directory(layout.evidence_base, label="canary evidence base")

    link_revisions, link_snapshots = _symlink_revisions(layout)
    ref_revisions, ref_snapshots = _protected_ref_revisions(layout)
    revisions = sorted(
        set(releases) | set(sources) | set(invalid_releases) | set(invalid_sources)
    )
    receipts = {revision: _receipt_state(layout, revision) for revision in revisions}
    terminal_releases = [
        revision
        for revision in revisions
        if revision in releases and receipts[revision].status == "terminal"
    ]
    terminal_releases.sort(
        key=lambda revision: (
            receipts[revision].created_at_unix,
            revision,
        ),
        reverse=True,
    )
    newest_terminal = set(terminal_releases[:TERMINAL_RETENTION_COUNT])
    protected = {production_sha} | link_revisions | ref_revisions | newest_terminal

    units: list[dict[str, Any]] = []
    for revision in revisions:
        reasons: list[str] = []
        receipt = receipts[revision]
        if revision in invalid_releases or revision in invalid_sources:
            reasons.append("invalid_release_or_source_entry")
        if revision not in releases or revision not in sources:
            reasons.append("release_source_pair_incomplete")
        if receipt.status != "terminal":
            reasons.append("receipt_absent_or_nonterminal")
        if revision == production_sha:
            reasons.append("production_sha")
        if revision in link_revisions:
            reasons.append("current_or_previous_target")
        if revision in ref_revisions:
            reasons.append("pending_owner_or_cutover_ref")
        if revision in newest_terminal:
            reasons.append("newest_terminal_retention")
        release_entry = releases.get(revision)
        source_entry = sources.get(revision)
        units.append({
            "revision": revision,
            "release_path": str(layout.release_base / revision),
            "source_path": str(layout.source_base / revision),
            "evidence_path": str(layout.evidence_base / revision / RECEIPT_NAME),
            "receipt_status": receipt.status,
            "receipt_created_at_unix": receipt.created_at_unix,
            "receipt_file_sha256": receipt.file_sha256,
            "release_identity": (
                list(release_entry.identity) if release_entry else None
            ),
            "source_identity": list(source_entry.identity) if source_entry else None,
            "action": "delete_pair" if not reasons else "preserve",
            "reasons": reasons,
        })

    unsigned: dict[str, Any] = {
        "schema": PLAN_SCHEMA,
        "production_sha": production_sha,
        "roots": {
            "release_base": str(layout.release_base),
            "source_base": str(layout.source_base),
            "evidence_base": str(layout.evidence_base),
        },
        "terminal_retention_count": TERMINAL_RETENTION_COUNT,
        "protected": {
            "newest_terminal_revisions": sorted(newest_terminal),
            "symlinks": link_snapshots,
            "structured_refs": ref_snapshots,
        },
        "unknown_entries": {
            "release_base": list(unknown_releases),
            "source_base": list(unknown_sources),
        },
        "units": units,
        "evidence_deletion_enabled": False,
    }
    return {**unsigned, "plan_sha256": _sha256_json(unsigned)}


def _assert_entry_identity(path: Path, expected: Sequence[int]) -> None:
    current = os.lstat(path)
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or tuple(expected) != _stat_identity(current)
    ):
        raise RuntimeError("GC unit identity changed before deletion")


def _assert_renamed_entry_identity(path: Path, expected: Sequence[int]) -> None:
    current = os.lstat(path)
    # A same-filesystem rename preserves the inode and content metadata but
    # legitimately advances ctime.  Every other captured field stays exact.
    if (
        not stat.S_ISDIR(current.st_mode)
        or stat.S_ISLNK(current.st_mode)
        or tuple(expected)[:-1] != _stat_identity(current)[:-1]
    ):
        raise RuntimeError("GC unit identity changed during tombstone move")


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _remove_pair(unit: Mapping[str, Any], *, plan_sha256: str) -> None:
    revision = _require_sha(str(unit["revision"]), label="GC unit revision")
    release = Path(str(unit["release_path"]))
    source = Path(str(unit["source_path"]))
    release_identity = unit.get("release_identity")
    source_identity = unit.get("source_identity")
    if not isinstance(release_identity, list) or not isinstance(source_identity, list):
        raise RuntimeError("GC unit lacks exact release/source identities")
    _assert_entry_identity(release, release_identity)
    _assert_entry_identity(source, source_identity)
    if not getattr(shutil.rmtree, "avoids_symlink_attacks", False):
        raise RuntimeError("symlink-safe directory removal is unavailable")

    suffix = plan_sha256[:16]
    release_tombstone = release.with_name(f".{revision}.{suffix}.gc")
    source_tombstone = source.with_name(f".{revision}.{suffix}.gc")
    if os.path.lexists(release_tombstone) or os.path.lexists(source_tombstone):
        raise RuntimeError("GC tombstone collision exists")

    os.rename(source, source_tombstone)
    try:
        _assert_renamed_entry_identity(source_tombstone, source_identity)
        os.rename(release, release_tombstone)
        _assert_renamed_entry_identity(release_tombstone, release_identity)
    except BaseException:
        if os.path.lexists(release_tombstone):
            os.rename(release_tombstone, release)
        if os.path.lexists(source_tombstone):
            os.rename(source_tombstone, source)
        raise
    _fsync_directory(source.parent)
    _fsync_directory(release.parent)
    shutil.rmtree(source_tombstone)
    shutil.rmtree(release_tombstone)
    _fsync_directory(source.parent)
    _fsync_directory(release.parent)


def apply_plan(
    layout: GCLayout,
    *,
    production_sha: str,
    approved_plan_sha256: str,
    require_root_linux: bool = True,
) -> dict[str, Any]:
    """Re-plan and remove only exact units authorized by the approved digest."""

    if require_root_linux and (
        os.geteuid() != 0 or not sys.platform.startswith("linux")
    ):
        raise PermissionError("canary GC apply requires root on Linux")
    if _SHA256_RE.fullmatch(str(approved_plan_sha256)) is None:
        raise ValueError("approved GC plan digest is invalid")
    plan = build_plan(layout, production_sha=production_sha)
    if plan["plan_sha256"] != approved_plan_sha256:
        raise PermissionError("approved GC plan digest does not match current state")

    removed: list[str] = []
    candidates = [unit for unit in plan["units"] if unit["action"] == "delete_pair"]
    for approved_unit in candidates:
        current = build_plan(layout, production_sha=production_sha)
        current_by_revision = {unit["revision"]: unit for unit in current["units"]}
        revision = approved_unit["revision"]
        current_unit = current_by_revision.get(revision)
        if current_unit is None or current_unit["action"] != "delete_pair":
            raise RuntimeError("GC unit is no longer eligible")
        for field in (
            "release_path",
            "source_path",
            "evidence_path",
            "receipt_file_sha256",
            "release_identity",
            "source_identity",
        ):
            if current_unit.get(field) != approved_unit.get(field):
                raise RuntimeError("GC unit changed after approval")
        _remove_pair(approved_unit, plan_sha256=approved_plan_sha256)
        removed.append(revision)

    result_unsigned = {
        "schema": RESULT_SCHEMA,
        "ok": True,
        "approved_plan_sha256": approved_plan_sha256,
        "removed_release_source_pairs": removed,
        "evidence_deleted": False,
    }
    return {**result_unsigned, "result_sha256": _sha256_json(result_unsigned)}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--production-sha", required=True)
    parser.add_argument("--current-link", action="append", default=[])
    parser.add_argument("--previous-link", action="append", default=[])
    parser.add_argument("--protected-ref", action="append", default=[])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--approved-plan-sha256")
    return parser


def _die(message: str) -> NoReturn:
    raise SystemExit(message)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    layout = GCLayout(
        current_links=tuple(Path(value) for value in args.current_link),
        previous_links=tuple(Path(value) for value in args.previous_link),
        protected_refs=tuple(Path(value) for value in args.protected_ref),
    )
    if not args.apply:
        if args.approved_plan_sha256 is not None:
            _die("--approved-plan-sha256 is valid only with --apply")
        report = build_plan(layout, production_sha=args.production_sha)
    else:
        if args.approved_plan_sha256 is None:
            _die("--apply requires --approved-plan-sha256")
        report = apply_plan(
            layout,
            production_sha=args.production_sha,
            approved_plan_sha256=args.approved_plan_sha256,
        )
    sys.stdout.buffer.write(_canonical_bytes(report) + b"\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
