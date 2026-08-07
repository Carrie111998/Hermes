"""Strict, fork-local Muncho release identity and completion contracts.

The human Muncho version is an alias for an immutable Git SHA.  Hermes keeps
its own upstream package version; this package never reads or mutates it.
"""

from .metadata import (
    ReleaseBundle,
    ReleaseHistory,
    ReleaseHistoryEntry,
    ReleaseMetadata,
    ReleaseMetadataError,
    ReleaseNotes,
    SemVer,
    load_optional_release_bundle,
    load_release_bundle,
    resolve_exact_release_sha,
)

__all__ = [
    "ReleaseBundle",
    "ReleaseHistory",
    "ReleaseHistoryEntry",
    "ReleaseMetadata",
    "ReleaseMetadataError",
    "ReleaseNotes",
    "SemVer",
    "load_optional_release_bundle",
    "load_release_bundle",
    "resolve_exact_release_sha",
]
