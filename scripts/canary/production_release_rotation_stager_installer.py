#!/usr/bin/env python3
"""Install the dormant rotation-stager builder boundary, without activating it.

The installer copies a closed, hard-coded asset set from one exact Git commit,
creates only the dedicated builder identity and promotion lock, and stops.  It
never enables, starts, schedules, or reloads a service; it never changes a
release pointer, gateway, application data, or credentials.
"""

from __future__ import annotations

import argparse
import ast
import grp
import hashlib
import json
import os
import pwd
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Never, Sequence

from scripts.canary import production_release_builder_phase as phase


INSTALL_RECEIPT_SCHEMA = "muncho-production-unit-input-rotation-stager-installation.v1"
REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA = (
    "muncho-production-unit-input-rotation-stager-installation.v2"
)
_REVISION = re.compile(r"^[0-9a-f]{40}$")
_REMOTE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_ASSETS: Mapping[str, tuple[str, int]] = {
    "scripts/__init__.py": (
        "library/scripts/__init__.py",
        0o444,
    ),
    "scripts/canary/__init__.py": (
        "library/scripts/canary/__init__.py",
        0o444,
    ),
    "scripts/canary/production_release_builder_runtime.py": (
        "library/scripts/canary/production_release_builder_runtime.py",
        0o444,
    ),
    "scripts/canary/production_release_builder_phase.py": (
        "library/scripts/canary/production_release_builder_phase.py",
        0o444,
    ),
    "scripts/canary/production_release_candidate_promoter.py": (
        "library/scripts/canary/production_release_candidate_promoter.py",
        0o444,
    ),
    "scripts/canary/production_release_rotation_stager_input_author.py": (
        "library/scripts/canary/production_release_rotation_stager_input_author.py",
        0o444,
    ),
    "scripts/canary/production_release_rotation_stager_launcher.py": (
        "library/scripts/canary/production_release_rotation_stager_launcher.py",
        0o444,
    ),
    "scripts/canary/production_release_rotation_stager_promoter.py": (
        "library/scripts/canary/production_release_rotation_stager_promoter.py",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder.sysusers": (
        "sysusers/muncho-release-builder.conf",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder.tmpfiles": (
        "tmpfiles/muncho-release-builder.conf",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder@.service": (
        "systemd/muncho-release-builder@.service",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder-phase": (
        "libexec/muncho-release-builder-phase",
        0o555,
    ),
    "ops/muncho/release-updater/muncho-release-candidate-promoter": (
        "libexec/muncho-release-candidate-promoter",
        0o555,
    ),
    "ops/muncho/release-updater/muncho-release-unit-input-rotation-stager": (
        "libexec/muncho-release-unit-input-rotation-stager",
        0o555,
    ),
    "ops/muncho/release-updater/muncho-release-rotation-stager-input-author": (
        "libexec/muncho-release-rotation-stager-input-author",
        0o555,
    ),
}
_REVISION_LIBRARY_ASSETS: Mapping[str, tuple[str, int]] = {
    source: (target.removeprefix("library/"), mode)
    for source, (target, mode) in _SOURCE_ASSETS.items()
    if target.startswith("library/")
}
_REVISION_STATIC_ASSETS: Mapping[str, tuple[str, int]] = {
    "ops/muncho/release-updater/muncho-release-builder.sysusers": (
        "sysusers/muncho-release-builder.conf",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder.tmpfiles": (
        "tmpfiles/muncho-release-builder.conf",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-builder-v2@.service": (
        "systemd/muncho-release-builder-v2@.service",
        0o444,
    ),
    "ops/muncho/release-updater/muncho-release-foundation-exec-v2": (
        "libexec/muncho-release-foundation-exec-v2",
        0o555,
    ),
}


class RotationStagerInstallerError(RuntimeError):
    """Stable, secret-free installation failure."""


def _fail(code: str, cause: BaseException | None = None) -> Never:
    del cause
    raise RotationStagerInstallerError(code) from None


def _read_posix_identity(name: Literal["geteuid", "getegid"]) -> int:
    reader = getattr(os, name, None)
    if not callable(reader):
        _fail("rotation_stager_installer_posix_identity_unavailable")
    try:
        value = reader()
    except (OSError, TypeError, ValueError) as exc:
        _fail("rotation_stager_installer_posix_identity_unavailable", exc)
    if type(value) is not int or value < 0:
        _fail("rotation_stager_installer_posix_identity_unavailable")
    return value


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii", errors="strict")
    except (TypeError, ValueError, UnicodeError) as exc:
        _fail("rotation_stager_installer_json_invalid", exc)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


