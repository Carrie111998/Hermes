#!/usr/bin/env python3
"""Fail-closed retention for content-addressed Muncho releases.

The legacy deploy helper keeps a bounded set of recent rollback releases.  A
release may also be a live systemd dependency even when it is older than that
window, so age alone is never sufficient authority to remove it.  This module
builds a complete, race-checked inventory before deleting anything:

* the active release and the newest rollback candidates are protected;
* exact release roots referenced by loaded systemd properties are protected;
* exact release roots referenced by installed systemd unit files are protected;
* a missing referenced release or an ambiguous inventory blocks all deletion.

Selection is exclusively mechanical: exact paths, file identities, systemd
resource identities, and content-addressed release names.  No prompt or prose
is inspected to decide what is retained.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence


MAX_UNIT_FILE_BYTES = 8 * 1024 * 1024
MAX_SYSTEMCTL_BYTES = 32 * 1024 * 1024
SYSTEMCTL_TIMEOUT_SECONDS = 30

_RELEASE_NAME = re.compile(r"^hermes-agent-(?P<prefix>[0-9a-f]{12})$")
_UNIT_NAME = re.compile(
    r"^(?:(?:[A-Za-z0-9_.@:-])|(?:\\x[0-9A-Fa-f]{2}))+"
    r"\.(?:path|service|socket|timer)$"
)
_SHOW_PROPERTIES = (
    "Id",
    "Names",
    "LoadState",
    "FragmentPath",
    "DropInPaths",
    "ExecStart",
    "ExecStartPre",
    "ExecStartPost",
    "ExecStop",
    "ExecReload",
    "WorkingDirectory",
    "RootDirectory",
    "Environment",
    "EnvironmentFiles",
    "AssertPathExists",
    "ConditionPathExists",
    "ReadOnlyPaths",
    "BindReadOnlyPaths",
    "ReadWritePaths",
    "InaccessiblePaths",
)
_REQUIRED_SHOW_PROPERTIES = frozenset({
    "Id",
    "Names",
    "LoadState",
    "FragmentPath",
    "DropInPaths",
})


class ReleaseRetentionError(RuntimeError):
    """Stable, secret-free retention failure."""

    def __init__(self, code: str, subject: str | None = None) -> None:
        self.code = code
        self.subject = subject
        super().__init__(code if subject is None else f"{code}:{subject}")


@dataclass(frozen=True)
class FileSnapshot:
    identity: tuple[int, ...]
    sha256: str
    references: frozenset[str]


@dataclass(frozen=True)
class SystemdSnapshot:
    loaded_units: tuple[str, ...]
    loaded_references: Mapping[str, frozenset[str]]
    unit_files: Mapping[str, FileSnapshot]

    @property
    def references(self) -> frozenset[str]:
        values: set[str] = set()
        for references in self.loaded_references.values():
            values.update(references)
        for item in self.unit_files.values():
            values.update(item.references)
        return frozenset(values)


@dataclass(frozen=True)
class CleanupPlan:
    releases_root: Path
    active_release: Path
    keep: int
    systemctl: Path
    unit_roots: tuple[Path, ...]
    systemd_snapshot: SystemdSnapshot
    release_inventory: Mapping[str, tuple[Path, tuple[int, ...]]]
    protected: tuple[Path, ...]
    removable: tuple[Path, ...]
    referenced_prefixes: tuple[str, ...]


def _fail(code: str, subject: str | None = None) -> None:
    raise ReleaseRetentionError(code, subject)


def _identity(item: os.stat_result) -> tuple[int, ...]:
    return (
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


def _validate_directory(path: Path, code: str) -> os.stat_result:
    if not path.is_absolute():
        _fail(code)
    try:
        item = path.lstat()
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ReleaseRetentionError(code) from exc
    if resolved != path or stat.S_ISLNK(item.st_mode) or not stat.S_ISDIR(item.st_mode):
        _fail(code)
    return item


def _stable_read(path: Path) -> tuple[bytes, tuple[int, ...]]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise ReleaseRetentionError("release_cleanup_unit_file_unavailable") from exc
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 0
            or before.st_size > MAX_UNIT_FILE_BYTES
        ):
            _fail("release_cleanup_unit_file_invalid")
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
    raw = b"".join(chunks)
    if len(raw) != before.st_size or _identity(before) != _identity(after):
        _fail("release_cleanup_unit_file_changed")
    return raw, _identity(before)


def _release_reference_pattern(releases_root: Path) -> re.Pattern[bytes]:
    return re.compile(
        re.escape(os.fsencode(str(releases_root)))
        + rb"/hermes-agent-(?P<prefix>[0-9a-f]{12})"
        + rb"(?=/|[^A-Za-z0-9_.-]|$)"
    )


def _references(raw: bytes, pattern: re.Pattern[bytes]) -> frozenset[str]:
    return frozenset(
        match.group("prefix").decode("ascii", errors="strict")
        for match in pattern.finditer(raw)
    )


def _path_within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _unit_file_names(roots: Sequence[Path]) -> tuple[Path, ...]:
    result: set[Path] = set()

    def onerror(error: OSError) -> None:
        raise ReleaseRetentionError(
            "release_cleanup_unit_inventory_ambiguous"
        ) from error

    for root in roots:
        for current, directories, files in os.walk(
            root, topdown=True, followlinks=False, onerror=onerror
        ):
            parent = Path(current)
            for name in list(directories):
                path = parent / name
                try:
                    item = path.lstat()
                except OSError as exc:
                    raise ReleaseRetentionError(
                        "release_cleanup_unit_inventory_ambiguous"
                    ) from exc
                if stat.S_ISLNK(item.st_mode):
                    directories.remove(name)
                    try:
                        target = path.resolve(strict=True)
                    except OSError as exc:
                        raise ReleaseRetentionError(
                            "release_cleanup_unit_inventory_ambiguous"
                        ) from exc
                    if target != Path("/dev/null") and not _path_within(target, roots):
                        _fail("release_cleanup_unit_inventory_ambiguous")
                elif not stat.S_ISDIR(item.st_mode):
                    _fail("release_cleanup_unit_inventory_ambiguous")
            for name in files:
                path = parent / name
                try:
                    item = path.lstat()
                except OSError as exc:
                    raise ReleaseRetentionError(
                        "release_cleanup_unit_inventory_ambiguous"
                    ) from exc
                if stat.S_ISLNK(item.st_mode):
                    try:
                        target = path.resolve(strict=True)
                    except OSError as exc:
                        raise ReleaseRetentionError(
                            "release_cleanup_unit_inventory_ambiguous"
                        ) from exc
                    if target != Path("/dev/null") and not _path_within(target, roots):
                        _fail("release_cleanup_unit_inventory_ambiguous")
                    continue
                if not stat.S_ISREG(item.st_mode):
                    _fail("release_cleanup_unit_inventory_ambiguous")
                result.add(path)
    return tuple(sorted(result))


def _unit_file_snapshot(
    roots: Sequence[Path], pattern: re.Pattern[bytes]
) -> dict[str, FileSnapshot]:
    files: dict[str, FileSnapshot] = {}
    for path in _unit_file_names(roots):
        raw, identity = _stable_read(path)
        files[str(path)] = FileSnapshot(
            identity=identity,
            sha256=hashlib.sha256(raw).hexdigest(),
            references=_references(raw, pattern),
        )
    return files


def _run_systemctl(systemctl: Path, arguments: Sequence[str]) -> bytes:
    try:
        completed = subprocess.run(
            [str(systemctl), *arguments],
            check=False,
            capture_output=True,
            timeout=SYSTEMCTL_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseRetentionError(
            "release_cleanup_systemd_inventory_ambiguous"
        ) from exc
    if (
        completed.returncode != 0
        or len(completed.stdout) > MAX_SYSTEMCTL_BYTES
        or len(completed.stderr) > MAX_SYSTEMCTL_BYTES
        or b"\x00" in completed.stdout
    ):
        _fail("release_cleanup_systemd_inventory_ambiguous")
    return completed.stdout


def _loaded_unit_names(systemctl: Path) -> tuple[str, ...]:
    raw = _run_systemctl(
        systemctl,
        (
            "list-units",
            "--all",
            "--plain",
            "--no-legend",
            "--no-pager",
            "--type=path",
            "--type=service",
            "--type=socket",
            "--type=timer",
        ),
    )
    try:
        lines = raw.decode("utf-8", errors="strict").splitlines()
    except UnicodeError as exc:
        raise ReleaseRetentionError(
            "release_cleanup_systemd_inventory_ambiguous"
        ) from exc
    names: list[str] = []
    for line in lines:
        if not line.strip():
            continue
        name = line.split(None, 1)[0]
        if _UNIT_NAME.fullmatch(name) is None or name in names:
            _fail("release_cleanup_systemd_inventory_ambiguous")
        names.append(name)
    if not names:
        _fail("release_cleanup_systemd_inventory_ambiguous")
    return tuple(sorted(names))


def _loaded_unit_references(
    systemctl: Path,
    names: Sequence[str],
    pattern: re.Pattern[bytes],
) -> dict[str, frozenset[str]]:
    result: dict[str, frozenset[str]] = {}
    property_arguments = tuple(
        item for name in _SHOW_PROPERTIES for item in ("--property", name)
    )
    for name in names:
        raw = _run_systemctl(
            systemctl,
            ("show", "--no-pager", *property_arguments, "--", name),
        )
        try:
            lines = raw.decode("utf-8", errors="strict").splitlines()
        except UnicodeError as exc:
            raise ReleaseRetentionError(
                "release_cleanup_systemd_inventory_ambiguous"
            ) from exc
        values: dict[str, str] = {}
        for line in lines:
            if "=" not in line:
                _fail("release_cleanup_systemd_inventory_ambiguous")
            key, value = line.split("=", 1)
            if key not in _SHOW_PROPERTIES or key in values:
                _fail("release_cleanup_systemd_inventory_ambiguous")
            values[key] = value
        if not _REQUIRED_SHOW_PROPERTIES.issubset(values):
            _fail("release_cleanup_systemd_inventory_ambiguous")
        for key in _SHOW_PROPERTIES:
            values.setdefault(key, "")
        aliases = set(values["Names"].split())
        if (
            values["Id"] != name
            or name not in aliases
            or values["LoadState"] != "loaded"
        ):
            _fail("release_cleanup_systemd_inventory_ambiguous")
        result[name] = _references(raw, pattern)
    return result


def systemd_snapshot(
    *,
    systemctl: Path,
    unit_roots: Sequence[Path],
    releases_root: Path,
) -> SystemdSnapshot:
    _validate_directory(systemctl.parent, "release_cleanup_systemctl_parent_invalid")
    try:
        systemctl_state = systemctl.lstat()
    except OSError as exc:
        raise ReleaseRetentionError("release_cleanup_systemctl_invalid") from exc
    if (
        stat.S_ISLNK(systemctl_state.st_mode)
        or not stat.S_ISREG(systemctl_state.st_mode)
        or not systemctl_state.st_mode & stat.S_IXUSR
    ):
        _fail("release_cleanup_systemctl_invalid")
    roots = tuple(unit_roots)
    if not roots or len(set(roots)) != len(roots):
        _fail("release_cleanup_unit_roots_invalid")
    root_identities = {
        str(root): _identity(
            _validate_directory(root, "release_cleanup_unit_root_invalid")
        )
        for root in roots
    }
    pattern = _release_reference_pattern(releases_root)
    names_before = _loaded_unit_names(systemctl)
    loaded_before = _loaded_unit_references(systemctl, names_before, pattern)
    files_before = _unit_file_snapshot(roots, pattern)
    files_after = _unit_file_snapshot(roots, pattern)
    names_after = _loaded_unit_names(systemctl)
    loaded_after = _loaded_unit_references(systemctl, names_after, pattern)
    reached_root_identities = {
        str(root): _identity(
            _validate_directory(root, "release_cleanup_unit_root_invalid")
        )
        for root in roots
    }
    if (
        names_before != names_after
        or loaded_before != loaded_after
        or files_before != files_after
        or root_identities != reached_root_identities
    ):
        _fail("release_cleanup_unit_inventory_ambiguous")
    return SystemdSnapshot(
        loaded_units=names_before,
        loaded_references=loaded_before,
        unit_files=files_before,
    )


def _release_inventory(releases_root: Path) -> dict[str, tuple[Path, os.stat_result]]:
    _validate_directory(releases_root, "release_cleanup_releases_root_invalid")
    result: dict[str, tuple[Path, os.stat_result]] = {}
    try:
        entries = tuple(os.scandir(releases_root))
    except OSError as exc:
        raise ReleaseRetentionError(
            "release_cleanup_release_inventory_ambiguous"
        ) from exc
    for entry in entries:
        match = _RELEASE_NAME.fullmatch(entry.name)
        if match is None:
            continue
        path = releases_root / entry.name
        try:
            item = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ReleaseRetentionError(
                "release_cleanup_release_inventory_ambiguous"
            ) from exc
        prefix = match.group("prefix")
        if (
            prefix in result
            or resolved != path
            or path.parent != releases_root
            or stat.S_ISLNK(item.st_mode)
            or not stat.S_ISDIR(item.st_mode)
        ):
            _fail("release_cleanup_release_inventory_ambiguous")
        result[prefix] = (path, item)
    if not result:
        _fail("release_cleanup_release_inventory_ambiguous")
    return result


def _release_inventory_identities(
    inventory: Mapping[str, tuple[Path, os.stat_result]],
) -> dict[str, tuple[Path, tuple[int, ...]]]:
    return {
        prefix: (path, _identity(item)) for prefix, (path, item) in inventory.items()
    }


def _plan_from_observations(
    *,
    releases_root: Path,
    active_release: Path,
    keep: int,
    systemctl: Path,
    unit_roots: tuple[Path, ...],
    inventory: Mapping[str, tuple[Path, os.stat_result]],
    snapshot: SystemdSnapshot,
) -> CleanupPlan:
    missing = sorted(snapshot.references.difference(inventory))
    if missing:
        _fail("release_cleanup_referenced_release_missing", missing[0])
    newest = sorted(
        inventory.values(),
        key=lambda item: (item[1].st_mtime_ns, item[0].name),
        reverse=True,
    )
    protected = {path for path, _item in newest[:keep]}
    protected.add(active_release)
    protected.update(inventory[prefix][0] for prefix in snapshot.references)
    removable = tuple(
        sorted(
            (path for path, _item in inventory.values() if path not in protected),
            key=lambda item: item.name,
        )
    )
    return CleanupPlan(
        releases_root=releases_root,
        active_release=active_release,
        keep=keep,
        systemctl=systemctl,
        unit_roots=unit_roots,
        systemd_snapshot=snapshot,
        release_inventory=_release_inventory_identities(inventory),
        protected=tuple(sorted(protected, key=lambda item: item.name)),
        removable=removable,
        referenced_prefixes=tuple(sorted(snapshot.references)),
    )


def build_cleanup_plan(
    *,
    releases_root: Path,
    active_release: Path,
    keep: int,
    systemctl: Path,
    unit_roots: Sequence[Path],
) -> CleanupPlan:
    if type(keep) is not int or not 2 <= keep <= 100:
        _fail("release_cleanup_keep_invalid")
    inventory = _release_inventory(releases_root)
    active_match = _RELEASE_NAME.fullmatch(active_release.name)
    if (
        not active_release.is_absolute()
        or active_release.parent != releases_root
        or active_match is None
        or active_match.group("prefix") not in inventory
        or inventory[active_match.group("prefix")][0] != active_release
    ):
        _fail("release_cleanup_active_release_invalid")
    roots = tuple(unit_roots)
    snapshot = systemd_snapshot(
        systemctl=systemctl,
        unit_roots=roots,
        releases_root=releases_root,
    )
    return _plan_from_observations(
        releases_root=releases_root,
        active_release=active_release,
        keep=keep,
        systemctl=systemctl,
        unit_roots=roots,
        inventory=inventory,
        snapshot=snapshot,
    )


def apply_cleanup_plan(plan: CleanupPlan) -> tuple[Path, ...]:
    # The first observation authorizes no mutation by itself.  Recollect the
    # complete release and systemd inventories immediately before deletion,
    # recompute the plan, and require exact equality.  A unit that starts
    # referencing an old release between these observations therefore blocks
    # the whole cleanup instead of racing with removal.
    reached_inventory = _release_inventory(plan.releases_root)
    reached_snapshot = systemd_snapshot(
        systemctl=plan.systemctl,
        unit_roots=plan.unit_roots,
        releases_root=plan.releases_root,
    )
    reached_plan = _plan_from_observations(
        releases_root=plan.releases_root,
        active_release=plan.active_release,
        keep=plan.keep,
        systemctl=plan.systemctl,
        unit_roots=plan.unit_roots,
        inventory=reached_inventory,
        snapshot=reached_snapshot,
    )
    if (
        reached_plan.release_inventory != plan.release_inventory
        or reached_plan.systemd_snapshot != plan.systemd_snapshot
        or reached_plan.protected != plan.protected
        or reached_plan.removable != plan.removable
        or reached_plan.referenced_prefixes != plan.referenced_prefixes
    ):
        _fail("release_cleanup_inventory_changed")
    # Revalidate every target before the first mutation.  The plan contains only
    # exact direct children discovered as non-symlink directories.
    for path in reached_plan.removable:
        try:
            item = path.lstat()
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise ReleaseRetentionError("release_cleanup_target_changed") from exc
        if (
            path.parent != plan.releases_root
            or resolved != path
            or stat.S_ISLNK(item.st_mode)
            or not stat.S_ISDIR(item.st_mode)
            or _RELEASE_NAME.fullmatch(path.name) is None
            or reached_plan.release_inventory[path.name.removeprefix("hermes-agent-")]
            != (path, _identity(item))
        ):
            _fail("release_cleanup_target_changed")
    for path in reached_plan.removable:
        try:
            shutil.rmtree(path)
        except OSError as exc:
            raise ReleaseRetentionError(
                "release_cleanup_remove_failed", path.name
            ) from exc
    return reached_plan.removable


def cleanup(
    *,
    releases_root: Path,
    active_release: Path,
    keep: int,
    systemctl: Path,
    unit_roots: Sequence[Path],
) -> tuple[Path, ...]:
    plan = build_cleanup_plan(
        releases_root=releases_root,
        active_release=active_release,
        keep=keep,
        systemctl=systemctl,
        unit_roots=unit_roots,
    )
    return apply_cleanup_plan(plan)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--releases-root", type=Path, required=True)
    parser.add_argument("--active-release", type=Path, required=True)
    parser.add_argument("--keep", type=int, required=True)
    parser.add_argument("--systemctl", type=Path, required=True)
    parser.add_argument("--unit-root", type=Path, action="append", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        removed = cleanup(
            releases_root=arguments.releases_root,
            active_release=arguments.active_release,
            keep=arguments.keep,
            systemctl=arguments.systemctl,
            unit_roots=arguments.unit_root,
        )
    except ReleaseRetentionError as exc:
        suffix = "" if exc.subject is None else f":{exc.subject}"
        print(f"release_cleanup_blocked={exc.code}{suffix}")
        return 1
    print("release_cleanup_removed=" + ",".join(str(path) for path in removed))
    return 0


if __name__ == "__main__":
    sys.exit(main())
