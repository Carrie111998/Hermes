"""Operator-owned target allowlist for the DevFlow Delegation Plane.

Requests cannot inject commands or paths. Stage 2 uses only a target explicitly
marked as a synthetic fixture and explicitly enabled for a bounded PR action.
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple, Union


class AllowlistError(ValueError):
    pass


_AUTONOMOUS_ACTIONS = frozenset({"none", "create_pr"})


@dataclass(frozen=True)
class TargetConfig:
    repo: str
    checkout_path: str
    default_branch: str = "main"
    remote: str = "origin"
    allowed_globs: Tuple[str, ...] = ()
    denied_globs: Tuple[str, ...] = ()
    worktree_base: str = ""
    # String entries remain supported for Stage-1 compatibility, but the Stage-2
    # validator executes only argv vectors and rejects strings rather than using
    # a shell. Tuple annotations preserve the existing JSON loader contract.
    test_commands: Tuple[Union[str, Tuple[str, ...]], ...] = ()
    lint_commands: Tuple[Union[str, Tuple[str, ...]], ...] = ()
    typecheck_commands: Tuple[Union[str, Tuple[str, ...]], ...] = ()
    build_commands: Tuple[Union[str, Tuple[str, ...]], ...] = ()
    command_timeout_seconds: int = 1800
    required_checks: Tuple[str, ...] = ()
    risk_ceiling: str = "medium"
    max_autonomous_action: str = "none"
    # Stage-2 action requires all three explicit target-owned gates below.
    executor_enabled: bool = False
    synthetic_fixture: bool = False
    implementation_command: Optional[Tuple[str, ...]] = None
    github_repo: str = ""
    deploy_command: Optional[str] = None
    rollback_command: Optional[str] = None
    health_checks: Tuple[str, ...] = ()
    live_gateway_imports: bool = False
    owners: Tuple[str, ...] = ()
    notify_route: str = ""


@dataclass(frozen=True)
class Allowlist:
    version: str
    targets: Dict[str, TargetConfig] = field(default_factory=dict)


def _tuple(value) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


def _validation_commands(value) -> Tuple[Union[str, Tuple[str, ...]], ...]:
    """Keep legacy strings but preserve JSON argv lists for the Stage-2 gate."""
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        return (str(value),)
    result = []
    for item in value:
        if isinstance(item, list):
            result.append(tuple(str(part) for part in item))
        else:
            result.append(str(item))
    return tuple(result)


def _command_tuple(value, name: str) -> Optional[Tuple[str, ...]]:
    """Parse an operator-owned argv vector; reject shell-string commands."""
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(part, str) and part for part in value):
        raise AllowlistError(
            f"allowlist target '{name}' malformed: implementation_command must be a nonempty argv list"
        )
    return tuple(value)


def load_allowlist(path: Path) -> Allowlist:
    path = Path(path)
    if not path.exists():
        raise AllowlistError(f"allowlist missing: {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AllowlistError(f"allowlist unreadable/malformed: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("targets"), dict):
        raise AllowlistError("allowlist malformed: expected {'version', 'targets'}")

    targets: Dict[str, TargetConfig] = {}
    for name, raw in data["targets"].items():
        if not isinstance(raw, dict) or not raw.get("repo") or not raw.get("checkout_path"):
            raise AllowlistError(f"allowlist target '{name}' malformed: repo+checkout_path required")
        try:
            command_timeout_seconds = int(raw.get("command_timeout_seconds", 1800))
        except (TypeError, ValueError) as exc:
            raise AllowlistError(
                f"allowlist target '{name}' malformed: command_timeout_seconds must be int ({exc})"
            ) from exc
        if command_timeout_seconds <= 0:
            raise AllowlistError(f"allowlist target '{name}' malformed: command_timeout_seconds must be positive")
        max_action = str(raw.get("max_autonomous_action", "none"))
        if max_action not in _AUTONOMOUS_ACTIONS:
            raise AllowlistError(
                f"allowlist target '{name}' malformed: unsupported max_autonomous_action {max_action!r}"
            )
        command = _command_tuple(raw.get("implementation_command"), str(name))
        executor_enabled = bool(raw.get("executor_enabled", False))
        synthetic_fixture = bool(raw.get("synthetic_fixture", False))
        github_repo = str(raw.get("github_repo", ""))
        if executor_enabled and not (synthetic_fixture and command and github_repo and max_action == "create_pr"):
            raise AllowlistError(
                f"allowlist target '{name}' malformed: enabled executor requires synthetic_fixture, "
                "implementation_command, github_repo, and max_autonomous_action='create_pr'"
            )
        targets[str(name)] = TargetConfig(
            repo=str(raw["repo"]),
            checkout_path=str(raw["checkout_path"]),
            default_branch=str(raw.get("default_branch", "main")),
            remote=str(raw.get("remote", "origin")),
            allowed_globs=_tuple(raw.get("allowed_globs")),
            denied_globs=_tuple(raw.get("denied_globs")),
            worktree_base=str(raw.get("worktree_base", "")),
            test_commands=_validation_commands(raw.get("test_commands")),
            lint_commands=_validation_commands(raw.get("lint_commands")),
            typecheck_commands=_validation_commands(raw.get("typecheck_commands")),
            build_commands=_validation_commands(raw.get("build_commands")),
            command_timeout_seconds=command_timeout_seconds,
            required_checks=_tuple(raw.get("required_checks")),
            risk_ceiling=str(raw.get("risk_ceiling", "medium")),
            max_autonomous_action=max_action,
            executor_enabled=executor_enabled,
            synthetic_fixture=synthetic_fixture,
            implementation_command=command,
            github_repo=github_repo,
            deploy_command=raw.get("deploy_command"),
            rollback_command=raw.get("rollback_command"),
            health_checks=_tuple(raw.get("health_checks")),
            live_gateway_imports=bool(raw.get("live_gateway_imports", False)),
            owners=_tuple(raw.get("owners")),
            notify_route=str(raw.get("notify_route", "")),
        )
    return Allowlist(version=str(data.get("version", "0")), targets=targets)


def resolve_target(allowlist: Allowlist, repo: str) -> Optional[TargetConfig]:
    if not repo:
        return None
    return allowlist.targets.get(repo)


def _glob_matches(rel_path: str, pattern: str) -> bool:
    """Match a POSIX relative path, including the root form of ``**/name``."""
    return fnmatch.fnmatch(rel_path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatch(rel_path, pattern[3:])
    )


def path_allowed(target: TargetConfig, rel_path: str) -> bool:
    """True iff rel_path matches >=1 allow glob and no deny glob."""
    rel = rel_path.replace("\\", "/").strip("/")
    allowed = any(_glob_matches(rel, glob) for glob in target.allowed_globs)
    denied = any(_glob_matches(rel, glob) for glob in target.denied_globs)
    return allowed and not denied
