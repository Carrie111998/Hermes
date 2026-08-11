"""Validated repository policy for governed Kanban boards.

The repository contract is deliberately data-only.  It describes the refs and
commands that a board is allowed to use; the lifecycle coordinator owns the
SQLite state transitions that consume it.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any


FULL_SHA = re.compile(r"[0-9a-f]{40}")

_REPOSITORY_KEYS = frozenset(
    {
        "base_ref",
        "target_branch",
        "verification_profiles",
        "ci_observation",
        "boundary_evidence",
    }
)
_VERIFICATION_PROFILE_KEYS = frozenset({"commands"})
_VERIFICATION_COMMAND_KEYS = frozenset({"argv", "workdir", "timeout_seconds"})
_CI_OBSERVATION_KEYS = frozenset({"provider", "required_workflows"})
_BOUNDARY_EVIDENCE_KEYS = frozenset(
    {"test_globs", "fixture_globs", "generated_paths"}
)
_MAX_TIMEOUT_SECONDS = 86_400


class RepositoryConfigurationError(ValueError):
    """A repository contract cannot safely be used.

    ``code`` is intentionally stable so callers can distinguish operator
    configuration failures from test or infrastructure failures without
    parsing an exception message.
    """

    def __init__(self, code: str, detail: str | None = None) -> None:
        self.code = code
        message = code if not detail else f"{code}: {detail}"
        super().__init__(message)


@dataclass(frozen=True)
class VerificationCommand:
    argv: tuple[str, ...]
    workdir: PurePosixPath
    timeout_seconds: int


@dataclass(frozen=True)
class VerificationProfile:
    commands: tuple[VerificationCommand, ...]


@dataclass(frozen=True)
class RepositoryContract:
    repo_root: Path
    base_ref: str
    target_branch: str
    verification: Mapping[str, VerificationProfile]
    generated_paths: tuple[PurePosixPath, ...]
    ci_workflows: tuple[str, ...]
    digest: str


@dataclass(frozen=True)
class RefreshRequest:
    """Pinned inputs for one dispatcher-owned story refresh attempt."""

    repo_root: Path
    story_id: str
    story_branch: str
    story_worktree: Path
    story_sha: str
    epic_branch: str
    epic_tip_sha: str


@dataclass(frozen=True)
class RefreshResult:
    """Typed result of an isolated story refresh attempt.

    ``conflict_worktree`` is intentionally retained for a conflict.  It is a
    disposable detached checkout, never the user's story worktree, and gives a
    later Development worker the exact files that need attention.
    """

    kind: str
    before_sha: str | None = None
    after_sha: str | None = None
    current_sha: str | None = None
    current_epic_tip_sha: str | None = None
    dirty_paths: tuple[str, ...] = ()
    conflict_paths: tuple[str, ...] = ()
    conflict_worktree: Path | None = None
    error: str | None = None
    story_id: str | None = None
    story_branch: str | None = None
    story_sha: str | None = None
    epic_branch: str | None = None
    epic_tip_sha: str | None = None

    @property
    def retained_worktree(self) -> Path | None:
        """Compatibility name for the retained conflict evidence checkout."""

        return self.conflict_worktree


def _error(code: str, detail: str | None = None) -> RepositoryConfigurationError:
    return RepositoryConfigurationError(code, detail)


def _require_mapping(value: Any, code: str, detail: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _error(code, detail)
    return value


def _reject_unknown_keys(
    value: Mapping[str, Any], allowed: frozenset[str]
) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise _error("unknown_key", ", ".join(unknown))


def _require_string(value: Any, code: str, field: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _error(code, field)
    if "\x00" in value or any(char.isspace() for char in value):
        raise _error(code, field)
    return value


def _validate_ref(value: Any, code: str, field: str) -> str:
    ref = _require_string(value, code, field)
    # Refs are passed as one argv item, but revision expressions and malformed
    # ref names would make the configured policy branch-dependent or ambiguous.
    if (
        ref.startswith("-")
        or ref.startswith("/")
        or ref.endswith(("/", "."))
        or "//" in ref
        or ".." in ref
        or "@{" in ref
        or any(char in ref for char in "~^:?*[\\")
    ):
        raise _error(code, field)
    return ref


def _normalize_relative_path(
    value: Any, *, code: str, field: str, allow_dot: bool
) -> PurePosixPath:
    if not isinstance(value, str) or not value or value != value.strip():
        raise _error(code, field)
    if "\x00" in value or "\\" in value:
        raise _error(code, field)
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts:
        raise _error(code, field)
    if not allow_dot and not path.parts:
        raise _error(code, field)
    return path


def _ensure_inside(root: Path, relative: PurePosixPath, *, code: str) -> Path:
    candidate = (root / Path(*relative.parts)).resolve(strict=False)
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise _error(code, str(relative)) from exc
    return candidate


def _validate_globs(value: Any, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise _error("malformed_boundary_evidence", field)
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item or item != item.strip():
            raise _error("malformed_boundary_evidence", field)
        if "\x00" in item or "\\" in item:
            raise _error("malformed_boundary_evidence", field)
        pattern = PurePosixPath(item)
        if pattern.is_absolute() or ".." in pattern.parts:
            raise _error("invalid_path", item)
        normalized.append(item)
    return tuple(normalized)


def _validate_workdir(repo_root: Path, value: Any) -> PurePosixPath:
    workdir = _normalize_relative_path(
        value, code="invalid_workdir", field="workdir", allow_dot=True
    )
    resolved = _ensure_inside(repo_root, workdir, code="invalid_workdir")
    if not resolved.is_dir():
        raise _error("invalid_workdir", str(workdir))
    return workdir


def _validate_command(
    repo_root: Path, value: Any
) -> tuple[VerificationCommand, dict[str, Any]]:
    command = _require_mapping(value, "malformed_command", "command")
    _reject_unknown_keys(command, _VERIFICATION_COMMAND_KEYS)
    if set(command) != _VERIFICATION_COMMAND_KEYS:
        raise _error("malformed_command", "argv, workdir, timeout_seconds are required")

    argv_value = command["argv"]
    if (
        not isinstance(argv_value, list)
        or not argv_value
        or any(
            not isinstance(arg, str) or not arg or "\x00" in arg
            for arg in argv_value
        )
    ):
        raise _error("malformed_command", "argv")
    argv = tuple(argv_value)

    workdir = _validate_workdir(repo_root, command["workdir"])
    timeout = command["timeout_seconds"]
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, int)
        or not 0 < timeout <= _MAX_TIMEOUT_SECONDS
    ):
        raise _error("invalid_timeout", "timeout_seconds")

    normalized = {
        "argv": list(argv),
        "workdir": workdir.as_posix(),
        "timeout_seconds": timeout,
    }
    return VerificationCommand(argv, workdir, timeout), normalized


def _validate_profile(
    repo_root: Path, value: Any
) -> tuple[VerificationProfile, list[dict[str, Any]]]:
    # The compact list form is accepted for board metadata written by the
    # first v2 prototype.  The object form is canonical and makes the profile
    # schema self-describing.
    if isinstance(value, list):
        commands_value = value
    else:
        profile = _require_mapping(value, "malformed_profile", "profile")
        _reject_unknown_keys(profile, _VERIFICATION_PROFILE_KEYS)
        if set(profile) != _VERIFICATION_PROFILE_KEYS:
            raise _error("malformed_profile", "commands")
        commands_value = profile["commands"]

    if not isinstance(commands_value, list) or not commands_value:
        raise _error("malformed_profile", "commands")

    commands: list[VerificationCommand] = []
    normalized: list[dict[str, Any]] = []
    for raw_command in commands_value:
        command, command_json = _validate_command(repo_root, raw_command)
        commands.append(command)
        normalized.append(command_json)
    return VerificationProfile(tuple(commands)), normalized


def _canonical_contract_json(
    *,
    base_ref: str,
    target_branch: str,
    verification_json: Mapping[str, list[dict[str, Any]]],
    ci_provider: str,
    ci_workflows: tuple[str, ...],
    test_globs: tuple[str, ...],
    fixture_globs: tuple[str, ...],
    generated_paths: tuple[PurePosixPath, ...],
) -> dict[str, Any]:
    return {
        "base_ref": base_ref,
        "target_branch": target_branch,
        "verification_profiles": {
            name: {"commands": verification_json[name]}
            for name in sorted(verification_json)
        },
        "ci_observation": {
            "provider": ci_provider,
            "required_workflows": list(ci_workflows),
        },
        "boundary_evidence": {
            "test_globs": list(test_globs),
            "fixture_globs": list(fixture_globs),
            "generated_paths": [path.as_posix() for path in generated_paths],
        },
    }


def load_repository_contract(
    board_metadata: Mapping[str, object], *, repo_root: Path
) -> RepositoryContract:
    """Validate and normalize ``board_metadata['repository']``.

    The returned paths are repository-relative POSIX paths.  The repository
    root itself is resolved once, and every generated path is checked against
    the tracked index so a later evidence phase cannot authorize an arbitrary
    filesystem path.
    """

    metadata = _require_mapping(
        board_metadata, "malformed_repository", "board_metadata"
    )
    if "repository" not in metadata:
        raise _error("missing_repository", "repository")
    repository = _require_mapping(
        metadata["repository"], "malformed_repository", "repository"
    )
    _reject_unknown_keys(repository, _REPOSITORY_KEYS)
    if set(repository) != _REPOSITORY_KEYS:
        missing = _REPOSITORY_KEYS - set(repository)
        if "base_ref" in missing:
            raise _error("missing_base_ref", "base_ref")
        if "target_branch" in missing:
            raise _error("missing_target_branch", "target_branch")
        raise _error("malformed_repository", "required repository fields")

    root = Path(repo_root).expanduser().resolve(strict=False)
    if not root.is_dir():
        raise _error("invalid_repo_root", str(root))

    base_ref = _validate_ref(repository["base_ref"], "malformed_base_ref", "base_ref")
    target_branch = _validate_ref(
        repository["target_branch"], "malformed_target_branch", "target_branch"
    )
    if target_branch.startswith("refs/"):
        raise _error("malformed_target_branch", "target_branch")

    profiles_value = repository["verification_profiles"]
    profiles_mapping = _require_mapping(
        profiles_value, "malformed_profiles", "verification_profiles"
    )
    if not profiles_mapping:
        raise _error("malformed_profiles", "verification_profiles")

    verification: dict[str, VerificationProfile] = {}
    verification_json: dict[str, list[dict[str, Any]]] = {}
    for name, raw_profile in profiles_mapping.items():
        if not isinstance(name, str) or not name or name != name.strip():
            raise _error("malformed_profiles", "profile name")
        profile, profile_json = _validate_profile(root, raw_profile)
        verification[name] = profile
        verification_json[name] = profile_json

    ci_observation = _require_mapping(
        repository["ci_observation"], "malformed_ci_observation", "ci_observation"
    )
    _reject_unknown_keys(ci_observation, _CI_OBSERVATION_KEYS)
    if "required_workflows" not in ci_observation:
        raise _error("missing_ci_workflows", "required_workflows")
    ci_provider = ""
    if "provider" in ci_observation:
        ci_provider = _require_string(
            ci_observation["provider"], "malformed_ci_observation", "provider"
        )
    workflows_value = ci_observation["required_workflows"]
    if not isinstance(workflows_value, list) or not workflows_value:
        raise _error("missing_ci_workflows", "required_workflows")
    if any(
        not isinstance(workflow, str)
        or not workflow
        or workflow != workflow.strip()
        or "\x00" in workflow
        for workflow in workflows_value
    ):
        raise _error("malformed_ci_workflows", "required_workflows")
    ci_workflows = tuple(workflows_value)
    if len(set(ci_workflows)) != len(ci_workflows):
        raise _error("malformed_ci_workflows", "duplicate workflow")

    boundary = _require_mapping(
        repository["boundary_evidence"], "malformed_boundary_evidence", "boundary_evidence"
    )
    _reject_unknown_keys(boundary, _BOUNDARY_EVIDENCE_KEYS)
    if set(boundary) != _BOUNDARY_EVIDENCE_KEYS:
        raise _error(
            "malformed_boundary_evidence",
            "test_globs, fixture_globs, generated_paths",
        )
    test_globs = _validate_globs(boundary["test_globs"], "test_globs")
    fixture_globs = _validate_globs(boundary["fixture_globs"], "fixture_globs")

    generated_value = boundary["generated_paths"]
    if not isinstance(generated_value, list):
        raise _error("malformed_boundary_evidence", "generated_paths")
    generated_paths: list[PurePosixPath] = []
    for raw_path in generated_value:
        path = _normalize_relative_path(
            raw_path,
            code="invalid_path",
            field="generated_paths",
            allow_dot=False,
        )
        _ensure_inside(root, path, code="invalid_path")
        completed = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "ls-files",
                "--error-unmatch",
                "--",
                path.as_posix(),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise _error("untracked_path", path.as_posix())
        generated_paths.append(path)
    normalized_generated_paths = tuple(generated_paths)
    if len(set(normalized_generated_paths)) != len(normalized_generated_paths):
        raise _error("malformed_boundary_evidence", "duplicate generated path")

    canonical = _canonical_contract_json(
        base_ref=base_ref,
        target_branch=target_branch,
        verification_json=verification_json,
        ci_provider=ci_provider,
        ci_workflows=ci_workflows,
        test_globs=test_globs,
        fixture_globs=fixture_globs,
        generated_paths=normalized_generated_paths,
    )
    digest = hashlib.sha256(
        json.dumps(
            canonical,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()

    return RepositoryContract(
        repo_root=root,
        base_ref=base_ref,
        target_branch=target_branch,
        verification=MappingProxyType(verification),
        generated_paths=normalized_generated_paths,
        ci_workflows=ci_workflows,
        digest=digest,
    )


def resolve_commit(repo_root: Path, ref: str) -> str:
    """Resolve a configured ref to one full commit SHA.

    Ambiguous short names are rejected even when Git happens to return a
    result, because accepting them would make a board depend on local ref
    layout.  No shell is involved and no ref is written.
    """

    if not isinstance(ref, str) or not ref or ref != ref.strip() or "\x00" in ref:
        raise _error("missing_ref", "invalid ref")
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(Path(repo_root).expanduser().resolve(strict=False)),
            "rev-parse",
            "--verify",
            f"{ref}^{{commit}}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    sha = completed.stdout.strip()
    if (
        completed.returncode != 0
        or "ambiguous" in completed.stderr.lower()
        or not FULL_SHA.fullmatch(sha)
    ):
        raise _error("missing_ref", ref)
    return sha


def _refresh_git(
    path: Path, *args: str, check: bool = False
) -> subprocess.CompletedProcess[str]:
    """Run one bounded Git operation without invoking a shell."""

    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=check,
    )


def _refresh_sha(path: Path, ref: str) -> str | None:
    completed = _refresh_git(path, "rev-parse", "--verify", f"{ref}^{{commit}}")
    value = (completed.stdout or "").strip()
    return value if completed.returncode == 0 and FULL_SHA.fullmatch(value) else None


def _refresh_status_paths(path: Path) -> tuple[str, ...] | None:
    completed = _refresh_git(
        path,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
    )
    if completed.returncode != 0:
        return None
    paths: list[str] = []
    for line in (completed.stdout or "").splitlines():
        if len(line) >= 4:
            paths.append(line[3:])
    return tuple(paths)


def _refresh_conflict_paths(path: Path) -> tuple[str, ...]:
    completed = _refresh_git(path, "diff", "--name-only", "--diff-filter=U")
    return tuple(
        line.strip()
        for line in (completed.stdout or "").splitlines()
        if line.strip()
    )


def _remove_refresh_worktree(repo_root: Path, worktree: Path, parent: Path) -> None:
    """Best-effort cleanup for a successful or failed disposable checkout."""

    try:
        _refresh_git(repo_root, "worktree", "remove", "--force", str(worktree))
    except (OSError, subprocess.SubprocessError):
        pass
    if worktree.exists():
        try:
            shutil.rmtree(worktree)
        except OSError:
            pass
    try:
        parent.rmdir()
    except OSError:
        pass


def _refresh_story_branch(request: RefreshRequest) -> RefreshResult:
    """Refresh a clean story branch from a pinned Epic tip in isolation.

    The caller pins both source refs immediately before dispatch.  This
    function rechecks both refs, refuses dirty user work, builds the merge in a
    detached disposable worktree, and only then advances the story ref with
    ``git update-ref <new> <old>``.  A conflict leaves that disposable worktree
    in place as evidence; the original story checkout and branch are untouched.
    """

    repo_root = Path(request.repo_root).expanduser().resolve(strict=False)
    story_worktree = Path(request.story_worktree).expanduser().resolve(strict=False)
    before_sha = str(request.story_sha or "").strip()
    pinned_epic_sha = str(request.epic_tip_sha or "").strip()
    if not FULL_SHA.fullmatch(before_sha):
        return RefreshResult("error", error="invalid_story_sha")
    if not FULL_SHA.fullmatch(pinned_epic_sha):
        return RefreshResult("error", error="invalid_epic_tip_sha")

    root = _refresh_git(story_worktree, "rev-parse", "--show-toplevel")
    resolved_root = (root.stdout or "").strip()
    if root.returncode != 0 or not resolved_root:
        return RefreshResult("error", before_sha=before_sha, error="story_worktree_not_git")
    if Path(resolved_root).expanduser().resolve(strict=False) != repo_root:
        return RefreshResult("error", before_sha=before_sha, error="repository_mismatch")

    current_story_sha = _refresh_sha(
        repo_root, f"refs/heads/{request.story_branch}"
    )
    current_epic_sha = _refresh_sha(repo_root, f"refs/heads/{request.epic_branch}")
    if current_story_sha is None or current_epic_sha is None:
        return RefreshResult("error", before_sha=before_sha, error="source_ref_missing")
    if current_story_sha != before_sha or current_epic_sha != pinned_epic_sha:
        return RefreshResult(
            "source_moved",
            before_sha=before_sha,
            current_sha=current_story_sha,
            current_epic_tip_sha=current_epic_sha,
        )

    dirty_paths = _refresh_status_paths(story_worktree)
    if dirty_paths is None:
        return RefreshResult("error", before_sha=before_sha, error="status_failed")
    if dirty_paths:
        return RefreshResult(
            "dirty",
            before_sha=before_sha,
            current_sha=current_story_sha,
            dirty_paths=dirty_paths,
        )

    ancestry = _refresh_git(
        repo_root,
        "merge-base",
        "--is-ancestor",
        pinned_epic_sha,
        before_sha,
    )
    if ancestry.returncode == 0:
        return RefreshResult("unchanged", before_sha=before_sha, after_sha=before_sha)
    if ancestry.returncode != 1:
        return RefreshResult("error", before_sha=before_sha, error="ancestry_check_failed")

    parent = Path(tempfile.mkdtemp(prefix="hermes-story-refresh-"))
    candidate = parent / f"candidate-{uuid.uuid4().hex[:12]}"
    try:
        parent.rmdir()
    except OSError:
        pass
    added = _refresh_git(
        repo_root,
        "worktree",
        "add",
        "--detach",
        str(candidate),
        before_sha,
    )
    if added.returncode != 0:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult("error", before_sha=before_sha, error="candidate_create_failed")

    merged = _refresh_git(candidate, "merge", "--no-ff", "--no-edit", pinned_epic_sha)
    if merged.returncode != 0:
        conflict_paths = _refresh_conflict_paths(candidate)
        if conflict_paths:
            return RefreshResult(
                "conflict",
                before_sha=before_sha,
                conflict_paths=conflict_paths,
                conflict_worktree=candidate,
            )
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult("error", before_sha=before_sha, error="candidate_merge_failed")

    candidate_sha = _refresh_sha(candidate, "HEAD")
    if candidate_sha is None:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult("error", before_sha=before_sha, error="candidate_head_missing")

    # Recheck both pins immediately before the CAS.  A source move never
    # overwrites the story branch and never leaves a disposable checkout behind.
    current_story_sha = _refresh_sha(
        repo_root, f"refs/heads/{request.story_branch}"
    )
    current_epic_sha = _refresh_sha(repo_root, f"refs/heads/{request.epic_branch}")
    if current_story_sha != before_sha or current_epic_sha != pinned_epic_sha:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult(
            "source_moved",
            before_sha=before_sha,
            current_sha=current_story_sha,
            current_epic_tip_sha=current_epic_sha,
        )

    # The isolated merge can take long enough for an operator to edit the
    # original checkout after the first clean-status check.  Recheck immediately
    # before the branch CAS so read-tree can never overwrite newly-created user
    # work, and leave the story branch untouched when it does.
    latest_dirty_paths = _refresh_status_paths(story_worktree)
    if latest_dirty_paths is None:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult("error", before_sha=before_sha, error="status_failed")
    if latest_dirty_paths:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult(
            "dirty",
            before_sha=before_sha,
            current_sha=current_story_sha,
            dirty_paths=latest_dirty_paths,
        )

    updated = _refresh_git(
        repo_root,
        "update-ref",
        f"refs/heads/{request.story_branch}",
        candidate_sha,
        before_sha,
    )
    if updated.returncode != 0:
        current_story_sha = _refresh_sha(
            repo_root, f"refs/heads/{request.story_branch}"
        )
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult(
            "source_moved",
            before_sha=before_sha,
            current_sha=current_story_sha,
            current_epic_tip_sha=current_epic_sha,
        )

    # ``update-ref`` changes the branch atomically, while ``read-tree`` brings
    # the already-verified clean checkout along without reset/clean/stash.
    checked_out = _refresh_git(story_worktree, "read-tree", "-mu", candidate_sha)
    if checked_out.returncode != 0:
        _remove_refresh_worktree(repo_root, candidate, parent)
        return RefreshResult("error", before_sha=before_sha, after_sha=candidate_sha, error="story_checkout_update_failed")
    _remove_refresh_worktree(repo_root, candidate, parent)
    return RefreshResult("refreshed", before_sha=before_sha, after_sha=candidate_sha)


def refresh_story_branch(request: RefreshRequest) -> RefreshResult:
    """Run :func:`_refresh_story_branch` and attach the pinned lineage facts."""

    result = _refresh_story_branch(request)
    return replace(
        result,
        story_id=request.story_id,
        story_branch=request.story_branch,
        story_sha=result.after_sha or result.before_sha,
        epic_branch=request.epic_branch,
        epic_tip_sha=request.epic_tip_sha,
    )
