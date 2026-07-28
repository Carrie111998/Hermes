"""Bounded, persistent governance for Telegram trading-research loops.

The Kanban board remains the source of truth for work.  This module stores only
loop governance, evidence manifests, approvals, and restart checkpoints under
``<HERMES_HOME>/loops/trading-research``.  It never submits or simulates an
exchange order and deliberately has no exchange integration.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import hmac
import json
import math
import os
import re
import tempfile
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional


SCHEMA_VERSION = 1
KANBAN_BOARD = "trading-research"
LOOP_TYPE = "TradingResearchLoop"
RECOVERY_ATTESTATION_TTL_SECONDS = 300
TOPIC_BY_ROLE = {
    "orchestrator": 1,
    "researcher": 10,
    "developer": 11,
    "backtester": 13,
    "reviewer": 16,
    "benchmark": 68,
    "risk": 106,
    "kronos": 1490,
}
DEFAULT_LIMITS = {
    "max_iterations": 5,
    "max_experiments": 20,
    "max_consecutive_failures": 2,
    "max_wall_clock_hours": 24,
    "max_cost_budget": 10.0,
    "approval_ttl_seconds": 3600,
}
REQUIRED_LIMITS = frozenset(DEFAULT_LIMITS)
TERMINAL_STATUSES = frozenset({"stopped"})
WORK_STATUSES = frozenset({"open", "researching", "developing", "backtesting", "reviewing"})
TRANSITIONS = {
    "open": {"researching", "blocked", "stopped"},
    "researching": {"developing", "blocked", "stopped"},
    "developing": {"backtesting", "blocked", "stopped"},
    "backtesting": {"reviewing", "blocked", "stopped"},
    "reviewing": {"researching", "awaiting_approval", "blocked", "stopped"},
    "awaiting_approval": {"promoting", "blocked", "stopped"},
    "promoting": {"stopped", "blocked"},
    "blocked": {"researching", "stopped"},
    "stopped": set(),
}
RESERVED_TRANSITION_TARGETS = frozenset({"awaiting_approval", "promoting"})

# Deterministic last line of defence. This operates on requested actions, not
# source text or prose reports, and complements (rather than replaces) tool and
# prompt policy. Research/backtest operations are intentionally absent.
_FORBIDDEN_ACTIONS = (
    re.compile(r"\b(place|submit|execute|send|cancel)\s+(an?\s+)?(market\s+|limit\s+|stop\s+)?orders?\b", re.I),
    re.compile(r"\b(buy|sell|swap)\b.{0,80}\b(real|live|actual)[_\s-]+(funds?|money|capital|crypto|usdc|btc|eth)\b", re.I),
    re.compile(r"\b(exchange|broker)\s+api\s+key\b", re.I),
    re.compile(r"\btransfer\s+(assets?|funds?|money|crypto|usdc|btc|eth)\b", re.I),
    re.compile(r"\bwithdraw(?:al)?\b", re.I),
    re.compile(r"\btrade\s+(live|real|on[- ]chain)\b", re.I),
)


class TradingResearchLoopError(RuntimeError):
    """Base error for governed loop operations."""


class InvalidTransition(TradingResearchLoopError):
    pass


class StateConflict(TradingResearchLoopError):
    pass


class CircuitBreakerOpen(TradingResearchLoopError):
    pass


class DuplicateExperiment(TradingResearchLoopError):
    pass


class PromotionBlocked(TradingResearchLoopError):
    pass


class SafetyViolation(TradingResearchLoopError):
    pass


def _now(value: Optional[int] = None) -> int:
    return int(time.time() if value is None else value)


def _finite_number(value: Any, name: str, *, minimum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be a finite number")
    if minimum is not None and number < minimum:
        raise ValueError(f"{name} must be >= {minimum}")
    return number


def default_root() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    return home / "loops" / "trading-research"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _validate_loop_id(value: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", value):
        raise argparse.ArgumentTypeError("loop_id must be a safe path component")
    return value


def _normalize_action(action: str) -> str:
    normalized = unicodedata.normalize("NFKC", str(action))
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", normalized).casefold()
    normalized = "".join(
        " " if unicodedata.category(character)[0] in {"P", "S", "Z"} else character
        for character in normalized
    )
    return " ".join(normalized.split())


def _attestation_fingerprint(attestation: Any) -> str:
    try:
        serialized = _canonical_json(attestation)
    except (TypeError, ValueError):
        serialized = repr(attestation)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def experiment_fingerprint(definition: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(definition).encode("utf-8")).hexdigest()


def topic_for_role(role: str) -> int:
    try:
        return TOPIC_BY_ROLE[role.strip().lower()]
    except KeyError as exc:
        raise ValueError(f"unknown trading-research role: {role!r}") from exc


def format_topic_update(*, loop_id: str, role: str, status: str, summary: str) -> str:
    topic_for_role(role)  # validate without changing configuration-owned routing
    return f"[{LOOP_TYPE} {loop_id}] {role}: {status}\n{summary.strip()}"


@contextlib.contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Cross-process exclusive lock, independent from the Kanban DB lock."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:  # pragma: no cover - exercised on Unix CI
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:  # pragma: no cover - exercised on Unix CI
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _atomic_jsonl_append(path: Path, value: Mapping[str, Any]) -> None:
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    fd, raw_tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(raw_tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(old)
            if old and not old.endswith("\n"):
                stream.write("\n")
            stream.write(_canonical_json(value) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


_TASK_ROLE_BY_KEY = {
    "parent": "orchestrator",
    "research": "researcher",
    "development": "developer",
    "backtest": "backtester",
    "review": "reviewer",
}
_TASK_KEY_BY_ROLE = {role: key for key, role in _TASK_ROLE_BY_KEY.items()}


def _assert_official_kanban_connection(conn: Any) -> None:
    from hermes_cli import kanban_db as kb

    rows = conn.execute("PRAGMA database_list").fetchall()
    actual_db = Path(str(rows[0][2])).resolve()
    expected_db = Path(kb.kanban_db_path(KANBAN_BOARD)).resolve()
    if actual_db != expected_db:
        raise ValueError(
            f"connection targets {actual_db}, "
            f"not official board {KANBAN_BOARD} database {expected_db}."
        )


def _task_is_official_producer(task: Any, *, loop_id: str, role: str) -> bool:
    metadata = _metadata_from_body(task.body) if task is not None else None
    expected_assignee = "tester" if role == "backtester" else role
    return bool(
        task
        and task.tenant == f"trading-research-loop:{loop_id}"
        and task.assignee == expected_assignee
        and task.created_by == "orchestrator"
        and metadata
        and metadata.get("schema") == "trading-research-loop-task/v1"
        and metadata.get("loop_id") == loop_id
        and metadata.get("loop_type") == LOOP_TYPE
        and metadata.get("board") == KANBAN_BOARD
        and metadata.get("role") == role
        and metadata.get("topic_id") == TOPIC_BY_ROLE.get(role)
        and metadata.get("live_trading") is False
    )


class TradingResearchLoopStore:
    """Atomic, restart-safe governance state for bounded research cycles."""

    def __init__(
        self,
        root: Optional[Path] = None,
        *,
        approval_verifier: Optional[Callable[[Any], Optional[str]]] = None,
        producer_verifier: Optional[Callable[[Mapping[str, Any], str, str], bool]] = None,
    ):
        self.root = Path(root) if root is not None else default_root()
        self._approval_verifier = approval_verifier
        self._producer_verifier = producer_verifier

    def _dir(self, loop_id: str) -> Path:
        try:
            _validate_loop_id(loop_id)
        except argparse.ArgumentTypeError as exc:
            raise ValueError(str(exc)) from exc
        return self.root / loop_id

    def _lock(self, loop_id: str):
        return _file_lock(self._dir(loop_id) / ".lock")

    def _load_unlocked(self, loop_id: str) -> dict[str, Any]:
        path = self._dir(loop_id) / "state.json"
        if not path.exists():
            raise FileNotFoundError(f"unknown trading research loop: {loop_id}")
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TradingResearchLoopError(
                f"corrupt trading research loop state: {loop_id}"
            ) from exc
        self._validate_state(state)
        if state.get("loop_id") != loop_id:
            raise TradingResearchLoopError("loop state identity does not match its directory")
        return state

    def _save_unlocked(self, state: dict[str, Any]) -> dict[str, Any]:
        state["updated_at"] = int(state["updated_at"])
        _atomic_json(self._dir(state["loop_id"]) / "state.json", state)
        return state

    @staticmethod
    def _validate_state(state: Mapping[str, Any]) -> None:
        required = {
            "schema_version", "loop_type", "loop_id", "goal", "scope", "status",
            "iteration", "experiment_count", "consecutive_failures", "cost_spent",
            "limits", "revision", "created_at", "updated_at", "kanban_board",
            "kanban_tasks", "telegram_threads", "artifacts", "stop_reason",
        }
        missing = required - set(state)
        if missing:
            raise ValueError(f"invalid loop state; missing: {sorted(missing)}")
        if state["loop_type"] != LOOP_TYPE or state["schema_version"] != SCHEMA_VERSION:
            raise ValueError("unsupported trading research loop schema")
        if state["status"] not in TRANSITIONS:
            raise ValueError(f"unknown loop status: {state['status']}")
        if REQUIRED_LIMITS - set(state["limits"]):
            raise ValueError("loop limits are incomplete")

    def open_loop(
        self,
        *,
        goal: str,
        scope: Mapping[str, Any],
        limits: Optional[Mapping[str, Any]] = None,
        loop_id: Optional[str] = None,
        now: Optional[int] = None,
    ) -> dict[str, Any]:
        if not goal.strip():
            raise ValueError("goal is required")
        if scope.get("market") != "spot":
            raise SafetyViolation("only spot research scope is allowed")
        symbols = list(scope.get("symbols") or [])
        timeframes = list(scope.get("timeframes") or [])
        if not symbols or not timeframes:
            raise ValueError("at least one symbol and timeframe are required")
        if scope.get("long_only") is False:
            raise SafetyViolation("only long-only research scope is allowed")
        if any(not str(symbol).upper().endswith("/USDC") for symbol in symbols):
            raise SafetyViolation("only USDC research pairs are allowed")
        loop_id = loop_id or f"trl-{uuid.uuid4().hex[:12]}"
        merged_limits = dict(DEFAULT_LIMITS)
        if limits:
            merged_limits.update(limits)
        integer_limits = {"max_iterations", "max_experiments", "max_consecutive_failures"}
        for key in REQUIRED_LIMITS:
            value = merged_limits[key]
            if key in integer_limits:
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise ValueError(f"{key} must be a positive integer")
            elif _finite_number(value, key) <= 0:
                raise ValueError(f"{key} must be positive")
        created = _now(now)
        directory = self._dir(loop_id)
        with self._lock(loop_id):
            state_path = directory / "state.json"
            if state_path.exists():
                raise FileExistsError(f"loop already exists: {loop_id}")
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "artifacts").mkdir(exist_ok=True)
            state = {
                "schema_version": SCHEMA_VERSION,
                "loop_type": LOOP_TYPE,
                "loop_id": loop_id,
                "goal": goal.strip(),
                "scope": {
                    **dict(scope),
                    "symbols": symbols,
                    "timeframes": timeframes,
                    "long_only": True,
                },
                "status": "open",
                "iteration": 0,
                "experiment_count": 0,
                "consecutive_failures": 0,
                "cost_spent": 0.0,
                "limits": merged_limits,
                "revision": 0,
                "created_at": created,
                "updated_at": created,
                "kanban_board": KANBAN_BOARD,
                "kanban_tasks": {},
                "telegram_threads": dict(TOPIC_BY_ROLE),
                "artifacts": [],
                "current_candidate": None,
                "stop_reason": None,
                "history": [{"at": created, "event": "opened", "status": "open"}],
            }
            _atomic_json(state_path, state)
            _atomic_json(directory / "hypotheses.json", [])
            for name in ("experiments.jsonl", "approvals.jsonl", "rejected_hypotheses.jsonl"):
                (directory / name).touch(exist_ok=False)
            return state

    def load(self, loop_id: str) -> dict[str, Any]:
        with self._lock(loop_id):
            return self._load_unlocked(loop_id)

    def _check_budget(self, state: dict[str, Any], now: int) -> None:
        if state["status"] in TERMINAL_STATUSES:
            raise CircuitBreakerOpen(f"loop is terminal: {state['stop_reason']}")
        elapsed_hours = (now - int(state["created_at"])) / 3600
        if elapsed_hours >= float(state["limits"]["max_wall_clock_hours"]):
            self._stop_unlocked(state, "max_wall_clock_reached", now)
            self._save_unlocked(state)
            raise CircuitBreakerOpen("max wall-clock budget reached")
        if float(state["cost_spent"]) >= float(state["limits"]["max_cost_budget"]):
            self._stop_unlocked(state, "max_cost_budget_reached", now)
            self._save_unlocked(state)
            raise CircuitBreakerOpen("max cost budget reached")

    def _stop_unlocked(self, state: dict[str, Any], reason: str, now: int, *, status: str = "stopped") -> None:
        state["status"] = status
        state["stop_reason"] = reason
        state["revision"] += 1
        state["updated_at"] = now
        state["history"].append({"at": now, "event": "circuit_breaker", "reason": reason, "status": status})
        self._save_unlocked(state)

    def transition(
        self,
        loop_id: str,
        status: str,
        *,
        reason: str,
        expected_revision: Optional[int] = None,
        now: Optional[int] = None,
    ) -> dict[str, Any]:
        at = _now(now)
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            self._check_budget(state, at)
            if expected_revision is not None and state["revision"] != expected_revision:
                raise StateConflict(f"revision {state['revision']} != expected {expected_revision}")
            current = state["status"]
            if status in RESERVED_TRANSITION_TARGETS:
                raise InvalidTransition(f"status {status!r} is reserved for the promotion gate")
            if current == "blocked" and status == "researching":
                raise InvalidTransition("blocked recovery requires authenticated recover()")
            if status not in TRANSITIONS[current]:
                raise InvalidTransition(f"transition {current!r} -> {status!r} is forbidden")
            state["status"] = status
            state["stop_reason"] = reason if status in {"blocked", "stopped"} else None
            state["revision"] += 1
            state["updated_at"] = at
            state["history"].append({"at": at, "event": "transition", "from": current, "to": status, "reason": reason})
            return self._save_unlocked(state)

    def recover(self, loop_id: str, *, attestation: Any, now: Optional[int] = None) -> dict[str, Any]:
        if self._approval_verifier is None:
            raise PromotionBlocked("no authenticated human ingress is configured")
        actor = self._approval_verifier(attestation)
        if not actor or not actor.startswith("human:"):
            raise PromotionBlocked("recovery attestation is not authenticated as a human")
        at = _now(now)
        if not isinstance(attestation, Mapping):
            raise PromotionBlocked("recovery attestation must be a fresh expiring envelope")
        attestation_id = str(attestation.get("id") or "").strip()
        if not attestation_id:
            raise PromotionBlocked("recovery attestation id is required")
        try:
            expires_at = _finite_number(attestation.get("expires_at"), "expires_at")
            issued_at = _finite_number(attestation.get("issued_at"), "issued_at")
        except ValueError as exc:
            raise PromotionBlocked(f"invalid recovery attestation: {exc}") from exc
        if expires_at <= at:
            raise PromotionBlocked("recovery attestation has expired")
        if issued_at > at:
            raise PromotionBlocked("recovery attestation is not yet valid")
        if expires_at <= issued_at or expires_at - issued_at > RECOVERY_ATTESTATION_TTL_SECONDS:
            raise PromotionBlocked("recovery attestation validity window is invalid")
        fingerprint = _attestation_fingerprint(attestation)
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            if state["status"] != "blocked":
                raise InvalidTransition("only a blocked loop may be recovered")
            if any(
                item.get("event") == "recovered"
                and hmac.compare_digest(str(item.get("attestation_fingerprint", "")), fingerprint)
                for item in state.get("history", [])
            ):
                raise PromotionBlocked("recovery attestation has already been used")
            state["status"] = "researching"
            state["stop_reason"] = None
            state["consecutive_failures"] = 0
            state["revision"] += 1
            state["updated_at"] = at
            state["history"].append(
                {
                    "at": at,
                    "event": "recovered",
                    "actor": actor,
                    "attestation_fingerprint": fingerprint,
                }
            )
            return self._save_unlocked(state)

    def begin_iteration(
        self,
        loop_id: str,
        *,
        expected_revision: Optional[int] = None,
        now: Optional[int] = None,
    ) -> dict[str, Any]:
        at = _now(now)
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            self._check_budget(state, at)
            if state["status"] not in WORK_STATUSES:
                raise CircuitBreakerOpen(f"loop cannot iterate while {state['status']}")
            if expected_revision is not None and state["revision"] != expected_revision:
                raise StateConflict(f"revision {state['revision']} != expected {expected_revision}")
            if state["iteration"] >= int(state["limits"]["max_iterations"]):
                self._stop_unlocked(state, "max_iterations_reached", at)
                raise CircuitBreakerOpen("max iterations reached")
            state["iteration"] += 1
            state["revision"] += 1
            state["updated_at"] = at
            state["history"].append({"at": at, "event": "iteration_started", "iteration": state["iteration"]})
            return self._save_unlocked(state)

    def register_experiment(
        self,
        loop_id: str,
        definition: Mapping[str, Any],
        *,
        outcome: str,
        cost: float = 0.0,
        now: Optional[int] = None,
    ) -> dict[str, Any]:
        at = _now(now)
        numeric_cost = _finite_number(cost, "cost", minimum=0.0)
        fingerprint = experiment_fingerprint(definition)
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            self._check_budget(state, at)
            if state["status"] not in WORK_STATUSES:
                raise CircuitBreakerOpen(
                    f"loop cannot register experiments while {state['status']}"
                )
            experiments_path = self._dir(loop_id) / "experiments.jsonl"
            if any(row.get("fingerprint") == fingerprint for row in _read_jsonl(experiments_path)):
                raise DuplicateExperiment(f"experiment already recorded: {fingerprint}")
            if state["experiment_count"] >= int(state["limits"]["max_experiments"]):
                self._stop_unlocked(state, "max_experiments_reached", at)
                raise CircuitBreakerOpen("max experiments reached")
            if state["cost_spent"] + numeric_cost > float(state["limits"]["max_cost_budget"]):
                self._stop_unlocked(state, "max_cost_budget_reached", at)
                raise CircuitBreakerOpen("experiment reaches cost budget")
            record = {
                "at": at,
                "definition": dict(definition),
                "fingerprint": fingerprint,
                "outcome": outcome,
                "cost": numeric_cost,
            }
            _atomic_jsonl_append(experiments_path, record)
            state["experiment_count"] += 1
            state["cost_spent"] += numeric_cost
            state["consecutive_failures"] = 0
            state["revision"] += 1
            state["updated_at"] = at
            self._save_unlocked(state)
            return record

    def record_failure(self, loop_id: str, error: str, *, now: Optional[int] = None) -> dict[str, Any]:
        at = _now(now)
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            self._check_budget(state, at)
            if state["status"] not in WORK_STATUSES:
                raise CircuitBreakerOpen(
                    f"loop cannot record worker failures while {state['status']}"
                )
            state["consecutive_failures"] += 1
            state["revision"] += 1
            state["updated_at"] = at
            state["history"].append({"at": at, "event": "failure", "error": error[:500]})
            if state["consecutive_failures"] >= int(state["limits"]["max_consecutive_failures"]):
                state["status"] = "blocked"
                state["stop_reason"] = "max_consecutive_failures_reached"
                self._save_unlocked(state)
                raise CircuitBreakerOpen("consecutive-failure circuit breaker opened")
            return self._save_unlocked(state)

    def assert_safe_research_action(self, action: str) -> None:
        normalized = _normalize_action(action)
        if any(pattern.search(normalized) for pattern in _FORBIDDEN_ACTIONS):
            raise SafetyViolation("live trading, credentials, transfers and real orders are forbidden")

    def _assert_official_producer(
        self, state: Mapping[str, Any], producer_role: str, producer_task_id: str
    ) -> None:
        if self._producer_verifier is not None:
            if self._producer_verifier(state, producer_role, producer_task_id) is True:
                return
            raise SafetyViolation("artifact lacks a verified official Kanban producer")
        expected_key = _TASK_KEY_BY_ROLE.get(producer_role)
        if not expected_key or state.get("kanban_tasks", {}).get(expected_key) != producer_task_id:
            raise SafetyViolation("artifact lacks a verified official Kanban producer")
        from hermes_cli import kanban_db as kb

        conn = kb.connect(board=KANBAN_BOARD)
        try:
            _assert_official_kanban_connection(conn)
            task = kb.get_task(conn, producer_task_id)
            if not _task_is_official_producer(
                task, loop_id=str(state["loop_id"]), role=producer_role
            ):
                raise SafetyViolation("artifact lacks a verified official Kanban producer")
        finally:
            conn.close()

    def register_hypothesis(
        self,
        loop_id: str,
        hypothesis: Mapping[str, Any],
        *,
        now: Optional[int] = None,
    ) -> dict[str, Any]:
        hypothesis_id = str(hypothesis.get("id") or "").strip()
        if not hypothesis_id:
            raise ValueError("hypothesis.id is required")
        record = {**dict(hypothesis), "id": hypothesis_id, "registered_at": _now(now)}
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            self._check_budget(state, record["registered_at"])
            if state["status"] not in WORK_STATUSES:
                raise CircuitBreakerOpen(
                    f"loop cannot register hypotheses while {state['status']}"
                )
            path = self._dir(loop_id) / "hypotheses.json"
            hypotheses = json.loads(path.read_text(encoding="utf-8"))
            if any(row.get("id") == hypothesis_id for row in hypotheses):
                raise ValueError(f"hypothesis already exists: {hypothesis_id}")
            hypotheses.append(record)
            _atomic_json(path, hypotheses)
            state["revision"] += 1
            state["updated_at"] = record["registered_at"]
            state["history"].append(
                {"at": record["registered_at"], "event": "hypothesis_registered", "id": hypothesis_id}
            )
            self._save_unlocked(state)
        return record

    def reject_hypothesis(
        self,
        loop_id: str,
        hypothesis: Mapping[str, Any],
        *,
        reason: str,
        counterproposal: str,
        now: Optional[int] = None,
    ) -> dict[str, Any]:
        if not reason.strip():
            raise ValueError("reason is required for every rejection")
        if not counterproposal.strip():
            raise ValueError("counterproposal is required for every rejection")
        record = {
            "at": _now(now), "hypothesis": dict(hypothesis), "reason": reason.strip(),
            "counterproposal": counterproposal.strip(),
        }
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            self._check_budget(state, record["at"])
            if state["status"] not in WORK_STATUSES:
                raise CircuitBreakerOpen(
                    f"loop cannot reject hypotheses while {state['status']}"
                )
            _atomic_jsonl_append(self._dir(loop_id) / "rejected_hypotheses.jsonl", record)
        return record

    @staticmethod
    def _promotion_reasons(candidate: Mapping[str, Any]) -> list[str]:
        metrics = candidate.get("metrics") or {}
        reasons: list[str] = []
        required = {"net_profit_pct", "max_drawdown_pct", "profit_factor", "trades", "baseline_beaten"}
        if required - set(metrics):
            reasons.append("independent fee-aware backtest metrics are incomplete")
        else:
            try:
                if _finite_number(metrics["net_profit_pct"], "net_profit_pct") <= 0:
                    reasons.append("net profit is not positive after fees")
                if _finite_number(metrics["max_drawdown_pct"], "max_drawdown_pct", minimum=0) > 20:
                    reasons.append("maximum drawdown exceeds 20%")
                if _finite_number(metrics["profit_factor"], "profit_factor", minimum=0) <= 1:
                    reasons.append("profit factor does not exceed 1")
                trades = metrics["trades"]
                if isinstance(trades, bool) or not isinstance(trades, int) or trades < 30:
                    reasons.append("fewer than 30 independent trades")
                if metrics["baseline_beaten"] is not True:
                    reasons.append("baseline is not beaten")
            except ValueError as exc:
                reasons.append(str(exc))
        if not candidate.get("artifact_path"):
            reasons.append("candidate artifact path is missing")
        if not re.fullmatch(r"[0-9a-f]{64}", str(candidate.get("artifact_sha256", ""))):
            reasons.append("candidate artifact hash is missing or invalid")
        return reasons

    @staticmethod
    def _artifact_reasons(state: Mapping[str, Any], candidate: Mapping[str, Any]) -> list[str]:
        manifest = state.get("artifacts") or []
        by_path = {str(item.get("path")): item for item in manifest}
        reasons: list[str] = []
        candidate_record = by_path.get(str(candidate.get("artifact_path")))
        if not candidate_record or candidate_record.get("sha256") != candidate.get("artifact_sha256"):
            reasons.append("candidate artifact is not registered in the loop manifest")
        evidence = {str(item) for item in candidate.get("evidence") or []}
        missing = sorted(evidence - set(by_path))
        if missing:
            reasons.append(f"evidence is not registered in the loop manifest: {missing}")
        evidence_records = [by_path[path] for path in evidence if path in by_path]
        evidence_types = {item.get("evidence_type") for item in evidence_records}
        if not {"backtest", "review"}.issubset(evidence_types):
            reasons.append("structured backtest and review attestations are required")
        producers = {
            (item.get("producer_role"), item.get("producer_task_id"))
            for item in evidence_records
            if item.get("evidence_type") in {"backtest", "review"}
        }
        if len(producers) < 2:
            reasons.append("backtest and review evidence must have independent producers")
        return reasons

    def request_promotion(self, loop_id: str, candidate: Mapping[str, Any], *, now: Optional[int] = None) -> dict[str, Any]:
        at = _now(now)
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            self._check_budget(state, at)
            if state["status"] != "reviewing":
                raise InvalidTransition("promotion may only be requested from reviewing")
            reasons = self._promotion_reasons(candidate) + self._artifact_reasons(state, candidate)
            if reasons:
                state["status"] = "blocked"
                state["stop_reason"] = "promotion_gate_failed"
                state["revision"] += 1
                state["updated_at"] = at
                state["history"].append({"at": at, "event": "promotion_denied", "reasons": reasons})
                self._save_unlocked(state)
                raise PromotionBlocked("; ".join(reasons))
            state["status"] = "awaiting_approval"
            state["current_candidate"] = dict(candidate)
            state["promotion_requested_at"] = at
            state["promotion_request_id"] = uuid.uuid4().hex
            state["revision"] += 1
            state["updated_at"] = at
            state["history"].append({"at": at, "event": "promotion_requested", "hypothesis_id": candidate["hypothesis_id"]})
            return self._save_unlocked(state)

    def record_approval(
        self,
        loop_id: str,
        *,
        attestation: Any,
        request_id: str,
        approved: bool,
        now: Optional[int] = None,
    ) -> dict[str, Any]:
        if type(approved) is not bool:
            raise PromotionBlocked("approval decision must be a boolean")
        if self._approval_verifier is None:
            raise PromotionBlocked("no authenticated human approval ingress is configured")
        actor = self._approval_verifier(attestation)
        if not actor or not actor.startswith("human:"):
            raise PromotionBlocked("approval attestation is not authenticated as a human")
        at = _now(now)
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            self._check_budget(state, at)
            if state["status"] != "awaiting_approval":
                raise PromotionBlocked("loop is not awaiting approval")
            if request_id != state.get("promotion_request_id"):
                raise PromotionBlocked("approval request id does not match")
            candidate = state["current_candidate"]
            record = {
                "at": at,
                "actor": actor,
                "request_id": request_id,
                "scope": f"promotion:{candidate['hypothesis_id']}",
                "artifact_sha256": candidate["artifact_sha256"],
                "approved": approved,
            }
            _atomic_jsonl_append(self._dir(loop_id) / "approvals.jsonl", record)
        return record

    def promote(self, loop_id: str, candidate: Mapping[str, Any], *, now: Optional[int] = None) -> dict[str, Any]:
        at = _now(now)
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            self._check_budget(state, at)
            if state["status"] != "awaiting_approval":
                raise PromotionBlocked("loop is not awaiting approval")
            if experiment_fingerprint(state["current_candidate"]) != experiment_fingerprint(candidate):
                raise PromotionBlocked("candidate differs from the reviewed candidate")
            scope = f"promotion:{candidate['hypothesis_id']}"
            approvals = [
                row for row in _read_jsonl(self._dir(loop_id) / "approvals.jsonl")
                if row.get("request_id") == state.get("promotion_request_id")
                and row.get("scope") == scope
            ]
            if not approvals:
                raise PromotionBlocked("fresh human approval is required")
            approval = approvals[-1]
            if approval.get("approved") is not True:
                raise PromotionBlocked("latest human decision does not approve promotion")
            if approval.get("artifact_sha256") != candidate.get("artifact_sha256"):
                raise PromotionBlocked("approval artifact does not match candidate artifact")
            if at - int(approval["at"]) > int(state["limits"]["approval_ttl_seconds"]):
                raise PromotionBlocked("human approval has expired")
            artifact_reasons = self._artifact_reasons(state, candidate)
            if artifact_reasons:
                raise PromotionBlocked("; ".join(artifact_reasons))
            for item in state["artifacts"]:
                loop_directory = self._dir(loop_id).resolve()
                artifact_directory = loop_directory / "artifacts"
                artifact_path = loop_directory / str(item["path"])
                try:
                    resolved_path = artifact_path.resolve(strict=True)
                except OSError as exc:
                    raise PromotionBlocked(
                        f"artifact changed after registration: {item['path']}"
                    ) from exc
                if (
                    artifact_directory.is_symlink()
                    or artifact_directory.resolve() != artifact_directory
                    or artifact_path.is_symlink()
                    or resolved_path.parent != artifact_directory
                    or not resolved_path.is_file()
                    or hashlib.sha256(resolved_path.read_bytes()).hexdigest() != item["sha256"]
                ):
                    raise PromotionBlocked(f"artifact changed after registration: {item['path']}")
            state["status"] = "promoting"
            state["history"].append(
                {"at": at, "event": "promotion_started", "actor": approval["actor"], "scope": scope}
            )
            state["status"] = "stopped"
            state["stop_reason"] = "promoted_with_human_approval"
            state["revision"] += 1
            state["updated_at"] = at
            state["history"].append({"at": at, "event": "promoted", "actor": approval["actor"], "scope": scope})
            return self._save_unlocked(state)

    def write_artifact(
        self,
        loop_id: str,
        name: str,
        payload: bytes,
        *,
        evidence_type: str,
        producer_role: str,
        producer_task_id: str,
        now: Optional[int] = None,
    ) -> dict[str, Any]:
        safe_name = Path(name).name
        if safe_name != name or not safe_name:
            raise ValueError("artifact name must be a plain filename")
        at = _now(now)
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            self._check_budget(state, at)
            loop_directory = self._dir(loop_id).resolve()
            artifact_directory = self._dir(loop_id) / "artifacts"
            expected_artifact_directory = loop_directory / "artifacts"
            if (
                artifact_directory.is_symlink()
                or artifact_directory.resolve() != expected_artifact_directory
            ):
                raise SafetyViolation("artifact directory escapes the governed loop directory")
            path = artifact_directory / safe_name
            registered_path = f"artifacts/{safe_name}"
            if path.exists() or path.is_symlink() or any(item.get("path") == registered_path for item in state["artifacts"]):
                raise ValueError("artifact paths are immutable; use a new name")
            if evidence_type not in {"candidate", "backtest", "review", "benchmark", "risk", "kronos", "other"}:
                raise ValueError("unsupported evidence_type")
            if not producer_role.strip() or not producer_task_id.strip():
                raise ValueError("producer_role and producer_task_id are required")
            self._assert_official_producer(
                state, producer_role.strip(), producer_task_id.strip()
            )
            fd, raw_tmp = tempfile.mkstemp(prefix=f".{safe_name}.", suffix=".tmp", dir=path.parent)
            tmp = Path(raw_tmp)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
            finally:
                if tmp.exists():
                    tmp.unlink()
            record = {
                "at": at,
                "path": registered_path,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "size": len(payload),
                "evidence_type": evidence_type,
                "producer_role": producer_role.strip(),
                "producer_task_id": producer_task_id.strip(),
            }
            state["artifacts"].append(record)
            state["revision"] += 1
            state["updated_at"] = at
            self._save_unlocked(state)
            return record

    def attach_kanban_tasks(self, loop_id: str, tasks: Mapping[str, str], *, now: Optional[int] = None) -> dict[str, Any]:
        at = _now(now)
        with self._lock(loop_id):
            state = self._load_unlocked(loop_id)
            unknown = set(tasks) - set(_TASK_ROLE_BY_KEY)
            if unknown:
                raise SafetyViolation(f"unknown governed Kanban task roles: {sorted(unknown)}")
            from hermes_cli import kanban_db as kb

            conn = kb.connect(board=KANBAN_BOARD)
            try:
                _assert_official_kanban_connection(conn)
                for key, task_id in tasks.items():
                    if not _task_is_official_producer(
                        kb.get_task(conn, task_id),
                        loop_id=loop_id,
                        role=_TASK_ROLE_BY_KEY[key],
                    ):
                        raise SafetyViolation(
                            f"Kanban task {task_id!r} is not an official producer for {key}"
                        )
            finally:
                conn.close()
            state["kanban_tasks"].update(dict(tasks))
            state["revision"] += 1
            state["updated_at"] = at
            return self._save_unlocked(state)


def _task_body(loop_id: str, hypothesis: Mapping[str, Any], role: str, board: str) -> str:
    metadata = {
        "schema": "trading-research-loop-task/v1", "loop_id": loop_id,
        "loop_type": LOOP_TYPE, "board": board, "role": role,
        "hypothesis": dict(hypothesis), "topic_id": TOPIC_BY_ROLE[role],
        "live_trading": False,
    }
    return (
        "Governed research task. Research/backtest only; spot long-only USDC; "
        "no credentials, funds, transfers, or real orders.\n"
        f"TRADING_RESEARCH_LOOP_METADATA={_canonical_json(metadata)}"
    )


def decompose_hypothesis(
    conn: Any,
    *,
    loop_id: str,
    hypothesis: Mapping[str, Any],
    board: str = KANBAN_BOARD,
    workspace_path: Optional[Path] = None,
) -> dict[str, str]:
    """Create an idempotent official-Kanban chain for one hypothesis."""
    from hermes_cli import kanban_db as kb

    if board != KANBAN_BOARD:
        raise ValueError(f"board must be the official trading-research board {KANBAN_BOARD!r}")
    _assert_official_kanban_connection(conn)
    hypothesis_id = str(hypothesis.get("id") or "").strip()
    if not hypothesis_id:
        raise ValueError("hypothesis.id is required")
    TradingResearchLoopStore().assert_safe_research_action(_canonical_json(hypothesis))
    database_rows = conn.execute("PRAGMA database_list").fetchall()
    actual_db = Path(str(database_rows[0][2])).resolve()
    expected_db = Path(kb.kanban_db_path(board)).resolve()
    if actual_db != expected_db:
        raise ValueError(f"connection targets {actual_db}, not official board database {expected_db}")
    workdir = (Path(workspace_path) if workspace_path else default_root() / loop_id / "workspace").resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    tenant = f"trading-research-loop:{loop_id}"
    base = f"trl:{loop_id}:{hypothesis_id}"
    common = {
        "created_by": "orchestrator", "workspace_kind": "dir", "workspace_path": str(workdir), "tenant": tenant,
        "max_runtime_seconds": 600, "max_retries": 2, "goal_mode": False,
        "initial_status": "running", "board": board,
    }
    parent = kb.create_task(
        conn, title=f"[{loop_id}] Govern hypothesis {hypothesis_id}",
        body=_task_body(loop_id, hypothesis, "orchestrator", board), assignee="orchestrator",
        idempotency_key=f"{base}:parent", skills=["trading-research-loop"], **common,
    )
    specs = (
        ("research", "researcher", "Research and baseline design", parent),
        ("development", "developer", "Implement reproducible candidate", None),
        ("backtest", "backtester", "Fee-aware independent backtest", None),
        ("review", "reviewer", "Independent falsification review", None),
    )
    ids = {"parent": parent}
    previous = parent
    for key, role, title, _ in specs:
        assignee = "tester" if role == "backtester" else role
        skills = ["trading-research-loop", "trading-strategy-research"]
        task_id = kb.create_task(
            conn, title=f"[{loop_id}/{hypothesis_id}] {title}",
            body=_task_body(loop_id, hypothesis, role, board), assignee=assignee,
            parents=[previous], idempotency_key=f"{base}:{key}", skills=skills, **common,
        )
        ids[key] = task_id
        previous = task_id
    return ids


def _metadata_from_body(body: Optional[str]) -> Optional[dict[str, Any]]:
    marker = "TRADING_RESEARCH_LOOP_METADATA="
    for line in (body or "").splitlines():
        if line.startswith(marker):
            value = json.loads(line[len(marker):])
            return value if isinstance(value, dict) else None
    return None


def reconstruct_from_kanban(conn: Any, loop_id: str) -> dict[str, Any]:
    """Rebuild a minimal checkpoint from official Kanban card metadata."""
    from hermes_cli import kanban_db as kb

    _assert_official_kanban_connection(conn)
    tasks = kb.list_tasks(conn, tenant=f"trading-research-loop:{loop_id}", include_archived=True)
    if not tasks:
        raise FileNotFoundError(f"no Kanban tasks found for loop {loop_id}")
    role_to_key = {"orchestrator": "parent", "researcher": "research", "developer": "development", "backtester": "backtest", "reviewer": "review"}
    ids: dict[str, str] = {}
    hypotheses: dict[str, dict[str, Any]] = {}
    board = KANBAN_BOARD
    for task in tasks:
        metadata = _metadata_from_body(task.body)
        if not metadata or metadata.get("loop_id") != loop_id:
            continue
        role = str(metadata.get("role"))
        if not _task_is_official_producer(task, loop_id=loop_id, role=role):
            continue
        key = role_to_key.get(role)
        if key:
            ids[key] = task.id
        hypothesis = metadata.get("hypothesis") or {}
        if hypothesis.get("id"):
            hypotheses[str(hypothesis["id"])] = dict(hypothesis)

    if not ids:
        raise ValueError("Kanban cards exist but contain no valid loop metadata")
    return {"loop_id": loop_id, "loop_type": LOOP_TYPE, "kanban_board": board, "kanban_tasks": ids, "hypotheses": list(hypotheses.values())}


def _local_human_verifier(expected_confirmation: str) -> Callable[[Any], Optional[str]]:
    """Authenticate an explicit local-CLI confirmation as the OS user."""
    def verify(attestation: Any) -> Optional[str]:
        confirmation = attestation.get("confirmation") if isinstance(attestation, Mapping) else attestation
        if isinstance(confirmation, str) and hmac.compare_digest(confirmation, expected_confirmation):
            return f"human:local-cli:{getpass.getuser()}"
        return None

    return verify


def _load_mapping_file(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain one JSON object")
    return value


def build_parser(subparsers: Any) -> argparse.ArgumentParser:
    parser = subparsers.add_parser("trading-loop", help="Govern bounded, persistent trading-research loops")
    commands = parser.add_subparsers(dest="trading_loop_command", required=True)
    open_cmd = commands.add_parser("open", help="Open a bounded spot/USDC research loop")
    open_cmd.add_argument("--goal", required=True)
    open_cmd.add_argument("--symbol", action="append", required=True)
    open_cmd.add_argument("--timeframe", action="append")
    open_cmd.add_argument("--loop-id", type=_validate_loop_id)
    show = commands.add_parser("show", help="Print persisted loop state")
    show.add_argument("loop_id", type=_validate_loop_id)
    step = commands.add_parser("transition", help="Apply one whitelisted state transition")
    step.add_argument("loop_id", type=_validate_loop_id)
    step.add_argument("status", choices=sorted(TRANSITIONS))
    step.add_argument("--reason", required=True)
    iterate = commands.add_parser("iterate", help="Consume one bounded iteration")
    iterate.add_argument("loop_id", type=_validate_loop_id)
    recover = commands.add_parser("recover", help="Human-authenticated recovery of a blocked loop")
    recover.add_argument("loop_id", type=_validate_loop_id)
    recover.add_argument("--human-confirmation", required=True, help="Exact text: RECOVER <loop-id>")
    hypothesis = commands.add_parser("hypothesis", help="Register a hypothesis from a JSON object")
    hypothesis.add_argument("loop_id", type=_validate_loop_id)
    hypothesis.add_argument("json_file")
    decompose = commands.add_parser("decompose", help="Create and attach official Kanban tasks")
    decompose.add_argument("loop_id", type=_validate_loop_id)
    decompose.add_argument("hypothesis_file")
    experiment = commands.add_parser("experiment", help="Register a bounded experiment")
    experiment.add_argument("loop_id", type=_validate_loop_id)
    experiment.add_argument("definition_file")
    experiment.add_argument("--outcome", required=True)
    experiment.add_argument("--cost", type=float, default=0.0)
    artifact = commands.add_parser("artifact", help="Register immutable official-producer evidence")
    artifact.add_argument("loop_id", type=_validate_loop_id)
    artifact.add_argument("source_file")
    artifact.add_argument("--name")
    artifact.add_argument("--type", dest="evidence_type", required=True)
    artifact.add_argument("--producer-role", required=True)
    artifact.add_argument("--producer-task-id", required=True)
    request = commands.add_parser("request-promotion", help="Apply gates and request human approval")
    request.add_argument("loop_id", type=_validate_loop_id)
    request.add_argument("candidate_file")
    approve = commands.add_parser("approve", help="Record authenticated local-human approval")
    approve.add_argument("loop_id", type=_validate_loop_id)
    approve.add_argument("--human-confirmation", required=True, help="Exact text: APPROVE <loop-id>")
    promote = commands.add_parser("promote", help="Promote the exactly reviewed and approved candidate")
    promote.add_argument("loop_id", type=_validate_loop_id)
    promote.add_argument("candidate_file")
    return parser


def trading_loop_command(args: argparse.Namespace) -> int:
    command = args.trading_loop_command
    verifier = None
    if command == "recover":
        verifier = _local_human_verifier(f"RECOVER {args.loop_id}")
    elif command == "approve":
        verifier = _local_human_verifier(f"APPROVE {args.loop_id}")
    store = TradingResearchLoopStore(approval_verifier=verifier)
    if command == "open":
        state = store.open_loop(
            goal=args.goal,
            scope={
                "symbols": args.symbol,
                "timeframes": args.timeframe or ["5m"],
                "market": "spot",
            },
            loop_id=args.loop_id,
        )
    elif command == "show":
        state = store.load(args.loop_id)
    elif command == "transition":
        state = store.transition(args.loop_id, args.status, reason=args.reason)
    elif command == "iterate":
        state = store.begin_iteration(args.loop_id)
    elif command == "recover":
        issued_at = _now()
        state = store.recover(
            args.loop_id,
            attestation={
                "confirmation": args.human_confirmation,
                "id": uuid.uuid4().hex,
                "issued_at": issued_at,
                "expires_at": issued_at + RECOVERY_ATTESTATION_TTL_SECONDS,
            },
            now=issued_at,
        )
    elif command == "hypothesis":
        state = store.register_hypothesis(args.loop_id, _load_mapping_file(args.json_file))
    elif command == "decompose":
        hypothesis = _load_mapping_file(args.hypothesis_file)
        from hermes_cli import kanban_db as kb

        conn = kb.connect(board=KANBAN_BOARD)
        try:
            tasks = decompose_hypothesis(conn, loop_id=args.loop_id, hypothesis=hypothesis)
        finally:
            conn.close()
        state = store.attach_kanban_tasks(args.loop_id, tasks)
    elif command == "experiment":
        state = store.register_experiment(
            args.loop_id, _load_mapping_file(args.definition_file),
            outcome=args.outcome, cost=args.cost,
        )
    elif command == "artifact":
        source = Path(args.source_file)
        state = store.write_artifact(
            args.loop_id, args.name or source.name, source.read_bytes(),
            evidence_type=args.evidence_type, producer_role=args.producer_role,
            producer_task_id=args.producer_task_id,
        )
    elif command == "request-promotion":
        state = store.request_promotion(args.loop_id, _load_mapping_file(args.candidate_file))
    elif command == "approve":
        loop = store.load(args.loop_id)
        state = store.record_approval(
            args.loop_id, attestation=args.human_confirmation,
            request_id=str(loop.get("promotion_request_id") or ""), approved=True,
        )
    elif command == "promote":
        state = store.promote(args.loop_id, _load_mapping_file(args.candidate_file))
    else:  # pragma: no cover - argparse enforces this
        raise ValueError(f"unknown command: {command}")
    print(json.dumps(state, indent=2, sort_keys=True, ensure_ascii=False))
    return 0
