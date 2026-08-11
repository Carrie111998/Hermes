"""Validated repository policy for governed Kanban boards.

The repository contract is deliberately data-only.  It describes the refs and
commands that a board is allowed to use; the lifecycle coordinator owns the
SQLite state transitions that consume it.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any



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
