"""Non-sensitive release provenance for the Hermes Runtime health surface."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from typing import Optional


SERVICE_NAME = "hermes-runtime"
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
class _SourceIdentity:
    git_commit: str
    artifact_digest: str
    source_dirty: Optional[bool]
    errors: tuple[str, ...]


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
    if not commit:
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
            artifact_digest="unknown",
            source_dirty=source_dirty,
            errors=tuple(errors + ["runtime artifact digest unavailable"]),
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
            return _SourceIdentity(commit, "unknown", source_dirty, tuple(errors))
        digest.update(b"\0untracked\0")
        digest.update(raw_path)
        digest.update(b"\0")
        digest.update(content)
    return _SourceIdentity(
        git_commit=commit,
        artifact_digest=f"sha256:{digest.hexdigest()}",
        source_dirty=source_dirty,
        errors=tuple(errors),
    )


def _installed_source_identity() -> _SourceIdentity:
    errors: list[str] = ["source state unavailable"]
    try:
        package = distribution("hermes-agent")
    except PackageNotFoundError:
        package = None

    artifact_digest = "unknown"
    if package is not None:
        record = package.read_text("RECORD")
        if record:
            artifact_digest = (
                f"sha256:{hashlib.sha256(record.encode('utf-8')).hexdigest()}"
            )
    if artifact_digest == "unknown":
        errors.append("runtime artifact digest unavailable")

    try:
        from hermes_cli.build_info import get_build_sha

        git_commit = get_build_sha(short=40) or "unknown"
    except (ImportError, OSError, ValueError):
        git_commit = "unknown"
    if git_commit == "unknown":
        errors.append("git commit unavailable")
    return _SourceIdentity(git_commit, artifact_digest, None, tuple(errors))


def _source_identity(project_root: Path) -> _SourceIdentity:
    return _git_source_identity(project_root) or _installed_source_identity()


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
    root = project_root or Path(__file__).resolve().parent.parent
    identity = _source_identity(root)
    errors = list(identity.errors)

    if version is None:
        try:
            from gateway.api_server_shared import _hermes_version

            version = _hermes_version()
        except ImportError:
            version = "dev"
    version = (version or "").strip() or "dev"
    if version == "dev":
        errors.append("version unavailable")

    if schema_version is None:
        try:
            from hermes_state import SCHEMA_VERSION

            schema_version = str(SCHEMA_VERSION)
        except ImportError:
            schema_version = "unknown"
    schema_version = (schema_version or "").strip() or "unknown"
    if schema_version == "unknown":
        errors.append("schema version unavailable")

    release_id = "development"
    build_timestamp = "unknown"
    errors.extend(("release identity unavailable", "build_timestamp unavailable"))
    errors = list(dict.fromkeys(errors))
    complete = (
        release_id != "development"
        and version != "dev"
        and identity.git_commit != "unknown"
        and build_timestamp != "unknown"
        and identity.artifact_digest != "unknown"
        and schema_version != "unknown"
        and identity.source_dirty is False
        and not errors
    )
    return RuntimeProvenance(
        service_name=SERVICE_NAME,
        release_id=release_id,
        version=version,
        git_commit=identity.git_commit,
        build_timestamp=build_timestamp,
        binary_sha256=identity.artifact_digest,
        schema_version=schema_version,
        config_digest=_config_digest(config),
        startup_timestamp=_rfc3339(startup_timestamp),
        source_dirty=identity.source_dirty,
        provenance_complete=complete,
        provenance_errors=tuple(errors),
    )
