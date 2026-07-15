from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
import errno
from importlib import resources
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
from typing import Final


_SKILL_NAME: Final = "session-sidebar-sync"
_SKILL_FILES: Final = ("SKILL.md", "agents/openai.yaml")
_INSTALL_LOCK = threading.RLock()
_LOCK_WAIT_SECONDS: Final = 10.0
_STAGING_PREFIX: Final = f".{_SKILL_NAME}.install-"
_STAGING_MARKER: Final = ".session-bridge-owned-staging"
_STAGING_MARKER_CONTENT: Final = b"session-bridge sidebar skill staging v1\n"


def resolve_codex_home(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve CODEX_HOME without reading or mutating any Codex state."""

    selected = os.environ if environ is None else environ
    configured = selected.get("CODEX_HOME")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".codex"


def install_sidebar_skill(codex_home: Path | str | None = None) -> Path:
    """Atomically install the packaged personal Codex sidebar skill."""

    home = Path(codex_home) if codex_home is not None else resolve_codex_home()
    home = home.expanduser()
    _ensure_directory(home, "Codex home")
    skills = home / "skills"
    _ensure_directory(skills, "Codex skills directory")
    destination = skills / _SKILL_NAME

    with _INSTALL_LOCK, _filesystem_install_lock(skills):
        identity = _InstallIdentity.capture(skills)
        _remove_verified_stale_staging(skills, identity)
        identity.revalidate()
        _assert_not_redirect(destination, "skill destination", missing_ok=True)
        if destination.exists() and _tree_matches_packaged_skill(destination):
            return destination

        identity.revalidate()
        staging = Path(tempfile.mkdtemp(prefix=_STAGING_PREFIX, dir=skills))
        identity.revalidate()
        staging_identity = identity.extend(staging)
        _guarded_write_bytes(
            staging / _STAGING_MARKER,
            _STAGING_MARKER_CONTENT,
            staging_identity,
        )
        backup: Path | None = None
        try:
            _copy_packaged_skill(staging, staging_identity)
            if not _tree_matches_packaged_skill(staging, allow_staging_marker=True):
                raise OSError("packaged sidebar skill staging verification failed")
            _guarded_unlink(staging / _STAGING_MARKER, staging_identity)
            if not _tree_matches_packaged_skill(staging):
                raise OSError("packaged sidebar skill staging verification failed")

            if destination.exists():
                backup = _next_backup_path(skills)
                _guarded_replace(destination, backup, identity)
            try:
                _guarded_replace(staging, destination, identity)
            except BaseException:
                try:
                    identity.revalidate()
                except PermissionError:
                    raise
                if backup is not None and not destination.exists():
                    _guarded_replace(backup, destination, identity)
                raise
            return destination
        except BaseException as operation_error:
            cleanup_error = _cleanup_current_staging(staging, staging_identity)
            if cleanup_error is not None:
                raise BaseExceptionGroup(
                    "sidebar skill install and staging cleanup both failed",
                    [operation_error, cleanup_error],
                ) from None
            raise


def _copy_packaged_skill(
    destination: Path, identity: _InstallIdentity | None = None
) -> None:
    source = resources.files("session_bridge").joinpath("assets", _SKILL_NAME)
    for relative in _SKILL_FILES:
        target = destination.joinpath(*relative.split("/"))
        if identity is not None:
            identity.revalidate()
        target.parent.mkdir(parents=True, exist_ok=True)
        if identity is not None:
            identity.revalidate()
            _guarded_write_bytes(
                target,
                source.joinpath(*relative.split("/")).read_bytes(),
                identity,
            )
        else:
            target.write_bytes(source.joinpath(*relative.split("/")).read_bytes())


def _tree_matches_packaged_skill(
    directory: Path, *, allow_staging_marker: bool = False
) -> bool:
    if not directory.is_dir():
        return False
    _assert_tree_has_no_redirects(directory)
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }
    expected = set(_SKILL_FILES)
    if allow_staging_marker:
        expected.add(_STAGING_MARKER)
    if actual != expected:
        return False
    if allow_staging_marker and (
        directory.joinpath(_STAGING_MARKER).read_bytes() != _STAGING_MARKER_CONTENT
    ):
        return False
    source = resources.files("session_bridge").joinpath("assets", _SKILL_NAME)
    return all(
        directory.joinpath(*relative.split("/")).read_bytes()
        == source.joinpath(*relative.split("/")).read_bytes()
        for relative in _SKILL_FILES
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


def _next_backup_path(skills: Path) -> Path:
    base = skills / f"{_SKILL_NAME}.backup"
    candidate = base
    suffix = 0
    while candidate.exists() or candidate.is_symlink():
        suffix += 1
        candidate = skills / f"{_SKILL_NAME}.backup-{suffix}"
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
    skills: Path, identity: _InstallIdentity
) -> None:
    identity.revalidate()
    for candidate in skills.glob(f"{_STAGING_PREFIX}*"):
        identity.revalidate()
        if not _is_verified_owned_staging(candidate):
            continue
        candidate_identity = identity.extend(candidate)
        candidate_identity.revalidate()
        shutil.rmtree(candidate)
        identity.revalidate()


def _is_verified_owned_staging(candidate: Path) -> bool:
    try:
        if not candidate.is_dir():
            return False
        _assert_tree_has_no_redirects(candidate)
        marker = candidate / _STAGING_MARKER
        if marker.read_bytes() != _STAGING_MARKER_CONTENT:
            return False
        allowed = {_STAGING_MARKER, *_SKILL_FILES}
        actual = {
            path.relative_to(candidate).as_posix()
            for path in candidate.rglob("*")
            if path.is_file()
        }
        if not actual <= allowed:
            return False
        source = resources.files("session_bridge").joinpath("assets", _SKILL_NAME)
        return all(
            relative == _STAGING_MARKER
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
def _filesystem_install_lock(skills: Path) -> Iterator[None]:
    lock_path = skills / f".{_SKILL_NAME}.install.lock"
    descriptor = _open_lock_descriptor(lock_path)
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    locked = False
    try:
        while not locked:
            locked = _try_lock_descriptor(descriptor)
            if locked:
                break
            if time.monotonic() >= deadline:
                raise TimeoutError("sidebar skill installer lock is busy") from None
            time.sleep(0.05)
        yield
    finally:
        if locked:
            _unlock_descriptor(descriptor)
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
