"""Bounded script-only execution boundary for review reconciliation and outboxes.

The runner is intentionally inert by default. ``dry-run`` is read-only,
``shadow`` may persist immutable reconciliation audit rows, and ``live`` can
process existing outbox intents only when both the operator config and an
explicitly injected provider adapter enable that surface. This module does not
create cron jobs, start agents, resolve model providers, register live adapters,
or perform recursive scheduling.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Literal, Mapping, Optional, Sequence, cast

from hermes_constants import display_hermes_home
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as github
from hermes_cli import kanban_reconciliation as reconciliation
from hermes_cli import kanban_slack as slack
from utils import is_truthy_value


RunnerMode = Literal["dry-run", "shadow", "live"]
RunnerStatus = Literal[
    "ok",
    "no_op",
    "disabled",
    "lease_held",
    "timed_out",
    "failed",
]

RUNNER_MODES = frozenset({"dry-run", "shadow", "live"})
RUNNER_LEASE_KEY = "human-review-boundary-v1"
DEFAULT_TIMEOUT_SECONDS = 120
DEFAULT_LEASE_SECONDS = 180
DEFAULT_MAX_ITEMS = 50
DEFAULT_RETRY_CEILING = 3
MAX_TIMEOUT_SECONDS = 15 * 60
MAX_LEASE_SECONDS = 60 * 60
MAX_ITEMS_PER_RUN = 500


class ReviewRunnerError(ValueError):
    """Runner configuration or persisted lease state is invalid."""


class ReviewRunnerDeadlineExceeded(TimeoutError):
    """The runner cannot safely start another bounded provider operation."""


def _positive_int(value: Any, field: str, *, maximum: int) -> int:
    if isinstance(value, bool):
        raise ReviewRunnerError(f"{field} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ReviewRunnerError(f"{field} must be a positive integer") from exc
    if parsed < 1 or parsed > maximum:
        raise ReviewRunnerError(f"{field} must be between 1 and {maximum}")
    return parsed


@dataclass(frozen=True)
class ReviewRunnerConfig:
    """Operator-controlled policy; every mutating/live capability defaults off."""

    enabled: bool = False
    gateway_enabled: bool = False
    mode: RunnerMode = "dry-run"
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    max_items: int = DEFAULT_MAX_ITEMS
    retry_ceiling: int = DEFAULT_RETRY_CEILING
    github_provider_enabled: bool = False
    slack_provider_enabled: bool = False

    def __post_init__(self) -> None:
        normalized_mode = str(self.mode or "").strip().casefold()
        if normalized_mode not in RUNNER_MODES:
            raise ReviewRunnerError(f"mode must be one of {sorted(RUNNER_MODES)!r}")
        timeout_seconds = _positive_int(
            self.timeout_seconds,
            "timeout_seconds",
            maximum=MAX_TIMEOUT_SECONDS,
        )
        lease_seconds = _positive_int(
            self.lease_seconds,
            "lease_seconds",
            maximum=MAX_LEASE_SECONDS,
        )
        if lease_seconds < timeout_seconds:
            raise ReviewRunnerError(
                "lease_seconds must be greater than or equal to timeout_seconds"
            )
        object.__setattr__(self, "mode", normalized_mode)
        object.__setattr__(self, "timeout_seconds", timeout_seconds)
        object.__setattr__(self, "lease_seconds", lease_seconds)
        object.__setattr__(
            self,
            "max_items",
            _positive_int(self.max_items, "max_items", maximum=MAX_ITEMS_PER_RUN),
        )
        object.__setattr__(
            self,
            "retry_ceiling",
            _positive_int(self.retry_ceiling, "retry_ceiling", maximum=20),
        )
        for field_name in (
            "enabled",
            "gateway_enabled",
            "github_provider_enabled",
            "slack_provider_enabled",
        ):
            object.__setattr__(
                self,
                field_name,
                is_truthy_value(getattr(self, field_name), default=False),
            )

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]]) -> "ReviewRunnerConfig":
        raw = value if isinstance(value, Mapping) else {}
        providers = raw.get("providers")
        providers = providers if isinstance(providers, Mapping) else {}
        github_cfg = providers.get("github")
        github_cfg = github_cfg if isinstance(github_cfg, Mapping) else {}
        slack_cfg = providers.get("slack")
        slack_cfg = slack_cfg if isinstance(slack_cfg, Mapping) else {}
        return cls(
            enabled=is_truthy_value(raw.get("enabled"), default=False),
            gateway_enabled=is_truthy_value(raw.get("gateway_enabled"), default=False),
            mode=cast(RunnerMode, str(raw.get("mode") or "dry-run")),
            timeout_seconds=raw.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS),
            lease_seconds=raw.get("lease_seconds", DEFAULT_LEASE_SECONDS),
            max_items=raw.get("max_items_per_run", DEFAULT_MAX_ITEMS),
            retry_ceiling=raw.get("retry_ceiling", DEFAULT_RETRY_CEILING),
            github_provider_enabled=is_truthy_value(
                github_cfg.get("enabled"), default=False
            ),
            slack_provider_enabled=is_truthy_value(
                slack_cfg.get("enabled"), default=False
            ),
        )

    def with_overrides(
        self,
        *,
        mode: Optional[str] = None,
        timeout_seconds: Optional[int] = None,
        lease_seconds: Optional[int] = None,
        max_items: Optional[int] = None,
        retry_ceiling: Optional[int] = None,
    ) -> "ReviewRunnerConfig":
        return replace(
            self,
            mode=mode if mode is not None else self.mode,
            timeout_seconds=(
                timeout_seconds if timeout_seconds is not None else self.timeout_seconds
            ),
            lease_seconds=(
                lease_seconds if lease_seconds is not None else self.lease_seconds
            ),
            max_items=max_items if max_items is not None else self.max_items,
            retry_ceiling=(
                retry_ceiling if retry_ceiling is not None else self.retry_ceiling
            ),
        )


@dataclass(frozen=True)
class ReviewRunnerAdapters:
    """Explicit injection point; no live adapter is constructed in this phase."""

    # Every future live adapter must enforce this request-level timeout in its
    # own HTTP/client layer. A runner wall-clock budget alone cannot safely
    # cancel an in-flight side effect, so missing/oversized declarations fail
    # closed before any provider call.
    provider_timeout_seconds: Optional[int] = None
    reconciliation_snapshot_provider: Optional[
        reconciliation.ReconciliationSnapshotProvider
    ] = None
    github_snapshot_provider: Optional[github.GitHubSnapshotProvider] = None
    github_delivery_transport: Optional[github.GitHubDeliveryTransport] = None
    slack_snapshot_provider: Optional[slack.PullRequestSnapshotProvider] = None
    slack_delivery_transport: Optional[slack.SlackDeliveryTransport] = None

    @property
    def has_registered_adapter(self) -> bool:
        return any(
            adapter is not None
            for adapter in (
                self.reconciliation_snapshot_provider,
                self.github_snapshot_provider,
                self.github_delivery_transport,
                self.slack_snapshot_provider,
                self.slack_delivery_transport,
            )
        )

    def timeout_is_bounded(self, *, runner_timeout_seconds: int) -> bool:
        if not self.has_registered_adapter:
            return True
        if self.provider_timeout_seconds is None:
            return False
        try:
            timeout = int(self.provider_timeout_seconds)
        except (TypeError, ValueError):
            return False
        return 1 <= timeout <= runner_timeout_seconds


@dataclass(frozen=True)
class RunnerCandidate:
    surface: Literal["github", "slack"]
    intent_id: str
    state: str
    attempt_count: int
    max_attempts: int
    created_at: int
    updated_at: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LeaseReceipt:
    acquired: bool
    owner_id: str
    lease_key: str
    expires_at: int
    previous_owner_id: Optional[str] = None
    stale_recovered: bool = False


@dataclass(frozen=True)
class ReviewRunnerReceipt:
    status: RunnerStatus
    mode: RunnerMode
    read_only: bool
    run_id: Optional[str]
    reconciliation_status: Optional[str]
    reconciliation_input_sha256: Optional[str]
    reconciliation_run_id: Optional[str]
    finding_count: int
    candidates: tuple[RunnerCandidate, ...]
    results: tuple[dict[str, Any], ...]
    skipped: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    lease_stale_recovered: bool = False

    @property
    def quiet_noop(self) -> bool:
        return self.status in {"no_op", "disabled", "lease_held"}

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": self.status,
            "mode": self.mode,
            "read_only": self.read_only,
            "run_id": self.run_id,
            "script_only": True,
            "llm_used": False,
            "recursive_scheduling": False,
            "external_adapters_registered_by_default": False,
            "reconciliation": {
                "status": self.reconciliation_status,
                "input_sha256": self.reconciliation_input_sha256,
                "run_id": self.reconciliation_run_id,
                "finding_count": self.finding_count,
            },
            "candidates": [item.to_dict() for item in self.candidates],
            "results": list(self.results),
            "skipped": list(self.skipped),
            "errors": list(self.errors),
            "lease_stale_recovered": self.lease_stale_recovered,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )


class _DisabledReconciliationSnapshotProvider:
    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> Optional[github.GitHubPullRequestSnapshot]:
        del repository, pr_number
        return None


@dataclass(frozen=True)
class _DeadlineSnapshotProvider:
    """Refuse to start a read that cannot finish inside the runner budget.

    Live adapters must enforce ``provider_timeout_seconds`` in their own client.
    This wrapper makes that declared request bound composable with the runner's
    wall-clock budget: once less than one complete request window remains, no
    additional provider call is started.
    """

    delegate: reconciliation.ReconciliationSnapshotProvider
    monotonic: Callable[[], float]
    deadline: float
    provider_timeout_seconds: int

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> Optional[github.GitHubPullRequestSnapshot]:
        remaining = self.deadline - self.monotonic()
        if remaining < self.provider_timeout_seconds:
            raise ReviewRunnerDeadlineExceeded(
                "runner deadline exhausted before the next reconciliation read"
            )
        return self.delegate.read_snapshot(
            repository=repository,
            pr_number=pr_number,
        )


def _candidate_provider_call_count(
    candidate: RunnerCandidate,
    *,
    config: ReviewRunnerConfig,
    adapters: ReviewRunnerAdapters,
) -> int:
    """Return the worst-case external calls made by one outbox attempt.

    Both provider boundaries perform one current-state read, one idempotency
    readback, one send, and a final readback if send raises. Reserving all four
    request windows prevents a runner timeout from expiring its board lease in
    the middle of an external delivery attempt.
    """
    if candidate.surface == "github":
        ready = (
            config.github_provider_enabled
            and adapters.github_snapshot_provider is not None
            and adapters.github_delivery_transport is not None
        )
    else:
        ready = (
            config.slack_provider_enabled
            and adapters.slack_snapshot_provider is not None
            and adapters.slack_delivery_transport is not None
        )
    return 4 if ready else 0


def acquire_runner_lease(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    now: int,
    lease_seconds: int,
) -> LeaseReceipt:
    """Acquire one board-local lease, recovering it only after exact expiry."""
    normalized_owner = str(owner_id or "").strip()
    if not normalized_owner:
        raise ReviewRunnerError("owner_id must be a non-empty string")
    lease_ttl = _positive_int(
        lease_seconds,
        "lease_seconds",
        maximum=MAX_LEASE_SECONDS,
    )
    expires_at = int(now) + lease_ttl
    with kb.write_txn(conn):
        existing = conn.execute(
            "SELECT owner_id, expires_at FROM review_boundary_runner_leases "
            "WHERE lease_key=?",
            (RUNNER_LEASE_KEY,),
        ).fetchone()
        if existing is not None and int(existing["expires_at"]) > int(now):
            return LeaseReceipt(
                False,
                normalized_owner,
                RUNNER_LEASE_KEY,
                int(existing["expires_at"]),
                previous_owner_id=str(existing["owner_id"]),
            )
        previous_owner = str(existing["owner_id"]) if existing is not None else None
        updated = conn.execute(
            """
            INSERT INTO review_boundary_runner_leases (
                lease_key, owner_id, acquired_at, heartbeat_at, expires_at
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(lease_key) DO UPDATE SET
                owner_id=excluded.owner_id,
                acquired_at=excluded.acquired_at,
                heartbeat_at=excluded.heartbeat_at,
                expires_at=excluded.expires_at
            WHERE review_boundary_runner_leases.expires_at <= excluded.acquired_at
            """,
            (
                RUNNER_LEASE_KEY,
                normalized_owner,
                int(now),
                int(now),
                expires_at,
            ),
        )
        return LeaseReceipt(
            updated.rowcount == 1,
            normalized_owner,
            RUNNER_LEASE_KEY,
            expires_at,
            previous_owner_id=previous_owner,
            stale_recovered=previous_owner is not None and updated.rowcount == 1,
        )


def renew_runner_lease(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    now: int,
    lease_seconds: int,
) -> bool:
    expires_at = int(now) + _positive_int(
        lease_seconds,
        "lease_seconds",
        maximum=MAX_LEASE_SECONDS,
    )
    with kb.write_txn(conn):
        updated = conn.execute(
            "UPDATE review_boundary_runner_leases "
            "SET heartbeat_at=?, expires_at=? "
            "WHERE lease_key=? AND owner_id=? AND expires_at>?",
            (int(now), expires_at, RUNNER_LEASE_KEY, owner_id, int(now)),
        )
    return updated.rowcount == 1


def release_runner_lease(conn: sqlite3.Connection, *, owner_id: str) -> bool:
    with kb.write_txn(conn):
        deleted = conn.execute(
            "DELETE FROM review_boundary_runner_leases "
            "WHERE lease_key=? AND owner_id=?",
            (RUNNER_LEASE_KEY, owner_id),
        )
    return deleted.rowcount == 1


def _surface_candidates(
    conn: sqlite3.Connection,
    *,
    surface: Literal["github", "slack"],
    now: int,
    retry_ceiling: int,
) -> tuple[tuple[RunnerCandidate, ...], int]:
    if surface == "github":
        table = "github_human_review_outbox"
        attempt_lease = github.ATTEMPT_LEASE_SECONDS
    else:
        table = "slack_human_review_outbox"
        attempt_lease = slack.ATTEMPT_LEASE_SECONDS
    rows = conn.execute(
        f"""
        SELECT id, state, attempt_count, max_attempts, created_at, updated_at
        FROM {table}
        WHERE (
            state IN ('pending', 'retry')
            AND (next_attempt_at IS NULL OR next_attempt_at <= ?)
        ) OR (
            state='attempting' AND updated_at <= ?
        )
        ORDER BY created_at, id
        """,
        (int(now), int(now) - attempt_lease),
    ).fetchall()
    candidates: list[RunnerCandidate] = []
    exhausted = 0
    for row in rows:
        attempt_count = int(row["attempt_count"])
        max_attempts = int(row["max_attempts"])
        effective_ceiling = min(max_attempts, retry_ceiling)
        if attempt_count >= effective_ceiling:
            exhausted += 1
            continue
        candidates.append(
            RunnerCandidate(
                surface=surface,
                intent_id=row["id"],
                state=row["state"],
                attempt_count=attempt_count,
                max_attempts=max_attempts,
                created_at=int(row["created_at"]),
                updated_at=int(row["updated_at"]),
            )
        )
    return tuple(candidates), exhausted


def list_runner_candidates(
    conn: sqlite3.Connection,
    *,
    now: int,
    max_items: int,
    retry_ceiling: int,
) -> tuple[tuple[RunnerCandidate, ...], dict[str, int]]:
    bound = _positive_int(max_items, "max_items", maximum=MAX_ITEMS_PER_RUN)
    ceiling = _positive_int(retry_ceiling, "retry_ceiling", maximum=20)
    github_candidates, github_exhausted = _surface_candidates(
        conn,
        surface="github",
        now=now,
        retry_ceiling=ceiling,
    )
    slack_candidates, slack_exhausted = _surface_candidates(
        conn,
        surface="slack",
        now=now,
        retry_ceiling=ceiling,
    )
    ordered = tuple(
        sorted(
            github_candidates + slack_candidates,
            key=lambda item: (item.created_at, item.surface, item.intent_id),
        )[:bound]
    )
    return ordered, {
        "github_due": len(github_candidates),
        "slack_due": len(slack_candidates),
        "github_retry_exhausted": github_exhausted,
        "slack_retry_exhausted": slack_exhausted,
        "bounded_candidates": len(ordered),
    }


def _surface_backlog_health(
    conn: sqlite3.Connection,
    *,
    surface: Literal["github", "slack"],
    now: int,
) -> dict[str, Any]:
    if surface == "github":
        table = "github_human_review_outbox"
        attempt_lease = github.ATTEMPT_LEASE_SECONDS
    else:
        table = "slack_human_review_outbox"
        attempt_lease = slack.ATTEMPT_LEASE_SECONDS
    state_counts = {
        str(row["state"]): int(row["count"])
        for row in conn.execute(
            f"SELECT state, COUNT(*) AS count FROM {table} GROUP BY state"
        ).fetchall()
    }
    open_row = conn.execute(
        f"""
        SELECT COUNT(*) AS count, MIN(created_at) AS oldest_created_at
        FROM {table}
        WHERE state IN ('pending', 'retry', 'attempting')
        """
    ).fetchone()
    stale_attempting = int(
        conn.execute(
            f"SELECT COUNT(*) FROM {table} "
            "WHERE state='attempting' AND updated_at <= ?",
            (int(now) - attempt_lease,),
        ).fetchone()[0]
    )
    oldest_created_at = open_row["oldest_created_at"]
    return {
        "state_counts": state_counts,
        "open_count": int(open_row["count"]),
        "stale_attempting": stale_attempting,
        "oldest_open_age_seconds": (
            max(0, int(now) - int(oldest_created_at))
            if oldest_created_at is not None
            else None
        ),
    }


def diagnose_review_runner(
    conn: sqlite3.Connection,
    *,
    config: ReviewRunnerConfig,
    now: Optional[int] = None,
    adapters: Optional[ReviewRunnerAdapters] = None,
) -> dict[str, Any]:
    checked_at = int(time.time()) if now is None else int(now)
    registered = adapters or ReviewRunnerAdapters()
    candidates, counts = list_runner_candidates(
        conn,
        now=checked_at,
        max_items=config.max_items,
        retry_ceiling=config.retry_ceiling,
    )
    lease = conn.execute(
        "SELECT owner_id, acquired_at, heartbeat_at, expires_at "
        "FROM review_boundary_runner_leases WHERE lease_key=?",
        (RUNNER_LEASE_KEY,),
    ).fetchone()
    lease_payload = None
    if lease is not None:
        lease_payload = {
            "owner_id": lease["owner_id"],
            "acquired_at": int(lease["acquired_at"]),
            "heartbeat_at": int(lease["heartbeat_at"]),
            "expires_at": int(lease["expires_at"]),
            "stale": int(lease["expires_at"]) <= checked_at,
        }
    github_registered = (
        registered.github_snapshot_provider is not None
        and registered.github_delivery_transport is not None
    )
    slack_registered = (
        registered.slack_snapshot_provider is not None
        and registered.slack_delivery_transport is not None
    )
    timeout_bounded = registered.timeout_is_bounded(
        runner_timeout_seconds=config.timeout_seconds
    )
    github_connectivity = (
        "disabled"
        if not config.github_provider_enabled
        else "registered_unprobed"
        if github_registered
        else "adapter_not_registered"
    )
    slack_connectivity = (
        "disabled"
        if not config.slack_provider_enabled
        else "registered_unprobed"
        if slack_registered
        else "adapter_not_registered"
    )
    return {
        "schema_version": 1,
        "checked_at": checked_at,
        "script_only": True,
        "llm_required": False,
        "recursive_scheduling": False,
        "configuration": {
            "valid": True,
            "enabled": config.enabled,
            "gateway_enabled": config.gateway_enabled,
            "mode": config.mode,
            "timeout_seconds": config.timeout_seconds,
            "lease_seconds": config.lease_seconds,
            "max_items_per_run": config.max_items,
            "retry_ceiling": config.retry_ceiling,
        },
        "providers": {
            "github": {
                "enabled": config.github_provider_enabled,
                "registered": github_registered,
                "connectivity": github_connectivity,
                "connectivity_probe_performed": False,
                "ready": (
                    config.github_provider_enabled
                    and github_registered
                    and timeout_bounded
                ),
            },
            "slack": {
                "enabled": config.slack_provider_enabled,
                "registered": slack_registered,
                "connectivity": slack_connectivity,
                "connectivity_probe_performed": False,
                "ready": (
                    config.slack_provider_enabled
                    and slack_registered
                    and timeout_bounded
                ),
            },
            "provider_timeout_seconds": registered.provider_timeout_seconds,
            "timeout_bounded": timeout_bounded,
        },
        "readiness": {
            "dry_run_ready": timeout_bounded,
            "shadow_ready": config.enabled and timeout_bounded,
            "live_ready": config.enabled
            and (
                (
                    config.github_provider_enabled
                    and github_registered
                    and timeout_bounded
                )
                or (
                    config.slack_provider_enabled
                    and slack_registered
                    and timeout_bounded
                )
            ),
        },
        "outbox": {
            **counts,
            "candidate_ids": [item.intent_id for item in candidates],
            "github": _surface_backlog_health(
                conn,
                surface="github",
                now=checked_at,
            ),
            "slack": _surface_backlog_health(
                conn,
                surface="slack",
                now=checked_at,
            ),
        },
        "lease": lease_payload,
        "gateway": {
            "requires_gateway_restart": False,
            "configuration_loaded_per_invocation": True,
            "code_deployment_requires_gateway_restart": True,
            "external_operator_restart_command": "hermes gateway " + "restart",
            "post_restart_verification": [
                "hermes gateway status",
                "/kanban review-runner health --json",
            ],
        },
        "cron": {
            "job_created_by_runner": False,
            "compatible_mode": "no_agent",
            "script_directory": f"{display_hermes_home()}/scripts",
            "runner_command": ("hermes kanban review-runner run --quiet --json"),
            "quiet_noop_stdout": True,
            "provider_credentials_inherited": False,
        },
    }


def _process_candidate(
    conn: sqlite3.Connection,
    candidate: RunnerCandidate,
    *,
    config: ReviewRunnerConfig,
    adapters: ReviewRunnerAdapters,
    now: int,
) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    if candidate.surface == "github":
        if not config.github_provider_enabled:
            return None, {
                "surface": "github",
                "intent_id": candidate.intent_id,
                "reason": "provider_disabled",
            }
        if (
            adapters.github_snapshot_provider is None
            or adapters.github_delivery_transport is None
        ):
            return None, {
                "surface": "github",
                "intent_id": candidate.intent_id,
                "reason": "adapter_not_registered",
            }
        result = github.process_intent(
            conn,
            candidate.intent_id,
            snapshot_provider=adapters.github_snapshot_provider,
            delivery_transport=adapters.github_delivery_transport,
            now=now,
        )
    else:
        if not config.slack_provider_enabled:
            return None, {
                "surface": "slack",
                "intent_id": candidate.intent_id,
                "reason": "provider_disabled",
            }
        if (
            adapters.slack_snapshot_provider is None
            or adapters.slack_delivery_transport is None
        ):
            return None, {
                "surface": "slack",
                "intent_id": candidate.intent_id,
                "reason": "adapter_not_registered",
            }
        result = slack.process_intent(
            conn,
            candidate.intent_id,
            snapshot_provider=adapters.slack_snapshot_provider,
            delivery_transport=adapters.slack_delivery_transport,
            now=now,
        )
    return {
        "surface": candidate.surface,
        **asdict(result),
    }, None


def run_review_runner(
    conn: sqlite3.Connection,
    *,
    config: ReviewRunnerConfig,
    adapters: Optional[ReviewRunnerAdapters] = None,
    linear_issue_ids: Optional[Sequence[str]] = None,
    now: Optional[int] = None,
    owner_id: Optional[str] = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> ReviewRunnerReceipt:
    """Run one bounded audit/outbox pass without creating follow-up work."""
    started_at = int(time.time()) if now is None else int(now)
    effective_adapters = adapters or ReviewRunnerAdapters()
    candidates, _counts = list_runner_candidates(
        conn,
        now=started_at,
        max_items=config.max_items,
        retry_ceiling=config.retry_ceiling,
    )
    mode = config.mode
    if not effective_adapters.timeout_is_bounded(
        runner_timeout_seconds=config.timeout_seconds
    ):
        return ReviewRunnerReceipt(
            "failed",
            mode,
            mode == "dry-run",
            None,
            None,
            None,
            None,
            0,
            candidates,
            (),
            (),
            (
                "registered provider adapters must declare a positive "
                "provider_timeout_seconds no greater than the runner timeout",
            ),
        )
    if mode != "dry-run" and not config.enabled:
        return ReviewRunnerReceipt(
            "disabled",
            mode,
            mode == "dry-run",
            None,
            None,
            None,
            None,
            0,
            candidates,
            (),
            ({"reason": "runner_disabled"},),
            (),
        )

    run_id = None if mode == "dry-run" else "rbr_" + secrets.token_hex(12)
    lease: Optional[LeaseReceipt] = None
    if mode != "dry-run":
        lease = acquire_runner_lease(
            conn,
            owner_id=owner_id or run_id or "review-runner",
            now=started_at,
            lease_seconds=config.lease_seconds,
        )
        if not lease.acquired:
            return ReviewRunnerReceipt(
                "lease_held",
                mode,
                False,
                run_id,
                None,
                None,
                None,
                0,
                candidates,
                (),
                ({"reason": "runner_lease_held"},),
                (),
            )

    started_tick = monotonic()
    deadline = started_tick + config.timeout_seconds
    execution: Optional[reconciliation.ReconciliationExecution] = None
    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[str] = []
    status: RunnerStatus = "no_op"
    lease_owner = lease.owner_id if lease is not None else None
    try:
        provider = effective_adapters.reconciliation_snapshot_provider
        if provider is None:
            provider = effective_adapters.github_snapshot_provider
        if provider is None:
            provider = _DisabledReconciliationSnapshotProvider()
        else:
            assert effective_adapters.provider_timeout_seconds is not None
            provider = _DeadlineSnapshotProvider(
                provider,
                monotonic,
                deadline,
                int(effective_adapters.provider_timeout_seconds),
            )
        completed_execution = reconciliation.reconcile(
            conn,
            snapshot_provider=provider,
            linear_issue_ids=linear_issue_ids,
            persist=mode in {"shadow", "live"},
            now=started_at,
            max_pull_requests=config.max_items,
        )
        execution = completed_execution
        if monotonic() >= deadline:
            status = "timed_out"
        elif mode in {"dry-run", "shadow"}:
            status = (
                "ok"
                if candidates
                or completed_execution.report.findings
                or (
                    completed_execution.persisted_run is not None
                    and completed_execution.persisted_run.created
                )
                else "no_op"
            )
        else:
            for candidate in candidates:
                current_tick = monotonic()
                if current_tick >= deadline:
                    status = "timed_out"
                    break
                provider_call_count = _candidate_provider_call_count(
                    candidate,
                    config=config,
                    adapters=effective_adapters,
                )
                provider_timeout = effective_adapters.provider_timeout_seconds or 0
                if (
                    provider_call_count
                    and deadline - current_tick < provider_call_count * provider_timeout
                ):
                    status = "timed_out"
                    break
                if lease_owner is not None:
                    renew_now = started_at + max(
                        0,
                        int(current_tick - started_tick),
                    )
                    if not renew_runner_lease(
                        conn,
                        owner_id=lease_owner,
                        now=renew_now,
                        lease_seconds=config.lease_seconds,
                    ):
                        errors.append("runner lease was lost during execution")
                        status = "failed"
                        break
                try:
                    result, skip = _process_candidate(
                        conn,
                        candidate,
                        config=config,
                        adapters=effective_adapters,
                        now=started_at,
                    )
                except (KeyboardInterrupt, SystemExit):
                    raise
                except Exception as exc:
                    errors.append(
                        f"{candidate.surface}:{candidate.intent_id}:"
                        f"unexpected {type(exc).__name__}"
                    )
                    continue
                if result is not None:
                    results.append(result)
                if skip is not None:
                    skipped.append(skip)
            else:
                if errors:
                    status = "failed"
                elif results:
                    status = "ok"
                else:
                    status = "no_op"
    except (KeyboardInterrupt, SystemExit):
        raise
    except ReviewRunnerDeadlineExceeded as exc:
        errors.append(str(exc))
        status = "timed_out"
    except Exception as exc:
        errors.append(f"unexpected {type(exc).__name__}")
        status = "failed"
    finally:
        if lease_owner is not None:
            release_runner_lease(conn, owner_id=lease_owner)

    persisted = execution.persisted_run if execution is not None else None
    report = execution.report if execution is not None else None
    return ReviewRunnerReceipt(
        status=status,
        mode=mode,
        read_only=mode == "dry-run",
        run_id=run_id,
        reconciliation_status=report.status if report is not None else None,
        reconciliation_input_sha256=(
            report.input_sha256 if report is not None else None
        ),
        reconciliation_run_id=persisted.run_id if persisted is not None else None,
        finding_count=len(report.findings) if report is not None else 0,
        candidates=candidates,
        results=tuple(results),
        skipped=tuple(skipped),
        errors=tuple(errors),
        lease_stale_recovered=lease.stale_recovered if lease is not None else False,
    )
