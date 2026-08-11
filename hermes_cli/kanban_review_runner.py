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
import re
import secrets
import sqlite3
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Literal, Mapping, Optional, Sequence, cast

from hermes_constants import display_hermes_home
from hermes_cli import kanban_coderabbit as coderabbit
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as github
from hermes_cli import kanban_human_review as human_review
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
DEFAULT_PROVIDER_TIMEOUT_SECONDS = 15
MAX_TIMEOUT_SECONDS = 15 * 60
MAX_LEASE_SECONDS = 60 * 60
MAX_ITEMS_PER_RUN = 500
_ADAPTER_FAILURE_KINDS = frozenset({
    "auth",
    "permission",
    "not_found",
    "rate_limited",
    "timeout",
    "unavailable",
    "validation",
})
_ADAPTER_FAILURE_TYPE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class ReviewRunnerError(ValueError):
    """Runner configuration or persisted lease state is invalid."""


class ReviewRunnerDeadlineExceeded(TimeoutError):
    """The runner cannot safely start another bounded provider operation."""


def _redacted_adapter_setup_failure(
    value: Optional[BaseException | Mapping[str, Any]],
) -> dict[str, str]:
    """Reduce an adapter failure to a fixed, non-secret diagnostic envelope."""

    if value is None:
        return {}
    if isinstance(value, BaseException):
        error_type = type(value).__name__
        raw_kind = getattr(value, "kind", None)
    else:
        error_type = str(value.get("type") or "Exception")
        raw_kind = value.get("kind")
    if not _ADAPTER_FAILURE_TYPE_RE.fullmatch(error_type):
        error_type = "Exception"
    kind = str(raw_kind or "").strip().casefold()
    if kind not in _ADAPTER_FAILURE_KINDS:
        kind = "unknown"
    return {"type": error_type, "kind": kind}


