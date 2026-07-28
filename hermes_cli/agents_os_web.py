"""Local-only Agents OS Mission Control web surface.

This module intentionally serves only loopback HTTP and exposes read-only/operator
planning payloads by default. Draft/action endpoints create local runtime records
or approval drafts; they do not execute outbound/public/security/financial work.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import mimetypes
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

from hermes_cli.agents_os import (
    AgentsOSPaths,
    AgentsOSService,
    connect,
    log_event,
    resolve_paths,
    row_to_dict,
    slugify,
    utc_now,
)
from hermes_cli.agents_os_idea_factory import draft_idea, idea_factory_schema
from hermes_cli.agents_os_executive_board import ExecutiveBoardService
from hermes_cli.agents_os_commands import (
    CommandConflict,
    confirm_command,
    create_command,
    get_command,
)
from hermes_cli.agents_os_orchestrator import (
    ExecutionCoordinator,
    execution_projection,
    resolve_allowed_cwds,
)
from hermes_cli.agents_os_seo import seo_mission_control_payload

LOCAL_HOSTS = {"127.0.0.1", "localhost"}
_COORDINATOR_LOCK = threading.RLock()
_COORDINATORS: dict[str, ExecutionCoordinator] = {}
_PAYLOAD_CACHE_LOCK = threading.RLock()
_PAYLOAD_CACHE: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_ACTIVE_REQUESTS_LOCK = threading.RLock()
_ACTIVE_REQUESTS: dict[int, tuple[str, float]] = {}
DONI_COMPANION_ORIGIN = "http://127.0.0.1:18792"


class DoniCompanionError(RuntimeError):
    """Fail-closed error from the local canonical Doni companion gateway."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def _raise_doni_http_error(status: int, raw: bytes) -> None:
    try:
        error_payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        error_payload = {}
    error = error_payload.get("error") if isinstance(error_payload, dict) else {}
    code = error.get("code") if isinstance(error, dict) else None
    message = error.get("message") if isinstance(error, dict) else None
    raise DoniCompanionError(
        status if 400 <= status < 500 else 502,
        str(code or "companion_request_failed"),
        str(message or "Canonical Doni gateway odbio je zahtjev."),
    )


def _doni_windows_loopback_request(
    path: str,
    *,
    method: str,
    body: bytes | None,
    timeout_seconds: float,
) -> bytes:
    """Reach a Windows-loopback gateway from WSL without widening its bind."""
    argv = [
        "curl.exe", "--silent", "--show-error", "--max-time", str(max(1, int(timeout_seconds))),
        "--request", method, "--header", "accept: application/json",
        "--write-out", "\n%{http_code}", f"{DONI_COMPANION_ORIGIN}{path}",
    ]
    if body is not None:
        argv[9:9] = ["--header", "content-type: application/json", "--data-binary", "@-"]
    try:
        completed = subprocess.run(
            argv,
            input=body,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds + 5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        raise DoniCompanionError(503, "gateway_unavailable", "Windows Doni loopback bridge nije dostupan.") from exc
    if completed.returncode != 0:
        raise DoniCompanionError(503, "gateway_unavailable", "Windows Doni loopback bridge nije dostupan.")
    raw, separator, status_raw = completed.stdout.rpartition(b"\n")
    if not separator:
        raise DoniCompanionError(502, "invalid_response", "Windows Doni bridge nije vratio HTTP status.")
    try:
        status = int(status_raw.strip())
    except ValueError as exc:
        raise DoniCompanionError(502, "invalid_response", "Windows Doni bridge status nije valjan.") from exc
    if not 200 <= status < 300:
        _raise_doni_http_error(status, raw)
    return raw


def _validate_doni_identity(payload: dict[str, Any]) -> dict[str, Any]:
    if (
        payload.get("schema_version") != "1.0"
        or payload.get("assistant_identity") != "doni"
        or payload.get("memory_authority") != "canonical-doni-runtime"
        or not isinstance(payload.get("runtime_boot_id"), str)
        or not payload["runtime_boot_id"]
    ):
        raise DoniCompanionError(502, "identity_mismatch", "Odgovor nije potvrđen kao canonical Doni runtime.")
    return payload


def doni_companion_json_request(
    path: str,
    *,
    method: str = "GET",
    data: dict[str, Any] | None = None,
    timeout_seconds: float = 45.0,
) -> dict[str, Any]:
    """Call only the fixed loopback companion origin and require JSON."""
    if not path.startswith("/v1/") or ".." in path:
        raise ValueError("invalid companion path")
    body = json.dumps(data, ensure_ascii=False).encode("utf-8") if data is not None else None
    request = Request(
        f"{DONI_COMPANION_ORIGIN}{path}",
        data=body,
        method=method,
        headers={"content-type": "application/json", "accept": "application/json"},
    )
    if os.name != "nt":
        raw = _doni_windows_loopback_request(
            path, method=method, body=body, timeout_seconds=timeout_seconds
        )
    else:
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                raw = response.read(1_000_000)
        except HTTPError as exc:
            _raise_doni_http_error(exc.code, exc.read(1_000_000))
        except (URLError, TimeoutError, OSError) as exc:
            raise DoniCompanionError(503, "gateway_unavailable", "Doni companion gateway nije dostupan.") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise DoniCompanionError(502, "invalid_response", "Doni gateway nije vratio valjani JSON.") from exc
    if not isinstance(payload, dict):
        raise DoniCompanionError(502, "invalid_response", "Doni gateway odgovor nije objekt.")
    return payload


def doni_open_session_action() -> dict[str, Any]:
    payload = doni_companion_json_request(
        "/v1/companion/sessions",
        method="POST",
        data={
            "schema_version": "1.0",
            "client": "doni-live-companion",
            "profile_id": "doni",
            "user_id": "goran",
            "locale": "hr-HR",
            "context_policy": "goran_voice_v1",
        },
    )
    _validate_doni_identity(payload)
    if not isinstance(payload.get("session_id"), str) or not payload["session_id"]:
        raise DoniCompanionError(502, "invalid_response", "Doni session nema valjani session ID.")
    return payload


def doni_start_turn_action(session_id: str, data: dict[str, Any]) -> dict[str, Any]:
    text = str(data.get("text") or "").strip()
    if not text or len(text) > 12_000:
        raise ValueError("Doni poruka mora sadržavati između 1 i 12000 znakova")
    turn_id = f"turn_{uuid.uuid4().hex}"
    payload = doni_companion_json_request(
        f"/v1/companion/sessions/{quote(session_id, safe='')}/turns",
        method="POST",
        data={
            "schema_version": "1.0",
            "turn_id": turn_id,
            "idempotency_key": turn_id,
            "locale": "hr-HR",
            "input": {"type": "text", "text": text},
            "response": {"format": "text", "style": "voice_concise", "max_characters": 900},
        },
        timeout_seconds=60.0,
    )
    return _validate_doni_identity(payload)


def doni_run_payload(run_id: str) -> dict[str, Any]:
    return _validate_doni_identity(
        doni_companion_json_request(f"/v1/companion/runs/{quote(run_id, safe='')}", timeout_seconds=30.0)
    )


def doni_cancel_run_action(run_id: str) -> dict[str, Any]:
    return _validate_doni_identity(
        doni_companion_json_request(
            f"/v1/companion/runs/{quote(run_id, safe='')}/cancel", method="POST", timeout_seconds=15.0
        )
    )


def doni_end_session_action(session_id: str) -> dict[str, Any]:
    return _validate_doni_identity(
        doni_companion_json_request(
            f"/v1/companion/sessions/{quote(session_id, safe='')}", method="DELETE", timeout_seconds=15.0
        )
    )


def _payload_cache_get(paths: AgentsOSPaths, name: str, ttl_seconds: float) -> dict[str, Any] | None:
    key = (str(paths.home.resolve()), name)
    with _PAYLOAD_CACHE_LOCK:
        cached = _PAYLOAD_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < ttl_seconds:
            return cached[1]
    return None


def _payload_cache_put(paths: AgentsOSPaths, name: str, payload: dict[str, Any]) -> dict[str, Any]:
    key = (str(paths.home.resolve()), name)
    with _PAYLOAD_CACHE_LOCK:
        _PAYLOAD_CACHE[key] = (time.monotonic(), payload)
    return payload


def execution_coordinator(service: AgentsOSService) -> ExecutionCoordinator:
    key = str(service.paths.db.resolve())
    with _COORDINATOR_LOCK:
        coordinator = _COORDINATORS.get(key)
        if coordinator is None:
            coordinator = ExecutionCoordinator(service.paths, allowed_cwds=resolve_allowed_cwds(service.paths))
            _COORDINATORS[key] = coordinator
        return coordinator


def cached_dashboard_payload(service: AgentsOSService) -> dict[str, Any]:
    """Single-flight short cache for the dashboard's generated-file projection."""
    key = (str(service.paths.home.resolve()), "dashboard")
    with _PAYLOAD_CACHE_LOCK:
        cached = _PAYLOAD_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 10.0:
            return cached[1]
        payload = service.dashboard_payload()
        _PAYLOAD_CACHE[key] = (time.monotonic(), payload)
        return payload
SOURCE_DEFAULTS = {
    "video:q13OqknCh-c": "https://youtu.be/q13OqknCh-c",
    "transcript:q13OqknCh-c": "sources/transcripts/q13OqknCh-c_transcript.txt",
    "youtube-note:q13OqknCh-c": "sources/youtube/2026-06-08-q13OqknCh-c-agent-operating-system.md",
    "video:wX0YzAYywmI": "https://youtu.be/wX0YzAYywmI",
    "transcript:wX0YzAYywmI": "/mnt/d/HermesAgent/home/transcripts/wX0YzAYywmI/transcript_timestamped.txt",
    "youtube-note:wX0YzAYywmI": "/mnt/d/Obsidian_Vault_v2/Hermes-Agent-Doni/01-INBOX/YouTube/2026-07-13-wX0YzAYywmI-new-hermes-ai-voice-agent.md",
    "plan:jarvis-wx0-voice-agent": "/home/goran/.hermes-doni-clean/projects/jarvis/artifacts/2026-07-13-wx0-hermes-voice-agent-plan-goal-loop.md",
    "plan:parity-q13OqknCh-c": "sources/plans/agent-os-parity-build-plan-q13OqknCh-c.md",
    "plan:full-product": "sources/plans/agent-os-full-product-plan.md",
    "contract:idea-factory-v0": "sources/contracts/idea-factory-v0-contract.md",
}
SOURCE_ENV = {
    "transcript:q13OqknCh-c": "AGENTS_OS_SOURCE_TRANSCRIPT",
    "youtube-note:q13OqknCh-c": "AGENTS_OS_SOURCE_YOUTUBE_NOTE",
    "plan:parity-q13OqknCh-c": "AGENTS_OS_SOURCE_PARITY_PLAN",
    "plan:full-product": "AGENTS_OS_SOURCE_FULL_PLAN",
    "contract:idea-factory-v0": "AGENTS_OS_SOURCE_IDEA_FACTORY_CONTRACT",
}
MEDIA_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".mp3", ".wav", ".ogg", ".mp4", ".webm", ".mov"}
ARTIFACT_SUFFIXES = {".md", ".txt", ".json", ".log", ".html", ".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mp3", ".wav", ".ogg"}
MAX_JSON_BODY_BYTES = 12 * 1024 * 1024
MAX_AUDIO_BYTES = 8 * 1024 * 1024


