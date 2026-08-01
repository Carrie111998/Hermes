from __future__ import annotations

import asyncio
from dataclasses import dataclass
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
import math
import os
from pathlib import Path
import re
import socket
import subprocess
import time
from typing import Any
import uuid

import httpx
import pytest
from mcp.shared.version import LATEST_PROTOCOL_VERSION
from starlette.testclient import TestClient

from hermes_constants import get_hermes_home
from hermes_state import SessionDB
from session_bridge.catalog import UnifiedCatalog
from session_bridge.claude_adapter import (
    ClaudeCursor,
    ClaudeParseResult,
    ClaudeSourceAdapter,
    ClaudeTargetAdapter,
    PlaceholderResult,
)
from session_bridge.codex_adapter import CodexSourceAdapter, CodexTargetAdapter
from session_bridge.config import BridgeConfig, SidebarConfig
from session_bridge.context_pack import ContextPackBuilder
from session_bridge.coordinator import (
    ContinueRequest,
    SessionBridgeCoordinator,
)
from session_bridge.mcp_server import create_app
from session_bridge.mirror import MirrorPolicy, enqueue_mirror_job
from session_bridge.models import (
    BridgeMarkerPayload,
    HydrationMarkerPayload,
    OriginKind,
    ProjectedMessage,
    Provider,
    Relation,
    SessionProjection,
    SidebarHydrationState,
    SidebarJobState,
    canonical_session_id,
    decode_bridge_marker,
    encode_bridge_marker,
)
from session_bridge.preview import build_session_preview
from session_bridge.sidebar import (
    VerifiedSidebarThread,
    build_registration_prompt,
    decode_hydration_marker,
    decode_sidebar_registration_identity,
    encode_hydration_marker,
)
from session_bridge.sidebar_executor import NativeTurnAmbiguous
from session_bridge.sidebar_hydration_executor import SidebarHydrationExecutor
from session_bridge.sidebar_reconciliation import (
    SidebarReconciliationEvidence,
    SidebarReconciliationState,
)
from session_bridge.store import SessionBridgeStore


_MARKER_SECRET = b"synthetic-end-to-end-marker-secret"
_SIDEBAR_TOKEN = "synthetic-sidebar-mcp-token-at-least-32-bytes"
_SIDEBAR_SKILL_PATH = (
    Path(__file__).parents[2]
    / "session_bridge"
    / "assets"
    / "session-sidebar-sync"
    / "SKILL.md"
)


@dataclass(frozen=True)
class _SidebarSkillPlacement:
    project_id: str
    inbox_cwd: str
    source_cwd: str
    runtime_workspace_roots: tuple[str, ...]


@dataclass(frozen=True)
class _SidebarSkillContract:
    status_tool: str
    pending_tool: str
    pending_limit: int
    projects_tool: str
    read_thread_tool: str
    reserve_tool: str
    create_tool: str
    bind_tool: str
    rename_tool: str
    commit_tool: str
    fail_tool: str
    reconciliation_fields: tuple[str, ...]
    project_precedence: tuple[str, ...]
    create_cwd_source: str
    runtime_root_sources: tuple[str, ...]
    failure_codes: dict[str, str]
    forbid_app_server: bool
    provider_degradation_isolated: bool

    @classmethod
    def load(cls, path: Path) -> "_SidebarSkillContract":
        try:
            text = path.read_text(encoding="utf-8")
            queue_selection = text.split("\n## Queue Selection\n", 1)[1].split(
                "\n## In-place Hydration Procedure\n", 1
            )[0]
            procedure = text.split("\n## Registration Procedure\n", 1)[1].split(
                "\n## Fixed Failure Mapping\n", 1
            )[0]
            steps = {
                int(number): body.strip()
                for number, body in re.findall(
                    r"(?ms)^(\d+)\. (.*?)(?=^\d+\. |\Z)",
                    procedure,
                )
            }
            if set(steps) != set(range(1, 10)):
                raise ValueError("procedure steps")

            status = re.search(
                r"Call `(session_status)` exactly once",
                queue_selection,
            )
            pending = re.search(
                r"call `(session_sidebar_pending)\(limit=(\d+)\)`",
                queue_selection,
                re.IGNORECASE,
            )
            projects = re.search(
                r"native tool `(list_[a-z_]+)\(\{\}\)` exactly once",
                queue_selection,
            )
            if "list_threads" in text:
                raise ValueError("native task discovery is forbidden")
            reconciliation_fields = tuple(
                field
                for field in (
                    "reconciliation_state",
                    "reconciliation_proof_digest",
                    "reconciliation_generation",
                )
                if f"`{field}`" in steps[5] or f"`{field}`" in steps[6]
            )
            if reconciliation_fields != (
                "reconciliation_state",
                "reconciliation_proof_digest",
                "reconciliation_generation",
            ):
                raise ValueError("authoritative reconciliation fields")
            read_thread = re.search(r"call `(read_thread)\(", steps[5])
            reserve = re.search(r"call `(session_sidebar_reserve)\(", steps[6])
            create = re.search(r"`(create_thread)\(", steps[6])
            bind = re.search(r"call `(session_sidebar_bind)\(", steps[6])
            rename = re.search(r"Use `(set_thread_title)\(", steps[7])
            commit = re.search(r"Call `(session_sidebar_commit)\(", steps[8])
            fail = re.search(r"call `(session_sidebar_fail)\(", steps[9])
            matches = (
                status,
                pending,
                projects,
                read_thread,
                reserve,
                create,
                bind,
                rename,
                commit,
                fail,
            )
            if any(match is None for match in matches):
                raise ValueError("required call schemas")
            assert status and pending and projects
            assert (
                read_thread
                and reserve
                and create
                and bind
                and rename
                and commit
                and fail
            )

            inbox_selection = re.search(
                r"select only the saved `(Session Inbox)` project whose canonical "
                r"path equals the resolved canonical local `\.hermes` inbox cwd",
                steps[3],
            )
            if inbox_selection is None:
                raise ValueError("project precedence")
            precedence = ("inbox",)

            create_schema = re.search(
                r"`create_thread\((\{[^\n`]+\})\)`",
                steps[6],
            )
            if create_schema is None:
                raise ValueError("native create schema")
            create_arguments = json.loads(create_schema.group(1))
            if set(create_arguments) != {"prompt", "target"}:
                raise ValueError("native create arguments")
            if create_arguments["prompt"] != "<registration_prompt verbatim>":
                raise ValueError("native create prompt")
            target = create_arguments["target"]
            if (
                not isinstance(target, dict)
                or target.get("type") != "project"
                or not isinstance(target.get("projectId"), str)
                or target.get("environment") != {"type": "local"}
            ):
                raise ValueError("native create placement")
            create_cwd_source = "inbox"
            runtime_root_sources = ("inbox", "source")

            forbidden_stale_rules = (
                "Exact source cwd is a saved project",
                "exact source cwd, exact git root, then",
                "match either the job's exact source cwd",
            )
            if any(
                rule in text for rule in forbidden_stale_rules
            ) or cls._contains_source_first_placement_rule(text):
                raise ValueError("stale source-first placement rule")

            mapping_block = text.split("\n## Fixed Failure Mapping\n", 1)[1].split(
                "\n## Deterministic Call-Failure Rules\n", 1
            )[0]
            failure_codes = {
                label.strip(): code
                for label, code in re.findall(
                    r"(?m)^\| ([^|]+?) \| `([^`]+)` \|$",
                    mapping_block,
                )
            }
            failure_codes["Native task outside Session Inbox placement"] = (
                failure_codes["Native task outside Session Inbox placement (registration/new mirror only)"]
            )
            required_failures = {
                "Desktop offline before native-create dispatch": "desktop_offline",
                "Bridge temporarily unavailable": "bridge_temporarily_unavailable",
                "Native Codex task/project operation unavailable before native-create dispatch, or during a non-create native operation": "codex_tool_unavailable",
                "Rename failed": "rename_failed",
                "Create response lost or otherwise ambiguous": "native_create_ambiguous",
                "Bound task not yet indexed": "native_task_not_indexed",
                "Authenticated marker conflict": "marker_conflict",
                "Source identity mismatch": "source_identity_mismatch",
                "Session Inbox unavailable": "inbox_unavailable",
                "Native task outside Session Inbox placement (registration/new mirror only)": "placement_mismatch",
            }
            if any(
                failure_codes.get(label) != code
                for label, code in required_failures.items()
            ):
                raise ValueError("fixed failure mapping")

            no_app_server_rule = "Never use app-server thread creation as a fallback"
            provider_isolation_rule = (
                "must not globally block healthy queued delivery from another provider"
            )
            required_rules = (
                "never select placement or project identity",
                "Trust only the authoritative reconciliation object",
                '"prompt":"<registration_prompt verbatim>"',
                "title before commit",
                "try fail/release once with `bridge_temporarily_unavailable`",
                no_app_server_rule,
                provider_isolation_rule,
            )
            if any(text.count(rule) < 1 for rule in required_rules):
                raise ValueError("required rule")
            return cls(
                status_tool=status.group(1),
                pending_tool=pending.group(1),
                pending_limit=int(pending.group(2)),
                projects_tool=projects.group(1),
                read_thread_tool=read_thread.group(1),
                reserve_tool=reserve.group(1),
                create_tool=create.group(1),
                bind_tool=bind.group(1),
                rename_tool=rename.group(1),
                commit_tool=commit.group(1),
                fail_tool=fail.group(1),
                reconciliation_fields=reconciliation_fields,
                project_precedence=precedence,
                create_cwd_source=create_cwd_source,
                runtime_root_sources=runtime_root_sources,
                failure_codes=failure_codes,
                forbid_app_server=text.count(no_app_server_rule) >= 1,
                provider_degradation_isolated=(
                    text.count(provider_isolation_rule) >= 1
                ),
            )
        except (IndexError, OSError, ValueError) as exc:
            raise ValueError(f"sidebar skill contract is invalid: {path}") from exc

    @staticmethod
    def _contains_source_first_placement_rule(text: str) -> bool:
        clauses = re.split(r"(?<=[.!?])(?:\s+|$)|[\r\n]+", text.casefold())
        source_terms = (
            "source cwd",
            "source folder",
            "source directory",
            "source workspace",
            "source project",
            "source repository",
            "originating directory",
            "originating folder",
            "originating workspace",
            "git root",
        )
        selection_pattern = re.compile(
            r"\b(?:favor|prefer|prioritize|select|choose|use|place|route|target)\b"
        )
        precedence_terms = (
            " over ",
            " before ",
            " ahead of ",
            " first",
            " fallback",
            " instead of ",
        )
        negations = (
            "never ",
            "do not ",
            "must not ",
            "cannot ",
            "can't ",
            "not select",
            "not choose",
            "not use",
        )
        return any(
            "inbox" in clause
            and any(term in clause for term in source_terms)
            and selection_pattern.search(clause) is not None
            and any(term in clause for term in precedence_terms)
            and not any(term in clause for term in negations)
            for clause in clauses
        )

    def failure_code(self, label: str) -> str:
        try:
            return self.failure_codes[label]
        except KeyError as exc:
            raise ValueError("sidebar skill contract has no fixed failure") from exc

    def choose_project(
        self,
        projects: dict[str, str],
        *,
        cwd: str,
        git_root: str | None,
        inbox: str,
    ) -> str:
        candidates = {"cwd": cwd, "git_root": git_root, "inbox": inbox}
        for source in self.project_precedence:
            candidate = candidates[source]
            if candidate is not None and candidate in projects:
                return projects[candidate]
        raise ValueError(
            self.failure_code("Session Inbox unavailable")
        )

    def validate_status(self, status: object, *, inbox: str) -> bool:
        if not isinstance(status, dict):
            return False
        health = status.get("health")
        sidebar = status.get("sidebar")
        if not isinstance(health, dict) or not isinstance(sidebar, dict):
            return False
        if (
            health.get("running") is not True
            or health.get("watcher_state") != "running"
        ):
            return False
        providers = health.get("providers")

        def valid_optional_number(value: object) -> bool:
            return value is None or (
                type(value) in (int, float) and math.isfinite(float(value))
            )

        def valid_provider(provider: object) -> bool:
            if not isinstance(provider, dict) or not {
                "last_success",
                "lag_seconds",
                "degraded_reason",
            }.issubset(provider):
                return False
            degraded_reason = provider["degraded_reason"]
            return (
                valid_optional_number(provider["last_success"])
                and valid_optional_number(provider["lag_seconds"])
                and (
                    degraded_reason is None
                    or (
                        type(degraded_reason) is str
                        and re.fullmatch(
                            r"[a-z][a-z0-9_]{0,127}",
                            degraded_reason,
                        )
                        is not None
                    )
                )
            )

        if not isinstance(providers, dict) or any(
            type(provider_name) is not str
            or not provider_name
            or not valid_provider(provider)
            for provider_name, provider in providers.items()
        ):
            return False
        placement = sidebar.get("placement")
        counts = sidebar.get("counts")
        hydration = sidebar.get("hydration")
        hydration_counts = (
            hydration.get("counts") if isinstance(hydration, dict) else None
        )
        if (
            not isinstance(placement, dict)
            or not isinstance(counts, dict)
            or not isinstance(hydration_counts, dict)
        ):
            return False
        try:
            status_inbox = _canonical_sidebar_path(placement.get("inbox_cwd", ""))
        except (OSError, TypeError, ValueError):
            return False
        required_counts = (
            SidebarJobState.PENDING.value,
            SidebarJobState.RETRY.value,
        )
        required_hydration_counts = (
            "pending",
            "retry",
        )
        return (
            status_inbox == _canonical_sidebar_path(inbox)
            and placement.get("generation") == 1
            and all(
                type(counts.get(state)) is int and counts[state] >= 0
                for state in required_counts
            )
            and all(
                type(hydration_counts.get(state)) is int
                and hydration_counts[state] >= 0
                for state in required_hydration_counts
            )
        )

    def build_project_map(
        self,
        projects: object,
        *,
        inbox: str,
    ) -> dict[str, str]:
        if not isinstance(projects, list):
            raise ValueError("project preflight")
        indexed: dict[str, str] = {}
        inbox_matches = 0
        canonical_inbox = _canonical_sidebar_path(inbox)
        for project in projects:
            if not isinstance(project, dict):
                raise ValueError("project preflight")
            project_id = project.get("projectId")
            path = project.get("path")
            host_id = project.get("hostId")
            if (
                type(project_id) is not str
                or not project_id
                or project_id != project_id.strip()
                or type(path) is not str
                or not path
                or host_id not in (None, "local")
            ):
                raise ValueError("project preflight")
            canonical_path = _canonical_sidebar_path(path)
            if canonical_path in indexed:
                raise ValueError("project preflight")
            indexed[canonical_path] = project_id
            if canonical_path == canonical_inbox:
                inbox_matches += 1
        if inbox_matches != 1:
            raise ValueError("project preflight")
        return indexed

    def resolve_placement(
        self,
        projects: dict[str, str],
        *,
        cwd: str,
        git_root: str | None,
        inbox: str,
    ) -> _SidebarSkillPlacement:
        project_id = self.choose_project(
            projects,
            cwd=cwd,
            git_root=git_root,
            inbox=inbox,
        )
        sources = {
            "inbox": inbox,
            "source": cwd,
        }
        placement_cwd = sources[self.create_cwd_source]
        runtime_workspace_roots = tuple(
            dict.fromkeys(sources[source] for source in self.runtime_root_sources)
        )
        return _SidebarSkillPlacement(
            project_id=project_id,
            inbox_cwd=placement_cwd,
            source_cwd=cwd,
            runtime_workspace_roots=runtime_workspace_roots,
        )

    def create_arguments(
        self,
        *,
        prompt: str,
        placement: _SidebarSkillPlacement,
    ) -> dict[str, Any]:
        return {
            "prompt": prompt,
            "target": {
                "type": "project",
                "projectId": placement.project_id,
                "environment": {"type": "local"},
            },
        }

    def validate_trace(self, trace: list[dict[str, Any]]) -> None:
        if not trace or trace[0]["tool"] != self.status_tool:
            raise AssertionError("sidebar worker must start with status")
        if sum(event["tool"] == self.status_tool for event in trace) != 1:
            raise AssertionError("status must be called exactly once")
        if len(trace) == 1:
            return
        if trace[1]["tool"] != self.projects_tool:
            raise AssertionError("projects must be listed before pending")
        if sum(event["tool"] == self.projects_tool for event in trace) != 1:
            raise AssertionError("projects must be listed exactly once")
        if len(trace) == 2:
            return
        if len(trace) < 3 or trace[2]["tool"] != self.pending_tool:
            raise AssertionError("pending must be called after projects")
        if sum(event["tool"] == self.pending_tool for event in trace) != 1:
            raise AssertionError("pending must be called exactly once")
        if any("app-server" in event["tool"] for event in trace):
            raise AssertionError("app-server creation is forbidden")

        job_ids = {event.get("job") for event in trace if event.get("job")}
        for job_id in job_ids:
            events = [event for event in trace if event.get("job") == job_id]
            tools = [event["tool"] for event in events]
            ranks = {
                "project_choice": 0,
                self.reserve_tool: 3,
                self.create_tool: 4,
                self.bind_tool: 5,
                self.rename_tool: 7,
                self.commit_tool: 8,
                self.fail_tool: 9,
            }
            try:
                ordered: list[int] = []
                bound = False
                for tool in tools:
                    if tool == self.read_thread_tool:
                        ordered.append(6 if bound else 2)
                    else:
                        ordered.append(ranks[tool])
                    if tool == self.bind_tool:
                        bound = True
            except KeyError as exc:
                raise AssertionError("worker trace contains an unknown call") from exc
            if ordered != sorted(ordered):
                raise AssertionError("worker calls violate shipped procedure order")
            if self.create_tool in tools:
                create = events[tools.index(self.create_tool)]
                if create["arguments"]["prompt"] != create["registration_prompt"]:
                    raise AssertionError("registration prompt must be verbatim")
                project_choice = events[tools.index("project_choice")]["arguments"]
                placement = _SidebarSkillPlacement(
                    project_id=project_choice["project_id"],
                    inbox_cwd=project_choice["inbox_cwd"],
                    source_cwd=project_choice["source_cwd"],
                    runtime_workspace_roots=tuple(
                        project_choice["runtime_workspace_roots"]
                    ),
                )
                if create["arguments"] != self.create_arguments(
                    prompt=create["registration_prompt"],
                    placement=placement,
                ):
                    raise AssertionError(
                        "native create must follow the shipped placement contract"
                    )
                if self.reserve_tool not in tools or tools.index(
                    self.reserve_tool
                ) > tools.index(self.create_tool):
                    raise AssertionError("create must follow durable reservation")
            if self.rename_tool in tools:
                if self.bind_tool not in tools or tools.index(
                    self.bind_tool
                ) > tools.index(self.rename_tool):
                    raise AssertionError("bind must precede rename")
            if self.commit_tool in tools:
                if self.rename_tool not in tools or tools.index(
                    self.rename_tool
                ) > tools.index(self.commit_tool):
                    raise AssertionError("rename must precede commit")
            fail_events = [event for event in events if event["tool"] == self.fail_tool]
            if len(fail_events) > 1:
                raise AssertionError("lease may be failed only once")
            for event in fail_events:
                if event["arguments"]["error_code"] not in self.failure_codes.values():
                    raise AssertionError("failure code must come from shipped mapping")
                fail_index = events.index(event)
                known_thread_id: str | None = None
                for prior in events[:fail_index]:
                    if prior["tool"] == self.bind_tool:
                        candidate = prior["arguments"].get("codex_thread_id")
                        if isinstance(candidate, str):
                            known_thread_id = candidate
                    elif prior["tool"] == self.read_thread_tool:
                        candidate = prior["arguments"].get("threadId")
                        if isinstance(candidate, str):
                            known_thread_id = candidate
                if (
                    known_thread_id is not None
                    and event["arguments"].get("codex_thread_id") != known_thread_id
                ):
                    raise AssertionError(
                        "failure must retain the exact known native ID"
                    )


