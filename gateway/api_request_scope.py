"""Immutable tenant identity for the shared API-server listener.

The HTTP API exposes public session/run/approval identifiers unchanged.  The
single multiplex listener must never use those raw values as process-local or
remote-memory keys, because two profiles may legitimately choose the same
value.  This module supplies the small, dependency-free identity primitive
used to domain-separate every internal API key.
"""

from __future__ import annotations

import hashlib
import json
import errno
import os
import re
import secrets
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Union


class APIRequestScopeError(RuntimeError):
    """The selected route profile and active runtime home disagree."""


class APIProfileGenerationError(APIRequestScopeError):
    """A listener-owned profile directory changed after startup."""


_API_PROFILE_MARKER_FILENAME = ".api-server-profile-id"
_API_PROFILE_MARKER_BYTES = 32
_API_PROFILE_MARKER_RE = re.compile(r"^[0-9a-f]{64}$")
_API_PROFILE_MARKER_READ_LIMIT = 128


def _open_marker_no_follow(path: Path) -> int:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    return os.open(path, flags)


def _read_profile_marker(path: Path) -> tuple[os.stat_result, str]:
    """Read and validate an existing host-owned profile marker."""

    marker_lstat = path.lstat()
    expected_uid = os.geteuid() if hasattr(os, "geteuid") else None
    if (
        not stat.S_ISREG(marker_lstat.st_mode)
        or stat.S_IMODE(marker_lstat.st_mode) != 0o600
        or (
            expected_uid is not None
            and int(marker_lstat.st_uid) != int(expected_uid)
        )
    ):
        raise APIRequestScopeError(
            f"API profile marker is not an owner-only regular file: {path}"
        )
    descriptor = _open_marker_no_follow(path)
    try:
        marker_fstat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(marker_fstat.st_mode)
            or stat.S_IMODE(marker_fstat.st_mode) != 0o600
            or (
                expected_uid is not None
                and int(marker_fstat.st_uid) != int(expected_uid)
            )
            or (
                int(marker_lstat.st_dev),
                int(marker_lstat.st_ino),
            )
            != (
                int(marker_fstat.st_dev),
                int(marker_fstat.st_ino),
            )
        ):
            raise APIRequestScopeError(
                f"API profile marker changed while it was opened: {path}"
            )
        chunks: list[bytes] = []
        remaining = _API_PROFILE_MARKER_READ_LIMIT + 1
        while remaining:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)
    if len(raw) > _API_PROFILE_MARKER_READ_LIMIT:
        raise APIRequestScopeError(f"API profile marker is oversized: {path}")
    try:
        marker_id = raw.decode("ascii").strip()
    except UnicodeDecodeError as exc:
        raise APIRequestScopeError(
            f"API profile marker is malformed: {path}"
        ) from exc
    if not _API_PROFILE_MARKER_RE.fullmatch(marker_id):
        raise APIRequestScopeError(f"API profile marker is malformed: {path}")
    return marker_fstat, marker_id


def _create_profile_marker(path: Path) -> None:
    """Atomically initialize one marker; concurrent creators converge."""

    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    marker = f"{secrets.token_hex(_API_PROFILE_MARKER_BYTES)}\n".encode("ascii")
    descriptor = os.open(path, flags, 0o600)
    try:
        # Do not rely on the process umask being sane.
        os.fchmod(descriptor, 0o600)
        view = memoryview(marker)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short write while creating API profile marker")
            view = view[written:]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if os.name == "posix":
        directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        directory_flags |= getattr(os, "O_DIRECTORY", 0)
        directory_descriptor = None
        try:
            directory_descriptor = os.open(path.parent, directory_flags)
            os.fsync(directory_descriptor)
        except OSError as exc:
            if exc.errno not in {
                errno.EBADF,
                errno.EINVAL,
                getattr(errno, "ENOTSUP", errno.EINVAL),
            }:
                raise
        finally:
            if directory_descriptor is not None:
                os.close(directory_descriptor)


