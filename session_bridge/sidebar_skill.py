from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
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
        _assert_not_redirect(destination, "skill destination", missing_ok=True)
        if destination.exists() and _tree_matches_packaged_skill(destination):
            return destination

        staging = Path(
            tempfile.mkdtemp(prefix=f".{_SKILL_NAME}.install-", dir=skills)
        )
        backup: Path | None = None
        try:
            _copy_packaged_skill(staging)
            if not _tree_matches_packaged_skill(staging):
                raise OSError("packaged sidebar skill staging verification failed")

            if destination.exists():
                backup = _next_backup_path(skills)
                os.replace(destination, backup)
            try:
                os.replace(staging, destination)
            except BaseException:
                if backup is not None and not destination.exists():
                    os.replace(backup, destination)
                raise
            return destination
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)


def _copy_packaged_skill(destination: Path) -> None:
    source = resources.files("session_bridge").joinpath("assets", _SKILL_NAME)
    for relative in _SKILL_FILES:
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.joinpath(*relative.split("/")).read_bytes())


def _tree_matches_packaged_skill(directory: Path) -> bool:
    if not directory.is_dir():
        return False
    _assert_tree_has_no_redirects(directory)
    actual = {
        path.relative_to(directory).as_posix()
        for path in directory.rglob("*")
        if path.is_file()
    }
    if actual != set(_SKILL_FILES):
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