def _string_tuple(value: Any, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ReviewRunnerError(f"{field} must be an array of non-empty strings")
    result = tuple(str(item or "").strip() for item in value)
    if any(not item for item in result):
        raise ReviewRunnerError(f"{field} must contain only non-empty strings")
    return result


def _adapter_kind(value: Any, field: str) -> str:
    normalized = str(value or "disabled").strip().casefold()
    if normalized not in {"disabled", "mcp"}:
        raise ReviewRunnerError(f"{field} must be 'disabled' or 'mcp'")
    return normalized


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
    provider_timeout_seconds: int = DEFAULT_PROVIDER_TIMEOUT_SECONDS
    github_provider_enabled: bool = False
    github_delivery_enabled: bool = False
    slack_provider_enabled: bool = False
    slack_delivery_enabled: bool = False
    github_adapter: str = "disabled"
    github_mcp_server: str = "github"
    github_repositories: tuple[str, ...] = ()
    github_reviewer_logins: tuple[str, ...] = ()
    coderabbit_logins: tuple[str, ...] = ("coderabbitai[bot]", "coderabbitai")
    slack_adapter: str = "disabled"
    slack_mcp_server: str = "slack"
    slack_channel_ids: tuple[str, ...] = ()
    slack_notification_channel_id: str = ""
    slack_acknowledgement_user_ids: tuple[str, ...] = ()

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
        provider_timeout = _positive_int(
            self.provider_timeout_seconds,
            "provider_timeout_seconds",
            maximum=MAX_TIMEOUT_SECONDS,
        )
        object.__setattr__(self, "provider_timeout_seconds", provider_timeout)
        object.__setattr__(
            self,
            "github_adapter",
            _adapter_kind(self.github_adapter, "providers.github.adapter"),
        )
        object.__setattr__(
            self,
            "slack_adapter",
            _adapter_kind(self.slack_adapter, "providers.slack.adapter"),
        )
        for field_name in ("github_mcp_server", "slack_mcp_server"):
            normalized = str(getattr(self, field_name) or "").strip()
            if not normalized:
                raise ReviewRunnerError(f"{field_name} must be a non-empty string")
            object.__setattr__(self, field_name, normalized)
        notification_channel = str(self.slack_notification_channel_id or "").strip()
        object.__setattr__(
            self,
            "slack_notification_channel_id",
            notification_channel,
        )
        for field_name in (
            "github_repositories",
            "github_reviewer_logins",
            "coderabbit_logins",
            "slack_channel_ids",
            "slack_acknowledgement_user_ids",
        ):
            object.__setattr__(
                self,
                field_name,
                _string_tuple(getattr(self, field_name), field_name),
            )
        for field_name in (
            "enabled",
            "gateway_enabled",
            "github_provider_enabled",
            "github_delivery_enabled",
            "slack_provider_enabled",
            "slack_delivery_enabled",
        ):
            object.__setattr__(
                self,
                field_name,
                is_truthy_value(getattr(self, field_name), default=False),
            )
        if (
            notification_channel
            and notification_channel not in self.slack_channel_ids
        ):
            raise ReviewRunnerError(
                "providers.slack.notification_channel_id must appear in "
                "providers.slack.channel_ids"
            )
        if self.github_delivery_enabled and not self.github_provider_enabled:
            raise ReviewRunnerError(
                "providers.github.delivery_enabled requires providers.github.enabled"
            )
        if self.slack_delivery_enabled and not self.slack_provider_enabled:
            raise ReviewRunnerError(
                "providers.slack.delivery_enabled requires providers.slack.enabled"
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
            provider_timeout_seconds=raw.get(
                "provider_timeout_seconds",
                DEFAULT_PROVIDER_TIMEOUT_SECONDS,
            ),
            github_provider_enabled=is_truthy_value(
                github_cfg.get("enabled"), default=False
            ),
            github_delivery_enabled=is_truthy_value(
                github_cfg.get("delivery_enabled"), default=False
            ),
            slack_provider_enabled=is_truthy_value(
                slack_cfg.get("enabled"), default=False
            ),
            slack_delivery_enabled=is_truthy_value(
                slack_cfg.get("delivery_enabled"), default=False
            ),
            github_adapter=str(github_cfg.get("adapter") or "disabled"),
            github_mcp_server=str(github_cfg.get("mcp_server") or "github"),
            github_repositories=_string_tuple(
                github_cfg.get("repositories"),
                "providers.github.repositories",
            ),
            github_reviewer_logins=_string_tuple(
                github_cfg.get("reviewer_logins"),
                "providers.github.reviewer_logins",
            ),
            coderabbit_logins=_string_tuple(
                github_cfg.get(
                    "coderabbit_logins",
                    ("coderabbitai[bot]", "coderabbitai"),
                ),
                "providers.github.coderabbit_logins",
            ),
            slack_adapter=str(slack_cfg.get("adapter") or "disabled"),
            slack_mcp_server=str(slack_cfg.get("mcp_server") or "slack"),
            slack_channel_ids=_string_tuple(
                slack_cfg.get("channel_ids"),
                "providers.slack.channel_ids",
            ),
            slack_notification_channel_id=str(
                slack_cfg.get("notification_channel_id") or ""
            ),
            slack_acknowledgement_user_ids=_string_tuple(
                slack_cfg.get("acknowledgement_user_ids"),
                "providers.slack.acknowledgement_user_ids",
            ),
        )

    def human_review_runtime_ready(self) -> bool:
        """Return whether the full QA-to-human MCP boundary is explicitly live."""
        return bool(
            self.enabled
            and self.mode == "live"
            and self.github_provider_enabled
            and self.github_delivery_enabled
            and self.github_adapter == "mcp"
            and self.github_repositories
            and self.github_reviewer_logins
            and self.slack_provider_enabled
            and self.slack_delivery_enabled
            and self.slack_adapter == "mcp"
            and self.slack_channel_ids
            and self.slack_notification_channel_id
            and self.slack_acknowledgement_user_ids
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
    """Explicit adapter surface; write-capable transports remain separate."""

    # Every future live adapter must enforce this request-level timeout in its
    # own HTTP/client layer. A runner wall-clock budget alone cannot safely
    # cancel an in-flight side effect, so missing/oversized declarations fail
    # closed before any provider call.
    provider_timeout_seconds: Optional[int] = None
    reconciliation_provider_call_count: int = 1
    reconciliation_snapshot_provider: Optional[
        reconciliation.ReconciliationSnapshotProvider
    ] = None
    github_snapshot_provider: Optional[github.GitHubSnapshotProvider] = None
    coderabbit_snapshot_provider: Optional[coderabbit.CodeRabbitSnapshotProvider] = None
    github_delivery_transport: Optional[github.GitHubDeliveryTransport] = None
    slack_snapshot_provider: Optional[slack.PullRequestSnapshotProvider] = None
    slack_delivery_transport: Optional[slack.SlackDeliveryTransport] = None
    slack_acknowledgement_provider: Optional[slack.SlackAcknowledgementProvider] = None
    mcp_credential_preflight: Optional[Mapping[str, Mapping[str, Any]]] = None

    @property
    def has_registered_adapter(self) -> bool:
        return any(
            adapter is not None
            for adapter in (
                self.reconciliation_snapshot_provider,
                self.github_snapshot_provider,
                self.coderabbit_snapshot_provider,
                self.github_delivery_transport,
                self.slack_snapshot_provider,
                self.slack_delivery_transport,
                self.slack_acknowledgement_provider,
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
        try:
            call_count = int(self.reconciliation_provider_call_count)
        except (TypeError, ValueError):
            return False
        return (
            1 <= timeout <= runner_timeout_seconds
            and 1 <= call_count <= MAX_ITEMS_PER_RUN
        )


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
class _CodeRabbitRecordingSnapshotProvider:
    """Bind CodeRabbit evidence to the exact head returned by the same PR read."""

    conn: sqlite3.Connection
    github_provider: reconciliation.ReconciliationSnapshotProvider
    coderabbit_provider: coderabbit.CodeRabbitSnapshotProvider
    persist: bool
    now: int

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> Optional[github.GitHubPullRequestSnapshot]:
        snapshot = self.github_provider.read_snapshot(
            repository=repository,
            pr_number=pr_number,
        )
        if snapshot is None:
            return None
        review = self.coderabbit_provider.read_review(
            repository=snapshot.repository,
            pr_number=snapshot.pr_number,
            expected_head_sha=snapshot.head_sha,
        )
        if not isinstance(review, coderabbit.CodeRabbitSnapshot):
            raise ReviewRunnerError(
                "CodeRabbit provider returned an invalid normalized snapshot"
            )
        if review.head_sha != snapshot.head_sha:
            raise ReviewRunnerError(
                "CodeRabbit provider returned evidence for a non-current head"
            )
        if self.persist:
            coderabbit.record_snapshot(
                self.conn,
                snapshot=review,
                current_head_sha=snapshot.head_sha,
                current_head_observed_at=snapshot.observed_at,
                now=self.now,
            )
        return snapshot


def _build_configured_mcp_adapters(
    config: ReviewRunnerConfig,
    *,
    clock: Callable[[], float] = time.time,
) -> ReviewRunnerAdapters:
    """Construct read-only MCP adapters only after explicit operator opt-in."""

    github_server = (
        config.github_mcp_server
        if config.github_provider_enabled and config.github_adapter == "mcp"
        else None
    )
    slack_server = (
        config.slack_mcp_server
        if config.slack_provider_enabled and config.slack_adapter == "mcp"
        else None
    )
    if github_server is None and slack_server is None:
        return ReviewRunnerAdapters()

    from hermes_cli.kanban_mcp_adapters import build_review_runner_mcp_bundle

    bundle = build_review_runner_mcp_bundle(
        provider_timeout_seconds=config.provider_timeout_seconds,
        github_server_name=github_server,
        github_repositories=config.github_repositories,
        coderabbit_logins=config.coderabbit_logins,
        github_delivery_enabled=config.github_delivery_enabled,
        slack_server_name=slack_server,
        slack_channel_ids=config.slack_channel_ids,
        slack_user_ids=config.slack_acknowledgement_user_ids,
        slack_delivery_enabled=config.slack_delivery_enabled,
        clock=clock,
    )
    return ReviewRunnerAdapters(
        provider_timeout_seconds=bundle.provider_timeout_seconds,
        # One GitHub snapshot collection is exactly four allowlisted MCP reads;
        # CodeRabbit normalization reuses that same in-memory exact-head bundle.
        reconciliation_provider_call_count=(
            4 if bundle.github_adapter is not None else 1
        ),
        reconciliation_snapshot_provider=bundle.github_adapter,
        github_snapshot_provider=bundle.github_adapter,
        coderabbit_snapshot_provider=bundle.github_adapter,
        github_delivery_transport=bundle.github_delivery_transport,
        slack_snapshot_provider=bundle.github_adapter,
        slack_delivery_transport=bundle.slack_delivery_transport,
        slack_acknowledgement_provider=bundle.slack_acknowledgement_provider,
        mcp_credential_preflight=bundle.credential_preflight,
    )


def _configured_principal(
    principal: Optional[str],
    *,
    prefix: str,
    allowed: Sequence[str],
    field: str,
) -> str:
    raw = str(principal or "").strip()
    expected_prefix = f"{prefix}:"
    if not raw.casefold().startswith(expected_prefix):
        raise ReviewRunnerError(f"{field} must use the {expected_prefix} principal form")
    value = raw[len(expected_prefix):].strip()
    allowlist = {str(item).strip().casefold() for item in allowed}
    if not value or value.casefold() not in allowlist:
        raise ReviewRunnerError(f"{field} is outside the configured allowlist")
    return value


def enqueue_human_review_gate_outboxes(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    snapshot: github.GitHubPullRequestSnapshot,
    config: ReviewRunnerConfig,
    now: Optional[int] = None,
    _within_transaction: bool = False,
) -> dict[str, Any]:
    """Bridge one QA gate into the typed exact-head MCP outboxes.

    This only persists local intents. External writes remain the live runner's
    responsibility and therefore stay behind its independent enable/mode/
    delivery gates.
    """
    if not config.human_review_runtime_ready():
        raise ReviewRunnerError("the complete live human-review runtime is not enabled")
    gate = human_review.get_human_review_gate(conn, gate_id)
    if gate is None:
        raise ReviewRunnerError(f"human-review gate {gate_id!r} does not exist")
    repository = snapshot.repository
    if repository not in {item.casefold() for item in config.github_repositories}:
        raise ReviewRunnerError("gate repository is outside the configured GitHub allowlist")
    reviewer = _configured_principal(
        gate.reviewer_principal,
        prefix="github",
        allowed=config.github_reviewer_logins,
        field="reviewer_principal",
    )
    slack_user = _configured_principal(
        gate.notification_principal,
        prefix="slack",
        allowed=config.slack_acknowledgement_user_ids,
        field="notification_principal",
    )
    changed_at = int(time.time()) if now is None else int(now)
    github_body = "\n".join((
        f"Human review requested from @{reviewer}.",
        f"Linear: {gate.linear_issue_id or 'not linked'}",
        f"PR: {gate.pr_url}",
        f"Approved head: {gate.approved_head_sha}",
        f"Gate: {gate.id}",
        "Review and merge remain human-only. Any new push invalidates this gate.",
    ))
    github_receipt = github.enqueue_intent(
        conn,
        gate_id=gate.id,
        snapshot=snapshot,
        expected_repository=gate.repo,
        expected_pr_number=gate.pr_number,
        expected_head_sha=gate.approved_head_sha,
        surface="pull_request_comments",
        operation="create_comment",
        payload={"body": github_body},
        max_attempts=config.retry_ceiling,
        now=changed_at,
        _within_transaction=_within_transaction,
    )
    slack_body = "\n".join((
        f"<@{slack_user}> exact-head GitHub review is ready.",
        f"Linear: {gate.linear_issue_id or 'not linked'}",
        f"PR: {gate.pr_url}",
        f"Approved head: {gate.approved_head_sha}",
        f"Gate: {gate.id}",
        "Slack acknowledgement is receipt-only; approve or request changes in GitHub.",
    ))
    slack_receipt = slack.enqueue_intent(
        conn,
        gate_id=gate.id,
        snapshot=snapshot,
        expected_repository=gate.repo,
        expected_pr_number=gate.pr_number,
        expected_head_sha=gate.approved_head_sha,
        channel_id=config.slack_notification_channel_id,
        thread_ts=None,
        surface="channel",
        operation="notify_human_review",
        payload={"body": slack_body},
        max_attempts=config.retry_ceiling,
        now=changed_at,
        _within_transaction=_within_transaction,
    )
    slack_ids = [slack_receipt.intent.id]

    # The configured GitHub MCP has no request-reviewer write operation. Keep
    # the legacy row as audit evidence, but never misreport it as delivered.
    if _within_transaction and not conn.in_transaction:
        raise ReviewRunnerError("internal bridge enqueue requires an active transaction")
    scope = nullcontext() if _within_transaction else kb.write_txn(conn)
    with scope:
        conn.execute(
            "UPDATE review_gate_deliveries SET state='superseded', "
            "next_attempt_at=NULL, last_error=?, updated_at=? "
            "WHERE gate_id=? AND channel='github_review_request' "
            "AND state NOT IN ('sent', 'superseded')",
            ("unsupported_by_configured_github_mcp", changed_at, gate.id),
        )
        kb._append_event(
            conn,
            gate.task_id,
            "human_gate_mcp_outboxes_enqueued",
            {
                "gate_id": gate.id,
                "github_intent_id": github_receipt.intent.id,
                "slack_intent_ids": slack_ids,
                "approved_head_sha": gate.approved_head_sha,
                "external_write": False,
            },
        )
    return {
        "github_intent_id": github_receipt.intent.id,
        "slack_intent_ids": slack_ids,
        "created": bool(github_receipt.created),
    }


def _legacy_delivery_state(states: Sequence[str]) -> str:
    if states and all(state == "sent" for state in states):
        return "sent"
    if "permanent_failure" in states:
        return "failed"
    if "superseded" in states:
        return "superseded"
    if "retry" in states:
        return "retry"
    return "pending"


def refresh_human_review_gate_from_outboxes(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    now: int,
) -> str:
    """Project typed MCP outbox outcomes onto the gate state machine."""
    gate = human_review.get_human_review_gate(conn, gate_id)
    if gate is None:
        raise ReviewRunnerError(f"human-review gate {gate_id!r} does not exist")
    github_rows = conn.execute(
        "SELECT state, external_id FROM github_human_review_outbox "
        "WHERE gate_id=? ORDER BY id",
        (gate_id,),
    ).fetchall()
    slack_rows = conn.execute(
        "SELECT state, external_message_ts FROM slack_human_review_outbox "
        "WHERE gate_id=? ORDER BY id",
        (gate_id,),
    ).fetchall()
    if not github_rows or not slack_rows:
        return gate.state
    states = [row["state"] for row in github_rows + slack_rows]
    if "superseded" in states:
        human_review.supersede_human_review_gate(
            conn,
            gate_id,
            reason="authoritative PR readback superseded an exact-head delivery intent",
            now=now,
        )
        refreshed = human_review.get_human_review_gate(conn, gate_id)
        return refreshed.state if refreshed is not None else "superseded"

    github_state = _legacy_delivery_state([row["state"] for row in github_rows])
    slack_state = _legacy_delivery_state([row["state"] for row in slack_rows])
    target = (
        "delivery_failed"
        if "permanent_failure" in states
        else "awaiting_human"
        if all(state == "sent" for state in states)
        else "pending_delivery"
    )
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE review_gate_deliveries SET state=?, external_id=?, updated_at=? "
            "WHERE gate_id=? AND channel='github_comment'",
            (
                github_state,
                next((row["external_id"] for row in github_rows if row["external_id"]), None),
                now,
                gate_id,
            ),
        )
        conn.execute(
            "UPDATE review_gate_deliveries SET state=?, external_id=?, updated_at=? "
            "WHERE gate_id=? AND channel='slack'",
            (
                slack_state,
                next((row["external_message_ts"] for row in slack_rows if row["external_message_ts"]), None),
                now,
                gate_id,
            ),
        )
        if gate.state in {"pending_delivery", "delivery_failed"} and target != gate.state:
            updated = conn.execute(
                "UPDATE human_review_gates SET state=?, updated_at=? "
                "WHERE id=? AND state=?",
                (target, now, gate_id, gate.state),
            )
            if updated.rowcount:
                kb._append_event(
                    conn,
                    gate.task_id,
                    "human_gate_delivery_state_changed",
                    {"gate_id": gate_id, "state": target},
                )
    refreshed = human_review.get_human_review_gate(conn, gate_id)
    return refreshed.state if refreshed is not None else target


def _mark_gate_seen_from_slack(
    conn: sqlite3.Connection,
    *,
    intent: slack.SlackOutboxIntent,
    receipt: slack.SlackAcknowledgementReceipt,
    now: int,
) -> bool:
    gate = human_review.get_human_review_gate(conn, intent.gate_id)
    if (
        gate is None
        or gate.approved_head_sha != intent.head_sha
        or receipt.gate_id != gate.id
        or receipt.head_sha != gate.approved_head_sha
        or receipt.channel_id != intent.channel_id
        or not receipt.acknowledged
    ):
        return False
    notification = str(gate.notification_principal or "").strip()
    if not notification.casefold().startswith("slack:"):
        return False
    expected_user = notification.split(":", 1)[1].strip()
    if not expected_user or receipt.user_id.casefold() != expected_user.casefold():
        return False
    with kb.write_txn(conn):
        updated = conn.execute(
            "UPDATE human_review_gates SET state='seen', updated_at=? "
            "WHERE id=? AND state='awaiting_human'",
            (now, gate.id),
        )
        if not updated.rowcount:
            return False
        kb._append_event(
            conn,
            gate.task_id,
            "human_gate_seen",
            {
                "gate_id": gate.id,
                "actor_principal": f"slack:{receipt.user_id}",
                "acknowledgement_action": receipt.normalized_action,
                "decision_authority": "github_only",
            },
        )
    return True


def _ingest_slack_acknowledgements(
    conn: sqlite3.Connection,
    *,
    provider: slack.SlackAcknowledgementProvider,
    max_items: int,
    now: int,
    monotonic: Callable[[], float],
    deadline: float,
    provider_timeout_seconds: int,
    allowed_channel_ids: Sequence[str] = (),
    advance_human_gate: bool = False,
) -> list[dict[str, Any]]:
    """Read existing stored Slack threads and persist replay-safe local acks."""

    rows = conn.execute(
        "SELECT * FROM slack_human_review_outbox "
        "WHERE state='sent' AND delivered_thread_ts IS NOT NULL "
        "ORDER BY sent_at, id LIMIT ?",
        (max_items,),
    ).fetchall()
    allowed_channels = frozenset(str(item) for item in allowed_channel_ids)
    results: list[dict[str, Any]] = []
    for row in rows:
        intent = slack.SlackOutboxIntent.from_row(row)
        if not intent.delivered_thread_ts:
            continue
        if allowed_channels and intent.channel_id not in allowed_channels:
            results.append({
                "surface": "slack_acknowledgement",
                "intent_id": intent.id,
                "outcome": "channel_outside_allowlist",
                "observed_count": 0,
                "created_count": 0,
                "external_write": False,
            })
            continue
        if deadline - monotonic() < provider_timeout_seconds:
            raise ReviewRunnerDeadlineExceeded(
                "runner deadline cannot accommodate another Slack MCP read"
            )
        events = provider.read_acknowledgements(
            channel_id=intent.channel_id,
            thread_ts=intent.delivered_thread_ts,
        )
        if not isinstance(events, tuple) or any(
            not isinstance(event, slack.SlackAcknowledgementEvent) for event in events
        ):
            raise ReviewRunnerError(
                "Slack acknowledgement provider returned invalid normalized events"
            )
        created = 0
        for event in events:
            receipt = slack.record_acknowledgement(
                conn,
                source_intent_id=intent.id,
                event=event,
                now=now,
            )
            created += int(receipt.created)
            if (
                advance_human_gate
                and receipt.created
                and receipt.receipt.acknowledged
            ):
                _mark_gate_seen_from_slack(
                    conn,
                    intent=intent,
                    receipt=receipt.receipt,
                    now=now,
                )
        results.append({
            "surface": "slack_acknowledgement",
            "intent_id": intent.id,
            "outcome": "recorded" if created else "replayed_or_empty",
            "observed_count": len(events),
            "created_count": created,
            "external_write": False,
        })
    return results


def _ingest_github_human_decisions(
    conn: sqlite3.Connection,
    *,
    provider: reconciliation.ReconciliationSnapshotProvider,
    allowed_reviewer_logins: Sequence[str],
    max_items: int,
    now: int,
) -> list[dict[str, Any]]:
    """Apply exact-head GitHub decisions from explicitly allowlisted reviewers."""
    rows = conn.execute(
        "SELECT * FROM human_review_gates "
        "WHERE state IN ('awaiting_human', 'seen') "
        "ORDER BY updated_at, id LIMIT ?",
        (max_items,),
    ).fetchall()
    results: list[dict[str, Any]] = []
    for row in rows:
        gate = human_review.HumanReviewGate.from_row(row)
        reviewer = _configured_principal(
            gate.reviewer_principal,
            prefix="github",
            allowed=allowed_reviewer_logins,
            field="reviewer_principal",
        )
        snapshot = provider.read_snapshot(
            repository=gate.repo,
            pr_number=gate.pr_number,
        )
        if snapshot is None:
            raise ReviewRunnerError("GitHub provider returned no human-review snapshot")
        github.validate_snapshot_freshness(snapshot, now=now)
        try:
            github.validate_exact_head(
                snapshot,
                expected_head_sha=gate.approved_head_sha,
            )
        except github.GitHubHeadMismatch:
            human_review.supersede_human_review_gate(
                conn,
                gate.id,
                reason="authoritative GitHub review readback observed a new head",
                observed_head_sha=snapshot.head_sha,
                now=now,
            )
            results.append({
                "surface": "github_human_decision",
                "gate_id": gate.id,
                "outcome": "superseded",
                "external_write": False,
            })
            continue
        decisions = github.read_human_review_decisions(
            snapshot,
            expected_head_sha=gate.approved_head_sha,
            reviewer_login=reviewer,
        )
        if not decisions:
            continue
        decision = decisions[-1]
        if decision.submitted_at < gate.qa_approved_at:
            # A current-head review can predate the QA gate. It remains valid
            # GitHub history, but it is not the post-QA human decision this
            # state machine is waiting for.
            continue
        changed = human_review.record_human_review_decision(
            conn,
            gate.id,
            reviewer_principal=gate.reviewer_principal,
            review_state=decision.state.upper(),
            review_head_sha=decision.head_sha,
            external_review_id=decision.review_id,
            now=now,
        )
        results.append({
            "surface": "github_human_decision",
            "gate_id": gate.id,
            "outcome": decision.state if changed else "replayed",
            "external_write": False,
        })
    return results


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
    provider_call_count: int = 1

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> Optional[github.GitHubPullRequestSnapshot]:
        remaining = self.deadline - self.monotonic()
        required_window = self.provider_timeout_seconds * self.provider_call_count
        if remaining < required_window:
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

    A candidate performs one exact-head snapshot collection plus an
    idempotency readback, one send, and a final readback if send raises. The
    snapshot collection can itself span several MCP requests (four for the
    configured GitHub adapter), so include its declared call count rather than
    treating it as one request window. This prevents a runner timeout from
    expiring its board lease in the middle of an external delivery attempt.
    """
    if candidate.surface == "github":
        ready = (
            config.github_provider_enabled
            and config.github_delivery_enabled
            and adapters.github_snapshot_provider is not None
            and adapters.github_delivery_transport is not None
        )
    else:
        ready = (
            config.slack_provider_enabled
            and config.slack_delivery_enabled
            and adapters.slack_snapshot_provider is not None
            and adapters.slack_delivery_transport is not None
        )
    return adapters.reconciliation_provider_call_count + 3 if ready else 0


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


def probe_review_runner_resources(
    *,
    config: ReviewRunnerConfig,
    repository: str,
    pr_number: int,
    expected_head_sha: str,
    channel_id: Optional[str] = None,
    thread_ts: Optional[str] = None,
    adapters: Optional[ReviewRunnerAdapters] = None,
    now: Optional[int] = None,
) -> dict[str, Any]:
    """Probe one explicit staging route without provider or local writes."""

    checked_at = int(time.time()) if now is None else int(now)
    registered = adapters or _build_configured_mcp_adapters(
        config,
        clock=lambda: float(checked_at),
    )
    provider = registered.github_snapshot_provider
    if provider is None:
        raise ReviewRunnerError(
            "staging requires a registered authoritative GitHub snapshot provider"
        )
    snapshot = provider.read_snapshot(
        repository=repository,
        pr_number=pr_number,
    )
    if not isinstance(snapshot, github.GitHubPullRequestSnapshot):
        raise ReviewRunnerError("GitHub staging probe returned an invalid snapshot")
    expected_repository = str(repository or "").strip().casefold()
    if snapshot.repository != expected_repository or snapshot.pr_number != int(
        pr_number
    ):
        raise ReviewRunnerError(
            "GitHub staging probe returned a different repository or pull request"
        )
    github.validate_snapshot_freshness(snapshot, now=checked_at)
    github.validate_exact_head(snapshot, expected_head_sha=expected_head_sha)

    coderabbit_payload: Optional[dict[str, Any]] = None
    if registered.coderabbit_snapshot_provider is not None:
        review = registered.coderabbit_snapshot_provider.read_review(
            repository=snapshot.repository,
            pr_number=snapshot.pr_number,
            expected_head_sha=snapshot.head_sha,
        )
        if not isinstance(review, coderabbit.CodeRabbitSnapshot):
            raise ReviewRunnerError(
                "CodeRabbit staging probe returned invalid evidence"
            )
        coderabbit_payload = {
            "provider": review.provider,
            "observation_id": review.observation_id,
            "head_sha": review.head_sha,
            "state": coderabbit.assess_snapshot(
                review,
                current_head_sha=snapshot.head_sha,
                now=checked_at,
            ).state,
        }

    normalized_channel = str(channel_id or "").strip()
    normalized_thread = str(thread_ts or "").strip()
    if bool(normalized_channel) != bool(normalized_thread):
        raise ReviewRunnerError(
            "Slack staging probe requires both --channel-id and --thread-ts"
        )
    slack_payload: Optional[dict[str, Any]] = None
    if config.slack_provider_enabled:
        if not normalized_channel or not normalized_thread:
            raise ReviewRunnerError(
                "enabled Slack staging requires an explicit channel ID and thread_ts"
            )
        if registered.slack_acknowledgement_provider is None:
            raise ReviewRunnerError(
                "staging requires a registered Slack acknowledgement provider"
            )
        events = registered.slack_acknowledgement_provider.read_acknowledgements(
            channel_id=normalized_channel,
            thread_ts=normalized_thread,
        )
        slack_payload = {
            "channel_id": normalized_channel,
            "thread_ts": normalized_thread,
            "acknowledgement_event_count": len(events),
            "event_ids": sorted(item.event_id for item in events),
        }

    return {
        "schema_version": 1,
        "status": "ready",
        "checked_at": checked_at,
        "mode": "staging",
        "script_only": True,
        "external_provider_writes": False,
        "local_writes": False,
        "github": {
            "repository": snapshot.repository,
            "pr_number": snapshot.pr_number,
            "pr_url": snapshot.pr_url,
            "base_ref": snapshot.base_ref,
            "head_ref": snapshot.head_ref,
            "head_sha": snapshot.head_sha,
            "snapshot_sha256": snapshot.snapshot_sha256(),
            "review_count": len(snapshot.reviews),
            "review_thread_count": len(snapshot.review_threads),
            "delivery_registered": registered.github_delivery_transport is not None,
        },
        "coderabbit": coderabbit_payload,
        "slack": slack_payload,
        "receipt_delivery": {
            "github": (
                "registered_not_sent"
                if registered.github_delivery_transport is not None
                else "disabled"
            ),
            "slack": (
                "registered_not_sent"
                if registered.slack_delivery_transport is not None
                else "disabled"
            ),
        },
        "merge_or_branch_write_capability": False,
    }


def diagnose_review_runner(
    conn: sqlite3.Connection,
    *,
    config: ReviewRunnerConfig,
    now: Optional[int] = None,
    adapters: Optional[ReviewRunnerAdapters] = None,
    adapter_setup_failure: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    checked_at = int(time.time()) if now is None else int(now)
    registered = adapters or ReviewRunnerAdapters()
    redacted_setup_failure = _redacted_adapter_setup_failure(adapter_setup_failure)
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
    github_write_registered = (
        registered.github_snapshot_provider is not None
        and registered.github_delivery_transport is not None
    )
    github_read_registered = (
        registered.github_snapshot_provider is not None
        and registered.coderabbit_snapshot_provider is not None
    )
    slack_write_registered = (
        registered.slack_snapshot_provider is not None
        and registered.slack_delivery_transport is not None
    )
    slack_read_registered = registered.slack_acknowledgement_provider is not None
    timeout_bounded = registered.timeout_is_bounded(
        runner_timeout_seconds=config.timeout_seconds
    )
    github_connectivity = (
        "disabled"
        if not config.github_provider_enabled
        else "registered_unprobed"
        if github_read_registered or github_write_registered
        else "adapter_not_registered"
    )
    slack_connectivity = (
        "disabled"
        if not config.slack_provider_enabled
        else "registered_unprobed"
        if slack_read_registered or slack_write_registered
        else "adapter_not_registered"
    )
    enabled_provider_count = sum((
        config.github_provider_enabled,
        config.slack_provider_enabled,
    ))
    github_shadow_ready = (
        config.github_provider_enabled
        and config.github_adapter == "mcp"
        and bool(config.github_repositories)
        and github_read_registered
        and timeout_bounded
    )
    slack_shadow_ready = (
        config.slack_provider_enabled
        and config.slack_adapter == "mcp"
        and bool(config.slack_channel_ids)
        and bool(config.slack_acknowledgement_user_ids)
        and slack_read_registered
        and timeout_bounded
    )
    human_review_policy_ready = config.human_review_runtime_ready()
    all_enabled_reads_ready = (
        enabled_provider_count > 0
        and (not config.github_provider_enabled or github_shadow_ready)
        and (not config.slack_provider_enabled or slack_shadow_ready)
    )
    all_enabled_writes_ready = (
        enabled_provider_count > 0
        and (not config.github_provider_enabled or github_write_registered)
        and (not config.slack_provider_enabled or slack_write_registered)
    )
    blockers: list[str] = []
    if redacted_setup_failure:
        blockers.append(
            "adapter_setup_failed:"
            f"{redacted_setup_failure['kind']}:"
            f"{redacted_setup_failure['type']}"
        )
    if not config.enabled:
        blockers.append("runner_disabled")
    if enabled_provider_count == 0:
        blockers.append("no_provider_enabled")
    if config.github_provider_enabled:
        if config.github_adapter != "mcp":
            blockers.append("github_mcp_adapter_not_selected")
        if not config.github_repositories:
            blockers.append("github_repository_allowlist_required")
        if not config.github_reviewer_logins:
            blockers.append("github_reviewer_login_allowlist_required")
        if not github_read_registered:
            blockers.append("github_read_adapter_not_registered")
        blockers.append("github_private_repository_authorization_unprobed")
        if not config.github_delivery_enabled:
            blockers.append("github_delivery_disabled")
        elif not github_write_registered:
            blockers.append("github_delivery_adapter_not_registered")
    if config.slack_provider_enabled:
        if config.slack_adapter != "mcp":
            blockers.append("slack_mcp_adapter_not_selected")
        if not config.slack_channel_ids:
            blockers.append("slack_channel_allowlist_required")
        if not config.slack_notification_channel_id:
            blockers.append("slack_notification_channel_id_required")
        if not config.slack_acknowledgement_user_ids:
            blockers.append("slack_acknowledgement_user_allowlist_required")
        if not slack_read_registered:
            blockers.append("slack_read_adapter_not_registered")
        blockers.append("slack_resource_authorization_unprobed")
        if not config.slack_delivery_enabled:
            blockers.append("slack_delivery_disabled")
        elif not slack_write_registered:
            blockers.append("slack_delivery_adapter_not_registered")
    if not github_write_registered and not slack_write_registered:
        blockers.append("live_delivery_transport_not_registered")
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
            "provider_timeout_seconds": config.provider_timeout_seconds,
            "external_writes_enabled": (
                github_write_registered or slack_write_registered
            ),
            "human_review_policy_ready": human_review_policy_ready,
        },
        "providers": {
            "github": {
                "enabled": config.github_provider_enabled,
                "delivery_enabled": config.github_delivery_enabled,
                "adapter": config.github_adapter,
                "read_registered": github_read_registered,
                "write_registered": github_write_registered,
                "repository_allowlist_count": len(config.github_repositories),
                "reviewer_login_allowlist_count": len(
                    config.github_reviewer_logins
                ),
                "connectivity": github_connectivity,
                "connectivity_probe_performed": False,
                "ready": (
                    config.github_provider_enabled
                    and github_read_registered
                    and timeout_bounded
                ),
            },
            "slack": {
                "enabled": config.slack_provider_enabled,
                "delivery_enabled": config.slack_delivery_enabled,
                "adapter": config.slack_adapter,
                "read_registered": slack_read_registered,
                "write_registered": slack_write_registered,
                "channel_allowlist_count": len(config.slack_channel_ids),
                "notification_channel_configured": bool(
                    config.slack_notification_channel_id
                ),
                "acknowledgement_user_allowlist_count": len(
                    config.slack_acknowledgement_user_ids
                ),
                "connectivity": slack_connectivity,
                "connectivity_probe_performed": False,
                "ready": (
                    config.slack_provider_enabled
                    and slack_read_registered
                    and timeout_bounded
                ),
            },
            "provider_timeout_seconds": registered.provider_timeout_seconds,
            "timeout_bounded": timeout_bounded,
            "credential_preflight": registered.mcp_credential_preflight or {},
            "adapter_setup_failure": redacted_setup_failure,
        },
        "readiness": {
            "dry_run_ready": timeout_bounded,
            "shadow_ready": config.enabled and all_enabled_reads_ready,
            "live_ready": config.enabled
            and all_enabled_writes_ready
            and timeout_bounded,
            "human_review_ready": human_review_policy_ready
            and github_read_registered
            and github_write_registered
            and slack_read_registered
            and slack_write_registered
            and timeout_bounded,
            "production_ready": False,
            "blockers": blockers,
        },
        "provider_limitations": {
            "github_null_review_commit_id": "fail_closed",
            "github_null_review_comment_commit_id": "fail_closed",
            "github_authoritative_thread_resolution": False,
            "github_thread_policy": (
                "current review comments remain unresolved/actionable until an "
                "authoritative thread-resolution provider is available"
            ),
            "github_write_tools_registered": github_write_registered,
            "github_request_reviewer_supported": False,
            "slack_write_tools_registered": slack_write_registered,
            "slack_is_approval_authority": False,
            "merge_or_branch_write_capability": False,
        },
        "operator_input_checklist": [
            "approved private repository owner/name allowlist",
            "approved GitHub reviewer login allowlist",
            "one approved Slack notification channel ID included in the read allowlist",
            "approved Slack acknowledgement user IDs",
            "GitHub and Slack credentials stored as env/OAuth references, never plaintext config",
            "resource probes proving private-repo and Slack channel/thread access",
            "separate approval before any live delivery adapter or cron/gateway routing change",
        ],
        "shadow_validation": {
            "command": (
                "hermes kanban review-runner run --mode shadow "
                "--linear-issue-id <issue-id> --json"
            ),
            "external_provider_writes": False,
            "local_writes": "immutable reconciliation and acknowledgement audit rows",
            "success_evidence": [
                "status is ok or no_op",
                "errors is empty",
                "private GitHub repository snapshot is returned for the exact head",
                "CodeRabbit evidence is classified without null commit_id",
                "Slack acknowledgement reads stay inside configured channel/user allowlists",
            ],
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
        if not config.github_delivery_enabled:
            return None, {
                "surface": "github",
                "intent_id": candidate.intent_id,
                "reason": "delivery_disabled",
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
        intent = github.get_intent(conn, candidate.intent_id)
        if intent is None:
            raise ReviewRunnerError(
                f"GitHub outbox intent {candidate.intent_id!r} does not exist"
            )
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
        if not config.slack_delivery_enabled:
            return None, {
                "surface": "slack",
                "intent_id": candidate.intent_id,
                "reason": "delivery_disabled",
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
        intent = slack.get_intent(conn, candidate.intent_id)
        if intent is None:
            raise ReviewRunnerError(
                f"Slack outbox intent {candidate.intent_id!r} does not exist"
            )
        result = slack.process_intent(
            conn,
            candidate.intent_id,
            snapshot_provider=adapters.slack_snapshot_provider,
            delivery_transport=adapters.slack_delivery_transport,
            now=now,
        )
    gate_state = None
    if human_review.get_human_review_gate(conn, intent.gate_id) is not None:
        gate_state = refresh_human_review_gate_from_outboxes(
            conn,
            gate_id=intent.gate_id,
            now=now,
        )
    return {
        "surface": candidate.surface,
        "gate_state": gate_state,
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
    candidates, _counts = list_runner_candidates(
        conn,
        now=started_at,
        max_items=config.max_items,
        retry_ceiling=config.retry_ceiling,
    )
    mode = config.mode
    if mode != "dry-run" and not config.enabled:
        return ReviewRunnerReceipt(
            "disabled",
            mode,
            False,
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
    if adapters is None and mode != "dry-run":
        try:
            effective_adapters = _build_configured_mcp_adapters(
                config,
                clock=lambda: float(started_at),
            )
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as exc:
            failure = _redacted_adapter_setup_failure(exc)
            error = (
                "configured MCP adapter setup failed: "
                f"{failure['type']} ({failure['kind']})"
            )
            return ReviewRunnerReceipt(
                "failed",
                mode,
                False,
                None,
                None,
                None,
                None,
                0,
                candidates,
                (),
                (),
                (error,),
            )
    else:
        effective_adapters = adapters or ReviewRunnerAdapters()
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
        if (
            mode != "dry-run"
            and effective_adapters.slack_acknowledgement_provider is not None
        ):
            assert effective_adapters.provider_timeout_seconds is not None
            results.extend(
                _ingest_slack_acknowledgements(
                    conn,
                    provider=effective_adapters.slack_acknowledgement_provider,
                    max_items=config.max_items,
                    now=started_at,
                    monotonic=monotonic,
                    deadline=deadline,
                    provider_timeout_seconds=int(
                        effective_adapters.provider_timeout_seconds
                    ),
                    allowed_channel_ids=config.slack_channel_ids,
                    advance_human_gate=(
                        mode == "live" and config.human_review_runtime_ready()
                    ),
                )
            )
        provider = effective_adapters.reconciliation_snapshot_provider
        if provider is None:
            provider = effective_adapters.github_snapshot_provider
        if provider is None:
            provider = _DisabledReconciliationSnapshotProvider()
        else:
            assert effective_adapters.provider_timeout_seconds is not None
            if effective_adapters.coderabbit_snapshot_provider is not None:
                provider = _CodeRabbitRecordingSnapshotProvider(
                    conn,
                    provider,
                    effective_adapters.coderabbit_snapshot_provider,
                    mode in {"shadow", "live"},
                    started_at,
                )
            provider = _DeadlineSnapshotProvider(
                provider,
                monotonic,
                deadline,
                int(effective_adapters.provider_timeout_seconds),
                effective_adapters.reconciliation_provider_call_count,
            )
        if (
            mode == "live"
            and effective_adapters.github_snapshot_provider is not None
            and config.human_review_runtime_ready()
        ):
            assert effective_adapters.provider_timeout_seconds is not None
            decision_provider = _DeadlineSnapshotProvider(
                effective_adapters.github_snapshot_provider,
                monotonic,
                deadline,
                int(effective_adapters.provider_timeout_seconds),
                effective_adapters.reconciliation_provider_call_count,
            )
            results.extend(
                _ingest_github_human_decisions(
                    conn,
                    provider=decision_provider,
                    allowed_reviewer_logins=config.github_reviewer_logins,
                    max_items=config.max_items,
                    now=started_at,
                )
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
        elif (
            mode == "live"
            and completed_execution.report.status != "healthy"
            and any(
                _candidate_provider_call_count(
                    candidate,
                    config=config,
                    adapters=effective_adapters,
                )
                for candidate in candidates
            )
        ):
            reconciliation_status = completed_execution.report.status
            errors.append(f"reconciliation_not_healthy:{reconciliation_status}")
            skipped.extend(
                {
                    "reason": "reconciliation_not_healthy",
                    "reconciliation_status": reconciliation_status,
                    "surface": candidate.surface,
                    "intent_id": candidate.intent_id,
                }
                for candidate in candidates
            )
            status = "failed"
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
        kind = getattr(exc, "kind", None)
        error = f"unexpected {type(exc).__name__}"
        if kind:
            error += f" ({kind})"
        elif isinstance(exc, ReviewRunnerError):
            error += f": {str(exc)[:200]}"
        errors.append(error)
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
