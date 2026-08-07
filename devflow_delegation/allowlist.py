"""Target allowlist — operator-owned configuration (spec: "Allowlist and
target contract"). Requests cannot inject commands or paths: commands are
configuration-owned, and every path is checked against allowed/denied globs
before any mutation (Stage 2 enforces this at the worktree boundary; Stage 1
only resolves targets).

Fail-closed: a missing or malformed allowlist raises AllowlistError, and the
emitter treats that as "no targets allowed" (declined: target_unresolved).
"""
from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple


class AllowlistError(ValueError):
    pass


@dataclass
class TargetConfig:
    repo: str
    checkout_path: str
    default_branch: str = "main"
    remote: str = "origin"
    allowed_globs: Tuple[str, ...] = ()
    denied_globs: Tuple[str, ...] = ()
    worktree_base: str = ""
    test_commands: Tuple[str, ...] = ()
    lint_commands: Tuple[str, ...] = ()
    typecheck_commands: Tuple[str, ...] = ()
    build_commands: Tuple[str, ...] = ()
    command_timeout_seconds: int = 1800
    required_checks: Tuple[str, ...] = ()
    risk_ceiling: str = "medium"
    max_autonomous_action: str = "none"
    deploy_command: Optional[str] = None
    rollback_command: Optional[str] = None
    health_checks: Tuple[str, ...] = ()
    live_gateway_imports: bool = False
    owners: Tuple[str, ...] = ()
    notify_route: str = ""


@dataclass
class Allowlist:
    version: str
    targets: Dict[str, TargetConfig] = field(default_factory=dict)


def _tuple(value) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple)):
        return tuple(str(v) for v in value)
    return (str(value),)


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
        # int() is the only raising coercion; guard it so a malformed timeout
        # fails closed as AllowlistError rather than leaking a bare ValueError
        # (the emitter's `except AllowlistError` would not catch the latter).
        try:
            command_timeout_seconds = int(raw.get("command_timeout_seconds", 1800))
        except (TypeError, ValueError) as exc:
            raise AllowlistError(
                f"allowlist target '{name}' malformed: command_timeout_seconds must be int ({exc})"
            ) from exc
        targets[str(name)] = TargetConfig(
            repo=str(raw["repo"]),
            checkout_path=str(raw["checkout_path"]),
            default_branch=str(raw.get("default_branch", "main")),
            remote=str(raw.get("remote", "origin")),
            allowed_globs=_tuple(raw.get("allowed_globs")),
            denied_globs=_tuple(raw.get("denied_globs")),
            worktree_base=str(raw.get("worktree_base", "")),
            test_commands=_tuple(raw.get("test_commands")),
            lint_commands=_tuple(raw.get("lint_commands")),
            typecheck_commands=_tuple(raw.get("typecheck_commands")),
            build_commands=_tuple(raw.get("build_commands")),
            command_timeout_seconds=command_timeout_seconds,
            required_checks=_tuple(raw.get("required_checks")),
            risk_ceiling=str(raw.get("risk_ceiling", "medium")),
            max_autonomous_action=str(raw.get("max_autonomous_action", "none")),
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


def path_allowed(target: TargetConfig, rel_path: str) -> bool:
    """True iff rel_path matches >=1 allowed glob and NO denied glob.

    POSIX-style relative paths; fnmatch's '*' matches across path separators,
    so '<dir>/**' and '**/<name>' shapes match nested paths as intended (the
    only allow/deny shapes used in Stage 1, plus single-file paths).
    """
    rel = rel_path.replace("\\", "/").strip("/")
    allowed = any(fnmatch.fnmatch(rel, g) for g in target.allowed_globs)
    denied = any(fnmatch.fnmatch(rel, g) for g in target.denied_globs)
    return allowed and not denied
