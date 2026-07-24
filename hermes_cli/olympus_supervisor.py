"""Observe-only Olympus supervisor backed exclusively by Hermes Kanban.

The supervisor never claims a card, launches a provider, mutates a repository,
consumes authority, sends a message, or writes to the Kanban database.  It opens
the configured board with SQLite ``mode=ro`` and writes only profile-scoped
checkpoint/projection/draft files beneath
``$HERMES_HOME/olympus-supervisor``.

Olympus cards are ordinary Kanban tasks with ``tenant="olympus"``.  Their
``body`` is a strict JSON object using ``olympus-kanban-task/1``.  Dependencies
remain canonical ``task_links`` rows and active ownership remains canonical
Kanban task/run claim state; no second queue is introduced here.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home
from hermes_cli import kanban_db
from hermes_cli.job_diagnostics import (
    DEFAULT_IDLE_AFTER_SECONDS,
    LaneStatus,
    capture_process_identity,
    diagnostics_snapshot,
    process_identity_status,
)

try:  # pragma: no cover - exercised on POSIX CI and macOS operator hosts.
    import fcntl
except ImportError:  # pragma: no cover - Windows compatibility.
    fcntl = None  # type: ignore[assignment]
    import msvcrt


SUPERVISOR_SCHEMA = "hermes-olympus-supervisor/1"
TASK_SCHEMA = "olympus-kanban-task/1"
GOAL_SCHEMA = "olympus-provider-goal/1"
GOAL_SET_SCHEMA = "olympus-provider-goal-set/1"
MISSION_CONTROL_SCHEMA = "olympus-mission-control-projection/1"
TELEGRAM_OUTBOX_SCHEMA = "olympus-telegram-draft-outbox/1"
STOP_SCHEMA = "olympus-supervisor-stop/1"
LEASE_SCHEMA = "olympus-supervisor-lease/1"
HEARTBEAT_SCHEMA = "olympus-supervisor-heartbeat/1"
FAILURE_SCHEMA = "olympus-supervisor-failure/1"
SHORT_SOAK_SCHEMA = "olympus-short-soak/1"

PROVIDER_ORDER = ("codex", "claude", "grok", "hermes")
PROVIDER_ALIASES = {
    "codex": "codex",
    "openai": "codex",
    "openai_codex": "codex",
    "openai-codex": "codex",
    "chatgpt": "codex",
    "claude": "claude",
    "anthropic": "claude",
    "grok": "grok",
    "xai": "grok",
    "x-ai": "grok",
    "hermes": "hermes",
    "hermes_orchestration": "hermes",
    "hermes-orchestration": "hermes",
}
RISK_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}
COMPLETE_STATUSES = {"done", "archived"}
LEASED_STATUSES = {"running"}
ACTIVE_DIAGNOSTIC_STATUSES = {
    LaneStatus.WORKING.value,
    LaneStatus.WAITING.value,
    LaneStatus.IDLE.value,
    LaneStatus.BLOCKED.value,
    LaneStatus.STALE.value,
}
STALE_DIAGNOSTIC_STATUSES = {
    LaneStatus.STALE.value,
    LaneStatus.DEAD.value,
}
_SLOT_RE = re.compile(r"^(codex|claude|grok|hermes):([1-9][0-9]*)$")
_MAX_OBJECTIVE_CHARS = 4000
_MAX_LIST_ITEMS = 64
_MAX_LIST_ITEM_CHARS = 1000
_MAX_SHORT_SOAK_CYCLES = 100
_MAX_CONSECUTIVE_CYCLE_FAILURES = 20
_MAX_ESTIMATED_TOKENS = 1_000_000_000
_OPERATIONAL_TASK_FIELDS = {
    "claim_lock",
    "claim_expires",
    "worker_pid",
    "last_heartbeat_at",
    "started_at",
    "ended_at",
}
_OPERATIONAL_RUN_FIELDS = {
    "claim_lock",
    "claim_expires",
    "worker_pid",
    "last_heartbeat_at",
    "started_at",
    "ended_at",
}


class OlympusSupervisorError(RuntimeError):
    """Base fail-closed supervisor error with a stable operator code."""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class QueueValidationError(OlympusSupervisorError):
    pass


class QueueDriftError(OlympusSupervisorError):
    pass


class DuplicateSupervisorError(OlympusSupervisorError):
    pass


class CheckpointDriftError(OlympusSupervisorError):
    pass


class StopRequested(OlympusSupervisorError):
    pass


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _iso_utc(timestamp: float) -> str:
    return (
        datetime
        .fromtimestamp(timestamp, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_timestamp(value: Any, *, field: str, task_id: str) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: {field} must be an ISO-8601 timestamp or epoch seconds",
        )
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str):
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: {field} must be an ISO-8601 timestamp or epoch seconds",
        )
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: {field} is not valid ISO-8601",
        ) from exc
    if parsed.tzinfo is None:
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: {field} must include a timezone",
        )
    return parsed.timestamp()


def _as_decimal(value: Any, *, field: str, task_id: str) -> Decimal:
    if isinstance(value, bool):
        raise QueueValidationError(
            "malformed_task", f"{task_id}: {field} must be a non-negative number"
        )
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise QueueValidationError(
            "malformed_task", f"{task_id}: {field} must be a non-negative number"
        ) from exc
    if not result.is_finite() or result < 0:
        raise QueueValidationError(
            "malformed_task", f"{task_id}: {field} must be a non-negative number"
        )
    return result


def _normalize_provider(value: Any, *, task_id: str = "queue") -> str:
    key = str(value or "").strip().lower().replace(" ", "_")
    provider = PROVIDER_ALIASES.get(key)
    if provider is None:
        raise QueueValidationError(
            "unknown_provider",
            f"{task_id}: unsupported provider {value!r}; expected one of "
            f"{', '.join(PROVIDER_ORDER)}",
        )
    return provider


def _clean_string_list(value: Any, *, field: str, task_id: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise QueueValidationError(
            "malformed_task", f"{task_id}: {field} must be an array of strings"
        )
    if len(value) > _MAX_LIST_ITEMS:
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: {field} exceeds {_MAX_LIST_ITEMS} entries",
        )
    cleaned: list[str] = []
    for item in value:
        text = item.strip()
        if not text or len(text) > _MAX_LIST_ITEM_CHARS:
            raise QueueValidationError(
                "malformed_task",
                f"{task_id}: {field} contains an empty or oversized entry",
            )
        if text not in cleaned:
            cleaned.append(text)
    return cleaned


def _parse_task_metadata(body: Any, task_id: str) -> dict[str, Any]:
    if not isinstance(body, str) or not body.strip():
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: active Olympus task body must contain {TASK_SCHEMA} JSON",
        )
    try:
        raw = json.loads(body)
    except json.JSONDecodeError as exc:
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: Olympus task body is not valid JSON ({exc.msg})",
        ) from exc
    if not isinstance(raw, dict):
        raise QueueValidationError(
            "malformed_task", f"{task_id}: Olympus task body must be a JSON object"
        )
    value = raw.get("olympus", raw)
    if not isinstance(value, dict):
        raise QueueValidationError(
            "malformed_task", f"{task_id}: olympus metadata must be an object"
        )
    if value.get("schema_version") != TASK_SCHEMA:
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: schema_version must be {TASK_SCHEMA!r}",
        )
    if not isinstance(value.get("enabled"), bool):
        raise QueueValidationError(
            "malformed_task", f"{task_id}: enabled must be true or false"
        )
    risk = str(value.get("risk") or "").strip().lower()
    if risk not in RISK_ORDER:
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: risk must be one of {', '.join(RISK_ORDER)}",
        )
    providers_raw = value.get("providers")
    if (
        not isinstance(providers_raw, list)
        or not providers_raw
        or any(not isinstance(item, str) for item in providers_raw)
    ):
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: providers must be a non-empty array",
        )
    providers: list[str] = []
    for item in providers_raw:
        provider = _normalize_provider(item, task_id=task_id)
        if provider not in providers:
            providers.append(provider)

    authority = value.get("authority")
    if not isinstance(authority, dict):
        raise QueueValidationError(
            "malformed_task", f"{task_id}: authority must be an object"
        )
    authority_status = str(authority.get("status") or "").strip().lower()
    if not authority_status:
        raise QueueValidationError(
            "malformed_task", f"{task_id}: authority.status is required"
        )
    recommendation_allowed = authority.get("recommendation_allowed")
    if not isinstance(recommendation_allowed, bool):
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: authority.recommendation_allowed must be true or false",
        )
    authority_expiry = _parse_timestamp(
        authority.get("expires_at"),
        field="authority.expires_at",
        task_id=task_id,
    )

    approval = value.get("approval", {})
    if not isinstance(approval, dict):
        raise QueueValidationError(
            "malformed_task", f"{task_id}: approval must be an object"
        )
    approval_required = approval.get("required", False)
    if not isinstance(approval_required, bool):
        raise QueueValidationError(
            "malformed_task", f"{task_id}: approval.required must be true or false"
        )
    approval_status = (
        str(
            approval.get("status")
            or ("pending" if approval_required else "not_required")
        )
        .strip()
        .lower()
    )

    goal = value.get("goal")
    if not isinstance(goal, dict):
        raise QueueValidationError(
            "malformed_task", f"{task_id}: goal must be an object"
        )
    objective = str(goal.get("objective") or "").strip()
    if not objective or len(objective) > _MAX_OBJECTIVE_CHARS:
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: goal.objective must be 1-{_MAX_OBJECTIVE_CHARS} characters",
        )
    max_turns = goal.get("max_turns", 20)
    timeout_seconds = goal.get("timeout_seconds", 1800)
    if (
        isinstance(max_turns, bool)
        or not isinstance(max_turns, int)
        or not 1 <= max_turns <= 100
    ):
        raise QueueValidationError(
            "malformed_task", f"{task_id}: goal.max_turns must be between 1 and 100"
        )
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, int)
        or not 1 <= timeout_seconds <= 86400
    ):
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: goal.timeout_seconds must be between 1 and 86400",
        )

    assigned_provider = value.get("assigned_provider")
    if assigned_provider not in (None, ""):
        assigned_provider = _normalize_provider(assigned_provider, task_id=task_id)
        if assigned_provider not in providers:
            raise QueueValidationError(
                "malformed_task",
                f"{task_id}: assigned_provider is not in providers",
            )
    else:
        assigned_provider = None
    assigned_slot = value.get("assigned_slot")
    if assigned_slot not in (None, ""):
        if not isinstance(assigned_slot, str) or not _SLOT_RE.match(assigned_slot):
            raise QueueValidationError(
                "malformed_task",
                f"{task_id}: assigned_slot must look like codex:1",
            )
        slot_provider = _SLOT_RE.match(assigned_slot).group(1)  # type: ignore[union-attr]
        if assigned_provider and slot_provider != assigned_provider:
            raise QueueValidationError(
                "malformed_task",
                f"{task_id}: assigned_slot provider disagrees with assigned_provider",
            )
    else:
        assigned_slot = None

    estimated_cost_known = "estimated_cost_usd" in value
    estimated_cost = _as_decimal(
        value.get("estimated_cost_usd", 0),
        field="estimated_cost_usd",
        task_id=task_id,
    )
    estimated_tokens = value.get("estimated_tokens")
    if estimated_tokens is not None and (
        isinstance(estimated_tokens, bool)
        or not isinstance(estimated_tokens, int)
        or not 0 <= estimated_tokens <= _MAX_ESTIMATED_TOKENS
    ):
        raise QueueValidationError(
            "malformed_task",
            f"{task_id}: estimated_tokens must be an integer between 0 and "
            f"{_MAX_ESTIMATED_TOKENS}",
        )
    return {
        "schema_version": TASK_SCHEMA,
        "enabled": value["enabled"],
        "risk": risk,
        "providers": providers,
        "assigned_provider": assigned_provider,
        "assigned_slot": assigned_slot,
        "estimated_cost_usd": str(estimated_cost),
        "estimated_cost_known": estimated_cost_known,
        "estimated_tokens": estimated_tokens,
        "authority": {
            "status": authority_status,
            "recommendation_allowed": recommendation_allowed,
            "expires_at": authority_expiry,
            "authority_id": str(authority.get("authority_id") or "").strip(),
            "revision": authority.get("revision"),
        },
        "approval": {
            "required": approval_required,
            "status": approval_status,
            "decision_id": str(approval.get("decision_id") or "").strip(),
        },
        "goal": {
            "objective": objective,
            "max_turns": max_turns,
            "timeout_seconds": timeout_seconds,
            "allowed_paths": _clean_string_list(
                goal.get("allowed_paths"),
                field="goal.allowed_paths",
                task_id=task_id,
            ),
            "forbidden_actions": _clean_string_list(
                goal.get("forbidden_actions"),
                field="goal.forbidden_actions",
                task_id=task_id,
            ),
            "deliverables": _clean_string_list(
                goal.get("deliverables"),
                field="goal.deliverables",
                task_id=task_id,
            ),
        },
    }


@dataclass(frozen=True)
class ProviderLimit:
    capacity: int
    available: bool


@dataclass(frozen=True)
class SupervisorSettings:
    board: str
    tenant: str
    heartbeat_interval_seconds: float
    stale_supervisor_seconds: float
    stale_task_seconds: float
    stale_job_seconds: float
    cycle_interval_seconds: float
    idle_backoff_initial_seconds: float
    idle_backoff_max_seconds: float
    idle_backoff_factor: float
    stop_poll_seconds: float
    notification_repeat_seconds: float
    max_selected_candidates: int
    max_risk: str
    max_task_estimated_cost_usd: Decimal
    max_cycle_estimated_cost_usd: Decimal
    max_cycle_estimated_tokens: int
    max_consecutive_cycle_failures: int
    provider_limits: dict[str, ProviderLimit]

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        board: str | None = None,
    ) -> "SupervisorSettings":
        raw = dict(value or {})

        def number(name: str, default: float, *, minimum: float) -> float:
            item = raw.get(name, default)
            if isinstance(item, bool):
                raise OlympusSupervisorError(
                    "invalid_config", f"{name} must be a number"
                )
            try:
                parsed = float(item)
            except (TypeError, ValueError) as exc:
                raise OlympusSupervisorError(
                    "invalid_config", f"{name} must be a number"
                ) from exc
            if parsed < minimum:
                raise OlympusSupervisorError(
                    "invalid_config", f"{name} must be at least {minimum}"
                )
            return parsed

        selected_board = board or str(raw.get("board") or "olympus").strip()
        try:
            kanban_db._normalize_board_slug(selected_board)
        except ValueError as exc:
            raise OlympusSupervisorError("invalid_config", str(exc)) from exc
        tenant = str(raw.get("tenant") or "olympus").strip()
        if not tenant:
            raise OlympusSupervisorError("invalid_config", "tenant must not be empty")
        max_risk = str(raw.get("max_risk") or "medium").strip().lower()
        if max_risk not in RISK_ORDER:
            raise OlympusSupervisorError(
                "invalid_config",
                f"max_risk must be one of {', '.join(RISK_ORDER)}",
            )
        max_selected = raw.get("max_selected_candidates", 6)
        if (
            isinstance(max_selected, bool)
            or not isinstance(max_selected, int)
            or max_selected < 1
        ):
            raise OlympusSupervisorError(
                "invalid_config", "max_selected_candidates must be a positive integer"
            )
        max_consecutive_failures = raw.get("max_consecutive_cycle_failures", 3)
        if (
            isinstance(max_consecutive_failures, bool)
            or not isinstance(max_consecutive_failures, int)
            or not 1 <= max_consecutive_failures <= _MAX_CONSECUTIVE_CYCLE_FAILURES
        ):
            raise OlympusSupervisorError(
                "invalid_config",
                "max_consecutive_cycle_failures must be an integer between 1 and "
                f"{_MAX_CONSECUTIVE_CYCLE_FAILURES}",
            )
        max_cycle_tokens = raw.get("max_cycle_estimated_tokens", 0)
        if (
            isinstance(max_cycle_tokens, bool)
            or not isinstance(max_cycle_tokens, int)
            or not 0 <= max_cycle_tokens <= _MAX_ESTIMATED_TOKENS
        ):
            raise OlympusSupervisorError(
                "invalid_config",
                "max_cycle_estimated_tokens must be an integer between 0 and "
                f"{_MAX_ESTIMATED_TOKENS}",
            )

        limits_raw = raw.get("providers") or {}
        if not isinstance(limits_raw, Mapping):
            raise OlympusSupervisorError(
                "invalid_config", "providers must be an object"
            )
        default_capacities = {"codex": 2, "claude": 2, "grok": 1, "hermes": 1}
        provider_limits: dict[str, ProviderLimit] = {}
        for provider in PROVIDER_ORDER:
            item = limits_raw.get(provider, {})
            if item is None:
                item = {}
            if not isinstance(item, Mapping):
                raise OlympusSupervisorError(
                    "invalid_config", f"providers.{provider} must be an object"
                )
            capacity = item.get("capacity", default_capacities[provider])
            available = item.get("available", True)
            if (
                isinstance(capacity, bool)
                or not isinstance(capacity, int)
                or capacity < 0
            ):
                raise OlympusSupervisorError(
                    "invalid_config",
                    f"providers.{provider}.capacity must be a non-negative integer",
                )
            if not isinstance(available, bool):
                raise OlympusSupervisorError(
                    "invalid_config",
                    f"providers.{provider}.available must be true or false",
                )
            provider_limits[provider] = ProviderLimit(capacity, available)

        def money(name: str, default: str) -> Decimal:
            try:
                parsed = Decimal(str(raw.get(name, default)))
            except (InvalidOperation, ValueError) as exc:
                raise OlympusSupervisorError(
                    "invalid_config", f"{name} must be a non-negative number"
                ) from exc
            if not parsed.is_finite() or parsed < 0:
                raise OlympusSupervisorError(
                    "invalid_config", f"{name} must be a non-negative number"
                )
            return parsed

        return cls(
            board=selected_board,
            tenant=tenant,
            heartbeat_interval_seconds=number(
                "heartbeat_interval_seconds", 60, minimum=1
            ),
            stale_supervisor_seconds=number("stale_supervisor_seconds", 180, minimum=2),
            stale_task_seconds=number("stale_task_seconds", 86400, minimum=1),
            stale_job_seconds=number("stale_job_seconds", 900, minimum=1),
            cycle_interval_seconds=number("cycle_interval_seconds", 60, minimum=1),
            idle_backoff_initial_seconds=number(
                "idle_backoff_initial_seconds", 60, minimum=1
            ),
            idle_backoff_max_seconds=number("idle_backoff_max_seconds", 600, minimum=1),
            idle_backoff_factor=number("idle_backoff_factor", 2, minimum=1),
            stop_poll_seconds=number("stop_poll_seconds", 5, minimum=0.1),
            notification_repeat_seconds=number(
                "notification_repeat_seconds", 3600, minimum=1
            ),
            max_selected_candidates=max_selected,
            max_risk=max_risk,
            max_task_estimated_cost_usd=money("max_task_estimated_cost_usd", "0"),
            max_cycle_estimated_cost_usd=money("max_cycle_estimated_cost_usd", "0"),
            max_cycle_estimated_tokens=max_cycle_tokens,
            max_consecutive_cycle_failures=max_consecutive_failures,
            provider_limits=provider_limits,
        )


class AtomicStateStore:
    """Crash-safe JSON sidecars and the explicit global stop control."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        clock: Callable[[], float] = time.time,
        crash_injector: Callable[[str, Path], None] | None = None,
    ):
        self.root = Path(root or (get_hermes_home() / "olympus-supervisor")).resolve()
        self.clock = clock
        self.crash_injector = crash_injector
        self.checkpoint_path = self.root / "checkpoint.json"
        self.heartbeat_path = self.root / "heartbeat.json"
        self.failure_path = self.root / "last-failure.json"
        self.lease_path = self.root / "supervisor-lease.json"
        self.lock_path = self.root / "supervisor.lock"
        self.stop_path = self.root / "STOP.json"
        self.goals_path = self.root / "proposed-goals.json"
        self.mission_control_path = self.root / "projections" / "mission-control.json"
        self.telegram_outbox_path = self.root / "drafts" / "telegram-outbox.json"

    def _assert_internal(self, path: Path) -> None:
        resolved_parent = path.parent.resolve()
        if resolved_parent != self.root and self.root not in resolved_parent.parents:
            raise OlympusSupervisorError(
                "unsafe_state_path", f"path escapes supervisor state root: {path}"
            )

    def read_json(self, path: Path, *, required: bool = False) -> Any:
        self._assert_internal(path)
        if not path.exists():
            if required:
                raise OlympusSupervisorError(
                    "missing_state", f"state file is missing: {path}"
                )
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OlympusSupervisorError(
                "malformed_state", f"{path.name}: {exc}"
            ) from exc

    def write_json(self, path: Path, value: Any) -> None:
        self._assert_internal(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            path.parent.chmod(0o700)
        except OSError:
            pass
        raw = json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        temp_name: str | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=path.parent,
                prefix=f".{path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temp_name = handle.name
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.chmod(temp_name, 0o600)
            except OSError:
                pass
            if self.crash_injector is not None:
                self.crash_injector("before_replace", path)
            os.replace(temp_name, path)
            temp_name = None
            try:
                dir_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                dir_fd = -1
            if dir_fd >= 0:
                try:
                    os.fsync(dir_fd)
                except OSError:
                    pass
                finally:
                    os.close(dir_fd)
        finally:
            if temp_name:
                try:
                    Path(temp_name).unlink()
                except OSError:
                    pass

    def stop_reason(self) -> dict[str, Any] | None:
        value = self.read_json(self.stop_path)
        if value is None:
            return None
        if not isinstance(value, dict) or value.get("schema_version") != STOP_SCHEMA:
            raise OlympusSupervisorError(
                "malformed_stop_control",
                f"{self.stop_path} is not a valid {STOP_SCHEMA} control",
            )
        return value

    def request_stop(
        self, reason: str, *, requested_by: str = "operator"
    ) -> dict[str, Any]:
        existing = self.stop_reason()
        if existing is not None:
            return existing
        now = self.clock()
        value = {
            "schema_version": STOP_SCHEMA,
            "reason": " ".join(str(reason or "operator emergency stop").split()),
            "requested_by": requested_by,
            "requested_at": now,
            "requested_at_iso": _iso_utc(now),
        }
        self.write_json(self.stop_path, value)
        return value

    def clear_stop(self) -> dict[str, Any] | None:
        existing = self.stop_reason()
        if existing is None:
            return None
        try:
            self.stop_path.unlink()
        except FileNotFoundError:
            pass
        return existing

    def load_checkpoint(self) -> dict[str, Any] | None:
        value = self.read_json(self.checkpoint_path)
        if value is None:
            return None
        return validate_checkpoint(value)

    def write_checkpoint(self, checkpoint: dict[str, Any]) -> None:
        validate_checkpoint(checkpoint)
        self.write_json(self.checkpoint_path, checkpoint)

    def load_outbox(self) -> dict[str, Any]:
        value = self.read_json(self.telegram_outbox_path)
        if value is None:
            return {
                "schema_version": TELEGRAM_OUTBOX_SCHEMA,
                "delivery_mode": "draft_only",
                "messages": [],
            }
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != TELEGRAM_OUTBOX_SCHEMA
            or not isinstance(value.get("messages"), list)
        ):
            raise OlympusSupervisorError(
                "malformed_state",
                f"{self.telegram_outbox_path.name} has invalid schema",
            )
        return value

    def append_drafts(self, drafts: Sequence[Mapping[str, Any]]) -> int:
        if not drafts:
            return 0
        outbox = self.load_outbox()
        existing = {
            str(item.get("dedupe_key"))
            for item in outbox["messages"]
            if isinstance(item, dict)
        }
        added = 0
        for item in drafts:
            key = str(item.get("dedupe_key") or "")
            if not key or key in existing:
                continue
            outbox["messages"].append(dict(item))
            existing.add(key)
            added += 1
        outbox["messages"] = outbox["messages"][-500:]
        if added:
            self.write_json(self.telegram_outbox_path, outbox)
        return added


