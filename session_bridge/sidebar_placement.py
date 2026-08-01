from __future__ import annotations

import ipaddress
import ntpath
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


_WIN32_COMPONENT_FORBIDDEN = frozenset('<>:"|?*')
_UNC_PCHAR_FORBIDDEN = frozenset('"\\\\/:|<>+=;,*?[]')
_RESERVED_WINDOWS_COMPONENT_NAMES = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{number}" for number in range(1, 10)),
        *(f"lpt{number}" for number in range(1, 10)),
        "com¹",
        "com²",
        "com³",
        "lpt¹",
        "lpt²",
        "lpt³",
    }
)
_MAX_UNC_SHARE_LENGTH = 80
_MAX_UNC_TAIL_COMPONENT_LENGTH = 255
_MAX_NETBIOS_NAME_BYTES = 15
_MAX_FQDN_LABEL_BYTES = 63
_MAX_FQDN_BYTES = 255


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


def filesystem_path_identity(
    value: object,
    *,
    platform: Literal["windows", "posix"] | None = None,
) -> str | None:
    """Return the canonical, host-platform filesystem identity without I/O."""

    selected_platform = platform or ("windows" if os.name == "nt" else "posix")
    if selected_platform == "windows":
        return ordinary_windows_path_identity(value)
    if selected_platform == "posix":
        return _ordinary_posix_path_identity(value)
    return None


def placement_paths_equivalent(
    left: object,
    right: object,
    *,
    platform: Literal["windows", "posix"] | None = None,
) -> bool:
    left_identity = filesystem_path_identity(left, platform=platform)
    right_identity = filesystem_path_identity(right, platform=platform)
    return left_identity is not None and left_identity == right_identity


def _ordinary_posix_path_identity(value: object) -> str | None:
    if type(value) is not str or not value or "\x00" in value:
        return None
    if value == "/":
        return value
    if (
        not value.startswith("/")
        or value.startswith("//")
        or value.endswith("/")
    ):
        return None
    components = value[1:].split("/")
    if any(component in {"", ".", ".."} for component in components):
        return None
    return value


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
        ):
            return None
        components = [part for part in tail.lstrip("\\").split("\\") if part]
        if is_unc_qualified:
            server, share = unc_parts
            if not (
                _is_ordinary_unc_server(server)
                and _is_ordinary_unc_share(share)
                and all(_is_ordinary_unc_tail_component(part) for part in components)
            ):
                return None
        elif any(
            not _is_ordinary_windows_component(component) for component in components
        ):
            return None
        return identity
    except (TypeError, ValueError):
        return None


def _is_ordinary_unc_server(component: str) -> bool:
    ipv6_literal_suffix = ".ipv6-literal.net"
    if component.casefold().endswith(ipv6_literal_suffix):
        transformed_address = component[: -len(ipv6_literal_suffix)]
        if (
            not transformed_address
            or any(character in ":%[]" for character in transformed_address)
            or any(
                character not in "0123456789abcdefABCDEF-."
                for character in transformed_address
            )
        ):
            return False
        try:
            ipaddress.IPv6Address(transformed_address.replace("-", ":"))
        except ValueError:
            return False
        return True

    labels = component.split(".")
    if len(labels) > 1 and all(label.isdecimal() for label in labels):
        try:
            ipaddress.IPv4Address(component)
        except ValueError:
            return False
        return True

    if _is_recognizable_ipv6_lookalike(component):
        return False
    if _is_ordinary_netbios_name(component):
        return True
    return _is_ordinary_fqdn(component)


def _is_recognizable_ipv6_lookalike(component: str) -> bool:
    candidate = component
    if candidate.startswith("["):
        closing_bracket = candidate.find("]")
        if closing_bracket > 1:
            candidate = (
                candidate[1:closing_bracket] + candidate[closing_bracket + 1 :]
            )
    address, separator, _zone = candidate.partition("%")
    if separator and not address:
        return False
    try:
        ipaddress.IPv6Address(address)
    except ValueError:
        return False
    return True


def _is_ordinary_netbios_name(component: str) -> bool:
    try:
        encoded = component.encode("latin-1")
    except UnicodeEncodeError:
        return False
    return 1 <= len(encoded) <= _MAX_NETBIOS_NAME_BYTES and "\x00" not in component


def _is_ordinary_fqdn(component: str) -> bool:
    if not component or _utf8_byte_length(component) > _MAX_FQDN_BYTES:
        return False
    labels = component.split(".")
    return (
        sum(_utf8_byte_length(label) + 1 for label in labels) + 1
        <= _MAX_FQDN_BYTES
        and any(character.isalpha() for character in labels[-1])
        and all(
            0 < _utf8_byte_length(label) <= _MAX_FQDN_LABEL_BYTES
            and not label.startswith("-")
            and not label.endswith("-")
            and all(character.isalnum() or character == "-" for character in label)
            for label in labels
        )
    )


