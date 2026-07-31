"""Execution write-scope policy and Docker capability validation.

The policy is provenance based. It never inspects shell or Python source, and
it does not select a different execution backend for the caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


LEGACY_SCOPE = "legacy"
WORKSPACE_SCOPE = "workspace"
SUPPORTED_SCOPES = frozenset({LEGACY_SCOPE, WORKSPACE_SCOPE})
DOCKER_BACKEND = "docker"

_workspace_roots: dict[str, str] = {}
_workspace_roots_lock = threading.Lock()
_policy_environment_keys: dict[str, tuple[str, ...]] = {}
_policy_environment_keys_lock = threading.Lock()
_MAX_ENVIRONMENT_KEY_LENGTH = 80


class ExecutionWriteScopeError(RuntimeError):
    """Named refusal raised before an unsupported execution can be created."""

    def __init__(self, result: "ExecutionCapability") -> None:
        self.result = result
        super().__init__(result.message)


@dataclass(frozen=True)
class DockerMapping:
    """A normalized Docker bind or volume mapping."""

    source: str
    target: str
    read_only: bool = False

    @property
    def spec(self) -> str:
        mode = ":ro" if self.read_only else ""
        return f"{self.source}:{self.target}{mode}"


@dataclass(frozen=True)
class ExecutionCapability:
    """Typed capability decision for a requested execution policy."""

    status: str
    code: str
    backend: str
    message: str
    mappings: tuple[DockerMapping, ...] = ()
    extra_args: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.status == "supported"

    def as_error(self) -> dict[str, str]:
        return {
            "error_code": self.code,
            "error": self.message,
            "backend": self.backend,
        }


@dataclass(frozen=True)
class ExecutionWritePolicy:
    """Immutable, session-scoped authority for host write exposure."""

    scope: str
    session_id: str
    workspace_root: str
    backend: str
    fingerprint: str
    capability: ExecutionCapability

    @property
    def is_workspace_scoped(self) -> bool:
        return self.scope == WORKSPACE_SCOPE

    @property
    def docker_mappings(self) -> tuple[DockerMapping, ...]:
        return self.capability.mappings

    @property
    def docker_extra_args(self) -> tuple[str, ...]:
        return self.capability.extra_args


def _canonical_path(value: Any, *, base: str | None = None) -> str:
    raw = os.path.expandvars(os.path.expanduser(str(value or ""))).strip()
    if not raw:
        return ""
    if not os.path.isabs(raw):
        raw = os.path.join(base or os.getcwd(), raw)
    return os.path.realpath(os.path.abspath(raw))


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([path, root]) == root
    except ValueError:
        return False


def _parse_volume(value: Any) -> tuple[str, str, bool] | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    parts = value.split(":")
    if len(parts) >= 3 and parts[-1].lower() in {"ro", "rw", "readonly"}:
        source = ":".join(parts[:-2])
        target = parts[-2]
        return source, target, parts[-1].lower() in {"ro", "readonly"}
    if len(parts) >= 2:
        source = ":".join(parts[:-1])
        target = parts[-1]
        return source, target, False
    return None


def _normalize_mapping(
    value: Any,
    workspace_root: str,
    *,
    allow_named_read_only: bool = True,
) -> DockerMapping | None:
    parsed = _parse_volume(value)
    if parsed is None:
        return None
    source, target, read_only = parsed
    if not target.startswith("/") or target == "/":
        return None
    source = source.strip()
    if not source:
        return None

    looks_like_path = (
        source.startswith(("/", "~", "./", "../"))
        or (len(source) >= 3 and source[1] == ":" and source[2] in "/\\")
    )
    if not looks_like_path:
        if read_only and allow_named_read_only:
            return DockerMapping(source, target, True)
        return None

    normalized_source = _canonical_path(source, base=workspace_root)
    if not read_only and not _inside(normalized_source, workspace_root):
        raise ValueError(
            f"writable Docker mapping {source!r} resolves outside session workspace "
            f"{workspace_root!r}"
        )
    return DockerMapping(normalized_source, target, read_only)


_UNSAFE_EXTRA_FLAGS = {
    "--privileged",
    "--pid",
    "--ipc",
    "--uts",
    "--userns",
    "--device",
    "--mount",
    "--volume",
    "--volumes-from",
    "-v",
}
_SAFE_EXTRA_FLAGS = frozenset({"--init", "--rm"})


def _normalize_extra_args(
    values: Any,
    workspace_root: str,
) -> tuple[tuple[str, ...], tuple[DockerMapping, ...], str | None]:
    if values is None:
        return (), (), None
    if not isinstance(values, (list, tuple)) or not all(isinstance(v, str) for v in values):
        return (), (), "docker_extra_args must be a list of strings"

    args = list(values)
    normalized: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i].strip()
        flag = arg.split("=", 1)[0].lower()
        if not arg:
            return (), (), "unparseable empty Docker extra argument"
        if flag in _UNSAFE_EXTRA_FLAGS or flag.startswith("--volumes-from"):
            return (), (), f"unsupported Docker host or namespace argument {arg!r}"
        if flag in {"--network", "--net"}:
            value = arg.split("=", 1)[1] if "=" in arg else (
                args[i + 1].strip() if i + 1 < len(args) else ""
            )
            if value.lower() != "none":
                return (), (), f"unsupported Docker network argument {arg!r}"
            normalized.append(arg)
            if "=" not in arg:
                if i + 1 >= len(args):
                    return (), (), f"unparseable Docker network argument {arg!r}"
                i += 1
                normalized.append(args[i])
        elif arg.lower() in _SAFE_EXTRA_FLAGS:
            normalized.append(arg)
        else:
            return (), (), f"unparseable or unsupported Docker extra argument {arg!r}"
        i += 1
    return tuple(normalized), (), None


def normalize_docker_mappings(
    workspace_root: str,
    volumes: Iterable[Any] = (),
    *,
    host_cwd: str | None = None,
    auto_mount_cwd: bool = False,
    extra_args: Any = (),
) -> ExecutionCapability:
    """Normalize and validate every writable host exposure for Docker."""
    mappings: list[DockerMapping] = []
    for value in volumes or ():
        try:
            mapping = _normalize_mapping(value, workspace_root)
        except ValueError as exc:
            return ExecutionCapability(
                "invalid", "outside_workspace_mapping", DOCKER_BACKEND, str(exc)
            )
        if mapping is None:
            return ExecutionCapability(
                "invalid", "invalid_docker_mapping", DOCKER_BACKEND,
                f"unparseable or unsupported Docker volume mapping {value!r}",
            )
        mappings.append(mapping)

    if auto_mount_cwd:
        if not host_cwd:
            return ExecutionCapability(
                "invalid", "invalid_docker_mapping", DOCKER_BACKEND,
                "Docker cwd mapping is enabled but has no host source",
            )
        try:
            mapping = _normalize_mapping(
                f"{host_cwd}:/workspace:rw", workspace_root, allow_named_read_only=False
            )
        except ValueError as exc:
            return ExecutionCapability("invalid", "outside_workspace_mapping", DOCKER_BACKEND, str(exc))
        if mapping is None:
            return ExecutionCapability(
                "invalid", "invalid_docker_mapping", DOCKER_BACKEND,
                f"unparseable Docker cwd mapping {host_cwd!r}",
            )
        mappings.insert(0, mapping)

    normalized_args, extra_mappings, error = _normalize_extra_args(extra_args, workspace_root)
    if error:
        return ExecutionCapability("invalid", "invalid_docker_extra_args", DOCKER_BACKEND, error)
    mappings.extend(extra_mappings)
    return ExecutionCapability(
        "supported", "supported", DOCKER_BACKEND,
        "Docker can enforce the workspace write scope",
        tuple(mappings), normalized_args,
    )


def _stable_workspace_root(session_id: str, candidate: str) -> str:
    normalized = _canonical_path(candidate)
    with _workspace_roots_lock:
        return _workspace_roots.setdefault(session_id, normalized)


def clear_execution_workspace(session_id: str | None) -> None:
    with _workspace_roots_lock:
        _workspace_roots.pop(str(session_id or "default"), None)
    with _policy_environment_keys_lock:
        _policy_environment_keys.pop(str(session_id or "default"), None)


def resolve_execution_write_policy(
    scope: str | None = LEGACY_SCOPE,
    *,
    session_id: str | None = "default",
    workspace_root: str | None = None,
    backend: str = "local",
    docker_volumes: Iterable[Any] = (),
    docker_extra_args: Any = (),
    host_cwd: str | None = None,
    docker_mount_cwd_to_workspace: bool = False,
) -> ExecutionWritePolicy:
    """Resolve one immutable policy for a session and selected backend."""
    normalized_scope = str(scope or LEGACY_SCOPE).strip().lower()
    session = str(session_id or "default")
    backend = str(backend or "local").strip().lower()
    root = _stable_workspace_root(session, workspace_root or os.getcwd())

    if normalized_scope not in SUPPORTED_SCOPES:
        capability = ExecutionCapability(
            "invalid", "invalid_execution_write_scope", backend,
            f"unsupported execution_write_scope {scope!r}; expected 'legacy' or 'workspace'",
        )
    elif normalized_scope == LEGACY_SCOPE:
        capability = ExecutionCapability("supported", "legacy", backend, "legacy execution write scope")
    elif backend != DOCKER_BACKEND:
        capability = ExecutionCapability(
            "unsupported", "unsupported_execution_backend", backend,
            f"execution_write_scope=workspace is unsupported for backend {backend!r}; "
            "select Docker or use execution_write_scope=legacy",
        )
    else:
        capability = normalize_docker_mappings(
            root,
            docker_volumes,
            host_cwd=host_cwd,
            auto_mount_cwd=docker_mount_cwd_to_workspace,
            extra_args=docker_extra_args,
        )

    identity = {
        "scope": normalized_scope,
        "session_id": session,
        "workspace_root": root,
        "backend": backend,
        "mappings": [m.spec for m in capability.mappings],
        "extra_args": list(capability.extra_args),
        "code": capability.code,
    }
    fingerprint = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return ExecutionWritePolicy(
        normalized_scope, session, root, backend, fingerprint, capability
    )


def validate_execution_capability(
    policy: ExecutionWritePolicy,
    backend: str | None = None,
) -> ExecutionCapability:
    """Return the named capability result without changing the backend."""
    if not policy.is_workspace_scoped:
        return policy.capability
    if backend and backend != policy.backend:
        return ExecutionCapability(
            "unsupported", "unsupported_execution_backend", backend,
            f"execution_write_scope=workspace is unsupported for backend {backend!r}",
        )
    return policy.capability


def policy_environment_key(task_id: str | None, policy: ExecutionWritePolicy) -> str:
    """Return one bounded, filesystem-safe key for the selected policy."""
    base = str(task_id or "default")
    if not policy.is_workspace_scoped:
        return base
    digest = hashlib.sha256(
        f"{base}\0{policy.session_id}\0{policy.fingerprint}".encode()
    ).hexdigest()
    return f"hermes-workspace-{digest}"[:_MAX_ENVIRONMENT_KEY_LENGTH]


def bind_policy_environment_key(
    session_id: str | None, task_id: str | None, policy: ExecutionWritePolicy
) -> str:
    """Bind raw session provenance to the bounded backend cache identity."""
    raw_session = str(session_id or "default")
    key = policy_environment_key(task_id, policy)
    if not policy.is_workspace_scoped:
        return key
    with _policy_environment_keys_lock:
        keys = list(_policy_environment_keys.get(raw_session, ()))
        if key not in keys:
            keys.append(key)
        _policy_environment_keys[raw_session] = tuple(keys)
    return key


def lookup_policy_environment_key(session_id: str | None) -> str | None:
    """Return a previously bound key without falling back across identities."""
    with _policy_environment_keys_lock:
        keys = _policy_environment_keys.get(str(session_id or "default"), ())
        return keys[-1] if keys else None


def policy_environment_keys_for_session(session_id: str | None) -> tuple[str, ...]:
    """Return every bounded identity recorded for one raw session."""
    with _policy_environment_keys_lock:
        return _policy_environment_keys.get(str(session_id or "default"), ())


def forget_policy_environment_key(identifier: str | None) -> tuple[str, ...]:
    """Remove a raw-session binding or a bounded backend identity."""
    if not identifier:
        return ()
    identifier = str(identifier)
    removed: list[str] = []
    with _policy_environment_keys_lock:
        session_keys = _policy_environment_keys.pop(identifier, None)
        if session_keys:
            removed.extend(session_keys)
        for session_id, keys in list(_policy_environment_keys.items()):
            if identifier not in keys:
                continue
            remaining = tuple(key for key in keys if key != identifier)
            removed.append(identifier)
            if remaining:
                _policy_environment_keys[session_id] = remaining
            else:
                _policy_environment_keys.pop(session_id, None)
    return tuple(dict.fromkeys(removed))


__all__ = [
    "DOCKER_BACKEND",
    "DockerMapping",
    "ExecutionCapability",
    "ExecutionWritePolicy",
    "ExecutionWriteScopeError",
    "LEGACY_SCOPE",
    "WORKSPACE_SCOPE",
    "clear_execution_workspace",
    "bind_policy_environment_key",
    "forget_policy_environment_key",
    "lookup_policy_environment_key",
    "policy_environment_keys_for_session",
    "normalize_docker_mappings",
    "policy_environment_key",
    "resolve_execution_write_policy",
    "validate_execution_capability",
]
