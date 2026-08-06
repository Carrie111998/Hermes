"""Non-sensitive release provenance for the Hermes Runtime health surface.

Release builds carry a small JSON contract next to the installed source tree.
The contract is deliberately separate from OCI labels: labels are metadata on
the image object and are not available to a process running inside that image.
Source checkouts continue to use live Git state, while installed artifacts use
the contract plus a digest of the allowlisted Runtime files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


SERVICE_NAME = "hermes-runtime"
BUILD_METADATA_FILENAME = ".hermes_build_metadata.json"
LEGACY_BUILD_SHA_FILENAME = ".hermes_build_sha"

# Keep the artifact definition narrow and stable.  In particular, the metadata
# file itself is not inside this list, so the digest cannot be self-referential
# or become a digest of metadata alone.
_RUNTIME_PATHS = (
    "agent",
    "gateway",
    "hermes_cli",
    "tools",
    "hermes_state.py",
    "model_tools.py",
    "run_agent.py",
    "toolsets.py",
    "pyproject.toml",
)
_RUNTIME_FILE_MAX_BYTES = 64 * 1024 * 1024
_RUNTIME_TOTAL_MAX_BYTES = 256 * 1024 * 1024
_BUILD_METADATA_MAX_BYTES = 4096
_BUILD_METADATA_VERSION = 1
_UNKNOWN = "unknown"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_BUILD_METADATA_KEYS = frozenset({
    "format_version",
    "git_commit",
    "release_id",
    "build_timestamp",
    "runtime_sha256",
})
_COMMIT_RE = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_RELEASE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}\Z")
_BUILD_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}"
    r"(?:\.\d{1,9})?(?:Z|\+00:00)\Z"
)
_RUNTIME_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class RuntimeProvenanceConfig:
    """Allowlisted effective config whose digest cannot contain credentials."""

    listen_host: str
    listen_port: int
    cors_origins: tuple[str, ...]
    model_name: str
    max_concurrent_runs: int


@dataclass(frozen=True)
class RuntimeProvenance:
    service_name: str
    release_id: str
    version: str
    git_commit: str
    build_timestamp: str
    binary_sha256: str
    schema_version: str
    config_digest: str
    startup_timestamp: str
    source_dirty: Optional[bool]
    provenance_complete: bool
    provenance_errors: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible copy of the immutable startup snapshot."""
        result = asdict(self)
        result["provenance_errors"] = list(self.provenance_errors)
        return result


@dataclass(frozen=True)
class _BuildMetadata:
    git_commit: str
    release_id: str
    build_timestamp: str
    runtime_sha256: str


@dataclass(frozen=True)
class _SourceIdentity:
    git_commit: str
    artifact_digest: str
    source_dirty: Optional[bool]
    errors: tuple[str, ...]
    source_checkout: bool


class BuildProvenanceError(ValueError):
    """A bounded, non-sensitive build provenance validation error."""


def _valid_commit(value: object) -> bool:
    return (
        isinstance(value, str)
        and _COMMIT_RE.fullmatch(value) is not None
        and bool(value.strip("0"))
    )


def _valid_release_id(value: object) -> bool:
    if not isinstance(value, str) or _RELEASE_ID_RE.fullmatch(value) is None:
        return False
    return value.casefold() not in {
        "dev",
        "development",
        "none",
        "null",
        "unknown",
    }


def _valid_build_timestamp(value: object) -> bool:
    if not isinstance(value, str) or _BUILD_TIMESTAMP_RE.fullmatch(value) is None:
        return False
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith("Z") else value
        )
    except ValueError:
        return False
    return parsed.utcoffset() == timedelta(0)


def _valid_runtime_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and _RUNTIME_SHA256_RE.fullmatch(value) is not None
        and bool(value[7:].strip("0"))
    )