class _SyntheticCodexClient:
    """In-memory request surface; it never starts or contacts Codex."""

    def __init__(self) -> None:
        self.available = True
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._threads: dict[str, dict[str, Any]] = {}
        self._next_thread = 1
        self._next_turn = 1
        self._completed_turns: list[tuple[str, str]] = []
        self.seed_thread(
            "codex-history",
            content="codexhistoryneedle synthetic prompt",
            title="Synthetic Codex history",
            cwd="C:/synthetic/codex",
        )

    def seed_thread(
        self,
        native_id: str,
        *,
        content: str,
        title: str,
        cwd: str | None,
    ) -> None:
        self._threads[native_id] = {
            "id": native_id,
            "title": title,
            "cwd": cwd,
            "createdAt": 100.0,
            "updatedAt": 101.0,
            "archived": False,
            "revision": "revision-1",
            "turns": [self._user_turn(native_id, content)],
        }

    def append_user_turn(self, native_id: str, content: str) -> None:
        thread = self._threads[native_id]
        thread["turns"].append(self._user_turn(native_id, content))
        self._touch(thread)

    def archive_thread(self, native_id: str) -> None:
        thread = self._threads[native_id]
        thread["archived"] = True
        self._touch(thread)

    def delete_thread(self, native_id: str) -> None:
        del self._threads[native_id]

    def has_thread(self, native_id: str) -> bool:
        return native_id in self._threads

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        del timeout
        self.calls.append((method, deepcopy(params)))
        if not self.available:
            raise RuntimeError("synthetic Codex outage")
        if method == "thread/list":
            archived = bool(params["archived"])
            return {
                "data": [
                    {
                        key: deepcopy(thread[key])
                        for key in (
                            "id",
                            "title",
                            "createdAt",
                            "updatedAt",
                            "archived",
                            "revision",
                        )
                    }
                    | (
                        {"cwd": deepcopy(thread["cwd"])}
                        if thread.get("cwd") is not None
                        else {}
                    )
                    for thread in self._threads.values()
                    if bool(thread["archived"]) is archived
                ]
            }
        if method == "thread/read":
            thread = self._threads[params["threadId"]]
            return {
                "thread": {
                    "id": thread["id"],
                    "turns": deepcopy(thread["turns"]),
                }
            }
        if method == "thread/start":
            native_id = f"codex-target-{self._next_thread}"
            self._next_thread += 1
            thread = {
                "id": native_id,
                "title": None,
                "createdAt": 200.0,
                "updatedAt": 200.0,
                "archived": False,
                "revision": "revision-1",
                "turns": [],
            }
            if params.get("cwd") is not None:
                thread["cwd"] = params["cwd"]
            self._threads[native_id] = thread
            return {"thread": {"id": native_id}}
        if method == "thread/name/set":
            thread = self._threads[params["threadId"]]
            thread["title"] = params["name"]
            self._touch(thread)
            return {}
        if method == "turn/start":
            native_id = params["threadId"]
            text = params["input"][0]["text"]
            turn = self._user_turn(native_id, text, completed=True)
            self._threads[native_id]["turns"].append(turn)
            self._touch(self._threads[native_id])
            self._completed_turns.append((native_id, turn["id"]))
            return {"turn": {"id": turn["id"], "status": "completed"}}
        raise AssertionError(f"unexpected synthetic Codex method: {method}")

    def take_notification(self, timeout: float = 0.0) -> dict[str, Any] | None:
        del timeout
        if not self._completed_turns:
            return None
        native_id, turn_id = self._completed_turns.pop(0)
        return {
            "method": "turn/completed",
            "params": {
                "threadId": native_id,
                "turn": {"id": turn_id, "status": "completed"},
            },
        }

    def _user_turn(
        self,
        native_id: str,
        content: str,
        *,
        completed: bool = True,
    ) -> dict[str, Any]:
        turn_number = self._next_turn
        self._next_turn += 1
        return {
            "id": f"turn-{native_id}-{turn_number}",
            "status": "completed" if completed else "inProgress",
            "createdAt": 100.0 + turn_number,
            "items": [
                {
                    "type": "userMessage",
                    "id": f"item-{native_id}-{turn_number}",
                    "createdAt": 100.0 + turn_number,
                    "content": [{"type": "text", "text": content}],
                }
            ],
        }

    @staticmethod
    def _touch(thread: dict[str, Any]) -> None:
        revision = int(str(thread["revision"]).rsplit("-", 1)[-1]) + 1
        thread["revision"] = f"revision-{revision}"
        thread["updatedAt"] = float(thread["updatedAt"]) + 1.0


class _ToggleClaudeAdapter:
    def __init__(self, delegate: ClaudeSourceAdapter) -> None:
        self.delegate = delegate
        self.available = True

    def discover(self) -> list[Path]:
        if not self.available:
            raise RuntimeError("synthetic Claude outage")
        return self.delegate.discover()

    def parse(self, path: Path) -> Any:
        if not self.available:
            raise RuntimeError("synthetic Claude outage")
        return self.delegate.parse(path)

    def find_native_session(self, native_id: str) -> Path | None:
        if not self.available:
            raise RuntimeError("synthetic Claude outage")
        return self.delegate.find_native_session(native_id)


class _SyntheticHarnessAdapter:
    """Provider boundary fake backed only by immutable synthetic projections."""

    def __init__(self, provider: Provider) -> None:
        self.provider = provider
        self.available = True
        self.sessions: dict[str, SessionProjection] = {}
        self.create_calls: list[str] = []

    def add(self, projection: SessionProjection) -> None:
        assert projection.provider is self.provider
        self.sessions[projection.native_id] = projection

    def create_placeholder(
        self,
        *,
        title: str,
        source_session_id: str,
        bridge_id: str,
        policy_generation: int,
        cwd: str | None = None,
        native_id: str | None = None,
    ) -> PlaceholderResult:
        del source_session_id, policy_generation
        if not self.available:
            raise RuntimeError(f"synthetic {self.provider.value} outage")
        target_native_id = native_id or f"{self.provider.value}-target-1"
        self.create_calls.append(target_native_id)
        self.add(
            _projection(
                self.provider,
                target_native_id,
                content="synthetic bridge placeholder",
                cwd=cwd,
                title=title,
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=bridge_id,
            )
        )
        return PlaceholderResult(
            native_id=target_native_id,
            canonical_session_id=canonical_session_id(
                self.provider,
                target_native_id,
            ),
            used_registration_turn=False,
            verified_at=1_000.0,
        )

    def find_native_session(self, native_id: str) -> Path | None:
        self._require_available()
        return Path(f"{native_id}.jsonl") if native_id in self.sessions else None

    def parse(self, path: Path) -> ClaudeParseResult:
        self._require_available()
        projection = self.sessions[path.stem]
        return ClaudeParseResult(
            projection=projection,
            cursor=ClaudeCursor(offset=1, head_length=1, head_hash="a" * 64),
            rebuild=True,
            malformed_lines=0,
            unknown_records=0,
        )

    def find_native_thread(
        self,
        native_id: str,
        *,
        source_kinds: tuple[str, ...] | None = None,
    ) -> SessionProjection | None:
        del source_kinds
        self._require_available()
        return self.sessions.get(native_id)

    def project_thread(self, summary: SessionProjection) -> SessionProjection:
        self._require_available()
        return summary

    def projection_has_marker_payload(
        self,
        projection: SessionProjection,
        payload: BridgeMarkerPayload,
    ) -> bool:
        return (
            projection.provider is payload.target_provider
            and projection.origin_bridge_id == payload.bridge_id
        )

    def advance(
        self,
        native_id: str,
        content: str,
        *,
        continuation: bool = False,
    ) -> SessionProjection:
        current = self.sessions[native_id]
        ordinal = len(current.messages)
        updated = replace(
            current,
            last_active=current.last_active + 1.0,
            messages=(
                *current.messages,
                ProjectedMessage(
                    native_event_id=f"{native_id}-event-{ordinal}",
                    ordinal=0,
                    role="user",
                    content=content,
                    timestamp=current.last_active + 1.0,
                ),
            ),
            native_cursor=f"cursor-{native_id}-{ordinal + 1}",
            native_hash=hashlib.sha256(
                f"{native_id}:{ordinal + 1}:{content}".encode()
            ).hexdigest(),
            origin_kind=(
                OriginKind.BRIDGE_CONTINUATION if continuation else current.origin_kind
            ),
        )
        self.sessions[native_id] = updated
        return updated

    def archive(self, native_id: str) -> SessionProjection:
        updated = replace(self.sessions[native_id], native_status="archived")
        self.sessions[native_id] = updated
        return updated

    def delete(self, native_id: str) -> None:
        del self.sessions[native_id]

    def _require_available(self) -> None:
        if not self.available:
            raise RuntimeError(f"synthetic {self.provider.value} outage")


def _projection(
    provider: Provider,
    native_id: str,
    *,
    content: str,
    cwd: str | None,
    title: str | None = None,
    origin_kind: OriginKind = OriginKind.NATIVE,
    origin_bridge_id: str | None = None,
) -> SessionProjection:
    return SessionProjection(
        provider=provider,
        native_id=native_id,
        title=title or f"Synthetic {provider.value} session",
        cwd=cwd,
        started_at=100.0,
        last_active=101.0,
        messages=(
            ProjectedMessage(
                native_event_id=f"{native_id}-event-0",
                ordinal=0,
                role="user",
                content=content,
                timestamp=101.0,
            ),
        ),
        native_status="active",
        native_cursor=f"cursor-{native_id}-1",
        native_hash=hashlib.sha256(f"{native_id}:1:{content}".encode()).hexdigest(),
        origin_kind=origin_kind,
        origin_bridge_id=origin_bridge_id,
    )


