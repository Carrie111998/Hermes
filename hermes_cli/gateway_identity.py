"""Release-aware identity helpers for the gateway watchdogs.

This module deliberately has no gateway/runtime imports at module import time.
It is used by the long-lived gateway and by the short-lived calendar guard, so
both paths must agree about what code is actually running.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping

SERVICE_NAME = "hermes-gateway.service"
CANONICAL_MARKER = ".hermes-release-sha"
LEGACY_MARKERS = ("RELEASE_COMMIT", "RELEASE_SHA")
KNOWN_MARKERS = (CANONICAL_MARKER, *LEGACY_MARKERS)
_UNKNOWN_MARKER_RE = re.compile(
    r"^(?:release[-_.]?[a-z0-9]+|\.hermes[-_.]release[-_.]?[a-z0-9]+)$",
    re.IGNORECASE,
)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")


class GatewayIdentityError(RuntimeError):
    """Raised when the active gateway identity cannot be proved safely."""


class ReleaseMarkerMissing(GatewayIdentityError):
    """Raised when a tree has no recognized release marker."""


@dataclass(frozen=True)
class GatewayIdentity:
    fingerprint: str
    source: str
    release_path: Path | None
    marker_name: str | None
    service_properties: Mapping[str, str]


def _read_git_fingerprint(project_root: Path) -> str | None:
    from hermes_cli.main import _read_git_revision_fingerprint

    return _read_git_revision_fingerprint(project_root)


def _marker_values(release_path: Path) -> tuple[str, str | None]:
    values: dict[str, str] = {}
    try:
        for name in KNOWN_MARKERS:
            path = release_path / name
            if not path.is_file():
                continue
            value = path.read_text(encoding="utf-8", errors="strict").strip()
            if not value:
                raise GatewayIdentityError(f"empty release marker: {path}")
            if not _SHA_RE.fullmatch(value):
                raise GatewayIdentityError(f"invalid release marker: {path}")
            values[name] = value

    except OSError as exc:
        raise GatewayIdentityError(
            f"release directory unavailable: {release_path}: {exc}"
        ) from exc

    try:
        for child in release_path.iterdir():
            if (
                child.is_file()
                and _UNKNOWN_MARKER_RE.match(child.name)
                and child.name not in KNOWN_MARKERS
                and child.suffix.lower() not in {".md", ".txt", ".rst"}
            ):
                raise GatewayIdentityError(
                    f"unrecognized release marker: {child.name}"
                )
    except OSError as exc:
        raise GatewayIdentityError(
            f"release directory unavailable: {release_path}: {exc}"
        ) from exc

    if not values:
        # A checkout has no release marker by design. Do not scan its whole
        # tree for release-like names; only release snapshots use this check.
        raise ReleaseMarkerMissing(f"release marker missing: {release_path}")
    distinct = set(values.values())
    if len(distinct) > 1:
        rendered = ", ".join(f"{k}={v}" for k, v in sorted(values.items()))
        raise GatewayIdentityError(f"conflicting release markers: {rendered}")
    marker = CANONICAL_MARKER if CANONICAL_MARKER in values else next(iter(values))
    return next(iter(distinct)), marker


def identity_from_project(
    project_root: Path, *, allow_git_fallback: bool = True
) -> GatewayIdentity:
    """Read the identity of the Python tree containing the running code."""
    project_root = project_root.resolve()
    try:
        value, marker = _marker_values(project_root)
    except ReleaseMarkerMissing:
        if not allow_git_fallback:
            raise GatewayIdentityError(
                f"release marker missing: {project_root}"
            ) from None
        fingerprint = _read_git_fingerprint(project_root)
        if not fingerprint:
            raise GatewayIdentityError(f"cannot read gateway identity: {project_root}") from None
        return GatewayIdentity(fingerprint, "checkout", None, None, {})
    return GatewayIdentity(f"release:{value}", "release", project_root, marker, {})


def parse_systemd_properties(text: str) -> dict[str, str]:
    props: dict[str, str] = {}
    for line in text.splitlines():
        if not line or "=" not in line:
            continue
        key, value = line.split("=", 1)
        props[key] = value
    required = ("ActiveState", "SubState", "MainPID", "WorkingDirectory")
    missing = [key for key in required if key not in props]
    if missing:
        raise GatewayIdentityError(f"systemd properties missing: {', '.join(missing)}")
    if "\n" in props["WorkingDirectory"] or "\r" in props["WorkingDirectory"]:
        raise GatewayIdentityError("systemd WorkingDirectory is multi-line")
    if not props["MainPID"].isdigit():
        raise GatewayIdentityError(f"invalid systemd MainPID: {props['MainPID']!r}")
    return props


def read_user_service_properties(
    service_name: str = SERVICE_NAME,
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, str]:
    """Read only the user-level service scope used by the DGX gateway."""
    if runner is None:
        def runner(args: list[str], **kwargs):
            return subprocess.run(args, **kwargs)
    args = [
        "systemctl",
        "--user",
        "show",
        service_name,
        "--property=ActiveState,SubState,MainPID,WorkingDirectory",
    ]
    try:
        result = runner(args, check=True, capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GatewayIdentityError(f"cannot query user systemd: {exc}") from exc
    return parse_systemd_properties(result.stdout)


def active_gateway_identity(
    hermes_home: Path,
    *,
    project_root: Path,
    service_name: str = SERVICE_NAME,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> GatewayIdentity:
    props = read_user_service_properties(service_name, runner=runner)
    try:
        working_dir = Path(props["WorkingDirectory"]).expanduser().resolve()
    except (OSError, RuntimeError) as exc:
        raise GatewayIdentityError(
            f"invalid gateway WorkingDirectory: {props['WorkingDirectory']!r}: {exc}"
        ) from exc
    releases_root = (hermes_home / "releases").resolve()
    legacy_roots = {project_root.resolve(), (hermes_home / "hermes-agent").resolve()}
    if working_dir in legacy_roots:
        identity = identity_from_project(working_dir, allow_git_fallback=True)
    else:
        try:
            working_dir.relative_to(releases_root)
        except ValueError:
            raise GatewayIdentityError(
                f"gateway WorkingDirectory is outside releases: {working_dir}"
            ) from None
        identity = identity_from_project(working_dir, allow_git_fallback=False)
    return GatewayIdentity(
        identity.fingerprint,
        identity.source,
        identity.release_path,
        identity.marker_name,
        props,
    )


def short_fingerprint(fingerprint: str) -> str:
    value = fingerprint.rsplit(":", 1)[-1]
    return value[:10] if len(value) > 10 else value
