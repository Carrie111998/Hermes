"""Verify lean sidebar registration and normal-runtime handoff.

The default invocation is read-only.  The mutating probe requires both
``--apply`` and the exact confirmation phrase in ``CONFIRMATION``.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any
import uuid

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.transports.codex_app_server import CodexAppServerClient
from session_bridge.characterize import resolve_cli_executable
from session_bridge.mcp_server import resolve_marker_key
from session_bridge.models import (
    BridgeMarkerPayload,
    Provider,
    encode_bridge_marker,
)
from session_bridge.sidebar import (
    SidebarCandidate,
    build_registration_prompt,
    sidebar_bridge_id,
    sidebar_create_recovery_key,
)
from session_bridge.sidebar_executor import CodexAppServerSidebarDelivery
from session_bridge.sidebar_runtime import (
    configured_mcp_server_names,
    sidebar_registration_app_server_args,
)


CONFIRMATION = "CREATE_ONE_DISPOSABLE_SIDEBAR_RUNTIME_PROBE"
_TIMEOUT_SECONDS = 180.0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Verify that sidebar registration uses the lean runtime while a "
            "freshly resumed task restores the normal Codex runtime."
        )
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="create and archive one disposable native Codex task",
    )
    parser.add_argument(
        "--confirm",
        help=f"required with --apply: {CONFIRMATION}",
    )
    return parser


def _validate_mutation_gate(args: argparse.Namespace) -> None:
    apply = args.apply is True
    confirmation = args.confirm
    if not apply and confirmation is None:
        return
    if apply and confirmation == CONFIRMATION:
        return
    raise ValueError(
        "mutation confirmation is invalid; use --apply --confirm "
        f"{CONFIRMATION}"
    )


def _mcp_names(response: object) -> tuple[str, ...]:
    if not isinstance(response, Mapping):
        raise ValueError("MCP status response must be an object")
    data = response.get("data")
    if not isinstance(data, list):
        raise ValueError("MCP status response must contain a data list")
    names: set[str] = set()
    for entry in data:
        if not isinstance(entry, Mapping):
            raise ValueError("MCP status entry must be an object")
        name = entry.get("name")
        if (
            not isinstance(name, str)
            or not name.strip()
            or name != name.strip()
        ):
            raise ValueError("MCP status entry name is malformed")
        names.add(name)
    return tuple(sorted(names))


def _exposed_mcp_names(response: object) -> tuple[str, ...]:
    names = set(_mcp_names(response))
    if not isinstance(response, Mapping):
        raise ValueError("MCP status response must be an object")
    data = response.get("data")
    if not isinstance(data, list):
        raise ValueError("MCP status response must contain a data list")
    exposed: set[str] = set()
    for entry in data:
        if not isinstance(entry, Mapping):
            raise ValueError("MCP status entry must be an object")
        tools = entry.get("tools")
        if not isinstance(tools, Mapping):
            raise ValueError("MCP status entry tools are malformed")
        if tools:
            name = entry.get("name")
            if name not in names:
                raise ValueError("MCP status entry name is malformed")
            exposed.add(name)
    return tuple(sorted(exposed))


def _config_digest(response: object) -> str:
    if not isinstance(response, Mapping):
        raise ValueError("config/read response must be an object")
    config = response.get("config")
    if not isinstance(config, Mapping):
        raise ValueError("config/read response must contain a config object")
    try:
        canonical = json.dumps(
            config,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("config/read response is not canonical JSON") from exc
    return hashlib.sha256(canonical).hexdigest()


def _contains_exact_identity(value: object, marker: str) -> bool:
    if isinstance(value, str):
        if value == marker:
            return True
        return any(line == f"Signed marker: {marker}" for line in value.splitlines())
    if isinstance(value, Mapping):
        return any(
            _contains_exact_identity(child, marker) for child in value.values()
        )
    if isinstance(value, list):
        return any(_contains_exact_identity(child, marker) for child in value)
    return False


def _archive_verified_probe(
    client: Any,
    *,
    response: object,
    thread_id: str,
    marker: str,
    timeout: float,
) -> None:
    if not isinstance(response, Mapping):
        raise ValueError("probe identity response is malformed")
    thread = response.get("thread")
    if (
        not isinstance(thread, Mapping)
        or thread.get("id") != thread_id
        or not _contains_exact_identity(thread, marker)
    ):
        raise ValueError("probe identity could not be proven exactly")
    client.request(
        "thread/archive",
        {"threadId": thread_id},
        timeout=timeout,
    )


def _list_all_mcp_names(
    client: CodexAppServerClient,
    *,
    timeout: float,
    thread_id: str | None = None,
    exposed_only: bool = False,
) -> tuple[str, ...]:
    names: set[str] = set()
    cursor: str | None = None
    while True:
        params: dict[str, object] = {
            "detail": "toolsAndAuthOnly",
            "limit": 100,
        }
        if thread_id is not None:
            params["threadId"] = thread_id
        if cursor is not None:
            params["cursor"] = cursor
        response = client.request(
            "mcpServerStatus/list",
            params,
            timeout=timeout,
        )
        names.update(
            _exposed_mcp_names(response) if exposed_only else _mcp_names(response)
        )
        next_cursor = response.get("nextCursor")
        if next_cursor is None:
            return tuple(sorted(names))
        if not isinstance(next_cursor, str) or not next_cursor:
            raise ValueError("MCP status next cursor is malformed")
        cursor = next_cursor


def _normal_client(codex_bin: str) -> CodexAppServerClient:
    return CodexAppServerClient(codex_bin=codex_bin)


def _initialize(client: CodexAppServerClient) -> None:
    client.initialize(
        capabilities={"experimentalApi": True},
        timeout=30.0,
    )


def _safe_thread_tag(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode("utf-8")).hexdigest()[:12]


def _read_normal_baseline(
    codex_bin: str,
    *,
    cwd: Path,
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    client = _normal_client(codex_bin)
    try:
        _initialize(client)
        config = client.request(
            "config/read",
            {"cwd": str(cwd), "includeLayers": False},
            timeout=30.0,
        )
        names = _list_all_mcp_names(client, timeout=30.0)
        return (
            _config_digest(config),
            names,
            configured_mcp_server_names(config),
        )
    finally:
        client.close()


def _run_probe() -> dict[str, object]:
    executable = resolve_cli_executable("codex")
    if len(executable) != 1:
        raise RuntimeError("Codex direct runtime is required")
    codex_bin = executable[0]
    cwd = Path.cwd().resolve()
    marker_secret = resolve_marker_key()
    (
        normal_digest,
        normal_mcp_names,
        configured_mcp_names,
    ) = _read_normal_baseline(codex_bin, cwd=cwd)

    nonce = uuid.uuid4().hex
    source_session_id = f"sidebar-runtime-probe-{nonce}"
    candidate = SidebarCandidate(
        source_session_id=source_session_id,
        provider=Provider.HERMES,
        bridge_id=sidebar_bridge_id(source_session_id),
        title=f"[Hermes] Sidebar runtime probe {nonce[:8]}",
        cwd=str(cwd),
        git_root=None,
        git_branch=None,
        git_head=None,
        worktree_id=None,
        eligible_at=time.time(),
    )
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=candidate.bridge_id,
            source_session_id=candidate.source_session_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        ),
        marker_secret,
    )
    prompt = build_registration_prompt(candidate, marker)
    recovery_key = sidebar_create_recovery_key(marker, marker_secret)

    lean_client = CodexAppServerClient(
        codex_bin=codex_bin,
        extra_args=sidebar_registration_app_server_args(configured_mcp_names),
    )
    thread_id: str | None = None
    started = time.monotonic()
    try:
        _initialize(lean_client)
        lean_mcp_names = _list_all_mcp_names(
            lean_client,
            timeout=30.0,
            exposed_only=True,
        )
        if lean_mcp_names:
            raise RuntimeError("lean registration runtime exposed MCP servers")
        delivery = CodexAppServerSidebarDelivery(
            lean_client,
            fresh_client_factory=lambda: _normal_client(codex_bin),
        )
        deadline = time.monotonic() + _TIMEOUT_SECONDS
        delivery.preflight(deadline=deadline)
        thread_id = delivery.create_thread(
            prompt=prompt,
            candidate=candidate,
            recovery_key=recovery_key,
            deadline=deadline,
        )
        delivery.register_thread(
            thread_id=thread_id,
            prompt=prompt,
            deadline=deadline,
            fresh=True,
        )
        delivery.rename_thread(
            thread_id=thread_id,
            title=candidate.title,
            deadline=deadline,
        )
        registration_ms = (time.monotonic() - started) * 1000.0
    finally:
        lean_client.close()

    if thread_id is None:
        raise RuntimeError("probe did not return an exact native task identity")

    normal_client = _normal_client(codex_bin)
    try:
        _initialize(normal_client)
        resumed = normal_client.request(
            "thread/resume",
            {"threadId": thread_id},
            timeout=30.0,
        )
        resumed_config = normal_client.request(
            "config/read",
            {"cwd": str(cwd), "includeLayers": False},
            timeout=30.0,
        )
        resumed_mcp_names = _list_all_mcp_names(
            normal_client,
            timeout=30.0,
            thread_id=thread_id,
        )
        if _config_digest(resumed_config) != normal_digest:
            raise RuntimeError("normal runtime config did not survive handoff")
        if resumed_mcp_names != normal_mcp_names:
            raise RuntimeError("normal runtime MCP capabilities did not survive handoff")
        _archive_verified_probe(
            normal_client,
            response=resumed,
            thread_id=thread_id,
            marker=marker,
            timeout=30.0,
        )
    except Exception as exc:
        tag = _safe_thread_tag(thread_id)
        raise RuntimeError(
            f"probe {tag} was not archived because exact verification failed"
        ) from exc
    finally:
        normal_client.close()

    return {
        "archived": True,
        "lean_mcp_count": 0,
        "normal_mcp_count": len(normal_mcp_names),
        "normal_runtime_restored": True,
        "registration_ms": round(registration_ms, 1),
        "thread_tag": _safe_thread_tag(thread_id),
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _validate_mutation_gate(args)
    if not args.apply:
        print(json.dumps({
            "apply": False,
            "confirmation_required": CONFIRMATION,
            "probe": "one disposable native Codex task, archived after exact proof",
            "registration_runtime": (
                "disable apps, plugins, and every MCP name discovered through "
                "config/read"
            ),
            "verification": [
                "lean runtime has no MCP servers",
                "fresh normal resume preserves config digest",
                "fresh normal resume restores configured MCP server names",
            ],
        }, indent=2, sort_keys=True))
        return 0
    print(json.dumps(_run_probe(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