def _profile_marker_identity(
    canonical_home: Path,
    *,
    initialize: bool,
) -> tuple[os.stat_result, str]:
    """Return a stable marker, optionally bootstrapping it at startup."""

    marker_path = canonical_home / _API_PROFILE_MARKER_FILENAME
    if initialize:
        try:
            _create_profile_marker(marker_path)
        except FileExistsError:
            pass

    # A losing concurrent O_EXCL creator can observe the winning file before
    # its payload is fully written. Retry only this tiny bootstrap window.
    attempts = 50 if initialize else 1
    last_error: BaseException | None = None
    for attempt in range(attempts):
        try:
            return _read_profile_marker(marker_path)
        except (FileNotFoundError, APIRequestScopeError) as exc:
            last_error = exc
            if attempt + 1 == attempts:
                break
            time.sleep(0.002)
    if isinstance(last_error, APIRequestScopeError):
        raise last_error
    raise APIRequestScopeError(
        f"API profile marker is missing: {marker_path}"
    ) from last_error


@dataclass(frozen=True, slots=True)
class APIProfileIdentity:
    """Host-owned identity of one profile directory at listener startup."""

    profile: str
    source_home: str
    canonical_home: str
    profile_generation: str


def capture_api_profile_identity(
    profile: object,
    home: Path,
    *,
    initialize_marker: bool = True,
) -> APIProfileIdentity:
    """Freeze the canonical path and filesystem generation of one profile."""

    profile_name = str(profile or "").strip()
    if not profile_name:
        raise APIRequestScopeError("API profile name is required")
    try:
        source_path = Path(home).expanduser().absolute()
        source_stat = source_path.lstat()
        canonical_path = source_path.resolve(strict=True)
        target_stat = canonical_path.stat()
        if not stat.S_ISDIR(target_stat.st_mode):
            raise APIRequestScopeError(
                f"API profile home is not a directory: {source_path}"
            )
        marker_stat, marker_id = _profile_marker_identity(
            canonical_path,
            initialize=initialize_marker,
        )
        generation_payload = json.dumps(
            (
                int(source_stat.st_dev),
                int(source_stat.st_ino),
                int(target_stat.st_dev),
                int(target_stat.st_ino),
                int(marker_stat.st_dev),
                int(marker_stat.st_ino),
                marker_id,
            ),
            separators=(",", ":"),
        )
        generation = "fs-v3:" + hashlib.sha256(
            generation_payload.encode("ascii")
        ).hexdigest()
    except (OSError, RuntimeError) as exc:
        raise APIRequestScopeError(
            f"API profile {profile_name!r} cannot be resolved at startup"
        ) from exc
    return APIProfileIdentity(
        profile=profile_name,
        source_home=str(source_path),
        canonical_home=str(canonical_path),
        profile_generation=generation,
    )


def freeze_api_profile_inventory(
    served_profiles: Iterable[tuple[str, Path]],
) -> tuple[APIProfileIdentity, ...]:
    """Capture one immutable listener-owned profile inventory."""

    identities = tuple(
        capture_api_profile_identity(profile, home)
        for profile, home in served_profiles
    )
    names = [identity.profile for identity in identities]
    if len(names) != len(set(names)):
        raise APIRequestScopeError("API profile inventory contains duplicates")
    homes = [identity.canonical_home for identity in identities]
    if len(homes) != len(set(homes)):
        raise APIRequestScopeError(
            "API profile inventory maps multiple profiles to one canonical home"
        )
    return identities