@dataclass(frozen=True)
class InstallerRoots:
    library: Path = Path("/usr/lib/muncho-release-updater")
    sysusers: Path = Path("/usr/lib/sysusers.d")
    tmpfiles: Path = Path("/usr/lib/tmpfiles.d")
    systemd: Path = Path("/etc/systemd/system")
    libexec: Path = Path("/usr/libexec")
    job_root: Path = phase.PRODUCTION_JOB_ROOT
    promotion_lock: Path = Path("/run/lock/muncho-release-builder-promotion.lock")
    library_releases: Path = Path("/usr/lib/muncho-release-updater-releases")


def _target(roots: InstallerRoots, relative: str) -> Path:
    namespace, remainder = relative.split("/", 1)
    root = getattr(roots, namespace)
    return root / remainder


def _git_environment() -> Mapping[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "LC_ALL": "C",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PROTOCOL_FROM_USER": "0",
        "GIT_TERMINAL_PROMPT": "0",
    }


def _git_command(source: Path, *arguments: str) -> tuple[str, ...]:
    return (
        "/usr/bin/git",
        "-c",
        f"safe.directory={source}",
        "-C",
        str(source),
        *arguments,
    )


def _git(source: Path, *arguments: str, maximum: int = 64 * 1024 * 1024) -> bytes:
    try:
        completed = subprocess.run(
            _git_command(source, *arguments),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=300,
            env=_git_environment(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail("rotation_stager_installer_git_invalid", exc)
    if completed.returncode != 0 or completed.stderr or len(completed.stdout) > maximum:
        _fail("rotation_stager_installer_git_invalid")
    return completed.stdout


def _line(raw: bytes) -> str:
    try:
        value = raw.decode("ascii", errors="strict").strip()
    except UnicodeError as exc:
        _fail("rotation_stager_installer_git_invalid", exc)
    if not value or "\n" in value or "\r" in value:
        _fail("rotation_stager_installer_git_invalid")
    return value


def _publish_exact(
    path: Path,
    raw: bytes,
    *,
    mode: int,
    expected_uid: int,
    expected_gid: int,
) -> bool:
    try:
        state = os.lstat(path)
    except FileNotFoundError:
        state = None
    except OSError as exc:
        _fail("rotation_stager_installer_target_invalid", exc)
    if state is not None:
        if (
            not stat.S_ISREG(state.st_mode)
            or stat.S_ISLNK(state.st_mode)
            or state.st_uid != expected_uid
            or state.st_gid != expected_gid
            or state.st_nlink != 1
            or stat.S_IMODE(state.st_mode) != mode
        ):
            _fail("rotation_stager_installer_target_conflict")
        try:
            existing = path.read_bytes()
        except OSError as exc:
            _fail("rotation_stager_installer_target_invalid", exc)
        if existing != raw:
            _fail("rotation_stager_installer_target_conflict")
        return False
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        view = memoryview(raw)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                _fail("rotation_stager_installer_write_failed")
            view = view[written:]
        os.fchmod(descriptor, mode)
        os.fchown(descriptor, expected_uid, expected_gid)
        os.fsync(descriptor)
    except RotationStagerInstallerError:
        raise
    except OSError as exc:
        _fail("rotation_stager_installer_write_failed", exc)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return True


def _ensure_directory(path: Path, *, create: bool, production: bool) -> None:
    if create:
        try:
            path.mkdir(mode=0o755, parents=True, exist_ok=True)
        except OSError as exc:
            _fail("rotation_stager_installer_directory_invalid", exc)
    try:
        state = os.lstat(path)
    except OSError as exc:
        _fail("rotation_stager_installer_directory_invalid", exc)
    expected_uid = 0 if production else _read_posix_identity("geteuid")
    expected_gid = 0 if production else _read_posix_identity("getegid")
    if (
        not path.is_absolute()
        or ".." in path.parts
        or not stat.S_ISDIR(state.st_mode)
        or stat.S_ISLNK(state.st_mode)
        or state.st_uid != expected_uid
        or state.st_gid != expected_gid
        or stat.S_IMODE(state.st_mode) & 0o022
    ):
        _fail("rotation_stager_installer_directory_invalid")


def _system_command(argv: Sequence[str]) -> None:
    try:
        completed = subprocess.run(
            tuple(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=60,
            env={"PATH": "/usr/bin:/bin", "LC_ALL": "C", "HOME": "/nonexistent"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _fail("rotation_stager_installer_foundation_failed", exc)
    if completed.returncode != 0:
        _fail("rotation_stager_installer_foundation_failed")


def _validate_builder_identity() -> None:
    try:
        user = pwd.getpwnam(phase.BUILDER_USER)
        group = grp.getgrnam(phase.BUILDER_GROUP)
    except KeyError as exc:
        _fail("rotation_stager_installer_identity_invalid", exc)
    if (
        user.pw_uid != phase.BUILDER_UID
        or user.pw_gid != phase.BUILDER_GID
        or group.gr_gid != phase.BUILDER_GID
        or user.pw_dir != "/nonexistent"
        or user.pw_shell != "/usr/sbin/nologin"
    ):
        _fail("rotation_stager_installer_identity_invalid")


def _validate_foundation(
    roots: InstallerRoots,
    *,
    root_uid: int,
    root_gid: int,
    builder_gid: int,
) -> None:
    try:
        job = os.lstat(roots.job_root)
        lock = os.lstat(roots.promotion_lock)
    except OSError as exc:
        _fail("rotation_stager_installer_foundation_invalid", exc)
    if (
        not stat.S_ISDIR(job.st_mode)
        or stat.S_ISLNK(job.st_mode)
        or job.st_uid != root_uid
        or job.st_gid != root_gid
        or stat.S_IMODE(job.st_mode) != 0o755
        or not stat.S_ISREG(lock.st_mode)
        or stat.S_ISLNK(lock.st_mode)
        or lock.st_uid != root_uid
        or lock.st_gid != builder_gid
        or lock.st_nlink != 1
        or stat.S_IMODE(lock.st_mode) != 0o440
    ):
        _fail("rotation_stager_installer_foundation_invalid")


def _install_for_test(
    *,
    source_root: Path,
    source_remote: str,
    repository_url: str,
    release_revision: str,
    roots: InstallerRoots,
    production: bool = True,
    command_runner: Callable[[Sequence[str]], None] = _system_command,
    identity_validator: Callable[[], None] = _validate_builder_identity,
    foundation_validator: Callable[[InstallerRoots], None] | None = None,
    revision_qualified: bool = False,
) -> Mapping[str, Any]:
    if (
        not isinstance(roots, InstallerRoots)
        or _REVISION.fullmatch(release_revision or "") is None
        or _REMOTE.fullmatch(source_remote or "") is None
        or not isinstance(repository_url, str)
        or not repository_url
        or "\x00" in repository_url
        or not source_root.is_absolute()
        or ".." in source_root.parts
        or type(production) is not bool
        or type(revision_qualified) is not bool
        or (
            production
            and (
                not sys.platform.startswith("linux")
                or _read_posix_identity("geteuid") != 0
            )
        )
        or any(
            not path.is_absolute() or ".." in path.parts
            for path in (
                roots.library,
                roots.sysusers,
                roots.tmpfiles,
                roots.systemd,
                roots.libexec,
                roots.job_root,
                roots.promotion_lock,
                roots.library_releases,
            )
        )
    ):
        _fail("rotation_stager_installer_contract_invalid")
    if production and roots != InstallerRoots():
        _fail("rotation_stager_installer_contract_invalid")
    top = _line(_git(source_root, "rev-parse", "--show-toplevel"))
    try:
        if Path(top).resolve(strict=True) != source_root.resolve(strict=True):
            _fail("rotation_stager_installer_git_invalid")
    except OSError as exc:
        _fail("rotation_stager_installer_git_invalid", exc)
    urls = tuple(
        item
        for item in _git(source_root, "remote", "get-url", "--all", source_remote)
        .decode("utf-8", errors="strict")
        .splitlines()
        if item
    )
    if urls != (repository_url,):
        _fail("rotation_stager_installer_repository_mismatch")
    commit = _line(
        _git(source_root, "rev-parse", "--verify", f"{release_revision}^{{commit}}")
    )
    tree_oid = _line(_git(source_root, "rev-parse", f"{release_revision}^{{tree}}"))
    if commit != release_revision:
        _fail("rotation_stager_installer_revision_mismatch")
    rotation_raw = _git(
        source_root,
        "cat-file",
        "blob",
        (
            f"{release_revision}:"
            "scripts/canary/production_cutover_unit_input_rotation.py"
        ),
    )
    launcher_raw = _git(
        source_root,
        "cat-file",
        "blob",
        (
            f"{release_revision}:"
            "scripts/canary/production_release_rotation_stager_launcher.py"
        ),
    )
    promoter_raw = _git(
        source_root,
        "cat-file",
        "blob",
        (f"{release_revision}:scripts/canary/production_release_candidate_promoter.py"),
    )
    builder_unit_source = (
        "ops/muncho/release-updater/muncho-release-builder-v2@.service"
        if revision_qualified
        else "ops/muncho/release-updater/muncho-release-builder@.service"
    )
    builder_wrapper_source = (
        "ops/muncho/release-updater/muncho-release-foundation-exec-v2"
        if revision_qualified
        else "ops/muncho/release-updater/muncho-release-builder-phase"
    )
    builder_unit_raw = _git(
        source_root,
        "cat-file",
        "blob",
        f"{release_revision}:{builder_unit_source}",
    )
    builder_wrapper_raw = _git(
        source_root,
        "cat-file",
        "blob",
        f"{release_revision}:{builder_wrapper_source}",
    )
    try:
        rotation_tree = ast.parse(rotation_raw.decode("utf-8", errors="strict"))
        launcher_tree = ast.parse(launcher_raw.decode("utf-8", errors="strict"))
        promoter_tree = ast.parse(promoter_raw.decode("utf-8", errors="strict"))

        def literal_set(tree: ast.AST, name: str) -> frozenset[str]:
            for node in getattr(tree, "body", ()):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == name
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                    and node.value.func.id == "frozenset"
                    and len(node.value.args) == 1
                    and isinstance(node.value.args[0], ast.Set)
                ):
                    values = {
                        ast.literal_eval(item) for item in node.value.args[0].elts
                    }
                    if all(isinstance(item, str) for item in values):
                        return frozenset(values)
            _fail("rotation_stager_installer_protocol_drift")

        def literal_string(tree: ast.AST, name: str) -> str:
            for node in getattr(tree, "body", ()):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Name)
                    and node.targets[0].id == name
                    and isinstance(node.value, ast.Constant)
                    and isinstance(node.value.value, str)
                ):
                    return node.value.value
            _fail("rotation_stager_installer_asset_binding_invalid")

        if literal_set(rotation_tree, "RELEASE_PHASE_ACTIONS") != literal_set(
            launcher_tree, "_PHASE_ACTIONS"
        ):
            _fail("rotation_stager_installer_protocol_drift")
        unit_hash_name = (
            "PRODUCTION_REVISION_BUILDER_UNIT_FRAGMENT_SHA256"
            if revision_qualified
            else "PRODUCTION_BUILDER_UNIT_FRAGMENT_SHA256"
        )
        wrapper_hash_name = (
            "PRODUCTION_REVISION_BUILDER_WRAPPER_SHA256"
            if revision_qualified
            else "PRODUCTION_BUILDER_WRAPPER_SHA256"
        )
        if literal_string(
            promoter_tree,
            unit_hash_name,
        ) != _sha256(builder_unit_raw) or literal_string(
            promoter_tree,
            wrapper_hash_name,
        ) != _sha256(builder_wrapper_raw):
            _fail("rotation_stager_installer_asset_binding_invalid")
    except (SyntaxError, UnicodeError, ValueError, TypeError) as exc:
        _fail("rotation_stager_installer_protocol_drift", exc)

    library_root = (
        roots.library_releases / release_revision
        if revision_qualified
        else roots.library
    )
    for root in (
        roots.library_releases if revision_qualified else roots.library,
        roots.sysusers,
        roots.tmpfiles,
        roots.systemd,
        roots.libexec,
    ):
        _ensure_directory(
            root,
            create=(
                root
                == (roots.library_releases if revision_qualified else roots.library)
                or not production
            ),
            production=production,
        )
    library_children = (
        library_root,
        library_root / "scripts",
        library_root / "scripts/canary",
    )
    for selected in library_children:
        _ensure_directory(selected, create=True, production=production)

    installed: list[Mapping[str, Any]] = []
    created_count = 0
    selected_assets = (
        {**_REVISION_LIBRARY_ASSETS, **_REVISION_STATIC_ASSETS}
        if revision_qualified
        else _SOURCE_ASSETS
    )
    for source_relative in sorted(selected_assets):
        target_relative, mode = selected_assets[source_relative]
        raw = _git(
            source_root,
            "cat-file",
            "blob",
            f"{release_revision}:{source_relative}",
        )
        target = (
            library_root / target_relative
            if revision_qualified and source_relative in _REVISION_LIBRARY_ASSETS
            else _target(roots, target_relative)
        )
        created = _publish_exact(
            target,
            raw,
            mode=mode,
            expected_uid=(0 if production else _read_posix_identity("geteuid")),
            expected_gid=(0 if production else _read_posix_identity("getegid")),
        )
        created_count += int(created)
        installed.append({
            "source_relative_path": source_relative,
            "target_path": str(target),
            "mode": f"{mode:04o}",
            "sha256": _sha256(raw),
            "created": created,
        })
    for selected in reversed(library_children):
        os.chmod(selected, 0o555)
    os.chmod(library_root, 0o555)
    if revision_qualified:
        # The root-owned namespace must remain traversable and extensible by a
        # later privileged installer so a second exact revision can coexist.
        # Individual revision directories are sealed 0555 above; unprivileged
        # identities still cannot create, replace, or mutate entries here.
        os.chmod(roots.library_releases, 0o755)

    sysusers_path = roots.sysusers / "muncho-release-builder.conf"
    tmpfiles_path = roots.tmpfiles / "muncho-release-builder.conf"
    command_runner(("/usr/bin/systemd-sysusers", str(sysusers_path)))
    identity_validator()
    command_runner(("/usr/bin/systemd-tmpfiles", "--create", str(tmpfiles_path)))
    if foundation_validator is None:
        _validate_foundation(
            roots,
            root_uid=0,
            root_gid=0,
            builder_gid=phase.BUILDER_GID,
        )
    else:
        foundation_validator(roots)

    deterministic_assets = [
        {name: item[name] for name in ("source_relative_path", "target_path", "mode", "sha256")}
        for item in installed
    ]
    unsigned = {
        "schema": (
            REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA
            if revision_qualified
            else INSTALL_RECEIPT_SCHEMA
        ),
        "repository_url": repository_url,
        "source_remote": source_remote,
        "release_revision": release_revision,
        "source_tree_oid": tree_oid,
        "assets": installed,
        "asset_count": len(installed),
        "created_asset_count": created_count,
        "builder_identity": dict(phase._BUILDER_IDENTITY),
        "builder_identity_installed": True,
        "job_root": str(roots.job_root),
        "job_root_owner": (
            "root:root"
            if production
            else (
                f"{_read_posix_identity('geteuid')}:{_read_posix_identity('getegid')}"
            )
        ),
        "job_root_mode": "0755",
        "job_root_installed": True,
        "promotion_lock": str(roots.promotion_lock),
        "promotion_lock_installed": True,
        "systemd_daemon_reload_performed": False,
        "unit_enabled": False,
        "unit_started": False,
        "unit_scheduled": False,
        "activation_performed": False,
        "release_pointer_mutated": False,
        "gateway_mutated": False,
        "data_mutated": False,
        "credentials_mutated": False,
        "secret_material_recorded": False,
        "secret_digest_recorded": False,
    }
    if revision_qualified:
        unsigned = {
            **unsigned,
            "foundation_layout": "revision-qualified-v2",
            "foundation_asset_manifest_sha256": _sha256(
                _canonical(deterministic_assets)
            ),
        }
    return {**unsigned, "receipt_sha256": _sha256(_canonical(unsigned))}


def install_rotation_stager_foundation(**kwargs: Any) -> Mapping[str, Any]:
    return _install_for_test(roots=InstallerRoots(), production=True, **kwargs)


def install_revision_qualified_foundation(**kwargs: Any) -> Mapping[str, Any]:
    return _install_for_test(
        roots=InstallerRoots(),
        production=True,
        revision_qualified=True,
        **kwargs,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-remote", required=True)
    parser.add_argument("--repository-url", required=True)
    parser.add_argument("--release-revision", required=True)
    parser.add_argument("--revision-qualified", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        installer = (
            install_revision_qualified_foundation
            if arguments.revision_qualified
            else install_rotation_stager_foundation
        )
        receipt = installer(
            source_root=arguments.source_root,
            source_remote=arguments.source_remote,
            repository_url=arguments.repository_url,
            release_revision=arguments.release_revision,
        )
    except (OSError, RotationStagerInstallerError):
        print(
            '{"error_code":"rotation_stager_installation_failed","ok":false}',
            file=sys.stderr,
        )
        return 2
    print(_canonical(receipt).decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "INSTALL_RECEIPT_SCHEMA",
    "REVISION_QUALIFIED_INSTALL_RECEIPT_SCHEMA",
    "InstallerRoots",
    "RotationStagerInstallerError",
    "install_revision_qualified_foundation",
    "install_rotation_stager_foundation",
]
