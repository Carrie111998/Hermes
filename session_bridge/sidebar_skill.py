from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib import resources
import json
import os
from pathlib import Path
import shutil
import stat
import tempfile
import threading
import time
from typing import Final
import uuid

import psutil


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
    token = uuid.uuid4().hex
    deadline = time.monotonic() + _LOCK_WAIT_SECONDS
    while True:
        _assert_not_redirect(lock_path, "installer lock", missing_ok=True)
        try:
            descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _stale_lock(lock_path, deadline_reached=time.monotonic() >= deadline):
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise TimeoutError("sidebar skill installer lock is busy") from None
            time.sleep(0.05)
            continue
        try:
            payload = json.dumps({"pid": os.getpid(), "token": token}).encode("ascii")
            os.write(descriptor, payload)
        finally:
            os.close(descriptor)
        break
    try:
        yield
    finally:
        try:
            payload = json.loads(lock_path.read_text(encoding="ascii"))
            if payload.get("token") == token:
                lock_path.unlink()
        except (FileNotFoundError, OSError, UnicodeError, ValueError, AttributeError):
            pass


def _stale_lock(path: Path, *, deadline_reached: bool) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="ascii"))
        pid = payload.get("pid")
        if type(pid) is int and pid > 0:
            return not psutil.pid_exists(pid)
    except (OSError, UnicodeError, ValueError, AttributeError):
        pass
    return deadline_reached
