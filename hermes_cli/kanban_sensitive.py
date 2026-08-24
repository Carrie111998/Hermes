"""Fail-closed security helpers for dispatcher-owned sensitive Kanban tasks."""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from agent.redact import redact_sensitive_text
from agent.secret_scope import (
    build_profile_secret_scope,
    current_secret_scope,
    set_secret_scope,
)
from hermes_constants import get_hermes_home

_MIN_SECRET_LENGTH = 8
_SECRET_NAME_RE = re.compile(
    r"(?:api_?key|token|secret|password|passwd|credential|private_?key|authorization|auth)",
    re.IGNORECASE,
)
_REDACTED_SENTINELS = ("«redacted", "[REDACTED", "***")
_BLOCK_MESSAGE = "Sensitive execution blocked a tool call containing credential material"
_FAIL_CLOSED_MESSAGE = "Sensitive execution policy failed closed"
_TERMINAL_BLOCK_MESSAGE = (
    "Sensitive execution permits only the fixed no-argument runner through terminal"
)
_UNMEDIATED_EXECUTION_BLOCK_MESSAGE = (
    "Sensitive execution blocked an unmediated execution capability"
)
_UNMEDIATED_EXECUTION_TOOLS = frozenset({
    "browser_exec",
    "computer_use",
    "cronjob",
    "delegate_task",
    "execute_code",
    "kanban_create",
    "tool_call",
})

# A sensitive model worker starts with process/runtime coordinates only. Profile
# credentials remain in the profile's protected stores and are resolved inside
# Hermes; they never need to be ambient child-process variables.
_SENSITIVE_WORKER_RUNTIME_ENV = frozenset({
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "TEMP",
    "TMP",
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
    "PROGRAMDATA",
    "PYTHONUTF8",
    "PYTHONIOENCODING",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
})
_SENSITIVE_WORKER_CONTEXT_ENV = frozenset({
    "HERMES_HOME",
    "HERMES_PROFILE",
    "HERMES_TENANT",
    "HERMES_SESSION_SOURCE",
    "HERMES_KANBAN_TASK",
    "HERMES_KANBAN_WORKSPACE",
    "HERMES_KANBAN_BRANCH",
    "HERMES_KANBAN_RUN_ID",
    "HERMES_KANBAN_CLAIM_LOCK",
    "HERMES_KANBAN_SENSITIVE",
    "HERMES_KANBAN_GOAL_MODE",
    "HERMES_KANBAN_GOAL_MAX_TURNS",
    "HERMES_KANBAN_DB",
    "HERMES_KANBAN_WORKSPACES_ROOT",
    "HERMES_KANBAN_BOARD",
    "TERMINAL_CWD",
    "TERMINAL_TIMEOUT",
    "TERMINAL_MAX_FOREGROUND_TIMEOUT",
})


def sensitive_mode_enabled() -> bool:
    return os.environ.get("HERMES_KANBAN_SENSITIVE") == "1"


def build_sensitive_worker_env(source: Mapping[str, Any]) -> dict[str, str]:
    """Return the deny-by-default environment for a sensitive model worker."""
    allowed = _SENSITIVE_WORKER_RUNTIME_ENV | _SENSITIVE_WORKER_CONTEXT_ENV
    return {
        key: str(value)
        for key, value in source.items()
        if key.upper() in allowed and value is not None
    }


def build_sensitive_runner_env(
    granted: Mapping[str, str], source: Mapping[str, Any]
) -> dict[str, str]:
    """Return fixed-runner resources plus Windows process essentials only."""
    windows_runtime = {"SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT"}
    child_env = {
        key: str(value)
        for key, value in source.items()
        if key.upper() in windows_runtime and value is not None
    }
    child_env["HERMES_KANBAN_SENSITIVE_RESOURCES"] = json.dumps(
        dict(granted), sort_keys=True, separators=(",", ":")
    )
    return child_env


def activate_sensitive_worker_credentials(hermes_home: Path) -> Any:
    """Install profile credentials in process-private scope, never environ."""
    home = Path(hermes_home)
    try:
        from hermes_cli.env_loader import hydrate_profile_secret_sources

        hydrate_profile_secret_sources(home)
    except Exception:
        # The ordinary secret-source path is fail-open too. A missing model
        # credential will fail provider resolution without widening ambient env.
        pass
    return set_secret_scope(build_profile_secret_scope(home))