def _run_git(project_root: Path, *args: str) -> Optional[bytes]:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=project_root,
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _git_source_identity(project_root: Path) -> Optional[_SourceIdentity]:
    commit_raw = _run_git(project_root, "rev-parse", "--verify", "HEAD")
    if not commit_raw:
        return None
    commit = commit_raw.decode("ascii", errors="replace").strip()
    if not _valid_commit(commit):
        return None

    status = _run_git(project_root, "status", "--porcelain", "--untracked-files=normal")
    tree = _run_git(
        project_root,
        "ls-tree",
        "-r",
        "-z",
        "HEAD",
        "--",
        *_RUNTIME_PATHS,
    )
    diff = _run_git(
        project_root,
        "diff",
        "--binary",
        "HEAD",
        "--",
        *_RUNTIME_PATHS,
    )
    untracked = _run_git(
        project_root,
        "ls-files",
        "-z",
        "--others",
        "--exclude-standard",
        "--",
        *_RUNTIME_PATHS,
    )
    errors: list[str] = []
    if status is None:
        errors.append("source state unavailable")
        source_dirty = None
    else:
        source_dirty = bool(status.strip())
        if source_dirty:
            errors.append("source checkout is dirty")

    if tree is None or diff is None or untracked is None:
        return _SourceIdentity(
            git_commit=commit,
            artifact_digest=_UNKNOWN,
            source_dirty=source_dirty,
            errors=tuple(errors + ["runtime artifact digest unavailable"]),
            source_checkout=True,
        )

    digest = hashlib.sha256()
    digest.update(tree)
    digest.update(b"\0working-tree-diff\0")
    digest.update(diff)
    for raw_path in sorted(filter(None, untracked.split(b"\0"))):
        path = project_root / raw_path.decode("utf-8", errors="surrogateescape")
        try:
            content = path.read_bytes()
        except OSError:
            errors.append("runtime artifact digest unavailable")
            return _SourceIdentity(
                commit,
                _UNKNOWN,
                source_dirty,
                tuple(errors),
                True,
            )
        digest.update(b"\0untracked\0")
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(content)
    return _SourceIdentity(
        git_commit=commit,
        artifact_digest=f"sha256:{digest.hexdigest()}",
        source_dirty=source_dirty,
        errors=tuple(errors),
        source_checkout=True,
    )


def _runtime_files(project_root: Path) -> Optional[tuple[tuple[str, Path], ...]]:
    """Return sorted regular files in the Runtime artifact allowlist.

    Symlinks are rejected instead of followed.  That keeps the identity tied
    to the code tree and prevents an installed artifact from hashing content
    outside the tree it claims to describe.
    """
    files: list[tuple[str, Path]] = []
    try:
        for relative_root in _RUNTIME_PATHS:
            candidate = project_root / relative_root
            if candidate.is_symlink():
                return None
            if candidate.is_file():
                files.append((relative_root, candidate))
                continue
            if not candidate.is_dir():
                continue
            for child in candidate.rglob("*"):
                if child.is_symlink():
                    return None
                if child.is_file():
                    files.append((child.relative_to(project_root).as_posix(), child))
    except OSError:
        return None
    return tuple(sorted(files))


def _runtime_artifact_digest(project_root: Path) -> str:
    """Hash allowlisted Runtime file paths and bytes, excluding metadata."""
    files = _runtime_files(project_root)
    if not files:
        return _UNKNOWN

    digest = hashlib.sha256()
    total_bytes = 0
    for relative_path, path in files:
        try:
            size = path.stat().st_size
            if size > _RUNTIME_FILE_MAX_BYTES:
                return _UNKNOWN
            with path.open("rb") as handle:
                content = handle.read(_RUNTIME_FILE_MAX_BYTES + 1)
        except OSError:
            return _UNKNOWN
        if len(content) > _RUNTIME_FILE_MAX_BYTES:
            return _UNKNOWN
        if len(content) != size:
            return _UNKNOWN
        total_bytes += size
        if total_bytes > _RUNTIME_TOTAL_MAX_BYTES:
            return _UNKNOWN
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(size.to_bytes(8, "big"))
        digest.update(content)
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def _remove_build_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:
        raise BuildProvenanceError("unable to update build provenance") from exc