def _write_claude_transcript(
    path: Path,
    *,
    native_id: str,
    content: str,
    cwd: str | None = "C:/synthetic/claude",
    title: str | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    if title is not None:
        records.append({
            "type": "custom-title",
            "sessionId": native_id,
            "customTitle": title,
        })
    records.extend([
        {
            "type": "user",
            "sessionId": native_id,
            "uuid": f"event-{native_id}",
            "timestamp": "2026-07-13T10:00:00Z",
            "cwd": cwd,
            "isSidechain": False,
            "message": {"role": "user", "content": content},
        },
        {
            "type": "assistant",
            "sessionId": native_id,
            "uuid": f"response-{native_id}",
            "timestamp": "2026-07-13T10:00:01Z",
            "cwd": cwd,
            "isSidechain": False,
            "message": {"role": "assistant", "content": "synthetic response"},
        },
    ])
    path.write_text(
        "".join(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
            for record in records
        ),
        encoding="utf-8",
    )
    return path


def _append_claude_user(
    path: Path,
    *,
    native_id: str,
    content: str,
    cwd: str | None = None,
) -> None:
    record = {
        "type": "user",
        "sessionId": native_id,
        "uuid": f"event-{native_id}-{path.stat().st_size}",
        "timestamp": "2026-07-13T10:00:02Z",
        "cwd": cwd,
        "isSidechain": False,
        "message": {"role": "user", "content": content},
    }
    with path.open("a", encoding="utf-8", newline="") as transcript:
        transcript.write(
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        )


class _SyntheticClaudeRunner:
    """Injected process boundary that persists exactly what Claude CLI was asked."""

    def __init__(self, projects_root: Path) -> None:
        self.projects_root = projects_root
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.paths: dict[str, Path] = {}

    def __call__(
        self,
        args: list[str],
        **kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        self.calls.append((list(args), dict(kwargs)))
        native_id = args[args.index("--session-id") + 1]
        title = args[args.index("--name") + 1]
        path = self.projects_root / "bridge-targets" / f"{native_id}.jsonl"
        self.paths[native_id] = _write_claude_transcript(
            path,
            native_id=native_id,
            title=title,
            cwd=kwargs.get("cwd"),
            content=args[-1],
        )
        return subprocess.CompletedProcess(args, 0, stdout="{}", stderr="")


@pytest.mark.asyncio
async def test_all_history_imports_claude_codex_and_hermes_into_fts(
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "synthetic-claude-projects"
    _write_claude_transcript(
        claude_root / "project" / "claude-history.jsonl",
        native_id="claude-history",
        content="claudehistoryneedle synthetic prompt",
    )
    codex_client = _SyntheticCodexClient()
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        db.create_session("hermes-history", "cli", cwd="C:/synthetic/hermes")
        db.append_message(
            "hermes-history",
            "user",
            "hermeshistoryneedle synthetic prompt",
            timestamp=103.0,
        )
        coordinator = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=store,
            adapters={
                Provider.CLAUDE: ClaudeSourceAdapter(
                    claude_root,
                    marker_secret=_MARKER_SECRET,
                ),
                Provider.CODEX: CodexSourceAdapter(
                    codex_client,
                    marker_secret=_MARKER_SECRET,
                ),
            },
        )

        summary = await coordinator.scan_all_history()

        assert (summary.discovered, summary.indexed, summary.failed) == (2, 2, 0)
        catalog = UnifiedCatalog(db, store)
        expected = {
            "claudehistoryneedle": "claude:claude-history",
            "codexhistoryneedle": "codex:codex-history",
            "hermeshistoryneedle": "hermes-history",
        }
        for query, session_id in expected.items():
            result = catalog.search(query=query)
            assert [row["session_id"] for row in result["results"]] == [session_id]
        assert [
            params["archived"]
            for method, params in codex_client.calls
            if method == "thread/list"
        ] == [False, True]
    finally:
        db.close()


@pytest.mark.asyncio
async def test_restart_mid_import_resumes_without_duplicate_catalog_rows(
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "synthetic-claude-projects"
    for suffix in ("one", "two"):
        _write_claude_transcript(
            claude_root / "project" / f"restart-{suffix}.jsonl",
            native_id=f"restart-{suffix}",
            content=f"restart{suffix}needle synthetic prompt",
        )
    db_path = tmp_path / "state.db"
    db = SessionDB(db_path=db_path)
    try:
        first = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=SessionBridgeStore(db, clock=lambda: 1_000.0),
            adapters={
                Provider.CLAUDE: ClaudeSourceAdapter(
                    claude_root,
                    marker_secret=_MARKER_SECRET,
                )
            },
            scan_batch_size=1,
        )
        first_pass = await first.scan_once(Provider.CLAUDE)
        assert (first_pass.discovered, first_pass.indexed, first_pass.failed) == (
            2,
            1,
            0,
        )
    finally:
        db.close()

    restarted_db = SessionDB(db_path=db_path)
    try:
        restarted_store = SessionBridgeStore(restarted_db, clock=lambda: 1_001.0)
        restarted = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=restarted_store,
            adapters={
                Provider.CLAUDE: ClaudeSourceAdapter(
                    claude_root,
                    marker_secret=_MARKER_SECRET,
                )
            },
            scan_batch_size=1,
        )

        resumed = await restarted.scan_once(Provider.CLAUDE)
        replay = await restarted.scan_once(Provider.CLAUDE)

        assert (resumed.indexed, resumed.failed) == (1, 0)
        assert (replay.discovered, replay.indexed, replay.failed) == (0, 0, 0)
        rows = UnifiedCatalog(restarted_db, restarted_store).search(
            provider="claude",
            limit=10,
        )["results"]
        assert {row["session_id"] for row in rows} == {
            "claude:restart-one",
            "claude:restart-two",
        }
        assert sum(row["message_count"] for row in rows) == 4
    finally:
        restarted_db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", (Provider.CLAUDE, Provider.CODEX))
async def test_provider_outage_is_isolated_and_later_scan_recovers(
    provider: Provider,
    tmp_path: Path,
) -> None:
    claude_root = tmp_path / "synthetic-claude-projects"
    _write_claude_transcript(
        claude_root / "project" / "claude-history.jsonl",
        native_id="claude-history",
        content="claude recovery prompt",
    )
    claude = _ToggleClaudeAdapter(
        ClaudeSourceAdapter(claude_root, marker_secret=_MARKER_SECRET)
    )
    codex_client = _SyntheticCodexClient()
    codex = CodexSourceAdapter(codex_client, marker_secret=_MARKER_SECRET)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        coordinator = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=store,
            adapters={Provider.CLAUDE: claude, Provider.CODEX: codex},
        )
        if provider is Provider.CLAUDE:
            claude.available = False
        else:
            codex_client.available = False

        outage = await coordinator.scan_once(provider)
        assert (outage.indexed, outage.failed) == (0, 1)
        assert (
            coordinator.health()["providers"][provider.value]["degraded_reason"]
            is not None
        )

        claude.available = True
        codex_client.available = True
        recovered = await coordinator.scan_once(provider)

        assert (recovered.indexed, recovered.failed) == (1, 0)
        assert (
            coordinator.health()["providers"][provider.value]["degraded_reason"] is None
        )
        assert UnifiedCatalog(db, store).search(
            provider=provider.value,
            limit=10,
        )["results"]
    finally:
        db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source_provider", "target_provider"),
    (
        (Provider.CLAUDE, Provider.CODEX),
        (Provider.CODEX, Provider.CLAUDE),
    ),
)
async def test_bidirectional_handoff_hydration_continuation_and_local_lifecycle(
    source_provider: Provider,
    target_provider: Provider,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claude_root = tmp_path / "synthetic-claude-projects"
    codex_client = _SyntheticCodexClient()
    source_native_id = f"{source_provider.value}-source"
    source_content = (
        f"{source_provider.value}handoffneedle synthetic work; "
        "mempalace://drawer/synthetic and "
        "gbrain://page/synthetic/session-bridge"
    )
    if source_provider is Provider.CLAUDE:
        _write_claude_transcript(
            claude_root / "source" / f"{source_native_id}.jsonl",
            native_id=source_native_id,
            content=source_content,
            cwd=None,
        )
    else:
        codex_client.seed_thread(
            source_native_id,
            content=source_content,
            title="Synthetic Codex source",
            cwd=None,
        )

    claude_source = ClaudeSourceAdapter(
        claude_root,
        marker_secret=_MARKER_SECRET,
    )
    codex_source = CodexSourceAdapter(
        codex_client,
        marker_secret=_MARKER_SECRET,
    )
    claude_runner = _SyntheticClaudeRunner(claude_root)
    claude_target = ClaudeTargetAdapter(
        claude_source,
        marker_secret=_MARKER_SECRET,
        claude_executable=("synthetic-claude",),
        runner=claude_runner,
        clock=lambda: 1_000.0,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
        discovery_timeout=0.0,
    )
    codex_target = CodexTargetAdapter(
        codex_client,
        source_adapter=codex_source,
        marker_secret=_MARKER_SECRET,
        clock=lambda: 1_000.0,
        monotonic=lambda: 0.0,
        sleep=lambda _seconds: None,
        request_timeout=1.0,
        require_registration_turn=True,
        verification_timeout=0.0,
    )
    adapters = {
        Provider.CLAUDE: claude_source,
        Provider.CODEX: codex_source,
    }
    target_adapters = {
        Provider.CLAUDE: claude_target,
        Provider.CODEX: codex_target,
    }

    def read_projection(provider: Provider, native_id: str) -> SessionProjection:
        if provider is Provider.CLAUDE:
            path = claude_source.find_native_session(native_id)
            assert path is not None
            return claude_source.parse(path).projection
        summary = codex_source.find_native_thread(native_id)
        assert summary is not None
        return codex_source.project_thread(summary)

    def append_provider_user(
        provider: Provider,
        native_id: str,
        content: str,
    ) -> None:
        if provider is Provider.CLAUDE:
            path = claude_source.find_native_session(native_id)
            assert path is not None
            _append_claude_user(
                path,
                native_id=native_id,
                content=content,
            )
        else:
            codex_client.append_user_turn(native_id, content)

    source = read_projection(source_provider, source_native_id)
    assert source.messages[0].content == source_content
    assert source.cwd is None
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        source_id = store.upsert_projection(source).session_id
        policy = MirrorPolicy(automatic_creation=False)
        job = enqueue_mirror_job(
            store,
            source_id,
            target_provider,
            policy=policy,
            manual_authorized=True,
        )
        coordinator = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=store,
            adapters=adapters,
            target_adapters={target_provider: target_adapters[target_provider]},
            context_builder=ContextPackBuilder(db, store),
            clock=lambda: 1_000.0,
        )

        mirrored = await coordinator.process_jobs_once(job_ids=[job["id"]])

        assert (mirrored.claimed, mirrored.succeeded) == (1, 1)
        link = store.get_bridge_summaries([source_id])[source_id]["bridge_links"][0]
        bridge_id = link["bridge_id"]
        assert link["relation"] == Relation.MIRRORS.value
        target_id = link["to_session_id"]
        target_native_id = target_id.split(":", 1)[1]
        if target_provider is Provider.CLAUDE:
            assert len(claude_runner.calls) == 1
        else:
            assert (
                sum(method == "thread/start" for method, _params in codex_client.calls)
                == 1
            )
        marker_payload = BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id=source_id,
            target_provider=target_provider,
            policy_generation=policy.generation,
        )
        target_projection = read_projection(target_provider, target_native_id)
        assert target_projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
        assert target_projection.origin_bridge_id == bridge_id
        if target_provider is Provider.CLAUDE:
            assert claude_source.projection_has_marker_payload(
                target_projection,
                marker_payload,
            )
        else:
            assert codex_source.projection_has_marker_payload(
                target_projection,
                marker_payload,
            )
        request = ContinueRequest(
            session_id=source_id,
            bridge_id=bridge_id,
            target_provider=target_provider,
            context_budget_chars=8_000,
        )

        def memory_network_forbidden(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("continuation must not contact memory backends")

        monkeypatch.setattr(socket, "create_connection", memory_network_forbidden)
        continued = await coordinator.continue_session(request)

        assert continued.link.relation is Relation.CONTINUES
        assert continued.pack.immutable_at == 1_000.0
        assert "[missing cwd]" in continued.pack.payload
        assert "mempalace://drawer/synthetic" in continued.pack.payload
        assert "gbrain://page/synthetic/session-bridge" in continued.pack.payload
        search = UnifiedCatalog(db, store).search(
            query=f"{source_provider.value}handoffneedle"
        )
        assert [row["session_id"] for row in search["results"]] == [source_id]

        append_provider_user(source_provider, source_native_id, "source advanced alone")
        append_provider_user(
            target_provider,
            target_native_id,
            "target advanced alone",
        )
        replay = await coordinator.continue_session(request)

        assert replay.pack.payload == continued.pack.payload
        assert replay.warnings == ("linked_sessions_diverged",)
        source_read = UnifiedCatalog(db, store).get(source_id)
        assert source_read["session"]["diverged"] is True
        assert "source advanced alone" not in replay.pack.payload
        assert "target advanced alone" not in replay.pack.payload
        target_row = store.get_external_session(target_id)
        assert target_row is not None
        assert target_row["origin_kind"] == OriginKind.BRIDGE_CONTINUATION.value

        codex_native_id = (
            source_native_id if source_provider is Provider.CODEX else target_native_id
        )
        claude_native_id = (
            source_native_id if source_provider is Provider.CLAUDE else target_native_id
        )
        codex_id = canonical_session_id(Provider.CODEX, codex_native_id)
        claude_id = canonical_session_id(Provider.CLAUDE, claude_native_id)

        codex_client.archive_thread(codex_native_id)
        archived = await coordinator.refresh_session(codex_id, timeout=1.0)
        archived_row = store.get_external_session(codex_id)
        claude_row = store.get_external_session(claude_id)
        assert archived.stale is False
        assert archived_row is not None
        assert claude_row is not None
        assert archived_row["native_status"] == "archived"
        assert claude_row["native_status"] == "active"

        codex_client.delete_thread(codex_native_id)
        codex_stale = await coordinator.refresh_session(codex_id, timeout=1.0)
        claude_path = claude_source.find_native_session(claude_native_id)
        assert codex_stale.stale is True
        assert claude_path is not None

        claude_path.unlink()
        claude_stale = await coordinator.refresh_session(claude_id, timeout=1.0)
        assert claude_stale.stale is True
        assert claude_stale.warning == "source_refresh_failed_using_durable_snapshot"
        assert codex_stale.warning == "source_refresh_failed_using_durable_snapshot"
        assert codex_client.has_thread(codex_native_id) is False
        assert store.get_external_session(codex_id) is not None
        assert store.get_external_session(claude_id) is not None
    finally:
        db.close()


@pytest.mark.asyncio
async def test_restart_mid_job_recovers_exact_claude_target_once(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state.db"
    source_adapter = _SyntheticHarnessAdapter(Provider.CODEX)
    target_adapter = _SyntheticHarnessAdapter(Provider.CLAUDE)
    source = _projection(
        Provider.CODEX,
        "restart-job-source",
        content="restart job source",
        cwd="C:/synthetic/restart-job",
    )
    source_adapter.add(source)
    db = SessionDB(db_path=db_path)
    try:
        store = SessionBridgeStore(db, clock=lambda: 1_000.0)
        source_id = store.upsert_projection(source).session_id
        policy = MirrorPolicy(automatic_creation=False)
        job = enqueue_mirror_job(
            store,
            source_id,
            Provider.CLAUDE,
            policy=policy,
            manual_authorized=True,
        )
        claimed = store.claim_due_jobs_with_limits(
            now=1_000.0,
            limit=1,
            policy=policy,
            job_ids=[job["id"]],
        )
        assert len(claimed) == 1
        bridge_id = (
            "bridge:"
            + hashlib.sha256(
                f"session-bridge:{job['idempotency_key']}".encode()
            ).hexdigest()
        )
        target_native_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"hermes-session-bridge:{job['idempotency_key']}",
            )
        )
        target_adapter.add(
            _projection(
                Provider.CLAUDE,
                target_native_id,
                content="created before synthetic crash",
                cwd="C:/synthetic/restart-job",
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=bridge_id,
            )
        )
        store.set_state(
            f"session-bridge:attempt:{job['id']}",
            {
                "version": 1,
                "phase": "provider_call_started",
                "bridge_id": bridge_id,
                "target_provider": Provider.CLAUDE.value,
                "policy_generation": policy.generation,
                "attempts": claimed[0]["attempts"],
                "expected_native_id": target_native_id,
            },
        )
    finally:
        db.close()

    restarted_db = SessionDB(db_path=db_path)
    try:
        restarted_store = SessionBridgeStore(restarted_db, clock=lambda: 1_001.0)
        restarted = SessionBridgeCoordinator(
            config=BridgeConfig(),
            store=restarted_store,
            adapters={
                Provider.CODEX: source_adapter,
                Provider.CLAUDE: target_adapter,
            },
            target_adapters={Provider.CLAUDE: target_adapter},
            clock=lambda: 1_001.0,
        )

        recovered = await restarted.reconcile_once()
        replay = await restarted.reconcile_once()

        assert (recovered.examined, recovered.recovered, recovered.failed) == (
            1,
            1,
            0,
        )
        assert replay.recovered == 0
        assert target_adapter.create_calls == []
        assert set(target_adapter.sessions) == {target_native_id}
        assert restarted_store.mirror_job_counts()["succeeded"] == 1
        target_id = canonical_session_id(Provider.CLAUDE, target_native_id)
        assert restarted_store.get_external_session(target_id) is not None
        links = restarted_store.get_bridge_summaries([source_id])[source_id][
            "bridge_links"
        ]
        assert len(links) == 1
        assert links[0]["to_session_id"] == target_id
        assert links[0]["relation"] == Relation.MIRRORS.value
    finally:
        restarted_db.close()


class _SidebarMcpCoordinator:
    """Expose the real coordinator's public sidebar methods without background loops."""

    def __init__(self, delegate: SessionBridgeCoordinator) -> None:
        self.delegate = delegate

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def claim_sidebar_jobs_for_delivery(self, *, limit: int):
        return await self.delegate.claim_sidebar_jobs_for_delivery(limit=limit)

    async def claim_sidebar_hydration_for_delivery(self, *, limit: int):
        return await self.delegate.claim_sidebar_hydration_for_delivery(limit=limit)

    async def commit_sidebar_job(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
        ensure_lineage: bool = False,
    ):
        return await self.delegate.commit_sidebar_job(
            lease_token=lease_token,
            codex_thread_id=codex_thread_id,
            ensure_lineage=ensure_lineage,
        )

    async def bind_sidebar_thread(
        self,
        *,
        lease_token: str,
        codex_thread_id: str,
    ):
        return await self.delegate.bind_sidebar_thread(
            lease_token=lease_token,
            codex_thread_id=codex_thread_id,
        )

    async def reserve_sidebar_create_authoritatively(
        self,
        *,
        lease_token: str,
        reconciliation_proof_digest: str,
        reconciliation_generation: str,
    ):
        return await self.delegate.reserve_sidebar_create_authoritatively(
            lease_token=lease_token,
            reconciliation_proof_digest=reconciliation_proof_digest,
            reconciliation_generation=reconciliation_generation,
        )

    def health(self) -> dict[str, Any]:
        return self.delegate.health()


