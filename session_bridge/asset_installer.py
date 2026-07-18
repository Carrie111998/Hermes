"""Shared atomic installer for packaged personal-agent assets.

The redirect and identity checks protect pre-existing redirects, accidental path
changes, and cooperative concurrent installers. They are not a security boundary
against a malicious process running as the same OS user.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
import errno
from importlib import resources
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
import time
from typing import Final


_SAFE_NAME: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


@dataclass(frozen=True)
class AssetInstallSpec:
    """Describe one packaged directory installed below an agent skills root."""

    asset_name: str
    destination_name: str
    files: tuple[str, ...]
    staging_marker_content: bytes
    error_label: str = "skill"

    def validated(self) -> AssetInstallSpec:
        if not _SAFE_NAME.fullmatch(self.asset_name):
            raise ValueError("asset name must be a safe path component")
        if not _SAFE_NAME.fullmatch(self.destination_name):
            raise ValueError("destination name must be a safe path component")
        if not self.files or len(set(self.files)) != len(self.files):
            raise ValueError("asset files must be non-empty and unique")
        for relative in self.files:
            path = PurePosixPath(relative)
            if (
                not relative
                or path.is_absolute()
                or "\\" in relative
                or any(part in {"", ".", ".."} for part in path.parts)
            ):
                raise ValueError("asset file path must be a safe relative path")
        if not self.staging_marker_content:
            raise ValueError("staging marker content must be non-empty")
        return self

    @property
    def staging_prefix(self) -> str:
        return f".{self.destination_name}.install-"

    @property
    def staging_marker(self) -> str:
        return ".session-bridge-owned-staging"


CopyAsset = Callable[[Path, "_InstallIdentity"], None]
TreeMatches = Callable[..., bool]
GuardedReplace = Callable[[Path, Path, "_InstallIdentity"], None]


def install_packaged_asset(
    skills: Path | str,
    spec: AssetInstallSpec,
    *,
    copy_asset: CopyAsset | None = None,
    tree_matches: TreeMatches | None = None,
    guarded_replace: GuardedReplace | None = None,
    filesystem_lock: Callable[[Path], AbstractContextManager[None]] | None = None,
) -> Path:
    """Atomically install one verified packaged asset below ``skills``."""

    selected = spec.validated()
    root = Path(skills).expanduser()
    _ensure_directory(root, f"{selected.error_label} skills directory")
    destination = root / selected.destination_name
    copier = copy_asset or (
        lambda target, identity: _copy_packaged_asset(target, selected, identity)
    )
    matcher = tree_matches or (
        lambda directory, **kwargs: _tree_matches_packaged_asset(
            directory, selected, **kwargs
        )
    )
    replacer = guarded_replace or _guarded_replace
    lock_factory = filesystem_lock or (
        lambda directory: _filesystem_install_lock(
            directory,
            skill_name=selected.destination_name,
            timeout_seconds=10.0,
            timeout_message=f"{selected.error_label} installer lock is busy",
        )
    )

    with lock_factory(root):
        identity = _InstallIdentity.capture(root)
        _remove_verified_stale_staging(root, identity, selected)
        identity.revalidate()
        _assert_not_redirect(destination, "skill destination", missing_ok=True)
        if destination.exists() and matcher(destination):
            return destination

        identity.revalidate()
        staging = Path(tempfile.mkdtemp(prefix=selected.staging_prefix, dir=root))
        identity.revalidate()
        staging_identity = identity.extend(staging)
        _guarded_write_bytes(
            staging / selected.staging_marker,
            selected.staging_marker_content,
            staging_identity,
        )
        backup: Path | None = None
        try:
            copier(staging, staging_identity)
            if not matcher(staging, allow_staging_marker=True):
                raise OSError(
                    f"packaged {selected.error_label} staging verification failed"
                )
            _guarded_unlink(staging / selected.staging_marker, staging_identity)
            if not matcher(staging):
                raise OSError(
                    f"packaged {selected.error_label} staging verification failed"
                )

            if destination.exists():
                backup = _next_backup_path(root, selected.destination_name)
                replacer(destination, backup, identity)
            try:
                replacer(staging, destination, identity)
            except BaseException as promotion_error:
                try:
                    identity.revalidate()
                except PermissionError:
                    raise
                if backup is not None and not destination.exists():
                    try:
                        replacer(backup, destination, identity)
                    except BaseException as restore_error:
                        raise BaseExceptionGroup(
                            f"{selected.error_label} promotion and backup restoration both failed",
                            [promotion_error, restore_error],
                        ) from None
                raise
            return destination
        except BaseException as operation_error:
            cleanup_error = _cleanup_current_staging(staging, staging_identity)
            if cleanup_error is not None:
                raise BaseExceptionGroup(
                    f"{selected.error_label} install and staging cleanup both failed",
                    [operation_error, cleanup_error],
                ) from None
            raise


def _copy_packaged_asset(
    destination: Path,
    spec: AssetInstallSpec,
    identity: _InstallIdentity | None = None,
) -> None:
    source = resources.files("session_bridge").joinpath("assets", spec.asset_name)
    for relative in spec.files:
        target = destination.joinpath(*relative.split("/"))
        if identity is not None:
            identity.revalidate()
        target.parent.mkdir(parents=True, exist_ok=True)
        if identity is not None:
            identity.revalidate()
            _guarded_write_bytes(
                target, source.joinpath(*relative.split("/")).read_bytes(), identity
            )
        else:
            target.write_bytes(source.joinpath(*relative.split("/")).read_bytes())


def _tree_matches_packaged_asset(
    directory: Path,
    spec: AssetInstallSpec,
    *,
    allow_staging_marker: bool = False,
) -> bool:
    if not directory.is_dir():
        return False
    _assert_tree_has_no_redirects(directory)
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }
    expected = set(spec.files)
    if allow_staging_marker:
        expected.add(spec.staging_marker)
    if actual != expected:
        return False
    if allow_staging_marker and (
        directory.joinpath(spec.staging_marker).read_bytes()
        != spec.staging_marker_content
    ):
        return False
    source = resources.files("session_bridge").joinpath("assets", spec.asset_name)
    return all(
        directory.joinpath(*relative.split("/")).read_bytes()
        == source.joinpath(*relative.split("/")).read_bytes()
        for relative in spec.files
    )


def _ensure_directory(path: Path, label: str) -> None:
    _assert_no_redirect_components(path, label)
    _assert_not_redirect(path, label, missing_ok=True)
    path.mkdir(parents=True, exist_ok=True)
    _assert_not_redirect(path, label)
    if not path.is_dir():
        raise NotADirectoryError(f"{label} is not a directory")


def _assert_no_redirect_components(path: Path, label: str) -> None:
    absolute = path.absolute()
    for component in reversed((absolute, *absolute.parents)):
        _assert_not_redirect(component, label, missing_ok=True)


def _assert_tree_has_no_redirects(root: Path) -> None:
    _assert_not_redirect(root, "skill tree")
    for path in root.rglob("*"):
        _assert_not_redirect(path, "skill tree entry")


def _assert_not_redirect(path: Path, label: str, *, missing_ok: bool = False) -> None:
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    attributes = getattr(info, "st_file_attributes", 0)
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(info.st_mode) or bool(attributes & reparse):
        raise PermissionError(f"{label} must not be a redirect")


def _next_backup_path(skills: Path, skill_name: str) -> Path:
    base = skills / f"{skill_name}.backup"
    candidate = base
    suffix = 0
    while candidate.exists() or candidate.is_symlink():
        suffix += 1
        candidate = skills / f"{skill_name}.backup-{suffix}"
    return candidate


@dataclass(frozen=True)
class _PathIdentity:
    path: Path
    device: int
    inode: int
    file_type: int
    attributes: int

    @classmethod
    def capture(cls, path: Path) -> _PathIdentity:
        _assert_not_redirect(path, "installer path")
        info = os.lstat(path)
        return cls(
            path=path,
            device=info.st_dev,
            inode=info.st_ino,
            file_type=stat.S_IFMT(info.st_mode),
            attributes=getattr(info, "st_file_attributes", 0),
        )

    def revalidate(self) -> None:
        try:
            _assert_not_redirect(self.path, "installer path")
            info = os.lstat(self.path)
        except (FileNotFoundError, PermissionError) as error:
            raise PermissionError(
                f"installer path identity changed: {self.path}"
            ) from error
        current = (
            info.st_dev,
            info.st_ino,
            stat.S_IFMT(info.st_mode),
            getattr(info, "st_file_attributes", 0),
        )
        expected = (self.device, self.inode, self.file_type, self.attributes)
        if current != expected:
            raise PermissionError(f"installer path identity changed: {self.path}")


@dataclass(frozen=True)
class _InstallIdentity:
    entries: tuple[_PathIdentity, ...]

    @classmethod
    def capture(cls, leaf: Path) -> _InstallIdentity:
        absolute = leaf.absolute()
        chain = tuple(reversed((absolute, *absolute.parents)))
        return cls(tuple(_PathIdentity.capture(path) for path in chain))

    def extend(self, path: Path) -> _InstallIdentity:
        self.revalidate()
        return _InstallIdentity((*self.entries, _PathIdentity.capture(path.absolute())))

    def revalidate(self) -> None:
        for entry in self.entries:
            entry.revalidate()


def _guarded_write_bytes(
    path: Path, content: bytes, identity: _InstallIdentity
) -> None:
    identity.revalidate()
    path.write_bytes(content)
    identity.revalidate()


def _guarded_unlink(path: Path, identity: _InstallIdentity) -> None:
    identity.revalidate()
    path.unlink()
    identity.revalidate()


def _guarded_replace(
    source: Path, destination: Path, identity: _InstallIdentity
) -> None:
    identity.revalidate()
    _assert_not_redirect(source, "installer mutation source")
    _assert_not_redirect(destination, "installer mutation destination", missing_ok=True)
    os.replace(source, destination)
    identity.revalidate()


def _remove_verified_stale_staging(
    skills: Path, identity: _InstallIdentity, spec: AssetInstallSpec
) -> None:
    identity.revalidate()
    for candidate in skills.glob(f"{spec.staging_prefix}*"):
        identity.revalidate()
        if not _is_verified_owned_staging(candidate, spec):
            continue
        candidate_identity = identity.extend(candidate)
        candidate_identity.revalidate()
        shutil.rmtree(candidate)
        identity.revalidate()


def _is_verified_owned_staging(candidate: Path, spec: AssetInstallSpec) -> bool:
    try:
        if not candidate.is_dir():
            return False
        _assert_tree_has_no_redirects(candidate)
        marker = candidate / spec.staging_marker
        if marker.read_bytes() != spec.staging_marker_content:
            return False
        allowed = {spec.staging_marker, *spec.files}
        actual = {
            path.relative_to(candidate).as_posix()
            for path in candidate.rglob("*")
            if path.is_file()
        }
        if not actual <= allowed:
            return False
        source = resources.files("session_bridge").joinpath("assets", spec.asset_name)
        return all(
            relative == spec.staging_marker
            or candidate.joinpath(*relative.split("/")).read_bytes()
            == source.joinpath(*relative.split("/")).read_bytes()
            for relative in actual
        )
    except (FileNotFoundError, OSError, PermissionError):
        return False


def _cleanup_current_staging(
    staging: Path, identity: _InstallIdentity
) -> BaseException | None:
    try:
        identity.revalidate()
    except PermissionError:
        return None
    if not staging.exists():
        return None
    try:
        shutil.rmtree(staging)
    except BaseException as error:
        return error
    return None


@contextmanager
def _filesystem_install_lock(
    skills: Path,
    *,
    skill_name: str,
    timeout_seconds: float,
    timeout_message: str,
) -> Iterator[None]:
    lock_path = skills / f".{skill_name}.install.lock"
    descriptor = _open_lock_descriptor(lock_path)
    deadline = time.monotonic() + timeout_seconds
    locked = False
    try:
        while not locked:
            locked = _try_lock_descriptor(descriptor)
            if locked:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError(timeout_message) from None
            time.sleep(0.05)
        yield
    finally:
        try:
            if locked:
                _unlock_descriptor(descriptor)
        finally:
            descriptor.close()


def _open_lock_descriptor(path: Path):
    _assert_not_redirect(path, "installer lock", missing_ok=True)
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_BINARY", 0)
    raw_descriptor = os.open(path, flags, 0o600)
    try:
        descriptor = os.fdopen(raw_descriptor, "r+b", buffering=0)
    except BaseException:
        os.close(raw_descriptor)
        raise
    try:
        _assert_not_redirect(path, "installer lock")
        opened = os.fstat(descriptor.fileno())
        current = os.lstat(path)
        if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
            raise PermissionError("installer lock identity changed while opening")
        if opened.st_size == 0:
            descriptor.write(b"\0")
        descriptor.seek(0)
        return descriptor
    except BaseException:
        descriptor.close()
        raise


def _try_lock_descriptor(descriptor) -> bool:
    descriptor.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(descriptor.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            if error.errno in {errno.EACCES, errno.EAGAIN, 13, 36}:
                return False
            raise
        return True
    import fcntl

    try:
        fcntl.flock(descriptor.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN}:
            return False
        raise
    return True


def _unlock_descriptor(descriptor) -> None:
    descriptor.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(descriptor.fileno(), msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(descriptor.fileno(), fcntl.LOCK_UN)
