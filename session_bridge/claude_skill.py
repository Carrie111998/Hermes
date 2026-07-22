"""Install the personal Claude Code unified session catalog skill."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from importlib import resources
import os
from pathlib import Path
import threading
from typing import Final

from .asset_installer import (
    AssetInstallSpec,
    _InstallIdentity,
    _copy_packaged_asset,
    _filesystem_install_lock as _shared_filesystem_install_lock,
    _guarded_replace,
    _tree_matches_packaged_asset,
    install_packaged_asset,
)


_SKILL_NAME: Final = "session-bridge"
_ASSET_NAME: Final = "claude-session-bridge"
_SKILL_FILES: Final = ("SKILL.md",)
_STAGING_MARKER_CONTENT: Final = b"session-bridge Claude skill staging v1\n"
_LOCK_WAIT_SECONDS: Final = 10.0
_INSTALL_LOCK = threading.RLock()
_INSTALL_SPEC = AssetInstallSpec(
    asset_name=_ASSET_NAME,
    destination_name=_SKILL_NAME,
    files=_SKILL_FILES,
    staging_marker_content=_STAGING_MARKER_CONTENT,
    error_label="Claude skill",
)


def resolve_claude_home(environ: Mapping[str, str] | None = None) -> Path:
    """Resolve CLAUDE_CONFIG_DIR without reading or mutating Claude state."""

    selected = os.environ if environ is None else environ
    configured = selected.get("CLAUDE_CONFIG_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".claude"


def install_claude_skill(claude_home: Path | str | None = None) -> Path:
    """Atomically install the packaged personal Claude catalog skill."""

    home = Path(claude_home) if claude_home is not None else resolve_claude_home()
    with _INSTALL_LOCK:
        return install_packaged_asset(
            home.expanduser() / "skills",
            _INSTALL_SPEC,
            copy_asset=_copy_packaged_skill,
            tree_matches=_tree_matches_packaged_skill,
            guarded_replace=_guarded_replace,
            filesystem_lock=_filesystem_install_lock,
        )


def claude_skill_digest() -> str:
    """Return the SHA-256 digest of the packaged SKILL.md bytes."""

    source = resources.files("session_bridge").joinpath(
        "assets", _ASSET_NAME, "SKILL.md"
    )
    return hashlib.sha256(source.read_bytes()).hexdigest()


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


def _filesystem_install_lock(skills: Path):
    return _shared_filesystem_install_lock(
        skills,
        skill_name=_SKILL_NAME,
        timeout_seconds=_LOCK_WAIT_SECONDS,
        timeout_message="Claude skill installer lock is busy",
    )