def validate_api_profile_inventory(
    inventory: object,
) -> tuple[APIProfileIdentity, ...]:
    """Validate and verify one already-frozen listener inventory.

    The returned object is the exact input tuple.  In particular, consumers
    must not rebuild identities from profile names and paths: GatewayRunner
    owns the listener-lifetime snapshot and every adapter shares it verbatim.
    """

    if not isinstance(inventory, tuple) or not inventory:
        raise APIRequestScopeError(
            "API listener has no frozen served profile inventory"
        )
    if not all(
        isinstance(identity, APIProfileIdentity)
        for identity in inventory
    ):
        raise APIRequestScopeError(
            "API profile inventory contains an invalid identity"
        )
    names = [identity.profile for identity in inventory]
    if len(names) != len(set(names)):
        raise APIRequestScopeError("API profile inventory contains duplicates")
    homes = [identity.canonical_home for identity in inventory]
    if len(homes) != len(set(homes)):
        raise APIRequestScopeError(
            "API profile inventory maps multiple profiles to one canonical home"
        )
    for identity in inventory:
        verify_api_profile_identity(identity)
    return inventory


def verify_api_profile_identity(identity: APIProfileIdentity) -> None:
    """Fail closed if a served profile was removed, replaced, or retargeted."""

    try:
        current = capture_api_profile_identity(
            identity.profile,
            Path(identity.source_home),
            initialize_marker=False,
        )
    except APIRequestScopeError as exc:
        raise APIProfileGenerationError(
            f"API profile {identity.profile!r} changed after listener startup; "
            "restart required"
        ) from exc
    if current != identity:
        raise APIProfileGenerationError(
            f"API profile {identity.profile!r} changed after listener startup; "
            "restart required"
        )


def verify_api_request_scope(scope: "APIRequestScope") -> None:
    """Verify the filesystem authority embedded in an immutable request scope."""

    verify_api_profile_identity(
        APIProfileIdentity(
            profile=scope.profile,
            source_home=scope.source_home,
            canonical_home=scope.canonical_home,
            profile_generation=scope.profile_generation,
        )
    )


