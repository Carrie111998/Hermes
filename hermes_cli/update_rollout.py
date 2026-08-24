"""Opt-in canary-first update rollout and verified rollback.

The regular updater remains the apply engine.  This module adds the
transaction boundary around it when ``updates.canary_profile`` is configured:

* snapshot the exact pre-update Git identity, complete venv, and generated
  dashboard bundle outside the checkout;
* restart one configured gateway profile first;
* require a fresh control-socket identity at the new SHA to remain stable for
  a bounded interval, followed by an import smoke test;
* restart the remaining gateway profiles in bounded batches; and
* on any canary/batch failure, restore Git, the venv, and the dashboard before
  restarting and verifying every already-advanced profile on the old SHA,
  canary first.

There is deliberately no second updater here.  Both the terminal command and
Telegram/Discord ``/update`` enter through ``hermes update`` and call these
same primitives.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from hermes_cli.web_dist_lock import (
    WebDistLockError,
    web_dist_lock,
)


CHECKPOINT_SCHEMA = 2
CHECKPOINT_DIR_NAME = "update-checkpoints"
_CHECKPOINT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RECOVERY_MARKER_NAMES = (".update-incomplete", ".lazy-refresh-incomplete")

# Keep the agent-facing entries aligned with update_cmd._UPDATE_CRITICAL_MODULES.
# The config/control-socket imports retain the original rollout probe coverage,
# while the remaining modules exercise the same import graph used by a real
# terminal or bot-mode agent startup.
_CANARY_SMOKE_MODULES = (
    "hermes_cli.config",
    "gateway.control_socket",
    "hermes_cli.main",
    "run_agent",
    "model_tools",
    "toolsets",
)
_CANARY_SMOKE_SENTINEL = "hermes-rollout-smoke-ok"
_CANARY_PROVIDER_SMOKE_PREFIX = "hermes-rollout-provider-smoke:"
_CANARY_PROVIDER_PROMPT = (
    "Hermes update canary health probe. Reply with a short acknowledgement only; "
    "do not call tools."
)
_PROCESS_START_TIME_EPSILON = 1e-6


class RolloutError(RuntimeError):
    """Base class for fail-closed rollout errors."""


class CheckpointError(RolloutError):
    """A pre-mutation checkpoint could not be created or validated."""


class RollbackError(RolloutError):
    """The checkpoint could not be restored exactly."""


class RolloutExecutionError(RolloutError):
    """A canary/batch failed; ``result`` describes rollback verification."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


@dataclass(frozen=True)
class GitMutationBoundary:
    """Exact Git identity plus tracked index/worktree generation."""

    sha: str
    branch: Optional[str]
    detached: bool
    tracked_sha256: str


def validate_real_venv_root(path: Path, *, allow_missing: bool = False) -> bool:
    """Require a project venv root to be a real directory, never a link.

    This check intentionally uses ``lstat`` before any ``exists``/interpreter
    lookup.  On Windows a junction or other reparse point can otherwise make
    the optional-dependency probe execute an interpreter outside the install.
    The same helper is used by checkpoint capture and live checkpoint
    validation so those paths cannot disagree about the venv topology.
    """
    path = Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return False
        raise CheckpointError(f"venv root is missing: {path}")
    except OSError as exc:
        raise CheckpointError(f"cannot inspect venv root {path}: {exc}") from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    ):
        raise CheckpointError(
            f"venv root must be a real directory, not a link or reparse point: {path}"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise CheckpointError(f"venv root is not a directory: {path}")
    return True


def validate_real_web_dist_root(
    path: Path, *, allow_missing: bool = False
) -> bool:
    """Require the generated dashboard bundle to be a real directory."""

    path = Path(path)
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        if allow_missing:
            return False
        raise CheckpointError(f"dashboard bundle is missing: {path}")
    except OSError as exc:
        raise CheckpointError(
            f"cannot inspect dashboard bundle root {path}: {exc}"
        ) from exc
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    if stat.S_ISLNK(metadata.st_mode) or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    ):
        raise CheckpointError(
            "dashboard bundle root must be a real directory, not a link or "
            f"reparse point: {path}"
        )
    if not stat.S_ISDIR(metadata.st_mode):
        raise CheckpointError(f"dashboard bundle root is not a directory: {path}")
    return True


def validate_no_reparse_topology(path: Path) -> None:
    """Reject links/reparse points in an existing state-path topology."""
    path = Path(path)
    absolute = Path(os.path.abspath(path))
    existing: list[Path] = []
    current = absolute
    while True:
        try:
            current.lstat()
        except FileNotFoundError:
            current = current.parent
            if current == current.parent:
                break
            continue
        except OSError as exc:
            raise CheckpointError(f"cannot inspect state path {current}: {exc}") from exc
        existing.append(current)
        if current == current.parent:
            break
        current = current.parent
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    for component in existing:
        metadata = component.lstat()
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0) & reparse_flag
        ):
            raise CheckpointError(
                f"state path contains a link or reparse point: {component}"
            )