class _FakeNativeCodexTasks:
    """Native Codex project/task surface used by the Task 11 broker tests."""

    def __init__(self, marker_secret: bytes, *, on_create=None) -> None:
        self.marker_secret = marker_secret
        self.on_create = on_create
        self.projects: list[dict[str, Any]] = []
        self.threads: dict[str, dict[str, Any]] = {}
        self.create_calls: list[dict[str, Any]] = []
        self.rename_calls: list[tuple[str, str]] = []
        self.reconciliation_calls: list[BridgeMarkerPayload] = []
        self.app_server_create_calls: list[dict[str, Any]] = []
        self.available = True
        self.rename_failures_remaining = 0
        self.next_thread_id: str | None = None
        self.send_calls: list[tuple[str, str]] = []
        self.drop_create_response: str | None = None

    def add_project(self, project_id: str, path: Path) -> None:
        self.projects.append({
            "projectId": project_id,
            "path": _canonical_sidebar_path(path),
            "hostId": None,
        })

    def list_projects(self) -> list[dict[str, Any]]:
        return [dict(project) for project in self.projects]

    def list_threads(self, *, query: str, limit: int) -> list[dict[str, Any]]:
        assert limit == 20
        return [
            {
                "threadId": thread["thread_id"],
                "projectId": thread["project_id"],
                "hostId": None,
            }
            for thread in self.threads.values()
            if thread["marker"] == query
        ][:limit]

    def read_thread(self, *, thread_id: str) -> dict[str, Any]:
        return dict(self.threads[thread_id])

    def create_thread(
        self,
        *,
        prompt: str,
        target: dict[str, Any] | None = None,
        cwd: str | None = None,
        runtimeWorkspaceRoots: list[str] | None = None,
    ) -> str:
        if not self.available:
            raise RuntimeError("synthetic Desktop offline")
        registration = decode_sidebar_registration_identity(
            prompt,
            self.marker_secret,
        )
        if target is not None:
            if target.get("type") != "project" or target.get("environment") != {
                "type": "local"
            }:
                raise AssertionError("native task target must be a local project")
            project_id = target.get("projectId")
            project = next(
                (
                    project
                    for project in self.projects
                    if project["projectId"] == project_id
                ),
                None,
            )
            if project is None:
                raise AssertionError("native task project must be saved")
            canonical_cwd = project["path"]
            runtime_roots = (
                canonical_cwd,
                _canonical_sidebar_path(registration.source_cwd),
            )
            desktop_request = {"prompt": prompt, "target": deepcopy(target)}
        else:
            if cwd is None or runtimeWorkspaceRoots is None:
                raise AssertionError("legacy fake create requires cwd and roots")
            canonical_cwd = _canonical_sidebar_path(cwd)
            project_id = next(
                (
                    project["projectId"]
                    for project in self.projects
                    if project["path"] == canonical_cwd
                ),
                None,
            )
            if project_id is None:
                raise AssertionError("native task cwd must resolve to a saved project")
            runtime_roots = tuple(
                _canonical_sidebar_path(root) for root in runtimeWorkspaceRoots
            )
            desktop_request = {
                "prompt": prompt,
                "cwd": cwd,
                "runtimeWorkspaceRoots": list(runtimeWorkspaceRoots),
            }
        thread_id = self.next_thread_id or f"native-sidebar-{len(self.threads) + 1}"
        self.next_thread_id = None
        marker = _registration_marker(prompt)
        payload = decode_bridge_marker(marker, self.marker_secret)
        call = {
            "thread_id": thread_id,
            "prompt": prompt,
            "project_id": project_id,
            "source_cwd": registration.source_cwd,
            "cwd": canonical_cwd,
            "runtime_workspace_roots": runtime_roots,
            "desktop_request": desktop_request,
        }
        self.create_calls.append(call)
        if self.drop_create_response == "before_processing":
            raise RuntimeError("synthetic create response drop before processing")
        self.threads[thread_id] = {
            **call,
            "title": None,
            "marker": marker,
            "payload": payload,
            "assistant_reply": "REGISTERED",
            "session_continue_calls": [],
            "turns": [
                {"role": "user", "content": prompt, "status": "completed"},
                {"role": "assistant", "content": "REGISTERED", "status": "completed"},
            ],
        }
        if self.on_create is not None:
            self.on_create(self.threads[thread_id])
        if self.drop_create_response == "after_processing":
            raise RuntimeError("synthetic create response drop after processing")
        return thread_id

    def send_message_to_thread(
        self,
        *,
        thread_id: str,
        message: str,
        drop_after_append: bool = False,
    ) -> dict[str, Any]:
        thread = self.threads[thread_id]
        self.send_calls.append((thread_id, message))
        thread["turns"].append({
            "role": "user",
            "content": message,
            "status": "completed",
        })
        thread["turns"].append({
            "role": "assistant",
            "content": "HYDRATED",
            "status": "completed",
        })
        if drop_after_append:
            raise RuntimeError("synthetic hydration response drop")
        return {"threadId": thread_id, "status": "completed"}

    def set_thread_title(self, thread_id: str, title: str) -> None:
        self.rename_calls.append((thread_id, title))
        if self.rename_failures_remaining:
            self.rename_failures_remaining -= 1
            raise RuntimeError("synthetic rename failure")
        self.threads[thread_id]["title"] = title

    def find_by_marker(
        self, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread | None:
        self.reconciliation_calls.append(expected)
        matches = [
            thread for thread in self.threads.values() if thread["payload"] == expected
        ]
        if not matches:
            return None
        assert len(matches) == 1, "fake native inventory must never hide duplicates"
        return _verified_native_thread(matches[0])

    def reconcile_marker(
        self,
        expected: BridgeMarkerPayload,
        *,
        now: float,
        ttl_seconds: float,
    ) -> SidebarReconciliationEvidence:
        self.reconciliation_calls.append(expected)
        marker = encode_bridge_marker(expected, self.marker_secret)
        matches = [
            thread for thread in self.threads.values() if thread["marker"] == marker
        ]
        marker_digest = hashlib.sha256(marker.encode("utf-8")).hexdigest()
        inventory_digest = hashlib.sha256(
            "\0".join(sorted(self.threads)).encode("utf-8")
        ).hexdigest()
        generation = f"synthetic:{len(self.reconciliation_calls)}:{inventory_digest}"
        if len(matches) == 1:
            state = SidebarReconciliationState.RECOVERED
            recovered_thread_id = matches[0]["thread_id"]
            fixed_reason = None
        elif not matches:
            state = SidebarReconciliationState.ABSENCE_PROVEN
            recovered_thread_id = None
            fixed_reason = None
        else:
            state = SidebarReconciliationState.BLOCKED
            recovered_thread_id = None
            fixed_reason = "marker_conflict"
        return SidebarReconciliationEvidence.create(
            state=state,
            generation=generation,
            completed_at=now,
            expires_at=now + ttl_seconds,
            inventory_digest=inventory_digest,
            marker_digest=marker_digest,
            match_count=len(matches),
            recovered_thread_id=recovered_thread_id,
            fixed_reason=fixed_reason,
        )

    def verify_thread(
        self, *, thread_id: str, expected: BridgeMarkerPayload
    ) -> VerifiedSidebarThread:
        thread = self.threads[thread_id]
        assert thread["payload"] == expected
        return _verified_native_thread(thread)


class _CommitDroppingClient:
    """Drop one public commit request before or after MCP server processing."""

    def __init__(self, delegate: TestClient, *, timing: str) -> None:
        if timing not in {"before_processing", "after_processing"}:
            raise ValueError("commit drop timing is invalid")
        self.delegate = delegate
        self.timing = timing
        self.commit_attempts = 0
        self.dropped = False
        self.tool_calls: list[str] = []

    @property
    def headers(self):
        return self.delegate.headers

    def post(self, *args: Any, **kwargs: Any):
        payload = kwargs.get("json")
        tool_name = None
        if isinstance(payload, dict) and payload.get("method") == "tools/call":
            params = payload.get("params")
            if isinstance(params, dict):
                tool_name = params.get("name")
                if isinstance(tool_name, str):
                    self.tool_calls.append(tool_name)
        if tool_name != "session_sidebar_commit" or self.dropped:
            return self.delegate.post(*args, **kwargs)

        self.commit_attempts += 1
        self.dropped = True
        if self.timing == "before_processing":
            raise httpx.ReadError("synthetic commit connection drop")
        self.delegate.post(*args, **kwargs)
        raise httpx.ReadError("synthetic commit response drop")


class _BindDroppingClient:
    """Drop one public bind request before or after MCP server processing."""

    def __init__(self, delegate: TestClient, *, timing: str) -> None:
        if timing not in {"before_processing", "after_processing"}:
            raise ValueError("bind drop timing is invalid")
        self.delegate = delegate
        self.timing = timing
        self.bind_attempts = 0
        self.dropped = False
        self.tool_calls: list[str] = []

    @property
    def headers(self):
        return self.delegate.headers

    def post(self, *args: Any, **kwargs: Any):
        payload = kwargs.get("json")
        tool_name = None
        if isinstance(payload, dict) and payload.get("method") == "tools/call":
            params = payload.get("params")
            if isinstance(params, dict):
                tool_name = params.get("name")
                if isinstance(tool_name, str):
                    self.tool_calls.append(tool_name)
        if tool_name != "session_sidebar_bind" or self.dropped:
            return self.delegate.post(*args, **kwargs)

        self.bind_attempts += 1
        self.dropped = True
        if self.timing == "before_processing":
            raise httpx.ReadError("synthetic bind connection drop")
        self.delegate.post(*args, **kwargs)
        raise httpx.ReadError("synthetic bind response drop")


class _SidebarEndToEndHarness:
    """Drive registration and delivery through the public MCP tools."""

    def __init__(
        self,
        tmp_path: Path,
        *,
        claude_projects_root: Path | None = None,
        skill_path: Path = _SIDEBAR_SKILL_PATH,
    ) -> None:
        self.contract = _SidebarSkillContract.load(skill_path)
        self.now = time.time()
        self.db_path = tmp_path / "sidebar-e2e-state.db"
        self.db = SessionDB(self.db_path)
        # The hermetic Windows runner intentionally clears HOME/TEMP-like
        # ambient state. Keep this high-volume end-to-end fixture's SQLite
        # scratch pages inside the connection rather than depending on a
        # process-global temp directory.
        self.db._conn.execute("PRAGMA temp_store=MEMORY")
        self.store = SessionBridgeStore(
            self.db,
            clock=lambda: self.now,
            sidebar_jitter=lambda _bound: 0.0,
        )
        self.inbox = get_hermes_home()
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.config = replace(
            BridgeConfig(),
            sidebar=replace(
                SidebarConfig(),
                enabled=True,
                continuous=False,
                inbox_cwd=str(self.inbox),
            ),
        )
        self.native = _FakeNativeCodexTasks(
            _MARKER_SECRET,
            on_create=self._index_native_thread,
        )
        adapters = {}
        if claude_projects_root is not None:
            adapters[Provider.CLAUDE] = ClaudeSourceAdapter(
                claude_projects_root,
                marker_secret=_MARKER_SECRET,
            )
        self.adapters = adapters
        self.coordinator = SessionBridgeCoordinator(
            config=self.config,
            store=self.store,
            adapters=adapters,
            target_adapters={},
            sidebar_verifier=self.native,
            clock=lambda: self.now,
        )
        self._mark_coordinator_healthy()
        self.catalog = UnifiedCatalog(self.db, self.store)
        self.production_backend: Any | None = None
        self.production_codex_target: CodexTargetAdapter | None = None
        self.allow_forbidden_app_server_fallback_for_mutation = False
        self.status_mutator = None
        self.worker_traces: list[list[dict[str, Any]]] = []
        self.native.add_project("session-inbox", self.inbox)
        self._rebuild_app()

    def _mark_coordinator_healthy(self) -> None:
        self.coordinator._running = True
        self.coordinator._watcher_state = "running"

    def _rebuild_app(self) -> None:
        self.app = create_app(
            catalog=self.catalog,
            coordinator=_SidebarMcpCoordinator(self.coordinator),
            store=self.store,
            config=self.config,
            token=_SIDEBAR_TOKEN,
            marker_key=_MARKER_SECRET,
        )

    def close(self) -> None:
        if self.production_backend is not None:
            self.production_backend._db = None
            self.production_backend._store = None
            self.production_backend._catalog = None
            self.production_backend.close()
        self.db.close()

    def install_production_runtime(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> SessionBridgeCoordinator:
        monkeypatch.setattr(Path, "home", lambda: self.inbox.parent)
        from session_bridge.cli import ProductionBackend

        class CompositionCodexClient:
            def __init__(self) -> None:
                self.closed = False

            def request(self, *_args: Any, **_kwargs: Any):
                raise AssertionError("app-server request is outside sidebar delivery")

            def take_notification(self, timeout: float = 0.0) -> None:
                del timeout
                return None

            def close(self) -> None:
                self.closed = True

        client = CompositionCodexClient()
        backend = ProductionBackend(self.config)
        backend._db = self.db
        backend._store = self.store
        backend._catalog = self.catalog
        monkeypatch.setattr(
            "session_bridge.cli.resolve_marker_key",
            lambda: _MARKER_SECRET,
        )
        monkeypatch.setattr(
            "session_bridge.cli.resolve_cli_executable",
            lambda name: (name,),
        )
        monkeypatch.setattr(
            "session_bridge.cli.CodexAppServerClient",
            lambda **_kwargs: client,
        )
        coordinator = backend._provider_runtime(
            targets=True,
            catalog_only=False,
            providers=(Provider.CODEX,),
        )
        coordinator._sidebar_verifier = self.native
        target = coordinator._target_adapters[Provider.CODEX]
        assert isinstance(target, CodexTargetAdapter)
        self.production_codex_target = target
        self.coordinator = coordinator
        self._mark_coordinator_healthy()
        self.production_backend = backend
        self._rebuild_app()
        return coordinator

    def add_project(self, project_id: str, path: Path) -> None:
        self.native.add_project(project_id, path)

    def seed_source(
        self,
        provider: Provider,
        native_id: str,
        *,
        cwd: Path,
        content: str | None = "Build the native sidebar broker",
        messages: tuple[ProjectedMessage, ...] | None = None,
        git_root: Path | None = None,
    ) -> str:
        cwd.mkdir(parents=True, exist_ok=True)
        if provider is Provider.CLAUDE:
            if messages is None:
                messages = (
                    ()
                    if content is None
                    else (
                        ProjectedMessage(
                            native_event_id=f"event-{native_id}",
                            ordinal=0,
                            role="user",
                            content=content,
                            timestamp=self.now,
                        ),
                    )
                )
            projection = SessionProjection(
                provider=Provider.CLAUDE,
                native_id=native_id,
                title=f"Claude {native_id}",
                cwd=str(cwd),
                started_at=self.now - 10,
                last_active=self.now,
                messages=messages,
                native_path=str(cwd / f"{native_id}.jsonl"),
                native_cursor=f"cursor-{native_id}",
                native_hash=f"hash-{native_id}",
                origin_kind=OriginKind.NATIVE,
            )
            source_id = self.store.upsert_projection(projection).session_id
        elif provider is Provider.HERMES:
            source_id = native_id
            self.db.create_session(
                source_id,
                "cli",
                cwd=str(cwd),
            )
            self.db._execute_write(
                lambda conn: conn.execute(
                    "UPDATE sessions SET title = ?, started_at = ? WHERE id = ?",
                    (f"Hermes {native_id}", self.now - 10, source_id),
                )
            )
            if content is not None:
                self.db.append_message(
                    source_id,
                    "user",
                    content,
                    timestamp=self.now,
                )
        else:  # pragma: no cover - misuse guard for the shared harness
            raise ValueError("sidebar source must be Claude or Hermes")
        if git_root is not None:
            self.db._execute_write(
                lambda conn: conn.execute(
                    "UPDATE sessions SET git_repo_root = ? WHERE id = ?",
                    (str(git_root), source_id),
                )
            )
        return source_id

    def rewrite_thread_as_legacy_placeholder(self, thread_id: str) -> None:
        thread = self.native.threads[thread_id]
        legacy_start = "This is a Hermes Session Bridge placeholder registration."
        _prefix, separator, legacy_tail = thread["prompt"].partition(legacy_start)
        if separator != legacy_start:
            raise AssertionError("readable registration has no legacy block")
        legacy_prompt = separator + legacy_tail
        thread["prompt"] = legacy_prompt
        thread["turns"][0]["content"] = legacy_prompt

    def seed_legacy_placeholder(
        self,
        *,
        native_id: str,
        project_id: str | None,
    ) -> tuple[str, str]:
        source_cwd = self.db_path.parent / f"{native_id}-source"
        source_id = self.seed_source(
            Provider.CLAUDE,
            native_id,
            cwd=source_cwd,
        )
        assert self.register().queued == 1
        with self.client() as client:
            outcome = self.run_worker_once(client)
        thread_id = str(outcome[0]["codex_thread_id"])
        self.rewrite_thread_as_legacy_placeholder(thread_id)
        self.native.threads[thread_id]["project_id"] = project_id
        return source_id, thread_id

    def seed_hydration(self, source_id: str, thread_id: str) -> None:
        snapshot = self.store.get_sidebar_preview_source(source_id)
        candidate = self.store.get_sidebar_candidate_for_delivery(source_id)
        preview = build_session_preview(
            source_session_id=source_id,
            source_cursor=snapshot["source_cursor"],
            source_hash=snapshot["source_hash"],
            title=snapshot["title"],
            provider=candidate.provider.value,
            cwd=candidate.cwd,
            captured_at=snapshot["captured_at"],
            messages=snapshot["messages"],
            git_root=candidate.git_root,
            git_branch=candidate.git_branch,
            git_head=candidate.git_head,
            worktree_id=candidate.worktree_id,
            budget_chars=24_000,
        )
        hydration_marker = encode_hydration_marker(
            HydrationMarkerPayload(
                bridge_id=candidate.bridge_id,
                codex_thread_id=thread_id,
                preview_digest=preview.digest,
                preview_version=preview.version,
                source_cursor=preview.source_cursor,
                source_hash=preview.source_hash,
                source_session_id=source_id,
            ),
            _MARKER_SECRET,
        )
        seeded = self.store.seed_sidebar_hydration_job(
            source_session_id=source_id,
            bridge_id=candidate.bridge_id,
            codex_thread_id=thread_id,
            source_cursor=preview.source_cursor,
            source_hash=preview.source_hash,
            preview_version=preview.version,
            preview_digest=preview.digest,
            hydration_marker=hydration_marker,
            now=self.now,
        )
        assert seeded["state"] == SidebarHydrationState.PENDING.value
        self.config = replace(
            self.config,
            sidebar=replace(
                self.config.sidebar,
                legacy_hydration_enabled=True,
            ),
        )
        self.coordinator._config = self.config
        self._rebuild_app()

    def _index_native_thread(self, thread: dict[str, Any]) -> None:
        payload = thread["payload"]
        self.store.upsert_projection(
            SessionProjection(
                provider=Provider.CODEX,
                native_id=thread["thread_id"],
                title="Native sidebar placeholder",
                cwd=thread["cwd"],
                started_at=self.now,
                last_active=self.now,
                messages=(
                    ProjectedMessage(
                        native_event_id=f"registration-{thread['thread_id']}",
                        ordinal=0,
                        role="user",
                        content=thread["prompt"],
                        timestamp=self.now,
                    ),
                ),
                native_path=f"native://{thread['thread_id']}",
                native_cursor=f"cursor-{thread['thread_id']}",
                native_hash=f"hash-{thread['thread_id']}",
                origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
                origin_bridge_id=payload.bridge_id,
            )
        )

    def register(self, *, limit: int = 100):
        return asyncio.run(
            self.coordinator.register_sidebar_jobs_once(
                now=self.now,
                limit=limit,
            )
        )

    def scan_claude_history(self):
        return asyncio.run(self.coordinator.scan_all_history(Provider.CLAUDE))

    @contextmanager
    def client(self):
        with TestClient(
            self.app,
            base_url="http://127.0.0.1:7484",
            follow_redirects=False,
        ) as client:
            yield client

    def advance_retry(self) -> None:
        self.now += 120.0

    def advance_lease_expiry(self) -> None:
        self.now += 301.0

    def restart_bridge(self) -> None:
        if self.production_backend is not None:
            raise RuntimeError("production composition cannot use harness restart")
        self.db.close()
        self.db = SessionDB(self.db_path)
        self.store = SessionBridgeStore(
            self.db,
            clock=lambda: self.now,
            sidebar_jitter=lambda _bound: 0.0,
        )
        self.coordinator = SessionBridgeCoordinator(
            config=self.config,
            store=self.store,
            adapters=self.adapters,
            target_adapters={},
            sidebar_verifier=self.native,
            clock=lambda: self.now,
        )
        self._mark_coordinator_healthy()
        self.catalog = UnifiedCatalog(self.db, self.store)
        self._rebuild_app()

    def _registration_identity_failure(
        self,
        thread: dict[str, Any],
        *,
        expected_thread_id: str,
        expected_marker: str,
        expected_source_id: str,
        expected_source_cwd: str,
    ) -> str | None:
        if (
            thread.get("thread_id") != expected_thread_id
            or thread.get("marker") != expected_marker
        ):
            return "Authenticated marker conflict"
        prompt = thread.get("prompt")
        try:
            if _registration_marker(prompt) != expected_marker:
                return "Authenticated marker conflict"
        except (AttributeError, StopIteration):
            return "Authenticated marker conflict"
        try:
            identity = decode_sidebar_registration_identity(
                prompt,
                _MARKER_SECRET,
            )
        except ValueError:
            return "Source identity mismatch"
        if (
            identity.source_session_id != expected_source_id
            or _canonical_sidebar_path(identity.source_cwd)
            != expected_source_cwd
        ):
            return "Source identity mismatch"
        return None

    def run_worker_once(
        self,
        client: Any,
    ) -> list[dict[str, Any]]:
        trace: list[dict[str, Any]] = [
            {"tool": self.contract.status_tool, "arguments": {}}
        ]
        status = _sidebar_call_tool(client, self.contract.status_tool, {})
        if self.status_mutator is not None:
            status = self.status_mutator(deepcopy(status))
        inbox_cwd = _canonical_sidebar_path(self.inbox)
        if not self.contract.validate_status(status, inbox=inbox_cwd):
            self.contract.validate_trace(trace)
            self.worker_traces.append(trace)
            return []
        counts = status["sidebar"]["counts"]
        if not (counts["sidebar_pending"] or counts["sidebar_retry"]):
            self.contract.validate_trace(trace)
            self.worker_traces.append(trace)
            return []

        trace.append({"tool": self.contract.projects_tool, "arguments": {}})
        listed_projects = getattr(
            self.native,
            self.contract.projects_tool,
        )()
        try:
            projects = self.contract.build_project_map(
                listed_projects,
                inbox=inbox_cwd,
            )
        except (OSError, TypeError, ValueError):
            self.contract.validate_trace(trace)
            self.worker_traces.append(trace)
            return []

        trace.append({
            "tool": self.contract.pending_tool,
            "arguments": {"limit": self.contract.pending_limit},
        })
        jobs = _sidebar_call_tool(
            client,
            self.contract.pending_tool,
            {"limit": self.contract.pending_limit},
        )["jobs"]
        outcomes: list[dict[str, Any]] = []
        if not jobs:
            self.contract.validate_trace(trace)
            self.worker_traces.append(trace)
            return outcomes

        for ordinal, job in enumerate(jobs):
            job_id = job.get("source_session_id", f"job-{ordinal}")
            cwd = _canonical_sidebar_path(job["cwd"])
            git_root = (
                _canonical_sidebar_path(job["git_root"])
                if job.get("git_root") is not None
                else None
            )
            placement = self.contract.resolve_placement(
                projects,
                cwd=cwd,
                git_root=git_root,
                inbox=inbox_cwd,
            )
            trace.append({
                "tool": "project_choice",
                "job": job_id,
                "arguments": {
                    "inbox_cwd": placement.inbox_cwd,
                    "source_cwd": placement.source_cwd,
                    "runtime_workspace_roots": placement.runtime_workspace_roots,
                    "project_id": placement.project_id,
                },
            })

            def fail_once(
                label: str,
                known_thread_id: str | None = None,
            ) -> dict[str, Any]:
                code = self.contract.failure_code(label)
                arguments = {
                    "lease_token": job["lease_token"],
                    "error_code": code,
                }
                if known_thread_id is not None:
                    arguments["codex_thread_id"] = known_thread_id
                trace.append({
                    "tool": self.contract.fail_tool,
                    "job": job_id,
                    "arguments": arguments,
                })
                settled = _sidebar_try_fail(
                    client,
                    self.contract.fail_tool,
                    arguments,
                )
                return (
                    settled
                    if settled is not None
                    else {"state": "commit_unknown", "fail_attempted": True}
                )

            thread_id = None
            created = False
            recovered_thread_id = job["recovered_thread_id"]
            reconciliation_state = job["reconciliation_state"]
            marker = _registration_marker(job["registration_prompt"])
            expected_registration = decode_sidebar_registration_identity(
                job["registration_prompt"],
                _MARKER_SECRET,
            )
            if reconciliation_state == "recovered":
                if recovered_thread_id is None or job.get("create_eligible") is not False:
                    outcomes.append(fail_once("Bridge temporarily unavailable"))
                    continue
                read_arguments = {"threadId": recovered_thread_id}
                trace.append({
                    "tool": self.contract.read_thread_tool,
                    "job": job_id,
                    "arguments": read_arguments,
                })
                try:
                    recovered = getattr(
                        self.native,
                        self.contract.read_thread_tool,
                    )(thread_id=recovered_thread_id)
                except (KeyError, RuntimeError):
                    outcomes.append(
                        fail_once(
                            "Bound task not yet indexed",
                            recovered_thread_id,
                        )
                    )
                    continue
                identity_failure = self._registration_identity_failure(
                    recovered,
                    expected_thread_id=recovered_thread_id,
                    expected_marker=marker,
                    expected_source_id=expected_registration.source_session_id,
                    expected_source_cwd=cwd,
                )
                if identity_failure == "Authenticated marker conflict":
                    outcomes.append(
                        fail_once(
                            identity_failure,
                            recovered_thread_id,
                        )
                    )
                    continue
                if (
                    recovered.get("project_id") != placement.project_id
                    or _canonical_sidebar_path(recovered.get("cwd", ""))
                    != placement.inbox_cwd
                ):
                    outcomes.append(
                        fail_once(
                            "Native task outside Session Inbox placement",
                            recovered_thread_id,
                        )
                    )
                    continue
                if identity_failure is not None:
                    outcomes.append(
                        fail_once(identity_failure, recovered_thread_id)
                    )
                    continue
                thread_id = recovered_thread_id
            elif (
                reconciliation_state != "absence_proven"
                or recovered_thread_id is not None
                or job.get("create_eligible") is not True
            ):
                outcomes.append(fail_once("Bridge temporarily unavailable"))
                continue

            if thread_id is None:
                reserve_arguments = {
                    "lease_token": job["lease_token"],
                    "reconciliation_proof_digest": job[
                        "reconciliation_proof_digest"
                    ],
                    "reconciliation_generation": job[
                        "reconciliation_generation"
                    ],
                }
                trace.append({
                    "tool": self.contract.reserve_tool,
                    "job": job_id,
                    "arguments": reserve_arguments,
                })
                try:
                    reservation = _sidebar_call_tool(
                        client,
                        self.contract.reserve_tool,
                        reserve_arguments,
                    )
                except (httpx.TransportError, AssertionError, ValueError):
                    outcomes.append(fail_once("Bridge temporarily unavailable"))
                    continue
                if reservation.get("state") == "recovered" and (
                    reservation.get("create_reserved") is False
                ):
                    recovered_after_reserve = reservation.get("codex_thread_id")
                    if not isinstance(recovered_after_reserve, str):
                        outcomes.append(fail_once("Bridge temporarily unavailable"))
                        continue
                    trace.append({
                        "tool": self.contract.read_thread_tool,
                        "job": job_id,
                        "arguments": {"threadId": recovered_after_reserve},
                    })
                    try:
                        recovered = getattr(
                            self.native,
                            self.contract.read_thread_tool,
                        )(thread_id=recovered_after_reserve)
                    except (KeyError, RuntimeError):
                        outcomes.append(
                            fail_once(
                                "Bound task not yet indexed",
                                recovered_after_reserve,
                            )
                        )
                        continue
                    identity_failure = self._registration_identity_failure(
                        recovered,
                        expected_thread_id=recovered_after_reserve,
                        expected_marker=marker,
                        expected_source_id=expected_registration.source_session_id,
                        expected_source_cwd=cwd,
                    )
                    if identity_failure is not None:
                        outcomes.append(
                            fail_once(identity_failure, recovered_after_reserve)
                        )
                        continue
                    thread_id = recovered_after_reserve
                elif reservation != {
                    "state": "sidebar_leased",
                    "create_reserved": True,
                }:
                    outcomes.append(fail_once("Bridge temporarily unavailable"))
                    continue
                else:
                    create_arguments = self.contract.create_arguments(
                        prompt=job["registration_prompt"],
                        placement=placement,
                    )
                    trace.append({
                        "tool": self.contract.create_tool,
                        "job": job_id,
                        "arguments": create_arguments,
                        "registration_prompt": job["registration_prompt"],
                    })
                    try:
                        thread_id = getattr(
                            self.native,
                            self.contract.create_tool,
                        )(**create_arguments)
                        created = True
                    except RuntimeError:
                        if self.production_codex_target is not None and (
                            self.allow_forbidden_app_server_fallback_for_mutation
                            or not self.contract.forbid_app_server
                        ):
                            marker_payload = decode_bridge_marker(
                                _registration_marker(job["registration_prompt"]),
                                _MARKER_SECRET,
                            )
                            trace.append({
                                "tool": "app-server.create_placeholder",
                                "job": job_id,
                                "arguments": {"source_session_id": job_id},
                            })
                            self.production_codex_target.create_placeholder(
                                title=job["title"],
                                source_session_id=marker_payload.source_session_id,
                                bridge_id=marker_payload.bridge_id,
                                policy_generation=marker_payload.policy_generation,
                                cwd=job["cwd"],
                            )
                        outcomes.append(
                            fail_once("Create response lost or otherwise ambiguous")
                        )
                        continue

            bind_arguments = {
                "lease_token": job["lease_token"],
                "codex_thread_id": thread_id,
            }
            trace.append({
                "tool": self.contract.bind_tool,
                "job": job_id,
                "arguments": bind_arguments,
            })
            try:
                _sidebar_call_tool(
                    client,
                    self.contract.bind_tool,
                    bind_arguments,
                )
            except (httpx.TransportError, AssertionError, ValueError):
                outcomes.append(fail_once("Bridge temporarily unavailable", thread_id))
                continue

            if created:
                trace.append({
                    "tool": self.contract.read_thread_tool,
                    "job": job_id,
                    "arguments": {"threadId": thread_id},
                })
                try:
                    indexed = getattr(
                        self.native,
                        self.contract.read_thread_tool,
                    )(thread_id=thread_id)
                except (KeyError, RuntimeError):
                    outcomes.append(fail_once("Bound task not yet indexed", thread_id))
                    continue
                if (
                    indexed.get("thread_id") != thread_id
                    or indexed.get("marker") != marker
                ):
                    outcomes.append(fail_once("Bound task not yet indexed", thread_id))
                    continue
                if (
                    indexed.get("project_id") != placement.project_id
                    or _canonical_sidebar_path(indexed.get("cwd", ""))
                    != placement.inbox_cwd
                ):
                    outcomes.append(
                        fail_once(
                            "Native task outside Session Inbox placement",
                            thread_id,
                        )
                    )
                    continue

            rename_arguments = {"threadId": thread_id, "title": job["title"]}
            trace.append({
                "tool": self.contract.rename_tool,
                "job": job_id,
                "arguments": rename_arguments,
            })
            try:
                getattr(self.native, self.contract.rename_tool)(
                    thread_id,
                    job["title"],
                )
            except RuntimeError:
                outcomes.append(fail_once("Rename failed", thread_id))
                continue

            commit_arguments = {
                "lease_token": job["lease_token"],
                "codex_thread_id": thread_id,
            }
            trace.append({
                "tool": self.contract.commit_tool,
                "job": job_id,
                "arguments": commit_arguments,
            })
            try:
                outcomes.append(
                    _sidebar_call_tool(
                        client,
                        self.contract.commit_tool,
                        commit_arguments,
                    )
                )
            except httpx.TransportError:
                outcomes.append(fail_once("Bridge temporarily unavailable", thread_id))
        self.contract.validate_trace(trace)
        self.worker_traces.append(trace)
        return outcomes

    def run_hydration_worker_once(
        self,
        client: Any,
        *,
        drop_after_append: bool = False,
    ) -> dict[str, Any] | None:
        trace: list[dict[str, Any]] = [
            {"tool": self.contract.status_tool, "arguments": {}}
        ]
        status = _sidebar_call_tool(client, self.contract.status_tool, {})
        hydration_counts = status["sidebar"]["hydration"]["counts"]
        if not (
            hydration_counts["pending"]
            or hydration_counts["retry"]
            or hydration_counts["ambiguous"]
        ):
            self.worker_traces.append(trace)
            return None
        trace.append({"tool": self.contract.projects_tool, "arguments": {}})
        self.native.list_projects()
        pending_tool = "session_sidebar_hydration_pending"
        trace.append({"tool": pending_tool, "arguments": {"limit": 1}})
        jobs = _sidebar_call_tool(client, pending_tool, {"limit": 1})["jobs"]
        if not jobs:
            self.worker_traces.append(trace)
            return None
        job = jobs[0]
        thread_id = job["codex_thread_id"]
        trace.append({"tool": "read_thread", "arguments": {"threadId": thread_id}})
        thread = self.native.read_thread(thread_id=thread_id)
        hydration_identity = decode_hydration_marker(
            job["hydration_marker"],
            _MARKER_SECRET,
        )
        if (
            thread["thread_id"] != thread_id
            or hydration_identity.codex_thread_id != thread_id
            or thread["payload"].source_session_id
            != hydration_identity.source_session_id
        ):
            raise AssertionError("hydration exact-task identity mismatch")
        marker_present = any(
            job["hydration_marker"] in turn["content"]
            and turn["status"] == "completed"
            for turn in thread["turns"]
        )
        if marker_present:
            arguments = {
                "lease_token": job["lease_token"],
                "codex_thread_id": thread_id,
                "hydration_marker": job["hydration_marker"],
            }
            trace.append({
                "tool": "session_sidebar_hydration_commit",
                "arguments": arguments,
            })
            result = _sidebar_call_tool(
                client,
                "session_sidebar_hydration_commit",
                arguments,
            )
            self.worker_traces.append(trace)
            return result
        if job["send_reserved"]:
            arguments = {
                "lease_token": job["lease_token"],
                "error_code": "hydration_send_ambiguous",
                "codex_thread_id": thread_id,
            }
            trace.append({
                "tool": "session_sidebar_hydration_fail",
                "arguments": arguments,
            })
            result = _sidebar_call_tool(
                client,
                "session_sidebar_hydration_fail",
                arguments,
            )
            self.worker_traces.append(trace)
            return result
        reserve_arguments = {"lease_token": job["lease_token"]}
        trace.append({
            "tool": "session_sidebar_hydration_reserve",
            "arguments": reserve_arguments,
        })
        _sidebar_call_tool(
            client,
            "session_sidebar_hydration_reserve",
            reserve_arguments,
        )
        trace.append({
            "tool": "send_message_to_thread",
            "arguments": {"threadId": thread_id},
        })
        try:
            self.native.send_message_to_thread(
                thread_id=thread_id,
                message=job["hydration_message"],
                drop_after_append=drop_after_append,
            )
        except RuntimeError:
            arguments = {
                "lease_token": job["lease_token"],
                "error_code": "hydration_send_ambiguous",
                "codex_thread_id": thread_id,
            }
            trace.append({
                "tool": "session_sidebar_hydration_fail",
                "arguments": arguments,
            })
            result = _sidebar_call_tool(
                client,
                "session_sidebar_hydration_fail",
                arguments,
            )
            self.worker_traces.append(trace)
            return result
        trace.append({"tool": "read_thread", "arguments": {"threadId": thread_id}})
        verified = self.native.read_thread(thread_id=thread_id)
        if not any(
            job["hydration_marker"] in turn["content"]
            and turn["status"] == "completed"
            for turn in verified["turns"]
        ):
            raise AssertionError("hydration marker was not indexed")
        arguments = {
            "lease_token": job["lease_token"],
            "codex_thread_id": thread_id,
            "hydration_marker": job["hydration_marker"],
        }
        trace.append({
            "tool": "session_sidebar_hydration_commit",
            "arguments": arguments,
        })
        result = _sidebar_call_tool(
            client,
            "session_sidebar_hydration_commit",
            arguments,
        )
        self.worker_traces.append(trace)
        return result


def _canonical_sidebar_path(value: str | Path) -> str:
    return os.path.normcase(os.path.realpath(os.fspath(value)))


def _registration_marker(prompt: str) -> str:
    return next(
        line.removeprefix("Signed marker: ")
        for line in prompt.splitlines()
        if line.startswith("Signed marker: ")
    )


def _verified_native_thread(thread: dict[str, Any]) -> VerifiedSidebarThread:
    payload = thread["payload"]
    return VerifiedSidebarThread(
        thread_id=thread["thread_id"],
        source_session_id=payload.source_session_id,
        bridge_id=payload.bridge_id,
        projection=SessionProjection(
            provider=Provider.CODEX,
            native_id=thread["thread_id"],
            title=thread["title"] or "Native sidebar placeholder",
            cwd=thread["cwd"],
            started_at=0.0,
            last_active=0.0,
            messages=(),
            native_path=f"native://{thread['thread_id']}",
            native_cursor=f"cursor-{thread['thread_id']}",
            native_hash=f"hash-{thread['thread_id']}",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=payload.bridge_id,
        ),
    )


def _sidebar_rpc(
    client: TestClient,
    method: str,
    params: dict[str, Any] | None = None,
    *,
    request_id: int = 1,
):
    if method != "initialize" and "Mcp-Session-Id" not in client.headers:
        _sidebar_rpc(
            client,
            "initialize",
            {
                "protocolVersion": LATEST_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "sidebar-e2e", "version": "1"},
            },
            request_id=0,
        )
        initialized = client.post(
            "/mcp",
            headers={
                "Authorization": f"Bearer {_SIDEBAR_TOKEN}",
                "Accept": "application/json, text/event-stream",
                "Content-Type": "application/json",
                "Mcp-Session-Id": client.headers["Mcp-Session-Id"],
            },
            json={
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
                "params": {},
            },
        )
        assert initialized.status_code == 202, initialized.text
    response = client.post(
        "/mcp",
        headers={
            "Authorization": f"Bearer {_SIDEBAR_TOKEN}",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
    )
    assert response.status_code == 200, response.text
    session_id = response.headers.get("Mcp-Session-Id")
    if session_id:
        client.headers["Mcp-Session-Id"] = session_id
    if "text/event-stream" in response.headers.get("content-type", ""):
        lines = [
            line.removeprefix("data: ")
            for line in response.text.splitlines()
            if line.startswith("data: ")
        ]
        assert lines, response.text
        return json.loads(lines[-1])
    return response.json()


def _sidebar_call_tool(
    client: TestClient,
    name: str,
    arguments: dict[str, Any],
):
    payload = _sidebar_rpc(
        client,
        "tools/call",
        {"name": name, "arguments": arguments},
        request_id=9,
    )
    assert "error" not in payload, payload
    result = payload["result"]
    if result.get("isError"):
        pytest.fail(result["content"][0]["text"])
    structured = result.get("structuredContent")
    return (
        structured
        if structured is not None
        else json.loads(result["content"][0]["text"])
    )


def _sidebar_try_fail(
    client: Any,
    name: str,
    arguments: dict[str, Any],
) -> dict[str, Any] | None:
    payload = _sidebar_rpc(
        client,
        "tools/call",
        {"name": name, "arguments": arguments},
        request_id=10,
    )
    assert "error" not in payload, payload
    result = payload["result"]
    if result.get("isError"):
        return None
    structured = result.get("structuredContent")
    return (
        structured
        if structured is not None
        else json.loads(result["content"][0]["text"])
    )


def test_sidebar_skill_trace_requires_exact_id_on_fail_after_recovered_read() -> None:
    contract = _SidebarSkillContract.load(_SIDEBAR_SKILL_PATH)
    job_id = "claude:known-recovered-id"
    trace = [
        {"tool": contract.status_tool},
        {"tool": contract.projects_tool},
        {"tool": contract.pending_tool},
        {"tool": "project_choice", "job": job_id},
        {
            "tool": contract.read_thread_tool,
            "job": job_id,
            "arguments": {"threadId": "native-known-recovered-id"},
        },
        {
            "tool": contract.fail_tool,
            "job": job_id,
            "arguments": {"error_code": "marker_conflict"},
        },
    ]

    with pytest.raises(
        AssertionError,
        match="known native ID",
    ):
        contract.validate_trace(trace)


def test_sidebar_skill_contract_preserves_provider_isolation() -> None:
    contract = _SidebarSkillContract.load(_SIDEBAR_SKILL_PATH)

    assert contract.provider_degradation_isolated is True


@pytest.mark.parametrize("provider", [Provider.CLAUDE, Provider.HERMES])
def test_sidebar_meaningful_source_reaches_visible_catalog_through_public_mcp(
    tmp_path: Path,
    provider: Provider,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / f"{provider.value}-source"
        harness.add_project(f"{provider.value}-project", source_cwd)
        source_id = harness.seed_source(
            provider,
            f"meaningful-{provider.value}",
            cwd=source_cwd,
        )
        summary = harness.register()

        with harness.client() as client:
            outcomes = harness.run_worker_once(client)

        assert summary.by_provider[provider.value] == 1
        assert outcomes == [
            {"state": "sidebar_visible", "codex_thread_id": "native-sidebar-1"}
        ]
        catalog_row = harness.store.get_bridge_summaries([source_id])[source_id]
        assert catalog_row["bridge_sidebar_state"] == "visible"
        assert catalog_row["bridge_sidebar_codex_thread_id"] == "native-sidebar-1"
        assert harness.native.threads["native-sidebar-1"]["title"].startswith(
            "[Claude] " if provider is Provider.CLAUDE else "[Hermes] "
        )
        assert [event["tool"] for event in harness.worker_traces[-1]] == [
            harness.contract.status_tool,
            harness.contract.projects_tool,
            harness.contract.pending_tool,
            "project_choice",
            harness.contract.reserve_tool,
            harness.contract.create_tool,
            harness.contract.bind_tool,
            harness.contract.read_thread_tool,
            harness.contract.rename_tool,
            harness.contract.commit_tool,
        ]
    finally:
        harness.close()


def test_claude_source_becomes_one_readable_hermes_project_task(
    tmp_path: Path,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "customer-project"
        messages = tuple(
            ProjectedMessage(
                native_event_id=f"visibility-{index}",
                ordinal=index,
                role="user" if index % 2 else "assistant",
                content=f"message-{index}",
                timestamp=harness.now + index,
            )
            for index in range(1, 7)
        )
        harness.config = replace(
            harness.config,
            sidebar=replace(
                harness.config.sidebar,
                readable_preview_enabled=True,
            ),
        )
        harness.coordinator._config = harness.config
        harness._rebuild_app()
        source_id = harness.seed_source(
            Provider.CLAUDE,
            "visibility-e2e",
            cwd=source_cwd,
            messages=messages,
        )
        assert harness.register().queued == 1
        with harness.client() as client:
            outcome = harness.run_worker_once(client)

        assert outcome == [
            {"state": "sidebar_visible", "codex_thread_id": "native-sidebar-1"}
        ], harness.worker_traces[-1]
        assert len(harness.native.create_calls) == 1
        created = harness.native.create_calls[0]
        assert created["project_id"] == "session-inbox"
        assert "cwd" not in created["desktop_request"]
        assert "runtimeWorkspaceRoots" not in created["desktop_request"]
        assert "idempotencyKey" not in created["desktop_request"]
        prompt = created["prompt"]
        assert prompt.startswith("# Imported Claude Code Session")
        last_five = prompt.split("## Last 5 Messages", 1)[1].split(
            "## Bridge Registration",
            1,
        )[0]
        assert "message-1" not in last_five
        for index in range(2, 7):
            assert f"message-{index}" in last_five
        job = harness.store.get_sidebar_job_for_source(source_id)
        assert job["codex_thread_id"] == "native-sidebar-1"
        links = harness.store.get_bridge_summaries([source_id])[source_id][
            "bridge_links"
        ]
        assert len(links) == 1
    finally:
        harness.close()


def test_sidebar_new_import_delivers_bounded_readable_registration(
    tmp_path: Path,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "readable-registration"
        source_cwd.mkdir()
        subprocess.run(
            ["git", "init", "-q", str(source_cwd)],
            check=True,
            capture_output=True,
        )
        harness.add_project("readable-registration-project", source_cwd)
        harness.config = replace(
            harness.config,
            sidebar=replace(
                harness.config.sidebar,
                readable_preview_enabled=True,
            ),
        )
        harness.coordinator._config = harness.config
        harness._rebuild_app()
        conversational = [
            ("user", "conversation-message-1 sk-" + ("q" * 24)),
            ("assistant", "conversation-message-2"),
            ("user", "conversation-message-3"),
            ("assistant", "conversation-message-4"),
            ("user", "conversation-message-5"),
            ("assistant", "conversation-message-6"),
            ("user", "conversation-message-7"),
        ]
        messages = [
            ProjectedMessage(
                native_event_id=f"conversation-{index}",
                ordinal=index,
                role=role,
                content=content,
                timestamp=harness.now - 10 + index,
            )
            for index, (role, content) in enumerate(conversational)
        ]
        messages.extend((
            ProjectedMessage(
                native_event_id="tool-output",
                ordinal=7,
                role="tool",
                content="tool-output-must-not-render",
                timestamp=harness.now - 3,
            ),
            ProjectedMessage(
                native_event_id="system-output",
                ordinal=8,
                role="system",
                content="system-output-must-not-render",
                timestamp=harness.now - 2,
            ),
        ))
        source_id = harness.store.upsert_projection(
            SessionProjection(
                provider=Provider.CLAUDE,
                native_id="readable-registration",
                title="Readable registration",
                cwd=str(source_cwd),
                started_at=harness.now - 10,
                last_active=harness.now,
                messages=tuple(messages),
                native_path=str(source_cwd / "readable-registration.jsonl"),
                native_cursor="cursor-readable-registration",
                native_hash="hash-readable-registration",
                git_branch="main",
            )
        ).session_id

        summary = harness.register()
        with harness.client() as client:
            outcomes = harness.run_worker_once(client)

        assert summary.queued == 1
        assert outcomes == [
            {"state": "sidebar_visible", "codex_thread_id": "native-sidebar-1"}
        ]
        thread = harness.native.threads["native-sidebar-1"]
        prompt = thread["prompt"]
        assert prompt.startswith("# Imported Claude Code Session")
        last_five = prompt.split("## Last 5 Messages", 1)[1].split(
            "## Bridge Registration",
            1,
        )[0]
        for index in range(3, 8):
            assert f"conversation-message-{index}" in last_five
        assert "conversation-message-1" not in last_five
        assert "conversation-message-2" not in last_five
        assert "tool-output-must-not-render" not in prompt
        assert "system-output-must-not-render" not in prompt
        assert "sk-" + ("q" * 24) not in prompt
        assert prompt.count("Signed marker: HERMES_SESSION_BRIDGE_V1:") == 1
        assert thread["assistant_reply"] == "REGISTERED"
        assert thread["session_continue_calls"] == []
        assert harness.store.get_sidebar_job_for_source(source_id)["state"] == (
            SidebarJobState.VISIBLE.value
        )
    finally:
        harness.close()


def test_legacy_hydration_targets_same_projectless_task_once(
    tmp_path: Path,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_id, thread_id = harness.seed_legacy_placeholder(
            native_id="legacy-projectless",
            project_id=None,
        )
        harness.seed_hydration(source_id, thread_id)
        create_count = len(harness.native.create_calls)
        rename_count = len(harness.native.rename_calls)
        with harness.client() as client:
            first = harness.run_hydration_worker_once(client)
            second = harness.run_hydration_worker_once(client)

        assert first == {
            "state": SidebarHydrationState.VISIBLE.value,
            "codex_thread_id": thread_id,
        }
        assert second is None
        assert [target for target, _message in harness.native.send_calls] == [
            thread_id
        ]
        assert len(harness.native.create_calls) == create_count
        assert len(harness.native.rename_calls) == rename_count
        assert harness.native.threads[thread_id]["project_id"] is None
        assert harness.native.threads[thread_id]["session_continue_calls"] == []
    finally:
        harness.close()


def test_reported_legacy_task_hydrates_once_and_reconciles_ambiguous_send(
    tmp_path: Path,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_native_id = "2a786924-8093-4a9f-a371-6e27ca66be32"
        source_id = f"claude:{source_native_id}"
        thread_id = "019f8927-8012-77d0-beb0-4cd5f8cc21f9"
        source_cwd = tmp_path / "reported-legacy-source"
        source_cwd.mkdir()
        harness.add_project("reported-legacy-project", source_cwd)
        messages = tuple(
            ProjectedMessage(
                native_event_id=f"reported-message-{index}",
                ordinal=index,
                role="user" if index % 2 == 0 else "assistant",
                content=f"reported-conversation-message-{index}",
                timestamp=harness.now - 578 + index,
            )
            for index in range(578)
        )
        persisted_source = harness.store.upsert_projection(
            SessionProjection(
                provider=Provider.CLAUDE,
                native_id=source_native_id,
                title="Reported legacy Claude task",
                cwd=str(source_cwd),
                started_at=harness.now - 600,
                last_active=harness.now,
                messages=messages,
                native_path=str(source_cwd / f"{source_native_id}.jsonl"),
                native_cursor="cursor-reported-578",
                native_hash="hash-reported-578",
            )
        ).session_id
        assert persisted_source == source_id
        harness.native.next_thread_id = thread_id
        assert harness.register().queued == 1
        with harness.client() as client:
            assert harness.run_worker_once(client) == [
                {"state": "sidebar_visible", "codex_thread_id": thread_id}
            ]
        harness.rewrite_thread_as_legacy_placeholder(thread_id)

        snapshot = harness.store.get_sidebar_preview_source(source_id)
        assert len(snapshot["messages"]) == 578
        candidate = harness.store.get_sidebar_candidate_for_delivery(source_id)
        preview = build_session_preview(
            source_session_id=source_id,
            source_cursor=snapshot["source_cursor"],
            source_hash=snapshot["source_hash"],
            title=snapshot["title"],
            provider=candidate.provider.value,
            cwd=candidate.cwd,
            captured_at=snapshot["captured_at"],
            messages=snapshot["messages"],
            git_root=candidate.git_root,
            git_branch=candidate.git_branch,
            git_head=candidate.git_head,
            worktree_id=candidate.worktree_id,
            budget_chars=24_000,
        )
        hydration_marker = encode_hydration_marker(
            HydrationMarkerPayload(
                bridge_id=candidate.bridge_id,
                codex_thread_id=thread_id,
                preview_digest=preview.digest,
                preview_version=preview.version,
                source_cursor=preview.source_cursor,
                source_hash=preview.source_hash,
                source_session_id=source_id,
            ),
            _MARKER_SECRET,
        )
        seeded = harness.store.seed_sidebar_hydration_job(
            source_session_id=source_id,
            bridge_id=candidate.bridge_id,
            codex_thread_id=thread_id,
            source_cursor=preview.source_cursor,
            source_hash=preview.source_hash,
            preview_version=preview.version,
            preview_digest=preview.digest,
            hydration_marker=hydration_marker,
            now=harness.now,
        )
        assert seeded["state"] == SidebarHydrationState.PENDING.value
        harness.config = replace(
            harness.config,
            sidebar=replace(
                harness.config.sidebar,
                legacy_hydration_enabled=True,
            ),
        )
        harness.coordinator._config = harness.config
        harness._rebuild_app()
        create_count = len(harness.native.create_calls)
        rename_count = len(harness.native.rename_calls)

        with harness.client() as client:
            first = harness.run_hydration_worker_once(
                client,
                drop_after_append=True,
            )
            harness.advance_retry()
            second = harness.run_hydration_worker_once(client)

        assert first == {
            "state": SidebarHydrationState.RETRY.value,
            "error_code": "hydration_send_ambiguous",
            "send_reserved": True,
        }
        assert second == {
            "state": SidebarHydrationState.VISIBLE.value,
            "codex_thread_id": thread_id,
        }, harness.worker_traces[-1]
        assert len(harness.native.send_calls) == 1
        sent_thread_id, sent_message = harness.native.send_calls[0]
        assert sent_thread_id == thread_id
        assert sent_message.startswith("# Imported Claude Code Session")
        last_five = sent_message.split("## Last 5 Messages", 1)[1].split(
            "## In-place Session Bridge Hydration",
            1,
        )[0]
        for index in range(573, 578):
            assert f"reported-conversation-message-{index}" in last_five
        assert "reported-conversation-message-572" not in last_five
        assert sent_message.count(hydration_marker) == 1
        thread = harness.native.threads[thread_id]
        assert thread["session_continue_calls"] == []
        assert len(harness.native.create_calls) == create_count
        assert len(harness.native.rename_calls) == rename_count
        assert harness.store.sidebar_hydration_status(harness.now)["counts"][
            SidebarHydrationState.VISIBLE.value
        ] == 1
        hydration_traces = harness.worker_traces[-2:]
        assert all(
            "create_thread" not in {event["tool"] for event in trace}
            and "set_thread_title" not in {event["tool"] for event in trace}
            and "set_thread_archived" not in {event["tool"] for event in trace}
            for trace in hydration_traces
        )
    finally:
        harness.close()


def test_sidebar_backlog_recovery_preserves_exact_tasks_and_drains_both_ledgers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    from session_bridge.cli import ProductionBackend

    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        legacy_sources: list[str] = []
        for label in ("legacy-a", "legacy-b"):
            cwd = tmp_path / label
            harness.add_project(f"{label}-project", cwd)
            source_id = harness.seed_source(
                Provider.CLAUDE,
                label,
                cwd=cwd,
                content=f"{label}-message-0",
            )
            for index in range(1, 7):
                harness.db.append_message(
                    source_id,
                    "user" if index % 2 == 0 else "assistant",
                    f"{label}-message-{index}",
                    timestamp=harness.now - 7 + index,
                )
            legacy_sources.append(source_id)

        assert harness.register().queued == 2
        with harness.client() as client:
            assert all(
                harness.run_worker_once(client)[0]["state"]
                == SidebarJobState.VISIBLE.value
                for _ in legacy_sources
            )
        for source_id in legacy_sources:
            thread_id = str(
                harness.store.get_sidebar_job_for_source(source_id)[
                    "codex_thread_id"
                ]
            )
            harness.rewrite_thread_as_legacy_placeholder(thread_id)

        harness.config = replace(
            harness.config,
            sidebar=replace(
                harness.config.sidebar,
                readable_preview_enabled=True,
                legacy_hydration_enabled=True,
            ),
        )
        harness.coordinator._config = harness.config
        harness._rebuild_app()
        readable_cwd = tmp_path / "already-readable"
        harness.add_project("already-readable-project", readable_cwd)
        readable_source = harness.seed_source(
            Provider.CLAUDE,
            "already-readable",
            cwd=readable_cwd,
            content="already-readable-message",
        )
        assert harness.register().queued == 1
        with harness.client() as client:
            assert harness.run_worker_once(client)[0]["state"] == (
                SidebarJobState.VISIBLE.value
            )

        original_thread_ids = {
            source_id: str(
                harness.store.get_sidebar_job_for_source(source_id)[
                    "codex_thread_id"
                ]
            )
            for source_id in (*legacy_sources, readable_source)
        }

        class ExactNativeRecovery:
            def __init__(self) -> None:
                self.ambiguous_thread_id = original_thread_ids[legacy_sources[1]]
                self.dropped = False

            def read_thread_initial_prompt(
                self,
                *,
                thread_id: str,
                deadline: float,
            ) -> str:
                assert deadline > 0
                return str(harness.native.read_thread(thread_id=thread_id)["prompt"])

            def thread_has_exact_marker(
                self,
                *,
                thread_id: str,
                marker: str,
                deadline: float,
            ) -> bool:
                assert deadline > 0
                return any(
                    marker in turn["content"] and turn["status"] == "completed"
                    for turn in harness.native.read_thread(thread_id=thread_id)[
                        "turns"
                    ]
                )

            def start_text_turn_and_verify_marker(
                self,
                *,
                thread_id: str,
                message: str,
                marker: str,
                deadline: float,
            ) -> None:
                assert deadline > 0
                drop = thread_id == self.ambiguous_thread_id and not self.dropped
                if drop:
                    self.dropped = True
                try:
                    harness.native.send_message_to_thread(
                        thread_id=thread_id,
                        message=message,
                        drop_after_append=drop,
                    )
                except RuntimeError as exc:
                    raise NativeTurnAmbiguous(
                        "synthetic post-dispatch ambiguity"
                    ) from exc
                assert self.thread_has_exact_marker(
                    thread_id=thread_id,
                    marker=marker,
                    deadline=deadline,
                )

        native_recovery = ExactNativeRecovery()
        backend = ProductionBackend(harness.config)
        backend._store = harness.store
        backend._catalog = harness.catalog
        monkeypatch.setattr(
            "session_bridge.cli.resolve_marker_key",
            lambda: _MARKER_SECRET,
        )
        monkeypatch.setattr(
            "session_bridge.cli.time.time",
            lambda: harness.now,
        )
        monkeypatch.setattr(
            backend,
            "_require_sidebar_terminal_delivery",
            lambda: native_recovery,
        )

        dry_run = backend.sidebar_hydration_seed_backfill(
            days=30,
            limit=10,
            apply=False,
            confirmation=None,
        )
        hydration_inventory = harness.store.list_sidebar_hydration_candidates(
            now=harness.now,
            backfill_days=30,
            limit=10,
        )
        expected_candidates = [
            {
                "source_session_id": str(row["source_session_id"]),
                "codex_thread_id": str(row["codex_thread_id"]),
                "visible_at": float(row["visible_at"]),
                "hydration_state": "not_seeded",
            }
            for row in hydration_inventory
            if row["source_session_id"] in legacy_sources
        ]
        assert dry_run == {
            "mode": "dry_run",
            "scope": "days",
            "days": 30,
            "limit": 10,
            "examined": 3,
            "eligible": 2,
            "already_readable": 1,
            "seeded": 0,
            "blocked": 0,
            "blocked_codes": {},
            "candidates": expected_candidates,
        }
        applied = backend.sidebar_hydration_seed_backfill(
            days=30,
            limit=10,
            apply=True,
            confirmation="HYDRATE_ALL_EXACT_EXISTING_TASKS",
        )
        assert applied == {
            **dry_run,
            "mode": "apply",
            "seeded": 2,
            "candidates": [
                {
                    **candidate,
                    "hydration_state": SidebarHydrationState.PENDING.value,
                }
                for candidate in expected_candidates
            ],
        }

        hydration_executor = SidebarHydrationExecutor(
            claim_once=lambda: asyncio.run(
                harness.coordinator.claim_sidebar_hydration_for_delivery(limit=1)
            ),
            store=harness.store,
            native=native_recovery,
            marker_secret=_MARKER_SECRET,
            clock=lambda: harness.now,
            monotonic=lambda: 1_000.0,
        )
        create_count_before_hydration = len(harness.native.create_calls)
        hydration_results = [
            hydration_executor.run_once(),
            hydration_executor.run_once(),
        ]
        assert [result.status for result in hydration_results] == [
            "visible",
            "retry",
        ]
        harness.advance_retry()
        reconciled = hydration_executor.run_once()
        assert reconciled.status == "visible"
        assert len(harness.native.create_calls) == create_count_before_hydration

        sent_by_thread = {
            thread_id: [
                message
                for sent_id, message in harness.native.send_calls
                if sent_id == thread_id
            ]
            for thread_id in original_thread_ids.values()
        }
        for source_id in legacy_sources:
            thread_id = original_thread_ids[source_id]
            assert len(sent_by_thread[thread_id]) == 1
            last_five = sent_by_thread[thread_id][0].split(
                "## Last 5 Messages",
                1,
            )[1].split("## In-place Session Bridge Hydration", 1)[0]
            label = source_id.removeprefix("claude:")
            for index in range(2, 7):
                assert f"{label}-message-{index}" in last_five
            assert f"{label}-message-1" not in last_five
            assert (
                harness.store.get_sidebar_job_for_source(source_id)[
                    "codex_thread_id"
                ]
                == thread_id
            )
        assert sent_by_thread[original_thread_ids[readable_source]] == []

        pending_sources: list[str] = []
        final_now = harness.now
        for label, age in (
            ("oldest", 100.0),
            ("fresh-3", 3.0),
            ("fresh-2", 2.0),
            ("fresh-1", 1.0),
        ):
            harness.now = final_now - age
            cwd = tmp_path / label
            harness.add_project(f"{label}-project", cwd)
            pending_sources.append(
                harness.seed_source(
                    Provider.CLAUDE,
                    label,
                    cwd=cwd,
                    content=f"{label}-message",
                )
            )
        harness.now = final_now
        pending_backfill = asyncio.run(
            harness.coordinator.backfill_sidebar_jobs_once(
                days=30,
                limit=10,
                apply=True,
                now=harness.now,
            )
        )
        assert pending_backfill.queued == 4

        def _prepare_pending_lane(conn) -> None:
            conn.execute(
                "DELETE FROM session_bridge_state WHERE key = ?",
                ("session-bridge:sidebar:pending-lane:v1",),
            )

        harness.db._execute_write(_prepare_pending_lane)
        harness._rebuild_app()
        with harness.client() as client:
            for index, _source_id in enumerate(pending_sources):
                outcome = harness.run_worker_once(client)
                assert outcome, (
                    index,
                    harness.store.sidebar_delivery_status(now=harness.now),
                )
                assert outcome[0]["state"] == SidebarJobState.VISIBLE.value

        registration_order = [
            decode_bridge_marker(
                _registration_marker(call["prompt"]),
                _MARKER_SECRET,
            ).source_session_id
            for call in harness.native.create_calls[-4:]
        ]
        assert registration_order == [
            pending_sources[3],
            pending_sources[2],
            pending_sources[1],
            pending_sources[0],
        ]
        assert len(harness.native.create_calls) == (
            create_count_before_hydration + len(pending_sources)
        )
        assert harness.native.app_server_create_calls == []

        sidebar_counts = harness.store.sidebar_delivery_status(
            now=harness.now
        )["counts"]
        hydration_counts = harness.store.sidebar_hydration_status(
            harness.now
        )["counts"]
        assert all(
            sidebar_counts[state.value] == 0
            for state in (
                SidebarJobState.PENDING,
                SidebarJobState.LEASED,
                SidebarJobState.RETRY,
            )
        )
        assert all(
            hydration_counts[state.value] == 0
            for state in (
                SidebarHydrationState.PENDING,
                SidebarHydrationState.LEASED,
                SidebarHydrationState.RETRY,
            )
        )
        assert sidebar_counts[SidebarJobState.FAILED.value] == 0
        assert hydration_counts[SidebarHydrationState.FAILED.value] == 0
        for source_id, thread_id in original_thread_ids.items():
            assert (
                harness.store.get_sidebar_job_for_source(source_id)[
                    "codex_thread_id"
                ]
                == thread_id
            )
    finally:
        harness.close()


def test_sidebar_saved_source_project_still_uses_session_inbox(tmp_path: Path) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "exact-cwd"
        harness.add_project("exact-cwd-project", source_cwd)
        harness.seed_source(Provider.CLAUDE, "exact-cwd", cwd=source_cwd)
        harness.register()

        with harness.client() as client:
            harness.run_worker_once(client)

        created = harness.native.create_calls[0]
        assert created["project_id"] == "session-inbox"
        assert _canonical_sidebar_path(created["cwd"]) == _canonical_sidebar_path(
            harness.inbox
        )
        assert tuple(created["runtime_workspace_roots"]) == (
            _canonical_sidebar_path(harness.inbox),
            _canonical_sidebar_path(source_cwd),
        )
    finally:
        harness.close()


def test_sidebar_saved_git_root_still_uses_session_inbox(tmp_path: Path) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        repo = tmp_path / "saved-git-root"
        source_cwd = repo / "nested" / "worktree"
        source_cwd.mkdir(parents=True)
        subprocess.run(
            ["git", "init", "-q", str(repo)],
            check=True,
            capture_output=True,
        )
        harness.add_project("git-root-project", repo)
        harness.seed_source(
            Provider.HERMES,
            "git-root-source",
            cwd=source_cwd,
            git_root=repo,
        )
        harness.register()

        with harness.client() as client:
            harness.run_worker_once(client)

        created = harness.native.create_calls[0]
        assert created["project_id"] == "session-inbox"
        assert _canonical_sidebar_path(created["cwd"]) == _canonical_sidebar_path(
            harness.inbox
        )
        assert tuple(created["runtime_workspace_roots"]) == (
            _canonical_sidebar_path(harness.inbox),
            _canonical_sidebar_path(source_cwd),
        )
    finally:
        harness.close()


def test_sidebar_inbox_fallback_preserves_exact_source_cwd(tmp_path: Path) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "unsaved" / "source-worktree"
        harness.seed_source(Provider.CLAUDE, "inbox-source", cwd=source_cwd)
        harness.register()

        with harness.client() as client:
            harness.run_worker_once(client)

        created = harness.native.create_calls[0]
        assert created["project_id"] == "session-inbox"
        assert _canonical_sidebar_path(created["source_cwd"]) == (
            _canonical_sidebar_path(source_cwd)
        )
        assert f"Source cwd: {json.dumps(created['source_cwd'])}" in created["prompt"]
    finally:
        harness.close()


def test_sidebar_transient_source_starts_in_inbox_and_restart_never_duplicates(
    tmp_path: Path,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = (
            tmp_path
            / "transient"
            / "repo"
            / ".claude"
            / "worktrees"
            / "placement-proof"
        )
        harness.add_project("transient-source-project", source_cwd)
        source_id = harness.seed_source(
            Provider.CLAUDE,
            "placement-proof",
            cwd=source_cwd,
            content="Keep the exact source identity while presenting this in the inbox",
        )
        harness.register()

        with harness.client() as client:
            dropped_client = _BindDroppingClient(client, timing="after_processing")
            first = harness.run_worker_once(dropped_client)

        harness.restart_bridge()
        harness.advance_retry()
        with harness.client() as client:
            second = harness.run_worker_once(client)

        created = harness.native.create_calls[0]
        assert _canonical_sidebar_path(created["cwd"]) == _canonical_sidebar_path(
            harness.inbox
        )
        assert tuple(
            _canonical_sidebar_path(root)
            for root in created["runtime_workspace_roots"]
        ) == (
            _canonical_sidebar_path(harness.inbox),
            _canonical_sidebar_path(source_cwd),
        )
        registration = decode_sidebar_registration_identity(
            created["prompt"],
            _MARKER_SECRET,
        )
        assert registration.source_session_id == source_id
        assert registration.source_cwd == str(source_cwd)
        assert f"Source cwd: {json.dumps(str(source_cwd))}" in created["prompt"]
        assert dropped_client.bind_attempts == 1
        assert first == [
            {
                "state": "sidebar_retry",
                "error_code": "bridge_temporarily_unavailable",
                "codex_thread_id": "native-sidebar-1",
            }
        ]
        assert second == [
            {"state": "sidebar_visible", "codex_thread_id": "native-sidebar-1"}
        ]
        assert len(harness.native.create_calls) == 1
        assert (
            sum(
                event["tool"] == harness.contract.commit_tool
                for trace in harness.worker_traces
                for event in trace
            )
            == 1
        )
        job = harness.store.get_sidebar_job_for_source(source_id)
        assert job["state"] == SidebarJobState.VISIBLE.value
        assert job["codex_thread_id"] == "native-sidebar-1"
        assert job["placement_generation"] == 1
        links = harness.store.get_bridge_summaries([source_id])[source_id][
            "bridge_links"
        ]
        assert len(links) == 1
        assert links[0]["relation"] == Relation.MIRRORS.value
        assert links[0]["to_session_id"] == "codex:native-sidebar-1"
    finally:
        harness.close()


@pytest.mark.parametrize(
    "drop_timing",
    ["before_processing", "after_processing"],
)
def test_desktop_response_loss_never_replacement_creates(
    tmp_path: Path,
    drop_timing: str,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_id = harness.seed_source(
            Provider.CLAUDE,
            f"desktop-drop-{drop_timing}",
            cwd=tmp_path / drop_timing,
        )
        harness.register()
        harness.native.drop_create_response = drop_timing
        with harness.client() as client:
            harness.run_worker_once(client)

        harness.restart_bridge()
        harness.advance_retry()
        with harness.client() as client:
            harness.run_worker_once(client)

        assert len(harness.native.create_calls) == 1
        job = harness.store.get_sidebar_job_for_source(source_id)
        assert job["state"] == SidebarJobState.FAILED.value
        assert job["error_code"] == "native_create_ambiguous"
        assert job["codex_thread_id"] is None
        links = harness.store.get_bridge_summaries([source_id])[source_id][
            "bridge_links"
        ]
        assert len(links) <= 1
        if drop_timing == "after_processing":
            assert set(harness.native.threads) == {"native-sidebar-1"}
        else:
            assert harness.native.threads == {}
    finally:
        harness.close()


@pytest.mark.parametrize("drop_timing", ["before_processing", "after_processing"])
def test_sidebar_commit_drop_reconciles_exact_marker_without_duplicate(
    tmp_path: Path,
    drop_timing: str,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "commit-drop"
        harness.add_project("commit-drop-project", source_cwd)
        source_id = harness.seed_source(
            Provider.CLAUDE,
            "commit-drop",
            cwd=source_cwd,
        )
        harness.register()

        with harness.client() as client:
            dropped_client = _CommitDroppingClient(client, timing=drop_timing)
            first = harness.run_worker_once(dropped_client)
            state_after_drop = harness.store.get_sidebar_job_for_source(source_id)
            if drop_timing == "before_processing":
                harness.advance_retry()
            second = harness.run_worker_once(client)

        assert dropped_client.commit_attempts == 1
        assert dropped_client.dropped is True
        assert dropped_client.tool_calls.count("session_sidebar_commit") == 1
        assert dropped_client.tool_calls.count("session_sidebar_fail") == 1
        if drop_timing == "before_processing":
            assert first == [
                {
                    "state": "sidebar_retry",
                    "error_code": "bridge_temporarily_unavailable",
                    "codex_thread_id": "native-sidebar-1",
                }
            ]
            assert state_after_drop["state"] == SidebarJobState.RETRY.value
            assert second == [
                {
                    "state": "sidebar_visible",
                    "codex_thread_id": "native-sidebar-1",
                }
            ]
            assert (
                harness.native.reconciliation_calls[-1].source_session_id == source_id
            )
        else:
            assert first == [{"state": "commit_unknown", "fail_attempted": True}]
            assert state_after_drop["state"] == SidebarJobState.VISIBLE.value
            assert second == []
        assert len(harness.native.create_calls) == 1
        assert harness.store.get_sidebar_job_for_source(source_id)["state"] == (
            SidebarJobState.VISIBLE.value
        )
    finally:
        harness.close()


@pytest.mark.parametrize("drop_timing", ["before_processing", "after_processing"])
def test_sidebar_bind_response_drop_survives_restart_without_replacement_creation(
    tmp_path: Path,
    drop_timing: str,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "bind-drop"
        harness.add_project("bind-drop-project", source_cwd)
        source_id = harness.seed_source(
            Provider.CLAUDE,
            "bind-drop",
            cwd=source_cwd,
        )
        harness.register()

        with harness.client() as client:
            dropped_client = _BindDroppingClient(client, timing=drop_timing)
            first = harness.run_worker_once(dropped_client)

        after_drop = harness.store.get_sidebar_job_for_source(source_id)
        harness.restart_bridge()
        harness.advance_retry()
        with harness.client() as client:
            second = harness.run_worker_once(client)

        assert dropped_client.bind_attempts == 1
        assert dropped_client.dropped is True
        assert dropped_client.tool_calls.count("session_sidebar_reserve") == 1
        assert dropped_client.tool_calls.count("session_sidebar_bind") == 1
        assert dropped_client.tool_calls.count("session_sidebar_fail") == 1
        assert first == [
            {
                "state": "sidebar_retry",
                "error_code": "bridge_temporarily_unavailable",
                "codex_thread_id": "native-sidebar-1",
            }
        ]
        assert after_drop["state"] == SidebarJobState.RETRY.value
        assert after_drop["codex_thread_id"] == "native-sidebar-1"
        assert second == [
            {
                "state": "sidebar_visible",
                "codex_thread_id": "native-sidebar-1",
            }
        ]
        assert len(harness.native.create_calls) == 1
        final = harness.store.get_sidebar_job_for_source(source_id)
        assert final["state"] == SidebarJobState.VISIBLE.value
        assert final["codex_thread_id"] == "native-sidebar-1"
    finally:
        harness.close()


def test_recovered_id_read_failure_settles_with_the_same_exact_id(
    tmp_path: Path,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / "missing-recovered-id"
        harness.add_project("missing-recovered-project", source_cwd)
        source_id = harness.seed_source(
            Provider.CLAUDE,
            "missing-recovered-id",
            cwd=source_cwd,
        )
        harness.register()
        lease = harness.store.claim_sidebar_jobs(now=harness.now, limit=1)[0]
        recovered_thread_id = harness.native.create_thread(
            prompt=build_registration_prompt(
                harness.store.get_sidebar_candidate_for_delivery(source_id),
                encode_bridge_marker(
                    BridgeMarkerPayload(
                        bridge_id=lease["bridge_id"],
                        source_session_id=source_id,
                        target_provider=Provider.CODEX,
                        policy_generation=1,
                    ),
                    _MARKER_SECRET,
                ),
            ),
            cwd=str(harness.inbox),
            runtimeWorkspaceRoots=[str(harness.inbox), str(source_cwd)],
        )
        harness.store.bind_sidebar_thread(
            lease_token=lease["lease_token"],
            codex_thread_id=recovered_thread_id,
            now=harness.now,
        )
        harness.store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="sqlite_busy",
            codex_thread_id=recovered_thread_id,
            now=harness.now,
        )
        harness.native.create_calls.clear()

        def unreadable_recovered(*, thread_id: str) -> dict[str, Any]:
            raise KeyError(thread_id)

        harness.native.read_thread = unreadable_recovered
        harness.advance_retry()

        with harness.client() as client:
            outcome = harness.run_worker_once(client)

        fail_event = next(
            event
            for event in harness.worker_traces[-1]
            if event["tool"] == harness.contract.fail_tool
        )
        assert fail_event["arguments"]["codex_thread_id"] == recovered_thread_id
        assert outcome == [
            {
                "state": "sidebar_retry",
                "error_code": "native_task_not_indexed",
                "codex_thread_id": recovered_thread_id,
            }
        ]
        persisted = harness.store.get_sidebar_job_for_source(source_id)
        assert persisted["codex_thread_id"] == recovered_thread_id
        assert harness.native.create_calls == []
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("thread_id", "marker_conflict"),
        ("marker", "marker_conflict"),
        ("placement", "placement_mismatch"),
    ],
)
def test_recovered_id_read_classifies_identity_and_placement_without_replacement(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / f"recovered-{mutation}"
        source_id = harness.seed_source(
            Provider.CLAUDE,
            f"recovered-{mutation}",
            cwd=source_cwd,
        )
        harness.register()
        lease = harness.store.claim_sidebar_jobs(now=harness.now, limit=1)[0]
        recovered_thread_id = harness.native.create_thread(
            prompt=build_registration_prompt(
                harness.store.get_sidebar_candidate_for_delivery(source_id),
                encode_bridge_marker(
                    BridgeMarkerPayload(
                        bridge_id=lease["bridge_id"],
                        source_session_id=source_id,
                        target_provider=Provider.CODEX,
                        policy_generation=1,
                    ),
                    _MARKER_SECRET,
                ),
            ),
            cwd=str(harness.inbox),
            runtimeWorkspaceRoots=[str(harness.inbox), str(source_cwd)],
        )
        harness.store.bind_sidebar_thread(
            lease_token=lease["lease_token"],
            codex_thread_id=recovered_thread_id,
            now=harness.now,
        )
        harness.store.fail_sidebar_job(
            lease_token=lease["lease_token"],
            error_code="sqlite_busy",
            codex_thread_id=recovered_thread_id,
            now=harness.now,
        )
        harness.native.create_calls.clear()
        original_read = harness.native.read_thread

        def mutated_read(*, thread_id: str) -> dict[str, Any]:
            result = original_read(thread_id=thread_id)
            if mutation == "thread_id":
                result["thread_id"] = "native-returned-different-id"
            elif mutation == "marker":
                result["marker"] = "HERMES_SESSION_BRIDGE_V1:wrong.signature"
            else:
                result["cwd"] = _canonical_sidebar_path(source_cwd)
            return result

        harness.native.read_thread = mutated_read
        harness.advance_retry()

        with harness.client() as client:
            outcome = harness.run_worker_once(client)

        assert outcome == [
            {
                "state": "sidebar_failed",
                "error_code": expected_code,
                "codex_thread_id": recovered_thread_id,
            }
        ]
        assert harness.native.create_calls == []
        assert harness.store.get_sidebar_job_for_source(source_id)[
            "codex_thread_id"
        ] == recovered_thread_id
    finally:
        harness.close()


def _seed_marker_search_candidate(
    harness: _SidebarEndToEndHarness,
    tmp_path: Path,
    *,
    label: str,
) -> tuple[str, str]:
    source_cwd = tmp_path / label
    source_id = harness.seed_source(
        Provider.CLAUDE,
        label,
        cwd=source_cwd,
    )
    harness.register()
    job = harness.store.get_sidebar_job_for_source(source_id)
    assert job is not None
    marker = encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=job["bridge_id"],
            source_session_id=source_id,
            target_provider=Provider.CODEX,
            policy_generation=1,
        ),
        _MARKER_SECRET,
    )
    prompt = build_registration_prompt(
        harness.store.get_sidebar_candidate_for_delivery(source_id),
        marker,
    )
    candidate_id = harness.native.create_thread(
        prompt=prompt,
        cwd=str(harness.inbox),
        runtimeWorkspaceRoots=[str(harness.inbox), str(source_cwd)],
    )
    harness.native.create_calls.clear()
    harness.native.threads[candidate_id]["payload"] = BridgeMarkerPayload(
        bridge_id="bridge:not-the-authenticated-candidate",
        source_session_id=source_id,
        target_provider=Provider.CODEX,
        policy_generation=1,
    )
    return source_id, candidate_id


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("unreadable", "native_task_not_indexed"),
        ("thread_id", "marker_conflict"),
        ("marker", "marker_conflict"),
        ("source_identity", "source_identity_mismatch"),
        ("placement", "placement_mismatch"),
    ],
)
def test_marker_search_candidate_read_reauthenticates_without_replacement(
    tmp_path: Path,
    mutation: str,
    expected_code: str,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_id, candidate_id = _seed_marker_search_candidate(
            harness,
            tmp_path,
            label=f"marker-search-{mutation}",
        )
        original_read = harness.native.read_thread

        def mutated_read(*, thread_id: str) -> dict[str, Any]:
            if mutation == "unreadable":
                raise KeyError(thread_id)
            result = original_read(thread_id=thread_id)
            if mutation == "thread_id":
                result["thread_id"] = "native-returned-different-id"
            elif mutation == "marker":
                result["marker"] = "HERMES_SESSION_BRIDGE_V1:wrong.signature"
            elif mutation == "source_identity":
                result["prompt"] = result["prompt"].replace(
                    f'Source session ID: "{source_id}"',
                    'Source session ID: "claude:different-source"',
                )
            elif mutation == "placement":
                result["cwd"] = _canonical_sidebar_path(
                    tmp_path / "wrong-placement"
                )
            return result

        harness.native.read_thread = mutated_read
        with harness.client() as client:
            outcome = harness.run_worker_once(client)

        expected_state = (
            "sidebar_retry"
            if expected_code == "native_task_not_indexed"
            else "sidebar_failed"
        )
        assert outcome == [
            {
                "state": expected_state,
                "error_code": expected_code,
                "codex_thread_id": candidate_id,
            }
        ]
        assert harness.native.create_calls == []
        assert harness.store.get_sidebar_job_for_source(source_id)[
            "codex_thread_id"
        ] == candidate_id
    finally:
        harness.close()


@pytest.mark.parametrize(
    "invalid_status",
    [
        "bridge_stopped",
        "watcher_degraded",
        "provider_malformed",
        "placement_cwd",
        "placement_generation",
    ],
)
def test_sidebar_status_preflight_stops_before_projects_or_pending(
    tmp_path: Path,
    invalid_status: str,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / f"status-{invalid_status}"
        harness.seed_source(
            Provider.CLAUDE,
            f"status-{invalid_status}",
            cwd=source_cwd,
        )
        harness.register()

        def mutate(status: dict[str, Any]) -> dict[str, Any]:
            if invalid_status == "bridge_stopped":
                status["health"]["running"] = False
            elif invalid_status == "watcher_degraded":
                status["health"]["watcher_state"] = "degraded"
            elif invalid_status == "provider_malformed":
                status["health"]["providers"]["claude"].pop(
                    "degraded_reason",
                    None,
                )
            elif invalid_status == "placement_cwd":
                status["sidebar"]["placement"]["inbox_cwd"] = str(
                    tmp_path / "wrong-inbox"
                )
            else:
                status["sidebar"]["placement"]["generation"] = 2
            return status

        harness.status_mutator = mutate
        with harness.client() as client:
            assert harness.run_worker_once(client) == []

        assert [event["tool"] for event in harness.worker_traces[-1]] == [
            harness.contract.status_tool
        ]
        assert harness.native.create_calls == []
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("degraded_provider", "healthy_provider"),
    [
        (Provider.CLAUDE, Provider.HERMES),
        (Provider.HERMES, Provider.CLAUDE),
    ],
)
def test_sidebar_provider_degradation_isolated_from_healthy_queued_delivery(
    tmp_path: Path,
    degraded_provider: Provider,
    healthy_provider: Provider,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / f"healthy-{healthy_provider.value}"
        source_id = harness.seed_source(
            healthy_provider,
            f"healthy-{healthy_provider.value}",
            cwd=source_cwd,
        )
        harness.register()

        def degrade_one_provider(status: dict[str, Any]) -> dict[str, Any]:
            status["health"]["providers"][degraded_provider.value] = {
                "last_success": None,
                "lag_seconds": None,
                "degraded_reason": "scan_failed",
            }
            return status

        harness.status_mutator = degrade_one_provider
        with harness.client() as client:
            delivered = harness.run_worker_once(client)

        assert delivered == [
            {"state": "sidebar_visible", "codex_thread_id": "native-sidebar-1"}
        ]
        assert harness.store.get_sidebar_job_for_source(source_id)["state"] == (
            SidebarJobState.VISIBLE.value
        )
    finally:
        harness.close()


@pytest.mark.parametrize("invalid_projects", ["duplicate_inbox", "remote_inbox"])
def test_sidebar_project_preflight_stops_before_pending(
    tmp_path: Path,
    invalid_projects: str,
) -> None:
    harness = _SidebarEndToEndHarness(tmp_path)
    try:
        source_cwd = tmp_path / f"project-{invalid_projects}"
        harness.seed_source(
            Provider.CLAUDE,
            f"project-{invalid_projects}",
            cwd=source_cwd,
        )
        harness.register()
        if invalid_projects == "duplicate_inbox":
            harness.native.add_project("duplicate-session-inbox", harness.inbox)
        else:
            harness.native.projects[0]["hostId"] = "remote-host"

        with harness.client() as client:
            assert harness.run_worker_once(client) == []

        assert [event["tool"] for event in harness.worker_traces[-1]] == [
            harness.contract.status_tool,
            harness.contract.projects_tool,
        ]
        assert harness.native.create_calls == []
    finally:
        harness.close()


@pytest.mark.parametrize(
    ("needle", "replacement"),
    [
        (
            "For the leased job, select only the saved `Session Inbox` project whose "
            "canonical path "
            "equals the resolved canonical local `.hermes` inbox cwd",
            "For the leased job, select the exact source cwd project first, then its "
            "exact git root, and use the Session Inbox only as a fallback",
        ),
        (
            '`create_thread({"prompt":"<registration_prompt verbatim>",'
            '"target":{"type":"project","projectId":'
            '"local-e59c279a6cdda9313cf111e46a80b027",'
            '"environment":{"type":"local"}}})`',
            '`create_thread({"prompt":"<registration_prompt verbatim>",'
            '"target":{"type":"project","projectId":"<chosen projectId>",'
            '"environment":"local"}})`',
        ),
        (
            "try fail/release once with `bridge_temporarily_unavailable`",
            "leave the lease unsettled",
        ),
        (
            "Never use app-server thread creation as a fallback",
            "Use app-server thread creation as a fallback",
        ),
    ],
)
def test_sidebar_harness_rejects_mutated_shipped_skill_contract(
    tmp_path: Path,
    needle: str,
    replacement: str,
) -> None:
    shipped = _SIDEBAR_SKILL_PATH.read_text(encoding="utf-8")
    assert shipped.count(needle) == 1
    mutated = tmp_path / "mutated-sidebar-SKILL.md"
    mutated.write_text(shipped.replace(needle, replacement), encoding="utf-8")

    with pytest.raises(ValueError, match="sidebar skill contract"):
        _SidebarEndToEndHarness(
            tmp_path / "harness",
            skill_path=mutated,
        )


@pytest.mark.parametrize(
    "insertion",
    [
        (
            "Operational shortcut: when the source folder is already a saved "
            "workspace, prefer that workspace over the inbox."
        ),
        (
            "When a saved source project exists, choose it before Session Inbox."
        ),
        (
            "Favor the originating directory ahead of Session Inbox for task "
            "placement."
        ),
    ],
)
def test_sidebar_harness_rejects_semantic_source_first_rule_outside_contract_blocks(
    tmp_path: Path,
    insertion: str,
) -> None:
    shipped = _SIDEBAR_SKILL_PATH.read_text(encoding="utf-8")
    mutated = tmp_path / "semantic-source-first-SKILL.md"
    mutated.write_text(
        shipped.replace(
            "\n## Verification\n",
            f"\n{insertion}\n\n## Verification\n",
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="sidebar skill contract"):
        _SidebarEndToEndHarness(
            tmp_path / "harness",
            skill_path=mutated,
        )
