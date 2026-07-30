from __future__ import annotations

import ntpath
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath


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


def ordinary_windows_path_identity(value: object) -> str | None:
    """Return an ordinary Windows filesystem-path identity without I/O.

    The caller keeps its own canonical spelling; this value exists only for
    validation and case/separator-insensitive comparison.
    """

    if type(value) is not str or not value:
        return None
    try:
        canonical = value.replace("/", "\\")
        if canonical.casefold().startswith(("\\\\?\\", "\\\\.\\")):
            return None
        drive, tail = ntpath.splitdrive(canonical)
        is_drive_qualified = (
            len(drive) == 2
            and drive[1] == ":"
            and "A" <= drive[0].upper() <= "Z"
            and tail.startswith("\\")
        )
        unc_parts = drive.lstrip("\\").split("\\")
        is_unc_qualified = (
            drive.startswith("\\\\")
            and len(unc_parts) == 2
            and all(unc_parts)
            and unc_parts[1].casefold() not in {"pipe", "mailslot", "ipc$"}
            and (not tail or tail.startswith("\\"))
        )
        identity = ntpath.normcase(ntpath.normpath(canonical))
        if (
            not (is_drive_qualified or is_unc_qualified)
            or identity != ntpath.normcase(canonical)
            or PureWindowsPath(canonical).is_reserved()
        ):
            return None
        components = [part for part in tail.lstrip("\\").split("\\") if part]
        reserved_names = {
            "con", "prn", "aux", "nul",
            *(f"com{number}" for number in range(1, 10)),
            *(f"lpt{number}" for number in range(1, 10)),
        }
        if any(
            component.endswith((".", " "))
            or ":" in component
            or component.split(".", 1)[0].casefold() in reserved_names
            for component in components
        ):
            return None
        return identity
    except (TypeError, ValueError):
        return None


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

    inbox_identity = ordinary_windows_path_identity(str(inbox))
    source_identity = ordinary_windows_path_identity(str(source))
    if inbox_identity is None:
        raise SidebarPlacementError("inbox_unavailable")
    if source_identity is None:
        raise SidebarPlacementError("source_identity_mismatch")
    roots = (str(inbox),)
    if inbox_identity != source_identity:
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
    except (OSError, RuntimeError, TypeError, ValueError):
        raise SidebarPlacementError("inbox_unavailable") from None
    if (
        not inbox.is_dir()
        or not home.is_dir()
        or configured_inbox_cwd != str(inbox)
        or _windows_identity(inbox) != _windows_identity(home)
        or ordinary_windows_path_identity(str(inbox)) is None
        or ordinary_windows_path_identity(str(home)) is None
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
    if ordinary_windows_path_identity(str(source)) is None:
        raise SidebarPlacementError("source_identity_mismatch")
    return source


def _windows_identity(path: Path) -> str:
    identity = ordinary_windows_path_identity(str(path))
    if identity is None:
        raise ValueError("path has no ordinary Windows identity")
    return identity
