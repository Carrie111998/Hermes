from __future__ import annotations

import ntpath
from dataclasses import dataclass
from pathlib import Path


class SidebarPlacementError(ValueError):
    def __init__(self, code: str) -> None:
        if code not in {"inbox_unavailable", "source_identity_mismatch"}:
            raise ValueError("sidebar placement error code is not fixed")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class SidebarPlacement:
    inbox_cwd: str
    local_host: str
    runtime_workspace_roots: tuple[str] | tuple[str, str]
    placement_generation: int


def resolve_sidebar_placement(
    configured_inbox_cwd: str,
    hermes_home: Path | str,
    placement_generation: int,
    source_cwd: str,
) -> SidebarPlacement:
    inbox = _resolve_canonical_inbox(configured_inbox_cwd, hermes_home)
    if not isinstance(placement_generation, int) or isinstance(
        placement_generation, bool
    ) or placement_generation != 1:
        raise SidebarPlacementError("source_identity_mismatch")
    source = _resolve_source(source_cwd)

    roots = (str(inbox),)
    if _windows_identity(inbox) != _windows_identity(source):
        roots += (str(source),)
    return SidebarPlacement(
        inbox_cwd=str(inbox),
        local_host="local",
        runtime_workspace_roots=roots,
        placement_generation=placement_generation,
    )


def _resolve_canonical_inbox(configured_inbox_cwd: str, hermes_home: Path | str) -> Path:
    if not isinstance(configured_inbox_cwd, str) or not configured_inbox_cwd:
        raise SidebarPlacementError("inbox_unavailable")
    try:
        configured_path = Path(configured_inbox_cwd)
        if not configured_path.is_absolute():
            raise SidebarPlacementError("inbox_unavailable")
        inbox = configured_path.resolve(strict=True)
        home = Path(hermes_home).resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise SidebarPlacementError("inbox_unavailable") from None
    if (
        not inbox.is_dir()
        or not home.is_dir()
        or configured_inbox_cwd != str(inbox)
        or _windows_identity(inbox) != _windows_identity(home)
    ):
        raise SidebarPlacementError("inbox_unavailable")
    return inbox


def _resolve_source(source_cwd: str) -> Path:
    if not isinstance(source_cwd, str):
        raise SidebarPlacementError("source_identity_mismatch")
    try:
        source_path = Path(source_cwd)
        if not source_path.is_absolute():
            raise SidebarPlacementError("source_identity_mismatch")
        source = source_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise SidebarPlacementError("source_identity_mismatch") from None
    if not source.is_dir():
        raise SidebarPlacementError("source_identity_mismatch")
    return source


def _windows_identity(path: Path) -> str:
    return ntpath.normcase(ntpath.normpath(str(path)))
