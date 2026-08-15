"""Requested and effective OCI strict-worker configuration."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

from hermes_cli.kanban_store.types import ContractError

_IMAGE_DIGEST_RE = re.compile(r"^.+@sha256:[0-9a-f]{64}$")
_FORBIDDEN_ENV_FRAGMENTS = (
    "TOKEN", "SECRET", "PASSWORD", "API_KEY", "AUTH", "CREDENTIAL", "GITHUB", "OPENAI",
)


@dataclass(frozen=True, slots=True)
class StrictOciConfig:
    runtime: str
    image: str
    container_name: str
    workspace_host: str
    context_host: str
    broker_dir_host: str
    command: tuple[str, ...]
    uid: int = 65532
    gid: int = 65532
    memory_bytes: int = 4 * 1024 * 1024 * 1024
    cpus: float = 2.0
    pids_limit: int = 512

    def __post_init__(self) -> None:
        if self.runtime not in {"docker", "podman"}:
            raise ContractError("strict runtime must be docker or podman")
        if not _IMAGE_DIGEST_RE.fullmatch(self.image):
            raise ContractError("strict worker image must be pinned by SHA-256 digest")
        if self.uid == 0 or self.gid == 0:
            raise ContractError("strict worker cannot run as root")
        if not self.command:
            raise ContractError("strict worker command is required")
        for path in (self.workspace_host, self.context_host, self.broker_dir_host):
            normalized = Path(path)
            # On Windows, Path("/tmp/work").is_absolute() returns False because a
            # bare forward-slash root is treated as relative to the current drive.
            # POSIX-style absolute paths (starting with /) are valid for OCI containers
            # running under Linux even when validated from a Windows host.
            if not normalized.is_absolute() and not str(path).startswith("/"):
                raise ContractError("strict worker host paths must be absolute")


def build_create_command(config: StrictOciConfig) -> list[str]:
    return [
        config.runtime,
        "create",
        "--name", config.container_name,
        "--network", "none",
        "--read-only",
        "--user", f"{config.uid}:{config.gid}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        "--pids-limit", str(config.pids_limit),
        "--memory", str(config.memory_bytes),
        "--cpus", str(config.cpus),
        "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=256m,mode=1777",
        "--tmpfs", "/run/hermes:rw,noexec,nosuid,nodev,size=16m,mode=0700",
        "--mount", f"type=bind,src={config.workspace_host},dst=/workspace,rw",
        "--mount", f"type=bind,src={config.context_host},dst=/run/hermes-context.json,ro",
        "--mount", f"type=bind,src={config.broker_dir_host},dst=/run/hermes-broker,rw",
        "--workdir", "/workspace",
        "--env", "HERMES_STRICT_WORKER=1",
        "--env", "HOME=/tmp/hermes-home",
        "--env", "HERMES_WORKER_CONTEXT=/run/hermes-context.json",
        config.image,
        *config.command,
    ]


def sanitized_runtime_env(base: Mapping[str, str] | None = None) -> dict[str, str]:
    source = dict(os.environ if base is None else base)
    allowed = {"PATH", "SYSTEMROOT", "WINDIR", "TMP", "TEMP", "LANG", "LC_ALL"}
    return {key: value for key, value in source.items() if key in allowed}


def validate_effective_inspect(config: StrictOciConfig, inspect: Mapping[str, object]) -> None:
    host = dict(inspect.get("HostConfig") or {})
    container = dict(inspect.get("Config") or {})
    state = dict(inspect.get("State") or {})
    if host.get("NetworkMode") != "none":
        raise ContractError("effective container network is not none")
    if host.get("ReadonlyRootfs") is not True:
        raise ContractError("effective root filesystem is writable")
    if host.get("Privileged") is True:
        raise ContractError("effective container is privileged")
    if host.get("PidMode") not in {"", None, "private"}:
        raise ContractError("effective container shares a PID namespace")
    cap_drop = set(host.get("CapDrop") or [])
    if "ALL" not in cap_drop:
        raise ContractError("effective container did not drop all capabilities")
    security = {str(item).lower() for item in host.get("SecurityOpt") or []}
    if not any("no-new-privileges" in item for item in security):
        raise ContractError("effective container lacks no-new-privileges")
    user = str(container.get("User") or "")
    if user in {"", "0", "0:0", "root"}:
        raise ContractError("effective container user is root")
    env = container.get("Env") or []
    for entry in env:
        key = str(entry).split("=", 1)[0].upper()
        if any(fragment in key for fragment in _FORBIDDEN_ENV_FRAGMENTS):
            raise ContractError(f"credential-shaped environment leaked: {key}")
    mounts = inspect.get("Mounts") or []
    allowed_destinations = {"/workspace", "/run/hermes-context.json", "/run/hermes-broker"}
    seen: set[str] = set()
    for mount in mounts:
        destination = str(mount.get("Destination"))
        if destination not in allowed_destinations:
            raise ContractError(f"unexpected host mount: {destination}")
        seen.add(destination)
        if destination == "/run/hermes-context.json" and mount.get("RW") is not False:
            raise ContractError("worker context mount is writable")
    if seen != allowed_destinations:
        raise ContractError("effective strict-worker mount set is incomplete")
    if container.get("Image") != config.image:
        raise ContractError("effective image differs from pinned request")


def create_and_verify(config: StrictOciConfig) -> str:
    env = sanitized_runtime_env()
    created = subprocess.run(
        build_create_command(config),
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        timeout=60,
    )
    container_id = created.stdout.strip()
    inspected = subprocess.run(
        [config.runtime, "inspect", container_id],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        timeout=30,
    )
    payload = json.loads(inspected.stdout)
    if not isinstance(payload, list) or len(payload) != 1:
        raise ContractError("runtime inspect returned an unexpected shape")
    validate_effective_inspect(config, payload[0])
    return container_id