def _usable_secret(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    if (
        len(value) < _MIN_SECRET_LENGTH
        or not value.strip()
        or value.lstrip().startswith(_REDACTED_SENTINELS)
    ):
        return None
    return value


def _walk_secret_fields(value: Any, *, credential_store: bool = False) -> Iterable[str]:
    if isinstance(value, Mapping):
        for key, child in value.items():
            named_secret = credential_store or bool(_SECRET_NAME_RE.search(str(key)))
            if isinstance(child, str) and named_secret:
                secret = _usable_secret(child)
                if secret:
                    yield secret
            elif isinstance(child, (Mapping, list, tuple)):
                yield from _walk_secret_fields(child, credential_store=credential_store)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_secret_fields(child, credential_store=credential_store)


def active_secret_values() -> tuple[str, ...]:
    """Return exact active-profile credential values without logging them."""
    values: set[str] = set()
    scope = current_secret_scope()
    if scope is None:
        scope = build_profile_secret_scope(get_hermes_home())
    for raw in scope.values():
        secret = _usable_secret(raw)
        if secret:
            values.add(secret)

    for key, raw in os.environ.items():
        if _SECRET_NAME_RE.search(key):
            secret = _usable_secret(raw)
            if secret:
                values.add(secret)

    try:
        from hermes_cli.config import load_config

        values.update(_walk_secret_fields(load_config()))
    except Exception:
        if sensitive_mode_enabled():
            raise

    try:
        from hermes_cli.auth import _load_auth_store

        values.update(_walk_secret_fields(_load_auth_store()))
    except Exception:
        if sensitive_mode_enabled():
            raise

    return tuple(sorted(values, key=len, reverse=True))


def redact_exact_secrets(text: str, secrets: Optional[Iterable[str]] = None) -> str:
    result = str(text)
    for secret in secrets if secrets is not None else active_secret_values():
        if secret:
            result = result.replace(secret, "«redacted-secret»")
    return redact_sensitive_text(result, force=True, redact_url_credentials=True)


def validate_final_tool_args(*, tool_name: str, args: Mapping[str, Any], **_context: Any) -> Optional[str]:
    serialized = json.dumps(args, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    if any(secret in serialized for secret in active_secret_values()):
        return _BLOCK_MESSAGE
    if tool_name in _UNMEDIATED_EXECUTION_TOOLS:
        return _UNMEDIATED_EXECUTION_BLOCK_MESSAGE
    if tool_name == "process":
        return _TERMINAL_BLOCK_MESSAGE
    if tool_name == "terminal":
        if set(args) - {"command"}:
            return _TERMINAL_BLOCK_MESSAGE
        command = args.get("command")
        if not isinstance(command, str):
            return _TERMINAL_BLOCK_MESSAGE
        try:
            tokens = shlex.split(command, posix=True)
        except ValueError:
            return _TERMINAL_BLOCK_MESSAGE
        if tokens != ["hermes", "kanban", "sensitive-run"]:
            return _TERMINAL_BLOCK_MESSAGE
    return None


def assert_sensitive_worker_context() -> None:
    """Prove this process belongs to the active sensitive task/run."""
    if not sensitive_mode_enabled():
        raise RuntimeError(_FAIL_CLOSED_MESSAGE)
    task_id = os.environ.get("HERMES_KANBAN_TASK", "")
    run_id = os.environ.get("HERMES_KANBAN_RUN_ID", "")
    claim_lock = os.environ.get("HERMES_KANBAN_CLAIM_LOCK", "")
    if not task_id or not run_id or not claim_lock:
        raise RuntimeError(_FAIL_CLOSED_MESSAGE)

    from hermes_cli import kanban_db as kb

    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
        if (
            task is None
            or not task.sensitive_execution
            or task.status != "running"
            or task.current_run_id is None
            or str(task.current_run_id) != run_id
            or task.claim_lock != claim_lock
            or task.assignee != (os.environ.get("HERMES_PROFILE") or task.assignee)
        ):
            raise RuntimeError(_FAIL_CLOSED_MESSAGE)


def scan_sensitive_artifact_bytes(data: bytes) -> None:
    secrets = active_secret_values()
    for secret in secrets:
        if secret.encode("utf-8") in data:
            raise ValueError("sensitive artifact contains credential material")
    if any(byte < 0x20 and byte not in (0x09, 0x0A, 0x0D) for byte in data):
        raise ValueError("sensitive artifacts must be auditable UTF-8 text")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("sensitive artifacts must be auditable UTF-8 text") from exc
    if redact_exact_secrets(text, secrets) != text:
        raise ValueError("sensitive artifact contains credential material")


def read_and_scan_sensitive_artifact(path: Path) -> bytes:
    """Read immutable bytes once; callers must persist these exact bytes."""
    data = Path(path).read_bytes()
    scan_sensitive_artifact_bytes(data)
    return data


def run_sensitive_runner() -> int:
    """Execute the current task's operator-fixed argv with no user arguments."""
    assert_sensitive_worker_context()
    from hermes_cli import kanban_db as kb
    from hermes_cli.config import load_config

    task_id = os.environ["HERMES_KANBAN_TASK"]
    with kb.connect_closing() as conn:
        task = kb.get_task(conn, task_id)
    if task is None or not task.sensitive_execution or not task.sensitive_runner_id:
        raise RuntimeError(_FAIL_CLOSED_MESSAGE)

    policy = (load_config().get("kanban") or {}).get("sensitive_execution") or {}
    runners = policy.get("runners") or {}
    resources = policy.get("resources") or {}
    runner = runners.get(task.sensitive_runner_id)
    argv = runner.get("argv") if isinstance(runner, Mapping) else None
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
        or not Path(argv[0]).is_absolute()
    ):
        raise RuntimeError("sensitive runner is not declared with a fixed absolute argv")

    granted: dict[str, str] = {}
    for resource_id in task.protected_resource_ids:
        raw_path = resources.get(resource_id)
        if not isinstance(raw_path, str) or not Path(raw_path).is_absolute():
            raise RuntimeError("sensitive protected resource is not declared")
        granted[resource_id] = str(Path(raw_path))

    # The runner's executable and arguments are fixed in policy, so it needs
    # no ambient process state. Pass only the task-scoped resource grant plus
    # the Windows variables required to start a subprocess at all.
    child_env = build_sensitive_runner_env(granted, os.environ)
    proc = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_env,
        check=False,
        shell=False,
    )
    secrets = active_secret_values()
    stdout = redact_exact_secrets(proc.stdout.decode("utf-8", errors="replace"), secrets)
    stderr = redact_exact_secrets(proc.stderr.decode("utf-8", errors="replace"), secrets)
    if stdout:
        sys.stdout.write(stdout)
    if stderr:
        sys.stderr.write(stderr)
    return int(proc.returncode)
