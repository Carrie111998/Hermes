"""Install the sidebar skill with portable cooperative filesystem hardening.

The redirect and identity checks protect pre-existing redirects, accidental path
changes, and cooperative concurrent installers. They are not a security boundary
against a malicious process running as the same OS user: such a process can swap a
path between filesystem syscalls and can modify the installed skill afterward.
CODEX_HOME and every parent directory must therefore be user-owned and not writable
by other principals.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import os
from pathlib import Path
import threading
import time
from typing import Final

from .asset_installer import (
    AssetInstallSpec,
    _InstallIdentity,
    _copy_packaged_asset,
    _ensure_directory,
    _guarded_replace,
    _open_lock_descriptor,
    _tree_matches_packaged_asset,
    _try_lock_descriptor,
    _unlock_descriptor,
    install_packaged_asset,
)


_SKILL_NAME: Final = "session-sidebar-sync"
_SKILL_FILES: Final = ("SKILL.md", "agents/openai.yaml")
_INSTALL_LOCK = threading.RLock()
_LOCK_WAIT_SECONDS: Final = 10.0
_STAGING_MARKER_CONTENT: Final = b"session-bridge sidebar skill staging v1\n"
_INSTALL_SPEC = AssetInstallSpec(
    asset_name=_SKILL_NAME,
    destination_name=_SKILL_NAME,
    files=_SKILL_FILES,
    staging_marker_content=_STAGING_MARKER_CONTENT,
    error_label="sidebar skill",
)


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
    with _INSTALL_LOCK:
        return install_packaged_asset(
            home / "skills",
            _INSTALL_SPEC,
            copy_asset=_copy_packaged_skill,
            tree_matches=_tree_matches_packaged_skill,
            guarded_replace=_guarded_replace,
            filesystem_lock=_filesystem_install_lock,
        )


def _copy_packaged_skill(
    destination: Path, identity: _InstallIdentity | None = None
) -> None:
    _copy_packaged_asset(destination, _INSTALL_SPEC, identity)


def _tree_matches_packaged_skill(
    directory: Path, *, allow_staging_marker: bool = False
) -> bool:
    return _tree_matches_packaged_asset(
        directory,
        _INSTALL_SPEC,
        allow_staging_marker=allow_staging_marker,
    )


@contextmanager
def _filesystem_install_lock(skills: Path) -> Iterator[None]:
    """Serialize installers while preserving legacy monkeypatch/test seams."""

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
        try:
            if locked:
                _unlock_descriptor(descriptor)
        finally:
            descriptor.close()


__all__ = ["install_sidebar_skill", "resolve_codex_home"]