def _checkpoint_payload(checkpoint: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(checkpoint)
    payload.pop("checkpoint_digest", None)
    return payload


def _validate_short_soak(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CheckpointDriftError("checkpoint_drift", "short_soak must be an object")
    required = {
        "schema_version",
        "soak_id",
        "status",
        "target_cycles",
        "successful_cycles",
        "remaining_cycles",
        "consecutive_cycle_failures",
        "max_consecutive_cycle_failures",
        "max_cycle_estimated_cost_usd",
        "max_cycle_estimated_tokens",
        "started_at",
        "updated_at",
    }
    missing = sorted(required - set(value))
    if missing:
        raise CheckpointDriftError(
            "checkpoint_drift", f"short_soak is missing fields: {missing}"
        )
    if value.get("schema_version") != SHORT_SOAK_SCHEMA:
        raise CheckpointDriftError(
            "checkpoint_drift",
            f"unsupported short_soak schema {value.get('schema_version')!r}",
        )
    if not isinstance(value.get("soak_id"), str) or not value["soak_id"]:
        raise CheckpointDriftError(
            "checkpoint_drift", "short_soak.soak_id must be a non-empty string"
        )
    if value.get("status") not in {
        "running",
        "completed",
        "failure_ceiling_reached",
    }:
        raise CheckpointDriftError("checkpoint_drift", "short_soak.status is invalid")
    for field, minimum, maximum in (
        ("target_cycles", 1, _MAX_SHORT_SOAK_CYCLES),
        ("successful_cycles", 0, _MAX_SHORT_SOAK_CYCLES),
        ("remaining_cycles", 0, _MAX_SHORT_SOAK_CYCLES),
        (
            "consecutive_cycle_failures",
            0,
            _MAX_CONSECUTIVE_CYCLE_FAILURES,
        ),
        (
            "max_consecutive_cycle_failures",
            1,
            _MAX_CONSECUTIVE_CYCLE_FAILURES,
        ),
        ("max_cycle_estimated_tokens", 0, _MAX_ESTIMATED_TOKENS),
    ):
        item = value.get(field)
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not minimum <= item <= maximum
        ):
            raise CheckpointDriftError(
                "checkpoint_drift",
                f"short_soak.{field} must be an integer between "
                f"{minimum} and {maximum}",
            )
    target = int(value["target_cycles"])
    successful = int(value["successful_cycles"])
    if successful > target or value["remaining_cycles"] != target - successful:
        raise CheckpointDriftError(
            "checkpoint_drift",
            "short_soak cycle progress is inconsistent",
        )
    failures = int(value["consecutive_cycle_failures"])
    ceiling = int(value["max_consecutive_cycle_failures"])
    if failures > ceiling:
        raise CheckpointDriftError(
            "checkpoint_drift",
            "short_soak consecutive failures exceed the configured ceiling",
        )
    if value["status"] == "completed" and successful != target:
        raise CheckpointDriftError(
            "checkpoint_drift",
            "completed short_soak has remaining cycles",
        )
    if value["status"] == "failure_ceiling_reached" and failures != ceiling:
        raise CheckpointDriftError(
            "checkpoint_drift",
            "short_soak failure status does not match its ceiling",
        )
    try:
        cost_limit = Decimal(str(value["max_cycle_estimated_cost_usd"]))
    except (InvalidOperation, ValueError) as exc:
        raise CheckpointDriftError(
            "checkpoint_drift",
            "short_soak.max_cycle_estimated_cost_usd must be non-negative",
        ) from exc
    if not cost_limit.is_finite() or cost_limit < 0:
        raise CheckpointDriftError(
            "checkpoint_drift",
            "short_soak.max_cycle_estimated_cost_usd must be non-negative",
        )
    for field in ("started_at", "updated_at"):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise CheckpointDriftError(
                "checkpoint_drift", f"short_soak.{field} must be a number"
            )
    return dict(value)


def validate_checkpoint(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise CheckpointDriftError(
            "checkpoint_drift", "checkpoint root must be an object"
        )
    if value.get("schema_version") != SUPERVISOR_SCHEMA:
        raise CheckpointDriftError(
            "checkpoint_drift",
            f"unsupported checkpoint schema {value.get('schema_version')!r}",
        )
    required = {
        "run_id",
        "generation",
        "queue_snapshot_identity",
        "queue",
        "completed_cycles",
        "status",
        "ranked_queue",
        "selected_candidates",
        "blocked_candidates",
        "provider_availability",
        "active_leases_observed",
        "stale_jobs",
        "dead_jobs",
        "blocked_jobs",
        "stale_tasks",
        "resumable_jobs",
        "diagnostic_issues",
        "last_heartbeat",
        "last_successful_reconciliation",
        "pending_operator_decisions",
        "backoff",
        "restart_checkpoint",
        "checkpoint_digest",
    }
    missing = sorted(required - set(value))
    if missing:
        raise CheckpointDriftError(
            "checkpoint_drift", f"checkpoint is missing fields: {missing}"
        )
    if not isinstance(value.get("run_id"), str) or not value["run_id"]:
        raise CheckpointDriftError(
            "checkpoint_drift", "checkpoint run_id must be a non-empty string"
        )
    for field, minimum in (("generation", 1), ("completed_cycles", 0)):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < minimum:
            raise CheckpointDriftError(
                "checkpoint_drift",
                f"checkpoint {field} must be an integer >= {minimum}",
            )
    queue_identity = value.get("queue_snapshot_identity")
    if (
        not isinstance(queue_identity, str)
        or not queue_identity.startswith("sha256:")
        or len(queue_identity) != 71
    ):
        raise CheckpointDriftError(
            "checkpoint_drift",
            "queue_snapshot_identity must be a sha256 identity",
        )
    semantic_identity = value.get("semantic_queue_identity")
    if semantic_identity is not None and semantic_identity != queue_identity:
        raise CheckpointDriftError(
            "checkpoint_drift",
            "semantic_queue_identity must match queue_snapshot_identity",
        )
    occupancy_identity = value.get("operational_occupancy_identity")
    if occupancy_identity is not None and (
        not isinstance(occupancy_identity, str)
        or not occupancy_identity.startswith("sha256:")
        or len(occupancy_identity) != 71
    ):
        raise CheckpointDriftError(
            "checkpoint_drift",
            "operational_occupancy_identity must be a sha256 identity",
        )
    if (
        not isinstance(value.get("queue"), dict)
        or value["queue"].get("authority") != "hermes_kanban"
    ):
        raise CheckpointDriftError(
            "checkpoint_drift",
            "checkpoint queue must declare hermes_kanban authority",
        )
    if value.get("status") not in {
        "working",
        "idle",
        "waiting",
        "blocked",
        "stopped",
        "failed",
    }:
        raise CheckpointDriftError("checkpoint_drift", "checkpoint status is invalid")
    for field in (
        "ranked_queue",
        "selected_candidates",
        "blocked_candidates",
        "active_leases_observed",
        "stale_jobs",
        "dead_jobs",
        "blocked_jobs",
        "stale_tasks",
        "resumable_jobs",
        "diagnostic_issues",
        "pending_operator_decisions",
    ):
        items = value.get(field)
        if not isinstance(items, list) or any(
            not isinstance(item, dict) for item in items
        ):
            raise CheckpointDriftError(
                "checkpoint_drift",
                f"checkpoint {field} must be an array of objects",
            )
    if "provider_occupancy_issues" in value:
        issues = value["provider_occupancy_issues"]
        if not isinstance(issues, list) or any(
            not isinstance(item, dict) for item in issues
        ):
            raise CheckpointDriftError(
                "checkpoint_drift",
                "checkpoint provider_occupancy_issues must be an array of objects",
            )
    if not isinstance(value.get("provider_availability"), dict):
        raise CheckpointDriftError(
            "checkpoint_drift",
            "checkpoint provider_availability must be an object",
        )
    failure_count = value.get("consecutive_cycle_failures", 0)
    if (
        isinstance(failure_count, bool)
        or not isinstance(failure_count, int)
        or not 0 <= failure_count <= _MAX_CONSECUTIVE_CYCLE_FAILURES
    ):
        raise CheckpointDriftError(
            "checkpoint_drift",
            "checkpoint consecutive_cycle_failures is invalid",
        )
    if "short_soak" in value:
        soak = _validate_short_soak(value["short_soak"])
        if soak["consecutive_cycle_failures"] != failure_count:
            raise CheckpointDriftError(
                "checkpoint_drift",
                "short_soak failure count disagrees with checkpoint",
            )
    if not isinstance(value.get("backoff"), dict):
        raise CheckpointDriftError(
            "checkpoint_drift", "checkpoint backoff must be an object"
        )
    for field in ("last_heartbeat", "last_successful_reconciliation"):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise CheckpointDriftError(
                "checkpoint_drift", f"checkpoint {field} must be a number"
            )
    restart = value.get("restart_checkpoint")
    if not isinstance(restart, dict):
        raise CheckpointDriftError(
            "checkpoint_drift", "restart_checkpoint must be an object"
        )
    for field in (
        "run_id",
        "generation",
        "queue_snapshot_identity",
        "completed_cycles",
    ):
        if restart.get(field) != value.get(field):
            raise CheckpointDriftError(
                "checkpoint_drift",
                f"restart_checkpoint.{field} does not match checkpoint",
            )
    expected = _digest(_checkpoint_payload(value))
    if value.get("checkpoint_digest") != expected:
        raise CheckpointDriftError(
            "checkpoint_drift", "checkpoint digest does not match its content"
        )
    return dict(value)


def _lock_nonblocking(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    handle.seek(0)
    try:  # pragma: no cover - Windows compatibility.
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        raise BlockingIOError from None


def _unlock(handle: Any) -> None:
    if fcntl is not None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return
    handle.seek(0)
    try:  # pragma: no cover - Windows compatibility.
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass


def _validate_lease_record(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or value.get("schema_version") != LEASE_SCHEMA:
        raise OlympusSupervisorError(
            "malformed_state",
            f"supervisor-lease.json is not a valid {LEASE_SCHEMA} record",
        )
    if value.get("status") not in {"active", "released"}:
        raise OlympusSupervisorError(
            "malformed_state",
            "supervisor-lease.json has an invalid status",
        )
    heartbeat = value.get("heartbeat_at")
    if (
        isinstance(heartbeat, bool)
        or not isinstance(heartbeat, (int, float))
        or heartbeat < 0
    ):
        raise OlympusSupervisorError(
            "malformed_state",
            "supervisor-lease.json has an invalid heartbeat_at",
        )
    if not isinstance(value.get("process"), dict):
        raise OlympusSupervisorError(
            "malformed_state",
            "supervisor-lease.json has an invalid process identity",
        )
    return dict(value)


class SupervisorLease:
    """Single-supervisor guard backed by an OS lock and durable identity."""

    def __init__(
        self,
        store: AtomicStateStore,
        *,
        run_id: str,
        stale_after_seconds: float,
        clock: Callable[[], float],
        identity_probe: Callable[[int | None], dict[str, Any]],
        identity_status: Callable[[Mapping[str, Any] | None], str],
    ):
        self.store = store
        self.run_id = run_id
        self.stale_after_seconds = stale_after_seconds
        self.clock = clock
        self.identity_probe = identity_probe
        self.identity_status = identity_status
        self.handle: Any | None = None
        self.identity = identity_probe(None)

    def acquire(self) -> None:
        self.store.root.mkdir(parents=True, exist_ok=True)
        try:
            self.store.root.chmod(0o700)
        except OSError:
            pass
        self.handle = self.store.lock_path.open("a+")
        try:
            _lock_nonblocking(self.handle)
        except (BlockingIOError, OSError) as exc:
            self.handle.close()
            self.handle = None
            raise DuplicateSupervisorError(
                "duplicate_supervisor",
                "another supervisor holds the process lock",
            ) from exc

        try:
            previous = _validate_lease_record(
                self.store.read_json(self.store.lease_path)
            )
            if previous is not None and previous.get("status") == "active":
                previous_run = str(previous.get("run_id") or "")
                heartbeat = float(previous.get("heartbeat_at") or 0)
                age = max(0.0, self.clock() - heartbeat)
                proc_state = self.identity_status(previous.get("process"))
                if previous_run != self.run_id and (
                    age < self.stale_after_seconds or proc_state in {"alive", "unknown"}
                ):
                    detail = (
                        f"run {previous_run or 'unknown'} heartbeat age={age:.1f}s "
                        f"process={proc_state}; stale ownership cannot be disproved"
                    )
                    raise DuplicateSupervisorError("duplicate_supervisor", detail)
            self.refresh("working", completed_cycles=0)
        except Exception:
            self.release(write_state=False)
            raise

    def refresh(self, state: str, *, completed_cycles: int) -> None:
        now = self.clock()
        value = {
            "schema_version": LEASE_SCHEMA,
            "run_id": self.run_id,
            "status": "active",
            "state": state,
            "process": self.identity,
            "heartbeat_at": now,
            "heartbeat_at_iso": _iso_utc(now),
            "completed_cycles": int(completed_cycles),
        }
        self.store.write_json(self.store.lease_path, value)

    def release(self, *, state: str = "stopped", write_state: bool = True) -> None:
        if self.handle is None:
            return
        if write_state:
            now = self.clock()
            self.store.write_json(
                self.store.lease_path,
                {
                    "schema_version": LEASE_SCHEMA,
                    "run_id": self.run_id,
                    "status": "released",
                    "state": state,
                    "process": self.identity,
                    "heartbeat_at": now,
                    "heartbeat_at_iso": _iso_utc(now),
                },
            )
        _unlock(self.handle)
        self.handle.close()
        self.handle = None

    def __enter__(self) -> "SupervisorLease":
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        state = "failed" if exc_type else "stopped"
        self.release(state=state)


class KanbanQueueReader:
    """Read and validate one Kanban board without schema initialization."""

    REQUIRED_TASK_COLUMNS = {
        "id",
        "title",
        "body",
        "assignee",
        "status",
        "priority",
        "created_at",
        "claim_lock",
        "claim_expires",
        "tenant",
        "worker_pid",
        "last_heartbeat_at",
        "current_run_id",
        "block_kind",
    }
    REQUIRED_RUN_COLUMNS = {
        "id",
        "task_id",
        "profile",
        "status",
        "claim_lock",
        "claim_expires",
        "worker_pid",
        "last_heartbeat_at",
        "started_at",
        "ended_at",
        "outcome",
        "summary",
        "error",
    }

    def __init__(
        self,
        *,
        board: str,
        tenant: str,
        db_path: str | Path | None = None,
    ):
        self.board = board
        self.tenant = tenant
        self.db_path = Path(db_path or kanban_db.kanban_db_path(board=board)).resolve()

    def _connect(self) -> sqlite3.Connection:
        if not self.db_path.is_file():
            raise QueueValidationError(
                "queue_missing",
                f"Kanban board {self.board!r} has no database at {self.db_path}",
            )
        uri = self.db_path.as_uri() + "?mode=ro"
        try:
            conn = sqlite3.connect(
                uri,
                uri=True,
                timeout=2.0,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise QueueValidationError(
                "queue_unreadable", f"cannot open Kanban read-only: {exc}"
            ) from exc
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA query_only = ON")
        except sqlite3.Error:
            conn.close()
            raise
        return conn

    @staticmethod
    def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
        return {str(row["name"]) for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _detect_cycle(
        task_ids: set[str], links: Sequence[tuple[str, str]]
    ) -> list[str] | None:
        children: dict[str, list[str]] = {task_id: [] for task_id in task_ids}
        for parent, child in links:
            if parent in task_ids and child in task_ids:
                children[parent].append(child)
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(node: str, path: list[str]) -> list[str] | None:
            if node in visiting:
                start = path.index(node) if node in path else 0
                return path[start:] + [node]
            if node in visited:
                return None
            visiting.add(node)
            for child in sorted(children.get(node, [])):
                cycle = visit(child, path + [node])
                if cycle:
                    return cycle
            visiting.remove(node)
            visited.add(node)
            return None

        for task_id in sorted(task_ids):
            cycle = visit(task_id, [])
            if cycle:
                return cycle
        return None

    def load(self, *, now: float) -> dict[str, Any]:
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            quick = conn.execute("PRAGMA quick_check").fetchall()
            if not quick or any(str(row[0]).lower() != "ok" for row in quick):
                raise QueueValidationError(
                    "malformed_queue", "SQLite quick_check did not return ok"
                )
            tables = {
                str(row["name"])
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            required_tables = {"tasks", "task_links", "task_runs"}
            missing_tables = sorted(required_tables - tables)
            if missing_tables:
                raise QueueValidationError(
                    "malformed_queue",
                    f"Kanban database is missing tables: {missing_tables}",
                )
            task_columns = self._columns(conn, "tasks")
            run_columns = self._columns(conn, "task_runs")
            missing_task_columns = sorted(self.REQUIRED_TASK_COLUMNS - task_columns)
            missing_run_columns = sorted(self.REQUIRED_RUN_COLUMNS - run_columns)
            if missing_task_columns or missing_run_columns:
                raise QueueValidationError(
                    "malformed_queue",
                    "Kanban schema is missing required read fields: "
                    f"tasks={missing_task_columns}, runs={missing_run_columns}",
                )

            task_rows = [dict(row) for row in conn.execute("SELECT * FROM tasks")]
            link_rows = [
                (str(row["parent_id"]), str(row["child_id"]))
                for row in conn.execute(
                    "SELECT parent_id, child_id FROM task_links "
                    "ORDER BY parent_id, child_id"
                )
            ]
            run_rows = [dict(row) for row in conn.execute("SELECT * FROM task_runs")]
            schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
            user_version = int(conn.execute("PRAGMA user_version").fetchone()[0])

            by_id = {str(row["id"]): row for row in task_rows}
            if len(by_id) != len(task_rows):
                raise QueueValidationError(
                    "malformed_queue", "duplicate task identifiers detected"
                )
            for parent, child in link_rows:
                if parent not in by_id or child not in by_id:
                    raise QueueValidationError(
                        "malformed_queue",
                        f"dangling dependency link {parent}->{child}",
                    )

            olympus_ids = {
                task_id
                for task_id, row in by_id.items()
                if row.get("tenant") == self.tenant
            }
            relevant_ids = set(olympus_ids)
            changed = True
            while changed:
                changed = False
                for parent, child in link_rows:
                    if child in relevant_ids and parent not in relevant_ids:
                        relevant_ids.add(parent)
                        changed = True
            relevant_links = [
                (parent, child) for parent, child in link_rows if child in relevant_ids
            ]
            cycle = self._detect_cycle(relevant_ids, relevant_links)
            if cycle:
                raise QueueValidationError(
                    "malformed_queue",
                    f"cyclic dependency detected: {' -> '.join(cycle)}",
                )

            parents: dict[str, list[str]] = {task_id: [] for task_id in relevant_ids}
            for parent, child in relevant_links:
                if child in parents:
                    parents[child].append(parent)
            runs_by_task: dict[str, list[dict[str, Any]]] = {
                task_id: [] for task_id in relevant_ids
            }
            for run in run_rows:
                task_id = str(run["task_id"])
                if task_id in runs_by_task:
                    runs_by_task[task_id].append(run)
            for rows in runs_by_task.values():
                rows.sort(key=lambda item: (int(item["started_at"]), int(item["id"])))

            tasks: list[dict[str, Any]] = []
            claim_owners: dict[str, str] = {}
            slot_owners: dict[str, str] = {}
            for task_id in sorted(relevant_ids):
                row = by_id[task_id]
                status = str(row["status"])
                if status not in kanban_db.VALID_STATUSES:
                    raise QueueValidationError(
                        "malformed_queue",
                        f"{task_id}: unsupported Kanban status {status!r}",
                    )
                metadata = None
                if task_id in olympus_ids and status not in COMPLETE_STATUSES:
                    metadata = _parse_task_metadata(row.get("body"), task_id)
                active_runs = [
                    run
                    for run in runs_by_task.get(task_id, [])
                    if run.get("ended_at") is None
                ]
                claim = row.get("claim_lock")
                current_run_id = row.get("current_run_id")
                if claim:
                    if task_id not in olympus_ids:
                        # Parent state is included only for dependency truth. Its
                        # worker ownership is outside this supervisor's scope.
                        pass
                    else:
                        if status not in LEASED_STATUSES:
                            raise QueueValidationError(
                                "conflicting_lease",
                                f"{task_id}: claim exists while status={status}",
                            )
                        if len(active_runs) != 1:
                            raise QueueValidationError(
                                "conflicting_lease",
                                f"{task_id}: claimed task has {len(active_runs)} active runs",
                            )
                        run = active_runs[0]
                        if current_run_id != run["id"]:
                            raise QueueValidationError(
                                "conflicting_lease",
                                f"{task_id}: current_run_id disagrees with active run",
                            )
                        if run.get("claim_lock") != claim:
                            raise QueueValidationError(
                                "conflicting_lease",
                                f"{task_id}: task/run claim locks disagree",
                            )
                        if (
                            run.get("status") != "running"
                            or run.get("outcome") is not None
                        ):
                            raise QueueValidationError(
                                "conflicting_lease",
                                f"{task_id}: active run has terminal state fields",
                            )
                        if run.get("claim_expires") != row.get("claim_expires"):
                            raise QueueValidationError(
                                "conflicting_lease",
                                f"{task_id}: task/run claim expiries disagree",
                            )
                        if run.get("worker_pid") != row.get("worker_pid"):
                            raise QueueValidationError(
                                "conflicting_lease",
                                f"{task_id}: task/run worker pids disagree",
                            )
                        if claim in claim_owners:
                            raise QueueValidationError(
                                "conflicting_lease",
                                f"claim {claim!r} is shared by "
                                f"{claim_owners[claim]} and {task_id}",
                            )
                        claim_owners[str(claim)] = task_id
                        if metadata is None or not metadata.get("assigned_provider"):
                            raise QueueValidationError(
                                "provider_occupancy_ambiguous",
                                f"{task_id}: active lease has no assigned_provider",
                            )
                        if not metadata.get("assigned_slot"):
                            raise QueueValidationError(
                                "provider_occupancy_ambiguous",
                                f"{task_id}: active lease has no assigned_slot",
                            )
                        slot = str(metadata["assigned_slot"])
                        if slot in slot_owners:
                            raise QueueValidationError(
                                "conflicting_lease",
                                f"provider slot {slot} is shared by "
                                f"{slot_owners[slot]} and {task_id}",
                            )
                        slot_owners[slot] = task_id
                elif task_id in olympus_ids:
                    leaked_fields = [
                        name
                        for name in ("claim_expires", "worker_pid")
                        if row.get(name) is not None
                    ]
                    if (
                        status == "running"
                        or active_runs
                        or current_run_id is not None
                        or leaked_fields
                    ):
                        detail = (
                            "running/active state exists without a task claim"
                            if (
                                status == "running"
                                or active_runs
                                or current_run_id is not None
                            )
                            else "claim metadata exists without a task claim: "
                            + ", ".join(leaked_fields)
                        )
                        raise QueueValidationError(
                            "conflicting_lease",
                            f"{task_id}: {detail}",
                        )
                task = dict(row)
                task["parents"] = sorted(parents.get(task_id, []))
                task["runs"] = runs_by_task.get(task_id, [])
                task["olympus_metadata"] = metadata
                tasks.append(task)

            stat = self.db_path.stat()
            semantic_tasks = []
            operational_tasks = []
            for task_id in sorted(relevant_ids):
                row = by_id[task_id]
                semantic = {
                    key: value
                    for key, value in row.items()
                    if key not in _OPERATIONAL_TASK_FIELDS
                }
                semantic["claim_active"] = bool(row.get("claim_lock"))
                semantic_tasks.append(semantic)
                operational_tasks.append({
                    "id": task_id,
                    **{key: row.get(key) for key in sorted(_OPERATIONAL_TASK_FIELDS)},
                })
            relevant_runs = sorted(
                [row for row in run_rows if str(row["task_id"]) in relevant_ids],
                key=lambda row: (str(row["task_id"]), int(row["id"])),
            )
            semantic_runs = []
            operational_runs = []
            for row in relevant_runs:
                semantic = {
                    key: value
                    for key, value in row.items()
                    if key not in _OPERATIONAL_RUN_FIELDS
                }
                semantic["claim_active"] = bool(row.get("claim_lock"))
                semantic_runs.append(semantic)
                operational_runs.append({
                    "id": row.get("id"),
                    "task_id": row.get("task_id"),
                    **{key: row.get(key) for key in sorted(_OPERATIONAL_RUN_FIELDS)},
                })
            semantic_identity_payload = {
                "board": self.board,
                "tenant": self.tenant,
                "schema_version": schema_version,
                "user_version": user_version,
                "task_columns": sorted(task_columns),
                "run_columns": sorted(run_columns),
                "tasks": semantic_tasks,
                "links": sorted(relevant_links),
                "runs": semantic_runs,
            }
            operational_occupancy_payload = {
                "board": self.board,
                "tenant": self.tenant,
                "tasks": operational_tasks,
                "runs": operational_runs,
            }
            identity = "sha256:" + _digest(semantic_identity_payload)
            operational_occupancy_identity = "sha256:" + _digest(
                operational_occupancy_payload
            )
            return {
                "board": self.board,
                "tenant": self.tenant,
                "db_path": str(self.db_path),
                "identity": identity,
                "semantic_identity": identity,
                "operational_occupancy_identity": operational_occupancy_identity,
                "identity_model": {
                    "semantic_queue_identity": (
                        "task definitions, dependencies, status, authority, priority, "
                        "provider routing, and active ownership"
                    ),
                    "operational_occupancy_snapshot": (
                        "claim tokens, lease expiry, worker pid, heartbeat, and "
                        "run start/end timestamps"
                    ),
                },
                "schema_version": schema_version,
                "user_version": user_version,
                "observed_at": now,
                "observed_at_iso": _iso_utc(now),
                "file_identity": {
                    "device": stat.st_dev,
                    "inode": stat.st_ino,
                    "size": stat.st_size,
                },
                "tasks": tasks,
                "olympus_task_ids": sorted(olympus_ids),
                "links": [
                    {"parent_id": parent, "child_id": child}
                    for parent, child in sorted(relevant_links)
                ],
            }
        except sqlite3.Error as exc:
            raise QueueValidationError(
                "queue_unreadable", f"Kanban read failed: {exc}"
            ) from exc
        finally:
            conn.close()

    def change_token(self) -> tuple[Any, ...]:
        tokens: list[Any] = []
        for path in (self.db_path, Path(str(self.db_path) + "-wal")):
            try:
                stat = path.stat()
                tokens.extend((str(path), stat.st_mtime_ns, stat.st_size))
            except FileNotFoundError:
                tokens.extend((str(path), None, None))
        return tuple(tokens)


def _diagnostic_reconciliation(
    snapshot: Mapping[str, Any],
    *,
    now: float,
    settings: SupervisorSettings,
    diagnostics_provider: Callable[..., dict[str, Any]],
) -> dict[str, Any]:
    olympus_ids = set(snapshot.get("olympus_task_ids") or [])
    tasks = {
        str(task["id"]): task
        for task in snapshot.get("tasks") or []
        if str(task["id"]) in olympus_ids
    }
    active_leases: list[dict[str, Any]] = []
    stale_jobs: list[dict[str, Any]] = []
    dead_jobs: list[dict[str, Any]] = []
    blocked_jobs: list[dict[str, Any]] = []
    resumable_jobs: list[dict[str, Any]] = []
    stale_tasks: list[dict[str, Any]] = []
    provider_occupancy_issues: list[dict[str, str]] = []
    ambiguous_providers: set[str] = set()

    occupied: dict[str, dict[str, Any]] = {
        provider: {"slots": {}, "tasks": [], "external_consumers": []}
        for provider in PROVIDER_ORDER
    }
    for task_id, task in sorted(tasks.items()):
        metadata = task.get("olympus_metadata") or {}
        claim = task.get("claim_lock")
        runs = task.get("runs") or []
        if claim:
            provider = str(metadata["assigned_provider"])
            slot = str(metadata["assigned_slot"])
            occupied[provider]["slots"][slot] = task_id
            occupied[provider]["tasks"].append(task_id)
            active_run = next(run for run in runs if run.get("ended_at") is None)
            heartbeat = (
                task.get("last_heartbeat_at")
                or active_run.get("last_heartbeat_at")
                or active_run.get("started_at")
                or task.get("created_at")
            )
            lease = {
                "task_id": task_id,
                "claim_lock": claim,
                "claim_expires": task.get("claim_expires"),
                "run_id": active_run.get("id"),
                "provider": provider,
                "slot": slot,
                "worker_pid": task.get("worker_pid") or active_run.get("worker_pid"),
                "last_heartbeat_at": heartbeat,
            }
            active_leases.append(lease)
            heartbeat_age = max(0.0, now - float(heartbeat or now))
            expired = (
                task.get("claim_expires") is not None
                and float(task["claim_expires"]) < now
            )
            if expired or heartbeat_age >= settings.stale_job_seconds:
                stale_jobs.append({
                    "source": "kanban",
                    "task_id": task_id,
                    "run_id": active_run.get("id"),
                    "provider": provider,
                    "slot": slot,
                    "heartbeat_age_seconds": round(heartbeat_age, 3),
                    "claim_expired": expired,
                    "reason": ("claim expired" if expired else "heartbeat stale"),
                })
        elif task.get("status") == "ready":
            prior = [
                run
                for run in runs
                if run.get("outcome")
                in {
                    "crashed",
                    "timed_out",
                    "failed",
                    "spawn_failed",
                    "reclaimed",
                    "released",
                    "rate_limited",
                }
            ]
            if prior:
                resumable_jobs.append({
                    "source": "kanban",
                    "task_id": task_id,
                    "last_run_id": prior[-1].get("id"),
                    "last_outcome": prior[-1].get("outcome"),
                    "resume_surface": "kanban_ready",
                })
        age = max(0.0, now - float(task.get("created_at") or now))
        if (
            not claim
            and task.get("status") not in COMPLETE_STATUSES
            and age >= settings.stale_task_seconds
        ):
            stale_tasks.append({
                "task_id": task_id,
                "status": task.get("status"),
                "age_seconds": round(age, 3),
                "reason": "task has not reached an active lease",
            })

    try:
        diag = diagnostics_provider(
            now=now,
            idle_after=DEFAULT_IDLE_AFTER_SECONDS,
            stale_after=settings.stale_job_seconds,
        )
    except Exception as exc:
        raise QueueValidationError(
            "diagnostics_parse_failure",
            f"job diagnostics snapshot failed: {exc}",
        ) from exc
    if not isinstance(diag, dict):
        raise QueueValidationError(
            "diagnostics_parse_failure",
            "job diagnostics provider returned a non-object",
        )
    if not {"generated_at", "jobs", "issues"} <= set(diag):
        raise QueueValidationError(
            "diagnostics_parse_failure",
            "job diagnostics snapshot is missing generated_at, jobs, or issues",
        )
    generated_at = diag.get("generated_at")
    diagnostic_issues = diag.get("issues")
    jobs = diag.get("jobs")
    if (
        isinstance(generated_at, bool)
        or not isinstance(generated_at, (int, float))
        or not isinstance(diagnostic_issues, list)
        or any(not isinstance(item, dict) for item in diagnostic_issues)
        or not isinstance(jobs, list)
    ):
        raise QueueValidationError(
            "diagnostics_parse_failure",
            "job diagnostics provider returned malformed generated_at/jobs/issues",
        )
    if diagnostic_issues:
        ambiguous_providers.update(PROVIDER_ORDER)
        provider_occupancy_issues.append({
            "code": "diagnostics_incomplete",
            "detail": (
                f"{len(diagnostic_issues)} diagnostics record(s) were unreadable; "
                "non-Olympus provider occupancy is ambiguous"
            ),
        })
    diagnostic_active_by_task: dict[str, str] = {}
    for job in jobs:
        if not isinstance(job, dict):
            raise QueueValidationError(
                "diagnostics_parse_failure",
                "job diagnostics contains a non-object job",
            )
        job_id = str(job.get("job_id") or "")
        if not job_id or "lanes" not in job:
            raise QueueValidationError(
                "diagnostics_parse_failure",
                "job diagnostics contains a job with missing job_id or lanes",
            )
        lanes = job.get("lanes")
        if not isinstance(lanes, Mapping):
            raise QueueValidationError(
                "diagnostics_parse_failure",
                f"job diagnostics {job_id} has malformed lanes",
            )
        for lane in lanes.values():
            if not isinstance(lane, dict):
                raise QueueValidationError(
                    "diagnostics_parse_failure",
                    f"job diagnostics {job_id} has a malformed lane",
                )
            lane_id = str(lane.get("lane_id") or "")
            status = str(
                lane.get("effective_status") or lane.get("status") or ""
            ).strip()
            if not lane_id or status not in {item.value for item in LaneStatus}:
                raise QueueValidationError(
                    "diagnostics_parse_failure",
                    f"job diagnostics {job_id} has a lane with missing or invalid "
                    "lane_id/status",
                )
            task_id = str(lane.get("task_id") or "")
            scoped = task_id in olympus_ids
            explicitly_olympus = (
                job_id.lower().startswith("olympus")
                or str(lane.get("platform") or "").lower() == "olympus"
            )
            if not scoped and status in ACTIVE_DIAGNOSTIC_STATUSES:
                if explicitly_olympus:
                    raise QueueValidationError(
                        "provider_occupancy_ambiguous",
                        f"active Olympus diagnostic {job_id}/{lane_id} "
                        "does not reference a canonical Kanban task",
                    )
            provider_value = lane.get("provider")
            provider = None
            if provider_value:
                try:
                    provider = _normalize_provider(
                        provider_value, task_id=task_id or job_id
                    )
                except QueueValidationError:
                    if status in ACTIVE_DIAGNOSTIC_STATUSES and not scoped:
                        ambiguous_providers.update(PROVIDER_ORDER)
                        provider_occupancy_issues.append({
                            "code": "provider_occupancy_ambiguous",
                            "detail": (
                                f"{job_id}/{lane_id}: active non-Olympus consumer "
                                f"has unsupported provider {provider_value!r}"
                            ),
                        })
                    elif scoped or explicitly_olympus:
                        raise
            if status in ACTIVE_DIAGNOSTIC_STATUSES and not scoped:
                if provider is None:
                    ambiguous_providers.update(PROVIDER_ORDER)
                    provider_occupancy_issues.append({
                        "code": "provider_occupancy_ambiguous",
                        "detail": (
                            f"{job_id}/{lane_id}: active non-Olympus consumer "
                            "does not identify its provider"
                        ),
                    })
                else:
                    consumer = {
                        "job_id": job_id,
                        "lane_id": lane_id,
                        "task_id": task_id or None,
                        "provider": provider,
                        "status": status,
                        "platform": str(lane.get("platform") or "") or None,
                    }
                    occupied[provider]["external_consumers"].append(consumer)
                continue
            if not scoped and not explicitly_olympus:
                continue
            if status in ACTIVE_DIAGNOSTIC_STATUSES:
                task = tasks[task_id]
                metadata = task.get("olympus_metadata") or {}
                owner = diagnostic_active_by_task.get(task_id)
                lane_name = f"{job_id}/{lane_id}"
                if owner is not None:
                    raise QueueValidationError(
                        "provider_occupancy_ambiguous",
                        f"{task_id}: active diagnostics are duplicated by "
                        f"{owner} and {lane_name}",
                    )
                diagnostic_active_by_task[task_id] = lane_name
                if not task.get("claim_lock"):
                    raise QueueValidationError(
                        "provider_occupancy_ambiguous",
                        f"{job_id}/{lane_id}: active job has no Kanban lease",
                    )
                if not provider or provider != metadata.get("assigned_provider"):
                    raise QueueValidationError(
                        "provider_occupancy_ambiguous",
                        f"{job_id}/{lane_id}: provider disagrees with Kanban",
                    )
            if status in STALE_DIAGNOSTIC_STATUSES:
                target = dead_jobs if status == LaneStatus.DEAD.value else stale_jobs
                diagnostic_record = {
                    "source": "job_diagnostics",
                    "job_id": job_id,
                    "lane_id": lane_id,
                    "task_id": task_id or None,
                    "provider": provider,
                    "status": status,
                    "heartbeat_at": lane.get("heartbeat_at"),
                    "current_step": lane.get("current_step"),
                    "next_expected_action": lane.get("next_expected_action"),
                    "blocker": lane.get("blocker"),
                    "timing": lane.get("timing"),
                    "why_slow_command": (
                        f"hermes jobs why-slow {job_id} --lane {lane_id}"
                    ),
                    "resume_plan_command": (
                        f"hermes jobs resume-plan {job_id} --lane {lane_id}"
                    ),
                }
                target.append(diagnostic_record)
                if status == LaneStatus.DEAD.value:
                    resumable_jobs.append({
                        "source": "job_diagnostics",
                        "job_id": job_id,
                        "lane_id": lane_id,
                        "task_id": task_id or None,
                        "resume_surface": "job_diagnostics_resume_plan",
                        "resume_plan_command": diagnostic_record["resume_plan_command"],
                        "safe_to_resume": None,
                    })
            elif status == LaneStatus.BLOCKED.value:
                blocked_jobs.append({
                    "source": "job_diagnostics",
                    "job_id": job_id,
                    "lane_id": lane_id,
                    "task_id": task_id,
                    "provider": provider,
                    "status": status,
                    "current_step": lane.get("current_step"),
                    "next_expected_action": lane.get("next_expected_action"),
                    "blocker": lane.get("blocker"),
                    "timing": lane.get("timing"),
                    "why_slow_command": (
                        f"hermes jobs why-slow {job_id} --lane {lane_id}"
                    ),
                    "resume_plan_command": (
                        f"hermes jobs resume-plan {job_id} --lane {lane_id}"
                    ),
                })

    active_leases.sort(key=lambda item: item["task_id"])
    stale_jobs.sort(
        key=lambda item: (
            str(item.get("task_id") or ""),
            str(item.get("job_id") or ""),
            str(item.get("lane_id") or ""),
        )
    )
    dead_jobs.sort(
        key=lambda item: (
            str(item.get("task_id") or ""),
            str(item.get("job_id") or ""),
        )
    )
    blocked_jobs.sort(
        key=lambda item: (
            str(item.get("task_id") or ""),
            str(item.get("job_id") or ""),
            str(item.get("lane_id") or ""),
        )
    )
    resumable_jobs.sort(
        key=lambda item: (
            str(item.get("task_id") or ""),
            str(item.get("job_id") or ""),
            str(item.get("lane_id") or ""),
        )
    )
    for provider in PROVIDER_ORDER:
        occupied[provider]["external_consumers"].sort(
            key=lambda item: (
                str(item.get("job_id") or ""),
                str(item.get("lane_id") or ""),
            )
        )
    return {
        "active_leases": active_leases,
        "occupied": occupied,
        "stale_jobs": stale_jobs,
        "dead_jobs": dead_jobs,
        "blocked_jobs": blocked_jobs,
        "resumable_jobs": resumable_jobs,
        "stale_tasks": stale_tasks,
        "diagnostic_issues": list(diagnostic_issues),
        "provider_occupancy_issues": provider_occupancy_issues,
        "ambiguous_providers": sorted(ambiguous_providers),
    }


def _task_reason(code: str, detail: str, *, category: str) -> dict[str, str]:
    return {"code": code, "detail": detail, "category": category}


def _evaluate_queue(
    snapshot: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    *,
    now: float,
    settings: SupervisorSettings,
    previous: Mapping[str, Any] | None,
    cycle_limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    strict_resource_estimates = cycle_limits is not None
    cycle_cost_limit = (
        Decimal(str(cycle_limits["max_cycle_estimated_cost_usd"]))
        if cycle_limits is not None
        else settings.max_cycle_estimated_cost_usd
    )
    cycle_token_limit = (
        int(cycle_limits["max_cycle_estimated_tokens"])
        if cycle_limits is not None
        else None
    )
    occupied = reconciliation["occupied"]
    ambiguous_providers = set(reconciliation.get("ambiguous_providers") or [])
    provider_availability: dict[str, dict[str, Any]] = {}
    available_slots: dict[str, list[str]] = {}
    for provider in PROVIDER_ORDER:
        limit = settings.provider_limits[provider]
        occupied_slots = sorted(occupied[provider]["slots"])
        external_consumers = list(occupied[provider]["external_consumers"])
        for slot in occupied_slots:
            match = _SLOT_RE.match(slot)
            if not match or int(match.group(2)) > limit.capacity:
                raise QueueValidationError(
                    "provider_occupancy_ambiguous",
                    f"observed slot {slot!r} exceeds configured {provider} capacity",
                )
        occupied_count = len(occupied_slots) + len(external_consumers)
        if occupied_count > limit.capacity:
            ambiguous_providers.add(provider)
        all_slots = [f"{provider}:{index}" for index in range(1, limit.capacity + 1)]
        free = [slot for slot in all_slots if slot not in occupied[provider]["slots"]]
        if external_consumers:
            free = free[len(external_consumers) :]
        occupancy_ambiguous = provider in ambiguous_providers
        if not limit.available or occupancy_ambiguous:
            free = []
        available_slots[provider] = free
        if occupancy_ambiguous:
            state = "ambiguous"
        elif not limit.available:
            state = "unavailable"
        elif not free:
            state = "full"
        else:
            state = "available"
        provider_availability[provider] = {
            "capacity": limit.capacity,
            "configured_available": limit.available,
            "available": state == "available",
            "occupancy_state": state,
            "occupied": occupied_count,
            "free": None if occupancy_ambiguous else len(free),
            "occupied_slots": occupied_slots,
            "observed_task_ids": sorted(occupied[provider]["tasks"]),
            "external_consumers": external_consumers,
        }

    olympus_ids = set(snapshot.get("olympus_task_ids") or [])
    tasks_by_id = {str(task["id"]): task for task in snapshot.get("tasks") or []}
    unfinished = [
        task
        for task_id, task in tasks_by_id.items()
        if task_id in olympus_ids and task.get("status") not in COMPLETE_STATUSES
    ]
    rows: list[dict[str, Any]] = []
    for task in unfinished:
        task_id = str(task["id"])
        metadata = task["olympus_metadata"]
        parent_ids = list(task.get("parents") or [])
        unresolved = [
            parent_id
            for parent_id in parent_ids
            if tasks_by_id[parent_id].get("status") not in COMPLETE_STATUSES
        ]
        risk = str(metadata["risk"])
        compatible = list(metadata["providers"])
        provider_headroom = max(
            (len(available_slots[provider]) for provider in compatible),
            default=0,
        )
        reasons: list[dict[str, str]] = []
        if not metadata["enabled"]:
            reasons.append(
                _task_reason("disabled", "task is not enabled", category="policy")
            )
        if task.get("claim_lock"):
            reasons.append(
                _task_reason(
                    "already_leased",
                    f"active claim {task['claim_lock']}",
                    category="lease",
                )
            )
        if unresolved:
            reasons.append(
                _task_reason(
                    "unresolved_dependencies",
                    "waiting on " + ", ".join(sorted(unresolved)),
                    category="dependency",
                )
            )
        authority = metadata["authority"]
        if authority["status"] not in {"active", "approved"}:
            reasons.append(
                _task_reason(
                    "authority_not_current",
                    f"authority status is {authority['status']}",
                    category="authority",
                )
            )
        if not authority["recommendation_allowed"]:
            reasons.append(
                _task_reason(
                    "recommendation_not_authorized",
                    "current authority does not permit supervisor recommendation",
                    category="authority",
                )
            )
        if authority["expires_at"] is not None and authority["expires_at"] <= now:
            reasons.append(
                _task_reason(
                    "authority_expired",
                    "task authority has expired",
                    category="authority",
                )
            )
        approval = metadata["approval"]
        if approval["required"] and approval["status"] not in {
            "approved",
            "not_required",
        }:
            reasons.append(
                _task_reason(
                    "operator_approval_required",
                    f"approval is {approval['status']}",
                    category="approval",
                )
            )
        if RISK_ORDER[risk] > RISK_ORDER[settings.max_risk]:
            reasons.append(
                _task_reason(
                    "risk_exceeds_limit",
                    f"{risk} exceeds configured {settings.max_risk}",
                    category="risk",
                )
            )
        estimated_cost = Decimal(metadata["estimated_cost_usd"])
        estimated_tokens = metadata.get("estimated_tokens")
        if strict_resource_estimates and not metadata["estimated_cost_known"]:
            reasons.append(
                _task_reason(
                    "cycle_cost_unknown",
                    "short-soak mode requires an explicit estimated_cost_usd",
                    category="spending",
                )
            )
        if strict_resource_estimates and estimated_tokens is None:
            reasons.append(
                _task_reason(
                    "cycle_tokens_unknown",
                    "short-soak mode requires explicit estimated_tokens",
                    category="spending",
                )
            )
        if estimated_cost > settings.max_task_estimated_cost_usd:
            reasons.append(
                _task_reason(
                    "task_spending_limit",
                    f"estimated ${estimated_cost} exceeds task limit "
                    f"${settings.max_task_estimated_cost_usd}",
                    category="spending",
                )
            )
        if task.get("status") != "ready":
            reasons.append(
                _task_reason(
                    "kanban_not_ready",
                    f"canonical Kanban status is {task.get('status')}",
                    category="queue",
                )
            )
        age_seconds = max(0.0, now - float(task.get("created_at") or now))
        ranking_key = (
            -int(task.get("priority") or 0),
            len(unresolved),
            RISK_ORDER[risk],
            int(task.get("created_at") or 0),
            -provider_headroom,
            task_id,
        )
        rows.append({
            "task_id": task_id,
            "title": task.get("title"),
            "status": task.get("status"),
            "priority": int(task.get("priority") or 0),
            "dependency_count": len(parent_ids),
            "dependencies": sorted(parent_ids),
            "unresolved_dependencies": sorted(unresolved),
            "risk": risk,
            "age_seconds": round(age_seconds, 3),
            "compatible_providers": compatible,
            "provider_headroom": provider_headroom,
            "estimated_cost_usd": str(estimated_cost),
            "estimated_tokens": estimated_tokens,
            "ranking_key": list(ranking_key),
            "ranking_explanation": (
                "priority desc; unresolved dependencies asc; risk asc; "
                "created_at asc (older first); provider headroom desc; task id"
            ),
            "reasons": reasons,
            "_metadata": metadata,
            "_ranking_key": ranking_key,
        })
    rows.sort(key=lambda item: item["_ranking_key"])

    selected: list[dict[str, Any]] = []
    blocked: list[dict[str, Any]] = []
    pending_decisions: list[dict[str, Any]] = []
    cycle_cost = Decimal("0")
    cycle_tokens = 0
    previous_action_ids = {
        str(item.get("action_id"))
        for item in (previous or {}).get("selected_candidates", [])
        if isinstance(item, dict)
    }
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
        reasons = list(row["reasons"])
        metadata = row["_metadata"]
        if not reasons:
            if len(selected) >= settings.max_selected_candidates:
                reasons.append(
                    _task_reason(
                        "concurrency_limit",
                        "cycle recommendation concurrency limit is full",
                        category="capacity",
                    )
                )
            estimated_cost = Decimal(row["estimated_cost_usd"])
            if not reasons and cycle_cost + estimated_cost > cycle_cost_limit:
                reasons.append(
                    _task_reason(
                        "cycle_spending_limit",
                        f"cycle estimated spending limit ${cycle_cost_limit} "
                        "would be exceeded",
                        category="spending",
                    )
                )
            estimated_tokens = row.get("estimated_tokens")
            if (
                not reasons
                and cycle_token_limit is not None
                and isinstance(estimated_tokens, int)
                and cycle_tokens + estimated_tokens > cycle_token_limit
            ):
                reasons.append(
                    _task_reason(
                        "cycle_token_limit",
                        f"cycle token limit {cycle_token_limit} would be exceeded",
                        category="spending",
                    )
                )
            chosen_provider = None
            chosen_slot = None
            if not reasons:
                choices = sorted(
                    (
                        provider
                        for provider in row["compatible_providers"]
                        if available_slots[provider]
                    ),
                    key=lambda provider: (
                        -len(available_slots[provider]),
                        PROVIDER_ORDER.index(provider),
                    ),
                )
                if choices:
                    chosen_provider = choices[0]
                    chosen_slot = available_slots[chosen_provider].pop(0)
                else:
                    ambiguous = sorted(
                        provider
                        for provider in row["compatible_providers"]
                        if provider in ambiguous_providers
                    )
                    code = (
                        "provider_occupancy_ambiguous"
                        if ambiguous
                        else "provider_unavailable"
                    )
                    detail = (
                        "provider occupancy is ambiguous for " + ", ".join(ambiguous)
                        if ambiguous
                        else "no compatible provider slot is available"
                    )
                    reasons.append(
                        _task_reason(
                            code,
                            detail,
                            category="capacity",
                        )
                    )
            if not reasons and chosen_provider and chosen_slot:
                cycle_cost += estimated_cost
                cycle_tokens += int(estimated_tokens or 0)
                action_payload = {
                    "task_id": row["task_id"],
                    "provider": chosen_provider,
                    "slot": chosen_slot,
                    "task_contract": metadata,
                    "dependencies": row["dependencies"],
                }
                action_id = "proposal:" + _digest(action_payload)
                goal = {
                    "schema_version": GOAL_SCHEMA,
                    "action_id": action_id,
                    "mode": "prepare_only",
                    "launch_authorized": False,
                    "authority_consumed": False,
                    "task_id": row["task_id"],
                    "provider": chosen_provider,
                    "proposed_slot": chosen_slot,
                    "objective": metadata["goal"]["objective"],
                    "bounds": {
                        "max_turns": metadata["goal"]["max_turns"],
                        "timeout_seconds": metadata["goal"]["timeout_seconds"],
                        "estimated_cost_usd": row["estimated_cost_usd"],
                        "estimated_tokens": estimated_tokens,
                        "allowed_paths": metadata["goal"]["allowed_paths"],
                        "forbidden_actions": metadata["goal"]["forbidden_actions"],
                        "deliverables": metadata["goal"]["deliverables"],
                    },
                    "dependencies": list(row["dependencies"]),
                    "queue_snapshot_identity": snapshot["identity"],
                    "prohibitions": [
                        "do not launch without separate Phase B authority",
                        "do not create a worktree",
                        "do not modify a repository",
                        "do not consume an approval",
                        "do not append a ledger",
                        "do not send a live message",
                    ],
                }
                public = {
                    key: value
                    for key, value in row.items()
                    if not key.startswith("_") and key != "reasons"
                }
                public.update({
                    "eligible": True,
                    "selected_provider": chosen_provider,
                    "proposed_slot": chosen_slot,
                    "action_id": action_id,
                    "new_recommendation": action_id not in previous_action_ids,
                    "bounded_goal": goal,
                })
                selected.append(public)
                row["reasons"] = []
                continue

        public_block = {
            key: value
            for key, value in row.items()
            if not key.startswith("_") and key != "reasons"
        }
        public_block.update({"eligible": False, "reasons": reasons})
        blocked.append(public_block)
        decision_reasons = [
            reason
            for reason in reasons
            if reason["category"] in {"approval", "authority"}
        ]
        if decision_reasons:
            kind = (
                "approval"
                if any(reason["category"] == "approval" for reason in decision_reasons)
                else "authority"
            )
            pending_decisions.append({
                "task_id": row["task_id"],
                "kind": kind,
                "decision_id": (
                    metadata["approval"]["decision_id"]
                    if kind == "approval"
                    else metadata["authority"]["authority_id"]
                )
                or None,
                "reason_codes": [reason["code"] for reason in decision_reasons],
                "reason": "; ".join(reason["detail"] for reason in decision_reasons),
            })

    ranked_queue: list[dict[str, Any]] = []
    selected_by_id = {item["task_id"]: item for item in selected}
    blocked_by_id = {item["task_id"]: item for item in blocked}
    for row in rows:
        item = selected_by_id.get(row["task_id"]) or blocked_by_id[row["task_id"]]
        ranked_queue.append({
            key: value for key, value in item.items() if key not in {"bounded_goal"}
        })

    if reconciliation.get("provider_occupancy_issues"):
        status = "blocked"
    elif (
        reconciliation.get("stale_jobs")
        or reconciliation.get("dead_jobs")
        or reconciliation.get("blocked_jobs")
    ):
        status = "blocked"
    elif selected or reconciliation.get("active_leases"):
        status = "working"
    elif not unfinished:
        status = "idle"
    else:
        categories = {
            reason["category"] for item in blocked for reason in item["reasons"]
        }
        if categories and categories <= {"capacity", "spending", "queue", "dependency"}:
            status = "waiting"
        elif categories:
            status = "blocked"
        else:
            status = "idle"

    return {
        "status": status,
        "ranked_queue": ranked_queue,
        "selected_candidates": selected,
        "blocked_candidates": blocked,
        "provider_availability": provider_availability,
        "pending_operator_decisions": pending_decisions,
        "cycle_estimated_cost_usd": str(cycle_cost),
        "cycle_estimated_tokens": cycle_tokens,
        "cycle_limits": {
            "max_estimated_cost_usd": str(cycle_cost_limit),
            "max_estimated_tokens": cycle_token_limit,
            "resource_estimates_required": strict_resource_estimates,
        },
    }


def _next_backoff(
    *,
    evaluation_status: str,
    previous: Mapping[str, Any] | None,
    queue_identity: str,
    settings: SupervisorSettings,
) -> float:
    if evaluation_status == "working":
        return settings.cycle_interval_seconds
    previous_backoff = (previous or {}).get("backoff") or {}
    same_queue = (previous or {}).get("queue_snapshot_identity") == queue_identity
    if same_queue and (previous or {}).get("status") in {"idle", "waiting", "blocked"}:
        base = float(
            previous_backoff.get("current_seconds")
            or settings.idle_backoff_initial_seconds
        )
        return min(
            settings.idle_backoff_max_seconds,
            max(
                settings.idle_backoff_initial_seconds,
                base * settings.idle_backoff_factor,
            ),
        )
    return min(
        settings.idle_backoff_max_seconds,
        settings.idle_backoff_initial_seconds,
    )


def _advance_short_soak(
    value: Mapping[str, Any],
    *,
    now: float,
) -> dict[str, Any]:
    soak = _validate_short_soak(value)
    successful = int(soak["successful_cycles"]) + 1
    target = int(soak["target_cycles"])
    soak.update({
        "status": "completed" if successful == target else "running",
        "successful_cycles": successful,
        "remaining_cycles": target - successful,
        "consecutive_cycle_failures": 0,
        "updated_at": now,
        "updated_at_iso": _iso_utc(now),
    })
    return _validate_short_soak(soak)


def _build_checkpoint(
    *,
    run_id: str,
    snapshot: Mapping[str, Any],
    reconciliation: Mapping[str, Any],
    evaluation: Mapping[str, Any],
    previous: Mapping[str, Any] | None,
    now: float,
    settings: SupervisorSettings,
    short_soak: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    queue_changed = (
        previous is None
        or previous.get("queue_snapshot_identity") != snapshot["identity"]
    )
    generation = int((previous or {}).get("generation") or 0)
    if queue_changed:
        generation += 1
    completed_cycles = int((previous or {}).get("completed_cycles") or 0) + 1
    backoff = _next_backoff(
        evaluation_status=str(evaluation["status"]),
        previous=previous,
        queue_identity=str(snapshot["identity"]),
        settings=settings,
    )
    checkpoint: dict[str, Any] = {
        "schema_version": SUPERVISOR_SCHEMA,
        "run_id": run_id,
        "previous_run_id": (
            previous.get("run_id")
            if previous and previous.get("run_id") != run_id
            else None
        ),
        "generation": generation,
        "queue_snapshot_identity": snapshot["identity"],
        "semantic_queue_identity": snapshot["semantic_identity"],
        "operational_occupancy_identity": snapshot["operational_occupancy_identity"],
        "queue": {
            "authority": "hermes_kanban",
            "board": snapshot["board"],
            "tenant": snapshot["tenant"],
            "db_path": snapshot["db_path"],
            "schema_version": snapshot["schema_version"],
            "user_version": snapshot["user_version"],
            "observed_at": snapshot["observed_at"],
            "file_identity": snapshot["file_identity"],
            "semantic_identity": snapshot["semantic_identity"],
            "operational_occupancy_identity": snapshot[
                "operational_occupancy_identity"
            ],
            "identity_model": snapshot["identity_model"],
        },
        "completed_cycles": completed_cycles,
        "consecutive_cycle_failures": 0,
        "status": evaluation["status"],
        "ranked_queue": evaluation["ranked_queue"],
        "selected_candidates": evaluation["selected_candidates"],
        "blocked_candidates": evaluation["blocked_candidates"],
        "provider_availability": evaluation["provider_availability"],
        "active_leases_observed": reconciliation["active_leases"],
        "stale_jobs": reconciliation["stale_jobs"],
        "dead_jobs": reconciliation["dead_jobs"],
        "blocked_jobs": reconciliation["blocked_jobs"],
        "stale_tasks": reconciliation["stale_tasks"],
        "resumable_jobs": reconciliation["resumable_jobs"],
        "diagnostic_issues": reconciliation["diagnostic_issues"],
        "provider_occupancy_issues": reconciliation["provider_occupancy_issues"],
        "last_heartbeat": now,
        "last_heartbeat_iso": _iso_utc(now),
        "last_successful_reconciliation": now,
        "last_successful_reconciliation_iso": _iso_utc(now),
        "pending_operator_decisions": evaluation["pending_operator_decisions"],
        "cycle_estimated_cost_usd": evaluation["cycle_estimated_cost_usd"],
        "cycle_estimated_tokens": evaluation["cycle_estimated_tokens"],
        "cycle_limits": evaluation["cycle_limits"],
        "backoff": {
            "current_seconds": backoff,
            "maximum_seconds": settings.idle_backoff_max_seconds,
            "next_cycle_not_before": now + backoff,
            "next_cycle_not_before_iso": _iso_utc(now + backoff),
        },
        "restart_checkpoint": {
            "run_id": run_id,
            "generation": generation,
            "queue_snapshot_identity": snapshot["identity"],
            "completed_cycles": completed_cycles,
            "created_at": now,
            "created_at_iso": _iso_utc(now),
        },
    }
    if short_soak is not None:
        checkpoint["short_soak"] = _advance_short_soak(short_soak, now=now)
    if previous and previous.get("last_failure"):
        checkpoint["last_failure"] = previous["last_failure"]
    checkpoint["checkpoint_digest"] = _digest(_checkpoint_payload(checkpoint))
    return checkpoint


def _cycle_budget_limits(
    settings: SupervisorSettings,
    *,
    max_cycle_cost_usd: Any = None,
    max_cycle_tokens: Any = None,
    require_explicit: bool,
) -> dict[str, Any]:
    if require_explicit and (max_cycle_cost_usd is None or max_cycle_tokens is None):
        raise OlympusSupervisorError(
            "invalid_argument",
            "run-once requires explicit --max-cycle-cost-usd and "
            "--max-cycle-tokens values",
        )
    raw_cost = (
        settings.max_cycle_estimated_cost_usd
        if max_cycle_cost_usd is None
        else max_cycle_cost_usd
    )
    if isinstance(raw_cost, bool):
        raise OlympusSupervisorError(
            "invalid_argument",
            "--max-cycle-cost-usd must be a finite non-negative number",
        )
    try:
        cost = Decimal(str(raw_cost))
    except (InvalidOperation, ValueError) as exc:
        raise OlympusSupervisorError(
            "invalid_argument",
            "--max-cycle-cost-usd must be a finite non-negative number",
        ) from exc
    if not cost.is_finite() or cost < 0:
        raise OlympusSupervisorError(
            "invalid_argument",
            "--max-cycle-cost-usd must be a finite non-negative number",
        )
    if cost > settings.max_cycle_estimated_cost_usd:
        raise OlympusSupervisorError(
            "invalid_argument",
            "--max-cycle-cost-usd cannot exceed the configured "
            "max_cycle_estimated_cost_usd",
        )
    tokens = (
        settings.max_cycle_estimated_tokens
        if max_cycle_tokens is None
        else max_cycle_tokens
    )
    if (
        isinstance(tokens, bool)
        or not isinstance(tokens, int)
        or not 0 <= tokens <= _MAX_ESTIMATED_TOKENS
    ):
        raise OlympusSupervisorError(
            "invalid_argument",
            "--max-cycle-tokens must be an integer between 0 and "
            f"{_MAX_ESTIMATED_TOKENS}",
        )
    if tokens > settings.max_cycle_estimated_tokens:
        raise OlympusSupervisorError(
            "invalid_argument",
            "--max-cycle-tokens cannot exceed the configured "
            "max_cycle_estimated_tokens",
        )
    return {
        "max_cycle_estimated_cost_usd": str(cost),
        "max_cycle_estimated_tokens": tokens,
    }


def _short_soak_limits(
    settings: SupervisorSettings,
    *,
    max_cycles: int | None,
    max_cycle_cost_usd: Any = None,
    max_cycle_tokens: Any = None,
) -> dict[str, Any]:
    if (
        isinstance(max_cycles, bool)
        or not isinstance(max_cycles, int)
        or not 1 <= max_cycles <= _MAX_SHORT_SOAK_CYCLES
    ):
        raise OlympusSupervisorError(
            "invalid_argument",
            "--max-cycles is required and must be an integer between 1 and "
            f"{_MAX_SHORT_SOAK_CYCLES}",
        )
    budgets = _cycle_budget_limits(
        settings,
        max_cycle_cost_usd=max_cycle_cost_usd,
        max_cycle_tokens=max_cycle_tokens,
        require_explicit=False,
    )
    return {
        "target_cycles": max_cycles,
        "max_consecutive_cycle_failures": settings.max_consecutive_cycle_failures,
        **budgets,
    }


def _prepare_short_soak(
    previous: Mapping[str, Any] | None,
    *,
    limits: Mapping[str, Any],
    now: float,
) -> dict[str, Any]:
    prior = (previous or {}).get("short_soak")
    if prior is not None:
        soak = _validate_short_soak(prior)
        expected = {
            key: limits[key]
            for key in (
                "target_cycles",
                "max_consecutive_cycle_failures",
                "max_cycle_estimated_cost_usd",
                "max_cycle_estimated_tokens",
            )
        }
        observed = {key: soak.get(key) for key in expected}
        if soak["status"] != "completed":
            if observed != expected:
                raise OlympusSupervisorError(
                    "soak_checkpoint_mismatch",
                    "an unfinished short soak exists with different bounds",
                )
            if soak["status"] == "failure_ceiling_reached":
                raise OlympusSupervisorError(
                    "cycle_failure_ceiling_reached",
                    "the persisted consecutive cycle-failure ceiling is already "
                    "reached; complete one successful run-once cycle after remediation "
                    "before starting another soak",
                )
            return soak
        if observed == expected:
            # Idempotent completion is the crash-safe acknowledgement path: if
            # a process dies after the final checkpoint commit, reissuing the
            # same bounded command must not execute the target cycles again.
            return soak
    value = {
        "schema_version": SHORT_SOAK_SCHEMA,
        "soak_id": "soak_" + uuid.uuid4().hex,
        "status": "running",
        **dict(limits),
        "successful_cycles": 0,
        "remaining_cycles": int(limits["target_cycles"]),
        "consecutive_cycle_failures": 0,
        "started_at": now,
        "started_at_iso": _iso_utc(now),
        "updated_at": now,
        "updated_at_iso": _iso_utc(now),
    }
    return _validate_short_soak(value)


def _failure_checkpoint(
    *,
    previous: Mapping[str, Any] | None,
    short_soak: Mapping[str, Any],
    error: OlympusSupervisorError | Exception,
    run_id: str,
    board: str,
    tenant: str,
    db_path: Path,
    now: float,
    settings: SupervisorSettings,
) -> tuple[dict[str, Any], dict[str, Any]]:
    code = (
        error.code
        if isinstance(error, OlympusSupervisorError)
        else "unexpected_failure"
    )
    detail = error.detail if isinstance(error, OlympusSupervisorError) else str(error)
    soak = _validate_short_soak(short_soak)
    failures = int(soak["consecutive_cycle_failures"]) + 1
    ceiling = int(soak["max_consecutive_cycle_failures"])
    soak.update({
        "status": ("failure_ceiling_reached" if failures >= ceiling else "running"),
        "consecutive_cycle_failures": failures,
        "updated_at": now,
        "updated_at_iso": _iso_utc(now),
    })
    failure = {
        "schema_version": FAILURE_SCHEMA,
        "run_id": run_id,
        "state": "failed",
        "code": code,
        "detail": detail,
        "completed_cycles": int((previous or {}).get("completed_cycles") or 0),
        "consecutive_cycle_failures": failures,
        "max_consecutive_cycle_failures": ceiling,
        "retry_will_continue": failures < ceiling,
        "at": now,
        "at_iso": _iso_utc(now),
    }
    if previous is None:
        semantic_identity = "sha256:" + _digest({
            "board": board,
            "tenant": tenant,
            "state": "queue_not_yet_observed",
        })
        occupancy_identity = "sha256:" + _digest({
            "board": board,
            "tenant": tenant,
            "state": "occupancy_not_yet_observed",
        })
        checkpoint: dict[str, Any] = {
            "schema_version": SUPERVISOR_SCHEMA,
            "run_id": run_id,
            "previous_run_id": None,
            "generation": 1,
            "queue_snapshot_identity": semantic_identity,
            "semantic_queue_identity": semantic_identity,
            "operational_occupancy_identity": occupancy_identity,
            "queue": {
                "authority": "hermes_kanban",
                "board": board,
                "tenant": tenant,
                "db_path": str(db_path),
                "semantic_identity": semantic_identity,
                "operational_occupancy_identity": occupancy_identity,
                "identity_model": {
                    "semantic_queue_identity": "queue not yet observed",
                    "operational_occupancy_snapshot": "occupancy not yet observed",
                },
            },
            "completed_cycles": 0,
            "ranked_queue": [],
            "selected_candidates": [],
            "blocked_candidates": [],
            "provider_availability": {
                provider: {
                    "capacity": settings.provider_limits[provider].capacity,
                    "configured_available": settings.provider_limits[
                        provider
                    ].available,
                    "available": False,
                    "occupancy_state": "ambiguous",
                    "occupied": None,
                    "free": None,
                    "occupied_slots": [],
                    "observed_task_ids": [],
                    "external_consumers": [],
                }
                for provider in PROVIDER_ORDER
            },
            "active_leases_observed": [],
            "stale_jobs": [],
            "dead_jobs": [],
            "blocked_jobs": [],
            "stale_tasks": [],
            "resumable_jobs": [],
            "diagnostic_issues": [{"code": code, "detail": detail}],
            "provider_occupancy_issues": [{"code": code, "detail": detail}],
            "last_successful_reconciliation": 0.0,
            "last_successful_reconciliation_iso": _iso_utc(0.0),
            "pending_operator_decisions": [],
            "cycle_estimated_cost_usd": "0",
            "cycle_estimated_tokens": 0,
            "cycle_limits": {
                "max_estimated_cost_usd": soak["max_cycle_estimated_cost_usd"],
                "max_estimated_tokens": soak["max_cycle_estimated_tokens"],
                "resource_estimates_required": True,
            },
        }
    else:
        checkpoint = json.loads(json.dumps(previous))
        checkpoint["previous_run_id"] = (
            previous.get("run_id")
            if previous.get("run_id") != run_id
            else previous.get("previous_run_id")
        )
        checkpoint["run_id"] = run_id
    checkpoint.update({
        "status": "failed",
        "consecutive_cycle_failures": failures,
        "short_soak": soak,
        "last_failure": failure,
        "last_heartbeat": now,
        "last_heartbeat_iso": _iso_utc(now),
        "backoff": {
            "current_seconds": settings.idle_backoff_initial_seconds,
            "maximum_seconds": settings.idle_backoff_max_seconds,
            "next_cycle_not_before": now + settings.idle_backoff_initial_seconds,
            "next_cycle_not_before_iso": _iso_utc(
                now + settings.idle_backoff_initial_seconds
            ),
        },
    })
    checkpoint["restart_checkpoint"] = {
        "run_id": run_id,
        "generation": checkpoint["generation"],
        "queue_snapshot_identity": checkpoint["queue_snapshot_identity"],
        "completed_cycles": checkpoint["completed_cycles"],
        "created_at": now,
        "created_at_iso": _iso_utc(now),
    }
    checkpoint.pop("checkpoint_digest", None)
    checkpoint["checkpoint_digest"] = _digest(_checkpoint_payload(checkpoint))
    return validate_checkpoint(checkpoint), failure


def mission_control_projection(
    checkpoint: Mapping[str, Any],
) -> dict[str, Any]:
    selected = checkpoint.get("selected_candidates") or []
    availability = checkpoint.get("provider_availability") or {}
    return {
        "schema_version": MISSION_CONTROL_SCHEMA,
        "authoritative": False,
        "read_only": True,
        "queue_authority": {
            "kind": "hermes_kanban",
            "board": (checkpoint.get("queue") or {}).get("board"),
            "queue_snapshot_identity": checkpoint.get("queue_snapshot_identity"),
            "semantic_queue_identity": checkpoint.get(
                "semantic_queue_identity",
                checkpoint.get("queue_snapshot_identity"),
            ),
            "operational_occupancy_identity": checkpoint.get(
                "operational_occupancy_identity"
            ),
        },
        "supervisor": {
            "run_id": checkpoint.get("run_id"),
            "generation": checkpoint.get("generation"),
            "state": checkpoint.get("status"),
            "completed_cycles": checkpoint.get("completed_cycles"),
            "consecutive_cycle_failures": checkpoint.get(
                "consecutive_cycle_failures", 0
            ),
            "short_soak": checkpoint.get("short_soak"),
            "last_heartbeat": checkpoint.get("last_heartbeat"),
            "last_heartbeat_iso": checkpoint.get("last_heartbeat_iso"),
            "source_checkpoint_digest": checkpoint.get("checkpoint_digest"),
        },
        "ranked_queue": checkpoint.get("ranked_queue") or [],
        "selected_next_task": (
            {key: value for key, value in selected[0].items() if key != "bounded_goal"}
            if selected
            else None
        ),
        "blocked_tasks": checkpoint.get("blocked_candidates") or [],
        "provider_utilization": {
            provider: {
                "capacity": data.get("capacity"),
                "occupied": data.get("occupied"),
                "free": data.get("free"),
                "available": data.get("available"),
                "occupancy_state": data.get("occupancy_state"),
                "external_consumers": data.get("external_consumers") or [],
            }
            for provider, data in availability.items()
        },
        "stale_jobs": checkpoint.get("stale_jobs") or [],
        "dead_jobs": checkpoint.get("dead_jobs") or [],
        "blocked_jobs": checkpoint.get("blocked_jobs") or [],
        "provider_occupancy_issues": (
            checkpoint.get("provider_occupancy_issues") or []
        ),
        "pending_approvals": [
            item
            for item in checkpoint.get("pending_operator_decisions") or []
            if item.get("kind") == "approval"
        ],
        "pending_operator_decisions": (
            checkpoint.get("pending_operator_decisions") or []
        ),
    }


def _telegram_text(
    message_type: str,
    checkpoint: Mapping[str, Any] | None,
    *,
    stop: Mapping[str, Any] | None = None,
) -> str:
    cp = checkpoint or {}
    selected = cp.get("selected_candidates") or []
    blocked = cp.get("blocked_candidates") or []
    blocked_jobs = cp.get("blocked_jobs") or []
    stale = (cp.get("stale_jobs") or []) + (cp.get("dead_jobs") or [])
    pending = cp.get("pending_operator_decisions") or []
    approvals = [item for item in pending if item.get("kind") == "approval"]
    board = (cp.get("queue") or {}).get("board") or "olympus"
    if message_type == "supervisor_started":
        return (
            f"Olympus supervisor started in observe-only mode on {board}. "
            "Kanban remains authoritative; nothing will be launched."
        )
    if message_type == "supervisor_healthy":
        return (
            f"Olympus supervisor healthy: state={cp.get('status', 'unknown')}, "
            f"cycle={cp.get('completed_cycles', 0)}, "
            f"queue={str(cp.get('queue_snapshot_identity') or 'unknown')[-12:]}."
        )
    if message_type == "new_recommended_task":
        if selected:
            item = selected[0]
            return (
                f"Olympus recommends {item['task_id']} via "
                f"{item['selected_provider']} ({item['proposed_slot']}). "
                "Draft only; no provider was launched."
            )
        return "Olympus has no executable task recommendation."
    if message_type == "operator_approval_required":
        task_id = approvals[0]["task_id"] if approvals else "an Olympus task"
        return (
            f"Olympus needs operator approval for {task_id}. "
            "No approval was consumed; review the Kanban authority record."
        )
    if message_type == "blocked_state":
        if blocked_jobs:
            job = blocked_jobs[0]
            task_id = job.get("task_id") or job.get("job_id") or "provider job"
            reason = (
                job.get("blocker")
                or job.get("next_expected_action")
                or "provider lane is blocked"
            )
        else:
            task_id = blocked[0]["task_id"] if blocked else "queue"
            reason = (
                blocked[0]["reasons"][0]["detail"]
                if blocked and blocked[0].get("reasons")
                else "no eligible work"
            )
        return f"Olympus is blocked at {task_id}: {reason}."
    if message_type == "stale_provider_job":
        job = stale[0] if stale else {}
        return (
            f"Olympus observed a stale provider job for "
            f"{job.get('task_id') or job.get('job_id') or 'unknown'}: "
            f"{job.get('reason') or job.get('status') or 'heartbeat stale'}."
        )
    if message_type == "emergency_stop":
        return (
            "Olympus emergency stop is active: "
            f"{(stop or {}).get('reason') or 'operator stop requested'}. "
            "Checkpoint preserved; no new cycle will start."
        )
    if message_type == "cycle_failure":
        failure = cp.get("last_failure") or {}
        count = failure.get("consecutive_cycle_failures")
        ceiling = failure.get("max_consecutive_cycle_failures")
        retry_text = (
            f" Consecutive failures: {count}/{ceiling}."
            if count is not None and ceiling is not None
            else ""
        )
        return (
            "Olympus supervisor failed closed "
            f"[{failure.get('code') or 'unknown_failure'}]: "
            f"{failure.get('detail') or 'cycle failed'}."
            f"{retry_text} No provider was launched and no live message was sent."
        )
    if message_type == "daily_summary":
        return (
            f"Olympus daily summary: {len(selected)} recommendation(s), "
            f"{len(blocked)} blocked/waiting task(s), "
            f"{len(blocked_jobs)} blocked job(s), {len(stale)} stale job(s), "
            f"{len(pending)} pending operator decision(s). No live action taken."
        )
    raise ValueError(f"unknown Telegram message type: {message_type}")


def telegram_templates(
    checkpoint: Mapping[str, Any] | None,
    *,
    stop: Mapping[str, Any] | None = None,
) -> dict[str, str]:
    return {
        message_type: _telegram_text(message_type, checkpoint, stop=stop)
        for message_type in (
            "supervisor_started",
            "supervisor_healthy",
            "new_recommended_task",
            "operator_approval_required",
            "blocked_state",
            "stale_provider_job",
            "emergency_stop",
            "cycle_failure",
            "daily_summary",
        )
    }


def _draft(
    message_type: str,
    text: str,
    *,
    signature: Any,
    now: float,
    dedupe_bucket: int | None = None,
) -> dict[str, Any]:
    signature_digest = _digest(signature)
    return {
        "channel": "telegram",
        "delivery_mode": "draft_only",
        "sent": False,
        "type": message_type,
        "text": text,
        "signature": signature_digest,
        "dedupe_key": _digest({
            "type": message_type,
            "signature": signature_digest,
            "repeat_bucket": dedupe_bucket,
        }),
        "created_at": now,
        "created_at_iso": _iso_utc(now),
    }


def _latest_matching_draft(
    outbox: Mapping[str, Any],
    message_type: str,
    signature: str,
) -> Mapping[str, Any] | None:
    for item in reversed(outbox.get("messages") or []):
        if (
            isinstance(item, dict)
            and item.get("type") == message_type
            and item.get("signature") == signature
        ):
            return item
    return None


def _cycle_drafts(
    checkpoint: Mapping[str, Any],
    *,
    outbox: Mapping[str, Any],
    now: float,
    repeat_seconds: float,
) -> list[dict[str, Any]]:
    drafts: list[dict[str, Any]] = []

    def add(
        message_type: str,
        signature_value: Any,
        *,
        repeat: bool = False,
    ) -> None:
        signature = _digest(signature_value)
        prior = _latest_matching_draft(outbox, message_type, signature)
        if prior:
            age = now - float(prior.get("created_at") or now)
            if not repeat or age < repeat_seconds:
                return
        item = _draft(
            message_type,
            _telegram_text(message_type, checkpoint),
            signature=signature_value,
            now=now,
            dedupe_bucket=(int(now // repeat_seconds) if repeat else None),
        )
        drafts.append(item)

    add("supervisor_started", {"run_id": checkpoint.get("run_id")})
    add(
        "supervisor_healthy",
        {
            "state": checkpoint.get("status"),
            "queue_size": len(checkpoint.get("ranked_queue") or []),
            "providers": {
                provider: {
                    "available": value.get("available"),
                    "occupied": value.get("occupied"),
                    "free": value.get("free"),
                }
                for provider, value in (
                    checkpoint.get("provider_availability") or {}
                ).items()
            },
        },
        repeat=True,
    )
    selected = checkpoint.get("selected_candidates") or []
    if selected:
        add(
            "new_recommended_task",
            {"action_id": selected[0].get("action_id")},
        )
    pending = checkpoint.get("pending_operator_decisions") or []
    approvals = [item for item in pending if item.get("kind") == "approval"]
    if approvals:
        add("operator_approval_required", approvals)
    if checkpoint.get("status") == "blocked":
        add(
            "blocked_state",
            {
                "tasks": [
                    {
                        "task_id": item.get("task_id"),
                        "reasons": item.get("reasons"),
                    }
                    for item in checkpoint.get("blocked_candidates") or []
                ],
                "jobs": [
                    {
                        "job_id": item.get("job_id"),
                        "lane_id": item.get("lane_id"),
                        "task_id": item.get("task_id"),
                        "provider": item.get("provider"),
                        "blocker": item.get("blocker"),
                        "next_expected_action": item.get("next_expected_action"),
                    }
                    for item in checkpoint.get("blocked_jobs") or []
                ],
            },
        )
    stale = (checkpoint.get("stale_jobs") or []) + (checkpoint.get("dead_jobs") or [])
    if stale:
        add(
            "stale_provider_job",
            [
                {
                    key: item.get(key)
                    for key in (
                        "source",
                        "task_id",
                        "job_id",
                        "lane_id",
                        "provider",
                        "slot",
                        "status",
                        "reason",
                        "claim_expired",
                    )
                }
                for item in stale
            ],
        )
    day = _iso_utc(now)[:10]
    add("daily_summary", {"date": day})
    return drafts


class OlympusSupervisor:
    """One authoritative, observe-only Olympus recommendation loop."""

    def __init__(
        self,
        settings: SupervisorSettings,
        *,
        state_root: str | Path | None = None,
        db_path: str | Path | None = None,
        clock: Callable[[], float] = time.time,
        sleeper: Callable[[float], None] = time.sleep,
        diagnostics_provider: Callable[..., dict[str, Any]] = diagnostics_snapshot,
        identity_probe: Callable[
            [int | None], dict[str, Any]
        ] = capture_process_identity,
        identity_status: Callable[
            [Mapping[str, Any] | None], str
        ] = process_identity_status,
        stage_hook: Callable[[str], None] | None = None,
        crash_injector: Callable[[str, Path], None] | None = None,
    ):
        self.settings = settings
        self.clock = clock
        self.sleeper = sleeper
        self.diagnostics_provider = diagnostics_provider
        self.identity_probe = identity_probe
        self.identity_status = identity_status
        self.stage_hook = stage_hook
        self.run_id = "sup_" + uuid.uuid4().hex
        self.store = AtomicStateStore(
            state_root,
            clock=clock,
            crash_injector=crash_injector,
        )
        self.reader = KanbanQueueReader(
            board=settings.board,
            tenant=settings.tenant,
            db_path=db_path,
        )

    def _stage(self, name: str) -> None:
        if self.stage_hook is not None:
            self.stage_hook(name)
        stop = self.store.stop_reason()
        if stop is not None:
            raise StopRequested(
                "supervisor_stopped",
                str(stop.get("reason") or "global stop control is active"),
            )

    def _lease(self) -> SupervisorLease:
        return SupervisorLease(
            self.store,
            run_id=self.run_id,
            stale_after_seconds=self.settings.stale_supervisor_seconds,
            clock=self.clock,
            identity_probe=self.identity_probe,
            identity_status=self.identity_status,
        )

    def _record_heartbeat(
        self,
        state: str,
        *,
        completed_cycles: int,
        detail: str = "",
    ) -> dict[str, Any]:
        now = self.clock()
        value = {
            "schema_version": HEARTBEAT_SCHEMA,
            "run_id": self.run_id,
            "state": state,
            "completed_cycles": completed_cycles,
            "detail": detail,
            "at": now,
            "at_iso": _iso_utc(now),
        }
        self.store.write_json(self.store.heartbeat_path, value)
        return value

    def _record_failure(
        self,
        error: OlympusSupervisorError | Exception,
        *,
        completed_cycles: int,
        failure_record: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = self.clock()
        code = (
            error.code
            if isinstance(error, OlympusSupervisorError)
            else "unexpected_failure"
        )
        detail = (
            error.detail if isinstance(error, OlympusSupervisorError) else str(error)
        )
        failure = (
            dict(failure_record)
            if failure_record is not None
            else {
                "schema_version": FAILURE_SCHEMA,
                "run_id": self.run_id,
                "state": "failed",
                "code": code,
                "detail": detail,
                "completed_cycles": completed_cycles,
                "at": now,
                "at_iso": _iso_utc(now),
            }
        )
        self.store.write_json(self.store.failure_path, failure)
        try:
            count = failure.get("consecutive_cycle_failures")
            ceiling = failure.get("max_consecutive_cycle_failures")
            retry_text = (
                f" Consecutive failures: {count}/{ceiling}."
                if count is not None and ceiling is not None
                else ""
            )
            self.store.append_drafts([
                _draft(
                    "cycle_failure",
                    (
                        f"Olympus supervisor failed closed [{code}]: {detail}."
                        f"{retry_text} No provider was launched and no live "
                        "Telegram message was sent."
                    ),
                    signature={
                        "code": code,
                        "detail": detail,
                        "consecutive_cycle_failures": count,
                        "max_consecutive_cycle_failures": ceiling,
                    },
                    now=float(failure.get("at") or now),
                )
            ])
        except Exception:
            # The durable failure record is authoritative. A broken draft sink
            # must not hide the original cycle failure or trigger an unbounded
            # retry loop.
            pass
        self._record_heartbeat(
            "failed",
            completed_cycles=completed_cycles,
            detail=f"{code}: {detail}",
        )
        return failure

    def _cycle(
        self,
        lease: SupervisorLease,
        *,
        short_soak: Mapping[str, Any] | None = None,
        cycle_limits: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        previous = self.store.load_checkpoint()
        completed_before = int((previous or {}).get("completed_cycles") or 0)
        self._stage("before_snapshot")
        lease.refresh("working", completed_cycles=completed_before)
        self._record_heartbeat("working", completed_cycles=completed_before)
        now = self.clock()
        first = self.reader.load(now=now)
        self._stage("after_snapshot")
        reconciliation = _diagnostic_reconciliation(
            first,
            now=now,
            settings=self.settings,
            diagnostics_provider=self.diagnostics_provider,
        )
        self._stage("after_reconciliation")
        evaluation = _evaluate_queue(
            first,
            reconciliation,
            now=now,
            settings=self.settings,
            previous=previous,
            cycle_limits=short_soak if short_soak is not None else cycle_limits,
        )
        self._stage("after_selection")
        second = self.reader.load(now=self.clock())
        if second["identity"] != first["identity"]:
            raise QueueDriftError(
                "queue_drift",
                "Kanban queue identity changed during the supervisor cycle",
            )
        checkpoint = _build_checkpoint(
            run_id=self.run_id,
            snapshot=first,
            reconciliation=reconciliation,
            evaluation=evaluation,
            previous=previous,
            now=self.clock(),
            settings=self.settings,
            short_soak=short_soak,
        )
        self._stage("before_checkpoint")

        outbox = self.store.load_outbox()
        drafts = _cycle_drafts(
            checkpoint,
            outbox=outbox,
            now=self.clock(),
            repeat_seconds=self.settings.notification_repeat_seconds,
        )
        self._stage("before_commit")
        final = self.reader.load(now=self.clock())
        if final["identity"] != first["identity"]:
            raise QueueDriftError(
                "queue_drift",
                "Kanban queue identity changed before checkpoint commit",
            )
        # Checkpoint is the commit marker. Derived sidecars are written only
        # after it is durable; if a later sidecar write fails, the next cycle
        # reconstructs it from this checkpoint and outbox dedupe identities.
        self.store.write_checkpoint(checkpoint)
        self.store.append_drafts(drafts)
        self.store.write_json(
            self.store.goals_path,
            {
                "schema_version": GOAL_SET_SCHEMA,
                "mode": "prepare_only",
                "queue_snapshot_identity": checkpoint["queue_snapshot_identity"],
                "source_checkpoint_digest": checkpoint["checkpoint_digest"],
                "goals": [
                    item["bounded_goal"] for item in checkpoint["selected_candidates"]
                ],
            },
        )
        self.store.write_json(
            self.store.mission_control_path,
            mission_control_projection(checkpoint),
        )
        self._record_heartbeat(
            str(checkpoint["status"]),
            completed_cycles=int(checkpoint["completed_cycles"]),
        )
        lease.refresh(
            str(checkpoint["status"]),
            completed_cycles=int(checkpoint["completed_cycles"]),
        )
        return checkpoint

    def run_once(
        self,
        *,
        max_cycle_cost_usd: Any = None,
        max_cycle_tokens: Any = None,
    ) -> dict[str, Any]:
        cycle_limits = _cycle_budget_limits(
            self.settings,
            max_cycle_cost_usd=max_cycle_cost_usd,
            max_cycle_tokens=max_cycle_tokens,
            require_explicit=True,
        )
        self._stage("before_cycle")
        with self._lease() as lease:
            try:
                return self._cycle(lease, cycle_limits=cycle_limits)
            except StopRequested:
                previous = self.store.load_checkpoint()
                self._record_heartbeat(
                    "stopped",
                    completed_cycles=int((previous or {}).get("completed_cycles") or 0),
                    detail="global stop control became active",
                )
                raise
            except Exception as exc:
                previous = self.store.load_checkpoint()
                self._record_failure(
                    exc,
                    completed_cycles=int((previous or {}).get("completed_cycles") or 0),
                )
                raise

    def _wait_responsive(
        self,
        seconds: float,
        *,
        lease: SupervisorLease,
        completed_cycles: int,
        state: str,
    ) -> None:
        deadline = self.clock() + max(0.0, seconds)
        token = self.reader.change_token()
        last_heartbeat = self.clock()
        while self.clock() < deadline:
            stop = self.store.stop_reason()
            if stop is not None:
                raise StopRequested(
                    "supervisor_stopped",
                    str(stop.get("reason") or "global stop control is active"),
                )
            if self.reader.change_token() != token:
                return
            remaining = deadline - self.clock()
            self.sleeper(min(self.settings.stop_poll_seconds, remaining))
            if (
                self.clock() - last_heartbeat
                >= self.settings.heartbeat_interval_seconds
            ):
                self._record_heartbeat(
                    state, completed_cycles=completed_cycles, detail="backoff"
                )
                lease.refresh(state, completed_cycles=completed_cycles)
                last_heartbeat = self.clock()

    def run_forever(
        self,
        *,
        max_cycles: int | None = None,
        max_cycle_cost_usd: Any = None,
        max_cycle_tokens: Any = None,
    ) -> int:
        limits = _short_soak_limits(
            self.settings,
            max_cycles=max_cycles,
            max_cycle_cost_usd=max_cycle_cost_usd,
            max_cycle_tokens=max_cycle_tokens,
        )
        short_soak = _prepare_short_soak(
            self.store.load_checkpoint(),
            limits=limits,
            now=self.clock(),
        )
        if short_soak["status"] == "completed":
            return int(short_soak["successful_cycles"])
        try:
            self._stage("before_cycle")
        except StopRequested:
            previous = self.store.load_checkpoint()
            self._record_heartbeat(
                "stopped",
                completed_cycles=int((previous or {}).get("completed_cycles") or 0),
                detail="global stop control is active",
            )
            return int(short_soak["successful_cycles"])
        with self._lease() as lease:
            while int(short_soak["successful_cycles"]) < int(
                short_soak["target_cycles"]
            ):
                try:
                    checkpoint = self._cycle(lease, short_soak=short_soak)
                    short_soak = _validate_short_soak(checkpoint["short_soak"])
                    if short_soak["status"] == "completed":
                        break
                    self._wait_responsive(
                        float(checkpoint["backoff"]["current_seconds"]),
                        lease=lease,
                        completed_cycles=int(checkpoint["completed_cycles"]),
                        state=str(checkpoint["status"]),
                    )
                except StopRequested:
                    previous = self.store.load_checkpoint()
                    self._record_heartbeat(
                        "stopped",
                        completed_cycles=int(
                            (previous or {}).get("completed_cycles") or 0
                        ),
                        detail="global stop control is active",
                    )
                    return int(short_soak["successful_cycles"])
                except DuplicateSupervisorError:
                    raise
                except Exception as exc:
                    previous = self.store.load_checkpoint()
                    completed = int((previous or {}).get("completed_cycles") or 0)
                    checkpoint, failure = _failure_checkpoint(
                        previous=previous,
                        short_soak=short_soak,
                        error=exc,
                        run_id=self.run_id,
                        board=self.settings.board,
                        tenant=self.settings.tenant,
                        db_path=self.reader.db_path,
                        now=self.clock(),
                        settings=self.settings,
                    )
                    self.store.write_checkpoint(checkpoint)
                    short_soak = _validate_short_soak(checkpoint["short_soak"])
                    self._record_failure(
                        exc,
                        completed_cycles=completed,
                        failure_record=failure,
                    )
                    lease.refresh("failed", completed_cycles=completed)
                    if short_soak["status"] == "failure_ceiling_reached":
                        raise OlympusSupervisorError(
                            "cycle_failure_ceiling_reached",
                            f"{failure['code']}: {failure['detail']} "
                            f"({failure['consecutive_cycle_failures']}/"
                            f"{failure['max_consecutive_cycle_failures']} "
                            "consecutive cycle failures)",
                        ) from exc
                    try:
                        self._wait_responsive(
                            self.settings.idle_backoff_initial_seconds,
                            lease=lease,
                            completed_cycles=completed,
                            state="failed",
                        )
                    except StopRequested:
                        self._record_heartbeat(
                            "stopped",
                            completed_cycles=completed,
                            detail="global stop control is active",
                        )
                        return int(short_soak["successful_cycles"])
        return int(short_soak["successful_cycles"])

    def read_queue(self) -> dict[str, Any]:
        """Fresh read-only queue/evaluation without checkpoint or lease writes."""
        now = self.clock()
        snapshot = self.reader.load(now=now)
        reconciliation = _diagnostic_reconciliation(
            snapshot,
            now=now,
            settings=self.settings,
            diagnostics_provider=self.diagnostics_provider,
        )
        evaluation = _evaluate_queue(
            snapshot,
            reconciliation,
            now=now,
            settings=self.settings,
            previous=self.store.load_checkpoint(),
        )
        return {
            "queue": {
                "authority": "hermes_kanban",
                "board": snapshot["board"],
                "identity": snapshot["identity"],
                "semantic_identity": snapshot["semantic_identity"],
                "operational_occupancy_identity": snapshot[
                    "operational_occupancy_identity"
                ],
                "identity_model": snapshot["identity_model"],
                "observed_at": snapshot["observed_at"],
            },
            "status": evaluation["status"],
            "ranked_queue": evaluation["ranked_queue"],
            "selected_candidates": evaluation["selected_candidates"],
            "blocked_candidates": evaluation["blocked_candidates"],
            "provider_availability": evaluation["provider_availability"],
            "reconciliation": {
                key: reconciliation[key]
                for key in (
                    "active_leases",
                    "stale_jobs",
                    "dead_jobs",
                    "blocked_jobs",
                    "resumable_jobs",
                    "stale_tasks",
                    "diagnostic_issues",
                    "provider_occupancy_issues",
                )
            },
        }

    def render_mission_control(self) -> dict[str, Any]:
        checkpoint = self.store.load_checkpoint()
        if checkpoint is None:
            raise OlympusSupervisorError(
                "missing_checkpoint",
                "run one successful supervisor cycle before rendering",
            )
        projection = mission_control_projection(checkpoint)
        self.store.write_json(self.store.mission_control_path, projection)
        return projection

    def telegram_preview(self) -> dict[str, Any]:
        checkpoint = self.store.load_checkpoint()
        stop = self.store.stop_reason()
        return {
            "delivery_mode": "draft_only",
            "live_send_authorized": False,
            "templates": telegram_templates(checkpoint, stop=stop),
            "outbox": self.store.load_outbox(),
        }

    def health(self) -> dict[str, Any]:
        now = self.clock()
        stop = self.store.stop_reason()
        checkpoint = self.store.load_checkpoint()
        heartbeat = self.store.read_json(self.store.heartbeat_path)
        failure = self.store.read_json(self.store.failure_path)
        lease = self.store.read_json(self.store.lease_path)
        if failure is not None and (
            not isinstance(failure, dict)
            or failure.get("schema_version") != FAILURE_SCHEMA
            or isinstance(failure.get("at"), bool)
            or not isinstance(failure.get("at"), (int, float))
        ):
            raise OlympusSupervisorError(
                "malformed_state",
                f"{self.store.failure_path.name} has invalid schema",
            )
        if stop is not None:
            return {
                "healthy": True,
                "state": "stopped",
                "reason": stop.get("reason"),
                "checkpoint_preserved": checkpoint is not None,
            }
        if (
            not isinstance(heartbeat, dict)
            or heartbeat.get("schema_version") != HEARTBEAT_SCHEMA
            or heartbeat.get("state")
            not in {
                "working",
                "idle",
                "waiting",
                "blocked",
                "stopped",
                "failed",
            }
            or isinstance(heartbeat.get("at"), bool)
            or not isinstance(heartbeat.get("at"), (int, float))
        ):
            return {
                "healthy": False,
                "state": "failed",
                "reason": "missing or malformed supervisor heartbeat",
            }
        age = max(0.0, now - float(heartbeat.get("at") or 0))
        if age >= self.settings.stale_supervisor_seconds:
            return {
                "healthy": False,
                "state": "failed",
                "reason": "stale supervisor heartbeat",
                "heartbeat_age_seconds": round(age, 3),
            }
        if heartbeat.get("state") == "failed":
            return {
                "healthy": False,
                "state": "failed",
                "reason": (
                    failure.get("detail")
                    if isinstance(failure, dict)
                    else heartbeat.get("detail") or "supervisor cycle failed"
                ),
                "code": (failure.get("code") if isinstance(failure, dict) else None),
                "consecutive_cycle_failures": (
                    failure.get("consecutive_cycle_failures")
                    if isinstance(failure, dict)
                    else None
                ),
                "max_consecutive_cycle_failures": (
                    failure.get("max_consecutive_cycle_failures")
                    if isinstance(failure, dict)
                    else None
                ),
                "heartbeat_age_seconds": round(age, 3),
            }
        if isinstance(failure, dict) and float(failure.get("at") or 0) > float(
            (checkpoint or {}).get("last_heartbeat") or 0
        ):
            return {
                "healthy": False,
                "state": "failed",
                "reason": failure.get("detail"),
                "code": failure.get("code"),
                "heartbeat_age_seconds": round(age, 3),
            }
        lease_record = _validate_lease_record(lease)
        lease_state = None
        if lease_record is not None and lease_record.get("status") == "active":
            lease_state = self.identity_status(lease_record.get("process"))
        return {
            "healthy": checkpoint is not None,
            "state": heartbeat.get("state"),
            "heartbeat_age_seconds": round(age, 3),
            "run_id": heartbeat.get("run_id"),
            "completed_cycles": heartbeat.get("completed_cycles"),
            "consecutive_cycle_failures": (
                checkpoint.get("consecutive_cycle_failures", 0) if checkpoint else 0
            ),
            "short_soak": checkpoint.get("short_soak") if checkpoint else None,
            "queue_snapshot_identity": (
                checkpoint.get("queue_snapshot_identity") if checkpoint else None
            ),
            "lease_process_state": lease_state,
            "checkpoint_digest": (
                checkpoint.get("checkpoint_digest") if checkpoint else None
            ),
        }

    def request_stop(self, reason: str) -> dict[str, Any]:
        stop = self.store.request_stop(reason)
        now = self.clock()
        result = dict(stop)
        try:
            draft = _draft(
                "emergency_stop",
                _telegram_text(
                    "emergency_stop",
                    self.store.load_checkpoint(),
                    stop=stop,
                ),
                signature={
                    "requested_at": stop.get("requested_at"),
                    "reason": stop.get("reason"),
                },
                now=now,
            )
            self.store.append_drafts([draft])
            result["draft_prepared"] = True
        except Exception as exc:
            # The emergency control is authoritative even if a projection is
            # malformed. Report the draft failure without undoing the stop.
            result["draft_prepared"] = False
            result["draft_error"] = {
                "code": (
                    exc.code
                    if isinstance(exc, OlympusSupervisorError)
                    else "draft_write_failed"
                ),
                "detail": (
                    exc.detail if isinstance(exc, OlympusSupervisorError) else str(exc)
                ),
            }
        return result

    def resume(self) -> dict[str, Any]:
        if self.store.stop_reason() is None:
            return {
                "resumed": False,
                "cleared_stop": None,
                "checkpoint_preserved": self.store.checkpoint_path.exists(),
                "cycle_started": False,
            }
        handle = self.store.lock_path.open("a+")
        locked = False
        try:
            try:
                _lock_nonblocking(handle)
                locked = True
            except (BlockingIOError, OSError) as exc:
                raise DuplicateSupervisorError(
                    "resume_refused",
                    "the supervisor process lock is still active",
                ) from exc
            lease = _validate_lease_record(self.store.read_json(self.store.lease_path))
            if lease is not None and lease.get("status") == "active":
                age = max(
                    0.0,
                    self.clock() - float(lease.get("heartbeat_at") or 0),
                )
                process_state = self.identity_status(lease.get("process"))
                if age < self.settings.stale_supervisor_seconds or process_state in {
                    "alive",
                    "unknown",
                }:
                    raise DuplicateSupervisorError(
                        "resume_refused",
                        "the prior supervisor may still own the lease",
                    )
            cleared = self.store.clear_stop()
        finally:
            if locked:
                _unlock(handle)
            handle.close()
        return {
            "resumed": cleared is not None,
            "cleared_stop": cleared,
            "checkpoint_preserved": self.store.checkpoint_path.exists(),
            "cycle_started": False,
        }


def _print_human(action: str, value: Mapping[str, Any]) -> None:
    if action == "run-once":
        selected = value.get("selected_candidates") or []
        print(
            "Olympus supervisor cycle complete "
            f"(state={value.get('status')}, generation={value.get('generation')}, "
            f"cycle={value.get('completed_cycles')})."
        )
        if selected:
            item = selected[0]
            print(
                f"Next: {item['task_id']} via {item['selected_provider']} "
                f"({item['proposed_slot']}); prepared only, not launched."
            )
        else:
            print("Next: none; the supervisor will idle/back off safely.")
        return
    if action == "run":
        print(
            f"Olympus supervisor exited after {value.get('cycles', 0)} cycle(s); "
            "no provider was launched."
        )
        return
    if action == "queue":
        print(
            f"Olympus Kanban queue {value['queue']['board']} "
            f"(state={value.get('status')}, "
            f"tasks={len(value.get('ranked_queue') or [])})."
        )
        for item in value.get("ranked_queue") or []:
            if item.get("eligible"):
                detail = (
                    f"eligible via {item.get('selected_provider')} "
                    f"({item.get('proposed_slot')})"
                )
            else:
                reasons = item.get("reasons") or []
                detail = reasons[0]["detail"] if reasons else "not eligible"
            print(f"  {item.get('rank')}. {item['task_id']}: {detail}")
        return
    if action == "explain-next":
        if value.get("eligible"):
            print(
                f"Next: {value['task_id']} via {value['selected_provider']} "
                f"({value['proposed_slot']})."
            )
            print(value.get("ranking_explanation") or "")
            print("Prepared only; no task was claimed and no provider was launched.")
        else:
            print("No executable Olympus task.")
            for item in value.get("blocked") or []:
                reason = (item.get("reasons") or [{}])[0].get("detail", "not eligible")
                print(f"  {item['task_id']}: {reason}")
        return
    if action == "health":
        print(
            f"Olympus supervisor health: "
            f"{'healthy' if value.get('healthy') else 'unhealthy'} "
            f"(state={value.get('state')})."
        )
        if value.get("reason"):
            print(f"Reason: {value['reason']}")
        return
    if action == "stop":
        print(
            f"Olympus supervisor stopped: {value.get('reason')}. "
            "The current checkpoint is preserved."
        )
        if value.get("draft_prepared") is False:
            error = value.get("draft_error") or {}
            print(
                "Telegram emergency-stop draft was not prepared: "
                f"{error.get('code', 'unknown_error')}: "
                f"{error.get('detail', 'unknown error')}."
            )
        return
    if action == "resume":
        print(
            "Olympus supervisor stop control "
            f"{'cleared' if value.get('resumed') else 'was not present'}; "
            "no cycle was started."
        )
        return
    if action == "render-mission-control":
        print(
            "Mission Control read-only projection rendered from checkpoint "
            f"{value.get('supervisor', {}).get('source_checkpoint_digest')}."
        )
        return
    if action == "telegram-preview":
        print("Telegram delivery mode: draft_only (live send is not authorized).")
        for message_type, text in (value.get("templates") or {}).items():
            print(f"  {message_type}: {text}")
        return
    if action == "checkpoint":
        print(
            f"Checkpoint {value.get('checkpoint_digest')} "
            f"(state={value.get('status')}, cycle={value.get('completed_cycles')})."
        )
        return
    if action == "inspect":
        checkpoint = value.get("checkpoint") or {}
        health = value.get("health") or {}
        print(
            f"Olympus supervisor inspect: state={health.get('state')}, "
            f"cycle={checkpoint.get('completed_cycles', 0)}, "
            f"stop={'active' if value.get('stop') else 'clear'}."
        )
        if value.get("failure"):
            print(
                f"Last failure: {value['failure'].get('code')}: "
                f"{value['failure'].get('detail')}"
            )
        return
    print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))


def olympus_supervisor_command(
    args: Any,
    *,
    supervisor: OlympusSupervisor | None = None,
    config: Mapping[str, Any] | None = None,
) -> int:
    """Dispatch one CLI action without widening Phase A authority."""

    import sys

    action = str(getattr(args, "olympus_supervisor_action", None) or "inspect")
    emit_json = bool(getattr(args, "json", False))
    try:
        selected = supervisor
        if selected is None:
            if config is None:
                from hermes_cli.config import load_config

                config = load_config()
            settings = SupervisorSettings.from_mapping(
                (config or {}).get("olympus_supervisor", {}),
                board=getattr(args, "board", None),
            )
            selected = OlympusSupervisor(settings)

        if action == "run":
            max_cycles = getattr(args, "max_cycles", None)
            cycles = selected.run_forever(
                max_cycles=max_cycles,
                max_cycle_cost_usd=getattr(args, "max_cycle_cost_usd", None),
                max_cycle_tokens=getattr(args, "max_cycle_tokens", None),
            )
            checkpoint = selected.store.load_checkpoint()
            value: dict[str, Any] = {
                "cycles": cycles,
                "stopped": selected.store.stop_reason() is not None,
                "checkpoint": checkpoint,
                "short_soak": (checkpoint or {}).get("short_soak"),
            }
        elif action == "run-once":
            value = selected.run_once(
                max_cycle_cost_usd=getattr(args, "max_cycle_cost_usd", None),
                max_cycle_tokens=getattr(args, "max_cycle_tokens", None),
            )
        elif action == "inspect":
            value = {
                "checkpoint": selected.store.load_checkpoint(),
                "heartbeat": selected.store.read_json(selected.store.heartbeat_path),
                "lease": selected.store.read_json(selected.store.lease_path),
                "stop": selected.store.stop_reason(),
                "failure": selected.store.read_json(selected.store.failure_path),
                "health": selected.health(),
            }
        elif action == "queue":
            value = selected.read_queue()
        elif action == "explain-next":
            queue = selected.read_queue()
            candidates = queue.get("selected_candidates") or []
            value = (
                dict(candidates[0])
                if candidates
                else {
                    "eligible": False,
                    "blocked": queue.get("blocked_candidates") or [],
                    "queue_snapshot_identity": queue["queue"]["identity"],
                }
            )
        elif action == "checkpoint":
            checkpoint = selected.store.load_checkpoint()
            if checkpoint is None:
                raise OlympusSupervisorError(
                    "missing_checkpoint", "no supervisor checkpoint exists"
                )
            value = checkpoint
        elif action == "health":
            value = selected.health()
        elif action == "stop":
            value = selected.request_stop(getattr(args, "reason", ""))
        elif action == "resume":
            value = selected.resume()
        elif action == "render-mission-control":
            value = selected.render_mission_control()
        elif action == "telegram-preview":
            value = selected.telegram_preview()
        else:
            raise OlympusSupervisorError(
                "invalid_argument", f"unknown supervisor action {action!r}"
            )

        if emit_json:
            print(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False))
        else:
            _print_human(action, value)
        if action == "health" and not value.get("healthy"):
            return 1
        return 0
    except OlympusSupervisorError as exc:
        payload = {
            "ok": False,
            "status": "BLOCKED",
            "code": exc.code,
            "error": exc.detail,
        }
        if emit_json:
            print(json.dumps(payload, indent=2, sort_keys=True))
        else:
            print(
                f"Olympus supervisor BLOCKED [{exc.code}]: {exc.detail}",
                file=sys.stderr,
            )
        return 2