@dataclass(frozen=True)
class RolloutConfig:
    """Normalized update rollout configuration.

    An empty canary profile is the feature flag.  Every other default is
    therefore inert for existing installs.
    """

    enabled: bool = False
    canary_profile: str = ""
    batch_size: int = 4
    health_timeout_seconds: float = 120.0
    healthy_after_seconds: float = 10.0
    smoke_timeout_seconds: float = 30.0
    canary_smoke_agent_turn: bool = False
    restart_timeout_seconds: float = 90.0
    checkpoint_keep: int = 3

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _bounded_number(
    value: Any,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = default
    return min(max(number, minimum), maximum)


def load_rollout_config(config: Optional[Mapping[str, Any]] = None) -> RolloutConfig:
    """Read and normalize ``updates.*`` rollout keys.

    ``config`` may be a whole Hermes config or the ``updates`` mapping.  When
    omitted the active profile's config is loaded.  A bad config read keeps
    the safe, disabled default.
    """

    if config is None:
        try:
            from hermes_cli.config import load_config

            config = load_config() or {}
        except Exception:
            config = {}
    updates: Mapping[str, Any]
    nested = config.get("updates") if isinstance(config, Mapping) else None
    if isinstance(nested, Mapping):
        updates = nested
    elif isinstance(config, Mapping):
        updates = config
    else:
        updates = {}

    raw_profile = updates.get("canary_profile", "")
    profile = str(raw_profile or "").strip().lower()
    batch_size = int(
        _bounded_number(
            updates.get("rollout_batch_size", 4), 4, minimum=1, maximum=64
        )
    )
    timeout = _bounded_number(
        updates.get("canary_health_timeout_seconds", 120),
        120,
        minimum=5,
        maximum=900,
    )
    stable = _bounded_number(
        updates.get("canary_healthy_after_seconds", 10),
        10,
        minimum=1,
        maximum=300,
    )
    # A stable window that cannot fit inside the deadline would otherwise
    # make every rollout fail by construction.  Fail closed but normalize to
    # a realizable bound so a typo does not create an unbounded wait.
    stable = min(stable, max(1.0, timeout - 1.0))
    return RolloutConfig(
        enabled=bool(profile),
        canary_profile=profile,
        batch_size=batch_size,
        health_timeout_seconds=timeout,
        healthy_after_seconds=stable,
        smoke_timeout_seconds=_bounded_number(
            updates.get("canary_smoke_timeout_seconds", 30),
            30,
            minimum=5,
            maximum=300,
        ),
        # This probe can incur provider usage, so accept only a real YAML
        # boolean.  Values such as the string "false" must not silently opt in.
        canary_smoke_agent_turn=updates.get("canary_smoke_agent_turn") is True,
        restart_timeout_seconds=_bounded_number(
            updates.get("canary_restart_timeout_seconds", 90),
            90,
            minimum=10,
            maximum=600,
        ),
        checkpoint_keep=int(
            _bounded_number(
                updates.get("rollback_checkpoint_keep", 3),
                3,
                minimum=1,
                maximum=20,
            )
        ),
    )


def _run_git(project_root: Path, *args: str, check: bool = True) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git command failed").strip()
        raise CheckpointError(detail.splitlines()[0])
    return (completed.stdout or "").strip()


def _git_identity(project_root: Path) -> tuple[str, Optional[str], bool]:
    sha = _run_git(project_root, "rev-parse", "HEAD")
    branch = _run_git(
        project_root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
    )
    return sha, branch or None, not bool(branch)


def _tracked_checkout_status(project_root: Path) -> str:
    """Return tracked/index status, raising when Git cannot prove it."""

    return _run_git(
        project_root,
        "status",
        "--porcelain=v2",
        "-z",
        "--untracked-files=no",
    )


def _tracked_state_digest(project_root: Path) -> str:
    """Hash tracked/index bytes without invoking external diff drivers."""

    digest = hashlib.sha256()
    commands = (
        ("status", "--porcelain=v2", "-z", "--untracked-files=no"),
        ("ls-files", "--stage", "-z"),
        (
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
        ),
        (
            "diff",
            "--cached",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
        ),
    )
    for args in commands:
        completed = subprocess.run(
            ["git", *args],
            cwd=project_root,
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or b"git command failed")
            raise RollbackError(
                "could not capture Git mutation boundary: "
                + detail.decode("utf-8", "replace").splitlines()[0]
            )
        digest.update("\0".join(args).encode("utf-8"))
        digest.update(b"\0")
        digest.update(completed.stdout)
        digest.update(b"\0")
    return digest.hexdigest()


def capture_git_mutation_boundary(project_root: Path) -> GitMutationBoundary:
    """Capture a stable Git boundary, refusing a concurrently changing tree."""

    project = Path(project_root)

    def capture_once() -> GitMutationBoundary:
        sha, branch, detached = _git_identity(project)
        tracked_sha256 = _tracked_state_digest(project)
        if _git_identity(project) != (sha, branch, detached):
            raise RollbackError("Git identity changed while capturing rollback boundary")
        return GitMutationBoundary(sha, branch, detached, tracked_sha256)

    first = capture_once()
    second = capture_once()
    if first != second:
        raise RollbackError("tracked Git state changed while capturing rollback boundary")
    return first


def _pin_git_recovery_ref(
    project_root: Path, sha: str, *, label: str
) -> str:
    """Create a unique durable ref without overwriting existing recovery data."""

    ref = f"refs/hermes-update-backups/{label}-{uuid.uuid4().hex}"
    zero_oid = "0" * len(sha)
    _run_git(project_root, "update-ref", ref, sha, zero_oid)
    if _run_git(project_root, "rev-parse", "--verify", f"{ref}^{{commit}}") != sha:
        raise RollbackError(f"could not verify Git recovery ref {ref}")
    return ref


def _preserve_tracked_rollback_state(project_root: Path) -> Optional[str]:
    """Stash and pin tracked/index dirt before an automatic rollback.

    The updater may legitimately reapply its transaction stash before the
    canary gate, but another process can edit the same checkout too. Preserve
    the complete tracked/index state instead of asserting ownership over it.
    Untracked files are deliberately left in place; Git's non-forced checkout
    remains responsible for refusing any overwrite.
    """

    if not _tracked_checkout_status(project_root):
        return None
    marker = f"hermes-rollback-preservation-{uuid.uuid4().hex}"
    completed = subprocess.run(
        ["git", "stash", "push", "-m", marker],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    listed = subprocess.run(
        ["git", "stash", "list", "--format=%H%x09%gs"],
        cwd=project_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if listed.returncode != 0:
        raise RollbackError("could not inspect rollback preservation stash")
    stash_sha = None
    for line in listed.stdout.splitlines():
        sha, separator, subject = line.partition("\t")
        exact = subject == marker or (
            ": " in subject and subject.rsplit(": ", 1)[1] == marker
        )
        if separator and exact and sha.strip():
            stash_sha = sha.strip()
            break
    if stash_sha is None:
        detail = (completed.stderr or completed.stdout or "git stash failed").strip()
        raise RollbackError(
            "could not preserve tracked rollback state: "
            + (detail.splitlines()[0] if detail else "unknown Git error")
        )
    pinned_ref = (
        "refs/hermes-update-stashes/rollback-preservation-"
        f"{uuid.uuid4().hex}"
    )
    zero_oid = "0" * len(stash_sha)
    _run_git(project_root, "update-ref", pinned_ref, stash_sha, zero_oid)
    if _run_git(
        project_root, "rev-parse", "--verify", f"{pinned_ref}^{{commit}}"
    ) != stash_sha:
        raise RollbackError("could not pin rollback preservation stash")
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "git stash failed").strip()
        raise RollbackError(
            "Git reported an error after creating rollback preservation stash "
            f"{pinned_ref}: "
            + (detail.splitlines()[0] if detail else "unknown Git error")
        )
    if _tracked_checkout_status(project_root):
        raise RollbackError(
            "rollback preservation stash did not leave a clean tracked checkout"
        )
    return pinned_ref


def checkpoint_root(project_root: Path, base: Optional[Path] = None) -> Path:
    """Return an external checkpoint directory, never inside ``project_root``."""

    project = Path(project_root).resolve()
    if base is None:
        from hermes_constants import get_default_hermes_root

        candidate = Path(get_default_hermes_root()) / CHECKPOINT_DIR_NAME
    else:
        candidate = Path(base)
    candidate = candidate.expanduser()
    validate_no_reparse_topology(candidate)
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(project)
    except ValueError:
        pass
    else:
        candidate = project.parent / f".{project.name}-{CHECKPOINT_DIR_NAME}"
    if candidate == project:
        candidate = project.parent / f".{project.name}-{CHECKPOINT_DIR_NAME}"
    # The inside-checkout fallback is just as security-sensitive as an
    # explicit base. Revalidate it after substitution so a pre-existing link
    # cannot redirect checkpoint writes back into the mutable checkout.
    validate_no_reparse_topology(candidate)
    candidate = candidate.resolve(strict=False)
    try:
        candidate.relative_to(project)
    except ValueError:
        pass
    else:
        raise CheckpointError(
            f"checkpoint root resolves inside the Hermes installation: {candidate}"
        )
    return candidate


def _dependency_state(venv: Path) -> dict[str, Any]:
    """Describe the copied environment without executing its interpreter."""

    validate_real_venv_root(venv)
    total_bytes = 0
    file_count = 0
    directory_count = 0
    manifest = hashlib.sha256()
    distributions: list[str] = []
    paths = list(_iter_dependency_state_paths(venv))
    for path in sorted(paths, key=lambda item: item.relative_to(venv).as_posix()):
        try:
            rel = path.relative_to(venv).as_posix() or "."
            metadata = path.lstat()
        except OSError:
            raise CheckpointError(f"cannot stat venv entry {path}")
        mode = stat.S_IMODE(metadata.st_mode)
        encoded_rel = rel.encode("utf-8", "surrogateescape")
        encoded_mode = format(mode, "04o").encode("ascii")
        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.readlink(path)
            except OSError as exc:
                raise CheckpointError(f"cannot read venv symlink {path}: {exc}") from exc
            file_count += 1
            manifest.update(b"L\0")
            manifest.update(encoded_rel)
            manifest.update(b"\0")
            manifest.update(encoded_mode)
            manifest.update(b"\0")
            manifest.update(target.encode("utf-8", "surrogateescape"))
            manifest.update(b"\n")
            continue
        if stat.S_ISDIR(metadata.st_mode):
            directory_count += 1
            manifest.update(b"D\0")
            manifest.update(encoded_rel)
            manifest.update(b"\0")
            manifest.update(encoded_mode)
            manifest.update(b"\n")
            continue
        if not stat.S_ISREG(metadata.st_mode):
            raise CheckpointError(f"unsupported venv entry type: {path}")
        size = metadata.st_size
        file_count += 1
        total_bytes += size
        manifest.update(b"F\0")
        manifest.update(encoded_rel)
        manifest.update(b"\0")
        manifest.update(encoded_mode)
        manifest.update(b"\0")
        manifest.update(str(size).encode("ascii"))
        manifest.update(b"\0")
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    manifest.update(chunk)
        except OSError as exc:
            raise CheckpointError(f"cannot hash venv file {path}: {exc}") from exc
        manifest.update(b"\n")
        if path.name == "METADATA" and path.parent.name.endswith(".dist-info"):
            distributions.append(path.parent.name)
    pyvenv = venv / "pyvenv.cfg"
    pyvenv_sha = None
    if pyvenv.is_file():
        try:
            pyvenv_sha = hashlib.sha256(pyvenv.read_bytes()).hexdigest()
        except OSError:
            pass
    return {
        "venv_present": True,
        "manifest_version": 2,
        "manifest_fields": ["type", "path", "mode", "target_or_content"],
        "file_count": file_count,
        "directory_count": directory_count,
        "entry_count": file_count + directory_count,
        "total_bytes": total_bytes,
        "manifest_sha256": manifest.hexdigest(),
        "pyvenv_cfg_sha256": pyvenv_sha,
        "distributions": sorted(distributions),
    }


def _iter_dependency_state_paths(
    venv: Path,
    *,
    label: str = "venv",
    reject_links: bool = False,
):
    """Walk a tree without following nested links or reparse points."""

    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    pending = [Path(venv)]
    yield Path(venv)
    while pending:
        current = pending.pop()
        try:
            with os.scandir(current) as entries:
                children = sorted(entries, key=lambda entry: entry.name)
                for entry in children:
                    path = Path(entry.path)
                    try:
                        metadata = entry.stat(follow_symlinks=False)
                    except OSError as exc:
                        raise CheckpointError(
                            f"cannot stat {label} entry {path}: {exc}"
                        ) from exc
                    is_reparse = bool(
                        getattr(metadata, "st_file_attributes", 0) & reparse_flag
                    )
                    if is_reparse or (
                        reject_links and stat.S_ISLNK(metadata.st_mode)
                    ):
                        raise CheckpointError(
                            f"{label} entry contains a link or reparse point: {path}"
                        )
                    yield path
                    if stat.S_ISDIR(metadata.st_mode):
                        pending.append(path)
        except OSError as exc:
            raise CheckpointError(
                f"cannot scan {label} directory {current}: {exc}"
            ) from exc


def _find_venv(project_root: Path) -> tuple[Path, str, bool]:
    for name in ("venv", ".venv"):
        candidate = project_root / name
        if validate_real_venv_root(candidate, allow_missing=True):
            return candidate, name, True
    # Absence is a dependency state too.  Restoring such a checkpoint removes
    # a venv created by the failed update.
    return project_root / "venv", "venv", False


_DEPENDENCY_STATE_FIELDS = (
    "venv_present",
    "manifest_version",
    "manifest_fields",
    "file_count",
    "directory_count",
    "entry_count",
    "total_bytes",
    "manifest_sha256",
    "pyvenv_cfg_sha256",
    "distributions",
)


def _dependency_states_match(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    return all(
        actual.get(field) == expected.get(field)
        for field in _DEPENDENCY_STATE_FIELDS
    )


_WEB_DIST_STATE_FIELDS = (
    "web_dist_present",
    "manifest_version",
    "manifest_fields",
    "file_count",
    "directory_count",
    "entry_count",
    "total_bytes",
    "manifest_sha256",
)


def _web_dist_state(web_dist: Path) -> dict[str, Any]:
    """Describe the generated dashboard bundle without trusting links."""

    validate_real_web_dist_root(web_dist)
    # Unlike a Python venv, the generated bundle has no legitimate symlink
    # entries. Refuse them before hashing/copying so rollback never traverses
    # outside the install-owned artifact tree.
    list(
        _iter_dependency_state_paths(
            web_dist,
            label="dashboard bundle",
            reject_links=True,
        )
    )
    state = _dependency_state(web_dist)
    return {
        "web_dist_present": True,
        **{field: state.get(field) for field in _WEB_DIST_STATE_FIELDS[1:]},
    }


def _web_dist_states_match(
    actual: Mapping[str, Any], expected: Mapping[str, Any]
) -> bool:
    return all(
        actual.get(field) == expected.get(field)
        for field in _WEB_DIST_STATE_FIELDS
    )


def _absent_web_dist_state() -> dict[str, Any]:
    return {
        "web_dist_present": False,
        "manifest_version": 2,
        "manifest_fields": ["type", "path", "mode", "target_or_content"],
        "file_count": 0,
        "directory_count": 0,
        "entry_count": 0,
        "total_bytes": 0,
        "manifest_sha256": None,
    }


def _validate_web_dist_state_payload(
    state: Mapping[str, Any], *, checkpoint: Path
) -> None:
    """Reject incomplete or nonsensical generated-bundle manifests."""

    if not isinstance(state.get("web_dist_present"), bool):
        raise CheckpointError(
            f"checkpoint has no valid dashboard bundle state: {checkpoint}"
        )
    if state.get("web_dist_present") is False:
        if not _web_dist_states_match(state, _absent_web_dist_state()):
            raise CheckpointError(
                f"checkpoint has an invalid absent dashboard bundle state: {checkpoint}"
            )
        return

    expected_fields = ["type", "path", "mode", "target_or_content"]
    count_fields = ("file_count", "directory_count", "entry_count", "total_bytes")
    digest = state.get("manifest_sha256")
    if (
        state.get("manifest_version") != 2
        or state.get("manifest_fields") != expected_fields
        or any(
            type(state.get(field)) is not int or state.get(field, -1) < 0
            for field in count_fields
        )
        or state.get("entry_count")
        != state.get("file_count", 0) + state.get("directory_count", 0)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
    ):
        raise CheckpointError(
            f"checkpoint has an invalid dashboard bundle manifest: {checkpoint}"
        )


def _checkpoint_id(sha: str, now: Optional[datetime] = None) -> str:
    instant = now or datetime.now(timezone.utc)
    return f"{instant.strftime('%Y%m%dT%H%M%SZ')}-{sha[:12]}-{uuid.uuid4().hex[:8]}"


def capture_rollout_relaunch_argv(plan: Any) -> None:
    """Snapshot redacted gateway argv while the planned PIDs are still live."""
    from hermes_cli.process_identity import redact_argv

    for runtime in getattr(plan, "runtimes", None) or []:
        if getattr(runtime, "kind", "") != "gateway" or not getattr(
            runtime, "pid", None
        ):
            continue
        detail = getattr(runtime, "detail", None)
        if not isinstance(detail, dict):
            detail = {}
            runtime.detail = detail
        try:
            import psutil

            process = psutil.Process(int(runtime.pid))
            if not detail.get("argv"):
                detail["argv"] = redact_argv(list(process.cmdline() or []))
            if detail.get("start_time") is None:
                detail["start_time"] = float(process.create_time())
        except Exception:
            pass


def _capture_recovery_markers(project: Path) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for name in _RECOVERY_MARKER_NAMES:
        marker = project / name
        if not os.path.lexists(marker):
            state[name] = {"present": False}
            continue
        try:
            metadata = marker.lstat()
            if not stat.S_ISREG(metadata.st_mode):
                raise CheckpointError(
                    f"recovery marker is not a regular file: {marker}"
                )
            state[name] = {
                "present": True,
                "content_hex": marker.read_bytes().hex(),
                "mode": stat.S_IMODE(metadata.st_mode),
            }
        except OSError as exc:
            raise CheckpointError(
                f"could not snapshot recovery marker {marker}: {exc}"
            ) from exc
    return state


def _restore_recovery_markers(
    project: Path, state: Optional[Mapping[str, Mapping[str, Any]]]
) -> None:
    if state is None:
        return
    for name in _RECOVERY_MARKER_NAMES:
        record = state.get(name, {"present": False})
        marker = project / name
        if not bool(record.get("present")):
            marker.unlink(missing_ok=True)
            continue
        try:
            content = bytes.fromhex(str(record.get("content_hex") or ""))
        except ValueError as exc:
            raise RollbackError(
                f"checkpoint recovery marker is invalid: {name}"
            ) from exc
        stage = marker.with_name(f".{marker.name}.{uuid.uuid4().hex}.tmp")
        try:
            stage.write_bytes(content)
            stage.chmod(int(record.get("mode") or 0o644))
            stage.replace(marker)
        finally:
            stage.unlink(missing_ok=True)


def create_checkpoint(
    project_root: Path,
    *,
    config: RolloutConfig,
    plan: Any = None,
    base: Optional[Path] = None,
    now: Optional[datetime] = None,
    prune: bool = True,
) -> Path:
    """Create an atomic checkpoint while excluding dashboard builds."""

    project = Path(project_root).resolve()
    try:
        with web_dist_lock(project, timeout_seconds=180.0):
            return _create_checkpoint_locked(
                project,
                config=config,
                plan=plan,
                base=base,
                now=now,
                prune=prune,
            )
    except WebDistLockError as exc:
        raise CheckpointError(
            f"cannot stabilize dashboard bundle for checkpoint: {exc}"
        ) from exc


def _create_checkpoint_locked(
    project_root: Path,
    *,
    config: RolloutConfig,
    plan: Any = None,
    base: Optional[Path] = None,
    now: Optional[datetime] = None,
    prune: bool = True,
) -> Path:
    """Create an atomic, external pre-mutation checkpoint.

    A failure removes the staging directory and raises ``CheckpointError``;
    callers must stop before fetch/stash/pip/restart.
    """

    project = Path(project_root).resolve()
    if not (project / ".git").exists():
        raise CheckpointError(f"checkpoint requires a Git checkout: {project}")
    sha, branch, detached = _git_identity(project)
    root = checkpoint_root(project, base)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise CheckpointError(f"cannot create checkpoint root {root}: {exc}") from exc
    checkpoint_id = _checkpoint_id(sha, now)
    stage = root / f".stage-{checkpoint_id}"
    final = root / checkpoint_id
    venv, venv_name, venv_present = _find_venv(project)
    web_dist = project / "hermes_cli" / "web_dist"
    web_dist_present = validate_real_web_dist_root(web_dist, allow_missing=True)
    try:
        stage.mkdir(mode=0o700)
        marker_state = _capture_recovery_markers(project)
        if venv_present:
            live_state_before = _dependency_state(venv)
            shutil.copytree(venv, stage / "venv", symlinks=True, copy_function=shutil.copy2)
            dependency_state = _dependency_state(stage / "venv")
            live_state_after = _dependency_state(venv)
            if not (
                _dependency_states_match(live_state_before, dependency_state)
                and _dependency_states_match(live_state_after, dependency_state)
            ):
                raise CheckpointError(
                    "live venv changed while the rollback checkpoint was copied"
                )
        else:
            dependency_state = {
                "venv_present": False,
                "manifest_version": 2,
                "manifest_fields": ["type", "path", "mode", "target_or_content"],
                "file_count": 0,
                "directory_count": 0,
                "entry_count": 0,
                "total_bytes": 0,
                "manifest_sha256": None,
                "pyvenv_cfg_sha256": None,
                "distributions": [],
            }
        if web_dist_present:
            web_dist_state_before = _web_dist_state(web_dist)
            shutil.copytree(
                web_dist,
                stage / "web_dist",
                # Preserve any link introduced during the copy so strict
                # staged-manifest validation rejects it rather than following
                # it outside the install-owned bundle tree.
                symlinks=True,
                copy_function=shutil.copy2,
            )
            web_dist_state = _web_dist_state(stage / "web_dist")
            web_dist_state_after = _web_dist_state(web_dist)
            if not (
                _web_dist_states_match(
                    web_dist_state_before, web_dist_state
                )
                and _web_dist_states_match(
                    web_dist_state_after, web_dist_state
                )
            ):
                raise CheckpointError(
                    "live dashboard bundle changed while the rollback "
                    "checkpoint was copied"
                )
        else:
            if os.path.lexists(web_dist):
                raise CheckpointError(
                    "live dashboard bundle appeared while the rollback "
                    "checkpoint was created"
                )
            web_dist_state = _absent_web_dist_state()
        runtime_profiles: list[str] = []
        checkpoint_plan = None
        if plan is not None:
            from hermes_cli.process_identity import redact_argv

            runtime_profiles = sorted(
                {
                    str(getattr(runtime, "profile", ""))
                    for runtime in (getattr(plan, "runtimes", None) or [])
                    if getattr(runtime, "kind", "") == "gateway"
                    and getattr(runtime, "profile", "")
                }
            )
            checkpoint_plan = plan.to_dict() if hasattr(plan, "to_dict") else None
            if isinstance(checkpoint_plan, dict):
                for runtime in checkpoint_plan.get("runtimes", []):
                    if not isinstance(runtime, dict):
                        continue
                    detail = runtime.get("detail")
                    if not isinstance(detail, dict):
                        detail = {}
                        runtime["detail"] = detail
                    pid = runtime.get("pid")
                    if runtime.get("kind") != "gateway" or not pid:
                        continue
                    if detail.get("argv"):
                        continue
                    try:
                        import psutil

                        detail["argv"] = redact_argv(
                            list(psutil.Process(int(pid)).cmdline() or [])
                        )
                    except Exception:
                        pass
        metadata = {
            "schema": CHECKPOINT_SCHEMA,
            "id": checkpoint_id,
            "created_at": (now or datetime.now(timezone.utc)).isoformat(),
            "status": "ready",
            "project_root": str(project),
            "pre_sha": sha,
            "pre_branch": branch,
            "detached": detached,
            "venv_name": venv_name,
            "dependency_state": dependency_state,
            "web_dist_state": web_dist_state,
            "recovery_markers": marker_state,
            "canary_profile": config.canary_profile,
            "runtime_profiles": runtime_profiles,
            "plan": checkpoint_plan,
            "rollout": config.to_dict(),
        }
        (stage / "checkpoint.json").write_text(
            json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8"
        )
        stage.replace(final)
    except BaseException as exc:
        shutil.rmtree(stage, ignore_errors=True)
        if not isinstance(exc, Exception):
            raise
        if isinstance(exc, CheckpointError):
            raise
        raise CheckpointError(f"could not create checkpoint: {exc}") from exc
    if prune:
        _prune_checkpoints(root, config.checkpoint_keep, preserve={final.name})
    return final


def prune_checkpoints_after_commit(
    checkpoint: Path, *, keep: int
) -> None:
    """Apply retention only after the candidate transaction commits."""

    path = Path(checkpoint).resolve()
    read_checkpoint(path)
    _prune_checkpoints(path.parent, int(keep), preserve={path.name})


def _prune_checkpoints(root: Path, keep: int, preserve: set[str]) -> None:
    try:
        checkpoints = sorted(
            (
                path
                for path in root.iterdir()
                if path.is_dir()
                and not path.name.startswith(".stage-")
                and (path / "checkpoint.json").is_file()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        retained = 0
        for path in checkpoints:
            if path.name in preserve or retained < max(1, keep):
                retained += 1
                continue
            shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass


def read_checkpoint(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads((Path(path) / "checkpoint.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise CheckpointError(f"invalid checkpoint {path}: {exc}") from exc
    if not isinstance(payload, dict) or payload.get("schema") != CHECKPOINT_SCHEMA:
        raise CheckpointError(f"unsupported checkpoint schema in {path}")
    if payload.get("status") != "ready" or not payload.get("pre_sha"):
        raise CheckpointError(f"checkpoint is not ready: {path}")
    web_dist_state = payload.get("web_dist_state")
    if not isinstance(web_dist_state, Mapping):
        raise CheckpointError(
            f"checkpoint has no valid dashboard bundle state: {path}"
        )
    _validate_web_dist_state_payload(web_dist_state, checkpoint=path)
    venv_name = payload.get("venv_name", "venv")
    if not isinstance(venv_name, str) or venv_name not in {"venv", ".venv"}:
        raise CheckpointError(
            f"checkpoint has unsafe venv_name {venv_name!r}: {path}"
        )
    payload["venv_name"] = venv_name
    return payload


def dependency_state_matches_checkpoint(
    checkpoint: Path, project_root: Path
) -> bool:
    """Return whether the live venv exactly matches ``checkpoint``.

    Absence is compared explicitly.  Present environments are checked using
    the same entry-type, path, mode, symlink-target, size, and content
    manifest used during checkpoint creation.
    """

    checkpoint = Path(checkpoint).resolve()
    project = Path(project_root).resolve()
    metadata = read_checkpoint(checkpoint)
    if Path(str(metadata.get("project_root", ""))).resolve() != project:
        return False
    expected = metadata.get("dependency_state") or {}
    live_venv = project / str(metadata.get("venv_name") or "venv")
    live_exists = os.path.lexists(live_venv)
    if not bool(expected.get("venv_present")):
        return not live_exists
    try:
        validate_real_venv_root(live_venv)
    except CheckpointError:
        return False
    try:
        actual = _dependency_state(live_venv)
    except CheckpointError:
        return False
    return _dependency_states_match(actual, expected)


def web_dist_state_matches_checkpoint(
    checkpoint: Path, project_root: Path
) -> bool:
    """Return whether the live dashboard bundle exactly matches a checkpoint."""

    checkpoint = Path(checkpoint).resolve()
    project = Path(project_root).resolve()
    metadata = read_checkpoint(checkpoint)
    if Path(str(metadata.get("project_root", ""))).resolve() != project:
        return False
    expected = metadata.get("web_dist_state") or {}
    live_web_dist = project / "hermes_cli" / "web_dist"
    live_exists = os.path.lexists(live_web_dist)
    if not bool(expected.get("web_dist_present")):
        return not live_exists
    try:
        actual = _web_dist_state(live_web_dist)
    except CheckpointError:
        return False
    return _web_dist_states_match(actual, expected)


def plan_from_checkpoint(metadata: Mapping[str, Any], current_plan: Any = None) -> Any:
    """Rebuild the pre-rollout worklist, overlaying any currently-live PID.

    Explicit rollback must work when the canary is down; current inventory is
    therefore only a freshness overlay, never the source of truth.
    """
    from hermes_cli.update_inventory import RuntimeRecord, UpdatePlan

    saved = metadata.get("plan")
    if not isinstance(saved, Mapping):
        raise CheckpointError("checkpoint does not contain a restart plan")
    current_by_profile: dict[str, Any] = {}
    for runtime in getattr(current_plan, "runtimes", None) or []:
        if getattr(runtime, "kind", "") != "gateway":
            continue
        profile = str(getattr(runtime, "profile", "") or "")
        if not profile:
            continue
        if profile in current_by_profile:
            raise CheckpointError(
                f"current inventory has duplicate gateway profile {profile!r}"
            )
        current_by_profile[profile] = runtime
    runtimes = []
    for item in saved.get("runtimes", []) or []:
        if not isinstance(item, Mapping) or item.get("kind") != "gateway":
            continue
        values = dict(item)
        live = current_by_profile.get(str(values.get("profile") or ""))
        if live is not None:
            values["pid"] = getattr(live, "pid", values.get("pid"))
            values["supervisor"] = getattr(
                live, "supervisor", values.get("supervisor", "manual")
            )
            values["restart_via"] = getattr(
                live, "restart_via", values.get("restart_via", "")
            )
        allowed = {
            "kind",
            "profile",
            "pid",
            "supervisor",
            "code_sha",
            "code_version",
            "restart_via",
            "detail",
        }
        runtimes.append(
            RuntimeRecord(
                **{
                    key: value
                    for key, value in values.items()
                    if key in allowed
                }
            )
        )
    return UpdatePlan(
        install_method=str(saved.get("install_method") or "git"),
        updatable_in_place=bool(saved.get("updatable_in_place", True)),
        update_mechanism=str(saved.get("update_mechanism") or "hermes update"),
        expected_sha=saved.get("expected_sha"),
        expected_version=saved.get("expected_version"),
        profiles=list(saved.get("profiles") or []),
        runtimes=runtimes,
    )


def resolve_checkpoint(
    reference: Optional[str], project_root: Path, *, base: Optional[Path] = None
) -> Path:
    """Resolve ``latest`` or a checkpoint id inside this install's root."""

    root = checkpoint_root(Path(project_root), base)
    ref = (reference or "latest").strip()
    if ref in {"", "latest"}:
        candidates = sorted(
            (
                path
                for path in root.glob("*")
                if path.is_dir() and (path / "checkpoint.json").is_file()
            ),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if not candidates:
            raise CheckpointError(f"no rollback checkpoints found in {root}")
        path = candidates[0]
    else:
        if not _CHECKPOINT_ID_RE.fullmatch(ref):
            raise CheckpointError("checkpoint must be an id from hermes update output")
        path = root / ref
    payload = read_checkpoint(path)
    if Path(str(payload.get("project_root", ""))).resolve() != Path(project_root).resolve():
        raise CheckpointError("checkpoint belongs to a different Hermes installation")
    return path


def _restore_git_identity(
    project: Path,
    sha: str,
    branch: Optional[str],
    detached: bool,
    *,
    expected_current: Optional[tuple[str, Optional[str], bool]] = None,
) -> None:
    """Move to an exact identity without force checkout or hard reset.

    The current commit is pinned first. Attached branches are detached before
    their ref is moved with compare-and-swap, so a concurrent commit either
    remains on its branch or makes the CAS fail. Detached HEAD uses a direct
    HEAD CAS. Ordinary checkout/reset operations remain non-forced and refuse
    late working-tree edits.
    """

    _run_git(project, "cat-file", "-e", f"{sha}^{{commit}}")
    current = _git_identity(project)
    if expected_current is not None and current != expected_current:
        raise RollbackError(
            "Git identity changed before rollback mutation boundary"
        )
    current_sha, current_branch, current_detached = current
    _pin_git_recovery_ref(project, current_sha, label="rollback-candidate")
    if _git_identity(project) != current:
        raise RollbackError(
            "Git identity changed while pinning rollback recovery state"
        )

    if current_detached:
        # CAS the direct HEAD ref so a concurrent detached commit cannot become
        # reflog-only between identity proof and checkout.
        _run_git(project, "update-ref", "--no-deref", "HEAD", sha, current_sha)
        try:
            _run_git(project, "reset", "--keep", sha)
        except BaseException:
            # Best-effort CAS compensation; never overwrite a newer HEAD.
            _run_git(
                project,
                "update-ref",
                "--no-deref",
                "HEAD",
                current_sha,
                sha,
                check=False,
            )
            raise
    else:
        # Detaching updates the worktree without moving the current branch;
        # any concurrent commit therefore remains branch-reachable.
        _run_git(project, "checkout", "--detach", sha)

    if not detached and branch:
        target_ref = f"refs/heads/{branch}"
        target_tip = _run_git(
            project, "rev-parse", "--verify", f"{target_ref}^{{commit}}", check=False
        )
        if current_branch == branch and not current_detached:
            # The transaction advanced this branch. Move it back only if it is
            # still at the exact candidate generation captured above.
            _run_git(project, "update-ref", target_ref, sha, current_sha)
        elif target_tip != sha:
            raise RollbackError(
                f"target branch {branch} changed before rollback attachment"
            )
        _run_git(project, "checkout", branch)

    actual = _run_git(project, "rev-parse", "HEAD")
    if actual != sha:
        raise RollbackError(f"Git verification failed: expected {sha}, found {actual}")
    actual_branch = _run_git(
        project, "symbolic-ref", "--quiet", "--short", "HEAD", check=False
    )
    if detached or not branch:
        if actual_branch:
            raise RollbackError("Git rollback expected detached HEAD")
    elif actual_branch != branch:
        raise RollbackError(
            f"Git rollback expected branch {branch}, found {actual_branch or 'detached HEAD'}"
        )


def _verify_restored_interpreter(
    venv: Path, project_root: Path, timeout_seconds: float = 30.0
) -> dict[str, Any]:
    """Prove the restored venv interpreter exists, is executable, and runs."""

    from hermes_constants import venv_python_path

    interpreter = venv_python_path(venv, windows=sys.platform == "win32")
    try:
        metadata = interpreter.stat()
    except OSError as exc:
        raise RollbackError(
            f"restored venv interpreter is missing: {interpreter}"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise RollbackError(
            f"restored venv interpreter is not a regular file: {interpreter}"
        )
    if sys.platform != "win32" and not os.access(interpreter, os.X_OK):
        raise RollbackError(
            f"restored venv interpreter is not executable: {interpreter}"
        )

    sentinel = "hermes-rollback-interpreter-ok"
    env = dict(os.environ)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    try:
        completed = subprocess.run(
            [
                str(interpreter),
                "-I",
                "-c",
                f"import json, sys; print({sentinel!r})",
            ],
            cwd=project_root,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(1.0, min(float(timeout_seconds), 300.0)),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RollbackError(
            f"restored venv interpreter could not run: {interpreter}: {exc}"
        ) from exc
    if completed.returncode != 0 or sentinel not in completed.stdout:
        detail = (
            completed.stderr
            or completed.stdout
            or f"exit code {completed.returncode}"
        ).strip()
        raise RollbackError(
            f"restored venv interpreter smoke test failed: {detail[:300]}"
        )
    return {"ok": True, "path": str(interpreter), "kind": "interpreter"}


def _validate_windows_coordinator_paths(
    project_root: Path,
    *,
    executable: Any,
    prefix: Any,
    exec_prefix: Any,
    cwd: Any,
    search_paths: list[Any],
    module_paths: list[tuple[str, Any]],
    loaded_images: list[Any],
) -> None:
    """Pure path-policy half of Windows coordinator validation."""

    project = Path(project_root).resolve(strict=False)
    roots = tuple(
        (project / name).resolve(strict=False) for name in ("venv", ".venv")
    )

    def under_live_venv(value: Any, label: str) -> Optional[Path]:
        try:
            rendered = "" if value is None else str(value).strip()
            if rendered in {"", "built-in", "frozen"}:
                return None
            candidate = Path(rendered).expanduser().resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            raise RolloutError(
                f"could not resolve Windows coordinator path {label}={value!r}: {exc}"
            ) from exc
        for root in roots:
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            return candidate
        return None

    conflicts: list[str] = []
    for label, value in (
        ("sys.executable", executable),
        ("sys.prefix", prefix),
        ("sys.exec_prefix", exec_prefix),
    ):
        matched = under_live_venv(value, label)
        if matched is not None:
            conflicts.append(f"{label}={matched}")

    matched_cwd = under_live_venv(cwd, "cwd")
    if matched_cwd is not None:
        conflicts.append(f"cwd={matched_cwd}")

    for index, entry in enumerate(search_paths):
        try:
            candidate = cwd if str(entry) == "" else entry
        except (OSError, RuntimeError, TypeError, ValueError):
            candidate = entry
        matched = under_live_venv(candidate, f"sys.path[{index}]")
        if matched is not None:
            conflicts.append(f"sys.path[{index}]={matched}")

    for module_name, module_path in module_paths:
        matched = under_live_venv(module_path, f"module {module_name}")
        if matched is not None:
            conflicts.append(f"module {module_name}={matched}")

    for module_path in loaded_images:
        matched = under_live_venv(module_path, "loaded image")
        if matched is not None:
            conflicts.append(f"loaded image={matched}")

    if conflicts:
        detail = "; ".join(dict.fromkeys(conflicts))
        raise RolloutError(
            "Windows rollout coordinator is using the live project venv; "
            f"handoff to an external interpreter before mutation ({detail})"
        )


def validate_rollout_coordinator(project_root: Path) -> None:
    """Refuse a Windows coordinator that can lock the live project venv.

    POSIX can atomically rename an in-use environment, but Windows cannot
    replace loaded executables or native extensions.  A Windows transaction
    therefore has to run from an interpreter whose executable, prefixes, and
    loaded native modules all resolve outside ``venv`` and ``.venv``.
    """

    if sys.platform != "win32":
        return
    try:
        cwd = Path.cwd().resolve(strict=False)
    except OSError as exc:
        raise RolloutError(
            f"could not resolve the Windows rollout coordinator cwd: {exc}"
        ) from exc
    module_paths: list[tuple[str, Any]] = []
    for module_name, module in list(sys.modules.items()):
        try:
            module_path = getattr(module, "__file__", None)
            if module_path is None:
                module_path = getattr(getattr(module, "__spec__", None), "origin", None)
        except Exception as exc:
            raise RolloutError(
                f"could not inspect Windows coordinator module {module_name}: {exc}"
            ) from exc
        if module_path:
            module_paths.append((module_name, module_path))

    # A DLL loaded directly with ctypes has no Python module origin. Inspect
    # the process image list as well so an otherwise external coordinator
    # cannot retain a native file handle inside the live venv during its swap.
    _validate_windows_coordinator_paths(
        project_root,
        executable=sys.executable,
        prefix=sys.prefix,
        exec_prefix=sys.exec_prefix,
        cwd=cwd,
        search_paths=list(sys.path),
        module_paths=module_paths,
        loaded_images=_windows_process_module_paths(),
    )


def _windows_process_module_paths() -> list[Path]:
    """Return every mapped image in this Windows process, fail-closed.

    Real Windows coordinators use PSAPI to catch DLLs loaded outside Python's
    import machinery (for example through ``ctypes.CDLL``).
    """

    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        get_current_process = kernel32.GetCurrentProcess
        get_current_process.argtypes = []
        get_current_process.restype = wintypes.HANDLE
        enum_modules = psapi.EnumProcessModulesEx
        enum_modules.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.HMODULE),
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.DWORD,
        ]
        enum_modules.restype = wintypes.BOOL
        module_name = psapi.GetModuleFileNameExW
        module_name.argtypes = [
            wintypes.HANDLE,
            wintypes.HMODULE,
            wintypes.LPWSTR,
            wintypes.DWORD,
        ]
        module_name.restype = wintypes.DWORD

        process = get_current_process()
        needed = wintypes.DWORD()
        list_modules_all = 0x03
        if not enum_modules(process, None, 0, ctypes.byref(needed), list_modules_all):
            raise OSError(ctypes.get_last_error(), "EnumProcessModulesEx failed")
        count = max(1, needed.value // ctypes.sizeof(wintypes.HMODULE))
        modules = (wintypes.HMODULE * count)()
        if not enum_modules(
            process,
            modules,
            ctypes.sizeof(modules),
            ctypes.byref(needed),
            list_modules_all,
        ):
            raise OSError(ctypes.get_last_error(), "EnumProcessModulesEx failed")
        if needed.value > ctypes.sizeof(modules):
            raise OSError("Windows module list changed while coordinator was validated")

        paths: list[Path] = []
        module_count = needed.value // ctypes.sizeof(wintypes.HMODULE)
        for handle in modules[:module_count]:
            buffer = ctypes.create_unicode_buffer(32768)
            length = module_name(process, handle, buffer, len(buffer))
            if length == 0:
                raise OSError(
                    ctypes.get_last_error(), "GetModuleFileNameExW failed"
                )
            paths.append(Path(buffer.value))
        return paths
    except Exception as exc:
        if isinstance(exc, RolloutError):
            raise
        raise RolloutError(
            f"could not enumerate loaded Windows coordinator modules: {exc}"
        ) from exc


def restore_checkpoint(
    checkpoint: Path,
    project_root: Path,
    *,
    require_clean: bool = True,
    transaction_owned_reset: bool = False,
    expected_git_boundary: Optional[GitMutationBoundary] = None,
    interpreter_verifier: Optional[
        Callable[[Path, Path], Mapping[str, Any]]
    ] = None,
) -> dict[str, Any]:
    """Restore a checkpoint while excluding concurrent dashboard builds."""

    project = Path(project_root).resolve()
    # Preserve the pre-I/O coordinator guard, then revalidate inside the
    # artifact lock at the actual mutation boundary.
    validate_rollout_coordinator(project)
    try:
        with web_dist_lock(project, timeout_seconds=180.0):
            return _restore_checkpoint_locked(
                checkpoint,
                project,
                require_clean=require_clean,
                transaction_owned_reset=transaction_owned_reset,
                expected_git_boundary=expected_git_boundary,
                interpreter_verifier=interpreter_verifier,
            )
    except WebDistLockError as exc:
        raise RollbackError(
            f"cannot stabilize dashboard bundle for rollback: {exc}"
        ) from exc


def _restore_checkpoint_locked(
    checkpoint: Path,
    project_root: Path,
    *,
    require_clean: bool = True,
    transaction_owned_reset: bool = False,
    expected_git_boundary: Optional[GitMutationBoundary] = None,
    interpreter_verifier: Optional[
        Callable[[Path, Path], Mapping[str, Any]]
    ] = None,
) -> dict[str, Any]:
    """Restore exact code, dependencies, and dashboard with compensation.

    The replacement venv and generated dashboard are fully staged before Git
    moves. If either artifact swap fails, both previous artifacts and the
    current Git identity are restored. On Windows an interpreter running from
    the live venv may prevent directory renames; that fails closed and leaves
    the checkpoint intact.

    ``transaction_owned_reset`` permits automatic compensation to park dirty
    tracked/index state in a durable recovery stash. It never grants authority
    to force-checkout or hard-reset that state. Interactive rollback keeps the
    clean-tree requirement by default.
    """

    project = Path(project_root).resolve()
    validate_rollout_coordinator(project)
    checkpoint = Path(checkpoint).resolve()
    metadata = read_checkpoint(checkpoint)
    if Path(str(metadata.get("project_root", ""))).resolve() != project:
        raise RollbackError("checkpoint belongs to a different Hermes installation")
    entry_git_boundary = capture_git_mutation_boundary(project)
    if (
        expected_git_boundary is not None
        and entry_git_boundary != expected_git_boundary
    ):
        raise RollbackError(
            "Git/index state changed after the rollback boundary was captured"
        )
    entry_git_identity = (
        entry_git_boundary.sha,
        entry_git_boundary.branch,
        entry_git_boundary.detached,
    )
    if (
        require_clean
        and not transaction_owned_reset
        and _tracked_checkout_status(project)
    ):
        raise RollbackError(
            "working tree has local changes; stash or commit them before rollback"
        )
    if interpreter_verifier is None:
        interpreter_verifier = _verify_restored_interpreter

    target_sha = str(metadata["pre_sha"])
    target_branch = metadata.get("pre_branch")
    target_detached = bool(metadata.get("detached"))
    current_sha, current_branch, current_detached = entry_git_identity
    venv_name = str(metadata.get("venv_name") or "venv")
    live_venv = project / venv_name
    snapshot_venv = checkpoint / "venv"
    expected_venv = bool(
        (metadata.get("dependency_state") or {}).get("venv_present")
    )
    if expected_venv and not snapshot_venv.is_dir():
        raise RollbackError("checkpoint venv snapshot is missing")
    expected_web_dist_state = metadata.get("web_dist_state") or {}
    expected_web_dist = bool(
        expected_web_dist_state.get("web_dist_present")
    )
    live_web_dist = project / "hermes_cli" / "web_dist"
    snapshot_web_dist = checkpoint / "web_dist"
    if expected_web_dist:
        try:
            snapshot_web_dist_state = _web_dist_state(snapshot_web_dist)
        except CheckpointError as exc:
            raise RollbackError(
                f"checkpoint dashboard bundle snapshot is invalid: {exc}"
            ) from exc
        if not _web_dist_states_match(
            snapshot_web_dist_state, expected_web_dist_state
        ):
            raise RollbackError(
                "checkpoint dashboard bundle snapshot does not match its manifest"
            )

    token = uuid.uuid4().hex[:10]
    stage_venv = project.parent / f".{project.name}-{venv_name}-restore-{token}"
    old_venv = project.parent / f".{project.name}-{venv_name}-previous-{token}"
    stage_web_dist = project.parent / f".{project.name}-web-dist-restore-{token}"
    old_web_dist = project.parent / f".{project.name}-web-dist-previous-{token}"
    live_move_attempted = False
    replacement_move_attempted = False
    web_live_move_attempted = False
    web_replacement_move_attempted = False
    candidate_venv_state = (
        _dependency_state(live_venv) if live_venv.is_dir() else None
    )
    if os.path.lexists(live_web_dist):
        try:
            candidate_web_dist_state = _web_dist_state(live_web_dist)
        except CheckpointError as exc:
            raise RollbackError(
                f"candidate dashboard bundle cannot be checkpointed for compensation: {exc}"
            ) from exc
    else:
        candidate_web_dist_state = _absent_web_dist_state()
    web_swap_needed = not _web_dist_states_match(
        candidate_web_dist_state, expected_web_dist_state
    )
    current_marker_state = _capture_recovery_markers(project)
    markers_restore_started = False
    git_restore_attempted = False
    rollback_preservation_ref: Optional[str] = None
    interpreter_check: Optional[dict[str, Any]] = None
    try:
        if expected_venv:
            shutil.copytree(
                snapshot_venv,
                stage_venv,
                symlinks=True,
                copy_function=shutil.copy2,
            )
            staged_state = _dependency_state(stage_venv)
            expected_state = metadata.get("dependency_state") or {}
            if not _dependency_states_match(staged_state, expected_state):
                raise RollbackError("staged venv does not match checkpoint manifest")
        if expected_web_dist and web_swap_needed:
            shutil.copytree(
                snapshot_web_dist,
                stage_web_dist,
                symlinks=True,
                copy_function=shutil.copy2,
            )
            if not _web_dist_states_match(
                _web_dist_state(stage_web_dist), expected_web_dist_state
            ):
                raise RollbackError(
                    "staged dashboard bundle does not match checkpoint manifest"
                )

        if os.path.lexists(live_web_dist):
            current_candidate_web_dist_state = _web_dist_state(live_web_dist)
        else:
            current_candidate_web_dist_state = _absent_web_dist_state()
        if not _web_dist_states_match(
            current_candidate_web_dist_state, candidate_web_dist_state
        ):
            raise RollbackError(
                "candidate dashboard bundle changed before the rollback mutation boundary"
            )

        if capture_git_mutation_boundary(project) != entry_git_boundary:
            raise RollbackError(
                "Git/index state changed while rollback artifacts were staged"
            )
        if transaction_owned_reset:
            rollback_preservation_ref = _preserve_tracked_rollback_state(
                project
            )
        elif _tracked_checkout_status(project):
            raise RollbackError(
                "working tree changed while rollback artifacts were staged"
            )
        if (
            _git_identity(project) != entry_git_identity
            or _tracked_checkout_status(project)
        ):
            raise RollbackError(
                "Git/index state changed at the rollback mutation boundary"
            )

        git_restore_attempted = True
        _restore_git_identity(
            project,
            target_sha,
            target_branch,
            target_detached,
            expected_current=entry_git_identity,
        )
        if os.path.lexists(live_venv):
            # Journal intent before the syscall. A KeyboardInterrupt can be
            # delivered after the atomic rename succeeds but before Python
            # executes the next bytecode instruction.
            live_move_attempted = True
            live_venv.replace(old_venv)
        if expected_venv:
            replacement_move_attempted = True
            stage_venv.replace(live_venv)
        if web_swap_needed and os.path.lexists(live_web_dist):
            web_live_move_attempted = True
            live_web_dist.replace(old_web_dist)
        if web_swap_needed and expected_web_dist:
            web_replacement_move_attempted = True
            stage_web_dist.replace(live_web_dist)
        if expected_venv and not live_venv.is_dir():
            raise RollbackError("restored venv is missing after swap")
        if expected_venv and not _dependency_states_match(
            _dependency_state(live_venv), metadata.get("dependency_state") or {}
        ):
            raise RollbackError("live venv failed post-swap manifest verification")
        if expected_venv:
            interpreter_check = dict(interpreter_verifier(live_venv, project))
            if not interpreter_check.get("ok", False):
                raise RollbackError(
                    "restored venv interpreter verification did not pass"
                )
        if not expected_venv and os.path.lexists(live_venv):
            raise RollbackError("rollback expected no venv, but one remains")
        if expected_web_dist:
            try:
                restored_web_dist_state = _web_dist_state(live_web_dist)
            except CheckpointError as web_exc:
                raise RollbackError(
                    f"restored dashboard bundle is invalid: {web_exc}"
                ) from web_exc
            if not _web_dist_states_match(
                restored_web_dist_state, expected_web_dist_state
            ):
                raise RollbackError(
                    "live dashboard bundle failed post-swap manifest verification"
                )
        elif os.path.lexists(live_web_dist):
            raise RollbackError(
                "rollback expected no dashboard bundle, but one remains"
            )
        markers_restore_started = metadata.get("recovery_markers") is not None
        _restore_recovery_markers(project, metadata.get("recovery_markers"))
    except BaseException as exc:
        # Compensate in reverse order.  Keep the original exception for the
        # receipt while making a best effort to return to the post-update
        # generation the operator was already running.  This deliberately
        # includes KeyboardInterrupt/SystemExit: an asynchronous interruption
        # between directory renames must not strand a split
        # Git/venv/dashboard generation.
        compensation_errors: list[str] = []
        web_dist_compensated = True
        venv_compensated = True
        web_live_moved = web_live_move_attempted and os.path.lexists(
            old_web_dist
        )
        web_replacement_moved = (
            web_replacement_move_attempted
            and os.path.lexists(live_web_dist)
            and not os.path.lexists(stage_web_dist)
        )
        try:
            if web_replacement_moved and os.path.lexists(live_web_dist):
                live_web_dist.replace(stage_web_dist)
        except BaseException as reverse_exc:
            web_dist_compensated = False
            compensation_errors.append(
                "could not demote restored dashboard bundle: "
                f"{type(reverse_exc).__name__}: {reverse_exc}"
            )
        if web_live_moved and os.path.lexists(old_web_dist):
            try:
                old_web_dist.replace(live_web_dist)
                web_dist_compensated = True
            except BaseException as reverse_exc:
                try:
                    if os.path.lexists(live_web_dist):
                        raise RollbackError(
                            "live dashboard bundle path remained occupied during compensation"
                        )
                    if not bool(
                        candidate_web_dist_state.get("web_dist_present")
                    ) or not old_web_dist.is_dir():
                        raise RollbackError(
                            "candidate dashboard bundle cannot be verified for reconstruction"
                        )
                    shutil.copytree(
                        old_web_dist,
                        live_web_dist,
                        symlinks=True,
                        copy_function=shutil.copy2,
                    )
                    if not _web_dist_states_match(
                        _web_dist_state(live_web_dist),
                        candidate_web_dist_state,
                    ):
                        raise RollbackError(
                            "reconstructed candidate dashboard bundle failed manifest verification"
                        )
                    web_dist_compensated = True
                except BaseException as reconstruction_exc:
                    web_dist_compensated = False
                    compensation_errors.append(
                        "could not restore candidate dashboard bundle: "
                        f"{type(reverse_exc).__name__}: {reverse_exc}; "
                        "reconstruction failed: "
                        f"{type(reconstruction_exc).__name__}: "
                        f"{reconstruction_exc}"
                    )
        elif not bool(candidate_web_dist_state.get("web_dist_present")):
            # The candidate generation had no bundle. A successfully demoted
            # checkpoint bundle therefore restores that exact absence.
            web_dist_compensated = not os.path.lexists(live_web_dist)
            if not web_dist_compensated:
                compensation_errors.append(
                    "could not restore candidate dashboard bundle absence"
                )
        # Reconcile each journaled rename from the filesystem topology. These
        # predicates cover both an ordinary later failure and an asynchronous
        # exception raised immediately after the OS completed Path.replace.
        live_moved = live_move_attempted and os.path.lexists(old_venv)
        replacement_moved = (
            replacement_move_attempted
            and os.path.lexists(live_venv)
            and not os.path.lexists(stage_venv)
        )
        try:
            if replacement_moved and os.path.lexists(live_venv):
                live_venv.replace(stage_venv)
        except BaseException as reverse_exc:
            venv_compensated = False
            compensation_errors.append(
                "could not demote restored venv: "
                f"{type(reverse_exc).__name__}: {reverse_exc}"
            )
        if live_moved and os.path.lexists(old_venv):
            try:
                old_venv.replace(live_venv)
                venv_compensated = True
            except BaseException as reverse_exc:
                # A transient Windows rename failure must not turn cleanup
                # into deletion of the only candidate dependency tree. When
                # the live path is free, reconstruct it from the preserved
                # candidate and verify the exact manifest before considering
                # compensation complete. On any failure both source trees are
                # retained for manual recovery.
                try:
                    if os.path.lexists(live_venv):
                        raise RollbackError(
                            "live venv path remained occupied during compensation"
                        )
                    if candidate_venv_state is None or not old_venv.is_dir():
                        raise RollbackError(
                            "candidate venv cannot be verified for reconstruction"
                        )
                    shutil.copytree(
                        old_venv,
                        live_venv,
                        symlinks=True,
                        copy_function=shutil.copy2,
                    )
                    if not _dependency_states_match(
                        _dependency_state(live_venv), candidate_venv_state
                    ):
                        raise RollbackError(
                            "reconstructed candidate venv failed manifest verification"
                        )
                    venv_compensated = True
                except BaseException as reconstruction_exc:
                    venv_compensated = False
                    compensation_errors.append(
                        "could not restore candidate venv: "
                        f"{type(reverse_exc).__name__}: {reverse_exc}; "
                        "reconstruction failed: "
                        f"{type(reconstruction_exc).__name__}: "
                        f"{reconstruction_exc}"
                    )
        if git_restore_attempted:
            try:
                expected_restored_identity = (
                    target_sha,
                    None if target_detached else target_branch,
                    target_detached,
                )
                _restore_git_identity(
                    project,
                    current_sha,
                    current_branch,
                    current_detached,
                    expected_current=expected_restored_identity,
                )
            except BaseException as git_exc:
                compensation_errors.append(
                    "could not restore candidate Git identity: "
                    f"{type(git_exc).__name__}: {git_exc}"
                )
        if markers_restore_started:
            try:
                _restore_recovery_markers(project, current_marker_state)
            except BaseException as marker_exc:
                compensation_errors.append(
                    "could not restore candidate recovery markers: "
                    f"{type(marker_exc).__name__}: {marker_exc}"
                )
        if web_dist_compensated:
            shutil.rmtree(stage_web_dist, ignore_errors=True)
        elif os.path.lexists(live_web_dist):
            try:
                if _web_dist_states_match(
                    _web_dist_state(live_web_dist), candidate_web_dist_state
                ):
                    web_dist_compensated = True
                    shutil.rmtree(stage_web_dist, ignore_errors=True)
            except Exception:
                pass
        if venv_compensated:
            shutil.rmtree(stage_venv, ignore_errors=True)
        elif os.path.lexists(live_venv) and candidate_venv_state is not None:
            try:
                if _dependency_states_match(
                    _dependency_state(live_venv), candidate_venv_state
                ):
                    venv_compensated = True
                    shutil.rmtree(stage_venv, ignore_errors=True)
            except Exception:
                pass
        if not isinstance(exc, Exception):
            if compensation_errors and hasattr(exc, "add_note"):
                exc.add_note("; ".join(compensation_errors))
            raise
        if compensation_errors:
            raise RollbackError(
                "rollback failed before commit and compensation was incomplete: "
                + "; ".join(compensation_errors)
            ) from exc
        if isinstance(exc, RollbackError):
            raise
        raise RollbackError(f"rollback failed before commit: {exc}") from exc

    shutil.rmtree(old_venv, ignore_errors=True)
    shutil.rmtree(stage_venv, ignore_errors=True)
    shutil.rmtree(old_web_dist, ignore_errors=True)
    shutil.rmtree(stage_web_dist, ignore_errors=True)
    return {
        "checkpoint": metadata["id"],
        "restored": True,
        "verified": _run_git(project, "rev-parse", "HEAD") == target_sha,
        "sha": target_sha,
        "branch": target_branch,
        "dependency_state": metadata.get("dependency_state"),
        "web_dist_state": metadata.get("web_dist_state"),
        "interpreter": interpreter_check,
        "transaction_owned_reset": transaction_owned_reset,
        "rollback_preservation_ref": rollback_preservation_ref,
    }


def validate_rollout_plan(plan: Any, config: RolloutConfig) -> dict[str, Any]:
    """Return one gateway runtime per profile or fail before mutation."""

    if not config.enabled:
        return {}
    if not bool(getattr(plan, "updatable_in_place", False)) or str(
        getattr(plan, "install_method", "unknown")
    ) != "git":
        raise RolloutError(
            "canary rollout requires an in-place Git install; ZIP/image/package "
            "updates cannot provide this transaction boundary"
        )
    from hermes_cli.profiles import normalize_profile_name, validate_profile_name

    try:
        canary_profile = normalize_profile_name(config.canary_profile)
        validate_profile_name(canary_profile)
    except ValueError as exc:
        raise RolloutError(f"invalid canary profile: {exc}") from exc
    if canary_profile != config.canary_profile:
        raise RolloutError(
            f"canary profile must use canonical id {canary_profile!r}"
        )
    known_profiles: set[str] = set()
    for raw_known_profile in getattr(plan, "profiles", None) or []:
        raw_known_profile = str(raw_known_profile or "")
        try:
            known_profile = normalize_profile_name(raw_known_profile)
            validate_profile_name(known_profile)
        except ValueError as exc:
            raise RolloutError(
                f"invalid known profile {raw_known_profile!r}: {exc}"
            ) from exc
        if raw_known_profile != known_profile:
            raise RolloutError(
                f"known profile must use canonical id {known_profile!r}, "
                f"got {raw_known_profile!r}"
            )
        if known_profile in known_profiles:
            raise RolloutError(f"duplicate known profile {known_profile!r}")
        known_profiles.add(known_profile)

    runtimes: dict[str, Any] = {}
    for runtime in getattr(plan, "runtimes", None) or []:
        if getattr(runtime, "kind", "") != "gateway":
            continue
        raw_profile = str(getattr(runtime, "profile", "") or "")
        try:
            profile = normalize_profile_name(raw_profile)
            validate_profile_name(profile)
        except ValueError as exc:
            raise RolloutError(f"invalid gateway profile {raw_profile!r}: {exc}") from exc
        if raw_profile != profile:
            raise RolloutError(
                f"gateway profile must use canonical id {profile!r}, got {raw_profile!r}"
            )
        if profile in runtimes:
            raise RolloutError(
                f"duplicate gateway runtime for profile {profile!r}; refusing ambiguous restart"
            )
        _validated_supervisor(profile, runtime)
        runtimes[profile] = runtime
    if config.canary_profile not in runtimes:
        raise RolloutError(
            f"configured canary profile '{config.canary_profile}' is not a running gateway"
        )
    return runtimes


def _validated_supervisor(profile: str, runtime: Any) -> str:
    """Return a supported supervisor id, rejecting every unknown shape."""

    supervisor = str(getattr(runtime, "supervisor", "") or "").strip().lower()
    restart_via = str(getattr(runtime, "restart_via", "") or "").strip().lower()
    expected_restart = {
        "manual": "manual",
        "systemd": "systemd",
        "launchd": "launchd",
        "service": "manual",
    }
    supported = {"manual"}
    if sys.platform.startswith("linux"):
        supported.add("systemd")
    elif sys.platform == "darwin":
        supported.add("launchd")
    elif sys.platform == "win32":
        supported.add("service")
    if supervisor not in supported:
        raise RolloutError(
            f"{profile}: unsupported or unverifiable gateway supervisor "
            f"{supervisor or '<missing>'!r} on {sys.platform}"
        )
    expected = expected_restart[supervisor]
    if restart_via != expected:
        raise RolloutError(
            f"{profile}: supervisor {supervisor!r} requires restart_via "
            f"{expected!r}, got {restart_via or '<missing>'!r}"
        )
    return supervisor


def _rollout_order(
    runtimes: Mapping[str, Any],
    canary_profile: str,
    profiles: Optional[list[str]] = None,
) -> list[str]:
    if profiles is None:
        selected = set(runtimes)
    else:
        selected = set()
        for profile in profiles:
            if profile not in runtimes:
                raise RolloutError(f"profile {profile!r} is not in the rollout plan")
            if profile in selected:
                raise RolloutError(f"profile {profile!r} was selected more than once")
            selected.add(profile)
    return ([canary_profile] if canary_profile in selected else []) + sorted(
        selected - {canary_profile}
    )


def _bounded_smoke_run(
    command: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a smoke child with bounded whole-tree timeout cleanup."""

    from hermes_cli._subprocess_compat import (
        IS_WINDOWS,
        kill_process_tree,
        windows_hide_flags,
    )

    spawn_options: dict[str, Any]
    if IS_WINDOWS:
        spawn_options = {"creationflags": windows_hide_flags()}
    else:
        # The ownership check inside kill_process_tree only signals the group
        # when this child is its leader, so no unrelated updater process can be
        # reached by timeout cleanup.
        spawn_options = {"process_group": 0}
    process = subprocess.Popen(
        list(command),
        cwd=cwd,
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **spawn_options,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        kill_process_tree(process)
        try:
            process.communicate(timeout=1)
        except Exception:
            pass
        raise
    return subprocess.CompletedProcess(
        list(command), process.returncode, stdout, stderr
    )


def _provider_smoke_turn(profile: str) -> dict[str, Any]:
    """Run one persistence-isolated turn through the configured provider.

    This function executes inside the candidate venv subprocess created by
    :func:`_profile_smoke`.  The subprocess environment already pins
    ``HERMES_HOME`` to ``profile``.  Reusing the normal runtime-provider and
    :class:`AIAgent` paths exercises the profile's real credentials, transport,
    and model without inventing a second provider protocol.
    """

    from gateway.session_context import declare_stateless_channel
    from hermes_cli.config import load_config, split_model_config_default
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from run_agent import AIAgent

    config = load_config() or {}
    model_config = config.get("model") or {}
    configured_provider: Optional[str] = None
    if isinstance(model_config, Mapping):
        raw_default = model_config.get("default") or model_config.get("model") or ""
        if isinstance(raw_default, Mapping):
            model, provider_from_default = split_model_config_default(raw_default)
        else:
            model = str(raw_default or "").strip()
            provider_from_default = ""
        configured_provider = (
            str(provider_from_default or model_config.get("provider") or "").strip()
            or None
        )
    else:
        model = str(model_config or "").strip()

    runtime = resolve_runtime_provider(
        requested=configured_provider,
        target_model=model or None,
    )
    declare_stateless_channel()
    agent = None
    try:
        agent = AIAgent(
            api_key=runtime.get("api_key"),
            base_url=runtime.get("base_url"),
            provider=runtime.get("provider"),
            requested_provider=runtime.get("requested_provider"),
            api_mode=runtime.get("api_mode"),
            acp_command=runtime.get("command"),
            acp_args=runtime.get("args"),
            credential_pool=runtime.get("credential_pool"),
            model=model,
            max_iterations=1,
            max_tokens=64,
            enabled_toolsets=[],
            quiet_mode=True,
            platform="cli",
            skip_context_files=True,
            skip_memory=True,
            skip_background_review=True,
            session_db=None,
            fallback_model=get_fallback_chain(config) or None,
        )
        agent.suppress_status_output = True
        agent.stream_delta_callback = None
        agent.tool_gen_callback = None
        turn = agent.run_conversation(_CANARY_PROVIDER_PROMPT)
    finally:
        if agent is not None:
            try:
                agent.close()
            except Exception:
                pass

    if not isinstance(turn, Mapping):
        raise RolloutError(f"{profile} provider smoke returned an invalid result")
    response = str(turn.get("final_response") or "").strip()
    if (
        turn.get("completed") is False
        or turn.get("failed")
        or turn.get("partial")
        or not response
    ):
        reason = str(turn.get("exit_reason") or "no final response")
        raise RolloutError(f"{profile} provider smoke did not complete: {reason}")
    try:
        api_calls = int(turn.get("api_calls") or 0)
    except (TypeError, ValueError):
        api_calls = 0
    if api_calls < 1:
        raise RolloutError(f"{profile} provider smoke made no provider call")
    return {
        "ok": True,
        "kind": "agent-turn",
        "mode": "provider-turn",
        "profile": profile,
        "provider": str(runtime.get("provider") or "unknown"),
        "model": model,
        "api_calls": api_calls,
        "completed": bool(turn.get("completed", True)),
        "response_received": True,
    }


def _profile_smoke(
    project_root: Path,
    profile: str,
    timeout: float,
    *,
    agent_turn: bool = False,
) -> dict[str, Any]:
    """Run a bounded agent bootstrap in the target environment.

    The child is isolated from the coordinator's ``PYTHONPATH`` so an external
    handoff process cannot accidentally satisfy imports missing from the
    candidate venv.  Importing the real startup module set catches cross-module
    skew, and reading the tool registries exercises a representative agent
    bootstrap.  The default is non-billable.  When ``agent_turn`` is true, the
    same bounded child then runs one persistence-isolated provider turn.
    """

    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import venv_python_path

    venv, _name, present = _find_venv(Path(project_root))
    if not present:
        # An enabled rollout always prepares a transaction-owned project venv
        # before reaching the canary. Falling back to the coordinator's own
        # interpreter here would let a deleted/broken candidate dependency
        # tree pass the smoke gate, especially on the external Windows
        # coordinator path.
        raise RolloutError(
            f"{profile} smoke test cannot find the candidate project venv"
        )
    interpreter = venv_python_path(venv, windows=sys.platform == "win32")
    probe = (
        "import importlib\n"
        f"for _name in {_CANARY_SMOKE_MODULES!r}:\n"
        "    try:\n"
        "        importlib.import_module(_name)\n"
        "    except BaseException as _exc:\n"
        "        raise RuntimeError(\n"
        "            f'critical module {_name!r} failed to import'\n"
        "        ) from _exc\n"
        "from hermes_cli.config import load_config\n"
        "from model_tools import get_all_tool_names\n"
        "from toolsets import get_all_toolsets\n"
        "load_config()\n"
        "if not isinstance(get_all_tool_names(), list):\n"
        "    raise RuntimeError('agent tool registry bootstrap returned an invalid shape')\n"
        "if not isinstance(get_all_toolsets(), dict):\n"
        "    raise RuntimeError('agent toolset bootstrap returned an invalid shape')\n"
    )
    if agent_turn:
        probe += (
            "import json\n"
            "from hermes_cli.update_rollout import _provider_smoke_turn\n"
            f"_provider_result = _provider_smoke_turn({profile!r})\n"
            f"print({_CANARY_PROVIDER_SMOKE_PREFIX!r} + "
            "json.dumps(_provider_result, sort_keys=True))\n"
        )
    probe += f"print({_CANARY_SMOKE_SENTINEL!r})\n"
    env = dict(os.environ)
    env["HERMES_HOME"] = str(get_profile_dir(profile))
    # ``-I`` already ignores Python-specific environment injection; remove the
    # two path variables as defense in depth and to keep subprocess receipts
    # deterministic across shells and bot supervisors.
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    try:
        result = _bounded_smoke_run(
            [str(interpreter), "-I", "-c", probe],
            cwd=project_root,
            env=env,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RolloutError(
            f"{profile} smoke process could not run: {type(exc).__name__}: {exc}"
        ) from exc
    if (
        result.returncode != 0
        or _CANARY_SMOKE_SENTINEL not in result.stdout.splitlines()
    ):
        detail = (result.stderr or result.stdout or "smoke process failed").strip()
        raise RolloutError(f"{profile} smoke test failed: {detail[-1000:]}")
    smoke_result: dict[str, Any] = {
        "ok": True,
        "kind": "agent-bootstrap",
        "mode": "provider-turn" if agent_turn else "structural",
        "profile": profile,
        "modules": list(_CANARY_SMOKE_MODULES),
    }
    if agent_turn:
        provider_line = next(
            (
                line[len(_CANARY_PROVIDER_SMOKE_PREFIX) :]
                for line in result.stdout.splitlines()
                if line.startswith(_CANARY_PROVIDER_SMOKE_PREFIX)
            ),
            "",
        )
        try:
            provider_result = json.loads(provider_line)
        except (TypeError, ValueError) as exc:
            raise RolloutError(
                f"{profile} provider smoke did not return a valid receipt"
            ) from exc
        if not isinstance(provider_result, dict) or provider_result.get("ok") is not True:
            raise RolloutError(f"{profile} provider smoke did not pass")
        smoke_result["agent_turn"] = provider_result
    return smoke_result


def stable_gateway_health(
    profile: str,
    expected_sha: str,
    *,
    previous_pid: Optional[int],
    stable_seconds: float,
    timeout_seconds: float,
    project_root: Path,
    smoke_timeout_seconds: float,
    probe: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
    status_probe: Optional[
        Callable[[str], Optional[Mapping[str, Any]]]
    ] = None,
    smoke: Optional[Callable[[str], Mapping[str, Any]]] = None,
    smoke_agent_turn: bool = False,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_seconds: float = 0.5,
) -> dict[str, Any]:
    """Require one fresh, running gateway identity continuously."""

    if probe is None or status_probe is None:
        from gateway.control_socket import identify_gateway, query_gateway_control
        from hermes_cli.profiles import get_profile_dir

    if probe is None:
        probe = lambda name: identify_gateway(get_profile_dir(name), timeout=1.0)
    if status_probe is None:
        status_probe = lambda name: query_gateway_control(
            get_profile_dir(name), "status", timeout=1.0
        )
    if smoke is None:
        smoke = lambda name: _profile_smoke(
            Path(project_root),
            name,
            smoke_timeout_seconds,
            agent_turn=smoke_agent_turn,
        )

    def _pid(payload: Optional[Mapping[str, Any]], field: str) -> Optional[int]:
        raw = payload.get(field) if payload else None
        try:
            value = int(raw) if raw is not None else None
        except (TypeError, ValueError):
            return None
        return value if value and value > 0 else None

    def _running_sample() -> tuple[
        Optional[tuple[int, Any, str]],
        Optional[Mapping[str, Any]],
        str,
    ]:
        identity = probe(profile)
        pid = _pid(identity, "pid")
        sha = str(identity.get("code_sha") or "") if identity else ""
        start_time = identity.get("start_time") if identity else None
        if not identity or pid is None:
            return None, None, "gateway did not answer identify with a valid PID"
        if start_time is None:
            return None, None, "gateway identify omitted process start time"
        if pid == previous_pid:
            return None, None, f"old gateway PID {pid} is still serving"
        if sha != expected_sha:
            return (
                None,
                None,
                f"gateway reported SHA {sha or 'unknown'}, expected {expected_sha}",
            )

        status = status_probe(profile)
        answering_pid = _pid(status, "answering_pid")
        status_pid = _pid(status, "pid")
        status_start_time = status.get("start_time") if status else None
        status_sha = str(status.get("code_sha") or "") if status else ""
        if not status:
            return None, None, "gateway did not answer its live status verb"
        if answering_pid != pid or status_pid != pid:
            return (
                None,
                status,
                "gateway status was answered by a different process identity",
            )
        if status_start_time != start_time or status_sha != sha:
            return None, status, "gateway status identity did not match identify"
        gateway_state = str(status.get("gateway_state") or "").strip().lower()
        if gateway_state != "running":
            return (
                None,
                status,
                f"gateway state is {gateway_state or 'unknown'}, expected running",
            )
        return (pid, start_time, sha), status, ""

    deadline = monotonic() + timeout_seconds
    stable_key: Optional[tuple[int, Any, str]] = None
    stable_since: Optional[float] = None
    last_reason = "gateway did not answer its control socket"
    while monotonic() < deadline:
        key, status, sample_reason = _running_sample()
        now = monotonic()
        if key is not None:
            if key != stable_key:
                stable_key = key
                stable_since = now
            if stable_since is not None and now - stable_since >= stable_seconds:
                smoke_result = dict(smoke(profile))
                if not smoke_result.get("ok", False):
                    raise RolloutError(f"{profile} smoke test did not pass")
                # The isolated bootstrap can run for up to the configured
                # smoke timeout.  Prove the exact gateway that earned the
                # stable window is still serving afterwards; otherwise a
                # process that died or restarted during the child probe would
                # be reported healthy and the rollout could advance.
                post_smoke_key, post_smoke_status, post_smoke_reason = (
                    _running_sample()
                )
                if post_smoke_key != stable_key:
                    raise RolloutError(
                        f"{profile} gateway readiness changed during smoke test: "
                        f"{post_smoke_reason or 'process identity changed'}"
                    )
                return {
                    "ok": True,
                    "profile": profile,
                    "pid": key[0],
                    "start_time": key[1],
                    "sha": key[2],
                    "gateway_state": "running",
                    "status_answering_pid": _pid(
                        post_smoke_status or status, "answering_pid"
                    ),
                    "stable_seconds": now - stable_since,
                    "smoke": smoke_result,
                }
            last_reason = "new gateway identity has not remained stable long enough"
        else:
            stable_key = None
            stable_since = None
            last_reason = sample_reason
        sleep(min(poll_seconds, max(0.0, deadline - monotonic())))
    raise RolloutError(
        f"{profile} health gate timed out after {timeout_seconds:g}s: {last_reason}"
    )


def _service_label(profile: str) -> str:
    return "hermes-gateway.service" if profile == "default" else f"hermes-gateway-{profile}.service"


def _saved_runtime_pid_is_same_gateway(runtime: Any, pid: int) -> bool:
    """Prove a saved PID still denotes the exact gateway process captured.

    PID existence alone is unsafe after a long update because operating
    systems reuse identifiers. A saved manual PID is signalable only when its
    process start time still matches and its command line remains gateway-like.
    """

    detail = getattr(runtime, "detail", {}) or {}
    if not isinstance(detail, Mapping):
        return False
    try:
        expected_start = float(detail["start_time"])
        import psutil

        process = psutil.Process(int(pid))
        if (
            abs(float(process.create_time()) - expected_start)
            > _PROCESS_START_TIME_EPSILON
        ):
            return False
        from gateway.status import _looks_like_gateway_process

        return bool(_looks_like_gateway_process(int(pid)))
    except (KeyError, TypeError, ValueError, OSError):
        return False
    except Exception:
        return False


def restart_profile_gateway(
    profile: str, runtime: Any, *, config: RolloutConfig
) -> dict[str, Any]:
    """Restart exactly one planned profile via its declared supervisor."""

    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    runtime_profile = str(getattr(runtime, "profile", "") or "")
    if runtime_profile != profile:
        raise RolloutError(
            f"restart route mismatch: requested {profile!r}, runtime is {runtime_profile!r}"
        )
    supervisor = _validated_supervisor(profile, runtime)
    old_pid = getattr(runtime, "pid", None)
    try:
        old_pid = int(old_pid) if old_pid is not None else None
    except (TypeError, ValueError):
        old_pid = None
    # On rollback this function is called a second time. The PID captured in
    # the pre-update plan is then necessarily stale, so prefer the gateway's
    # live, self-declared control-socket identity on every invocation.
    live_identity_verified = False
    try:
        from gateway.control_socket import identify_gateway

        identity = identify_gateway(get_profile_dir(profile), timeout=1.0)
        if identity and identity.get("pid") is not None:
            old_pid = int(identity["pid"])
            live_identity_verified = True
    except (TypeError, ValueError, OSError):
        live_identity_verified = False
    token = set_hermes_home_override(str(get_profile_dir(profile)))
    try:
        import importlib

        gateway_cli = importlib.import_module("hermes_cli.gateway")

        if supervisor == "systemd":
            scopes = gateway_cli.get_installed_systemd_scopes()
            if len(scopes) != 1:
                raise RolloutError(
                    f"{profile}: expected one installed systemd scope, found {scopes or 'none'}"
                )
            gateway_cli.systemd_restart(system=scopes[0] == "system")
            return {
                "profile": profile,
                "old_pid": old_pid,
                "restarted_services": [_service_label(profile)],
                "relaunched_profiles": [],
                "externally_supervised_profiles": [],
                "killed_pids": [old_pid] if old_pid else [],
            }
        if supervisor == "launchd":
            gateway_cli.launchd_restart()
            return {
                "profile": profile,
                "old_pid": old_pid,
                "restarted_services": [gateway_cli.get_launchd_label()],
                "relaunched_profiles": [],
                "externally_supervised_profiles": [],
                "killed_pids": [old_pid] if old_pid else [],
            }
        if supervisor == "service":
            if sys.platform != "win32":
                raise RolloutError(f"{profile}: Windows service supervisor on {sys.platform}")
            from hermes_cli import gateway_windows

            gateway_windows.restart()
            return {
                "profile": profile,
                "old_pid": old_pid,
                "restarted_services": [],
                "relaunched_profiles": [profile],
                "externally_supervised_profiles": [],
                "killed_pids": [old_pid] if old_pid else [],
            }
        if supervisor == "desktop":
            raise RolloutError(f"{profile}: Desktop owns this gateway restart")
        if old_pid is not None and not live_identity_verified:
            if not _saved_runtime_pid_is_same_gateway(runtime, old_pid):
                # The captured PID is dead, reused, or unverifiable. Never arm
                # a watcher or signal it; the validated saved argv is the only
                # safe recovery route.
                old_pid = None
        if old_pid is None:
            old_pid_alive = False
        else:
            try:
                from gateway.status import _pid_exists

                old_pid_alive = bool(_pid_exists(old_pid))
            except Exception:
                old_pid_alive = True
        if not old_pid_alive:
            detail = getattr(runtime, "detail", {}) or {}
            argv = detail.get("argv") if isinstance(detail, Mapping) else None
            if not isinstance(argv, list) or not argv:
                raise RolloutError(
                    f"{profile}: gateway is down and checkpoint has no relaunch argv"
                )
            env = dict(os.environ)
            env.pop("_HERMES_GATEWAY", None)
            env["HERMES_HOME"] = str(get_profile_dir(profile))
            popen_kwargs: dict[str, Any] = {
                "cwd": str(Path(__file__).resolve().parents[1]),
                "env": env,
                "stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            }
            if sys.platform == "win32":
                from hermes_cli._subprocess_compat import (
                    windows_detach_popen_kwargs,
                )

                popen_kwargs.update(windows_detach_popen_kwargs())
            else:
                popen_kwargs["start_new_session"] = True
            subprocess.Popen(list(argv), **popen_kwargs)
            return {
                "profile": profile,
                "old_pid": old_pid,
                "restarted_services": [],
                "relaunched_profiles": [profile],
                "externally_supervised_profiles": [],
                "killed_pids": [],
            }
        if old_pid is None:
            raise RolloutError(f"{profile}: live gateway identity lost its PID")
        restart_mode = gateway_cli._prepare_profile_gateway_update_restart(profile, old_pid)
        if restart_mode is None:
            raise RolloutError(f"{profile}: could not arm a replacement gateway")
        drained = gateway_cli._graceful_restart_via_sigusr1(
            old_pid, drain_timeout=config.restart_timeout_seconds
        )
        if not drained:
            try:
                os.kill(old_pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        return {
            "profile": profile,
            "old_pid": old_pid,
            "restarted_services": [],
            "relaunched_profiles": [] if restart_mode == "external-supervisor" else [profile],
            "externally_supervised_profiles": [profile]
            if restart_mode == "external-supervisor"
            else [],
            "killed_pids": [old_pid],
        }
    finally:
        reset_hermes_home_override(token)


def _quiesce_until_stable(
    *,
    initial_pid: Optional[int],
    initial_start_time: Optional[float] = None,
    timeout_seconds: float,
    terminate: bool,
) -> tuple[bool, list[int]]:
    """Require a profile gateway to remain absent across a respawn window."""

    from gateway.status import (
        _looks_like_gateway_process,
        _pid_exists,
        get_running_pid,
        terminate_pid,
        write_planned_stop_marker,
    )

    # ``restart_timeout_seconds`` is already config-bounded to 10..600s.
    # Preserve that drain budget for a manual gateway's own SIGTERM handler:
    # it is responsible for finishing active chat/API/cron/Kanban work.  The
    # old fixed two-second escalation amputated otherwise healthy turns.  Only
    # reserve the final stable-absence window (plus one poll) for a force-stop
    # and proof that a detached watcher did not immediately respawn it.
    poll_seconds = 0.1
    stable_absence_seconds = 1.0
    started_at = time.monotonic()
    timeout = max(2.0, min(float(timeout_seconds), 600.0))
    deadline = started_at + timeout
    force_at = max(
        started_at,
        deadline - stable_absence_seconds - poll_seconds,
    )
    absent_since: Optional[float] = None
    signalled: dict[int, float] = {}
    force_signalled: set[int] = set()
    stopped: set[int] = set()
    initial_candidate_live = initial_pid is not None

    def _same_initial_process(pid: int) -> bool:
        if initial_pid is None or pid != initial_pid:
            return False
        if initial_start_time is None:
            # A bare PID is never enough to reacquire a process after the PID
            # file disappeared.  The caller snapshots create_time whenever it
            # has a controllable live gateway.
            return False
        try:
            import psutil

            return (
                abs(
                    float(psutil.Process(pid).create_time())
                    - float(initial_start_time)
                )
                <= _PROCESS_START_TIME_EPSILON
            )
        except Exception:
            return False

    while time.monotonic() < deadline:
        try:
            current_pid = get_running_pid(cleanup_stale=False)
        except Exception:
            current_pid = None
        if current_pid is None and initial_candidate_live and initial_pid is not None:
            try:
                still_exists = _pid_exists(initial_pid)
            except Exception:
                still_exists = False
            if still_exists and _same_initial_process(initial_pid):
                current_pid = initial_pid
            else:
                # Once the original instance is proven absent or its creation
                # time changes, never reacquire that numeric PID during the
                # one-second respawn window.
                initial_candidate_live = False
        if current_pid is None:
            now = time.monotonic()
            if absent_since is None:
                absent_since = now
            # Detached restart watchers poll at 0.2s.  Requiring a full
            # second of absence catches a replacement spawned after its old
            # PID disappears instead of racing the venv swap.
            if now - absent_since >= stable_absence_seconds:
                return True, sorted(stopped)
            time.sleep(poll_seconds)
            continue

        absent_since = None
        if not terminate:
            time.sleep(0.1)
            continue
        pid = int(current_pid)
        try:
            valid_gateway = _looks_like_gateway_process(pid)
        except Exception:
            valid_gateway = False
        if pid == initial_pid:
            valid_gateway = valid_gateway and _same_initial_process(pid)
            if not valid_gateway:
                initial_candidate_live = False
        if not valid_gateway:
            raise RolloutError(
                f"refusing to signal PID {pid}: process identity is not a "
                "validated gateway instance"
            )
        now = time.monotonic()
        first_signal = signalled.get(pid)
        if first_signal is None:
            try:
                write_planned_stop_marker(pid)
            except Exception:
                pass
            signalled[pid] = now
            stopped.add(pid)
            if now < force_at:
                terminate_pid(pid, force=False)
            else:
                # A replacement first observed after the fleet-wide grace
                # period cannot receive a fresh full budget: doing so would
                # let a restart loop postpone the transaction indefinitely.
                terminate_pid(pid, force=True)
                force_signalled.add(pid)
                continue
        elif now >= force_at and pid not in force_signalled:
            terminate_pid(pid, force=True)
            force_signalled.add(pid)
            # Probe again immediately so the full remaining interval can
            # establish the required one-second stable absence.
            continue
        time.sleep(poll_seconds)
    return False, sorted(stopped)


def quiesce_profile_gateway(
    profile: str, runtime: Any, *, config: RolloutConfig
) -> dict[str, Any]:
    """Stop one planned gateway and prove it cannot hold the live venv."""

    from hermes_cli.profiles import get_profile_dir
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    runtime_profile = str(getattr(runtime, "profile", "") or "")
    if runtime_profile != profile:
        raise RolloutError(
            f"quiesce route mismatch: requested {profile!r}, runtime is {runtime_profile!r}"
        )
    supervisor = _validated_supervisor(profile, runtime)
    if supervisor == "manual":
        detail = getattr(runtime, "detail", {}) or {}
        argv = detail.get("argv") if isinstance(detail, Mapping) else None
        try:
            from gateway.status import looks_like_gateway_command_line

            valid_argv = (
                isinstance(argv, list)
                and bool(argv)
                and all(isinstance(part, str) and part for part in argv)
                and looks_like_gateway_command_line(" ".join(argv))
            )
        except Exception:
            valid_argv = False
        if isinstance(argv, list) and "--external-supervisor" in argv:
            raise RolloutError(
                f"{profile}: external supervisor cannot be quiesced by Hermes"
            )
        if not valid_argv:
            raise RolloutError(
                f"{profile}: manual gateway has no validated relaunch argv; "
                "refusing to stop it"
            )
    token = set_hermes_home_override(str(get_profile_dir(profile)))
    try:
        from gateway.status import (
            _looks_like_gateway_process,
            _pid_exists,
            get_running_pid,
        )

        try:
            old_pid = get_running_pid(cleanup_stale=False)
        except Exception:
            old_pid = None
        if old_pid is None:
            try:
                raw_pid = getattr(runtime, "pid", None)
                candidate_pid = int(raw_pid) if raw_pid is not None else None
            except (TypeError, ValueError):
                candidate_pid = None
            if candidate_pid is not None and _pid_exists(candidate_pid):
                if not _looks_like_gateway_process(candidate_pid):
                    raise RolloutError(
                        f"{profile}: saved PID {candidate_pid} is live but is not "
                        "a validated gateway; refusing to signal it"
                    )
                old_pid = candidate_pid

        old_start_time: Optional[float] = None
        if old_pid is not None:
            detail = getattr(runtime, "detail", {}) or {}
            runtime_pid = getattr(runtime, "pid", None)
            if (
                isinstance(detail, Mapping)
                and runtime_pid is not None
                and int(runtime_pid) == int(old_pid)
                and detail.get("start_time") is not None
            ):
                try:
                    old_start_time = float(detail["start_time"])
                except (TypeError, ValueError):
                    old_start_time = None
            if old_start_time is None:
                try:
                    import psutil

                    old_start_time = float(psutil.Process(old_pid).create_time())
                except Exception:
                    old_start_time = None
            if supervisor == "manual" and old_start_time is None:
                raise RolloutError(
                    f"{profile}: could not snapshot gateway PID {old_pid} "
                    "creation time; refusing to signal it"
                )

        import importlib

        gateway_cli = importlib.import_module("hermes_cli.gateway")
        if supervisor == "systemd":
            scopes = gateway_cli.get_installed_systemd_scopes()
            if len(scopes) != 1:
                raise RolloutError(
                    f"{profile}: expected one installed systemd scope, "
                    f"found {scopes or 'none'}"
                )
            gateway_cli.systemd_stop(system=scopes[0] == "system")
        elif supervisor == "launchd":
            gateway_cli.launchd_stop()
        elif supervisor == "service":
            if sys.platform != "win32":
                raise RolloutError(f"{profile}: Windows service supervisor on {sys.platform}")
            from hermes_cli import gateway_windows

            gateway_windows.stop()

        quiesced, stopped_pids = _quiesce_until_stable(
            initial_pid=old_pid,
            initial_start_time=old_start_time,
            timeout_seconds=config.restart_timeout_seconds,
            terminate=supervisor == "manual",
        )
        if not quiesced:
            raise RolloutError(
                f"{profile}: could not prove gateway quiescence before rollback"
            )
        return {
            "ok": True,
            "quiesced": True,
            "profile": profile,
            "supervisor": supervisor,
            "old_pid": old_pid,
            "stopped_pids": stopped_pids,
        }
    finally:
        reset_hermes_home_override(token)


def quiesce_rollout_fleet(
    plan: Any,
    *,
    config: RolloutConfig,
    profiles: Optional[list[str]] = None,
    quiesce_profile: Optional[
        Callable[[str, Any], Mapping[str, Any]]
    ] = None,
    worker_probe: Optional[Callable[[], list[int]]] = None,
    worker_timeout_seconds: Optional[float] = None,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Best-effort stop gateways and prove their task workers are gone."""

    runtimes = validate_rollout_plan(plan, config)
    order = _rollout_order(runtimes, config.canary_profile, profiles)
    if quiesce_profile is None:
        quiesce_profile = lambda name, runtime: quiesce_profile_gateway(
            name, runtime, config=config
        )
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    quiesced_profiles: list[str] = []
    for profile in order:
        try:
            detail = dict(quiesce_profile(profile, runtimes[profile]))
            if detail.get("ok") is not True or detail.get("quiesced") is not True:
                raise RolloutError("quiesce callback did not prove the gateway stopped")
            detail.update({"ok": True, "quiesced": True, "profile": profile})
            results.append(detail)
            quiesced_profiles.append(profile)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            results.append(
                {"ok": False, "quiesced": False, "profile": profile, "error": error}
            )
            errors.append({"profile": profile, "error": error})
    worker_result: dict[str, Any]
    if errors:
        worker_result = {
            "ok": False,
            "skipped": True,
            "error": "gateway quiescence failed before worker drain proof",
        }
    else:
        worker_profiles = list(order)
        if worker_probe is None:
            try:
                from hermes_cli.kanban_db import active_worker_pids_all_boards
                from hermes_cli.profiles import get_profile_dir
                from hermes_constants import (
                    reset_hermes_home_override,
                    set_hermes_home_override,
                )

                # A staged rollout drains only the profiles in this batch.
                # Waiting on workers owned by untouched gateways would turn a
                # canary back into a fleet-wide outage boundary.
                def probe_all_profile_workers() -> list[int]:
                    active: set[int] = set()
                    for profile in worker_profiles:
                        token = set_hermes_home_override(str(get_profile_dir(profile)))
                        try:
                            active.update(active_worker_pids_all_boards())
                        finally:
                            reset_hermes_home_override(token)
                    return sorted(active)

                worker_probe = probe_all_profile_workers
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                worker_result = {"ok": False, "error": error, "pids": []}
                errors.append({"profile": "kanban-workers", "error": error})
                worker_probe = None
        if worker_probe is not None:
            timeout = (
                config.restart_timeout_seconds
                if worker_timeout_seconds is None
                else worker_timeout_seconds
            )
            deadline = monotonic() + max(0.1, min(float(timeout), 60.0))
            last_pids: list[int] = []
            while True:
                try:
                    last_pids = sorted({int(pid) for pid in worker_probe()})
                except Exception as exc:
                    error = f"{type(exc).__name__}: {exc}"
                    worker_result = {
                        "ok": False,
                        "error": error,
                        "pids": last_pids,
                    }
                    errors.append({"profile": "kanban-workers", "error": error})
                    break
                if not last_pids:
                    worker_result = {
                        "ok": True,
                        "pids": [],
                        "profiles": worker_profiles,
                    }
                    break
                now = monotonic()
                if now >= deadline:
                    error = (
                        "active Kanban worker PIDs did not exit before timeout: "
                        + ", ".join(str(pid) for pid in last_pids)
                    )
                    worker_result = {
                        "ok": False,
                        "error": error,
                        "pids": last_pids,
                    }
                    errors.append({"profile": "kanban-workers", "error": error})
                    break
                sleep(min(0.1, max(0.0, deadline - now)))
    return {
        "ok": (
            bool(order)
            and not errors
            and len(quiesced_profiles) == len(order)
            and worker_result.get("ok", False)
        ),
        "attempted_profiles": order,
        "quiesced_profiles": quiesced_profiles,
        "results": results,
        "workers": worker_result,
        "errors": errors,
    }


def _bookkeeping(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        key: sorted(
            {
                value
                for record in records
                for value in (record.get(key) or [])
                if value is not None
            }
        )
        for key in (
            "restarted_services",
            "relaunched_profiles",
            "externally_supervised_profiles",
            "killed_pids",
        )
    }


def restart_and_verify_fleet(
    plan: Any,
    *,
    expected_sha: str,
    config: RolloutConfig,
    project_root: Path,
    profiles: Optional[list[str]] = None,
    restart_profile: Optional[Callable[[str, Any], Mapping[str, Any]]] = None,
    health_gate: Optional[
        Callable[[str, str, Optional[int]], Mapping[str, Any]]
    ] = None,
) -> dict[str, Any]:
    """Restart and verify a previously quiesced fleet without restoring disk.

    Every selected profile is attempted even when an earlier restart or gate
    fails.  This makes the helper suitable for explicit rollback after the
    caller has quiesced the fleet and restored one checkpoint exactly once.
    """

    runtimes = validate_rollout_plan(plan, config)
    order = _rollout_order(runtimes, config.canary_profile, profiles)
    if restart_profile is None:
        restart_profile = lambda name, runtime: restart_profile_gateway(
            name, runtime, config=config
        )
    if health_gate is None:
        health_gate = lambda name, sha, old_pid: stable_gateway_health(
            name,
            sha,
            previous_pid=old_pid,
            stable_seconds=config.healthy_after_seconds,
            timeout_seconds=config.health_timeout_seconds,
            project_root=Path(project_root),
            smoke_timeout_seconds=config.smoke_timeout_seconds,
            smoke_agent_turn=(
                config.canary_smoke_agent_turn
                and name == config.canary_profile
            ),
        )

    restart_records: list[dict[str, Any]] = []
    gates: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    restarted_profiles: list[str] = []
    deferred_base_exception: BaseException | None = None
    for profile in order:
        detail: dict[str, Any] = {"profile": profile, "ok": False}
        try:
            restarted = dict(restart_profile(profile, runtimes[profile]))
            restarted["profile"] = profile
            restart_records.append(restarted)
            restarted_profiles.append(profile)
            detail["restart"] = restarted
            gate = dict(health_gate(profile, expected_sha, restarted.get("old_pid")))
            gate.setdefault("profile", profile)
            gates.append(gate)
            detail["health"] = gate
            if not gate.get("ok", False):
                raise RolloutError("health callback did not verify the gateway")
            detail["ok"] = True
        except BaseException as exc:
            error = f"{type(exc).__name__}: {exc}"
            detail["error"] = error
            errors.append({"profile": profile, "error": error})
            if not isinstance(exc, Exception) and deferred_base_exception is None:
                deferred_base_exception = exc
        results.append(detail)

    outcome = {
        "expected_sha": expected_sha,
        "attempted_profiles": order,
        "restarted_profiles": restarted_profiles,
        "canary_restarted": config.canary_profile in restarted_profiles,
        "health": gates,
        "results": results,
        "errors": errors,
        "verified": bool(order) and not errors and all(
            result.get("ok", False) for result in results
        ),
        **_bookkeeping(restart_records),
    }
    if deferred_base_exception is not None:
        # Preserve control-flow semantics only after every stopped profile has
        # received its liveness attempt.
        raise deferred_base_exception
    return outcome


def _recover_failed_quiesce(
    plan: Any,
    *,
    runtimes: Mapping[str, Any],
    expected_sha: str,
    config: RolloutConfig,
    project_root: Path,
    proven_stopped: list[str],
    ambiguous_profiles: list[str],
    quiesce_profile: Callable[[str, Any], Mapping[str, Any]],
    restart_profile: Optional[Callable[[str, Any], Mapping[str, Any]]] = None,
    health_gate: Optional[
        Callable[[str, str, Optional[int]], Mapping[str, Any]]
    ] = None,
) -> tuple[dict[str, Any], list[str], BaseException | None]:
    """Prove ambiguous stops, then relaunch only profiles proven absent.

    A stop callback can finish its OS action and then be interrupted before it
    returns its receipt. Relaunching that ambiguous profile immediately can
    race an exiting/manual instance. Repeating the idempotent quiesce first
    turns the state into a positive absence proof; profiles that still cannot
    be proven stopped are deliberately not spawned.
    """

    recovery_errors: list[str] = []
    deferred_base_exception: BaseException | None = None
    stopped = list(dict.fromkeys(proven_stopped))
    recovery: dict[str, Any]
    for profile in dict.fromkeys(ambiguous_profiles):
        if profile in stopped:
            continue
        try:
            detail = dict(quiesce_profile(profile, runtimes[profile]))
            if detail.get("ok") is True and detail.get("quiesced") is True:
                stopped.append(profile)
            else:
                recovery_errors.append(
                    f"{profile}: quiesce retry did not prove absence"
                )
        except BaseException as exc:
            recovery_errors.append(
                f"{profile}: quiesce retry failed: "
                f"{type(exc).__name__}: {exc}"
            )
            if not isinstance(exc, Exception) and deferred_base_exception is None:
                deferred_base_exception = exc

    if stopped:
        try:
            recovery = restart_and_verify_fleet(
                plan,
                expected_sha=expected_sha,
                config=config,
                project_root=project_root,
                profiles=stopped,
                restart_profile=restart_profile,
                health_gate=health_gate,
            )
        except BaseException as exc:
            recovery = {
                "verified": False,
                "attempted_profiles": stopped,
                "restarted_profiles": [],
                "errors": [
                    {
                        "profile": "fleet",
                        "error": (
                            "quiesce failure recovery restart failed: "
                            f"{type(exc).__name__}: {exc}"
                        ),
                    }
                ],
            }
            if not isinstance(exc, Exception) and deferred_base_exception is None:
                deferred_base_exception = exc
    else:
        recovery = {
            "verified": not recovery_errors and not ambiguous_profiles,
            "attempted_profiles": [],
            "restarted_profiles": [],
            "errors": [],
            "restarted_services": [],
            "relaunched_profiles": [],
            "externally_supervised_profiles": [],
            "killed_pids": [],
        }

    recovery["recovery_profiles"] = stopped
    if recovery_errors:
        recovery["verified"] = False
        recovery.setdefault("errors", []).extend(
            {"profile": "quiesce", "error": error}
            for error in recovery_errors
        )
    return recovery, recovery_errors, deferred_base_exception


def quiesce_rollout_fleet_for_update(
    plan: Any,
    *,
    expected_sha: str,
    config: RolloutConfig,
    project_root: Path,
    restart_profile: Optional[Callable[[str, Any], Mapping[str, Any]]] = None,
    health_gate: Optional[
        Callable[[str, str, Optional[int]], Mapping[str, Any]]
    ] = None,
    quiesce_profile: Optional[
        Callable[[str, Any], Mapping[str, Any]]
    ] = None,
    quiesce_worker_probe: Optional[Callable[[], list[int]]] = None,
    recovery_callback: Optional[Callable[[Mapping[str, Any]], None]] = None,
) -> dict[str, Any]:
    """Quiesce for apply, recovering liveness on every failed exit path.

    Success intentionally leaves the fleet stopped for checkpoint/apply.
    Ordinary failure and asynchronous interruption instead relaunch only the
    profiles whose absence is positively proven. ``recovery_callback`` is
    invoked before a deferred control-flow exception is re-raised, allowing
    the command boundary to avoid performing a second ambiguous recovery.
    """

    runtimes = validate_rollout_plan(plan, config)
    base_quiesce = quiesce_profile or (
        lambda name, runtime: quiesce_profile_gateway(
            name, runtime, config=config
        )
    )
    proven_stopped: list[str] = []
    ambiguous_profiles: list[str] = []

    def tracked_quiesce(name: str, runtime: Any) -> Mapping[str, Any]:
        try:
            detail = dict(base_quiesce(name, runtime))
        except BaseException:
            ambiguous_profiles.append(name)
            raise
        if detail.get("ok") is True and detail.get("quiesced") is True:
            proven_stopped.append(name)
        else:
            ambiguous_profiles.append(name)
        return detail

    try:
        quiesce = quiesce_rollout_fleet(
            plan,
            config=config,
            quiesce_profile=tracked_quiesce,
            worker_probe=quiesce_worker_probe,
        )
    except BaseException as exc:
        recovery, recovery_errors, deferred = _recover_failed_quiesce(
            plan,
            runtimes=runtimes,
            expected_sha=expected_sha,
            config=config,
            project_root=project_root,
            proven_stopped=proven_stopped,
            ambiguous_profiles=ambiguous_profiles,
            quiesce_profile=base_quiesce,
            restart_profile=restart_profile,
            health_gate=health_gate,
        )
        if recovery_callback is not None:
            recovery_callback(recovery)
        notes = list(recovery_errors)
        if deferred is not None and deferred is not exc:
            notes.append(
                "quiesce recovery was also interrupted: "
                f"{type(deferred).__name__}: {deferred}"
            )
        if notes and hasattr(exc, "add_note"):
            exc.add_note("; ".join(notes))
        raise

    if quiesce.get("ok", False):
        return quiesce

    recovery, recovery_errors, deferred = _recover_failed_quiesce(
        plan,
        runtimes=runtimes,
        expected_sha=expected_sha,
        config=config,
        project_root=project_root,
        proven_stopped=proven_stopped,
        ambiguous_profiles=ambiguous_profiles,
        quiesce_profile=base_quiesce,
        restart_profile=restart_profile,
        health_gate=health_gate,
    )
    quiesce["failure_recovery"] = recovery
    if recovery_errors:
        quiesce["recovery_errors"] = recovery_errors
    if recovery_callback is not None:
        recovery_callback(recovery)
    if deferred is not None:
        raise deferred
    return quiesce


def quiesce_restart_and_verify_fleet(
    plan: Any,
    *,
    expected_sha: str,
    config: RolloutConfig,
    project_root: Path,
    restart_profile: Optional[Callable[[str, Any], Mapping[str, Any]]] = None,
    health_gate: Optional[
        Callable[[str, str, Optional[int]], Mapping[str, Any]]
    ] = None,
    quiesce_profile: Optional[
        Callable[[str, Any], Mapping[str, Any]]
    ] = None,
    quiesce_worker_probe: Optional[Callable[[], list[int]]] = None,
) -> dict[str, Any]:
    """Canary-first restart/health verification of an already-live fleet.

    This is the post-migration pass used after the first code/venv canary has
    succeeded.  Draining the complete fleet here would silently reintroduce
    the fleet-wide outage that staged rollout is meant to avoid: the canary is
    quiesced, restarted, and gated first, then the remaining profiles advance
    in configured batches.  A failed stage stops the worklist and performs a
    liveness recovery only for profiles whose absence was positively proven.

    Asynchronous control-flow exceptions are re-raised, but only after the
    current stage receives the same bounded recovery attempt.  This preserves
    Ctrl-C/SystemExit semantics without knowingly leaving a stopped gateway.
    """

    runtimes = validate_rollout_plan(plan, config)
    base_quiesce = quiesce_profile or (
        lambda name, runtime: quiesce_profile_gateway(
            name, runtime, config=config
        )
    )
    order = _rollout_order(runtimes, config.canary_profile)
    stages = [[order[0]]] + [
        order[index : index + config.batch_size]
        for index in range(1, len(order), config.batch_size)
    ]
    quiesce_summary: dict[str, Any] = {
        "ok": True,
        "attempted_profiles": [],
        "quiesced_profiles": [],
        "results": [],
        "workers": {"ok": True, "stages": []},
        "errors": [],
        "stages": [],
    }
    result: dict[str, Any] = {
        "expected_sha": expected_sha,
        "order": order,
        "batches": stages[1:],
        "attempted_profiles": [],
        "restarted_profiles": [],
        "canary_restarted": False,
        "health": [],
        "results": [],
        "errors": [],
        "verified": False,
        "status": "running",
        "quiesce": quiesce_summary,
        "stages": [],
        "restarted_services": [],
        "relaunched_profiles": [],
        "externally_supervised_profiles": [],
        "killed_pids": [],
    }

    def merge_restart(outcome: Mapping[str, Any]) -> None:
        for key in (
            "restarted_profiles",
            "restarted_services",
            "relaunched_profiles",
            "externally_supervised_profiles",
            "killed_pids",
        ):
            merged = list(result[key])
            for value in outcome.get(key, []) or []:
                if value not in merged:
                    merged.append(value)
            result[key] = merged
        result["health"].extend(list(outcome.get("health", []) or []))
        result["results"].extend(list(outcome.get("results", []) or []))
        result["canary_restarted"] = (
            config.canary_profile in result["restarted_profiles"]
        )

    def record_quiesce(
        detail: Mapping[str, Any], *, batch_number: int
    ) -> dict[str, Any]:
        recorded = dict(detail)
        recorded["batch"] = batch_number
        quiesce_summary["stages"].append(recorded)
        quiesce_summary["attempted_profiles"].extend(
            list(recorded.get("attempted_profiles", []) or [])
        )
        quiesce_summary["quiesced_profiles"].extend(
            list(recorded.get("quiesced_profiles", []) or [])
        )
        quiesce_summary["results"].extend(
            list(recorded.get("results", []) or [])
        )
        quiesce_summary["errors"].extend(
            list(recorded.get("errors", []) or [])
        )
        quiesce_summary["workers"]["stages"].append(
            dict(recorded.get("workers", {}) or {})
        )
        if not recorded.get("ok", False):
            quiesce_summary["ok"] = False
            quiesce_summary["workers"]["ok"] = False
        return recorded

    def stage_recovery(profiles: list[str]) -> dict[str, Any]:
        """Retry liveness for a stage already proven fully stopped."""

        try:
            return restart_and_verify_fleet(
                plan,
                expected_sha=expected_sha,
                config=config,
                project_root=project_root,
                profiles=profiles,
                restart_profile=restart_profile,
                health_gate=health_gate,
            )
        except BaseException as recovery_exc:
            return {
                "verified": False,
                "attempted_profiles": list(profiles),
                "restarted_profiles": [],
                "errors": [
                    {
                        "profile": "stage-recovery",
                        "error": (
                            f"{type(recovery_exc).__name__}: {recovery_exc}"
                        ),
                    }
                ],
                "interrupted": recovery_exc,
            }

    for batch_number, stage_profiles in enumerate(stages):
        result["attempted_profiles"].extend(stage_profiles)
        proven_stopped: list[str] = []
        ambiguous_profiles: list[str] = []

        def tracked_quiesce(name: str, runtime: Any) -> Mapping[str, Any]:
            try:
                detail = dict(base_quiesce(name, runtime))
            except BaseException:
                ambiguous_profiles.append(name)
                raise
            if detail.get("ok") is True and detail.get("quiesced") is True:
                proven_stopped.append(name)
            else:
                ambiguous_profiles.append(name)
            return detail

        try:
            quiesce = quiesce_rollout_fleet(
                plan,
                config=config,
                profiles=stage_profiles,
                quiesce_profile=tracked_quiesce,
                worker_probe=quiesce_worker_probe,
            )
        except BaseException as exc:
            recovery, recovery_errors, deferred = _recover_failed_quiesce(
                plan,
                runtimes=runtimes,
                expected_sha=expected_sha,
                config=config,
                project_root=project_root,
                proven_stopped=proven_stopped,
                ambiguous_profiles=ambiguous_profiles,
                quiesce_profile=base_quiesce,
                restart_profile=restart_profile,
                health_gate=health_gate,
            )
            notes = list(recovery_errors)
            if deferred is not None and deferred is not exc:
                notes.append(
                    "final-pass recovery was also interrupted: "
                    f"{type(deferred).__name__}: {deferred}"
                )
            if not recovery.get("verified", False):
                notes.append("final-pass recovery could not verify liveness")
            if notes and hasattr(exc, "add_note"):
                exc.add_note("; ".join(notes))
            raise

        quiesce = record_quiesce(quiesce, batch_number=batch_number)
        stage: dict[str, Any] = {
            "batch": batch_number,
            "profiles": list(stage_profiles),
            "quiesce": quiesce,
        }
        result["stages"].append(stage)
        if not quiesce.get("ok", False):
            recovery, recovery_errors, deferred = _recover_failed_quiesce(
                plan,
                runtimes=runtimes,
                expected_sha=expected_sha,
                config=config,
                project_root=project_root,
                proven_stopped=proven_stopped,
                ambiguous_profiles=ambiguous_profiles,
                quiesce_profile=base_quiesce,
                restart_profile=restart_profile,
                health_gate=health_gate,
            )
            stage["recovery"] = recovery
            result["partial_quiesce_recovery"] = recovery
            merge_restart(recovery)
            result["errors"].extend(list(quiesce.get("errors", []) or []))
            result["errors"].extend(list(recovery.get("errors", []) or []))
            result["errors"].extend(
                {"profile": "quiesce", "error": error}
                for error in recovery_errors
            )
            result["status"] = "failed"
            if deferred is not None:
                raise deferred
            return result

        try:
            restart = restart_and_verify_fleet(
                plan,
                expected_sha=expected_sha,
                config=config,
                project_root=project_root,
                profiles=stage_profiles,
                restart_profile=restart_profile,
                health_gate=health_gate,
            )
        except BaseException as exc:
            recovery = stage_recovery(stage_profiles)
            interrupted_recovery = recovery.pop("interrupted", None)
            if not recovery.get("verified", False) and hasattr(exc, "add_note"):
                exc.add_note("final-pass stage recovery could not verify liveness")
            if (
                interrupted_recovery is not None
                and interrupted_recovery is not exc
                and hasattr(exc, "add_note")
            ):
                exc.add_note(
                    "final-pass stage recovery was also interrupted: "
                    f"{type(interrupted_recovery).__name__}: "
                    f"{interrupted_recovery}"
                )
            raise

        stage["restart"] = restart
        merge_restart(restart)
        if not restart.get("verified", False):
            recovery = stage_recovery(stage_profiles)
            interrupted_recovery = recovery.pop("interrupted", None)
            stage["recovery"] = recovery
            result["stage_recovery"] = recovery
            merge_restart(recovery)
            result["errors"].extend(list(restart.get("errors", []) or []))
            result["errors"].extend(list(recovery.get("errors", []) or []))
            result["status"] = "failed"
            if interrupted_recovery is not None:
                raise interrupted_recovery
            return result

    result["verified"] = True
    result["status"] = "healthy"
    return result


def run_canary_rollout(
    plan: Any,
    *,
    expected_sha: str,
    checkpoint: Path,
    config: RolloutConfig,
    project_root: Path,
    restart_profile: Optional[Callable[[str, Any], Mapping[str, Any]]] = None,
    health_gate: Optional[
        Callable[[str, str, Optional[int]], Mapping[str, Any]]
    ] = None,
    rollback: Optional[Callable[[Path], Mapping[str, Any]]] = None,
    rollback_git_boundary: Optional[GitMutationBoundary] = None,
    quiesce_profile: Optional[
        Callable[[str, Any], Mapping[str, Any]]
    ] = None,
    quiesce_worker_probe: Optional[Callable[[], list[int]]] = None,
    prequiesced_profiles: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Quiesce/restart the canary, gate it, then advance bounded batches.

    Any exception after the first restart stops the worklist, restores the
    checkpoint, restarts every attempted profile plus any caller-proven
    ``prequiesced_profiles`` on ``pre_sha`` (canary first), and raises a typed
    ``RolloutExecutionError`` carrying durable receipt data. Passing an empty
    ``prequiesced_profiles`` list opts into profile-scoped staged quiescence;
    ``None`` preserves the legacy helper contract for injected restart tests.
    """

    validate_rollout_coordinator(Path(project_root))
    if rollback_git_boundary is None:
        rollback_git_boundary = capture_git_mutation_boundary(
            Path(project_root)
        )
    runtimes = validate_rollout_plan(plan, config)
    prequiesced = (
        _rollout_order(runtimes, config.canary_profile, prequiesced_profiles)
        if prequiesced_profiles is not None
        else []
    )
    if restart_profile is None:
        restart_profile = lambda name, runtime: restart_profile_gateway(
            name, runtime, config=config
        )
    if health_gate is None:
        health_gate = lambda name, sha, old_pid: stable_gateway_health(
            name,
            sha,
            previous_pid=old_pid,
            stable_seconds=config.healthy_after_seconds,
            timeout_seconds=config.health_timeout_seconds,
            project_root=Path(project_root),
            smoke_timeout_seconds=config.smoke_timeout_seconds,
            smoke_agent_turn=(
                config.canary_smoke_agent_turn
                and name == config.canary_profile
            ),
        )
    if rollback is None:
        rollback = lambda path: restore_checkpoint(
            path,
            project_root,
            transaction_owned_reset=True,
            expected_git_boundary=rollback_git_boundary,
        )

    ordered = _rollout_order(runtimes, config.canary_profile)
    batches = [
        ordered[index : index + config.batch_size]
        for index in range(1, len(ordered), config.batch_size)
    ]
    restart_records: list[Mapping[str, Any]] = []
    attempted_profiles: list[str] = []
    gates: list[Mapping[str, Any]] = []
    smoke_mode = (
        "provider-turn" if config.canary_smoke_agent_turn else "structural"
    )
    canary_gate_started = False
    result: dict[str, Any] = {
        "enabled": True,
        "checkpoint": Path(checkpoint).name,
        "expected_sha": expected_sha,
        "canary_profile": config.canary_profile,
        "order": ordered,
        "batches": batches,
        "status": "running",
        "gates": gates,
        "quiesce": [],
        "prequiesced_profiles": prequiesced,
        "smoke": {"mode": smoke_mode, "ok": None, "result": None},
        "rollback": {"attempted": False},
    }

    def quiesce_stage(profiles: list[str], batch_number: int) -> None:
        # Production always supplies this parameter: all profiles on Windows
        # (where the live venv is locked), or [] on POSIX so old-generation
        # peers stay available until their own bounded batch advances.
        if prequiesced_profiles is None:
            return
        pending = [profile for profile in profiles if profile not in prequiesced]
        if not pending:
            return
        detail = dict(
            quiesce_rollout_fleet(
                plan,
                config=config,
                profiles=pending,
                quiesce_profile=quiesce_profile,
                worker_probe=quiesce_worker_probe,
            )
        )
        detail["batch"] = batch_number
        result["quiesce"].append(detail)
        if not detail.get("ok", False):
            raise RolloutError(
                "could not quiesce the staged rollout profiles: "
                + ", ".join(pending)
            )

    try:
        canary_runtime = runtimes[config.canary_profile]
        attempted_profiles.append(config.canary_profile)
        quiesce_stage([config.canary_profile], 0)
        canary_restart = dict(restart_profile(config.canary_profile, canary_runtime))
        canary_restart["profile"] = config.canary_profile
        restart_records.append(canary_restart)
        canary_gate_started = True
        canary_gate = dict(
            health_gate(
                config.canary_profile,
                expected_sha,
                canary_restart.get("old_pid"),
            )
        )
        gates.append(canary_gate)
        reported_smoke = canary_gate.get("smoke")
        if isinstance(reported_smoke, Mapping):
            smoke_detail = dict(reported_smoke)
            smoke_detail.setdefault("mode", smoke_mode)
            result["smoke"] = {
                "mode": smoke_mode,
                "ok": smoke_detail.get("ok") is True,
                "result": smoke_detail,
            }
        else:
            result["smoke"] = {
                "mode": smoke_mode,
                "ok": canary_gate.get("ok") is True,
                "result": None,
            }
        if not canary_gate.get("ok", False):
            raise RolloutError("canary health callback did not verify the gateway")
        for batch_number, batch in enumerate(batches, start=1):
            batch_records: list[tuple[str, Mapping[str, Any]]] = []
            attempted_profiles.extend(batch)
            quiesce_stage(batch, batch_number)
            for profile in batch:
                record = dict(restart_profile(profile, runtimes[profile]))
                record["profile"] = profile
                restart_records.append(record)
                batch_records.append((profile, record))
            for profile, record in batch_records:
                gate = dict(health_gate(profile, expected_sha, record.get("old_pid")))
                gate["batch"] = batch_number
                gates.append(gate)
                if not gate.get("ok", False):
                    raise RolloutError(
                        f"{profile} health callback did not verify the gateway"
                    )
        result["status"] = "healthy"
        result["attempted_profiles"] = attempted_profiles
        result.update(_bookkeeping(restart_records))
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["failure"] = f"{type(exc).__name__}: {exc}"
        if canary_gate_started and result["smoke"].get("ok") is None:
            result["smoke"] = {
                "mode": smoke_mode,
                "ok": False,
                "result": {"error": result["failure"]},
            }
        rollback_result: dict[str, Any] = {
            "attempted": True,
            "restore_attempted": False,
            "restored": False,
            "canary_restarted": False,
            "verified": False,
            "attempted_profiles": list(attempted_profiles),
        }
        recovery_profiles = _rollout_order(
            runtimes,
            config.canary_profile,
            list(dict.fromkeys([*attempted_profiles, *prequiesced])),
        )

        def recover_generation(sha: str, key: str) -> None:
            """Bring every ambiguously stopped profile back without restoring."""

            try:
                recovery = restart_and_verify_fleet(
                    plan,
                    expected_sha=sha,
                    config=config,
                    project_root=project_root,
                    profiles=recovery_profiles,
                    restart_profile=restart_profile,
                    health_gate=health_gate,
                )
                recovery["recovery_profiles"] = recovery.pop(
                    "attempted_profiles"
                )
                rollback_result[key] = recovery
            except Exception as recovery_exc:
                rollback_result[key] = {
                    "verified": False,
                    "recovery_profiles": recovery_profiles,
                    "error": f"{type(recovery_exc).__name__}: {recovery_exc}",
                }

        try:
            quiesce_result = quiesce_rollout_fleet(
                plan,
                config=config,
                profiles=list(attempted_profiles),
                quiesce_profile=quiesce_profile,
                worker_probe=quiesce_worker_probe,
            )
            rollback_result["quiesce"] = quiesce_result
            if not quiesce_result.get("ok", False):
                rollback_result["error"] = (
                    "could not quiesce every attempted profile before restore"
                )
                # Disk is still the candidate generation. The pre-apply drain
                # may also have stopped profiles that never advanced, so the
                # recovery worklist is attempted + prequiesced, not merely the
                # subset returned by this failed drain.
                recover_generation(expected_sha, "current_generation_recovery")
            else:
                rollback_result["restore_attempted"] = True
                restored = dict(rollback(Path(checkpoint)))
                rollback_result.update(restored)
                rollback_result["restored"] = bool(restored.get("restored"))
                if not rollback_result["restored"]:
                    raise RollbackError(
                        "rollback callback did not restore the checkpoint"
                    )
                old_sha = str(
                    restored.get("sha") or read_checkpoint(checkpoint)["pre_sha"]
                )
                recovery = restart_and_verify_fleet(
                    plan,
                    expected_sha=old_sha,
                    config=config,
                    project_root=project_root,
                    profiles=recovery_profiles,
                    restart_profile=restart_profile,
                    health_gate=health_gate,
                )
                recovery["recovery_profiles"] = recovery.pop(
                    "attempted_profiles"
                )
                rollback_result.update(recovery)
                rollback_result["attempted_profiles"] = list(attempted_profiles)
        except Exception as rollback_exc:
            rollback_result.setdefault(
                "error", f"{type(rollback_exc).__name__}: {rollback_exc}"
            )
            # ``restore_checkpoint`` compensates its own pre-commit failures,
            # while an ``after_restore`` callback may fail after old disk was
            # committed. Inspect only identities we can prove; otherwise the
            # still-current candidate is the conservative expectation.
            recovery_sha = expected_sha
            try:
                old_sha_hint = str(read_checkpoint(checkpoint)["pre_sha"])
                live_sha, _branch, _detached = _git_identity(Path(project_root))
                if live_sha in {expected_sha, old_sha_hint}:
                    recovery_sha = live_sha
            except Exception:
                pass
            recover_generation(
                recovery_sha, "failed_rollback_generation_recovery"
            )
        result["rollback"] = rollback_result
        result["attempted_profiles"] = attempted_profiles
        result.update(_bookkeeping(restart_records))
        raise RolloutExecutionError(str(exc), result) from exc


def restore_and_verify_fleet(
    checkpoint: Path,
    plan: Any,
    *,
    config: RolloutConfig,
    project_root: Path,
    restart_profile: Optional[Callable[[str, Any], Mapping[str, Any]]] = None,
    health_gate: Optional[
        Callable[[str, str, Optional[int]], Mapping[str, Any]]
    ] = None,
    quiesce_profile: Optional[
        Callable[[str, Any], Mapping[str, Any]]
    ] = None,
    quiesce_worker_probe: Optional[Callable[[], list[int]]] = None,
    after_restore: Optional[Callable[[], None]] = None,
    transaction_owned_reset: bool = False,
) -> dict[str, Any]:
    """Compensate an apply/coordinator failure with a full old fleet restart.

    Used when failure occurs outside ``run_canary_rollout``'s precisely
    tracked worklist. Restarting the complete saved gateway plan is the only
    fail-closed answer when the set of advanced profiles is unknown.
    """
    project = Path(project_root)
    validate_rollout_coordinator(project)
    rollback_git_boundary = capture_git_mutation_boundary(project)
    current_sha, _current_branch, _current_detached = _git_identity(project)

    def recover_generation(
        expected_sha: str, profiles: Optional[list[str]] = None
    ) -> str | None:
        """Best-effort compensation before any checkpoint bytes are restored."""

        try:
            restarted = restart_and_verify_fleet(
                plan,
                expected_sha=expected_sha,
                config=config,
                project_root=project,
                profiles=profiles,
                restart_profile=restart_profile,
                health_gate=health_gate,
            )
            if not restarted.get("verified", False):
                return "ambiguous fleet restart could not be verified"
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return None

    try:
        quiesce = quiesce_rollout_fleet(
            plan,
            config=config,
            quiesce_profile=quiesce_profile,
            worker_probe=quiesce_worker_probe,
        )
    except BaseException as exc:
        # The quiesce loop may have stopped one or more profiles before an
        # asynchronous exception escaped, but no structured worklist exists.
        # Restart the complete current generation before preserving the
        # original control-flow exception.
        restart_error = recover_generation(current_sha)
        if isinstance(exc, Exception):
            recovery_detail = (
                f"; current-generation recovery failed: {restart_error}"
                if restart_error
                else "; current generation restarted and verified"
            )
            raise RollbackError(
                "refusing checkpoint restore because gateway quiescence "
                f"was interrupted: {type(exc).__name__}: {exc}{recovery_detail}"
            ) from exc
        raise
    if not quiesce.get("ok", False):
        failures = "; ".join(
            f"{item['profile']}: {item['error']}" for item in quiesce["errors"]
        )
        attempted = list(quiesce.get("attempted_profiles", [])) or None
        restart_error = recover_generation(current_sha, attempted)
        recovery_detail = (
            f"; current-generation recovery failed: {restart_error}"
            if restart_error
            else "; current generation restarted and verified"
        )
        raise RollbackError(
            "refusing checkpoint restore because gateway quiescence failed: "
            f"{failures}{recovery_detail}"
        )
    try:
        restored = restore_checkpoint(
            checkpoint,
            project_root,
            transaction_owned_reset=transaction_owned_reset,
            expected_git_boundary=rollback_git_boundary,
        )
    except BaseException as exc:
        restart_error = recover_generation(current_sha)
        if isinstance(exc, Exception):
            recovery_detail = (
                f"; current-generation recovery failed: {restart_error}"
                if restart_error
                else "; current generation restarted and verified"
            )
            raise RollbackError(
                "checkpoint restore failed before commit: "
                f"{type(exc).__name__}: {exc}{recovery_detail}"
            ) from exc
        raise
    old_sha = str(restored["sha"])
    try:
        if after_restore is not None:
            after_restore()
    except BaseException as exc:
        restart_error = recover_generation(old_sha)
        if isinstance(exc, Exception):
            recovery_detail = (
                f"; restored-generation recovery failed: {restart_error}"
                if restart_error
                else "; restored generation restarted and verified"
            )
            raise RollbackError(
                "checkpoint restored but post-restore reconciliation failed: "
                f"{type(exc).__name__}: {exc}{recovery_detail}"
            ) from exc
        raise
    recovery = restart_and_verify_fleet(
        plan,
        expected_sha=old_sha,
        config=config,
        project_root=project_root,
        restart_profile=restart_profile,
        health_gate=health_gate,
    )
    return {
        **restored,
        **recovery,
        "attempted": True,
        "restored": True,
        "quiesce": quiesce,
    }


def rollout_confirmation_context(
    *,
    action: str,
    project_root: Path,
    plan: Any = None,
    config: Optional[RolloutConfig] = None,
    checkpoint: Optional[Path] = None,
    correlation_id: Optional[str] = None,
    origin: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Typed, profile/install-scoped payload for Telegram/Discord prompts."""

    from hermes_constants import get_hermes_home
    from hermes_cli.process_identity import install_id

    origin = dict(origin or {})
    profile_home = origin.get("profile_home") or str(get_hermes_home().resolve())
    return {
        "schema": 1,
        "kind": "update_confirmation",
        "action": action,
        "correlation_id": correlation_id,
        "origin_profile": origin.get("origin_profile"),
        "profile_home": str(profile_home),
        "control_home": origin.get("control_home")
        or str(get_hermes_home().resolve()),
        "install_root": str(Path(project_root).resolve()),
        "install_id": install_id(Path(project_root)),
        "plan": plan.to_dict() if hasattr(plan, "to_dict") else plan,
        "rollout": config.to_dict() if config is not None else None,
        "checkpoint": Path(checkpoint).name if checkpoint is not None else None,
    }


def format_confirmation(context: Mapping[str, Any]) -> str:
    """Compact text fallback; adapters may render the typed context richly."""

    action = str(context.get("action") or "update")
    rollout = context.get("rollout") or {}
    lines = [
        f"Confirm Hermes {action} for this bot profile?",
        f"Profile: {context.get('profile_home')}",
        f"Install: {context.get('install_root')}",
    ]
    if action == "rollback":
        lines.append(f"Checkpoint: {context.get('checkpoint') or 'latest'}")
    elif rollout.get("enabled"):
        lines.append(f"Canary first: {rollout.get('canary_profile')}")
        lines.append(
            "On a failed health/smoke gate, code and dependencies will be rolled back."
        )
    lines.append("Proceed? [y/N]")
    return "\n".join(lines)