def _is_ordinary_unc_share(component: str) -> bool:
    return _is_ordinary_unc_pchar_component(
        component,
        maximum_length=_MAX_UNC_SHARE_LENGTH,
    )


def _is_ordinary_unc_tail_component(component: str) -> bool:
    return _is_ordinary_unc_pchar_component(
        component,
        maximum_length=_MAX_UNC_TAIL_COMPONENT_LENGTH,
    )


def _is_ordinary_unc_pchar_component(
    component: str,
    *,
    maximum_length: int,
) -> bool:
    return (
        1 <= len(component) <= maximum_length
        and not any(
            ord(character) <= 31 or character in _UNC_PCHAR_FORBIDDEN
            for character in component
        )
    )


def _is_ordinary_windows_component(
    component: str,
    *,
    forbidden: frozenset[str] = _WIN32_COMPONENT_FORBIDDEN,
) -> bool:
    return (
        not component.endswith((".", " "))
        and not any(
            ord(character) <= 31 or character in forbidden
            for character in component
        )
        and component.split(".", 1)[0].casefold()
        not in _RESERVED_WINDOWS_COMPONENT_NAMES
    )


def _utf8_byte_length(value: str) -> int:
    try:
        return len(value.encode("utf-8"))
    except UnicodeEncodeError:
        return _MAX_FQDN_BYTES + 1


def resolve_sidebar_placement(
    configured_inbox_cwd: str,
    hermes_home: Path | str,
    placement_generation: int,
    source_cwd: str | None,
) -> SidebarPlacement:
    inbox = _resolve_canonical_inbox(configured_inbox_cwd, hermes_home)
    if not isinstance(placement_generation, int) or isinstance(
        placement_generation, bool
    ) or placement_generation != 1:
        raise SidebarPlacementError("source_identity_mismatch")
    if source_cwd is None:
        return SidebarPlacement(
            inbox_cwd=str(inbox),
            local_host="local",
            runtime_workspace_roots=(str(inbox),),
            placement_generation=placement_generation,
        )
    source = _resolve_source(source_cwd)

    inbox_identity = filesystem_path_identity(str(inbox))
    source_identity = filesystem_path_identity(str(source))
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
    raw_inbox_identity = _raw_filesystem_path_identity(
        configured_inbox_cwd,
        error_code="inbox_unavailable",
    )
    raw_home_identity = _raw_filesystem_path_identity(
        hermes_home,
        allow_path=True,
        error_code="inbox_unavailable",
    )
    try:
        configured_path = Path(configured_inbox_cwd)
        if not configured_path.is_absolute():
            raise SidebarPlacementError("inbox_unavailable")
        inbox = configured_path.resolve(strict=True)
        home = Path(hermes_home).resolve(strict=True)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise SidebarPlacementError("inbox_unavailable") from None
    inbox_identity = filesystem_path_identity(str(inbox))
    home_identity = filesystem_path_identity(str(home))
    if (
        not inbox.is_dir()
        or not home.is_dir()
        or inbox_identity is None
        or home_identity is None
        or raw_inbox_identity != inbox_identity
        or raw_home_identity != home_identity
        or inbox_identity != home_identity
    ):
        raise SidebarPlacementError("inbox_unavailable")
    return inbox


def _resolve_source(source_cwd: str) -> Path:
    raw_source_identity = _raw_filesystem_path_identity(
        source_cwd,
        error_code="source_identity_mismatch",
    )
    try:
        source_path = Path(source_cwd)
        if not source_path.is_absolute():
            raise SidebarPlacementError("source_identity_mismatch")
        source = source_path.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        raise SidebarPlacementError("source_identity_mismatch") from None
    if not source.is_dir():
        raise SidebarPlacementError("source_identity_mismatch")
    source_identity = filesystem_path_identity(str(source))
    if source_identity is None or raw_source_identity != source_identity:
        raise SidebarPlacementError("source_identity_mismatch")
    return source


def _windows_identity(path: Path) -> str:
    identity = ordinary_windows_path_identity(str(path))
    if identity is None:
        raise ValueError("path has no ordinary Windows identity")
    return identity


def _raw_filesystem_path_identity(
    value: object,
    *,
    error_code: str,
    allow_path: bool = False,
) -> str:
    if type(value) is str:
        raw_path = value
    elif allow_path and isinstance(value, Path):
        raw_path = str(value)
    else:
        raise SidebarPlacementError(error_code)
    identity = filesystem_path_identity(raw_path)
    if identity is None:
        raise SidebarPlacementError(error_code)
    return identity