def _atomic_write_text(path: Path, content: str, *, encoding: str) -> None:
    """Replace one build record without exposing partial file contents."""
    temporary_path: Optional[Path] = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding=encoding,
            newline="\n",
            prefix=f".{path.name}.",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o644)
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as exc:
        raise BuildProvenanceError("unable to update build provenance") from exc
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def stamp_build_provenance(
    project_root: Path,
    git_commit: str,
    release_id: str,
    build_timestamp: str,
) -> None:
    """Validate and atomically stamp the build-time provenance contract.

    This is the only writer used by the Docker build, and it shares the
    validators and artifact digest with runtime verification.  The three
    values are explicit inputs so build metadata never becomes runtime
    environment configuration.
    """
    root = project_root.resolve()
    metadata_path = root / BUILD_METADATA_FILENAME
    legacy_sha_path = root / LEGACY_BUILD_SHA_FILENAME

    if not any((git_commit, release_id, build_timestamp)):
        _remove_build_file(metadata_path)
        _remove_build_file(legacy_sha_path)
        return

    if not release_id and not build_timestamp:
        if not _valid_commit(git_commit):
            raise BuildProvenanceError("git commit is invalid")
        _remove_build_file(metadata_path)
        _atomic_write_text(legacy_sha_path, f"{git_commit}\n", encoding="ascii")
        return

    if not git_commit or not release_id or not build_timestamp:
        raise BuildProvenanceError(
            "git commit, release id, and build timestamp are required"
        )
    if not _valid_commit(git_commit):
        raise BuildProvenanceError("git commit is invalid")
    if not _valid_release_id(release_id):
        raise BuildProvenanceError("release id is invalid")
    if not _valid_build_timestamp(build_timestamp):
        raise BuildProvenanceError("build timestamp is invalid")

    runtime_sha256 = _runtime_artifact_digest(root)
    if runtime_sha256 == _UNKNOWN:
        raise BuildProvenanceError("runtime artifact digest unavailable")
    metadata = {
        "build_timestamp": build_timestamp,
        "format_version": _BUILD_METADATA_VERSION,
        "git_commit": git_commit,
        "release_id": release_id,
        "runtime_sha256": runtime_sha256,
    }
    encoded_metadata = (
        json.dumps(metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n"
    )
    _atomic_write_text(legacy_sha_path, f"{git_commit}\n", encoding="ascii")
    _atomic_write_text(metadata_path, encoded_metadata, encoding="utf-8")


def _installed_source_identity(project_root: Path) -> _SourceIdentity:
    artifact_digest = _runtime_artifact_digest(project_root)
    if artifact_digest == _UNKNOWN:
        return _SourceIdentity(
            git_commit=_UNKNOWN,
            artifact_digest=_UNKNOWN,
            source_dirty=None,
            errors=("runtime artifact digest unavailable",),
            source_checkout=False,
        )
    return _SourceIdentity(
        git_commit=_UNKNOWN,
        artifact_digest=artifact_digest,
        # An installed tree has no source checkout to be dirty.  The digest
        # check below is what upgrades this fact into complete provenance.
        source_dirty=False,
        errors=(),
        source_checkout=False,
    )


def _source_identity(project_root: Path) -> _SourceIdentity:
    return _git_source_identity(project_root) or _installed_source_identity(
        project_root
    )


def _no_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _read_build_metadata(
    project_root: Path,
) -> tuple[Optional[_BuildMetadata], tuple[str, ...]]:
    """Read and validate only the bounded, allowlisted build contract."""
    path = project_root / BUILD_METADATA_FILENAME
    try:
        if path.is_symlink() or not path.is_file():
            return None, ("build metadata unavailable",)
        if path.stat().st_size > _BUILD_METADATA_MAX_BYTES:
            return None, ("build metadata malformed",)
        with path.open("rb") as handle:
            raw = handle.read(_BUILD_METADATA_MAX_BYTES + 1)
        if len(raw) > _BUILD_METADATA_MAX_BYTES:
            return None, ("build metadata malformed",)
    except OSError:
        return None, ("build metadata unavailable",)

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_no_duplicate_json_keys,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None, ("build metadata malformed",)
    if not isinstance(payload, dict) or set(payload) != _BUILD_METADATA_KEYS:
        return None, ("build metadata incomplete",)
    if (
        type(payload.get("format_version")) is not int
        or payload.get("format_version") != _BUILD_METADATA_VERSION
    ):
        return None, ("build metadata invalid",)

    git_commit = payload.get("git_commit")
    release_id = payload.get("release_id")
    build_timestamp = payload.get("build_timestamp")
    runtime_sha256 = payload.get("runtime_sha256")
    if not (
        _valid_commit(git_commit)
        and _valid_release_id(release_id)
        and _valid_build_timestamp(build_timestamp)
        and _valid_runtime_sha256(runtime_sha256)
    ):
        return None, ("build metadata invalid",)
    return (
        _BuildMetadata(
            git_commit=git_commit,
            release_id=release_id,
            build_timestamp=build_timestamp,
            runtime_sha256=runtime_sha256,
        ),
        (),
    )


def _config_digest(config: RuntimeProvenanceConfig) -> str:
    normalized = {
        "listen_host": config.listen_host.strip().lower(),
        "listen_port": config.listen_port,
        "cors_origins": sorted({
            origin.strip().lower() for origin in config.cors_origins if origin.strip()
        }),
        "model_name": config.model_name.strip(),
        "max_concurrent_runs": config.max_concurrent_runs,
    }
    encoded = json.dumps(
        normalized,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _rfc3339(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (
        value
        .astimezone(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def collect_runtime_provenance(
    config: RuntimeProvenanceConfig,
    *,
    startup_timestamp: datetime,
    project_root: Optional[Path] = None,
    version: Optional[str] = None,
    schema_version: Optional[str] = None,
) -> RuntimeProvenance:
    """Collect an immutable, non-secret provenance snapshot for this process."""
    root = (project_root or _PROJECT_ROOT).resolve()
    identity = _source_identity(root)
    errors = list(identity.errors)

    if version is None:
        try:
            from gateway.api_server_shared import _hermes_version

            version = _hermes_version()
        except ImportError:
            version = "dev"
    version = (version or "").strip() or "dev"
    if version.casefold() in {"dev", "development", _UNKNOWN}:
        errors.append("version unavailable")

    if schema_version is None:
        try:
            from hermes_state import SCHEMA_VERSION

            schema_version = str(SCHEMA_VERSION)
        except ImportError:
            schema_version = _UNKNOWN
    schema_version = (schema_version or "").strip() or _UNKNOWN
    if schema_version.casefold() in {"dev", "development", _UNKNOWN}:
        errors.append("schema version unavailable")

    release_id = "development"
    git_commit = identity.git_commit if identity.source_checkout else _UNKNOWN
    build_timestamp = _UNKNOWN
    binary_sha256 = identity.artifact_digest if identity.source_checkout else _UNKNOWN
    metadata_matches_artifact = False

    if identity.source_checkout:
        # A source checkout is intentionally never a release artifact.  Do not
        # let a copied or partial metadata file change its live-Git semantics.
        errors.extend(("release identity unavailable", "build_timestamp unavailable"))
    else:
        metadata, metadata_errors = _read_build_metadata(root)
        errors.extend(metadata_errors)
        if metadata is not None:
            if identity.artifact_digest == _UNKNOWN:
                errors.append("runtime artifact digest unavailable")
            elif metadata.runtime_sha256 != identity.artifact_digest:
                errors.append("runtime artifact digest mismatch")
            else:
                metadata_matches_artifact = True
                release_id = metadata.release_id
                git_commit = metadata.git_commit
                build_timestamp = metadata.build_timestamp
                binary_sha256 = metadata.runtime_sha256

    errors = list(dict.fromkeys(errors))
    complete = (
        not identity.source_checkout
        and metadata_matches_artifact
        and version.casefold() not in {"dev", "development", _UNKNOWN}
        and schema_version.casefold() not in {"dev", "development", _UNKNOWN}
        and _valid_commit(git_commit)
        and _valid_release_id(release_id)
        and _valid_build_timestamp(build_timestamp)
        and _valid_runtime_sha256(binary_sha256)
        and identity.source_dirty is False
        and not errors
    )
    return RuntimeProvenance(
        service_name=SERVICE_NAME,
        release_id=release_id,
        version=version,
        git_commit=git_commit,
        build_timestamp=build_timestamp,
        binary_sha256=binary_sha256,
        schema_version=schema_version,
        config_digest=_config_digest(config),
        startup_timestamp=_rfc3339(startup_timestamp),
        source_dirty=identity.source_dirty,
        provenance_complete=complete,
        provenance_errors=tuple(errors),
    )


def _build_cli(argv: Optional[list[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4 or args[0] != "stamp-build":
        print(
            "usage: build-provenance stamp-build "
            "<git_commit> <release_id> <build_timestamp>",
            file=sys.stderr,
        )
        return 2
    try:
        stamp_build_provenance(_PROJECT_ROOT, args[1], args[2], args[3])
    except BuildProvenanceError as exc:
        print(f"invalid Hermes build provenance: {exc}", file=sys.stderr)
        return 2
    except Exception:
        # The build must fail closed without surfacing local paths or input
        # values from an unexpected filesystem/runtime failure.
        print("invalid Hermes build provenance: internal failure", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(_build_cli())