@dataclass(frozen=True, slots=True)
class APIRequestScope:
    """One immutable, domain-separated API identity.

    ``profile`` is retained for diagnostics and for an explicit second tenant
    binding.  ``canonical_home`` is the filesystem authority.  ``kind`` and
    ``raw_id`` prevent equal public strings in different state domains (for
    example a session ID and a run ID) from aliasing.
    """

    canonical_home: str
    source_home: str
    profile: str
    profile_generation: str
    kind: str
    raw_id: str

    def __post_init__(self) -> None:
        if not self.canonical_home:
            raise ValueError("canonical_home is required")
        if not self.source_home:
            raise ValueError("source_home is required")
        if not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", self.kind):
            raise ValueError(f"invalid API request scope kind: {self.kind!r}")
        if not self.profile:
            raise ValueError("profile is required")
        if not self.profile_generation:
            raise ValueError("profile_generation is required")

    @property
    def public_id(self) -> str:
        """The caller-visible identifier, intentionally unchanged."""

        return self.raw_id

    @property
    def internal_key(self) -> str:
        """Stable opaque key binding home, profile, domain, and raw ID."""

        payload = json.dumps(
            (
                self.canonical_home,
                self.profile,
                self.profile_generation,
                self.kind,
                self.raw_id,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return f"api_server:v1:{self.kind}:{digest}"

    def bind(self, kind: str, raw_id: object) -> "APIRequestScope":
        """Return a new identity under the same immutable tenant authority."""

        return APIRequestScope(
            canonical_home=self.canonical_home,
            source_home=self.source_home,
            profile=self.profile,
            profile_generation=self.profile_generation,
            kind=kind,
            raw_id=str(raw_id or ""),
        )

    def internal_session_key(
        self,
        raw_key: object,
        *,
        kind: str,
    ) -> str:
        """Scope an API memory/control key while preserving valid native keys.

        Native gateway session keys already carry the profile namespace:
        ``agent:main:*`` for the default profile and
        ``agent:<profile>:*`` for named profiles.  A correctly namespaced key
        remains unchanged so intentional API-to-native continuity keeps
        working.  Arbitrary keys, and native-looking keys for another profile,
        are isolated through the stable opaque form.
        """

        raw = str(raw_key or "")
        if not raw:
            return ""
        match = re.match(r"^agent:([^:]+):", raw)
        expected_namespace = "main" if self.profile == "default" else self.profile
        if (
            kind in {"memory", "runner-model"}
            and match is not None
            and match.group(1) == expected_namespace
        ):
            return raw
        return self.bind(kind, raw).internal_key


ServedProfile = Union[APIProfileIdentity, tuple[str, Path]]


def _coerce_served_profile(identity: ServedProfile) -> APIProfileIdentity:
    if isinstance(identity, APIProfileIdentity):
        return identity
    profile, home = identity
    return capture_api_profile_identity(profile, home)


def resolve_api_request_scope(
    *,
    current_home: Path,
    selected_profile: Optional[str],
    multiplex: bool,
    served_profiles: Iterable[ServedProfile] = (),
    active_profile: Optional[str] = None,
    kind: str,
    raw_id: object,
) -> APIRequestScope:
    """Build a scope only after host-owned route/home authority agrees.

    In multiplex mode the selected URL prefix is interpreted solely as a
    profile *name* and resolved through ``served_profiles``.  The request's
    active runtime home must be exactly that canonical served home.  An
    unprefixed request and ``/p/default`` both normalize to ``default``.

    Single-profile mode has no shared listener tenant boundary; its current
    runtime home is authoritative and ``active_profile`` is diagnostic only.
    """

    canonical_current = str(Path(current_home).expanduser().resolve())
    selected = str(selected_profile or "").strip()
    if multiplex:
        profile = selected or "default"
        served = {
            identity.profile: identity
            for identity in map(_coerce_served_profile, served_profiles)
        }
        identity = served.get(profile)
        if identity is None:
            raise APIRequestScopeError(
                f"API profile {profile!r} is not in the served profile set"
            )
        if canonical_current != identity.canonical_home:
            raise APIRequestScopeError(
                "API request profile scope mismatch: "
                f"{profile!r} resolved to {identity.canonical_home}, "
                f"but the active runtime home is {canonical_current}"
            )
        verify_api_profile_identity(identity)
    else:
        profile = selected or str(active_profile or "").strip() or "default"
        served = tuple(map(_coerce_served_profile, served_profiles))
        if served:
            identity = next(
                (item for item in served if item.profile == profile),
                None,
            )
            if identity is None or canonical_current != identity.canonical_home:
                raise APIRequestScopeError(
                    "API request profile scope mismatch for the single-profile "
                    "listener"
                )
            verify_api_profile_identity(identity)
        else:
            identity = capture_api_profile_identity(profile, Path(canonical_current))

    return APIRequestScope(
        canonical_home=identity.canonical_home,
        source_home=identity.source_home,
        profile=profile,
        profile_generation=identity.profile_generation,
        kind=kind,
        raw_id=str(raw_id or ""),
    )


def resolve_multiplex_api_route_scope(
    *,
    selected_profile: Optional[str],
    served_profiles: Iterable[ServedProfile],
    kind: str = "request",
    raw_id: object = "",
) -> APIRequestScope:
    """Resolve one multiplex route from a single host-owned inventory snapshot.

    This variant is used by middleware *before* entering the selected profile
    runtime scope.  It never consults the caller's current home and never
    accepts a path from HTTP input; the canonical home comes exclusively from
    ``served_profiles``.
    """

    profile = str(selected_profile or "").strip() or "default"
    served = {
        identity.profile: identity
        for identity in map(_coerce_served_profile, served_profiles)
    }
    identity = served.get(profile)
    if identity is None:
        raise APIRequestScopeError(
            f"API profile {profile!r} is not in the served profile set"
        )
    verify_api_profile_identity(identity)
    return APIRequestScope(
        canonical_home=identity.canonical_home,
        source_home=identity.source_home,
        profile=profile,
        profile_generation=identity.profile_generation,
        kind=kind,
        raw_id=str(raw_id or ""),
    )
