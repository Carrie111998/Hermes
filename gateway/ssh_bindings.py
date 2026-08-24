"""Profile-scoped gateway session bindings for SSH Mode."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import threading
import time
from typing import Any

from hermes_constants import get_hermes_home
from gateway.ssh_targets import (
    SshTarget,
    find_ssh_target,
    load_ssh_targets,
    validate_ssh_target_for_runtime,
)

LOCAL_BACKEND = "local"
_STORE_LOCK = threading.RLock()


@dataclass(frozen=True)
class SshBinding:
    """A gateway session binding to one Hermes-managed SSH target."""

    session_key: str
    alias: str
    cwd: str | None = None
    source: str = "user"
    created_at: float | None = None
    updated_at: float | None = None


def default_ssh_bindings_path() -> Path:
    """Return the profile-scoped binding store path."""

    return get_hermes_home() / "ssh" / "bindings.json"


def _empty_store() -> dict[str, Any]:
    return {"bindings": {}}


def _read_store(path: str | Path | None = None) -> dict[str, Any]:
    store_path = (
        Path(path).expanduser()
        if path is not None
        else default_ssh_bindings_path()
    )
    try:
        data = json.loads(store_path.read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        return _empty_store()
    if not isinstance(data, dict) or not isinstance(data.get("bindings"), dict):
        return _empty_store()
    return data


def _write_store(
    data: dict[str, Any],
    path: str | Path | None = None,
) -> None:
    store_path = (
        Path(path).expanduser()
        if path is not None
        else default_ssh_bindings_path()
    )
    store_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        store_path.parent.chmod(0o700)
    except OSError:
        pass
    text = json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    tmp_path = store_path.with_suffix(store_path.suffix + ".tmp")
    tmp_path.write_text(text, encoding="utf-8")
    try:
        tmp_path.chmod(0o600)
    except OSError:
        pass
    tmp_path.replace(store_path)
    try:
        store_path.chmod(0o600)
    except OSError:
        pass


def _coerce_binding(session_key: str, raw: Any) -> SshBinding | None:
    if not session_key or not isinstance(raw, dict):
        return None
    alias = str(raw.get("alias") or "").strip()
    if not alias:
        return None
    cwd = str(raw.get("cwd") or "").strip() or None
    source = str(raw.get("source") or "user").strip() or "user"
    created_at = raw.get("created_at")
    updated_at = raw.get("updated_at")
    return SshBinding(
        session_key=session_key,
        alias=alias,
        cwd=cwd,
        source=source,
        created_at=created_at if isinstance(created_at, (int, float)) else None,
        updated_at=updated_at if isinstance(updated_at, (int, float)) else None,
    )


def get_ssh_binding(
    session_key: str,
    *,
    path: str | Path | None = None,
) -> SshBinding | None:
    """Return the SSH binding for *session_key*, if one exists."""

    if not session_key:
        return None
    with _STORE_LOCK:
        data = _read_store(path)
        return _coerce_binding(session_key, data["bindings"].get(session_key))


def set_ssh_binding(
    session_key: str,
    *,
    alias: str,
    cwd: str | None = None,
    source: str = "user",
    path: str | Path | None = None,
) -> SshBinding:
    """Persist an explicit user-selected SSH binding."""

    clean_alias = str(alias or "").strip()
    if not session_key:
        raise ValueError("session_key is required")
    if not clean_alias or clean_alias.lower() == LOCAL_BACKEND:
        raise ValueError("alias must name a non-local SSH target")

    with _STORE_LOCK:
        data = _read_store(path)
        bindings = data["bindings"]
        existing = (
            bindings.get(session_key)
            if isinstance(bindings.get(session_key), dict)
            else {}
        )
        now = time.time()
        created_at = existing.get("created_at")
        if not isinstance(created_at, (int, float)):
            created_at = now
        record: dict[str, Any] = {
            "alias": clean_alias,
            "source": str(source or "user").strip() or "user",
            "created_at": created_at,
            "updated_at": now,
        }
        clean_cwd = str(cwd or "").strip()
        if clean_cwd:
            record["cwd"] = clean_cwd
        bindings[session_key] = record
        _write_store(data, path)
    binding = _coerce_binding(session_key, record)
    assert binding is not None
    return binding


def clear_ssh_binding(
    session_key: str,
    *,
    path: str | Path | None = None,
) -> bool:
    """Remove a session binding, returning whether one existed."""

    if not session_key:
        return False
    with _STORE_LOCK:
        data = _read_store(path)
        bindings = data["bindings"]
        existed = session_key in bindings
        if existed:
            bindings.pop(session_key, None)
            _write_store(data, path)
    return existed


def resolve_binding_target(
    session_key: str,
    *,
    targets: list[SshTarget] | None = None,
    path: str | Path | None = None,
) -> tuple[SshBinding, SshTarget] | None:
    """Resolve a stored binding against the current target registry."""

    binding = get_ssh_binding(session_key, path=path)
    if binding is None:
        return None
    target = find_ssh_target(
        targets if targets is not None else load_ssh_targets(),
        binding.alias,
    )
    if target is None:
        return None
    return binding, target


def binding_to_task_overrides(
    binding: SshBinding,
    target: SshTarget,
) -> dict[str, Any]:
    """Convert a validated binding into terminal/file/code overrides."""

    validation_error = validate_ssh_target_for_runtime(target)
    if validation_error:
        return {
            "env_type": "ssh",
            "ssh_alias": binding.alias,
            "ssh_host": "",
            "ssh_user": "",
            "ssh_port": target.port or 22,
            "ssh_key": "",
            "ssh_persistent": True,
            "ssh_binding_error": validation_error,
        }

    overrides: dict[str, Any] = {
        "env_type": "ssh",
        "ssh_alias": binding.alias,
        "ssh_host": target.host,
        "ssh_user": target.user,
        "ssh_port": target.port or 22,
        "ssh_key": target.identity_file or "",
        "ssh_persistent": True,
    }
    cwd = binding.cwd or target.cwd
    if cwd:
        overrides["cwd"] = cwd
    return overrides


def resolve_binding_task_overrides(
    session_key: str,
    *,
    targets: list[SshTarget] | None = None,
    path: str | Path | None = None,
) -> dict[str, Any]:
    """Return task overrides for a session binding, or local defaults.

    A binding whose target was removed from the registry remains an SSH
    override with empty connection fields.  That deliberately makes backend
    construction fail instead of silently falling back to local execution.
    """

    binding = get_ssh_binding(session_key, path=path)
    if binding is None:
        return {}
    target = find_ssh_target(
        targets if targets is not None else load_ssh_targets(),
        binding.alias,
    )
    if target is None:
        return {
            "env_type": "ssh",
            "ssh_alias": binding.alias,
            "ssh_host": "",
            "ssh_user": "",
            "ssh_port": 22,
            "ssh_key": "",
            "ssh_persistent": True,
            "ssh_binding_error": (
                f"SSH target `{binding.alias}` is no longer configured"
            ),
        }
    return binding_to_task_overrides(binding, target)