def _json_safe(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _read_json_body(handler: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(handler.headers.get("content-length") or 0)
    if length <= 0:
        return {}
    if length > MAX_JSON_BODY_BYTES:
        raise ValueError(f"request body exceeds {MAX_JSON_BODY_BYTES} bytes")
    raw = handler.rfile.read(length).decode("utf-8")
    data = json.loads(raw or "{}")
    if not isinstance(data, dict):
        raise ValueError("request payload must be a JSON object")
    return data


def _send_json(handler: BaseHTTPRequestHandler, payload: dict[str, Any] | list[Any], status: int = 200) -> None:
    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        with _ACTIVE_REQUESTS_LOCK:
            _ACTIVE_REQUESTS.pop(threading.get_ident(), None)


def _send_html(handler: BaseHTTPRequestHandler, html: str, status: int = 200) -> None:
    data = html.encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "text/html; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    try:
        handler.wfile.write(data)
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        with _ACTIVE_REQUESTS_LOCK:
            _ACTIVE_REQUESTS.pop(threading.get_ident(), None)


def _parse_caps(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        return value if isinstance(value, list) else []
    except json.JSONDecodeError:
        return []


def _path_info(path: str) -> dict[str, Any]:
    p = Path(path)
    # UI refresh must stay responsive. Windows-mounted /mnt/d can take seconds per
    # stat/exists call under WSL, so Mission Control marks it as an unscanned
    # external mount instead of blocking loadAll(). Deep artifact scans happen in
    # dedicated/background lanes, not synchronous cockpit refresh.
    if str(p).startswith("/mnt/d/"):
        return {
            "path": str(p),
            "exists": False,
            "suffix": p.suffix.lower(),
            "kind": "external_mount_unscanned",
            "size_bytes": None,
        }
    exists = p.exists()
    is_file = exists and p.is_file()
    return {
        "path": str(p),
        "exists": exists,
        "suffix": p.suffix.lower(),
        "kind": "directory" if exists and p.is_dir() else "file",
        "size_bytes": p.stat().st_size if is_file else None,
    }


def _default_agent_cards(paths: AgentsOSPaths) -> list[dict[str, Any]]:
    return [
        {
            "id": "local-agent",
            "name": "Hermes / Doni",
            "status": "available",
            "capabilities": ["operator execution", "planning", "TDD", "reports", "Mission Control"],
            "runtime_home": str(paths.home),
            "reference_home": str(paths.root),
            "identity_boundary": "active local Hermes profile; operator authority remains outside the UI payload",
            "memory_boundary": "profile-local Hermes home only",
            "auth_boundary": "profile-local auth only; credentials are never displayed",
            "allowed_lanes": ["safe local files", "tests", "local API smoke", "local reports", "vault artefacts"],
            "allowed_actions": ["safe local files", "tests", "local API smoke", "local reports"],
            "approval_gates": ["deploy", "public send", "credential use", "gateway restart", "destructive changes"],
        },
        {
            "id": "coding-delegate",
            "name": "Codex",
            "status": "reference",
            "capabilities": ["coding delegate", "review", "patch suggestions"],
            "runtime_home": "repo-local worker context only when explicitly invoked",
            "reference_home": "repo-local only when explicitly routed",
            "identity_boundary": "coding delegate is not the active local authority",
            "memory_boundary": "no active profile memory merge",
            "auth_boundary": "no credential sharing from the active profile",
            "allowed_lanes": ["local branch work after explicit routing", "code review", "test implementation"],
            "allowed_actions": ["local branch work after explicit routing"],
            "approval_gates": ["push", "PR", "public GitHub action", "credential use"],
        },
        {
            "id": "separate-profile",
            "name": "Claude",
            "status": "separate_profile",
            "capabilities": ["separate Hermes profile"],
            "runtime_home": "separate-profile-home",
            "reference_home": "read-only boundary reference",
            "identity_boundary": "separate profile; not active local authority",
            "memory_boundary": "separate personal memory; no merge",
            "auth_boundary": "separate profile auth; no cross-copy",
            "allowed_lanes": ["status reference only"],
            "allowed_actions": ["status reference only"],
            "approval_gates": ["any profile write", "auth change", "gateway lifecycle"],
        },
        {
            "id": "external-reference-runtime",
            "name": "OpenClaw",
            "status": "separate_runtime",
            "capabilities": ["reference bridge", "source artefacts"],
            "runtime_home": "external-runtime-home",
            "reference_home": "external-reference-home",
            "identity_boundary": "external runtime is separate from active local identity",
            "memory_boundary": "separate runtime memory; reference only",
            "auth_boundary": "no auth/session sharing",
            "allowed_lanes": ["read-only reference when useful", "explicit bridge tasks"],
            "allowed_actions": ["read-only reference when useful"],
            "approval_gates": ["runtime write", "bridge mutation", "memory import/export"],
        },
        {
            "id": "candidate-agent",
            "name": "Candidate agents",
            "status": "candidate",
            "capabilities": ["future plugin/worker slots"],
            "runtime_home": "not assigned",
            "reference_home": "Mission Control registry",
            "identity_boundary": "must declare before activation",
            "memory_boundary": "must declare before activation",
            "auth_boundary": "must declare before activation",
            "allowed_lanes": ["registration draft only"],
            "allowed_actions": ["registration draft only"],
            "approval_gates": ["activation", "tool access", "credential access"],
        },
    ]


def agents_registry_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    cached = _payload_cache_get(paths, "agents", 60.0)
    if cached is not None:
        return cached
    cards = {card["id"]: card for card in _default_agent_cards(paths)}
    cards["codex"] = {**cards["coding-delegate"], "id": "codex", "name": "Codex"}
    cards["claude"] = {**cards["separate-profile"], "id": "claude", "name": "Claude"}
    cards["openclaw"] = {**cards["external-reference-runtime"], "id": "openclaw", "name": "OpenClaw"}
    runtime_ids = {"local-agent": "hermes", "codex": "codex", "claude": "claude", "openclaw": "openclaw"}
    try:
        probes = ExecutionCoordinator(paths, allowed_cwds=resolve_allowed_cwds(paths)).capabilities()
        for card_id, runtime in runtime_ids.items():
            probe = probes.get(runtime, {})
            detected = bool(probe.get("available"))
            activation_enabled = card_id in {"local-agent", "codex"}
            cards[card_id]["runtime"] = runtime
            cards[card_id]["status"] = "available" if detected and activation_enabled else "disabled"
            cards[card_id]["runtime_probe"] = {
                "available": detected,
                "activation_enabled": activation_enabled,
                "reason": None if detected else "runtime_unavailable",
            }
            cards[card_id]["memory_boundary"] = "shared registry with explicit scope/provenance; no identity auto-merge"
    except Exception as exc:
        for card_id, runtime in runtime_ids.items():
            cards[card_id]["runtime"] = runtime
            cards[card_id]["status"] = "probe_error"
            cards[card_id]["runtime_probe"] = {"available": False, "reason": f"{exc.__class__.__name__}: {exc}"}
    with connect(paths) as conn:
        for row in conn.execute("SELECT * FROM agents ORDER BY created_at ASC").fetchall():
            item = row_to_dict(row)
            base = cards.get(item["id"], {})
            cards[item["id"]] = {
                **base,
                "id": item["id"],
                "name": item.get("name") or item["id"],
                "status": item.get("status") or base.get("status", "available"),
                "capabilities": _parse_caps(item.get("capabilities")) or base.get("capabilities", []),
                "runtime_home": base.get("runtime_home", str(paths.root)),
                "reference_home": base.get("reference_home", str(paths.root)),
                "memory_boundary": base.get("memory_boundary", "declared local boundary required"),
                "auth_boundary": base.get("auth_boundary", "credentials are never displayed"),
                "identity_boundary": base.get("identity_boundary", "declared local boundary required"),
                "allowed_lanes": base.get("allowed_lanes", base.get("allowed_actions", ["safe local tasks"])),
                "allowed_actions": base.get("allowed_actions", ["safe local tasks"]),
                "approval_gates": base.get("approval_gates", ["public", "credential", "deploy", "destructive"]),
            }
    return _payload_cache_put(paths, "agents", {"local_only": True, "agents": list(cards.values())})


def knowledge_index_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    nodes = []
    for node_id, default_path in SOURCE_DEFAULTS.items():
        value = os.environ.get(SOURCE_ENV.get(node_id, ""), default_path)
        info = _path_info(value) if not value.startswith("http") else {"path": value, "exists": True, "kind": "url", "size_bytes": None, "suffix": ""}
        kind = node_id.split(":", 1)[0]
        nodes.append({"id": node_id, "kind": kind, "label": node_id, "weight": 10 if info["exists"] else 3, **info})
    with connect(paths) as conn:
        for row in conn.execute("SELECT id,title,path,kind,task_id,workflow,created_at FROM artifacts ORDER BY created_at DESC LIMIT 40").fetchall():
            item = row_to_dict(row)
            info = _path_info(item["path"])
            nodes.append({"id": f"artifact:{item['id']}", "kind": "artifact", "label": item["title"], "weight": 7 if info["exists"] else 2, "task_id": item.get("task_id"), "workflow": item.get("workflow"), **info})
        for row in conn.execute("""SELECT o.id,o.title,o.kind,o.scope,o.profile_id,o.task_id,o.body_uri,
                                          p.producer_runtime,p.producer_agent,p.run_id
                                   FROM memory_objects o LEFT JOIN memory_provenance p ON p.object_id=o.id
                                   ORDER BY o.created_at DESC LIMIT 40""").fetchall():
            item = row_to_dict(row)
            nodes.append({"id": f"memory:{item['id']}", "kind": "memory", "label": item["title"],
                          "weight": 9, "path": item.get("body_uri") or "sqlite:memory_objects",
                          "exists": True, "scope": item.get("scope"), "profile_id": item.get("profile_id"),
                          "task_id": item.get("task_id"), "run_id": item.get("run_id"),
                          "producer_runtime": item.get("producer_runtime"), "producer_agent": item.get("producer_agent")})
    edges = [
        {"from": "video:q13OqknCh-c", "to": "transcript:q13OqknCh-c", "relation": "has_transcript"},
        {"from": "video:q13OqknCh-c", "to": "youtube-note:q13OqknCh-c", "relation": "intake_note"},
        {"from": "youtube-note:q13OqknCh-c", "to": "plan:parity-q13OqknCh-c", "relation": "informed_plan"},
        {"from": "plan:parity-q13OqknCh-c", "to": "plan:full-product", "relation": "expands_to"},
        {"from": "contract:idea-factory-v0", "to": "plan:full-product", "relation": "implements_slice"},
    ]
    return {"local_only": True, "runtime_memory_registry": True, "runtime_memory_merge": False,
            "note": "provenance registry + vault/reference graph; identity scopes are never auto-merged",
            "nodes": nodes, "edges": edges}


def memory_search_action(paths: AgentsOSPaths, data: dict[str, Any]) -> dict[str, Any]:
    from hermes_cli.agents_os_memory import search_memory
    query = str(data.get("query") or "").strip()
    if not query:
        raise ValueError("query is required")
    scopes = data.get("scopes") or ["profile", "task", "shared"]
    if not isinstance(scopes, list):
        raise ValueError("scopes must be an array")
    with connect(paths) as conn:
        items = search_memory(
            conn, query, profile_id=str(data.get("profile_id") or "doni"), scopes=scopes,
            project_id=data.get("project_id"), task_id=data.get("task_id"), limit=int(data.get("limit") or 20),
        )
    return {"query": query, "items": items, "count": len(items), "scopes": scopes, "identity_merge": False}


def executive_board_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    """Return the local company/board snapshot without invoking an agent or connector."""
    with connect(paths) as conn:
        payload = ExecutiveBoardService(conn).company_snapshot()
    return {
        "local_only": True,
        "external_calls": False,
        "identity_merge": False,
        "owner_final_authority": True,
        **payload,
    }


def executive_board_action(service: AgentsOSService, data: dict[str, Any]) -> dict[str, Any]:
    """Apply one validated local board mutation; external execution remains disabled."""
    action = str(data.get("action") or "").strip()
    with connect(service.paths) as conn:
        board = ExecutiveBoardService(conn)
        if action == "create_meeting":
            meeting_id = board.create_meeting(
                data.get("objective", ""), project_id=data.get("project_id"),
                risk_class=data.get("risk_class", "safe-local"),
            )
            result = {"status": "created", "meeting_id": meeting_id}
        elif action == "submit_proposal":
            result = {"status": "submitted", **board.submit_proposal(data["meeting_id"], data["agent_id"], data["content"], data["scorecard"])}
        elif action == "submit_challenge":
            result = {"status": "submitted", **board.submit_challenge(data["meeting_id"], data["challenger_id"], data["target_agent_id"], data["content"])}
        elif action == "finalize_recommendation":
            result = board.finalize_recommendation(
                data["meeting_id"], data["recommendation"], data["goal_prompt"],
                consensus=data.get("consensus", "consensus"), dissent=data.get("dissent", ""),
            )
        elif action == "record_decision":
            result = board.record_owner_decision(
                data["meeting_id"], data["decision"], decided_by=data.get("decided_by", ""), reason=data.get("reason", ""),
            )
        elif action == "stage_memory_candidate":
            result = board.stage_memory_candidate(
                data["capsule_id"], data["capsule_sha256"], data["classification"], data["summary"], data["provenance"],
            )
        elif action == "promote_memory_candidate":
            result = board.promote_memory_candidate(
                data["candidate_id"], approved_by=data.get("approved_by", ""), reason=data.get("reason", ""),
            )
        elif action == "upsert_project":
            result = board.upsert_project(
                data["project_id"], data["name"], status=data["status"], owner=data["owner"],
                next_action=data["next_action"], revenue_potential=data["revenue_potential"],
                strategic_value=data["strategic_value"], risk=data["risk"],
            )
        elif action == "add_idea":
            result = board.add_idea(
                data["idea_id"], data["title"], source=data["source"], status=data["status"],
                value=data["value"], feasibility=data["feasibility"], risk=data["risk"],
                cost=data["cost"], time=data["time"], revenue=data["revenue"],
            )
        else:
            raise ValueError("unsupported executive-board action")
    return {**result, "local_only": True, "external_calls": False, "identity_merge": False}


def board_meeting_schema_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    agents = agents_registry_payload(paths)["agents"]
    return {
        "local_only": True,
        "execution_created": False,
        "external_calls": False,
        "fields": {
            "objective": {"type": "string", "required": True, "max_length": 2000},
            "participants": {"type": "array", "items": [agent["id"] for agent in agents]},
        },
        "default_participants": [agent["id"] for agent in agents if agent["id"] == "local-agent"] or ([agents[0]["id"]] if agents else []),
        "approval_gates": [
            "deploy",
            "push",
            "PR",
            "credentials",
            "external integrations",
            "microphone",
            "TTS",
            "computer-control",
            "gateway restart",
        ],
    }


def _board_meeting_risk(objective: str) -> tuple[bool, str, list[str]]:
    lowered = f" {objective.lower()} "
    risky_markers = {
        "deploy": "deploy",
        "deployaj": "deploy",
        "push": "push",
        "pull request": "PR",
        " pr ": "PR",
        "pošalji": "external_send",
        "posalji": "external_send",
        "email": "external_send",
        "credential": "credentials",
        "token": "credentials",
        "api key": "credentials",
        "mikrofon": "microphone",
        "microphone": "microphone",
        "tts": "TTS",
        "computer-control": "computer-control",
        "gateway restart": "gateway restart",
    }
    gates = sorted({gate for marker, gate in risky_markers.items() if marker in lowered})
    return bool(gates), "approval_gated" if gates else "safe_local", gates


def draft_board_meeting_action(service: AgentsOSService, data: dict[str, Any]) -> dict[str, Any]:
    paths = service.paths
    objective = str(data.get("objective") or "").strip()
    if not objective:
        raise ValueError("objective is required")
    if len(objective) > 2000:
        objective = objective[:2000]
    known_agents = {agent["id"]: agent for agent in agents_registry_payload(paths)["agents"]}
    raw_participants = data.get("participants") or ["local-agent"]
    if not isinstance(raw_participants, list):
        raw_participants = [raw_participants]
    participants = [str(item) for item in raw_participants if str(item) in known_agents]
    if not participants and "local-agent" in known_agents:
        participants = ["local-agent"]
    risky, risk_class, gates = _board_meeting_risk(objective)
    stamp = utc_now().replace(":", "").replace("-", "").replace(".", "")[:15] + "-" + uuid.uuid4().hex[:6]
    slug = slugify(objective[:80], fallback="board-meeting")
    task_id = f"task-board-{stamp}"
    artifact_id = f"artifact-board-{stamp}"
    approval_id = f"approval-board-{stamp}" if risky else None
    mode = "approval_draft" if risky else "safe_local_task"
    now = utc_now()
    board_dir = paths.artifacts / "board-meetings"
    board_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = board_dir / f"{stamp}-{slug}.md"
    body_payload = {
        "objective": objective,
        "participants": participants,
        "risk_class": risk_class,
        "approval_required": risky,
        "approval_gates_triggered": gates,
        "execution_created": False,
        "created_at": now,
    }
    participant_lines = "\n".join(f"- {known_agents[p]['name']} (`{p}`)" for p in participants)
    artifact_path.write_text(
        "# Board Meeting Draft\n\n"
        f"## Objective\n{objective}\n\n"
        f"## Participants\n{participant_lines or '- none'}\n\n"
        f"## Safety\n- local_only: true\n- execution_created: false\n- mode: {mode}\n- approval_required: {str(risky).lower()}\n\n"
        "## Payload\n```json\n"
        + json.dumps(body_payload, ensure_ascii=False, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )
    with connect(paths) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tasks(id,title,status,workflow,priority,created_at,updated_at,notes,route,approval_required) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, f"Board Meeting: {objective[:90]}", "needs_approval" if risky else "pending", "board-meeting", 2, now, now, objective, "approval_gate" if risky else "local:direct", 1 if risky else 0),
        )
        conn.execute(
            "INSERT OR REPLACE INTO artifacts(id,kind,title,path,task_id,workflow,created_at) VALUES(?,?,?,?,?,?,?)",
            (artifact_id, "board_meeting", "Board Meeting Draft", str(artifact_path), task_id, "board-meeting", now),
        )
        if approval_id:
            conn.execute(
                "INSERT OR REPLACE INTO approvals(id,title,status,risk,task_id,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (approval_id, f"Approval draft: Board Meeting — {objective[:80]}", "pending", risk_class, task_id, json.dumps(body_payload, ensure_ascii=False), now),
            )
            log_event(conn, "approval_requested", task_id=task_id, payload={"approval_id": approval_id, "risk": risk_class, "execution_created": False})
        log_event(conn, "board_meeting_drafted", task_id=task_id, payload={"artifact_id": artifact_id, "approval_id": approval_id, "mode": mode, "execution_created": False})
        conn.commit()
    return {
        "status": "drafted",
        "local_only": True,
        "execution_created": False,
        "external_calls": False,
        "mode": mode,
        "approval_required": risky,
        "approval_gates_triggered": gates,
        "task_id": task_id,
        "approval_id": approval_id,
        "artifact_id": artifact_id,
        "artifact_path": str(artifact_path),
        "participants": participants,
    }


def workflow_factory_schema_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    return {
        "local_only": True,
        "execution_created": False,
        "external_calls": False,
        "mutation_scope": "local_artifact_only",
        "fields": {
            "input_text": {"type": "string", "required": True, "max_length": 4000},
            "source_url": {"type": "string", "required": False},
            "create_task": {"type": "boolean", "default": False, "note": "v0 keeps task creation disabled; draft artifact only"},
        },
        "output_contract": [
            "input_type", "classification", "capability_bucket", "value_score", "risk_class",
            "approval_required", "approval_gates", "next_safe_local_action", "suggested_agent_lane",
            "artifact_title", "artifact_path", "execution_created",
        ],
        "approval_gates": ["deploy", "push/pr", "dns/domain", "credentials", "external_send", "browser/computer-control", "gateway_restart", "cross_agent_memory_merge"],
        "artifact_root": str(paths.artifacts / "workflow-factory"),
    }


def _workflow_factory_gate_text(value: str) -> str:
    return f" {value.lower()} "


def _workflow_factory_input_type(text: str, source_url: str) -> str:
    merged = f"{source_url}\n{text}".lower()
    if "youtu.be" in merged or "youtube.com" in merged:
        return "video"
    if any(marker in merged for marker in ("screenshot", "slika", ".png", ".jpg", ".webp")):
        return "screenshot"
    if any(marker in merged for marker in ("http://", "https://", ".com", ".de", ".hr")):
        return "web_project"
    return "idea"


def _workflow_factory_risk(text: str) -> tuple[str, bool, list[str]]:
    lowered = _workflow_factory_gate_text(text)
    gates: list[str] = []
    gate_markers = {
        " deploy": "deploy", "deployaj": "deploy", "vercel": "deploy",
        " push": "push/pr", " pull request": "push/pr", " pr ": "push/pr",
        "dns": "dns/domain", "domain": "dns/domain", "domena": "dns/domain",
        "email": "external_send", "pošalji": "external_send", "posalji": "external_send",
        "token": "credentials", "credential": "credentials", "api key": "credentials", "cookie": "credentials",
        "browser-control": "browser/computer-control", "computer-control": "browser/computer-control", "open up google": "browser/computer-control",
        "gateway restart": "gateway_restart", "memory merge": "cross_agent_memory_merge",
    }
    for marker, gate in gate_markers.items():
        if marker in lowered and gate not in gates:
            gates.append(gate)
    if gates:
        return "approval_gated", True, sorted(gates)
    return "safe_local", False, []


def _workflow_factory_bucket(text: str, input_type: str) -> tuple[str, str, int, str]:
    lowered = text.lower()
    if input_type == "video":
        return "source_ingest", "youtube_or_video_capability_intake", 86, "doni-vault-intake"
    if any(marker in lowered for marker in ("vercel", "domain", "domena", "deploy", "roadtripbarber", "stara verzija")):
        return "web_ops", "web_deploy_domain_diagnostics", 82, "doni-web-ops"
    if any(marker in lowered for marker in ("agent os", "approval", "pending", "panel", "runtime", "memory")):
        return "agent_runtime", "agent_os_runtime_capability", 90, "doni-runtime-with-kodi-review"
    if input_type == "screenshot":
        return "visual_intake", "screenshot_to_implementation", 74, "doni-visual-intake"
    return "idea_ops", "general_safe_local_workflow", 65, "doni-local-planning"


def draft_workflow_factory_action(service: AgentsOSService, data: dict[str, Any]) -> dict[str, Any]:
    paths = service.paths
    input_text = str(data.get("input_text") or data.get("text") or data.get("idea_text") or "").strip()
    source_url = str(data.get("source_url") or data.get("url") or "").strip()
    if not input_text and source_url:
        input_text = source_url
    if not input_text:
        raise ValueError("input_text is required")
    if len(input_text) > 4000:
        input_text = input_text[:4000]
    combined = f"{source_url}\n{input_text}".strip()
    input_type = _workflow_factory_input_type(input_text, source_url)
    capability_bucket, classification, value_score, suggested_agent_lane = _workflow_factory_bucket(combined, input_type)
    risk_class, approval_required, approval_gates = _workflow_factory_risk(combined)
    if approval_required:
        next_safe_local_action = "Write local draft/report only; wait for explicit Goran approval before gated action."
    elif capability_bucket == "source_ingest":
        next_safe_local_action = "Create vault intake note, capability map, and local implementation plan."
    elif capability_bucket == "web_ops":
        next_safe_local_action = "Run local/source baseline and prepare diagnostics without deploy/DNS mutation."
    elif capability_bucket == "agent_runtime":
        next_safe_local_action = "Patch or stage local Agent OS read-back slice, then py_compile/pytest/API smoke."
    else:
        next_safe_local_action = "Create local task charter and verification checklist."
    stamp = utc_now().replace(":", "").replace("-", "").replace(".", "")[:15] + "-" + uuid.uuid4().hex[:6]
    artifact_title = f"Workflow Factory Draft — {classification}"
    artifact_dir = paths.artifacts / "workflow-factory"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_dir / f"{stamp}-{slugify(classification)}.md"
    draft_payload = {
        "input_type": input_type,
        "classification": classification,
        "capability_bucket": capability_bucket,
        "value_score": value_score,
        "risk_class": risk_class,
        "approval_required": approval_required,
        "approval_gates": approval_gates,
        "next_safe_local_action": next_safe_local_action,
        "suggested_agent_lane": suggested_agent_lane,
        "artifact_title": artifact_title,
        "artifact_path": str(artifact_path),
        "execution_created": False,
        "task_created": False,
        "source_url": source_url or None,
    }
    artifact_path.write_text(
        f"# {artifact_title}\n\n"
        f"## Input\n{input_text}\n\n"
        "## Draft payload\n```json\n"
        + json.dumps(draft_payload, ensure_ascii=False, indent=2)
        + "\n```\n",
        encoding="utf-8",
    )
    artifact_id = f"artifact-workflow-{stamp}"
    with connect(paths) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO artifacts(id,kind,title,path,task_id,workflow,created_at) VALUES(?,?,?,?,?,?,?)",
            (artifact_id, "workflow_factory", artifact_title, str(artifact_path), None, "workflow-factory", utc_now()),
        )
        log_event(conn, "workflow_factory_drafted", payload={"artifact_id": artifact_id, "classification": classification, "risk_class": risk_class, "execution_created": False})
        conn.commit()
    return {
        "status": "drafted",
        "local_only": True,
        "external_calls": False,
        "execution_created": False,
        "artifact_id": artifact_id,
        "draft_payload": draft_payload,
        **draft_payload,
    }


def _bounded_files(root: Path, suffixes: set[str], *, max_scan: int, max_items: int) -> tuple[list[Path], bool]:
    if not root.exists():
        return [], False
    found: list[Path] = []
    scanned = 0
    stack = [root]
    while stack and scanned < max_scan and len(found) < max_items:
        current = stack.pop()
        try:
            entries = current.iterdir()
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned >= max_scan or len(found) >= max_items:
                break
            try:
                if entry.is_dir() and not entry.is_symlink():
                    stack.append(entry)
                elif entry.is_file() and entry.suffix.lower() in suffixes:
                    found.append(entry)
            except OSError:
                continue
    return found, bool(stack or scanned >= max_scan or len(found) >= max_items)


def _artifacts_payload_uncached(paths: AgentsOSPaths) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    with connect(paths) as conn:
        for row in conn.execute("SELECT * FROM artifacts ORDER BY created_at DESC LIMIT 100").fetchall():
            item = row_to_dict(row)
            info = _path_info(item["path"])
            path_kind = info.pop("kind")
            item.update(info)
            item["path_kind"] = path_kind
            item["preview_type"] = "markdown" if info["suffix"] == ".md" else ("json" if info["suffix"] == ".json" else ("media" if info["suffix"] in MEDIA_SUFFIXES else "text"))
            items.append(item)
    seen = {item["path"] for item in items}
    scan_truncated = False
    for root in [paths.artifacts, paths.vault_root]:
        files, truncated = _bounded_files(root, ARTIFACT_SUFFIXES, max_scan=900, max_items=max(0, 160 - len(items)))
        scan_truncated = scan_truncated or truncated
        for p in files:
            if str(p) not in seen:
                info = _path_info(str(p))
                items.append({"id": f"file:{slugify(str(p))}", "kind": "file", "title": p.name, "task_id": None, "workflow": None, **info})
                seen.add(str(p))
    summary: dict[str, int] = {}
    for item in items:
        key = str(item.get("kind") or item.get("preview_type") or item.get("suffix") or "unknown")
        summary[key] = summary.get(key, 0) + 1
    return {"local_only": True, "read_only": True, "mutation_enabled": False, "credentials_visible": False, "bounded_scan": True, "scan_truncated": scan_truncated, "summary": summary, "items": items}


def artifacts_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    key = (str(paths.home.resolve()), "artifacts")
    with _PAYLOAD_CACHE_LOCK:
        cached = _PAYLOAD_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 15.0:
            return cached[1]
        payload = _artifacts_payload_uncached(paths)
        _PAYLOAD_CACHE[key] = (time.monotonic(), payload)
        return payload


def _media_assets_payload_uncached(paths: AgentsOSPaths) -> dict[str, Any]:
    assets = []
    scan_truncated = False
    for root in [paths.artifacts, paths.vault_root]:
        files, truncated = _bounded_files(root, MEDIA_SUFFIXES, max_scan=900, max_items=max(0, 80 - len(assets)))
        scan_truncated = scan_truncated or truncated
        for p in files:
            try:
                mime, _ = mimetypes.guess_type(str(p))
                assets.append({"path": str(p), "title": p.name, "mime": mime or "application/octet-stream", "size_bytes": p.stat().st_size, "read_only": True})
            except OSError:
                continue
    return {"local_only": True, "generation_enabled": False, "posting_enabled": False, "bounded_scan": True, "scan_truncated": scan_truncated, "assets": assets}


def media_assets_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    key = (str(paths.home.resolve()), "media")
    with _PAYLOAD_CACHE_LOCK:
        cached = _PAYLOAD_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 30.0:
            return cached[1]
        payload = _media_assets_payload_uncached(paths)
        _PAYLOAD_CACHE[key] = (time.monotonic(), payload)
        return payload


def _safe_json_preview(value: Any, *, limit: int = 900) -> Any:
    """Return a bounded, non-secret preview for UI read-only surfaces."""
    if value is None:
        return None
    text = str(value)
    lowered = text.lower()
    if any(marker in lowered for marker in ("api_key", "apikey", "authorization", "bearer ", "token", "secret", "password", "cookie")):
        return "[redacted-sensitive-preview]"
    try:
        parsed = json.loads(text)
        dumped = json.dumps(parsed, ensure_ascii=False)
        if len(dumped) > limit:
            return dumped[:limit] + "…"
        return parsed
    except Exception:
        return text[:limit] + ("…" if len(text) > limit else "")


def _table_rows(paths: AgentsOSPaths, table: str, *, order: str = "created_at DESC", limit: int = 100) -> list[dict[str, Any]]:
    allowed = {"tasks", "approvals", "runs", "events", "artifacts", "reviews", "state_snapshots"}
    if table not in allowed:
        raise ValueError(f"unsupported table: {table}")
    allowed_orders = {
        "created_at DESC",
        "CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC",
        "CASE status WHEN 'ready' THEN 0 WHEN 'pending' THEN 1 WHEN 'needs_approval' THEN 2 WHEN 'review' THEN 3 WHEN 'blocked' THEN 4 WHEN 'completed' THEN 8 ELSE 7 END, priority ASC, created_at DESC",
    }
    if order not in allowed_orders:
        raise ValueError(f"unsupported order: {order}")
    quoted_table = '"' + table.replace('"', '""') + '"'
    query = "SELECT * FROM " + quoted_table + " ORDER BY " + order + " LIMIT ?"
    with connect(paths) as conn:
        rows = [row_to_dict(row) for row in conn.execute(query, (limit,)).fetchall()]
    return rows


def tasks_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    rows = _table_rows(paths, "tasks", order="CASE status WHEN 'ready' THEN 0 WHEN 'pending' THEN 1 WHEN 'needs_approval' THEN 2 WHEN 'review' THEN 3 WHEN 'blocked' THEN 4 WHEN 'completed' THEN 8 ELSE 7 END, priority ASC, created_at DESC", limit=160)
    counts: dict[str, int] = {}
    hygiene_counts: dict[str, int] = {}
    for item in rows:
        counts[item.get("status") or "unknown"] = counts.get(item.get("status") or "unknown", 0) + 1
        item["approval_required"] = bool(item.get("approval_required"))
        hygiene = _task_hygiene_projection(item)
        item["queue_hygiene"] = hygiene
        hygiene_counts[hygiene["queue_class"]] = hygiene_counts.get(hygiene["queue_class"], 0) + 1
    return {
        "local_only": True,
        "read_only": True,
        "counts": counts,
        "hygiene_counts": hygiene_counts,
        "hygiene_contract": {
            "operator_actionable": "real local work that can be inspected or continued without public/credential/destructive side effects",
            "proof_generated": "E2E/browser/proof artifacts kept for audit trail; hidden from primary real-work queue",
            "approval_gated": "visible but blocked until explicit Goran decision",
            "stale_draft": "older draft/planning task that should be reviewed before execution",
        },
        "items": rows,
    }


def _task_hygiene_projection(item: dict[str, Any]) -> dict[str, Any]:
    """Read-only queue hygiene labels; does not mutate runtime state."""
    status = str(item.get("status") or "unknown")
    workflow = str(item.get("workflow") or "")
    text = " ".join(str(item.get(key) or "") for key in ("id", "title", "notes", "workflow")).lower()
    approval_gated = bool(item.get("approval_required")) or status == "needs_approval" or "approval" in str(item.get("route") or "").lower()
    proof_generated = any(marker in text for marker in (
        "e2e",
        "proof",
        "browser proof",
        "rerun",
        "artifact proof",
        "screenshot",
        "cdp",
    ))
    stale_draft = (not approval_gated and not proof_generated and status == "pending" and workflow in {"board-meeting", "research_brief", "seo-goal", "youtube-content-intake", "clarify-or-research"})
    operator_actionable = status in {"ready", "pending", "review", "blocked"} and not approval_gated and not proof_generated and not stale_draft
    if approval_gated:
        queue_class = "approval_gated"
        next_step = "Goran decision required; keep visible but do not execute or resolve autonomously."
    elif proof_generated:
        queue_class = "proof_generated"
        next_step = "Keep as audit/proof trail; do not let it crowd primary real-work queue."
    elif stale_draft:
        queue_class = "stale_draft"
        next_step = "Review intent/evidence before treating as active work."
    elif operator_actionable:
        queue_class = "operator_actionable"
        next_step = "Safe local candidate: inspect task detail, create/verify artifact, then close with evidence."
    else:
        queue_class = "archive_or_monitor"
        next_step = "No immediate operator action."
    return {
        "queue_class": queue_class,
        "operator_actionable": operator_actionable,
        "proof_generated": proof_generated,
        "approval_gated": approval_gated,
        "stale_draft": stale_draft,
        "next_step": next_step,
    }


def approvals_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    rows = _table_rows(paths, "approvals", order="CASE status WHEN 'pending' THEN 0 ELSE 1 END, created_at DESC", limit=120)
    counts: dict[str, int] = {}
    for item in rows:
        counts[item.get("status") or "unknown"] = counts.get(item.get("status") or "unknown", 0) + 1
        item["payload_preview"] = _safe_json_preview(item.pop("payload", None))
        item["safe_actions"] = []
        item["resolution_enabled"] = False
    return {"local_only": True, "read_only": True, "credentials_visible": False, "resolution_enabled": False, "counts": counts, "items": rows}


def runs_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    rows = _table_rows(paths, "runs", order="created_at DESC", limit=120)
    counts: dict[str, int] = {}
    for item in rows:
        counts[item.get("status") or "unknown"] = counts.get(item.get("status") or "unknown", 0) + 1
        item["input_preview"] = _safe_json_preview(item.pop("input", None))
    return {"local_only": True, "read_only": True, "counts": counts, "items": rows}


def events_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    rows = _table_rows(paths, "events", order="created_at DESC", limit=160)
    counts: dict[str, int] = {}
    for item in rows:
        counts[item.get("event_type") or "unknown"] = counts.get(item.get("event_type") or "unknown", 0) + 1
        item["payload_preview"] = _safe_json_preview(item.pop("payload", None))
    return {"local_only": True, "read_only": True, "counts": counts, "items": rows}


def cron_readiness_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    cron_dir = paths.home / "cron"
    scripts_dir = paths.home / "scripts"
    agents_log = paths.root / "logs" / "mission-control-18791.log"
    launchers = paths.root / "launchers"
    launcher = launchers / "start_agents_os_mission_control.sh"
    watchdog = launchers / "watch_agents_os_mission_control.sh"
    desktop_launcher = Path(os.environ["AGENTS_OS_DESKTOP_LAUNCHER"]) if os.environ.get("AGENTS_OS_DESKTOP_LAUNCHER") else None
    return {
        "local_only": True,
        "read_only": True,
        "cron_mutation_enabled": False,
        "startup_mutation_enabled": False,
        "watchdog": {"path": str(watchdog), "exists": watchdog.exists()},
        "launcher": {"path": str(launcher), "exists": launcher.exists()},
        "desktop_launcher": {"path": str(desktop_launcher) if desktop_launcher else None, "exists": desktop_launcher.exists() if desktop_launcher else False},
        "logs": {"mission_control": str(agents_log), "exists": agents_log.exists()},
        "cron_dir": {"path": str(cron_dir), "exists": cron_dir.exists()},
        "scripts_dir": {"path": str(scripts_dir), "exists": scripts_dir.exists()},
    }


def tool_shed_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    skills = skills_visibility_payload(paths)
    cron = cron_readiness_payload(paths)
    manage = redacted_manage_status_payload(paths)
    connectors = [
        {"id": "skills", "label": "Skills", "status": "available", "count": skills.get("count", 0), "mutation_enabled": False},
        {"id": "toolsets", "label": "Hermes toolsets", "status": "available", "mutation_enabled": False, "note": "Runtime-provided tools; inventory view only."},
        {"id": "cron_watchdogs", "label": "Cron / watchdogs", "status": "available" if cron.get("cron_dir", {}).get("exists") else "not_connected", "mutation_enabled": False, "cron_mutation_enabled": False},
        {"id": "external_connectors", "label": "External connectors", "status": "approval_required", "mutation_enabled": False, "credentials_visible": False},
        {"id": "mcp", "label": "MCP servers", "status": "available", "mutation_enabled": False, "note": manage.get("mcp", {}).get("note")},
    ]
    return {
        "local_only": True,
        "read_only": True,
        "credentials_visible": False,
        "mutation_actions_enabled": False,
        "connectors": connectors,
        "skills": {"count": skills.get("count", 0), "content_visible": False, "items": skills.get("items", [])[:40]},
        "cron": cron,
        "toolsets": ["terminal", "file", "web", "browser", "cronjob", "skills", "memory", "vision", "image_gen"],
        "gated_actions": ["install/edit/delete skills", "cron pause/resume/delete/run-now", "credential-backed connector auth", "deploy/push/PR", "gateway restart"],
    }


def safety_payload(service: AgentsOSService) -> dict[str, Any]:
    """Read-only safety contract for Mission Control operator UI.

    This is a deterministic local summary. It does not execute scans, mutate cron,
    restart the gateway, read credentials, or resolve approvals.
    """
    paths = service.paths
    doctor = service.doctor_payload()
    approvals = approvals_payload(paths)
    manage = redacted_manage_status_payload(paths)
    cron = cron_readiness_payload(paths)
    pending = [item for item in approvals.get("items", []) if item.get("status") == "pending"]
    pending_text = json.dumps(pending, ensure_ascii=False).lower()
    high_risk_terms = ("credential", "token", "cookie", "api_key", "deploy", "publish", "send", "external", "gateway", "restart")
    now = datetime.now(timezone.utc)
    stale_approvals = 0
    for item in pending:
        try:
            created = datetime.fromisoformat(str(item.get("created_at") or "").replace("Z", "+00:00"))
            stale_approvals += int((now - created).total_seconds() >= 7 * 86400)
        except (TypeError, ValueError):
            stale_approvals += 1
    with connect(paths) as conn:
        approval_blocked_tasks = conn.execute(
            "SELECT COUNT(*) FROM tasks WHERE approval_required=1 OR status='needs_approval'"
        ).fetchone()[0]
    doctor_ok = doctor.get("ok") is True
    return {
        "local_only": True,
        "read_only": True,
        "status": "ok" if doctor_ok and not pending else "attention",
        "doctor": {"ok": doctor_ok, "checks": doctor.get("checks", {})},
        "mirror_validate": {"status": "not_run_from_web", "mutation_enabled": False},
        "credential_scan": {"status": "not_run_from_web", "real_leaks": None, "credentials_visible": False},
        "network_side_effects": False,
        "runtime_config_changed": False,
        "gateway_restart": False,
        "profile_home_isolation": doctor.get("checks", {}).get("policy_home_isolated") is True,
        "doni_marija_ero_separation": {"status": "policy_declared_not_deep_scanned", "verified": None},
        "management_surface": {"credentials_visible": manage.get("credentials_visible") is True, "gateway_restart": manage.get("hermes", {}).get("gateway_restart") is True},
        "cron": {"read_only": cron.get("read_only") is True, "mutation_enabled": cron.get("cron_mutation_enabled") is True},
        "approval_risk": {
            "status": "attention" if pending else "ok",
            "pending_approvals": len(pending),
            "high_risk_pending_approvals": sum(1 for item in pending if any(term in json.dumps(item, ensure_ascii=False).lower() for term in high_risk_terms)),
            "stale_approvals": stale_approvals,
            "approval_blocked_tasks": approval_blocked_tasks,
            "credential_sensitive_pending": int(any(term in pending_text for term in ("credential", "token", "cookie", "api_key"))),
            "gateway_or_runtime_change_pending": int(any(term in pending_text for term in ("gateway", "restart", "runtime_config"))),
            "external_action_pending": int(any(term in pending_text for term in ("external", "send", "publish", "deploy"))),
        },
    }


def _bounded_file_count(root: Path, *, name: str | None = None, max_scan: int = 600) -> tuple[int, bool]:
    """Fast bounded local status count; avoids slow recursive scans in UI requests."""
    if not root.exists():
        return 0, False
    count = 0
    scanned = 0
    stack = [root]
    while stack and scanned < max_scan:
        current = stack.pop()
        try:
            entries = current.iterdir()
        except OSError:
            continue
        for entry in entries:
            scanned += 1
            if scanned >= max_scan:
                break
            if entry.is_dir():
                stack.append(entry)
            elif name is None or entry.name == name:
                count += 1
    return count, bool(stack or scanned >= max_scan)


def _redacted_manage_status_payload_uncached(paths: AgentsOSPaths) -> dict[str, Any]:
    skills_dir = paths.home / "skills"
    plugins_dir = paths.home / "plugins"
    cron_dir = paths.home / "cron"
    skill_count, skill_truncated = _bounded_file_count(skills_dir, name="SKILL.md")
    cron_count, cron_truncated = _bounded_file_count(cron_dir)
    try:
        plugin_count = len(list(plugins_dir.iterdir())) if plugins_dir.exists() else 0
    except OSError:
        plugin_count = 0
    return {
        "local_only": True,
        "credentials_visible": False,
        "hermes": {"home": str(paths.home), "agents_os_home": str(paths.root), "state_db": str(paths.db), "gateway_restart": False},
        "skills": {"path": str(skills_dir), "count": skill_count, "truncated": skill_truncated},
        "plugins": {"path": str(plugins_dir), "count": plugin_count},
        "cron": {"path": str(cron_dir), "status_only": True, "count": cron_count, "truncated": cron_truncated},
        "mcp": {"status_only": True, "note": "Use hermes mcp test/list outside this read-only panel when needed."},
        "model_provider": "redacted",
        "candidate_integrations": ["desktop shell", "voice dry-run", "read-only project library"],
    }


def redacted_manage_status_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    key = (str(paths.home.resolve()), "manage")
    with _PAYLOAD_CACHE_LOCK:
        cached = _PAYLOAD_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 60.0:
            return cached[1]
        payload = _redacted_manage_status_payload_uncached(paths)
        _PAYLOAD_CACHE[key] = (time.monotonic(), payload)
        return payload


def voice_status_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    cache_audio = paths.home / "cache" / "audio"
    audio_files = list(cache_audio.glob("*"))[-5:] if cache_audio.exists() else []
    return {
        "local_only": True,
        "stt_status": "detectable" if audio_files else "not_detected_or_no_recent_audio",
        "tts_status": "configured_by_runtime_or_tool_provider",
        "recent_audio_count": len(audio_files),
        "jarvis_dry_run_design": [
            "transcribe local voice input",
            "classify intent through deterministic Jarvis risk gate",
            "show command draft and required approval badge",
            "support interrupt/cancel before any execution object is created",
            "execute only safe local/read-only actions after explicit local UI confirmation",
        ],
        "video_informed_capability_map": {
            "source": "https://youtu.be/wX0YzAYywmI",
            "label": "Hermes/Apollo voice agent pattern imported as safe-local Jarvis contract",
            "usable_now": ["typed command preview", "push-to-talk artefact capture", "interrupt/cancel", "SEO keyword preview", "website draft preview", "local status/show"],
            "approval_gated": ["external browser open", "computer control", "public deploy/publish", "outbound email/message", "provider keys", "always-on wake word"],
            "not_adopted": ["Gemini key path", "cross-agent memory merge", "automatic browser/computer control"],
        },
        "computer_control": "approval_gated_unexecuted",
    }


def jarvis_briefing_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    """Safe-local Jarvis/Oracle briefing contract for Mission Control.

    This is a read-only/dry-run payload. It summarizes local state and declares
    command modes without enabling microphone, wake-word, browser, computer, or
    public side effects.
    """
    with connect(paths) as conn:
        task_rows = conn.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status").fetchall()
        approval_rows = conn.execute("SELECT status, COUNT(*) AS count FROM approvals GROUP BY status").fetchall()
        artifact_count = conn.execute("SELECT COUNT(*) AS count FROM artifacts").fetchone()["count"]
        recent_artifacts = [
            row_to_dict(row)
            for row in conn.execute(
                "SELECT id,kind,title,path,task_id,workflow,created_at FROM artifacts ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
        ]
    task_counts = {row["status"]: row["count"] for row in task_rows}
    approval_counts = {row["status"]: row["count"] for row in approval_rows}
    open_task_count = sum(task_counts.get(status, 0) for status in ("new", "pending", "routed", "ready", "in_progress", "needs_approval", "blocked", "review"))
    return {
        "local_only": True,
        "execution_created": False,
        "always_on_microphone": False,
        "wake_word_enabled": False,
        "computer_control": "approval_gated_unexecuted",
        "briefing": {
            "timestamp": utc_now(),
            "agents_os_home": str(paths.root),
            "state_db": str(paths.db),
            "open_task_count": open_task_count,
            "completed_task_count": task_counts.get("completed", 0),
            "pending_approval_count": approval_counts.get("pending", 0),
            "artifact_count": artifact_count,
            "recent_artifacts": recent_artifacts,
        },
        "commands": [
            {"name": "rundown", "mode": "read_only_briefing", "approval_required": False, "does": "Show operator rundown from local Mission Control state."},
            {"name": "show tasks", "mode": "read_only_retrieval", "approval_required": False, "does": "Show current local task board/queue."},
            {"name": "stop", "mode": "interrupt_cancel", "approval_required": False, "does": "Cancel the current local draft/response; no execution object is created."},
            {"name": "open Google", "mode": "approval_gated_external", "approval_required": True, "does": "Prepare external-open preview only; do not control browser/computer from voice."},
            {"name": "SEO keyword ideas", "mode": "safe_local_preview", "approval_required": False, "does": "Prepare local SEO keyword idea preview without publishing or connector use."},
            {"name": "build website", "mode": "safe_local_draft", "approval_required": False, "does": "Create a local website/site-concept draft only; no deploy/publish."},
            {"name": "draft board meeting", "mode": "safe_local_draft", "approval_required": False, "does": "Prepare a local Board Meeting draft only."},
            {"name": "summarize business pulse", "mode": "read_only_briefing", "approval_required": False, "does": "Summarize local business pulse sources without connectors."},
            {"name": "wake", "mode": "read_only_briefing", "approval_required": False, "does": "Boot/status briefing only."},
            {"name": "show", "mode": "read_only_retrieval", "approval_required": False, "does": "Show local tasks, artifacts, notes, and reference graph."},
            {"name": "build", "mode": "safe_local_draft", "approval_required": False, "does": "Create local draft artifacts/tasks only."},
            {"name": "act", "mode": "approval_draft_only", "approval_required": True, "does": "Prepare risky action for explicit approval; do not execute."},
        ],
        "approval_gates": [
            "microphone_wake_word",
            "computer_control",
            "external_open",
            "deploy_publish",
            "credentials",
            "cross_agent_memory_merge",
        ],
        "wall_mode_contract": {
            "enabled_for_display": True,
            "execution_from_wall_mode": False,
            "description": "Large-screen Mission Control display; action execution remains gated.",
        },
    }


def _jarvis_slug_from_time() -> str:
    return utc_now().replace(":", "").replace("-", "").replace(".", "")[:15] + "-" + uuid.uuid4().hex[:6]


def _decode_optional_audio(data: dict[str, Any]) -> bytes:
    raw = data.get("audio_base64") or ""
    if not raw:
        return b""
    if "," in raw and raw.split(",", 1)[0].startswith("data:"):
        raw = raw.split(",", 1)[1]
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("audio_base64 must contain valid base64") from exc
    if len(decoded) > MAX_AUDIO_BYTES:
        raise ValueError(f"decoded audio exceeds {MAX_AUDIO_BYTES} bytes")
    return decoded


def _jarvis_audio_suffix(mime: str | None) -> str:
    mime = (mime or "").lower()
    if "wav" in mime:
        return ".wav"
    if "ogg" in mime:
        return ".ogg"
    if "mpeg" in mime or "mp3" in mime:
        return ".mp3"
    return ".webm"


def _jarvis_preview_from_text(transcript_text: str) -> dict[str, Any]:
    draft = draft_idea(transcript_text)
    normalized = transcript_text.lower()
    if any(token in normalized for token in ["stop", "prekini", "zaustavi", "cancel", "interrupt", "šuti", "suti"]):
        draft["classification"] = "interrupt_cancel"
        draft["risk_class"] = "safe_local"
        draft["recommended_lane"] = "jarvis-interrupt-cancel"
        draft["approval_required"] = False
        draft["plan_steps"] = [
            "Prekinuti trenutni lokalni draft/odgovor.",
            "Označiti command preview kao cancelled.",
            "Ne kreirati execution i ne dirati vanjske sustave.",
        ]
        draft["expected_artifacts"] = ["local_cancel_preview"]
    if any(token in normalized for token in ["open google", "open up google", "otvori google", "open browser", "otvori browser", "otvori web", "open website", "otvori stranicu"]):
        draft["classification"] = "external_open_gated"
        draft["risk_class"] = "external_gated"
        draft["recommended_lane"] = "external-open-approval-preview"
        draft["approval_required"] = True
        draft["plan_steps"] = [
            "Prikazati koji URL/aplikaciju bi trebalo otvoriti.",
            "Ne upravljati browserom ili računalom iz voice layera.",
            "Čekati eksplicitno Goranovo odobrenje za vanjski open/computer-control.",
        ]
        draft["expected_artifacts"] = ["external_open_preview"]
    if any(token in normalized for token in ["seo keyword", "keyword", "ključne riječi", "kljucne rijeci", "seo ideje"]):
        draft["classification"] = "seo_keyword_preview"
        draft["risk_class"] = "safe_local"
        draft["recommended_lane"] = "seo-keyword-local-preview"
        draft["approval_required"] = False
        draft["plan_steps"] = [
            "Napraviti lokalni SEO keyword preview.",
            "Označiti ga kao brainstorming/draft, ne kao live research ako nema izvora.",
            "Ne objavljivati i ne koristiti credential-backed alate.",
        ]
        draft["expected_artifacts"] = ["seo_keyword_preview_card"]
    if any(token in normalized for token in ["build me out a website", "build website", "napravi web", "izgradi web", "website concept", "site concept"]):
        draft["classification"] = "website_build_draft"
        draft["risk_class"] = "safe_local"
        draft["recommended_lane"] = "local-website-draft-preview"
        draft["approval_required"] = False
        draft["plan_steps"] = [
            "Pripremiti lokalni site concept ili implementation prompt.",
            "Zadržati rezultat kao lokalni draft/artefakt.",
            "Ne deployati, ne pushati i ne objavljivati bez approvala.",
        ]
        draft["expected_artifacts"] = ["local_website_draft_card", "implementation_goal_prompt"]
    if any(token in normalized for token in ["rundown", "show tasks", "prikaži", "prikazi", "show", "status", "stanje", "zadnje", "otvori lokalni", "local status"]):
        draft["classification"] = "read_only_operator_command"
        draft["risk_class"] = "safe_local"
        draft["recommended_lane"] = "read-only-status"
        draft["approval_required"] = False
        draft["plan_steps"] = [
            "Dohvatiti lokalni status ili postojeći artefakt.",
            "Prikazati rezultat u command preview kartici.",
            "Ne izvršiti nikakvu vanjsku ili rizičnu akciju.",
        ]
    if "draft board meeting" in normalized or "board meeting" in normalized:
        draft["classification"] = "board_meeting_draft"
        draft["risk_class"] = "safe_local"
        draft["recommended_lane"] = "board-meeting-draft"
        draft["approval_required"] = False
        draft["plan_steps"] = [
            "Pripremiti lokalni Board Meeting draft.",
            "Spremiti samo lokalni artefakt/task ako operator klikne draft flow.",
            "Ne izvršiti javne, credential ili deploy akcije.",
        ]
    if "summarize business pulse" in normalized or "business pulse" in normalized:
        draft["classification"] = "business_pulse_preview"
        draft["risk_class"] = "safe_local"
        draft["recommended_lane"] = "read-only-business-pulse"
        draft["approval_required"] = False
        draft["plan_steps"] = [
            "Sažeti dostupne lokalne task/artefakt signale.",
            "Označiti nedostupne connectore kao approval_required/not_connected.",
            "Ne kontaktirati CRM/email/calendar ili vanjske API-je.",
        ]
    if any(token in normalized for token in ["sigurnosni", "security", "pentest", "penetration", "ranjiv", "vulnerability", "exploit", "scan klijent", "skeniraj"]):
        draft["classification"] = "security_gated"
        draft["risk_class"] = "security_gated"
        draft["recommended_lane"] = "security-scope-gate"
        draft["approval_required"] = True
        draft["plan_steps"] = [
            "Prikazati security scope i authorization zahtjev u command preview kartici.",
            "Ne pokretati aktivni scan ili test bez eksplicitnog scope/legal approvala.",
            "Dopustiti samo jasno označene read-only provjere nakon approval gatea.",
        ]
    if any(token in normalized for token in ["deploy", "deployaj", "push", "pr ", "pull request", "objavi", "pošalji", "posalji", "email"]):
        draft["classification"] = "public_outbound_gated"
        draft["risk_class"] = "public_gated"
        draft["recommended_lane"] = "public-action-approval"
        draft["approval_required"] = True
        draft["plan_steps"] = [
            "Prikazati namjeru i rizičnu radnju u command preview kartici.",
            "Ne izvršiti javnu, deploy, push ili outbound akciju iz glasa.",
            "Čekati eksplicitno operator odobrenje prije side-effecta.",
        ]
    return draft


def jarvis_preview_payload(paths: AgentsOSPaths, data: dict[str, Any]) -> dict[str, Any]:
    transcript_text = (data.get("transcript_text") or data.get("text") or "").strip()
    if not transcript_text:
        raise ValueError("transcript_text is required")
    draft = _jarvis_preview_from_text(transcript_text)
    command_card = {
        "heard": transcript_text,
        "interpreted_intent": draft["classification"],
        "risk_class": draft["risk_class"],
        "proposed_action": draft["recommended_lane"],
        "approval_required": draft["approval_required"],
        "expected_output": draft["expected_artifacts"],
        "execution_created": False,
        "allowed_now": draft["risk_class"] == "safe_local",
        "source_video": "https://youtu.be/wX0YzAYywmI",
        "policy": "preview_only_deterministic_gate",
        "side_effects": False,
    }
    return {
        "local_only": True,
        "execution_created": False,
        "transcript_text": transcript_text,
        "command_card": command_card,
        "draft": draft,
        "audit": {"agents_os_home": str(paths.root), "created_at": utc_now(), "policy": "preview_only"},
    }


def jarvis_commands_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    with connect(paths) as conn:
        from hermes_cli.agents_os_commands import ensure_schema
        ensure_schema(conn)
        ids = [row[0] for row in conn.execute("SELECT id FROM jarvis_commands ORDER BY created_at DESC LIMIT 40").fetchall()]
        items = [get_command(conn, command_id) for command_id in ids]
    return {"items": items, "count": len(items), "real_execution_supported": True}


def jarvis_create_command_action(service: AgentsOSService, data: dict[str, Any]) -> dict[str, Any]:
    preview = jarvis_preview_payload(service.paths, data)
    card = preview["command_card"]
    idempotency_key = str(data.get("idempotency_key") or f"jarvis-ui-{uuid.uuid4().hex}").strip()
    with connect(service.paths) as conn:
        from hermes_cli.agents_os_commands import ensure_schema
        ensure_schema(conn)
        existing = conn.execute("SELECT id FROM jarvis_commands WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        if existing:
            return {"command": get_command(conn, existing[0]), "preview": preview, "deduped": True}
        task_id = f"task-jarvis-{uuid.uuid4().hex[:10]}"
        now = utc_now()
        approval_required = bool(card["approval_required"])
        conn.execute(
            """INSERT INTO tasks(id,title,status,workflow,priority,created_at,updated_at,notes,route,approval_required)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (task_id, f"Jarvis: {preview['transcript_text'][:100]}", "needs_approval" if approval_required else "ready",
             "jarvis-command", 2, now, now, preview["transcript_text"],
             "approval_gate" if approval_required else "runtime:operator-selected", int(approval_required)),
        )
        command = create_command(
            conn, transcript=preview["transcript_text"], idempotency_key=idempotency_key,
            risk_class=str(card["risk_class"]), approval_required=approval_required,
            intent={"classification": card["interpreted_intent"], "proposed_action": card["proposed_action"]},
            metadata={"task_id": task_id, "workflow": "jarvis-command", "profile_id": "doni", "source": "jarvis_ui"},
        )
        log_event(conn, "jarvis_command_created", task_id=task_id,
                  payload={"command_id": command["id"], "risk_class": card["risk_class"], "approval_required": approval_required})
        conn.commit()
    return {"command": command, "preview": preview, "deduped": False}


def jarvis_start_command_action(service: AgentsOSService, command_id: str, data: dict[str, Any]) -> dict[str, Any]:
    with connect(service.paths) as conn:
        command = get_command(conn, command_id)
        task_id = (command.get("metadata") or {}).get("task_id")
        expected = int(data.get("expected_version", command["version"]))
        if command["state"] == "draft":
            if command["approval_required"]:
                approval_id = f"approval-{uuid.uuid4().hex[:10]}"
                now = utc_now()
                conn.execute(
                    "INSERT INTO approvals(id,title,status,risk,task_id,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                    (approval_id, f"Jarvis command {command_id}", "pending", command["risk_class"], task_id,
                     json.dumps({"command_id": command_id, "transcript": command["transcript"]}, ensure_ascii=False), now),
                )
                command = confirm_command(conn, command_id, expected_version=expected, approval_id=approval_id)
                log_event(conn, "jarvis_approval_requested", task_id=task_id,
                          payload={"command_id": command_id, "approval_id": approval_id})
                conn.commit()
                return {"command": command, "execution": None, "approval_required": True}
            command = confirm_command(conn, command_id, expected_version=expected)
            conn.commit()
        elif command["state"] not in {"queued"}:
            return {"command": command, "execution": execution_projection(conn, command.get("run_id")) if command.get("run_id") else None}
    allowed = resolve_allowed_cwds(service.paths)
    requested = Path(str(data.get("cwd") or allowed[0])).expanduser().resolve(strict=True)
    if requested not in allowed:
        raise ValueError("cwd is not in AGENTS_OS_RUNTIME_CWDS")
    coordinator = execution_coordinator(service)
    execution = coordinator.queue(
        command_id=command_id, runtime=str(data.get("runtime") or "hermes"), cwd=requested,
        approved_model_call=data.get("approved_model_call") is True,
        timeout_seconds=float(data.get("timeout_seconds") or 300.0),
        options={"max_budget_usd": float(data.get("max_budget_usd") or 1.0), "allowed_tools": data.get("allowed_tools") or []},
    )
    with connect(service.paths) as conn:
        command = get_command(conn, command_id)
    return {"command": command, "execution": execution, "approval_required": False}


def jarvis_cancel_command_action(service: AgentsOSService, command_id: str, data: dict[str, Any]) -> dict[str, Any]:
    with connect(service.paths) as conn:
        command = get_command(conn, command_id)
    if not command.get("run_id"):
        from hermes_cli.agents_os_commands import cancel_command
        with connect(service.paths) as conn:
            cancelled = cancel_command(conn, command_id, expected_version=int(data.get("expected_version", command["version"])), reason=str(data.get("reason") or "operator"))
            task_id = (cancelled.get("metadata") or {}).get("task_id")
            if task_id:
                conn.execute("UPDATE tasks SET status='cancelled',updated_at=? WHERE id=? AND status NOT IN ('completed','cancelled')", (utc_now(), task_id))
                log_event(conn, "jarvis_command_cancelled", task_id=task_id,
                          payload={"command_id": command_id, "reason": str(data.get("reason") or "operator")})
            conn.commit()
        return {"command": cancelled, "execution": None}
    cancelled = execution_coordinator(service).cancel(
        run_id=command["run_id"], command_id=command_id,
        expected_version=int(data.get("expected_version", command["version"])),
        reason=str(data.get("reason") or "operator"),
    )
    with connect(service.paths) as conn:
        execution = execution_projection(conn, command["run_id"])
    return {"command": cancelled, "execution": execution}


def resolve_web_approval_action(service: AgentsOSService, approval_id: str, data: dict[str, Any]) -> dict[str, Any]:
    decision = str(data.get("decision") or "").lower()
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    with connect(service.paths) as conn:
        old = conn.row_factory
        conn.row_factory = sqlite3.Row
        try:
            approval = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        finally:
            conn.row_factory = old
        if approval is None:
            raise ValueError("approval not found")
        if approval["status"] != "pending":
            raise ValueError(f"approval is already {approval['status']}")
        payload = json.loads(approval["payload"] or "{}")
        command_id = payload.get("command_id")
        now = utc_now()
        conn.execute("UPDATE approvals SET status=?,resolved_at=? WHERE id=?", (decision, now, approval_id))
        if approval["task_id"]:
            conn.execute(
                "UPDATE tasks SET status=?,approval_required=0,updated_at=? WHERE id=?",
                ("ready" if decision == "approved" else "cancelled", now, approval["task_id"]),
            )
        command = None
        if command_id:
            from hermes_cli.agents_os_commands import resolve_command_approval
            current = get_command(conn, command_id)
            command = resolve_command_approval(
                conn, command_id, expected_version=current["version"],
                approved=decision == "approved", approval_id=approval_id,
            )
        log_event(conn, "approval_resolved", task_id=approval["task_id"],
                  payload={"approval_id": approval_id, "decision": decision, "command_id": command_id, "operator": "mission-control"})
        conn.commit()
    return {"approval_id": approval_id, "decision": decision, "command": command, "execution_created": False}


def _jarvis_stt_payload(data: dict[str, Any], audio_path: Path | None = None) -> dict[str, Any]:
    provided = (data.get("transcript_text") or data.get("text") or "").strip()
    if provided:
        return {"provider": "provided_transcript", "text": provided, "confidence": None, "status": "provided"}
    stt_result = data.get("stt_result") if isinstance(data.get("stt_result"), dict) else {}
    stt_text = (stt_result.get("text") or "").strip()
    if stt_text:
        return {
            "provider": stt_result.get("provider") or "external_stt_adapter",
            "text": stt_text,
            "confidence": stt_result.get("confidence"),
            "status": "transcribed",
        }
    if data.get("use_local_stt") and audio_path is not None:
        try:
            return _transcribe_with_local_faster_whisper(
                str(audio_path),
                model=str(data.get("stt_model") or "base"),
                language=str(data.get("stt_language") or "hr"),
            )
        except Exception as exc:
            return {
                "provider": "local-faster-whisper",
                "text": "[stt_pending] Local STT failed; audio artifact was saved for retry.",
                "confidence": None,
                "status": "error",
                "error": exc.__class__.__name__,
                "message": str(exc),
            }
    return {
        "provider": "stub_pending",
        "text": "[stt_pending] Audio captured; STT backend not connected in this local slice.",
        "confidence": None,
        "status": "pending",
    }


def _transcribe_with_local_faster_whisper(audio_path: str, *, model: str = "base", language: str = "hr") -> dict[str, Any]:
    from faster_whisper import WhisperModel  # type: ignore[import-not-found]

    whisper = WhisperModel(model, device="cpu", compute_type="int8")
    segments, info = whisper.transcribe(audio_path, beam_size=5, language=language or None, vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments).strip()
    return {
        "provider": "local-faster-whisper",
        "text": text or "[stt_empty] Local STT produced no transcript.",
        "confidence": getattr(info, "language_probability", None),
        "status": "transcribed" if text else "empty",
        "language": getattr(info, "language", None),
        "model": model,
    }


def jarvis_model_advisor_payload(paths: AgentsOSPaths, data: dict[str, Any]) -> dict[str, Any]:
    transcript_text = (data.get("transcript_text") or data.get("text") or "").strip()
    if not transcript_text:
        raise ValueError("transcript_text is required")
    deterministic = data.get("deterministic_preview") if isinstance(data.get("deterministic_preview"), dict) else jarvis_preview_payload(paths, {"transcript_text": transcript_text})
    command_card = dict(deterministic.get("command_card") or {})
    model_result = data.get("model_result") if isinstance(data.get("model_result"), dict) else {}
    model_risk = model_result.get("risk_class")
    authoritative_risk = command_card.get("risk_class")
    semantic_intent = model_result.get("semantic_intent") or command_card.get("interpreted_intent")
    voice_reply = model_result.get("voice_reply_short") or _jarvis_voice_reply(command_card)
    command_card["semantic_intent"] = semantic_intent
    command_card["voice_reply_short"] = voice_reply
    command_card["risk_class"] = authoritative_risk
    command_card["approval_required"] = bool(command_card.get("approval_required"))
    command_card["execution_created"] = False
    return {
        "local_only": True,
        "execution_created": False,
        "provider": data.get("provider") or "deterministic",
        "model": data.get("model") or "none",
        "transcript_text": transcript_text,
        "authoritative_risk_class": authoritative_risk,
        "model_risk_class": model_risk,
        "risk_disagreement": bool(model_risk and model_risk != authoritative_risk),
        "command_card": command_card,
        "model_result": model_result,
        "audit": {"agents_os_home": str(paths.root), "created_at": utc_now(), "policy": "deterministic_gate_authoritative"},
    }


def _jarvis_voice_reply(command_card: dict[str, Any]) -> str:
    if command_card.get("approval_required"):
        return "Ovo treba odobrenje. Pripremio sam preview, ništa ne izvršavam."
    return "Ovo je sigurno lokalno. Pripremio sam preview, bez izvršavanja."


def _jarvis_cleaned_transcript(raw_text: str, data: dict[str, Any]) -> str:
    model_result = data.get("model_result") if isinstance(data.get("model_result"), dict) else {}
    cleaned = (
        model_result.get("normalized_transcript")
        or model_result.get("cleaned_transcript")
        or model_result.get("cleaned_text")
        or data.get("cleaned_transcript")
        or data.get("cleaned_text")
        or raw_text
    )
    return str(cleaned).strip() or raw_text


def _jarvis_gate_text(raw_text: str, cleaned_text: str) -> str:
    if cleaned_text and cleaned_text != raw_text:
        return f"{raw_text}\n{cleaned_text}"
    return raw_text


def _hume_octave_request(text: str, data: dict[str, Any]) -> dict[str, Any]:
    voice_description = data.get("voice_description") or data.get("description") or "calm Croatian operator voice, concise, warm, and clear"
    fmt = data.get("format") or data.get("audio_format") or "mp3"
    return {
        "utterances": [
            {
                "text": text,
                "description": voice_description,
            }
        ],
        "format": {"type": fmt},
        "num_generations": int(data.get("num_generations") or 1),
    }


def jarvis_reply_payload(paths: AgentsOSPaths, data: dict[str, Any]) -> dict[str, Any]:
    text = (data.get("text") or data.get("voice_reply_short") or "").strip()
    if not text:
        raise ValueError("text is required")
    stamp = _jarvis_slug_from_time()
    reply_dir = paths.artifacts / "jarvis_replies"
    audio_dir = paths.artifacts / "jarvis_reply_audio"
    reply_dir.mkdir(parents=True, exist_ok=True)
    audio_dir.mkdir(parents=True, exist_ok=True)
    audio_bytes = _decode_optional_audio(data)
    audio_path: Path | None = None
    if audio_bytes:
        audio_path = audio_dir / f"{stamp}-jarvis-reply{_jarvis_audio_suffix(data.get('audio_mime'))}"
        audio_path.write_bytes(audio_bytes)
    provider = data.get("provider") or ("hermes-tts" if audio_bytes else "text-only-fallback")
    hume_request = _hume_octave_request(text, data) if provider == "hume-octave" else None
    hume_key_present = bool(os.environ.get("HUME_API_KEY"))
    reply_path = reply_dir / f"{stamp}-jarvis-reply.md"
    status = "audio_ready" if audio_path else ("provider_unconfigured" if provider == "hume-octave" and not hume_key_present else "text_only")
    payload = {
        "local_only": True,
        "execution_created": False,
        "status": status,
        "text": text,
        "audio_artifact_path": str(audio_path) if audio_path else None,
        "tts": {
            "provider": provider,
            "mime": data.get("audio_mime") if audio_path else None,
            "fallback": audio_path is None,
            "requires_api_key": provider == "hume-octave",
            "api_key_present": hume_key_present if provider == "hume-octave" else None,
            "api_called": False,
            "supported_formats": ["mp3", "wav", "pcm"] if provider == "hume-octave" else None,
        },
    }
    if hume_request:
        payload["hume_octave_request"] = hume_request
    reply_path.write_text(f"# Jarvis voice reply\n\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```\n", encoding="utf-8")
    artifact_id = f"artifact-jarvis-reply-{stamp}"
    with connect(paths) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO artifacts(id,kind,title,path,task_id,workflow,created_at) VALUES(?,?,?,?,?,?,?)",
            (artifact_id, "jarvis_voice_reply", "Jarvis voice reply", str(reply_path), None, "jarvis-voice-reply", utc_now()),
        )
        log_event(conn, "jarvis_voice_reply_created", payload={"artifact_id": artifact_id, "audio_path": str(audio_path) if audio_path else None, "execution_created": False})
        conn.commit()
    return {**payload, "reply_artifact_path": str(reply_path), "artifact_id": artifact_id}


def jarvis_transcribe_payload(paths: AgentsOSPaths, data: dict[str, Any]) -> dict[str, Any]:
    """Persist a local push-to-talk artefact and return transcript + intent preview.

    This v0.1 endpoint accepts browser audio plus an optional transcript stub. It
    deliberately does not execute commands; real STT can replace the transcript
    stub behind the same payload contract.
    """
    stamp = _jarvis_slug_from_time()
    audio_bytes = _decode_optional_audio(data)
    suffix = _jarvis_audio_suffix(data.get("audio_mime"))
    audio_dir = paths.artifacts / "jarvis_audio"
    transcript_dir = paths.artifacts / "jarvis_transcripts"
    audio_dir.mkdir(parents=True, exist_ok=True)
    transcript_dir.mkdir(parents=True, exist_ok=True)
    audio_path = audio_dir / f"{stamp}-jarvis-command{suffix}"
    transcript_path = transcript_dir / f"{stamp}-jarvis-transcript.md"
    audio_path.write_bytes(audio_bytes)
    stt = _jarvis_stt_payload(data, audio_path)
    transcript_text = stt["text"]
    cleaned_text = _jarvis_cleaned_transcript(transcript_text, data)
    gate_text = _jarvis_gate_text(transcript_text, cleaned_text)
    preview = jarvis_preview_payload(paths, {"transcript_text": gate_text})
    preview["command_card"]["heard"] = transcript_text
    preview["command_card"]["cleaned_text"] = cleaned_text
    preview["command_card"]["gate_text"] = gate_text
    advisor = jarvis_model_advisor_payload(
        paths,
        {
            "transcript_text": transcript_text,
            "deterministic_preview": preview,
            "model_result": data.get("model_result") if isinstance(data.get("model_result"), dict) else {},
            "provider": data.get("advisor_provider") or data.get("provider") or "deterministic",
            "model": data.get("advisor_model") or data.get("model") or "none",
        },
    )
    transcript_body = {
        "local_only": True,
        "execution_created": False,
        "stt": stt,
        "advisor": {"provider": advisor["provider"], "model": advisor["model"], "risk_disagreement": advisor["risk_disagreement"]},
        "transcript": {"text": transcript_text, "cleaned_text": cleaned_text, "source": stt["provider"], "created_at": utc_now()},
        "intent_preview": advisor["command_card"],
        "audio_artifact_path": str(audio_path),
    }
    transcript_path.write_text(f"# Jarvis transcript\n\n```json\n{json.dumps(transcript_body, ensure_ascii=False, indent=2)}\n```\n", encoding="utf-8")
    artifact_id = f"artifact-jarvis-transcript-{stamp}"
    with connect(paths) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO artifacts(id,kind,title,path,task_id,workflow,created_at) VALUES(?,?,?,?,?,?,?)",
            (artifact_id, "jarvis_transcript", "Jarvis transcript", str(transcript_path), None, "jarvis-push-to-talk", utc_now()),
        )
        log_event(conn, "jarvis_transcribed", payload={"artifact_id": artifact_id, "audio_path": str(audio_path), "execution_created": False})
        conn.commit()
    return {
        "status": "transcribed",
        "local_only": True,
        "execution_created": False,
        "audio_artifact_path": str(audio_path),
        "transcript_artifact_path": str(transcript_path),
        "transcript": transcript_body["transcript"],
        "stt": stt,
        "advisor": transcript_body["advisor"],
        "intent_preview": advisor["command_card"],
        "command_card": advisor["command_card"],
        "artifact_id": artifact_id,
    }


def operator_loop_payload(service: AgentsOSService) -> dict[str, Any]:
    dashboard = service.dashboard_payload()
    tasks = dashboard.get("tasks", [])
    reviews = dashboard.get("reviews", [])
    events = dashboard.get("events", [])
    judge_events = [event for event in events if "judge" in event.get("event_type", "") or "review" in event.get("event_type", "")]
    return {
        "local_only": True,
        "acceptance_criteria": ["evidence exists", "tests/smoke recorded", "approval gates respected"],
        "task_detail_available": True,
        "tasks": tasks,
        "reviews": reviews,
        "evidence_links": dashboard.get("recent_completions", []),
        "judge_status": "ready" if reviews or judge_events else "pending",
        "judge_results_faked": False,
        "blocked_reason": None,
    }


def _write_artifact(paths: AgentsOSPaths, title: str, body: str, *, kind: str, task_id: str | None = None, workflow: str | None = None) -> tuple[str, str]:
    suffix = uuid.uuid4().hex[:8]
    artifact_id = f"artifact-{slugify(title)[:24]}-{suffix}"
    target = paths.artifacts / kind / f"{utc_now().split('T', 1)[0]}-{slugify(title)}-{suffix}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    text = f"# {title}\n\n{body}\n"
    target.write_text(text, encoding="utf-8")
    with connect(paths) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO artifacts(id,kind,title,path,task_id,workflow,created_at) VALUES(?,?,?,?,?,?,?)",
            (artifact_id, kind, title, str(target), task_id, workflow, utc_now()),
        )
        log_event(conn, "artifact_created", task_id=task_id, payload={"artifact_id": artifact_id, "path": str(target), "source": "mission_control_web"})
        conn.commit()
    return artifact_id, str(target)


def governance_status_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    artifacts = artifacts_payload(paths).get("items", [])[:20]
    if not artifacts:
        artifacts = [{
            "id": "governance-runtime-map",
            "kind": "virtual_system_map",
            "title": "Agents OS runtime governance map",
            "path": None,
        }]
    return {
        "status": "ok",
        "local_only": True,
        "read_only": True,
        "execution_created": False,
        "external_calls": False,
        "artifacts": artifacts,
        "actions": [
            {"id": "artifact_preview", "label": "Preview governance artifact", "safe_local": True},
            {"id": "gated_approval_draft", "label": "Draft approval: deploy / public publish", "safe_local": True},
            {"id": "create_local_e2e_task", "label": "Create local E2E proof task", "safe_local": True},
        ],
        "boundaries": ["no deploy", "no public publish", "no credentials", "no gateway restart"],
    }


def governance_action_payload(paths: AgentsOSPaths, data: dict[str, Any]) -> dict[str, Any]:
    action = str(data.get("action") or "")
    if action == "artifact_preview":
        detail = artifact_detail_payload(paths, str(data.get("artifact_id") or ""))
        return {
            **detail,
            "status": "preview_ready" if detail.get("status") == "ok" else "not_found",
            "local_only": True,
            "execution_created": False,
            "external_calls": False,
        }
    if action not in {"gated_approval_draft", "create_local_e2e_task"}:
        return {"status": "rejected", "reason": "unsupported_governance_action", "local_only": True, "execution_created": False}

    suffix = uuid.uuid4().hex[:10]
    task_id = f"task-gov-{suffix}"
    now = utc_now()
    label = str(data.get("label") or action).strip()[:240]
    gated = action == "gated_approval_draft"
    approval_id = f"approval-gov-{suffix}" if gated else None
    status = "needs_approval" if gated else "pending"
    route = "approval_gate" if gated else "local:governance-proof"
    with connect(paths) as conn:
        conn.execute(
            "INSERT INTO tasks(id,title,status,workflow,priority,created_at,updated_at,notes,route,approval_required) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, label, status, "governance", 2, now, now, "Mission Control governance action", route, 1 if gated else 0),
        )
        if approval_id:
            conn.execute(
                "INSERT INTO approvals(id,title,status,risk,task_id,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (approval_id, f"Governance approval required: {label}", "pending", "external-action", task_id, json.dumps(data, ensure_ascii=False), now),
            )
            log_event(conn, "approval_requested", task_id=task_id, payload={"approval_id": approval_id, "source": "governance", "execution_created": False})
        conn.commit()
    artifact_id, artifact_path = _write_artifact(
        paths,
        label,
        f"- action: {action}\n- local_only: true\n- execution_created: false\n- approval_required: {str(gated).lower()}",
        kind="governance",
        task_id=task_id,
        workflow="governance",
    )
    return {
        "status": "approval_drafted" if gated else "local_task_created",
        "local_only": True,
        "execution_created": False,
        "external_calls": False,
        "task_id": task_id,
        "approval_id": approval_id,
        "artifact_id": artifact_id,
        "artifact_path": artifact_path,
    }


def create_idea_action(service: AgentsOSService, data: dict[str, Any]) -> dict[str, Any]:
    draft = service.idea_factory_draft_payload(data)
    paths = service.paths
    title = data.get("title") or f"Idea Factory: {data.get('idea_text', '')[:70]}"
    task_id = f"task-{draft['idea_id'].replace('idea-', '')}"
    now = utc_now()
    approval_id = None
    mode = "safe_local_task"
    status = "pending"
    approval_required = 0
    if draft["approval_required"]:
        mode = "approval_draft"
        status = "needs_approval"
        approval_required = 1
        approval_id = f"approval-{draft['idea_id'].replace('idea-', '')}"
    body = json.dumps({"idea": data, "draft": draft, "mode": mode, "execution_created": False}, ensure_ascii=False, indent=2)
    with connect(paths) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO tasks(id,title,status,workflow,priority,created_at,updated_at,notes,route,approval_required) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (task_id, title, status, draft["recommended_lane"], 2, now, now, data.get("idea_text", ""), "approval_gate" if approval_required else "local:direct", approval_required),
        )
        if approval_id:
            conn.execute(
                "INSERT OR REPLACE INTO approvals(id,title,status,risk,task_id,payload,created_at) VALUES(?,?,?,?,?,?,?)",
                (approval_id, f"Approval draft: {title}", "pending", draft["risk_class"], task_id, body, now),
            )
            log_event(conn, "approval_requested", task_id=task_id, payload={"approval_id": approval_id, "risk": draft["risk_class"], "execution_created": False})
        conn.commit()
    artifact_id, artifact_path = _write_artifact(paths, title, f"```json\n{body}\n```", kind="idea_factory", task_id=task_id, workflow=draft["recommended_lane"])
    with connect(paths) as conn:
        log_event(conn, "idea_action_created", task_id=task_id, payload={"mode": mode, "draft": draft, "artifact_id": artifact_id})
        conn.commit()
    return {"mode": mode, "task_id": task_id, "approval_id": approval_id, "artifact_id": artifact_id, "artifact_path": artifact_path, "draft": draft, "execution_created": False}


def _redact_collection_payloads(items: list[dict[str, Any]], *fields: str) -> list[dict[str, Any]]:
    redacted = []
    for item in items:
        copy = dict(item)
        for field in fields:
            if field in copy:
                copy[f"{field}_preview"] = _safe_json_preview(copy.pop(field))
        redacted.append(copy)
    return redacted


def _risk_taxonomy(risk: str | None, payload_preview: Any) -> dict[str, Any]:
    risk_value = (risk or "unknown").lower()
    text = json.dumps(payload_preview, ensure_ascii=False).lower() if payload_preview is not None else ""
    flags = []
    if "external" in risk_value or "public" in risk_value:
        flags.append("external_or_public_action")
    if any(word in text for word in ["deploy", "push", "publish", "email", "send"]):
        flags.append("outbound_or_publish_intent")
    if payload_preview == "[redacted-sensitive-preview]":
        flags.append("sensitive_payload_redacted")
    if not flags:
        flags.append("manual_review_required")
    severity = "high" if any(flag in flags for flag in ["external_or_public_action", "sensitive_payload_redacted"]) else "medium"
    return {"risk": risk or "unknown", "severity": severity, "flags": flags, "deterministic": True}


def task_detail_payload(paths: AgentsOSPaths, task_id: str) -> dict[str, Any]:
    with connect(paths) as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            return {"status": "not_found", "task_id": task_id, "local_only": True, "read_only": True}
        approvals = [row_to_dict(r) for r in conn.execute("SELECT * FROM approvals WHERE task_id=? ORDER BY created_at DESC", (task_id,)).fetchall()]
        runs = [row_to_dict(r) for r in conn.execute("SELECT * FROM runs WHERE task_id=? ORDER BY created_at DESC", (task_id,)).fetchall()]
        events = [row_to_dict(r) for r in conn.execute("SELECT * FROM events WHERE task_id=? ORDER BY created_at DESC", (task_id,)).fetchall()]
        artifacts = [row_to_dict(r) for r in conn.execute("SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at DESC", (task_id,)).fetchall()]
        reviews = [row_to_dict(r) for r in conn.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY created_at DESC", (task_id,)).fetchall()]
    task_payload = row_to_dict(task)
    task_payload["approval_required"] = bool(task_payload.get("approval_required"))
    return {
        "status": "ok",
        "local_only": True,
        "read_only": True,
        "task": task_payload,
        "relationships": {"parent": None, "children": [], "dependencies": []},
        "dependency_status": "not_modeled",
        "acceptance_criteria": ["planned", "implemented", "verified", "evidence linked"],
        "approvals": _redact_collection_payloads(approvals, "payload"),
        "runs": _redact_collection_payloads(runs, "input"),
        "events": _redact_collection_payloads(events, "payload"),
        "artifacts": artifacts,
        "reviews": reviews,
        "evidence_summary": {"artifact_count": len(artifacts), "review_count": len(reviews), "event_count": len(events)},
        "safe_actions": ["copy_task_id", "open_related_artifact_read_only"],
        "mutation_actions_enabled": False,
        "judge_status": "pending" if not reviews else "review_available",
    }


def approval_detail_payload(paths: AgentsOSPaths, approval_id: str) -> dict[str, Any]:
    with connect(paths) as conn:
        approval = conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if approval is None:
            return {"status": "not_found", "approval_id": approval_id, "local_only": True, "read_only": True}
        item = row_to_dict(approval)
        task = conn.execute("SELECT id,title,status,workflow,approval_required FROM tasks WHERE id=?", (item.get("task_id"),)).fetchone() if item.get("task_id") else None
    payload_preview = _safe_json_preview(item.pop("payload", None))
    return {
        "status": "ok",
        "local_only": True,
        "read_only": True,
        "credentials_visible": False,
        "resolution_enabled": item.get("status") == "pending",
        "approval": {**item, "payload_preview": payload_preview},
        "task_summary": row_to_dict(task) if task else None,
        "risk_taxonomy": _risk_taxonomy(item.get("risk"), payload_preview),
        "stale_warning": item.get("status") == "pending",
        "blocked_actions": ["execute_without_resolution"],
        "allowed_now": item.get("status") == "pending",
        "next_required_human_decision": "Explicit operator approval is required before any approve/deny/resolve action.",
    }


def run_detail_payload(paths: AgentsOSPaths, run_id: str) -> dict[str, Any]:
    with connect(paths) as conn:
        run = conn.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            return {"status": "not_found", "run_id": run_id, "local_only": True, "read_only": True}
        run_item = row_to_dict(run)
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (run_item.get("task_id"),)).fetchone() if run_item.get("task_id") else None
        events = [row_to_dict(r) for r in conn.execute("SELECT * FROM events WHERE run_id=? ORDER BY created_at DESC", (run_id,)).fetchall()]
        artifacts = [row_to_dict(r) for r in conn.execute("SELECT * FROM artifacts WHERE run_id=? ORDER BY created_at DESC", (run_id,)).fetchall()]
        execution = execution_projection(conn, run_id)
    return {"status": "ok", "local_only": True, "read_only": True, "run": _redact_collection_payloads([run_item], "input")[0], "execution": execution, "task": row_to_dict(task) if task else None, "events": _redact_collection_payloads(events, "payload"), "artifacts": artifacts, "mutation_actions_enabled": bool(execution and execution.get("status") in {"queued","running"})}


def _artifact_credential_like_path(value: str) -> bool:
    lowered = str(value).lower()
    return any(marker in lowered for marker in (".env", "auth.json", "token", "secret", "credential", "password", "api_key", "apikey", "cookie"))


def _artifact_allowed(path: Path, paths: AgentsOSPaths) -> bool:
    allowed_roots = [paths.artifacts, paths.vault_root]
    extra_root = os.environ.get("AGENTS_OS_ARTIFACT_PREVIEW_ROOT")
    if extra_root:
        allowed_roots.append(Path(extra_root))
    try:
        resolved = path.resolve()
    except OSError:
        return False
    return any(resolved == root.resolve() or root.resolve() in resolved.parents for root in allowed_roots if root.exists())


def artifact_detail_payload(paths: AgentsOSPaths, artifact_id: str) -> dict[str, Any]:
    with connect(paths) as conn:
        row = conn.execute("SELECT * FROM artifacts WHERE id=?", (artifact_id,)).fetchone()
        if row is None:
            return {"status": "not_found", "artifact_id": artifact_id, "local_only": True, "read_only": True}
        item = row_to_dict(row)
    path = Path(item.get("path") or "")
    info = _path_info(str(path))
    preview = None
    preview_status = "not_previewed"
    if info["exists"] and info["suffix"] in {".md", ".txt", ".json", ".log"} and _artifact_allowed(path, paths) and not _artifact_credential_like_path(str(path)):
        preview = _safe_json_preview(path.read_text(errors="replace"), limit=2500)
        preview_status = "ok"
    elif _artifact_credential_like_path(str(path)):
        preview_status = "blocked_sensitive_path"
    elif not _artifact_allowed(path, paths):
        preview_status = "blocked_outside_allowlist"
    return {"status": "ok", "local_only": True, "read_only": True, "artifact": {**item, **info}, "preview_status": preview_status, "preview": preview, "mutation_actions_enabled": False}



def _evidence_item_summary(task: dict[str, Any], artifacts: list[dict[str, Any]], runs: list[dict[str, Any]], events: list[dict[str, Any]], approvals: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> dict[str, Any]:
    status = task.get("status") or "unknown"
    has_artifact = bool(artifacts)
    has_run = bool(runs)
    has_event = bool(events)
    has_review = bool(reviews)
    approval_required = bool(task.get("approval_required")) or status == "needs_approval" or bool(approvals)
    if approval_required:
        verdict = "gated"
        next_step = "Goran decision required before approval resolution or execution."
    elif has_artifact and (has_event or has_run or has_review):
        verdict = "evidence_linked"
        next_step = "Open drilldown and inspect linked artifact/run/event before closeout."
    elif has_artifact:
        verdict = "artifact_only"
        next_step = "Open artifact preview; add run/event/review evidence before final closeout if needed."
    else:
        verdict = "missing_evidence"
        next_step = "Create or link a local proof artifact before claiming completion."
    return {
        "task_id": task.get("id"),
        "title": task.get("title"),
        "status": status,
        "workflow": task.get("workflow"),
        "priority": task.get("priority"),
        "approval_required": approval_required,
        "verdict": verdict,
        "next_step": next_step,
        "counts": {
            "artifacts": len(artifacts),
            "runs": len(runs),
            "events": len(events),
            "approvals": len(approvals),
            "reviews": len(reviews),
        },
        "latest_artifact_id": artifacts[0].get("id") if artifacts else None,
        "latest_run_id": runs[0].get("id") if runs else None,
        "updated_at": task.get("updated_at") or task.get("created_at"),
    }


def evidence_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    with connect(paths) as conn:
        tasks = [row_to_dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY CASE status WHEN 'ready' THEN 0 WHEN 'pending' THEN 1 WHEN 'needs_approval' THEN 2 WHEN 'review' THEN 3 WHEN 'blocked' THEN 4 WHEN 'completed' THEN 8 ELSE 7 END, priority ASC, created_at DESC LIMIT 80").fetchall()]
        artifact_rows = [row_to_dict(r) for r in conn.execute("SELECT * FROM artifacts ORDER BY created_at DESC LIMIT 200").fetchall()]
        run_rows = [row_to_dict(r) for r in conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 160").fetchall()]
        event_rows = [row_to_dict(r) for r in conn.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT 220").fetchall()]
        approval_rows = [row_to_dict(r) for r in conn.execute("SELECT id,title,status,risk,task_id,created_at,resolved_at FROM approvals ORDER BY created_at DESC LIMIT 160").fetchall()]
        review_rows = [row_to_dict(r) for r in conn.execute("SELECT * FROM reviews ORDER BY created_at DESC LIMIT 160").fetchall()]
    def by_task(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
        out: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            task_id = row.get("task_id")
            if task_id:
                out.setdefault(task_id, []).append(row)
        return out
    artifacts_by_task = by_task(artifact_rows)
    runs_by_task = by_task(run_rows)
    events_by_task = by_task(event_rows)
    approvals_by_task = by_task(approval_rows)
    reviews_by_task = by_task(review_rows)
    items = []
    for task in tasks:
        task["approval_required"] = bool(task.get("approval_required"))
        tid = task.get("id")
        items.append(_evidence_item_summary(
            task,
            artifacts_by_task.get(tid, []),
            runs_by_task.get(tid, []),
            events_by_task.get(tid, []),
            approvals_by_task.get(tid, []),
            reviews_by_task.get(tid, []),
        ))
    counts: dict[str, int] = {}
    for item in items:
        counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
    return {
        "local_only": True,
        "read_only": True,
        "mutation_actions_enabled": False,
        "credentials_visible": False,
        "summary": counts,
        "items": items,
        "acceptance_contract": ["task present", "linked artifact/run/event/review visible", "approval gates respected", "no mutation from UI"],
    }


def evidence_detail_payload(paths: AgentsOSPaths, task_id: str) -> dict[str, Any]:
    detail = task_detail_payload(paths, task_id)
    if detail.get("status") != "ok":
        return {**detail, "evidence_drilldown": True}
    task = detail.get("task") or {}
    artifacts = detail.get("artifacts") or []
    runs = detail.get("runs") or []
    events = detail.get("events") or []
    approvals = detail.get("approvals") or []
    reviews = detail.get("reviews") or []
    summary = _evidence_item_summary(task, artifacts, runs, events, approvals, reviews)
    timeline = []
    for row in artifacts:
        timeline.append({"kind": "artifact", "id": row.get("id"), "created_at": row.get("created_at"), "title": row.get("title"), "path": row.get("path"), "run_id": row.get("run_id")})
    for row in runs:
        timeline.append({"kind": "run", "id": row.get("id"), "created_at": row.get("created_at"), "status": row.get("status"), "workflow": row.get("workflow")})
    for row in events:
        timeline.append({"kind": "event", "id": row.get("id"), "created_at": row.get("created_at"), "event_type": row.get("event_type"), "run_id": row.get("run_id")})
    for row in approvals:
        timeline.append({"kind": "approval", "id": row.get("id"), "created_at": row.get("created_at"), "status": row.get("status"), "risk": row.get("risk")})
    for row in reviews:
        timeline.append({"kind": "review", "id": row.get("id"), "created_at": row.get("created_at"), "status": row.get("status")})
    timeline.sort(key=lambda x: str(x.get("created_at") or ""), reverse=True)
    return {
        "status": "ok",
        "local_only": True,
        "read_only": True,
        "mutation_actions_enabled": False,
        "credentials_visible": False,
        "task": task,
        "summary": summary,
        "timeline": timeline[:80],
        "artifacts": artifacts,
        "runs": runs,
        "events": events,
        "approvals": approvals,
        "reviews": reviews,
        "safe_actions": ["copy_task_id", "open_task_detail", "open_artifact_preview_read_only", "open_run_detail_read_only"],
        "blocked_actions": ["mark_done", "approve", "deny", "execute", "deploy", "push_pr"],
    }


def skills_visibility_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    cached = _payload_cache_get(paths, "skills", 300.0)
    if cached is not None:
        return cached
    skills_root = paths.home / "skills"
    items = []
    scan_truncated = False
    if skills_root.exists():
        skill_files: list[Path] = []
        stack = [skills_root]
        scanned = 0
        while stack and scanned < 1200 and len(skill_files) < 160:
            current = stack.pop()
            try:
                entries = current.iterdir()
            except OSError:
                continue
            for entry in entries:
                scanned += 1
                if scanned >= 1200:
                    break
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    stack.append(entry)
                elif entry.name == "SKILL.md":
                    skill_files.append(entry)
                    if len(skill_files) >= 160:
                        break
        scan_truncated = bool(stack or scanned >= 1200 or len(skill_files) >= 160)
        for skill_file in sorted(skill_files):
            text = skill_file.read_text(errors="replace")[:2000]
            name = skill_file.parent.name
            desc = ""
            for line in text.splitlines():
                if line.startswith("name:"):
                    name = line.split(":",1)[1].strip().strip('"')
                if line.startswith("description:"):
                    desc = line.split(":",1)[1].strip().strip('"')
            items.append({"name": name, "description": desc, "path": str(skill_file), "category": str(skill_file.parent.parent.relative_to(skills_root)) if skill_file.parent.parent != skills_root else "root"})
    return _payload_cache_put(paths, "skills", {"local_only": True, "read_only": True, "content_visible": False, "mutation_actions_enabled": False, "bounded_scan": True, "scan_truncated": scan_truncated, "count": len(items), "items": items})


def sessions_visibility_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    cached = _payload_cache_get(paths, "sessions", 60.0)
    if cached is not None:
        return cached
    sessions_dir = paths.home / "sessions"
    items = []
    if sessions_dir.exists():
        candidates = []
        try:
            for index, file in enumerate(sessions_dir.iterdir()):
                if index >= 500:
                    break
                if file.is_file() and file.suffix == ".json":
                    try:
                        stat = file.stat()
                        candidates.append((stat.st_mtime, file, stat.st_size))
                    except OSError:
                        continue
        except OSError:
            candidates = []
        for modified, file, size in sorted(candidates, reverse=True)[:40]:
            items.append({"file": file.name, "path": str(file), "size_bytes": size, "modified": int(modified), "raw_transcript_visible": False})
    return _payload_cache_put(paths, "sessions", {"local_only": True, "read_only": True, "metadata_only": True, "raw_transcript_visible": False, "mutation_actions_enabled": False, "bounded_scan": True, "count": len(items), "items": items})


def _command_age_label(created_at: Any) -> dict[str, Any]:
    text = str(created_at or "")
    try:
        normalized = text.replace("Z", "+00:00")
        created = datetime.fromisoformat(normalized)
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        age_hours = max(0.0, (datetime.now(timezone.utc) - created.astimezone(timezone.utc)).total_seconds() / 3600.0)
        if age_hours >= 48:
            bucket = "stale_48h_plus"
        elif age_hours >= 24:
            bucket = "stale_24h_plus"
        elif age_hours >= 6:
            bucket = "aging_6h_plus"
        else:
            bucket = "fresh"
        return {"hours": round(age_hours, 1), "bucket": bucket, "created_at": text}
    except Exception:
        return {"hours": None, "bucket": "unknown", "created_at": text}


def _command_item(kind: str, item: dict[str, Any], *, severity: str, reason: str, next_step: str) -> dict[str, Any]:
    age = _command_age_label(item.get("created_at") or item.get("updated_at"))
    return {
        "kind": kind,
        "severity": severity,
        "id": item.get("id"),
        "title": item.get("title") or item.get("id"),
        "status": item.get("status"),
        "workflow": item.get("workflow"),
        "risk": item.get("risk"),
        "task_id": item.get("task_id"),
        "reason": reason,
        "next_step": next_step,
        "age": age,
        "operator_decision_required": severity in {"high", "blocked"} or kind == "approval",
        "mutation_actions_enabled": False,
        "open_target": item.get("task_id") if kind == "approval" and item.get("task_id") else item.get("id"),
    }



def automation_inbox_payload(paths: AgentsOSPaths) -> dict[str, Any]:
    """Read-only Automation Bridge v0 inbox for Mission Control."""
    items: list[dict[str, Any]] = []
    with connect(paths) as conn:
        rows = conn.execute(
            "SELECT * FROM automation_intakes ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
        for row in rows:
            item = row_to_dict(row)
            for key in ("payload", "result"):
                try:
                    item[key] = json.loads(item.get(key) or "{}")
                except json.JSONDecodeError:
                    item[key] = {"raw": item.get(key)}
            items.append(item)
    return {
        "local_only": True,
        "mutation_actions_enabled": True,
        "external_callbacks_executed": False,
        "count": len(items),
        "items": items,
        "contract": {
            "intake_schema": "automation-intake.v0",
            "result_schema": "automation-result.v0",
            "dedup_key": "source:event_id",
            "approval_boundary": "external callbacks are draft/approval-gated only",
        },
    }


def _command_center_payload_uncached(service: AgentsOSService) -> dict[str, Any]:
    dashboard = cached_dashboard_payload(service)
    paths = service.paths
    tasks = tasks_payload(paths)
    approvals = approvals_payload(paths)
    artifacts = artifacts_payload(paths)
    safety = safety_payload(service)
    queue = dashboard.get("queue_summary") or {}
    task_items = tasks.get("items") or []
    approval_items = approvals.get("items") or []
    artifact_items = artifacts.get("items") or []
    ready_tasks = [t for t in task_items if t.get("status") in {"ready", "pending"} and not t.get("approval_required")]
    real_work_tasks = [t for t in task_items if (t.get("queue_hygiene") or {}).get("queue_class") == "operator_actionable"]
    proof_generated_tasks = [t for t in task_items if (t.get("queue_hygiene") or {}).get("queue_class") == "proof_generated"]
    stale_draft_tasks = [t for t in task_items if (t.get("queue_hygiene") or {}).get("queue_class") == "stale_draft"]
    gated_tasks = [t for t in task_items if (t.get("queue_hygiene") or {}).get("queue_class") == "approval_gated"]
    blocked_tasks = [t for t in task_items if t.get("status") == "blocked"]
    review_tasks = [t for t in task_items if t.get("status") == "review"]
    failed_runs = [r for r in _table_rows(paths, "runs", limit=80) if r.get("status") in {"failed", "blocked"}]
    recent_artifacts = artifact_items[:5]
    pending_approvals = [a for a in approval_items if a.get("status") == "pending"]
    action_required = int(queue.get("action_required") or 0)

    action_queue: list[dict[str, Any]] = []
    for approval in pending_approvals[:8]:
        risk = str(approval.get("risk") or "unknown")
        severity = "high" if any(marker in risk.lower() for marker in ["public", "external", "credential", "deploy", "push"]) else "medium"
        action_queue.append(_command_item(
            "approval",
            approval,
            severity=severity,
            reason=f"Pending approval gate: {risk}",
            next_step="Goran decision required: approve/deny outside this read-only view; no execution from Command Center.",
        ))
    for task in blocked_tasks[:6]:
        action_queue.append(_command_item(
            "blocked_task",
            task,
            severity="blocked",
            reason="Task is blocked in runtime queue.",
            next_step="Open task detail and inspect evidence/dependencies before changing status.",
        ))
    for task in review_tasks[:6]:
        action_queue.append(_command_item(
            "review_task",
            task,
            severity="medium",
            reason="Task is waiting for review/evidence decision.",
            next_step="Open task detail and verify evidence before closeout.",
        ))
    for run in failed_runs[:6]:
        action_queue.append(_command_item(
            "failed_run",
            run,
            severity="high",
            reason="Run failed or blocked.",
            next_step="Open run detail and inspect logs/artifacts; do not retry external actions without approval.",
        ))

    safe_work_queue = [
        _command_item(
            "safe_local_task",
            task,
            severity="low",
            reason="Real-work queue item: safe local task is not proof noise and not approval-gated.",
            next_step=(task.get("queue_hygiene") or {}).get("next_step") or "Doni can continue locally: inspect task detail, create/verify artifact, then report evidence.",
        )
        for task in real_work_tasks[:8]
    ]
    proof_noise_queue = [
        _command_item(
            "proof_generated",
            task,
            severity="info",
            reason="Proof/test artifact kept for audit trail; hidden from primary real-work queue.",
            next_step=(task.get("queue_hygiene") or {}).get("next_step") or "Keep as audit/proof trail.",
        )
        for task in proof_generated_tasks[:8]
    ]

    today_focus = []
    if action_queue:
        today_focus.append({"priority": 1, "label": "Resolve Action Required queue", "why": f"{len(action_queue)} concrete item(s) need attention", "kind": "action_required", "target_id": action_queue[0].get("id")})
    if safe_work_queue:
        today_focus.append({"priority": 2, "label": "Continue safe local work", "why": f"{len(safe_work_queue)} safe item(s) can proceed without public side effects", "kind": "safe_local", "target_id": safe_work_queue[0].get("id")})
    if recent_artifacts:
        today_focus.append({"priority": 3, "label": "Review latest proof artifact", "why": recent_artifacts[0].get("title"), "kind": "proof_review", "target_id": recent_artifacts[0].get("id")})
    if not today_focus:
        today_focus.append({"priority": 1, "label": "System healthy", "why": "No immediate operator action detected", "kind": "monitor"})

    operator_brief = {
        "headline": "Action required" if action_queue else "Safe local work available" if safe_work_queue else "No active queue pressure",
        "decision_count": len(action_queue),
        "safe_count": len(safe_work_queue),
        "oldest_pending_age_hours": max([i["age"].get("hours") or 0 for i in action_queue], default=0),
        "next_human_decision": action_queue[0]["next_step"] if action_queue else None,
        "next_safe_step": safe_work_queue[0]["next_step"] if safe_work_queue else None,
    }

    return {
        "local_only": True,
        "read_only": False,
        "mutation_actions_enabled": True,
        "operational_actions": ["jarvis_command_create", "explicit_confirm_and_run", "durable_cancel", "approval_resolve", "memory_search"],
        "credentials_visible": False,
        "gateway_restart": False,
        "status": "attention" if action_queue else "ok",
        "today_focus": today_focus[:3],
        "operator_brief": operator_brief,
        "action_required": {
            "count": action_required,
            "rendered_queue_items": len(action_queue),
            "pending_approvals": approvals.get("counts", {}).get("pending", 0),
            "blocked_tasks": queue.get("blocked_tasks", 0),
            "review_tasks": queue.get("review_tasks", 0),
            "failed_executions": queue.get("failed_executions", 0),
            "gated_tasks": len(gated_tasks),
        },
        "queue_hygiene": {
            "counts": tasks.get("hygiene_counts") or {},
            "contract": tasks.get("hygiene_contract") or {},
            "real_work_count": len(real_work_tasks),
            "proof_generated_count": len(proof_generated_tasks),
            "stale_draft_count": len(stale_draft_tasks),
            "approval_gated_count": len(gated_tasks),
            "real_work_preview_ids": [t.get("id") for t in real_work_tasks[:8]],
            "proof_noise_preview_ids": [t.get("id") for t in proof_generated_tasks[:8]],
            "stale_draft_preview_ids": [t.get("id") for t in stale_draft_tasks[:8]],
        },
        "action_queue": action_queue,
        "safe_work_queue": safe_work_queue,
        "proof_noise_queue": proof_noise_queue,
        "doni_active_lane": {
            "project": "agents-os",
            "phase": "operational hardening",
            "current_slice": "schema v4, concurrency-safe Mission Control and governance",
            "mode": "local-only autonomous build",
        },
        "safe_next_actions": [
            {"label": "Open safe task detail", "enabled": bool(safe_work_queue), "target_id": safe_work_queue[0].get("id") if safe_work_queue else None},
            {"label": "Open latest proof artifact", "enabled": bool(recent_artifacts), "target_id": recent_artifacts[0].get("id") if recent_artifacts else None},
            {"label": "Run local E2E proof", "enabled": True, "target_id": "tests/hermes_cli/test_agents_os_web.py"},
        ],
        "gated_actions": [
            {"label": "push / PR", "requires": "explicit Goran approval"},
            {"label": "deploy / public publish", "requires": "explicit Goran approval"},
            {"label": "credential-backed integrations", "requires": "explicit Goran approval"},
            {"label": "approval resolution", "requires": "explicit Goran decision"},
        ],
        "recent_proof": [{"id": a.get("id"), "title": a.get("title"), "kind": a.get("kind"), "path": a.get("path")} for a in recent_artifacts],
        "runtime_health": {"doctor": safety.get("doctor"), "operator_product": "Agents OS Mission Control"},
    }


def command_center_payload(service: AgentsOSService) -> dict[str, Any]:
    key = (str(service.paths.home.resolve()), "command-center")
    with _PAYLOAD_CACHE_LOCK:
        cached = _PAYLOAD_CACHE.get(key)
        if cached and time.monotonic() - cached[0] < 10.0:
            return cached[1]
        payload = _command_center_payload_uncached(service)
        _PAYLOAD_CACHE[key] = (time.monotonic(), payload)
        return payload


def mission_control_html(service: AgentsOSService) -> str:
    status = service.status_payload()
    bootstrap = {"status": status, "knowledge_note": "vault/reference graph, not runtime memory merge"}
    return f"""<!doctype html>
<html lang=\"hr\">
<head>
<meta charset=\"utf-8\" />
<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
<title>Agents OS Mission Control</title>
<style>
:root {{ color-scheme: dark; --bg:#080b14; --panel:#101827; --panel2:#142035; --text:#e8f0ff; --muted:#93a4bd; --accent:#66d9ff; --warn:#ffc857; --ok:#7dffb2; --bad:#ff6b7a; }}
* {{ box-sizing:border-box; }} body {{ margin:0; font-family:Inter, ui-sans-serif, system-ui, Segoe UI, Arial; background:radial-gradient(circle at 20% 0%, #13284a 0, var(--bg) 38%); color:var(--text); }}
header {{ padding:28px 34px; border-bottom:1px solid #24344f; }} h1 {{ margin:0; letter-spacing:.02em; }} .sub {{ color:var(--muted); margin-top:8px; }}
.tabs {{ display:flex; gap:8px; flex-wrap:wrap; padding:18px 34px; border-bottom:1px solid #1e2c44; position:sticky; top:0; background:#080b14dd; backdrop-filter:blur(8px); z-index:5; }}
button {{ background:#16243a; color:var(--text); border:1px solid #2a4166; border-radius:12px; padding:10px 13px; cursor:pointer; }} button.active, button:hover {{ border-color:var(--accent); box-shadow:0 0 0 1px #66d9ff55 inset; }}
main {{ padding:24px 34px 60px; }} section {{ display:none; }} section.active {{ display:block; }} .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(260px,1fr)); gap:16px; }}
.card {{ background:linear-gradient(180deg,var(--panel),var(--panel2)); border:1px solid #263a5b; border-radius:18px; padding:16px; box-shadow:0 20px 50px #0007; }}
.kv {{ color:var(--muted); font-size:13px; }} .ok {{ color:var(--ok); }} .warn {{ color:var(--warn); }} .bad {{ color:var(--bad); }} textarea {{ width:100%; min-height:96px; border-radius:14px; background:#09111f; color:var(--text); border:1px solid #2a4166; padding:12px; }} pre {{ overflow:auto; background:#070b12; border:1px solid #1d2c45; border-radius:14px; padding:12px; }} .pill {{ display:inline-block; border:1px solid #35537e; border-radius:999px; padding:3px 8px; color:var(--muted); margin:2px; }}
</style>
</head>
<body>
<header><h1>Agents OS Mission Control</h1><div class=\"sub\">Local-only operator cockpit · gateway restart: false · vault/reference graph, not runtime memory merge</div></header>
<nav class=\"tabs\">
<button data-tab="overview" class="active">Overview</button><button data-tab="command">Command Center</button><button data-tab="executiveBoard">Executive Board</button><button data-tab="governance">Governance</button><button data-tab="automation">Automation Inbox</button><button data-tab="tasks">Task board</button><button data-tab="approvals">Approvals</button><button data-tab="runs">Runs</button><button data-tab="evidence">Evidence</button><button data-tab="events">Events / Logs</button><button data-tab="idea">Idea Factory</button><button data-tab="workflowFactory">Workflow Factory</button><button data-tab="agents">Agent Registry</button><button data-tab="knowledge">Knowledge Galaxy</button><button data-tab="board">Board Meeting</button><button data-tab="artifacts">Artifact Library</button><button data-tab="seo">SEO Mission Control</button><button data-tab="operator">Operator Loop</button><button data-tab="media">Media Studio</button><button data-tab="toolShed">Tool Shed</button><button data-tab="skills">Skills</button><button data-tab="sessions">Sessions</button><button data-tab="manage">Manage / Status</button><button data-tab="voice">Doni</button>
</nav>
<main>
<section id=\"overview\" class=\"active\"><div class=\"grid\"><div class=\"card\"><h2>HEALTH <span class=\"ok\">OK</span></h2><div class=\"kv\">State DB: {status.get('state_db')}</div><div class=\"kv\">Schema: {status.get('schema_version')}</div></div><div class=\"card\"><h2>Queue</h2><pre id=\"queueSummary\"></pre></div></div></section>
<section id=\"command\"><h2>Command Center / Today Focus</h2><div class=\"kv\">CEO/operator-readable next actions. Read-only: no deploy, no push, no credentials, no gateway restart.</div><div id=\"commandFocus\" class=\"grid\"></div><div class=\"grid\"><div class=\"card\"><h3>Operator brief</h3><pre id=\"commandOperatorBrief\"></pre></div><div class=\"card\"><h3>Action required counts</h3><pre id=\"commandActionRequired\"></pre></div><div class=\"card\"><h3>Queue hygiene</h3><pre id=\"commandQueueHygiene\"></pre></div><div class=\"card\"><h3>Active lane</h3><pre id=\"commandLane\"></pre></div></div><h3>Action Required queue</h3><div id=\"commandActionQueue\" class=\"grid\"></div><h3>Real work queue</h3><div id=\"commandSafeQueue\" class=\"grid\"></div><h3>Proof/test noise queue</h3><div id=\"commandProofNoiseQueue\" class=\"grid\"></div><div class=\"grid\"><div class=\"card\"><h3>Safe next actions</h3><div id=\"commandSafeActions\"></div></div><div class=\"card\"><h3>Gated actions</h3><div id=\"commandGatedActions\"></div></div></div><h3>Recent proof</h3><div id=\"commandRecentProof\" class=\"grid\"></div><pre id=\"commandPayload\"></pre></section>
<section id=\"executiveBoard\"><h2>Executive Board / Okrugli stol</h2><div class=\"kv\">Doni + Kodi izvršni par · jedna zajednička preporuka · vlasnička odluka ostaje izvan agenata.</div><div class=\"grid\"><div class=\"card\"><h3>Otvori temu</h3><textarea id=\"executiveObjective\">Procijeniti sljedeći prioritet Agents OS-a</textarea><input id=\"executiveProjectId\" value=\"agents-os\" placeholder=\"project id\" style=\"width:100%;border-radius:12px;background:#09111f;color:var(--text);border:1px solid #2a4166;padding:10px;margin-top:8px\"/><p><button id=\"createExecutiveMeeting\">Otvori Okrugli stol</button> <button id=\"refreshExecutiveBoard\">Osvježi</button></p><pre id=\"executiveResult\"></pre></div><div class=\"card\"><h3>Doni + Kodi lifecycle</h3><input id=\"executiveMeetingId\" placeholder=\"meeting id\" style=\"width:100%;border-radius:12px;background:#09111f;color:var(--text);border:1px solid #2a4166;padding:10px\"/><textarea id=\"executiveDoniProposal\" placeholder=\"Donijev prijedlog\"></textarea><button id=\"submitExecutiveDoniProposal\">Spremi Donijev prijedlog</button><textarea id=\"executiveKodiProposal\" placeholder=\"Kodijev prijedlog\"></textarea><button id=\"submitExecutiveKodiProposal\">Spremi Kodijev prijedlog</button><textarea id=\"executiveDoniChallenge\" placeholder=\"Donijev challenge Kodiju\"></textarea><button id=\"submitExecutiveDoniChallenge\">Spremi Donijev challenge</button><textarea id=\"executiveKodiChallenge\" placeholder=\"Kodijev challenge Doniju\"></textarea><button id=\"submitExecutiveKodiChallenge\">Spremi Kodijev challenge</button></div><div class=\"card\"><h3>Zajednička preporuka i odluka</h3><textarea id=\"executiveRecommendation\" placeholder=\"Zajednička preporuka\"></textarea><textarea id=\"executiveGoalPrompt\" placeholder=\"/goal prompt\"></textarea><select id=\"executiveConsensus\"><option value=\"consensus\">consensus</option><option value=\"dissent\">dissent</option></select><textarea id=\"executiveDissent\" placeholder=\"Dissent zapis, ako postoji\"></textarea><button id=\"finalizeExecutiveRecommendation\">Zaključi preporuku</button><select id=\"executiveOwnerDecision\"><option value=\"approved\">approved</option><option value=\"rejected\">rejected</option><option value=\"deferred\">deferred</option></select><textarea id=\"executiveDecisionReason\" placeholder=\"Goranov razlog odluke\"></textarea><button id=\"recordExecutiveDecision\">Spremi vlasničku odluku</button></div><div class=\"card\"><h3>Reviewed Shared Memory</h3><input id=\"executiveMemoryCapsuleId\" placeholder=\"capsule id\"/><input id=\"executiveMemorySha\" placeholder=\"SHA-256\"/><select id=\"executiveMemoryClass\"><option value=\"P0\">P0</option><option value=\"P1\">P1</option></select><textarea id=\"executiveMemorySummary\" placeholder=\"Pregledana sažeta činjenica\"></textarea><textarea id=\"executiveMemoryProvenance\" placeholder=\"provenance JSON: source_type, source_ref, sha256\"></textarea><button id=\"executiveMemoryCandidate\">Stage memory candidate</button><input id=\"executiveMemoryCandidateId\" placeholder=\"candidate id\"/><textarea id=\"executiveMemoryReason\" placeholder=\"Goranov razlog odobrenja\"></textarea><button id=\"promoteExecutiveMemory\">Promoviraj pregledanu memoriju</button></div><div class=\"card\"><h3>Company Overview</h3><pre id=\"executiveCompany\"></pre></div><div class=\"card\"><h3>Idea Pipeline</h3><pre id=\"executiveIdeas\"></pre></div><div class=\"card\"><h3>Money & Opportunity</h3><pre id=\"executiveMoney\"></pre></div><div class=\"card\"><h3>Execution Room</h3><pre id=\"executiveExecution\"></pre></div><div class=\"card\"><h3>Decision Desk</h3><pre id=\"executiveDecisions\"></pre></div><div class=\"card\"><h3>Reviewed Shared Knowledge</h3><pre id=\"executiveKnowledge\"></pre></div></div><h3>Agent Roster</h3><div id=\"executiveRoster\" class=\"grid\"></div><pre id=\"executivePayload\"></pre></section>
<section id=\"automation\"><h2>Automation Inbox</h2><div class=\"kv\">Local-only automation intake: validate → dedup → task/run/artifact/event. External callbacks are not executed.</div><div class=\"grid\"><div class=\"card\"><h3>Paste automation-intake.v0 payload</h3><textarea id=\"automationPayload\">{{\"schema_version\":\"automation-intake.v0\",\"source\":\"mission-control-ui\",\"event_id\":\"ui-smoke\",\"goal\":\"Create safe-local proof task from UI\",\"callback\":{{\"type\":\"local_file\"}}}}</textarea><p><button id=\"submitAutomationPayload\">Intake automation payload</button> <button id=\"refreshAutomationInbox\">Refresh inbox</button></p><pre id=\"automationResult\"></pre></div><div class=\"card\"><h3>Automation inbox payload</h3><pre id=\"automationPayloadView\"></pre></div></div><div id=\"automationList\" class=\"grid\"></div></section>
<section id=\"tasks\"><h2>Task board / Tasks</h2><div class=\"kv\">Read-only task surface. No status mutation from UI.</div><pre id=\"taskDetail\"></pre><pre id=\"tasksPayload\"></pre><div id=\"tasksList\" class=\"grid\"></div></section>
<section id=\"approvals\"><h2>Approvals</h2><div class=\"kv\">Read-only approval visibility. Approve/deny/resolve is disabled without explicit operator decision.</div><pre id=\"approvalDetail\"></pre><pre id=\"approvalsPayload\"></pre><div id=\"approvalsList\" class=\"grid\"></div></section>
<section id=\"runs\"><h2>Runs / Executions</h2><div class=\"kv\">Read-only run lifecycle surface.</div><pre id=\"runDetail\"></pre><pre id=\"runsPayload\"></pre><div id=\"runsList\" class=\"grid\"></div></section>
<section id=\"evidence\"><h2>Evidence Drilldown</h2><div class=\"kv\">Task → run → event → artifact read-only proof map. No status mutation, no approval resolution.</div><pre id=\"evidenceSummary\"></pre><pre id=\"evidenceDetail\"></pre><div id=\"evidenceList\" class=\"grid\"></div></section>
<section id="events"><h2>Events / Logs</h2><div class="kv">Read-only recent event log with bounded redacted payload preview.</div><pre id="eventsPayload"></pre><div id="eventsList" class="grid"></div></section>
<section id="idea"><div class="card"><h2>Idea Factory</h2><textarea id="ideaText">Obradi YouTube video</textarea><p><button id="draftIdea">Draft only</button> <button id="createIdea">Create safe task / approval draft</button></p><pre id="ideaResult"></pre><div class="kv">Fields: classification · risk class · recommended lane · plan steps · approval badge · expected artifacts · acceptance criteria</div></div></section>
<section id="workflowFactory"><div class="card"><h2>Workflow Factory</h2><div class="kv">Classify any link/idea/screenshot/task into capability bucket, approval gates, agent lane, and next safe-local action. Draft artifact only; no execution.</div><textarea id="workflowInput">Agent OS: dodaj read-only proof panel bez gateway restarta</textarea><p><button id="draftWorkflow">Draft workflow</button></p><pre id="workflowResult"></pre><pre id="workflowSchema"></pre></div></section>
<section id="agents"><h2>Paperclip Agent Registry</h2><div id="agentsList" class="grid"></div></section>
<section id=\"knowledge\"><h2>Knowledge / Memory Galaxy v0</h2><div class=\"kv\">Read-only vault/reference graph, not runtime memory merge.</div><div id=\"knowledgeList\" class=\"grid\"></div></section>
<section id=\"board\"><div class=\"card\"><h2>Board Meeting</h2><div class=\"kv\">Draft-only cockpit flow. Creates a local board meeting artifact plus safe-local task or approval draft; execution remains false.</div><textarea id=\"boardObjective\">Planirati Asset Library read-only proof iz Mission Controla</textarea><div id=\"boardParticipants\" class=\"kv\"></div><p><button id=\"draftBoardMeeting\">Draft board meeting</button></p><pre id=\"boardMeetingResult\"></pre></div></section>
<section id=\"artifacts\"><h2>Artifact / Asset Library</h2><div class=\"card\"><input id=\"assetSearch\" placeholder=\"Search assets\" style=\"width:100%;border-radius:12px;background:#09111f;color:var(--text);border:1px solid #2a4166;padding:10px;margin-bottom:8px;\" /><select id=\"assetTypeFilter\" style=\"width:100%;border-radius:12px;background:#09111f;color:var(--text);border:1px solid #2a4166;padding:10px;\"><option value=\"\">All types</option></select><pre id=\"assetSummary\"></pre></div><pre id=\"artifactDetail\"></pre><div id=\"artifactList\" class=\"grid\"></div></section>
<section id=\"seo\"><h2>SEO Mission Control</h2><div class=\"card\"><h3>Draft-only SEO/AISO lane</h3><div class=\"kv\">publish disabled · outreach disabled · live metrics require approval-gated credentials</div><pre id=\"seoPayload\"></pre></div><div id=\"seoList\" class=\"grid\"></div></section>
<section id=\"operator\"><h2>Operator Loop / Judge / Evidence</h2><pre id=\"operatorPayload\"></pre></section>
<section id=\"media\"><h2>Media Studio Browser v0</h2><div class=\"kv\">Read-only. No generation. No posting.</div><div id=\"mediaList\" class=\"grid\"></div></section>
<section id=\"toolShed\"><h2>Tool Shed v0</h2><div class=\"kv\">Read-only tools · skills · cron/watchdogs · connectors. Secrets are not read or displayed; mutations disabled.</div><pre id=\"toolShedPayload\"></pre><div id=\"toolShedList\" class=\"grid\"></div></section>
<section id=\"skills\"><h2>Skills read-only</h2><div class=\"kv\">Metadata only. No install/edit/delete.</div><div id=\"skillsList\" class=\"grid\"></div></section>
<section id=\"sessions\"><h2>Sessions read-only</h2><div class=\"kv\">Metadata only. Raw transcripts are not displayed.</div><div id=\"sessionsList\" class=\"grid\"></div></section>
<section id=\"manage\"><h2>Manage / Update / Status</h2><pre id=\"managePayload\"></pre></section>
<section id=\"voice\"><div class=\"card\" style=\"max-width:920px;margin:0 auto;padding:24px\"><h2 style=\"margin-top:0\">Doni / Osobni asistent</h2><div class=\"kv\">Razgovaraj s Donijem. Specijalisti i vanjske radnje ostaju iza naprednih kontrola i posebne potvrde.</div><div id=\"jarvisConversation\" style=\"min-height:180px;max-height:440px;overflow:auto;margin:18px 0;padding:16px;border:1px solid #2a4166;border-radius:16px;background:#07101e\"><div style=\"max-width:78%;padding:12px 14px;border-radius:14px;background:#172a46\"><strong>Doni</strong><br>Tu sam. Napiši što želiš napraviti ili pitati.</div></div><textarea id=\"jarvisTranscript\" placeholder=\"Napiši poruku Doniju…\" style=\"min-height:92px\"></textarea><p style=\"display:flex;gap:10px;align-items:center;flex-wrap:wrap\"><select id=\"jarvisRuntime\" style=\"min-width:180px;border-radius:10px;padding:10px;background:#09111f;color:var(--text);border:1px solid #2a4166\"><option value=\"hermes\">Hermes / Doni</option><option value=\"codex\">Codex</option><option value=\"claude\">Claude</option><option value=\"openclaw\">OpenClaw</option></select><button id=\"sendJarvis\" style=\"font-weight:700;padding:11px 22px\">Pošalji</button><button id=\"interruptJarvis\">Zaustavi</button><span id=\"jarvisHumanStatus\" class=\"kv\">Spreman.</span></p><details><summary>Napredne kontrole i tehnički detalji · wake/show/build/act</summary><p><button id=\"recordJarvis\">Record command / Snimi glas</button> <button id=\"stopJarvis\" disabled>Zaustavi snimanje</button> <button id=\"previewJarvis\">Command Preview / Pregled naredbe</button> <button id=\"clearJarvis\">Očisti</button> <button id=\"replyJarvis\">Voice Reply / Glasovni odgovor</button></p><pre id=\"jarvisRuntimes\"></pre><pre id=\"jarvisGateStatus\"></pre><pre id=\"jarvisCommandCard\"></pre><pre id=\"jarvisReply\"></pre><audio id=\"jarvisAudio\" controls></audio><pre id=\"jarvisPayload\"></pre><pre id=\"voicePayload\"></pre></details></div></section>
<section id=\"governance\"><h2>Governance / System Map</h2><div class=\"kv\">Safe-local operator controls. Preview and proof actions stay local; risky actions create approval drafts and never execution.</div><p><button id=\"previewGovernanceArtifact\">Preview governance artifact</button> <button id=\"draftGovernanceApproval\">Draft approval: deploy / public publish</button> <button id=\"createGovernanceProofTask\">Create local E2E proof task</button></p><pre id=\"governanceResult\"></pre><pre id=\"governancePayload\"></pre><div id=\"governanceArtifacts\" class=\"grid\"></div></section>
</main>
<script id=\"bootstrap\" type=\"application/json\">{_json_safe(bootstrap)}</script>
<script>
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const asCard = (title, body) => '<div class="card"><h3>' + escapeHtml(title) + '</h3>' + body + '</div>';
function pill(v) {{ return '<span class="pill">' + escapeHtml(v) + '</span>'; }}
function escapeHtml(v) {{ return String(v ?? '').replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
function setSafeMarkup(targetOrSelector, markup) {{
 const target = typeof targetOrSelector === 'string' ? $(targetOrSelector) : targetOrSelector; if (!target) return;
 const parsed = new DOMParser().parseFromString('<main>' + String(markup ?? '') + '</main>', 'text/html');
 target.replaceChildren(...Array.from(parsed.body.firstElementChild.childNodes));
}}
async function j(url, opts={{}}) {{ const r = await fetch(url, {{headers:{{'content-type':'application/json'}}, ...opts}}); return await r.json(); }}
async function jTimeout(url, ms=1800, opts={{}}) {{
 const ctrl = new AbortController(); const timer = setTimeout(() => ctrl.abort(), ms);
 try {{ return await j(url, {{...opts, signal: ctrl.signal}}); }}
 catch (e) {{ return {{items: [], timeout: true, error: String(e)}}; }}
 finally {{ clearTimeout(timer); }}
}}
function showPre(sel, obj) {{ $(sel).textContent = JSON.stringify(obj, null, 2); }}
$$('button[data-tab]').forEach(b => b.addEventListener('click', () => {{ $$('button[data-tab]').forEach(x=>x.classList.remove('active')); $$('section').forEach(x=>x.classList.remove('active')); b.classList.add('active'); $('#' + b.dataset.tab).classList.add('active'); }}));
async function showTaskDetail(id) {{ showPre('#taskDetail', await j('/api/tasks/' + encodeURIComponent(id))); }}
async function showApprovalDetail(id) {{
 const payload=await j('/api/approvals/' + encodeURIComponent(id)); showPre('#approvalDetail',payload);
 const old=$('#approvalDecisionControls'); if (old) old.remove();
 const approval=payload.approval || payload; if (approval.status !== 'pending') return;
 const wrap=document.createElement('p'); wrap.id='approvalDecisionControls';
 for (const decision of ['approved','rejected']) {{ const button=document.createElement('button'); button.textContent=decision==='approved'?'Approve':'Reject'; button.addEventListener('click',async()=>{{ const result=await j('/api/approvals/'+encodeURIComponent(id)+'/resolve',{{method:'POST',body:JSON.stringify({{decision}})}}); showPre('#approvalDetail',result); wrap.remove(); await loadAll(); }}); wrap.appendChild(button); }}
 $('#approvalDetail').insertAdjacentElement('afterend',wrap);
}}
async function showRunDetail(id) {{ showPre('#runDetail', await j('/api/runs/' + encodeURIComponent(id))); }}
async function showArtifactDetail(id) {{ showPre('#artifactDetail', await j('/api/artifacts/' + encodeURIComponent(id))); }}
async function showEvidenceDetail(id) {{ showPre('#evidenceDetail', await j('/api/evidence/' + encodeURIComponent(id))); }}
function renderEvidence(evidence) {{
 showPre('#evidenceSummary', {{summary:evidence.summary || {{}}, acceptance_contract:evidence.acceptance_contract || []}});
 setSafeMarkup('#evidenceList', (evidence.items || []).slice(0,40).map(item => asCard((item.verdict || 'unknown').toUpperCase() + ' · ' + (item.title || item.task_id), '<div class="kv">' + escapeHtml(item.task_id) + ' · ' + escapeHtml(item.status) + ' · ' + escapeHtml(item.workflow) + '</div><div class="kv">artifacts=' + escapeHtml(item.counts?.artifacts ?? 0) + ' runs=' + escapeHtml(item.counts?.runs ?? 0) + ' events=' + escapeHtml(item.counts?.events ?? 0) + ' approvals=' + escapeHtml(item.counts?.approvals ?? 0) + ' reviews=' + escapeHtml(item.counts?.reviews ?? 0) + '</div><p>' + escapeHtml(item.next_step || '') + '</p><p><button data-detail-id="' + escapeHtml(item.task_id) + '" onclick="showEvidenceDetail(this.dataset.detailId)">Open evidence drilldown</button></p>')).join('') || '<div class="card kv">No evidence items.</div>');
}}

function selectedBoardParticipants() {{ return $$('#boardParticipants input[type="checkbox"]:checked').map(x => x.value); }}
function renderBoardAgents(agents) {{
 const target = $('#boardParticipants'); if (!target) return;
 setSafeMarkup(target, '<div class="kv">Participants</div>' + agents.agents.map(a => '<label class="pill"><input type="checkbox" value="' + escapeHtml(a.id) + '" ' + (a.id === 'local-agent' ? 'checked' : '') + '> ' + escapeHtml(a.name) + '</label>').join(''));
}}
function renderAssets(artifacts) {{
 showPre('#assetSummary', artifacts.summary || {{}});
 const types = Object.keys(artifacts.summary || {{}}).sort(); const filter = $('#assetTypeFilter');
 const current = filter.value || ''; setSafeMarkup(filter, '<option value="">All types</option>' + types.map(t => '<option value="' + escapeHtml(t) + '">' + escapeHtml(t) + '</option>').join('')); filter.value = current;
 const q = ($('#assetSearch')?.value || '').toLowerCase(); const type = filter.value;
 const filtered = (artifacts.items || []).filter(a => (!type || a.kind === type || a.preview_type === type) && (!q || String((a.id||'') + ' ' + (a.title||'') + ' ' + (a.path||'') + ' ' + (a.kind||'')).toLowerCase().includes(q)));
 setSafeMarkup('#artifactList', filtered.slice(0,60).map(a => asCard(a.title, '<div class="kv">' + escapeHtml(a.id) + '</div><div class="kv">' + escapeHtml(a.kind) + ' · ' + escapeHtml(a.preview_type||a.suffix) + '</div><div class="kv">' + escapeHtml(a.path) + '</div><p><button data-detail-id="' + escapeHtml(a.id) + '" onclick="showArtifactDetail(this.dataset.detailId)">Open artifact preview</button></p>')).join('') || '<div class="card kv">No matching assets.</div>');
}}
function commandQueueCard(item) {{
 const age = item.age || {{}};
 return asCard((item.severity || 'info').toUpperCase() + ' · ' + (item.title || item.id), '<div class="kv">' + escapeHtml(item.kind) + ' · ' + escapeHtml(item.status || 'n/a') + ' · age=' + escapeHtml(age.hours ?? 'unknown') + 'h · ' + escapeHtml(age.bucket || '') + '</div><p>' + escapeHtml(item.reason || '') + '</p><div class="kv">Next: ' + escapeHtml(item.next_step || '') + '</div><div class="kv">target=' + escapeHtml(item.open_target || item.id || 'none') + '</div>');
}}
function renderCommandCenter(command) {{
 showPre('#commandPayload', command);
 showPre('#commandOperatorBrief', command.operator_brief || {{}});
 showPre('#commandActionRequired', command.action_required || {{}});
 showPre('#commandQueueHygiene', command.queue_hygiene || {{}});
 showPre('#commandLane', command.doni_active_lane || {{}});
 setSafeMarkup('#commandFocus', (command.today_focus || []).map(f => asCard('#' + escapeHtml(f.priority) + ' ' + escapeHtml(f.label), '<div class="kv">' + escapeHtml(f.kind) + '</div><p>' + escapeHtml(f.why) + '</p><div class="kv">target=' + escapeHtml(f.target_id || 'none') + '</div>')).join('') || '<div class="card kv">No focus items.</div>');
 setSafeMarkup('#commandActionQueue', (command.action_queue || []).map(commandQueueCard).join('') || '<div class="card kv">No Action Required items.</div>');
 setSafeMarkup('#commandSafeQueue', (command.safe_work_queue || []).map(commandQueueCard).join('') || '<div class="card kv">No real-work queue items.</div>');
 setSafeMarkup('#commandProofNoiseQueue', (command.proof_noise_queue || []).map(commandQueueCard).join('') || '<div class="card kv">No proof/test noise items.</div>');
 setSafeMarkup('#commandSafeActions', (command.safe_next_actions || []).map(a => '<div class="pill">' + escapeHtml(a.enabled ? 'READY' : 'N/A') + ' · ' + escapeHtml(a.label) + ' · ' + escapeHtml(a.target_id || 'none') + '</div>').join(''));
 setSafeMarkup('#commandGatedActions', (command.gated_actions || []).map(a => '<div class="pill">GATED · ' + escapeHtml(a.label) + ' · ' + escapeHtml(a.requires) + '</div>').join(''));
 setSafeMarkup('#commandRecentProof', (command.recent_proof || []).map(a => asCard(a.title || a.id, '<div class="kv">' + escapeHtml(a.id) + ' · ' + escapeHtml(a.kind) + '</div><div class="kv">' + escapeHtml(a.path) + '</div>')).join('') || '<div class="card kv">No recent proof.</div>');
}}

async function refreshExecutiveBoard() {{
 const payload = await j('/api/executive-board');
 showPre('#executivePayload', payload);
 showPre('#executiveCompany', payload.company_overview || {{}});
 showPre('#executiveIdeas', payload.idea_pipeline || {{}});
 showPre('#executiveMoney', payload.money_opportunity || []);
 showPre('#executiveExecution', payload.execution_room || {{}});
 showPre('#executiveDecisions', payload.decision_desk || {{}});
 showPre('#executiveKnowledge', payload.shared_knowledge || {{}});
 setSafeMarkup('#executiveRoster', (payload.agent_roster || []).map(a => asCard(a.name || a.id, '<div class="kv">' + escapeHtml(a.id) + ' · ' + escapeHtml(a.status) + ' · ' + escapeHtml(a.role) + '</div><div class="kv">Memory: ' + escapeHtml(a.memory_boundary) + '</div><div class="kv">Auth: ' + escapeHtml(a.auth_boundary) + '</div>')).join(''));
 return payload;
}}
async function createExecutiveMeeting() {{
 const payload = {{action:'create_meeting', objective:$('#executiveObjective').value, project_id:$('#executiveProjectId').value, risk_class:'safe-local'}};
 const result = await runExecutiveAction(payload); if (result.meeting_id) $('#executiveMeetingId').value = result.meeting_id; return result;
}}
async function runExecutiveAction(payload) {{
 const result = await j('/api/executive-board/action', {{method:'POST', body:JSON.stringify(payload)}});
 showPre('#executiveResult', result); if (result.meeting_id) $('#executiveMeetingId').value = result.meeting_id; if (result.candidate_id) $('#executiveMemoryCandidateId').value = result.candidate_id; await refreshExecutiveBoard(); return result;
}}
const executiveScorecard = {{value:8,feasibility:8,risk:3,cost:4,time:5,revenue:7}};
async function submitExecutiveProposal(agent) {{ return runExecutiveAction({{action:'submit_proposal',meeting_id:$('#executiveMeetingId').value,agent_id:agent,content:$(agent==='doni'?'#executiveDoniProposal':'#executiveKodiProposal').value,scorecard:executiveScorecard}}); }}
async function submitExecutiveChallenge(challenger) {{ const doni=challenger==='doni'; return runExecutiveAction({{action:'submit_challenge',meeting_id:$('#executiveMeetingId').value,challenger_id:challenger,target_agent_id:doni?'kodi':'doni',content:$(doni?'#executiveDoniChallenge':'#executiveKodiChallenge').value}}); }}
async function finalizeExecutiveRecommendation() {{ return runExecutiveAction({{action:'finalize_recommendation',meeting_id:$('#executiveMeetingId').value,recommendation:$('#executiveRecommendation').value,goal_prompt:$('#executiveGoalPrompt').value,consensus:$('#executiveConsensus').value,dissent:$('#executiveDissent').value}}); }}
async function recordExecutiveDecision() {{ return runExecutiveAction({{action:'record_decision',meeting_id:$('#executiveMeetingId').value,decision:$('#executiveOwnerDecision').value,decided_by:'goran',reason:$('#executiveDecisionReason').value}}); }}
async function stageExecutiveMemory() {{ let provenance; try {{ provenance=JSON.parse($('#executiveMemoryProvenance').value); }} catch(e) {{ showPre('#executiveResult',{{status:'invalid_provenance_json',error:String(e)}}); return; }} return runExecutiveAction({{action:'stage_memory_candidate',capsule_id:$('#executiveMemoryCapsuleId').value,capsule_sha256:$('#executiveMemorySha').value,classification:$('#executiveMemoryClass').value,summary:$('#executiveMemorySummary').value,provenance}}); }}
async function promoteExecutiveMemory() {{ return runExecutiveAction({{action:'promote_memory_candidate',candidate_id:$('#executiveMemoryCandidateId').value,approved_by:'goran',reason:$('#executiveMemoryReason').value}}); }}

async function refreshAutomationInbox() {{
 const inbox = await j('/api/automation/inbox');
 showPre('#automationPayloadView', inbox);
 setSafeMarkup('#automationList', (inbox.items || []).slice(0,30).map(i => asCard((i.source || 'source') + ':' + (i.event_id || 'event'), '<div class="kv">' + escapeHtml(i.id) + ' · task=' + escapeHtml(i.task_id) + ' · approval_required=' + escapeHtml(i.approval_required) + '</div><div class="kv">artifact=' + escapeHtml(i.artifact_path) + '</div>')).join('') || '<div class="card kv">No automation intakes yet.</div>');
 return inbox;
}}
async function submitAutomationPayload() {{
 let payload;
 try {{ payload = JSON.parse($('#automationPayload').value || '{{}}'); }} catch (e) {{ showPre('#automationResult', {{status:'invalid_json', error:String(e)}}); return; }}
 const result = await j('/api/automation/intake', {{method:'POST', body:JSON.stringify(payload)}});
 showPre('#automationResult', result);
 await refreshAutomationInbox();
 await loadAll();
}}
let governancePayload = {{artifacts:[]}};
async function refreshGovernance() {{
 governancePayload = await j('/api/governance');
 showPre('#governancePayload', governancePayload);
 setSafeMarkup('#governanceArtifacts', (governancePayload.artifacts || []).map(a => asCard(a.title || a.id, '<div class="kv">' + escapeHtml(a.kind) + '</div><div class="kv">' + escapeHtml(a.path || 'virtual') + '</div>')).join(''));
 return governancePayload;
}}
async function runGovernanceAction(action, label) {{
 const data = {{action, label}};
 if (action === 'artifact_preview') data.artifact_id = governancePayload.artifacts?.[0]?.id || '';
 const result = await j('/api/governance/action', {{method:'POST', body:JSON.stringify(data)}});
 showPre('#governanceResult', result);
 await refreshGovernance();
 return result;
}}
let latestArtifacts = {{items:[], summary:{{}}}};
async function loadAllOnce() {{
 const dashboard = await j('/api/dashboard'); showPre('#queueSummary', dashboard.queue_summary || {{}});
 // Keep Jarvis/Voice panel non-blocking: populate it before slower dashboard sections.
 showPre('#managePayload', await jTimeout('/api/manage/status', 1800));
 showPre('#voicePayload', await jTimeout('/api/voice/status', 1800));
 showPre('#jarvisPayload', await jTimeout('/api/doni/briefing', 7000));
 const command = await j('/api/command-center'); renderCommandCenter(command);
 await refreshExecutiveBoard();
 await refreshGovernance();
 if (location.search.includes('demo=command')) {{ document.querySelector('button[data-tab="command"]').click(); }}
 const tasks = await j('/api/tasks'); showPre('#tasksPayload', tasks); setSafeMarkup('#tasksList', tasks.items.slice(0,30).map(t => asCard(t.title || t.id, '<div class="kv">' + escapeHtml(t.id) + ' · ' + escapeHtml(t.status) + ' · ' + escapeHtml(t.workflow) + '</div><div class="kv">queue=' + escapeHtml(t.queue_hygiene?.queue_class || 'unknown') + ' · priority=' + escapeHtml(t.priority) + ' · approval_required=' + escapeHtml(t.approval_required) + '</div><p>' + escapeHtml(t.queue_hygiene?.next_step || '') + '</p><p><button data-detail-id="' + escapeHtml(t.id) + '" onclick="showTaskDetail(this.dataset.detailId)">Open task detail</button></p>')).join(''));
 const approvals = await j('/api/approvals'); showPre('#approvalsPayload', approvals); setSafeMarkup('#approvalsList', approvals.items.slice(0,30).map(a => asCard(a.title || a.id, '<div class="kv">' + escapeHtml(a.id) + ' · ' + escapeHtml(a.status) + ' · risk=' + escapeHtml(a.risk) + '</div><div class="kv">resolution enabled: ' + escapeHtml(a.resolution_enabled) + '</div><p><button data-detail-id="' + escapeHtml(a.id) + '" onclick="showApprovalDetail(this.dataset.detailId)">Open approval detail</button></p>')).join('') || '<div class="card kv">No approvals.</div>');
 const runs = await j('/api/runs'); showPre('#runsPayload', runs); setSafeMarkup('#runsList', runs.items.slice(0,30).map(r => asCard(r.id, '<div class="kv">' + escapeHtml(r.status) + ' · ' + escapeHtml(r.workflow) + '</div><div class="kv">task=' + escapeHtml(r.task_id) + '</div><p><button data-detail-id="' + escapeHtml(r.id) + '" onclick="showRunDetail(this.dataset.detailId)">Open run detail</button></p>')).join('') || '<div class="card kv">No runs.</div>');
 const evidence = await j('/api/evidence'); renderEvidence(evidence);
 const events = await j('/api/events'); showPre('#eventsPayload', events); setSafeMarkup('#eventsList', events.items.slice(0,30).map(e => asCard(e.event_type || e.id, '<div class="kv">' + escapeHtml(e.id) + ' · ' + escapeHtml(e.created_at) + '</div><div class="kv">task=' + escapeHtml(e.task_id) + ' run=' + escapeHtml(e.run_id) + '</div>')).join('') || '<div class="card kv">No events.</div>');
 showPre('#workflowSchema', await j('/api/workflow-factory/schema'));
 const agents = await j('/api/agents'); renderBoardAgents(agents); setSafeMarkup('#agentsList', agents.agents.map(a => asCard(a.name, '<div class="kv">' + escapeHtml(a.id) + ' · ' + escapeHtml(a.status) + '</div><p>' + (a.capabilities||[]).map(pill).join('') + '</p><div class="kv">Memory: ' + escapeHtml(a.memory_boundary) + '</div><div class="kv">Auth: ' + escapeHtml(a.auth_boundary) + '</div><div class="kv">Gates: ' + (a.approval_gates||[]).map(escapeHtml).join(', ') + '</div>')).join(''));
 const knowledge = await j('/api/knowledge/index'); setSafeMarkup('#knowledgeList', knowledge.nodes.map(n => asCard(n.label, '<div class="kv">' + escapeHtml(n.kind) + ' · exists=' + escapeHtml(n.exists) + '</div><div class="kv">' + escapeHtml(n.path) + '</div>')).join(''));
 latestArtifacts = await j('/api/assets'); renderAssets(latestArtifacts);
 const seo = await j('/api/seo'); showPre('#seoPayload', seo); setSafeMarkup('#seoList', ['goals','keyword_queue','draft_queue','review_gates'].map(k => asCard(k, '<div class="kv">' + escapeHtml((seo[k]||[]).length) + ' item(s)</div>')).join(''));
 const operator = await j('/api/operator-loop'); showPre('#operatorPayload', operator);
 const media = await j('/api/media'); setSafeMarkup('#mediaList', media.assets.map(m => asCard(m.title, '<div class="kv">' + escapeHtml(m.mime) + ' · ' + escapeHtml(m.size_bytes) + ' bytes</div><div class="kv">' + escapeHtml(m.path) + '</div>')).join('') || '<div class="card kv">No local media assets found.</div>');
 const toolShed = await j('/api/tool-shed'); showPre('#toolShedPayload', toolShed); setSafeMarkup('#toolShedList', (toolShed.connectors || []).map(c => asCard(c.label || c.id, '<div class="kv">status=' + escapeHtml(c.status) + ' · mutation_enabled=' + escapeHtml(c.mutation_enabled) + ' · credentials_visible=' + escapeHtml(c.credentials_visible ?? false) + '</div><div class="kv">' + escapeHtml(c.note || '') + '</div>')).join('') || '<div class="card kv">No tool shed connectors.</div>');
 const skills = await j('/api/skills'); setSafeMarkup('#skillsList', skills.items.slice(0,80).map(s => asCard(s.name, '<div class="kv">' + escapeHtml(s.category) + '</div><div class="kv">' + escapeHtml(s.description) + '</div>')).join('') || '<div class="card kv">No skills metadata.</div>');
 showPre('#managePayload', await jTimeout('/api/manage/status', 1800));
 showPre('#voicePayload', await jTimeout('/api/voice/status', 1800));
 showPre('#jarvisPayload', await jTimeout('/api/doni/briefing', 7000));
 const sessions = await jTimeout('/api/sessions', 1800); setSafeMarkup('#sessionsList', (sessions.items || []).map(s => asCard(s.file, '<div class="kv">size=' + escapeHtml(s.size_bytes) + ' raw_transcript_visible=' + escapeHtml(s.raw_transcript_visible) + '</div>')).join('') || '<div class="card kv">No sessions metadata. ' + escapeHtml(sessions.timeout ? 'Timed out after 1.8s; panel remains non-blocking.' : '') + '</div>');
 if (location.search.includes('demo=executive-board')) {{ document.querySelector('button[data-tab="executiveBoard"]').click(); await refreshExecutiveBoard(); }}
 if (location.search.includes('demo=task-detail') && tasks.items.length) {{ document.querySelector('button[data-tab="tasks"]').click(); await showTaskDetail(tasks.items[0].id); }}
 if (location.search.includes('demo=approval-detail') && approvals.items.length) {{ document.querySelector('button[data-tab="approvals"]').click(); await showApprovalDetail(approvals.items[0].id); }}
}}
let loadAllPromise = null;
function loadAll() {{
 if (loadAllPromise) return loadAllPromise;
 loadAllPromise = loadAllOnce().finally(() => {{ loadAllPromise = null; }});
 return loadAllPromise;
}}
$('#draftIdea').addEventListener('click', async () => showPre('#ideaResult', await j('/api/idea-factory/draft', {{method:'POST', body:JSON.stringify({{idea_text:$('#ideaText').value}})}})));
$('#createIdea').addEventListener('click', async () => {{ showPre('#ideaResult', await j('/api/idea-factory/action', {{method:'POST', body:JSON.stringify({{idea_text:$('#ideaText').value}})}})); await loadAll(); }});
$('#draftWorkflow').addEventListener('click', async () => {{ showPre('#workflowResult', await j('/api/workflow-factory/draft', {{method:'POST', body:JSON.stringify({{input_text:$('#workflowInput').value}})}})); await loadAll(); }});
$('#createExecutiveMeeting').addEventListener('click', createExecutiveMeeting);
$('#refreshExecutiveBoard').addEventListener('click', refreshExecutiveBoard);
$('#submitExecutiveDoniProposal').addEventListener('click', () => submitExecutiveProposal('doni'));
$('#submitExecutiveKodiProposal').addEventListener('click', () => submitExecutiveProposal('kodi'));
$('#submitExecutiveDoniChallenge').addEventListener('click', () => submitExecutiveChallenge('doni'));
$('#submitExecutiveKodiChallenge').addEventListener('click', () => submitExecutiveChallenge('kodi'));
$('#finalizeExecutiveRecommendation').addEventListener('click', finalizeExecutiveRecommendation);
$('#recordExecutiveDecision').addEventListener('click', recordExecutiveDecision);
$('#executiveMemoryCandidate').addEventListener('click', stageExecutiveMemory);
$('#promoteExecutiveMemory').addEventListener('click', promoteExecutiveMemory);
$('#draftBoardMeeting').addEventListener('click', async () => {{ const result = await j('/api/board-meeting/draft', {{method:'POST', body:JSON.stringify({{objective:$('#boardObjective').value, participants:selectedBoardParticipants()}})}}); showPre('#boardMeetingResult', result); await loadAll(); }});
$('#assetSearch').addEventListener('input', () => renderAssets(latestArtifacts));
$('#assetTypeFilter').addEventListener('change', () => renderAssets(latestArtifacts));
let refreshTimer = null;
function scheduleRefresh() {{
 clearTimeout(refreshTimer);
 refreshTimer = setTimeout(() => {{ if (document.hidden) {{ scheduleRefresh(); return; }} loadAll().catch(e => showPre('#queueSummary', {{error:String(e), refresh:'poll_failed'}})).finally(scheduleRefresh); }}, 60000);
}}
let jarvisRecorder = null; let jarvisChunks = []; let jarvisSuppressOnStop = false; let latestJarvisCommand = null; let jarvisPollTimer = null;
function installJarvisOperationalControls() {{
 const transcript = $('#jarvisTranscript'); if (!transcript || !$('#sendJarvis')) return;
 $('#sendJarvis').addEventListener('click', sendJarvisMessage);
 transcript.addEventListener('keydown', event => {{ if (event.key === 'Enter' && !event.shiftKey) {{ event.preventDefault(); sendJarvisMessage(); }} }});
 j('/api/runtimes').then(payload => showPre('#jarvisRuntimes', payload));
}}
function setJarvisHumanStatus(text) {{ const el=$('#jarvisHumanStatus'); if (el) el.textContent=text; }}
function appendJarvisBubble(role, text, id='') {{
 const box=$('#jarvisConversation'); if (!box) return null;
 const bubble=document.createElement('div'); if (id) bubble.id=id;
 bubble.style.cssText='max-width:78%;margin:10px 0;padding:12px 14px;border-radius:14px;white-space:pre-wrap;overflow-wrap:anywhere;' + (role==='user' ? 'margin-left:auto;background:#2458a6;' : 'background:#172a46;');
 const label=document.createElement('strong'); label.textContent=role==='user' ? 'Goran' : 'Doni';
 const body=document.createElement('div'); body.textContent=text; bubble.append(label, body); box.appendChild(bubble); box.scrollTop=box.scrollHeight; return body;
}}
let doniSessionId=null; let doniActiveRunId=null; let doniRunPollTimer=null; let doniRequestEpoch=0;
function requireCanonicalDoni(payload) {{
 if (payload?.error) throw new Error(payload.error.message || 'Canonical Doni request nije uspio.');
 if (payload?.schema_version !== '1.0' || payload?.assistant_identity !== 'doni' || payload?.memory_authority !== 'canonical-doni-runtime' || !payload?.runtime_boot_id) {{
   throw new Error('Odgovor nije potvrđen kao canonical Doni runtime.');
 }}
 return payload;
}}
async function doniJson(url, opts={{}}) {{ return requireCanonicalDoni(await j(url, opts)); }}
async function ensureDoniSession() {{
 if (doniSessionId) return doniSessionId;
 const session=await doniJson('/api/doni/sessions', {{method:'POST',body:'{{}}'}});
 doniSessionId=session.session_id; return doniSessionId;
}}
function finishDoniTurn(statusText) {{
 clearTimeout(doniRunPollTimer); doniActiveRunId=null; setJarvisHumanStatus(statusText);
 const reply=$('#jarvisActiveReply'); if(reply) reply.removeAttribute('id');
 $('#sendJarvis').disabled=false; $('#jarvisTranscript').focus();
}}
async function pollDoniRun(epoch) {{
 if (epoch !== doniRequestEpoch || !doniActiveRunId) return;
 try {{
   const payload=await doniJson('/api/doni/runs/'+encodeURIComponent(doniActiveRunId));
   if (epoch !== doniRequestEpoch) return;
   const reply=$('#jarvisActiveReply');
   if (['started','queued','running','stopping'].includes(payload.status)) {{
     if(reply) reply.textContent=payload.status==='stopping'?'Zaustavljam…':'Razmišljam…';
     doniRunPollTimer=setTimeout(() => pollDoniRun(epoch), 700); return;
   }}
   if (payload.status==='completed') {{
     if(reply) reply.textContent=payload.text; finishDoniTurn('Gotovo.'); return;
   }}
   if(reply) reply.textContent=payload.status==='cancelled'?'Zaustavljeno.':(payload.error || 'Doni nije uspio završiti odgovor.');
   finishDoniTurn(payload.status==='cancelled'?'Zaustavljeno.':'Doni nije odgovorio.');
 }} catch(error) {{
   const reply=$('#jarvisActiveReply'); if(reply) reply.textContent='Ne mogu nastaviti ovaj turn. '+String(error?.message || error);
   finishDoniTurn('Doni gateway nije dostupan.');
 }}
}}
async function sendJarvisMessage() {{
 const text=$('#jarvisTranscript').value.trim(); if (!text || $('#sendJarvis').disabled) return;
 appendJarvisBubble('user', text); $('#jarvisTranscript').value=''; $('#sendJarvis').disabled=true;
 const reply=appendJarvisBubble('assistant','Razmišljam…','jarvisActiveReply'); setJarvisHumanStatus('Doni razmišlja…');
 const epoch=++doniRequestEpoch;
 try {{
   const sessionId=await ensureDoniSession();
   const started=await doniJson('/api/doni/sessions/'+encodeURIComponent(sessionId)+'/turns', {{method:'POST',body:JSON.stringify({{text}})}});
   if (epoch !== doniRequestEpoch) return;
   doniActiveRunId=started.run_id; pollDoniRun(epoch);
 }} catch(error) {{
   reply.textContent='Ne mogu nastaviti ovaj turn. '+String(error?.message || error);
   finishDoniTurn('Doni gateway nije dostupan.');
 }}
}}
async function createJarvisCommand() {{
 const payload = await j('/api/doni/commands', {{method:'POST', body:JSON.stringify({{transcript_text:$('#jarvisTranscript').value, idempotency_key:'ui-' + crypto.randomUUID()}})}});
 latestJarvisCommand = payload.command; showPre('#jarvisCommandCard', payload); setJarvisGateStatus('command_created', {{command_id:latestJarvisCommand?.id, state:latestJarvisCommand?.state}}); return payload;
}}
async function pollJarvisCommand() {{
 clearTimeout(jarvisPollTimer); if (!latestJarvisCommand?.id) return;
 const payload = await j('/api/doni/commands/' + encodeURIComponent(latestJarvisCommand.id)); latestJarvisCommand = payload.command; showPre('#jarvisCommandCard', payload);
 setJarvisGateStatus('command_' + latestJarvisCommand.state, {{command_id:latestJarvisCommand.id, run_id:latestJarvisCommand.run_id}});
 const reply=$('#jarvisActiveReply');
 if (['queued','running','cancelling'].includes(latestJarvisCommand.state)) {{ if(reply) reply.textContent=latestJarvisCommand.state==='queued'?'Čekam agenta…':'Radim na tome…'; setJarvisHumanStatus('Agent radi…'); jarvisPollTimer=setTimeout(pollJarvisCommand,1000); return; }}
 if (latestJarvisCommand.state==='succeeded') {{ if(reply) reply.textContent=latestJarvisCommand.result?.text || 'Zadatak je završen.'; setJarvisHumanStatus('Gotovo.'); }}
 else if (latestJarvisCommand.state==='cancelled') {{ if(reply) reply.textContent='Zaustavljeno.'; setJarvisHumanStatus('Zaustavljeno.'); }}
 else {{ if(reply) reply.textContent=latestJarvisCommand.error?.status==='timed_out'?'Agent nije odgovorio unutar 2 minute. Pokušaj drugi runtime ili kraću poruku.':'Agent nije uspio završiti zadatak.'; setJarvisHumanStatus('Zadatak nije uspio.'); }}
 if(reply) reply.removeAttribute('id'); $('#sendJarvis').disabled=false; $('#jarvisTranscript').focus();
}}
async function runJarvisCommand() {{
 if (!latestJarvisCommand) await createJarvisCommand();
 const payload = await j('/api/doni/commands/' + encodeURIComponent(latestJarvisCommand.id) + '/start', {{method:'POST', body:JSON.stringify({{expected_version:latestJarvisCommand.version, runtime:$('#jarvisRuntime').value, approved_model_call:true}})}});
 latestJarvisCommand = payload.command; showPre('#jarvisCommandCard', payload);
 if (payload.approval_required) setJarvisGateStatus('awaiting_approval', {{approval_id:latestJarvisCommand.approval_id}}); else pollJarvisCommand();
}}
installJarvisOperationalControls();
function setJarvisGateStatus(status, extra={{}}) {{ showPre('#jarvisGateStatus', {{local_only:true, status, execution_created:Boolean(latestJarvisCommand?.run_id), approval_required_for:['external_actions','computer_control','deploy','credentials','always_on_microphone'], ...extra}}); }}
async function previewJarvisCommand() {{ setJarvisGateStatus('preview_requested', {{transcript_length:$('#jarvisTranscript').value.length}}); const payload = await j('/api/doni/preview', {{method:'POST', body:JSON.stringify({{transcript_text:$('#jarvisTranscript').value}})}}); showPre('#jarvisCommandCard', payload); setJarvisGateStatus('preview_ready', {{risk_class: payload.command_card?.risk_class || 'unknown', allowed_now: payload.command_card?.allowed_now ?? false}}); }}
async function interruptJarvisCommand() {{
 jarvisSuppressOnStop = true; if (jarvisRecorder && jarvisRecorder.state !== 'inactive') jarvisRecorder.stop(); $('#recordJarvis').disabled = false; $('#stopJarvis').disabled = true;
 if (doniActiveRunId) {{
   const runId=doniActiveRunId; doniActiveRunId=null; ++doniRequestEpoch; clearTimeout(doniRunPollTimer);
   try {{ await doniJson('/api/doni/runs/'+encodeURIComponent(runId)+'/cancel', {{method:'POST',body:'{{}}'}}); }}
   catch(error) {{ showPre('#jarvisCommandCard', {{status:'doni_cancel_failed',message:String(error?.message || error)}}); }}
   const reply=$('#jarvisActiveReply'); if(reply) reply.textContent='Zaustavljeno.';
   finishDoniTurn('Zaustavljeno.'); return;
 }}
 if (latestJarvisCommand && !['succeeded','failed','cancelled'].includes(latestJarvisCommand.state)) {{ const payload=await j('/api/doni/commands/' + encodeURIComponent(latestJarvisCommand.id) + '/cancel', {{method:'POST',body:JSON.stringify({{expected_version:latestJarvisCommand.version,reason:'operator_interrupt'}})}}); latestJarvisCommand=payload.command; showPre('#jarvisCommandCard',payload); setJarvisGateStatus('cancel_requested',{{command_id:latestJarvisCommand.id,state:latestJarvisCommand.state}}); return; }}
 showPre('#jarvisCommandCard', {{local_only:true, status:'interrupted_cancelled', execution_created:false, approval_required:false, message:'Local Doni action draft was interrupted/cancelled.', cancelled_at:new Date().toISOString()}}); setJarvisGateStatus('interrupted_cancelled');
}}
function clearJarvisCommand() {{ $('#jarvisTranscript').value = ''; showPre('#jarvisCommandCard', {{local_only:true, status:'cleared', execution_created:false}}); showPre('#jarvisReply', {{local_only:true, status:'cleared', execution_created:false}}); setJarvisGateStatus('cleared'); }}
async function replyJarvisCommand() {{ setJarvisGateStatus('voice_reply_preview_requested'); const payload = await j('/api/doni/reply', {{method:'POST', body:JSON.stringify({{text:$('#jarvisTranscript').value}})}}); showPre('#jarvisReply', payload); if (payload.audio_artifact_path) $('#jarvisAudio').src = payload.audio_artifact_path; setJarvisGateStatus('voice_reply_ready', {{audio_artifact_path: payload.audio_artifact_path || null}}); }}
$('#previewJarvis').addEventListener('click', previewJarvisCommand);
$('#interruptJarvis').addEventListener('click', interruptJarvisCommand);
$('#clearJarvis').addEventListener('click', clearJarvisCommand);
$('#submitAutomationPayload').addEventListener('click', submitAutomationPayload);
$('#refreshAutomationInbox').addEventListener('click', refreshAutomationInbox);
$('#previewGovernanceArtifact').addEventListener('click', () => runGovernanceAction('artifact_preview', 'Preview governance artifact'));
$('#draftGovernanceApproval').addEventListener('click', () => runGovernanceAction('gated_approval_draft', 'deploy / public publish'));
$('#createGovernanceProofTask').addEventListener('click', () => runGovernanceAction('create_local_e2e_task', 'Create local E2E proof task'));
$('#replyJarvis').addEventListener('click', replyJarvisCommand);
$('#recordJarvis').addEventListener('click', async () => {{
 if (!navigator.mediaDevices || !window.MediaRecorder) {{ showPre('#jarvisCommandCard', {{status:'browser_audio_unavailable', local_only:true, execution_created:false, fallback:'Use typed command preview instead.'}}); setJarvisGateStatus('browser_audio_unavailable', {{fallback:'typed_command_preview'}}); return; }}
 const stream = await navigator.mediaDevices.getUserMedia({{audio:true}}); jarvisChunks = []; jarvisRecorder = new MediaRecorder(stream);
 jarvisRecorder.ondataavailable = e => {{ if (e.data && e.data.size) jarvisChunks.push(e.data); }};
 jarvisRecorder.onstop = async () => {{
   if (jarvisSuppressOnStop) {{ jarvisSuppressOnStop = false; jarvisRecorder.stream.getTracks().forEach(t=>t.stop()); return; }}
   stream.getTracks().forEach(t => t.stop());
   const blob = new Blob(jarvisChunks, {{type: jarvisRecorder.mimeType || 'audio/webm'}});
   const reader = new FileReader();
   reader.onloadend = async () => {{ showPre('#jarvisCommandCard', await j('/api/doni/transcribe', {{method:'POST', body:JSON.stringify({{audio_base64:String(reader.result), audio_mime:blob.type, transcript_text:$('#jarvisTranscript').value}})}})); }};
   reader.readAsDataURL(blob); $('#recordJarvis').disabled = false; $('#stopJarvis').disabled = true;
 }};
 jarvisRecorder.start(); $('#recordJarvis').disabled = true; $('#stopJarvis').disabled = false; showPre('#jarvisCommandCard', {{status:'recording', execution_created:false}});
}});
$('#stopJarvis').addEventListener('click', () => {{ if (jarvisRecorder && jarvisRecorder.state !== 'inactive') jarvisRecorder.stop(); }});
loadAll().catch(e => showPre('#queueSummary', {{error:String(e)}})).finally(scheduleRefresh);
</script>
</body></html>"""


class MissionControlHandler(BaseHTTPRequestHandler):
    service: AgentsOSService

    def log_message(self, fmt: str, *args: Any) -> None:  # keep smoke output clean
        return

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        with _ACTIVE_REQUESTS_LOCK:
            _ACTIVE_REQUESTS[threading.get_ident()] = (path, time.monotonic())
        if os.environ.get("AGENTS_OS_REQUEST_TRACE") == "1":
            print(f"agents-os request start GET {path}", flush=True)
        try:
            if path == "/api/debug/active":
                now = time.monotonic()
                with _ACTIVE_REQUESTS_LOCK:
                    active = [{"thread": tid, "path": item[0], "seconds": round(now - item[1], 3)} for tid, item in _ACTIVE_REQUESTS.items()]
                _send_json(self, {"active": active})
            elif path == "/":
                _send_html(self, mission_control_html(self.service))
            elif path == "/api/status":
                payload = self.service.status_payload()
                payload["operator_ui"] = {"product": "Agents OS Mission Control", "local_only": True, "gateway_restart": False}
                _send_json(self, payload)
            elif path == "/api/dashboard":
                _send_json(self, cached_dashboard_payload(self.service))
            elif path == "/api/command-center":
                _send_json(self, command_center_payload(self.service))
            elif path == "/api/governance":
                _send_json(self, governance_status_payload(self.service.paths))
            elif path == "/api/automation/inbox":
                _send_json(self, automation_inbox_payload(self.service.paths))
            elif path == "/api/safety":
                _send_json(self, safety_payload(self.service))
            elif path == "/api/tasks":
                _send_json(self, tasks_payload(self.service.paths))
            elif path == "/api/approvals":
                _send_json(self, approvals_payload(self.service.paths))
            elif path.startswith("/api/approvals/"):
                _send_json(self, approval_detail_payload(self.service.paths, path.rsplit("/", 1)[-1]))
            elif path == "/api/runs":
                _send_json(self, runs_payload(self.service.paths))
            elif path.startswith("/api/runs/"):
                _send_json(self, run_detail_payload(self.service.paths, path.rsplit("/", 1)[-1]))
            elif path == "/api/events":
                _send_json(self, events_payload(self.service.paths))
            elif path == "/api/evidence":
                _send_json(self, evidence_payload(self.service.paths))
            elif path.startswith("/api/evidence/"):
                _send_json(self, evidence_detail_payload(self.service.paths, path.rsplit("/", 1)[-1]))
            elif path == "/api/cron":
                _send_json(self, cron_readiness_payload(self.service.paths))
            elif path == "/api/idea-factory/schema":
                _send_json(self, self.service.idea_factory_schema_payload())
            elif path == "/api/workflow-factory/schema":
                _send_json(self, workflow_factory_schema_payload(self.service.paths))
            elif path == "/api/agents":
                _send_json(self, agents_registry_payload(self.service.paths))
            elif path == "/api/executive-board":
                _send_json(self, executive_board_payload(self.service.paths))
            elif path == "/api/board-meeting/schema":
                _send_json(self, board_meeting_schema_payload(self.service.paths))
            elif path in {"/api/knowledge", "/api/knowledge/index"}:
                _send_json(self, knowledge_index_payload(self.service.paths))
            elif path in {"/api/artifacts", "/api/assets", "/api/artifact-library"}:
                _send_json(self, artifacts_payload(self.service.paths))
            elif path.startswith("/api/artifacts/"):
                _send_json(self, artifact_detail_payload(self.service.paths, path.rsplit("/", 1)[-1]))
            elif path == "/api/tool-shed":
                _send_json(self, tool_shed_payload(self.service.paths))
            elif path == "/api/skills":
                _send_json(self, skills_visibility_payload(self.service.paths))
            elif path == "/api/sessions":
                _send_json(self, sessions_visibility_payload(self.service.paths))
            elif path == "/api/seo":
                _send_json(self, seo_mission_control_payload(self.service.paths))
            elif path == "/api/operator-loop":
                _send_json(self, operator_loop_payload(self.service))
            elif path.startswith("/api/tasks/"):
                _send_json(self, task_detail_payload(self.service.paths, path.rsplit("/", 1)[-1]))
            elif path == "/api/media":
                _send_json(self, media_assets_payload(self.service.paths))
            elif path == "/api/manage/status":
                _send_json(self, redacted_manage_status_payload(self.service.paths))
            elif path in {"/api/voice", "/api/voice/status"}:
                payload = voice_status_payload(self.service.paths)
                if path == "/api/voice":
                    briefing = jarvis_briefing_payload(self.service.paths)
                    payload = {
                        **payload,
                        "execution_created": False,
                        "always_on_microphone": False,
                        "wake_word_enabled": False,
                        "computer_control": "approval_gated_unexecuted",
                        "briefing": briefing,
                    }
                _send_json(self, payload)
            elif path in {"/api/doni/briefing", "/api/jarvis/briefing"}:
                _send_json(self, jarvis_briefing_payload(self.service.paths))
            elif path == "/api/doni/health":
                _send_json(self, _validate_doni_identity(doni_companion_json_request("/v1/companion/health")))
            elif path.startswith("/api/doni/runs/") and len(path.strip("/").split("/")) == 4:
                _send_json(self, doni_run_payload(path.strip("/").split("/")[3]))
            elif path == "/api/runtimes":
                coordinator = execution_coordinator(self.service)
                _send_json(self, {"runtimes": coordinator.capabilities(), "allowed_cwds": [str(item) for item in coordinator.allowed_cwds]})
            elif path in {"/api/doni/commands", "/api/jarvis/commands"}:
                _send_json(self, jarvis_commands_payload(self.service.paths))
            elif path.startswith(("/api/doni/commands/", "/api/jarvis/commands/")):
                command_id = path.split("/")[4]
                with connect(self.service.paths) as conn:
                    command = get_command(conn, command_id)
                    execution = execution_projection(conn, command["run_id"]) if command.get("run_id") else None
                _send_json(self, {"command": command, "execution": execution})
            else:
                _send_json(self, {"status": "not_found", "path": path}, 404)
        except DoniCompanionError as exc:
            _send_json(self, {"status": "error", "error": {"code": exc.code, "message": str(exc)}}, exc.status)
        except Exception as exc:  # deterministic local error payload
            _send_json(self, {"status": "error", "error": exc.__class__.__name__, "message": str(exc)}, 500)

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            data = _read_json_body(self)
            if path == "/api/automation/intake":
                payload = self.service.automation_intake_payload(data)
                _send_json(self, payload, 400 if payload.get("status") == "rejected" else 200)
            elif path == "/api/doni/sessions":
                _send_json(self, doni_open_session_action(), 201)
            elif path.startswith("/api/doni/sessions/") and path.endswith("/turns"):
                session_id = path.strip("/").split("/")[3]
                _send_json(self, doni_start_turn_action(session_id, data), 202)
            elif path.startswith("/api/doni/runs/") and path.endswith("/cancel"):
                run_id = path.strip("/").split("/")[3]
                _send_json(self, doni_cancel_run_action(run_id))
            elif path == "/api/governance/action":
                payload = governance_action_payload(self.service.paths, data)
                _send_json(self, payload, 400 if payload.get("status") == "rejected" else 200)
            elif path == "/api/idea-factory/draft":
                _send_json(self, self.service.idea_factory_draft_payload(data))
            elif path == "/api/idea-factory/action":
                _send_json(self, create_idea_action(self.service, data))
            elif path == "/api/workflow-factory/draft":
                _send_json(self, draft_workflow_factory_action(self.service, data))
            elif path == "/api/executive-board/action":
                _send_json(self, executive_board_action(self.service, data))
            elif path == "/api/board-meeting/draft":
                _send_json(self, draft_board_meeting_action(self.service, data))
            elif path in {"/api/doni/preview", "/api/jarvis/preview"}:
                _send_json(self, jarvis_preview_payload(self.service.paths, data))
            elif path in {"/api/doni/commands", "/api/jarvis/commands"}:
                _send_json(self, jarvis_create_command_action(self.service, data), 201)
            elif path == "/api/memory/search":
                _send_json(self, memory_search_action(self.service.paths, data))
            elif path.startswith(("/api/doni/commands/", "/api/jarvis/commands/")) and path.endswith("/start"):
                command_id = path.split("/")[4]
                _send_json(self, jarvis_start_command_action(self.service, command_id, data), 202)
            elif path.startswith(("/api/doni/commands/", "/api/jarvis/commands/")) and path.endswith("/cancel"):
                command_id = path.split("/")[4]
                _send_json(self, jarvis_cancel_command_action(self.service, command_id, data))
            elif path.startswith("/api/approvals/") and path.endswith("/resolve"):
                approval_id = path.split("/")[3]
                _send_json(self, resolve_web_approval_action(self.service, approval_id, data))
            elif path in {"/api/doni/advisor", "/api/jarvis/advisor"}:
                _send_json(self, jarvis_model_advisor_payload(self.service.paths, data))
            elif path in {"/api/doni/reply", "/api/jarvis/reply"}:
                _send_json(self, jarvis_reply_payload(self.service.paths, data))
            elif path in {"/api/doni/transcribe", "/api/jarvis/transcribe"}:
                _send_json(self, jarvis_transcribe_payload(self.service.paths, data))
            else:
                _send_json(self, {"status": "not_found", "path": path}, 404)
        except DoniCompanionError as exc:
            _send_json(self, {"status": "error", "error": {"code": exc.code, "message": str(exc)}}, exc.status)
        except ValueError as exc:
            _send_json(self, {"status": "error", "error": "bad_request", "message": str(exc)}, 400)
        except Exception as exc:
            _send_json(self, {"status": "error", "error": exc.__class__.__name__, "message": str(exc)}, 500)

    def do_DELETE(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        try:
            if path.startswith("/api/doni/sessions/") and len(path.strip("/").split("/")) == 4:
                session_id = path.strip("/").split("/")[3]
                _send_json(self, doni_end_session_action(session_id))
            else:
                _send_json(self, {"status": "not_found", "path": path}, 404)
        except DoniCompanionError as exc:
            _send_json(self, {"status": "error", "error": {"code": exc.code, "message": str(exc)}}, exc.status)
        except Exception as exc:
            _send_json(self, {"status": "error", "error": exc.__class__.__name__, "message": str(exc)}, 500)


def run_server(host: str, port: int, service: AgentsOSService) -> None:
    if host not in LOCAL_HOSTS:
        raise ValueError("Agents OS web may bind only to 127.0.0.1/localhost")
    handler = type("BoundMissionControlHandler", (MissionControlHandler,), {"service": service})
    server = ThreadingHTTPServer((host, port), handler)
    print(f"Agents OS Mission Control: http://{host}:{port}", flush=True)
    server.serve_forever()


def web_cmd(args: argparse.Namespace) -> int:
    paths = resolve_paths(args)
    service = AgentsOSService(paths)
    url = f"http://{args.host}:{args.port}"
    payload = {"status": "ready", "url": url, "local_only": True, "gateway_restart": False, "state_db": str(paths.db)}
    if getattr(args, "json", False):
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.host not in LOCAL_HOSTS:
        print("Agents OS web may bind only to 127.0.0.1/localhost", file=sys.stderr)
        return 2
    if getattr(args, "open", False):
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    run_server(args.host, args.port, service)
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run local-only Agents OS Mission Control without importing the full Hermes CLI.")
    parser.add_argument("--host", default="127.0.0.1", help="Local bind host; only 127.0.0.1 or localhost are allowed")
    parser.add_argument("--port", type=int, default=18791, help="Local dashboard port (default: 18791)")
    parser.add_argument("--open", action="store_true", help="Open dashboard in the default browser")
    parser.add_argument("--json", action="store_true", help="Print launcher/status payload without starting a server")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Fast dedicated Mission Control entrypoint.

    `hermes_cli.main agents-os web` can pay the full Hermes CLI/plugin import cost before
    it reaches the Agents OS subcommand. This module-level entrypoint keeps the local
    proof server on a small import surface while preserving the same `web_cmd` contract.
    """
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    return web_cmd(args)


# Attach thin service helpers here to avoid widening the core runtime surface too much.
def _service_idea_factory_schema_payload(self: AgentsOSService) -> dict[str, Any]:
    return idea_factory_schema()


def _service_idea_factory_draft_payload(self: AgentsOSService, data: dict[str, Any]) -> dict[str, Any]:
    payload = draft_idea(
        data.get("idea_text") or data.get("idea") or "",
        context=data.get("context"),
        desired_output=data.get("desired_output"),
        urgency=data.get("urgency", "normal"),
        source_links=data.get("source_links") or data.get("source_link") or [],
    )
    payload["execution_created"] = False
    return payload


def _service_operator_loop_payload(self: AgentsOSService) -> dict[str, Any]:
    return operator_loop_payload(self)


AgentsOSService.idea_factory_schema_payload = _service_idea_factory_schema_payload  # type: ignore[attr-defined]
AgentsOSService.idea_factory_draft_payload = _service_idea_factory_draft_payload  # type: ignore[attr-defined]
AgentsOSService.operator_loop_payload = _service_operator_loop_payload  # type: ignore[attr-defined]


if __name__ == "__main__":
    raise SystemExit(main())
