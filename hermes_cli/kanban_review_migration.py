"""Dry-run-first migration tooling for exact-head human-review records.

The planner consumes the same normalized, authoritative GitHub snapshots and
reconciliation evidence as :mod:`hermes_cli.kanban_reconciliation`. It never
infers repository, PR, or head identity from task titles or prose. Planning is
pure and deterministic. Applying a plan only mutates local Kanban SQLite rows,
requires an exact operator confirmation token, re-reads the authoritative PR
snapshot before every checkpoint, and has no provider-write capability.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Literal, Mapping, Optional, Protocol, Sequence

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as github
from hermes_cli import kanban_human_review as human_review
from hermes_cli import kanban_reconciliation as reconciliation
from hermes_cli import kanban_review_runner as review_runner


SCHEMA_VERSION = 1
POLICY_VERSION = "echlon-srdja-review-migration-v1"
PLAN_PREFIX = "rmp_"
ACTION_PREFIX = "rma_"
Classification = Literal[
    "current_head",
    "stale",
    "terminal",
    "duplicate",
    "orphan",
    "ambiguous",
]
ActionKind = Literal[
    "backfill_gate_delivery",
    "suppress_gate_bundle",
    "suppress_outbox_bundle",
]
PlanStatus = Literal[
    "prepared",
    "in_progress",
    "completed",
    "rolled_back",
    "rollback_blocked",
]
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_ACTIVE_OUTBOX_STATES = frozenset({"pending", "retry"})
_TERMINAL_OUTBOX_STATES = frozenset({"sent", "superseded", "permanent_failure"})
_HARD_FINDING_FRAGMENTS = (
    "duplicate",
    "conflict",
    "without_linear_pr_link",
    "missing_human_task",
    "missing_implementation_task",
    "missing_qa_task",
    "without_gate",
    "without_intent",
    "without_source_intent",
    "destination_conflict",
)
_SAFE_FINDING_CODES = frozenset({
    "active_exact_head_human_gate",
    "active_gate_head_is_stale",
    "github_outbox_head_is_stale",
    "slack_outbox_head_is_stale",
    "stored_pr_head_is_stale",
    "coderabbit_head_is_stale",
    "github_pr_closed",
    "github_pr_merged",
    "active_gate_on_closed_pr",
    "active_gate_on_merged_pr",
})
_MIGRATION_SNAPSHOT_PROVIDER: Optional["MigrationSnapshotProvider"] = None


class MigrationBoundaryError(ValueError):
    """Migration input, plan, or checkpoint violates a safety invariant."""


class MigrationConfirmationRequired(MigrationBoundaryError):
    """Write mode was requested without the plan-specific confirmation token."""


class MigrationConflict(MigrationBoundaryError):
    """Source truth changed after planning or a checkpoint no longer matches."""


class MigrationSnapshotProvider(Protocol):
    """Read-only authoritative GitHub snapshot provider."""

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> Optional[github.GitHubPullRequestSnapshot]: ...


class UnavailableSnapshotProvider:
    """Fail-closed provider used by the standalone CLI unless explicitly wired."""

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> Optional[github.GitHubPullRequestSnapshot]:
        del repository, pr_number
        return None


def register_migration_snapshot_provider(
    provider: Optional[MigrationSnapshotProvider],
) -> None:
    """Register a process-local read-only provider; the default remains disabled."""
    global _MIGRATION_SNAPSHOT_PROVIDER
    _MIGRATION_SNAPSHOT_PROVIDER = provider


def migration_snapshot_provider() -> Optional[MigrationSnapshotProvider]:
    return _MIGRATION_SNAPSHOT_PROVIDER


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _semantic_evidence(values: Sequence[str]) -> tuple[str, ...]:
    """Remove read-observation transients while preserving source identity."""
    normalized: set[str] = set()
    for value in values:
        if value.startswith(("reconciliation:input:", "reconciliation:report:")):
            continue
        if value.startswith("github:") and ":snapshot-sha256:" in value:
            normalized.add(
                "github:snapshot-sha256:" + value.rsplit(":snapshot-sha256:", 1)[1]
            )
            continue
        if value.startswith("reconciliation:finding:"):
            normalized.add("reconciliation:finding-code:" + value.rsplit(":", 1)[1])
            continue
        normalized.add(value)
    return tuple(sorted(normalized))


def _full_sha(value: str, field: str) -> str:
    normalized = str(value or "").strip().casefold()
    if not _FULL_SHA_RE.fullmatch(normalized):
        raise MigrationBoundaryError(f"{field} must be a full lowercase commit SHA")
    return normalized


def _ref(repository: str, pr_number: int) -> reconciliation.PullRequestIdentity:
    return reconciliation.PullRequestIdentity(repository, pr_number)


@dataclass(frozen=True)
class MigrationRecord:
    classification: Classification
    entity_type: str
    entity_id: str
    repository: Optional[str]
    pr_number: Optional[int]
    stored_head_sha: Optional[str]
    authoritative_head_sha: Optional[str]
    reason: str
    source_evidence: tuple[str, ...]
    proposed_action_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.classification not in {
            "current_head",
            "stale",
            "terminal",
            "duplicate",
            "orphan",
            "ambiguous",
        }:
            raise MigrationBoundaryError(
                f"unsupported migration classification: {self.classification!r}"
            )
        if (
            not self.entity_type.strip()
            or not self.entity_id.strip()
            or not self.reason.strip()
        ):
            raise MigrationBoundaryError(
                "migration records require entity identity and reason"
            )
        if self.stored_head_sha is not None:
            object.__setattr__(
                self,
                "stored_head_sha",
                _full_sha(self.stored_head_sha, "stored_head_sha"),
            )
        if self.authoritative_head_sha is not None:
            object.__setattr__(
                self,
                "authoritative_head_sha",
                _full_sha(self.authoritative_head_sha, "authoritative_head_sha"),
            )
        object.__setattr__(
            self, "source_evidence", tuple(sorted(set(self.source_evidence)))
        )
        object.__setattr__(
            self,
            "proposed_action_ids",
            tuple(sorted(set(self.proposed_action_ids))),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "classification": self.classification,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "stored_head_sha": self.stored_head_sha,
            "authoritative_head_sha": self.authoritative_head_sha,
            "reason": self.reason,
            "source_evidence": list(self.source_evidence),
            "proposed_action_ids": list(self.proposed_action_ids),
        }

    def identity_dict(self) -> dict[str, Any]:
        value = self.to_dict()
        value["source_evidence"] = list(_semantic_evidence(self.source_evidence))
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MigrationRecord":
        return cls(
            classification=str(value["classification"]),  # type: ignore[arg-type]
            entity_type=str(value["entity_type"]),
            entity_id=str(value["entity_id"]),
            repository=(str(value["repository"]) if value.get("repository") else None),
            pr_number=(int(value["pr_number"]) if value.get("pr_number") else None),
            stored_head_sha=(
                str(value["stored_head_sha"]) if value.get("stored_head_sha") else None
            ),
            authoritative_head_sha=(
                str(value["authoritative_head_sha"])
                if value.get("authoritative_head_sha")
                else None
            ),
            reason=str(value["reason"]),
            source_evidence=tuple(
                str(item) for item in value.get("source_evidence", ())
            ),
            proposed_action_ids=tuple(
                str(item) for item in value.get("proposed_action_ids", ())
            ),
        )


@dataclass(frozen=True)
class MigrationAction:
    action_id: str
    idempotency_key: str
    kind: ActionKind
    target_type: str
    target_ids: tuple[str, ...]
    repository: str
    pr_number: int
    head_sha: str
    required_pr_state: str
    source_evidence: tuple[str, ...]
    before: dict[str, Any]
    after: dict[str, Any]
    rollback: dict[str, Any]

    def __post_init__(self) -> None:
        if self.kind not in {
            "backfill_gate_delivery",
            "suppress_gate_bundle",
            "suppress_outbox_bundle",
        }:
            raise MigrationBoundaryError(f"unsupported migration action: {self.kind!r}")
        if not self.action_id.startswith(ACTION_PREFIX):
            raise MigrationBoundaryError("migration action ID has an invalid prefix")
        if not self.idempotency_key.strip() or not self.target_ids:
            raise MigrationBoundaryError("migration action identity is incomplete")
        if self.required_pr_state not in {"open", "closed", "merged"}:
            raise MigrationBoundaryError("migration action PR state is unsupported")
        object.__setattr__(self, "head_sha", _full_sha(self.head_sha, "head_sha"))
        object.__setattr__(self, "target_ids", tuple(sorted(set(self.target_ids))))
        object.__setattr__(
            self, "source_evidence", tuple(sorted(set(self.source_evidence)))
        )

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "kind": self.kind,
            "target_type": self.target_type,
            "target_ids": list(self.target_ids),
            "repository": self.repository,
            "pr_number": self.pr_number,
            "head_sha": self.head_sha,
            "required_pr_state": self.required_pr_state,
            "source_evidence": list(self.source_evidence),
            "before": self.before,
            "after": self.after,
            "rollback": self.rollback,
        }

    def identity_dict(self) -> dict[str, Any]:
        value = self.semantic_dict()
        value["source_evidence"] = list(_semantic_evidence(self.source_evidence))
        return value

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "idempotency_key": self.idempotency_key,
            **self.semantic_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MigrationAction":
        action = cls(
            action_id=str(value["action_id"]),
            idempotency_key=str(value["idempotency_key"]),
            kind=str(value["kind"]),  # type: ignore[arg-type]
            target_type=str(value["target_type"]),
            target_ids=tuple(str(item) for item in value.get("target_ids", ())),
            repository=str(value["repository"]),
            pr_number=int(value["pr_number"]),
            head_sha=str(value["head_sha"]),
            required_pr_state=str(value["required_pr_state"]),
            source_evidence=tuple(
                str(item) for item in value.get("source_evidence", ())
            ),
            before=dict(value.get("before") or {}),
            after=dict(value.get("after") or {}),
            rollback=dict(value.get("rollback") or {}),
        )
        expected = _action_id(action.identity_dict())
        if action.action_id != expected:
            raise MigrationBoundaryError(
                "stored migration action ID does not match content"
            )
        expected_key = f"review-migration-action:v1:{action.action_id}"
        if action.idempotency_key != expected_key:
            raise MigrationBoundaryError(
                "stored migration action idempotency key does not match content"
            )
        return action


@dataclass(frozen=True)
class MigrationPlan:
    plan_id: str
    idempotency_key: str
    reconciliation_input_sha256: str
    reconciliation_report_sha256: str
    records: tuple[MigrationRecord, ...]
    actions: tuple[MigrationAction, ...]
    blocked_reasons: tuple[str, ...]

    @property
    def blocked(self) -> bool:
        return bool(self.blocked_reasons)

    @property
    def apply_confirmation(self) -> str:
        return f"APPLY {self.plan_id}"

    @property
    def rollback_confirmation(self) -> str:
        return f"ROLLBACK {self.plan_id}"

    def semantic_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "reconciliation_input_sha256": self.reconciliation_input_sha256,
            "reconciliation_report_sha256": self.reconciliation_report_sha256,
            "records": [item.to_dict() for item in self.records],
            "actions": [item.to_dict() for item in self.actions],
            "blocked_reasons": list(self.blocked_reasons),
        }

    def identity_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "records": [item.identity_dict() for item in self.records],
            "actions": [item.identity_dict() for item in self.actions],
            "blocked_reasons": list(self.blocked_reasons),
        }

    def to_dict(self) -> dict[str, Any]:
        counts = {
            name: 0
            for name in (
                "current_head",
                "stale",
                "terminal",
                "duplicate",
                "orphan",
                "ambiguous",
            )
        }
        for record in self.records:
            counts[record.classification] += 1
        return {
            "plan_id": self.plan_id,
            "idempotency_key": self.idempotency_key,
            **self.semantic_dict(),
            "status": "blocked" if self.blocked else "ready",
            "classification_counts": counts,
            "action_count": len(self.actions),
            "apply_confirmation": self.apply_confirmation,
            "rollback_confirmation": self.rollback_confirmation,
            "safety": {
                "default_mode": "dry-run",
                "read_only": True,
                "external_side_effects": "none",
                "provider_writes": False,
                "linear_mutation": False,
                "github_write": False,
                "slack_post": False,
                "merge": False,
                "human_cards_dispatchable": False,
            },
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    def plan_sha256(self) -> str:
        return _sha256_text(_canonical_json(self.identity_dict()))

    def to_markdown(self) -> str:
        counts = self.to_dict()["classification_counts"]
        lines = [
            "# Echlon Srdja review migration dry-run report",
            "",
            f"- Status: `{'blocked' if self.blocked else 'ready'}`",
            f"- Plan ID: `{self.plan_id}`",
            f"- Policy: `{POLICY_VERSION}`",
            f"- Reconciliation input SHA-256: `{self.reconciliation_input_sha256}`",
            f"- Reconciliation report SHA-256: `{self.reconciliation_report_sha256}`",
            f"- Records inventoried: {len(self.records)}",
            f"- Proposed local mutations: {len(self.actions)}",
            "- Default execution: read-only dry-run",
            "- External side effects: none",
            "",
            "## Classification summary",
            "",
        ]
        for name in (
            "current_head",
            "stale",
            "terminal",
            "duplicate",
            "orphan",
            "ambiguous",
        ):
            lines.append(f"- `{name}`: {counts[name]}")
        lines.extend(["", "## Inventory", ""])
        if not self.records:
            lines.append(
                "No legacy human-review, outbox, or evidence records were found."
            )
        else:
            lines.extend([
                "| Classification | Entity | ID | Repo / PR | Stored head | Authoritative head | Evidence | Reason | Actions |",
                "|---|---|---|---|---|---|---|---|---|",
            ])
            for record in self.records:
                repo_pr = (
                    f"{record.repository}#{record.pr_number}"
                    if record.repository and record.pr_number is not None
                    else "—"
                )
                lines.append(
                    "| "
                    + " | ".join(
                        _markdown_cell(value)
                        for value in (
                            record.classification,
                            record.entity_type,
                            record.entity_id,
                            repo_pr,
                            record.stored_head_sha or "—",
                            record.authoritative_head_sha or "—",
                            ", ".join(record.source_evidence) or "—",
                            record.reason,
                            ", ".join(record.proposed_action_ids) or "none",
                        )
                    )
                    + " |"
                )
        lines.extend(["", "## Proposed mutations", ""])
        if not self.actions:
            lines.append("No mutation is proposed.")
        else:
            lines.extend([
                "| Action | Kind | Target IDs | Repo / PR / authoritative full SHA | Source evidence | Rollback / recovery |",
                "|---|---|---|---|---|---|",
            ])
            for action in self.actions:
                identity = f"{action.repository}#{action.pr_number} / {action.head_sha}"
                rollback = action.rollback.get("path") or action.rollback.get("reason")
                lines.append(
                    "| "
                    + " | ".join(
                        _markdown_cell(value)
                        for value in (
                            action.action_id,
                            action.kind,
                            ", ".join(action.target_ids),
                            identity,
                            ", ".join(action.source_evidence),
                            rollback,
                        )
                    )
                    + " |"
                )
        lines.extend(["", "## Blocked and ambiguous cases", ""])
        if self.blocked_reasons:
            lines.extend(f"- {reason}" for reason in self.blocked_reasons)
        else:
            lines.append(
                "None. Write mode still requires fresh exact-head readback and confirmation."
            )
        lines.extend([
            "",
            "## Write, resume, and rollback controls",
            "",
            f"- Apply token: `{self.apply_confirmation}`",
            f"- Rollback token: `{self.rollback_confirmation}`",
            "- Each action is a local SQLite transaction and durable checkpoint.",
            "- A rerun skips applied action IDs and resumes pending checkpoints.",
            "- Insert-only delivery backfills have automatic rollback; stale/terminal suppression has manual recovery metadata and is never auto-reactivated.",
            "",
            "## Safety boundary",
            "",
            "This plan never infers a head from a task title, body, Linear prose, or URL. "
            "Write mode cannot merge, approve, notify, post to Slack, mutate Linear or GitHub, "
            "or dispatch human-only cards. It only updates the local Kanban SQLite rows named "
            "in a checkpoint after re-reading the exact authoritative PR head.",
            "",
        ])
        return "\n".join(lines)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MigrationPlan":
        plan = cls(
            plan_id=str(value["plan_id"]),
            idempotency_key=str(value["idempotency_key"]),
            reconciliation_input_sha256=str(value["reconciliation_input_sha256"]),
            reconciliation_report_sha256=str(value["reconciliation_report_sha256"]),
            records=tuple(
                MigrationRecord.from_dict(item) for item in value.get("records", ())
            ),
            actions=tuple(
                MigrationAction.from_dict(item) for item in value.get("actions", ())
            ),
            blocked_reasons=tuple(
                str(item) for item in value.get("blocked_reasons", ())
            ),
        )
        expected_id = _plan_id(plan.identity_dict())
        if plan.plan_id != expected_id:
            raise MigrationBoundaryError(
                "stored migration plan ID does not match content"
            )
        expected_key = f"review-migration-plan:v1:{plan.plan_id}"
        if plan.idempotency_key != expected_key:
            raise MigrationBoundaryError(
                "stored migration plan idempotency key does not match content"
            )
        return plan


@dataclass(frozen=True)
class MigrationInputs:
    reconciliation_inputs: reconciliation.ReconciliationInputs
    legacy_human_tasks: tuple[reconciliation.KanbanTaskState, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "legacy_human_tasks",
            tuple(sorted(self.legacy_human_tasks, key=lambda item: item.task_id)),
        )


@dataclass(frozen=True)
class MigrationExecutionReceipt:
    plan_id: str
    status: PlanStatus
    applied_action_ids: tuple[str, ...]
    skipped_action_ids: tuple[str, ...]
    pending_action_ids: tuple[str, ...]
    checkpoint_count: int
    external_side_effects: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "applied_action_ids": list(self.applied_action_ids),
            "skipped_action_ids": list(self.skipped_action_ids),
            "pending_action_ids": list(self.pending_action_ids),
            "checkpoint_count": self.checkpoint_count,
            "external_side_effects": self.external_side_effects,
        }


@dataclass(frozen=True)
class MigrationRollbackReceipt:
    plan_id: str
    status: PlanStatus
    rolled_back_action_ids: tuple[str, ...]
    recovery_required_action_ids: tuple[str, ...]
    checkpoint_count: int
    external_side_effects: str = "none"

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "status": self.status,
            "rolled_back_action_ids": list(self.rolled_back_action_ids),
            "recovery_required_action_ids": list(self.recovery_required_action_ids),
            "checkpoint_count": self.checkpoint_count,
            "external_side_effects": self.external_side_effects,
        }


def _action_id(semantic: Mapping[str, Any]) -> str:
    return ACTION_PREFIX + _sha256_text(_canonical_json(semantic))[:24]


def _plan_id(semantic: Mapping[str, Any]) -> str:
    return PLAN_PREFIX + _sha256_text(_canonical_json(semantic))[:24]


def _make_action(
    *,
    kind: ActionKind,
    target_type: str,
    target_ids: Sequence[str],
    repository: str,
    pr_number: int,
    head_sha: str,
    required_pr_state: str,
    source_evidence: Sequence[str],
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    rollback: Mapping[str, Any],
) -> MigrationAction:
    semantic = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "kind": kind,
        "target_type": target_type,
        "target_ids": sorted(set(target_ids)),
        "repository": repository,
        "pr_number": int(pr_number),
        "head_sha": _full_sha(head_sha, "head_sha"),
        "required_pr_state": required_pr_state,
        "source_evidence": sorted(set(source_evidence)),
        "before": dict(before),
        "after": dict(after),
        "rollback": dict(rollback),
    }
    identity = dict(semantic)
    identity["source_evidence"] = list(
        _semantic_evidence(tuple(semantic["source_evidence"]))
    )
    action_id = _action_id(identity)
    return MigrationAction(
        action_id=action_id,
        idempotency_key=f"review-migration-action:v1:{action_id}",
        kind=kind,
        target_type=target_type,
        target_ids=tuple(semantic["target_ids"]),
        repository=repository,
        pr_number=int(pr_number),
        head_sha=semantic["head_sha"],
        required_pr_state=required_pr_state,
        source_evidence=tuple(semantic["source_evidence"]),
        before=dict(before),
        after=dict(after),
        rollback=dict(rollback),
    )


def _make_plan(
    *,
    report: reconciliation.ReconciliationReport,
    records: Sequence[MigrationRecord],
    actions: Sequence[MigrationAction],
    blocked_reasons: Sequence[str],
) -> MigrationPlan:
    ordered_records = tuple(
        sorted(
            records,
            key=lambda item: (
                item.repository or "",
                item.pr_number or 0,
                item.entity_type,
                item.entity_id,
            ),
        )
    )
    ordered_actions = tuple(sorted(actions, key=lambda item: item.action_id))
    reasons = tuple(sorted(set(blocked_reasons)))
    identity = {
        "schema_version": SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "records": [item.identity_dict() for item in ordered_records],
        "actions": [item.identity_dict() for item in ordered_actions],
        "blocked_reasons": list(reasons),
    }
    plan_id = _plan_id(identity)
    return MigrationPlan(
        plan_id=plan_id,
        idempotency_key=f"review-migration-plan:v1:{plan_id}",
        reconciliation_input_sha256=report.input_sha256,
        reconciliation_report_sha256=report.report_sha256(),
        records=ordered_records,
        actions=ordered_actions,
        blocked_reasons=reasons,
    )


def _finding_is_safe_candidate(finding: reconciliation.ReconciliationFinding) -> bool:
    if finding.code in _SAFE_FINDING_CODES:
        return True
    return (
        finding.code.startswith("missing_gate_delivery_")
        or finding.code.startswith("missing_github_outbox_")
        or finding.code == "missing_slack_outbox_notification"
    )


def _finding_is_hard(finding: reconciliation.ReconciliationFinding) -> bool:
    return any(fragment in finding.code for fragment in _HARD_FINDING_FRAGMENTS)


def _finding_ref(
    finding: reconciliation.ReconciliationFinding,
) -> Optional[reconciliation.PullRequestIdentity]:
    if finding.repository and finding.pr_number is not None:
        return _ref(finding.repository, finding.pr_number)
    return None


def _snapshot_evidence(snapshot: github.GitHubPullRequestSnapshot) -> str:
    return (
        f"github:{snapshot.observation_id}:snapshot-sha256:{snapshot.snapshot_sha256()}"
    )


def _gate_evidence(gate: human_review.HumanReviewGate) -> str:
    return f"human_review_gates:{gate.id}:packet-sha256:{gate.approval_packet_sha256}"


def _delivery_destination(
    gate: human_review.HumanReviewGate,
    channel: str,
) -> Optional[str]:
    if channel.startswith("github_"):
        return f"{gate.repo}#{gate.pr_number}"
    if channel == "slack":
        principal = gate.notification_principal
        if not principal:
            return None
        return principal.split(":", 1)[1] if ":" in principal else principal
    return None


def _delivery_marker(gate: human_review.HumanReviewGate, channel: str) -> str:
    if channel == "github_comment":
        return f"<!-- echlon-human-review-gate:v1:{gate.id} -->"
    return (
        f"echlon-srdja-review:v1:{gate.repo}:pr:{gate.pr_number}:"
        f"head:{gate.approved_head_sha}"
    )


def _record_evidence(
    *,
    report: reconciliation.ReconciliationReport,
    base: Sequence[str],
    findings: Sequence[reconciliation.ReconciliationFinding],
    snapshot: Optional[github.GitHubPullRequestSnapshot],
) -> tuple[str, ...]:
    values = [
        *base,
        f"reconciliation:input:{report.input_sha256}",
        f"reconciliation:report:{report.report_sha256()}",
        *(f"reconciliation:finding:{item.key}:{item.code}" for item in findings),
    ]
    if snapshot is not None:
        values.append(_snapshot_evidence(snapshot))
    return tuple(sorted(set(values)))


def _suppress_gate_action(
    *,
    gate: human_review.HumanReviewGate,
    task: reconciliation.KanbanTaskState,
    snapshot: github.GitHubPullRequestSnapshot,
    deliveries: Sequence[human_review.ReviewGateDelivery],
    github_intents: Sequence[github.GitHubOutboxIntent],
    slack_intents: Sequence[Any],
    evidence: Sequence[str],
) -> MigrationAction:
    active_deliveries = [
        {"gate_id": item.gate_id, "channel": item.channel, "state": item.state}
        for item in deliveries
        if item.state in {"pending", "attempting", "retry", "failed"}
    ]
    active_github = [
        {"id": item.id, "state": item.state, "next_attempt_at": item.next_attempt_at}
        for item in github_intents
        if item.state in _ACTIVE_OUTBOX_STATES
    ]
    active_slack = [
        {"id": item.id, "state": item.state, "next_attempt_at": item.next_attempt_at}
        for item in slack_intents
        if item.state in _ACTIVE_OUTBOX_STATES
    ]
    before = {
        "gate": {"id": gate.id, "state": gate.state},
        "task": {"id": task.task_id, "status": task.status},
        "deliveries": active_deliveries,
        "github_intents": active_github,
        "slack_intents": active_slack,
    }
    after = {
        "gate": {"id": gate.id, "state": "superseded"},
        "task": {"id": task.task_id, "status": "archived"},
        "deliveries": [{**item, "state": "superseded"} for item in active_deliveries],
        "github_intents": [
            {**item, "state": "superseded", "next_attempt_at": None}
            for item in active_github
        ],
        "slack_intents": [
            {**item, "state": "superseded", "next_attempt_at": None}
            for item in active_slack
        ],
    }
    return _make_action(
        kind="suppress_gate_bundle",
        target_type="human_review_gate_bundle",
        target_ids=(
            gate.id,
            task.task_id,
            *(str(item["channel"]) for item in active_deliveries),
            *(str(item["id"]) for item in active_github),
            *(str(item["id"]) for item in active_slack),
        ),
        repository=gate.repo,
        pr_number=gate.pr_number,
        head_sha=snapshot.head_sha,
        required_pr_state=snapshot.state,
        source_evidence=evidence,
        before=before,
        after=after,
        rollback={
            "mode": "manual",
            "reason": "Never reactivate stale or terminal human-review authority automatically.",
            "path": (
                "Use the checkpoint before-state only after fresh exact-head QA and "
                "human approval; otherwise create a new immutable gate."
            ),
        },
    )


def _suppress_outbox_action(
    *,
    surface: Literal["github", "slack"],
    intent: Any,
    snapshot: github.GitHubPullRequestSnapshot,
    evidence: Sequence[str],
) -> MigrationAction:
    before = {
        "surface": surface,
        "rows": [
            {
                "id": intent.id,
                "state": intent.state,
                "next_attempt_at": intent.next_attempt_at,
            }
        ],
    }
    after = {
        "surface": surface,
        "rows": [
            {
                "id": intent.id,
                "state": "superseded",
                "next_attempt_at": None,
            }
        ],
    }
    return _make_action(
        kind="suppress_outbox_bundle",
        target_type=f"{surface}_outbox",
        target_ids=(intent.id,),
        repository=intent.repository,
        pr_number=intent.pr_number,
        head_sha=snapshot.head_sha,
        required_pr_state=snapshot.state,
        source_evidence=evidence,
        before=before,
        after=after,
        rollback={
            "mode": "manual",
            "reason": "Suppressed external delivery intent must not be reactivated automatically.",
            "path": "Re-plan from a fresh exact-head snapshot and create a new intent if authorized.",
        },
    )


def _backfill_delivery_action(
    *,
    gate: human_review.HumanReviewGate,
    channel: str,
    destination: str,
    snapshot: github.GitHubPullRequestSnapshot,
    evidence: Sequence[str],
) -> MigrationAction:
    row = {
        "gate_id": gate.id,
        "channel": channel,
        "destination": destination,
        "state": "pending",
        "attempt_count": 0,
        "next_attempt_at": None,
        "external_id": None,
        "dedupe_marker": _delivery_marker(gate, channel),
        "last_error": None,
        "created_at": gate.created_at,
        "updated_at": gate.created_at,
    }
    return _make_action(
        kind="backfill_gate_delivery",
        target_type="review_gate_delivery",
        target_ids=(gate.id, channel),
        repository=gate.repo,
        pr_number=gate.pr_number,
        head_sha=snapshot.head_sha,
        required_pr_state="open",
        source_evidence=evidence,
        before={"row": None},
        after={"row": row},
        rollback={
            "mode": "automatic",
            "reason": "Insert-only row can be deleted if it remains byte-for-byte unchanged.",
            "path": f"DELETE review_gate_deliveries WHERE gate_id={gate.id} AND channel={channel}",
        },
    )


def collect_migration_inputs(
    conn: sqlite3.Connection,
    *,
    snapshot_provider: MigrationSnapshotProvider,
    linear_issue_ids: Optional[Sequence[str]] = None,
    now: Optional[int] = None,
    max_pull_requests: int = reconciliation.DEFAULT_MAX_PULL_REQUESTS,
    max_source_records: int = reconciliation.DEFAULT_MAX_SOURCE_RECORDS,
) -> MigrationInputs:
    """Collect source state without writes, including legacy human-only tasks."""
    inputs = reconciliation.collect_reconciliation_inputs(
        conn,
        snapshot_provider=snapshot_provider,
        linear_issue_ids=linear_issue_ids,
        now=now,
        max_pull_requests=max_pull_requests,
        max_source_records=max_source_records,
    )
    gate_task_ids = {gate.task_id for gate in inputs.human_gates}
    legacy_tasks = tuple(
        reconciliation.KanbanTaskState.from_row(row)
        for row in conn.execute(
            "SELECT id, status, assignee, current_run_id FROM tasks "
            "WHERE status='awaiting_human' ORDER BY id"
        ).fetchall()
        if row["id"] not in gate_task_ids
    )
    return MigrationInputs(inputs, legacy_tasks)


def build_migration_plan(
    inputs: MigrationInputs,
    *,
    report: Optional[reconciliation.ReconciliationReport] = None,
) -> MigrationPlan:
    """Build a deterministic plan; ambiguous evidence produces no guessed repair."""
    if not isinstance(inputs, MigrationInputs):
        raise MigrationBoundaryError("inputs must be a MigrationInputs instance")
    computed_report = reconciliation.build_reconciliation_report(
        inputs.reconciliation_inputs
    )
    if report is not None and report.to_json() != computed_report.to_json():
        raise MigrationBoundaryError(
            "supplied reconciliation report does not match migration source inputs"
        )
    effective_report = computed_report
    source = inputs.reconciliation_inputs
    findings_by_ref: dict[
        reconciliation.PullRequestIdentity,
        list[reconciliation.ReconciliationFinding],
    ] = {}
    unscoped_findings: list[reconciliation.ReconciliationFinding] = []
    for finding in effective_report.findings:
        finding_ref = _finding_ref(finding)
        if finding_ref is None:
            unscoped_findings.append(finding)
        else:
            findings_by_ref.setdefault(finding_ref, []).append(finding)

    read_groups: dict[
        reconciliation.PullRequestIdentity,
        list[reconciliation.TrustedPullRequestRead],
    ] = {}
    for read in source.trusted_pr_reads:
        read_groups.setdefault(read.requested, []).append(read)
    snapshots: dict[
        reconciliation.PullRequestIdentity,
        github.GitHubPullRequestSnapshot,
    ] = {}
    for requested, reads in read_groups.items():
        if (
            len(reads) == 1
            and reads[0].status == "ok"
            and reads[0].snapshot is not None
        ):
            snapshots[requested] = reads[0].snapshot

    gate_semantic_groups: dict[tuple[str, int, str, str], list[str]] = {}
    active_gate_groups: dict[tuple[str, int, str], list[str]] = {}
    for gate in source.human_gates:
        gate_semantic_groups.setdefault(
            (gate.repo, gate.pr_number, gate.gate_kind, gate.approved_head_sha), []
        ).append(gate.id)
        if gate.state in human_review.ACTIVE_GATE_STATES:
            active_gate_groups.setdefault(
                (gate.repo, gate.pr_number, gate.gate_kind), []
            ).append(gate.id)
    duplicate_gate_ids = {
        gate_id
        for ids in (*gate_semantic_groups.values(), *active_gate_groups.values())
        if len(ids) > 1
        for gate_id in ids
    }

    delivery_groups: dict[tuple[str, str], list[str]] = {}
    for delivery in source.gate_deliveries:
        delivery_groups.setdefault((delivery.gate_id, delivery.channel), []).append(
            delivery.dedupe_marker
        )
    duplicate_delivery_keys = {
        key for key, markers in delivery_groups.items() if len(markers) > 1
    }

    github_groups: dict[tuple[str, int, str, str, str], list[str]] = {}
    for intent in source.github_intents:
        github_groups.setdefault(
            (
                intent.repository,
                intent.pr_number,
                intent.head_sha,
                intent.surface,
                intent.operation,
            ),
            [],
        ).append(intent.id)
    duplicate_github_ids = {
        intent_id for ids in github_groups.values() if len(ids) > 1 for intent_id in ids
    }

    slack_groups: dict[tuple[str, str, str, int, str, str, str], list[str]] = {}
    for intent in source.slack_intents:
        slack_groups.setdefault(
            (
                intent.channel_id,
                intent.thread_ts,
                intent.repository,
                intent.pr_number,
                intent.head_sha,
                intent.surface,
                intent.operation,
            ),
            [],
        ).append(intent.id)
    duplicate_slack_ids = {
        intent_id for ids in slack_groups.values() if len(ids) > 1 for intent_id in ids
    }

    task_by_id = {task.task_id: task for task in source.task_states}
    gate_by_id = {gate.id: gate for gate in source.human_gates}
    deliveries_by_gate: dict[str, list[human_review.ReviewGateDelivery]] = {}
    for delivery in source.gate_deliveries:
        deliveries_by_gate.setdefault(delivery.gate_id, []).append(delivery)
    github_by_gate: dict[str, list[github.GitHubOutboxIntent]] = {}
    for intent in source.github_intents:
        github_by_gate.setdefault(intent.gate_id, []).append(intent)
    slack_by_gate: dict[str, list[Any]] = {}
    for intent in source.slack_intents:
        slack_by_gate.setdefault(intent.gate_id, []).append(intent)

    records: list[MigrationRecord] = []
    actions: list[MigrationAction] = []
    blocked_reasons: list[str] = []
    gate_classification_by_id: dict[str, Classification] = {}
    gate_action_ids: dict[str, tuple[str, ...]] = {}
    suppressed_outbox_ids: set[str] = set()

    for gate in source.human_gates:
        gate_ref = _ref(gate.repo, gate.pr_number)
        snapshot = snapshots.get(gate_ref)
        findings = findings_by_ref.get(gate_ref, [])
        evidence = _record_evidence(
            report=effective_report,
            base=(_gate_evidence(gate), f"tasks:{gate.task_id}"),
            findings=findings,
            snapshot=snapshot,
        )
        task = task_by_id.get(gate.task_id)
        hard_findings = [item for item in findings if _finding_is_hard(item)]
        unsafe_findings = [
            item
            for item in findings
            if not _finding_is_safe_candidate(item) and item.severity != "info"
        ]
        in_flight_ids = sorted(
            item.id
            for item in (
                *github_by_gate.get(gate.id, ()),
                *slack_by_gate.get(gate.id, ()),
            )
            if item.state == "attempting"
        )
        in_flight_ids.extend(
            sorted(
                f"{item.gate_id}:{item.channel}"
                for item in deliveries_by_gate.get(gate.id, ())
                if item.state == "attempting"
            )
        )
        if gate.id in duplicate_gate_ids:
            classification: Classification = "duplicate"
            reason = "Multiple gates claim the same exact-head or active PR identity."
        elif task is None:
            classification = "orphan"
            reason = "The gate's non-dispatchable human task row is missing."
        elif snapshot is None:
            classification = "orphan"
            reason = (
                "No single fresh authoritative GitHub snapshot exists for this gate."
            )
        elif snapshot.repository != gate.repo or snapshot.pr_number != gate.pr_number:
            classification = "ambiguous"
            reason = "Authoritative snapshot identity conflicts with the stored gate."
        elif in_flight_ids:
            classification = "ambiguous"
            reason = (
                "One or more delivery rows are attempting; reconcile provider readback "
                "before any migration write."
            )
        elif (
            snapshot.state in {"closed", "merged"}
            or gate.state in human_review.TERMINAL_GATE_STATES
        ):
            classification = "terminal"
            reason = "The PR or stored human-review gate is terminal."
        elif gate.approved_head_sha != snapshot.head_sha:
            classification = "stale"
            reason = (
                "The gate is bound to a superseded head; it must never be retargeted."
            )
        elif hard_findings:
            classification = "ambiguous"
            reason = (
                "Gate lineage or immutable source identity conflicts in reconciliation."
            )
        elif unsafe_findings:
            classification = "ambiguous"
            reason = (
                "Current-head source evidence has unresolved reconciliation findings."
            )
        else:
            classification = "current_head"
            reason = "Gate matches one fresh authoritative open GitHub head."

        gate_actions: list[MigrationAction] = []
        if (
            classification in {"stale", "terminal"}
            and gate.state in human_review.ACTIVE_GATE_STATES
            and snapshot is not None
            and task is not None
        ):
            action = _suppress_gate_action(
                gate=gate,
                task=task,
                snapshot=snapshot,
                deliveries=deliveries_by_gate.get(gate.id, ()),
                github_intents=github_by_gate.get(gate.id, ()),
                slack_intents=slack_by_gate.get(gate.id, ()),
                evidence=evidence,
            )
            gate_actions.append(action)
            suppressed_outbox_ids.update(
                item.id
                for item in (
                    *github_by_gate.get(gate.id, ()),
                    *slack_by_gate.get(gate.id, ()),
                )
                if item.state in _ACTIVE_OUTBOX_STATES
            )
        elif classification == "current_head" and snapshot is not None:
            existing_channels = {
                item.channel for item in deliveries_by_gate.get(gate.id, ())
            }
            for channel in human_review.DEFAULT_DELIVERY_CHANNELS:
                if channel in existing_channels:
                    continue
                destination = _delivery_destination(gate, channel)
                if not destination:
                    classification = "ambiguous"
                    reason = f"Current-head gate lacks an immutable destination for {channel}."
                    blocked_reasons.append(
                        f"{gate.id}: missing immutable {channel} destination"
                    )
                    continue
                gate_actions.append(
                    _backfill_delivery_action(
                        gate=gate,
                        channel=channel,
                        destination=destination,
                        snapshot=snapshot,
                        evidence=evidence,
                    )
                )
        actions.extend(gate_actions)
        gate_classification_by_id[gate.id] = classification
        gate_action_ids[gate.id] = tuple(item.action_id for item in gate_actions)
        records.append(
            MigrationRecord(
                classification=classification,
                entity_type="human_review_gate",
                entity_id=gate.id,
                repository=gate.repo,
                pr_number=gate.pr_number,
                stored_head_sha=gate.approved_head_sha,
                authoritative_head_sha=(snapshot.head_sha if snapshot else None),
                reason=reason,
                source_evidence=evidence,
                proposed_action_ids=tuple(item.action_id for item in gate_actions),
            )
        )

    for delivery in source.gate_deliveries:
        gate = gate_by_id.get(delivery.gate_id)
        gate_classification = gate_classification_by_id.get(delivery.gate_id)
        snapshot = (
            snapshots.get(_ref(gate.repo, gate.pr_number)) if gate is not None else None
        )
        evidence = _record_evidence(
            report=effective_report,
            base=(f"review_gate_deliveries:{delivery.gate_id}:{delivery.channel}",),
            findings=(
                findings_by_ref.get(_ref(gate.repo, gate.pr_number), ())
                if gate is not None
                else ()
            ),
            snapshot=snapshot,
        )
        key = (delivery.gate_id, delivery.channel)
        if key in duplicate_delivery_keys:
            classification = "duplicate"
            reason = "Multiple delivery rows claim one gate/channel identity."
        elif gate is None or gate_classification is None:
            classification = "orphan"
            reason = "Delivery row references a missing human-review gate."
        elif delivery.state == "attempting":
            classification = "ambiguous"
            reason = "Delivery attempt is in flight and requires provider readback."
        else:
            classification = gate_classification
            reason = (
                "Delivery row inherits the exact-head classification of its typed gate."
            )
        records.append(
            MigrationRecord(
                classification=classification,
                entity_type="review_gate_delivery",
                entity_id=f"{delivery.gate_id}:{delivery.channel}",
                repository=(gate.repo if gate is not None else None),
                pr_number=(gate.pr_number if gate is not None else None),
                stored_head_sha=(gate.approved_head_sha if gate is not None else None),
                authoritative_head_sha=(snapshot.head_sha if snapshot else None),
                reason=reason,
                source_evidence=evidence,
                proposed_action_ids=gate_action_ids.get(delivery.gate_id, ()),
            )
        )

    for task in inputs.legacy_human_tasks:
        evidence = (
            f"tasks:{task.task_id}:status:{task.status}",
            f"reconciliation:input:{effective_report.input_sha256}",
        )
        records.append(
            MigrationRecord(
                classification="orphan",
                entity_type="legacy_human_task",
                entity_id=task.task_id,
                repository=None,
                pr_number=None,
                stored_head_sha=None,
                authoritative_head_sha=None,
                reason=(
                    "Human-only task has no typed gate; repository, PR, and head are "
                    "not inferred from its title or body."
                ),
                source_evidence=evidence,
            )
        )

    outbox_groups: tuple[
        tuple[Literal["github", "slack"], Sequence[Any], set[str]], ...
    ] = (
        ("github", source.github_intents, duplicate_github_ids),
        ("slack", source.slack_intents, duplicate_slack_ids),
    )
    for surface, intents, duplicates in outbox_groups:
        for intent in intents:
            intent_ref = _ref(intent.repository, intent.pr_number)
            snapshot = snapshots.get(intent_ref)
            findings = findings_by_ref.get(intent_ref, [])
            gate = gate_by_id.get(intent.gate_id)
            evidence = _record_evidence(
                report=effective_report,
                base=(f"{surface}_human_review_outbox:{intent.id}",),
                findings=findings,
                snapshot=snapshot,
            )
            if intent.id in duplicates:
                classification = "duplicate"
                reason = "Outbox contains duplicate semantic exact-head intents."
            elif gate is None:
                classification = "orphan"
                reason = "Outbox intent references a missing human-review gate."
            elif (
                gate.repo != intent.repository
                or gate.pr_number != intent.pr_number
                or gate.approved_head_sha != intent.head_sha
            ):
                classification = "ambiguous"
                reason = "Outbox immutable PR/head identity conflicts with its gate."
            elif intent.state == "attempting":
                classification = "ambiguous"
                reason = "Outbox delivery is in flight and requires provider readback."
            elif snapshot is None:
                classification = "orphan"
                reason = "No single fresh authoritative GitHub snapshot exists."
            elif snapshot.state in {"closed", "merged"}:
                classification = "terminal"
                reason = "Outbox intent targets a terminal PR."
            elif intent.head_sha != snapshot.head_sha:
                classification = "stale"
                reason = "Outbox intent targets a superseded exact head."
            elif intent.state in _TERMINAL_OUTBOX_STATES:
                classification = "terminal"
                reason = f"Outbox row is already terminal ({intent.state})."
            else:
                classification = "current_head"
                reason = (
                    "Outbox intent matches its gate and authoritative current head."
                )
            intent_actions: list[MigrationAction] = []
            if (
                classification in {"stale", "terminal"}
                and intent.state in _ACTIVE_OUTBOX_STATES
                and snapshot is not None
                and intent.id not in suppressed_outbox_ids
            ):
                intent_actions.append(
                    _suppress_outbox_action(
                        surface=surface,
                        intent=intent,
                        snapshot=snapshot,
                        evidence=evidence,
                    )
                )
            actions.extend(intent_actions)
            records.append(
                MigrationRecord(
                    classification=classification,
                    entity_type=f"{surface}_outbox_intent",
                    entity_id=intent.id,
                    repository=intent.repository,
                    pr_number=intent.pr_number,
                    stored_head_sha=intent.head_sha,
                    authoritative_head_sha=(snapshot.head_sha if snapshot else None),
                    reason=reason,
                    source_evidence=evidence,
                    proposed_action_ids=tuple(
                        item.action_id for item in intent_actions
                    ),
                )
            )

    head_groups: dict[reconciliation.PullRequestIdentity, list[Any]] = {}
    for pointer in source.coderabbit_heads:
        head_groups.setdefault(pointer.ref, []).append(pointer)
    for pointer in source.coderabbit_heads:
        snapshot = snapshots.get(pointer.ref)
        evidence = _record_evidence(
            report=effective_report,
            base=(
                f"coderabbit_pr_heads:{pointer.ref.repository}#{pointer.ref.pr_number}",
            ),
            findings=findings_by_ref.get(pointer.ref, ()),
            snapshot=snapshot,
        )
        if len(head_groups[pointer.ref]) > 1:
            classification = "duplicate"
            reason = "Multiple CodeRabbit current-head pointers exist for one PR."
        elif snapshot is None:
            classification = "orphan"
            reason = "CodeRabbit pointer has no fresh authoritative GitHub snapshot."
        elif snapshot.state in {"closed", "merged"}:
            classification = "terminal"
            reason = "CodeRabbit pointer belongs to a terminal PR."
        elif pointer.current_head_sha != snapshot.head_sha:
            classification = "stale"
            reason = "CodeRabbit pointer is retained stale evidence for an older head."
        else:
            classification = "current_head"
            reason = "CodeRabbit pointer matches the authoritative current head."
        records.append(
            MigrationRecord(
                classification=classification,
                entity_type="coderabbit_head_pointer",
                entity_id=f"{pointer.ref.repository}#{pointer.ref.pr_number}",
                repository=pointer.ref.repository,
                pr_number=pointer.ref.pr_number,
                stored_head_sha=pointer.current_head_sha,
                authoritative_head_sha=(snapshot.head_sha if snapshot else None),
                reason=reason,
                source_evidence=evidence,
            )
        )

    assessment_groups: dict[tuple[str, int, str], list[Any]] = {}
    for assessment in source.coderabbit_assessments:
        assessment_groups.setdefault(
            (assessment.repository, assessment.pr_number, assessment.head_sha), []
        ).append(assessment)
    for assessment in source.coderabbit_assessments:
        assessment_ref = _ref(assessment.repository, assessment.pr_number)
        snapshot = snapshots.get(assessment_ref)
        evidence = _record_evidence(
            report=effective_report,
            base=(
                f"coderabbit_head_assessments:{assessment.repository}#"
                f"{assessment.pr_number}:{assessment.snapshot_sha256}",
            ),
            findings=findings_by_ref.get(assessment_ref, ()),
            snapshot=snapshot,
        )
        group = assessment_groups[
            (assessment.repository, assessment.pr_number, assessment.head_sha)
        ]
        if len(group) > 1:
            classification = "duplicate"
            reason = "Multiple CodeRabbit assessments claim the same exact head."
        elif snapshot is None:
            classification = "orphan"
            reason = "Assessment has no fresh authoritative GitHub snapshot."
        elif snapshot.state in {"closed", "merged"}:
            classification = "terminal"
            reason = "Assessment is preserved evidence for a terminal PR."
        elif assessment.head_sha != snapshot.head_sha:
            classification = "stale"
            reason = "Assessment is preserved immutable evidence for an older head."
        else:
            classification = "current_head"
            reason = "Assessment matches the authoritative current head."
        records.append(
            MigrationRecord(
                classification=classification,
                entity_type="coderabbit_assessment",
                entity_id=assessment.snapshot_sha256,
                repository=assessment.repository,
                pr_number=assessment.pr_number,
                stored_head_sha=assessment.head_sha,
                authoritative_head_sha=(snapshot.head_sha if snapshot else None),
                reason=reason,
                source_evidence=evidence,
            )
        )

    for record in records:
        if record.classification in {"duplicate", "orphan", "ambiguous"}:
            blocked_reasons.append(
                f"{record.classification}:{record.entity_type}:{record.entity_id}: "
                f"{record.reason}"
            )
    for finding in unscoped_findings:
        if finding.severity != "info" and not _finding_is_safe_candidate(finding):
            blocked_reasons.append(
                f"reconciliation:{finding.key}:{finding.code}: {finding.summary}"
            )

    unique_actions = {action.action_id: action for action in actions}
    return _make_plan(
        report=effective_report,
        records=records,
        actions=tuple(unique_actions.values()),
        blocked_reasons=blocked_reasons,
    )


@contextlib.contextmanager
def open_read_only_snapshot(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Clone a board DB into memory and add current schema without live writes."""
    memory = sqlite3.connect(":memory:")
    memory.row_factory = sqlite3.Row
    try:
        if db_path.exists():
            uri = db_path.resolve().as_uri() + "?mode=ro"
            live = sqlite3.connect(uri, uri=True)
            try:
                live.backup(memory)
            finally:
                live.close()
        try:
            memory.executescript(kb.SCHEMA_SQL)
        except sqlite3.IntegrityError as exc:
            # Legacy boards may contain the exact duplicate rows this planner
            # must inventory. All CREATE TABLE statements precede the indexes
            # in SCHEMA_SQL, so retain the copied rows and current table shapes
            # while deliberately leaving conflicting indexes absent in this
            # disposable in-memory snapshot.
            if "UNIQUE constraint failed" not in str(exc):
                raise
        memory.commit()
        memory.execute("PRAGMA query_only=ON")
        yield memory
    finally:
        memory.close()


def write_plan_report(plan: MigrationPlan, path: Path) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(plan.to_markdown(), encoding="utf-8")
    return path


def _persist_plan(conn: sqlite3.Connection, plan: MigrationPlan, *, now: int) -> None:
    plan_json = _canonical_json(plan.to_dict())
    plan_sha = plan.plan_sha256()
    with kb.write_txn(conn):
        existing = conn.execute(
            "SELECT plan_json, plan_sha256 FROM review_migration_plans WHERE id=?",
            (plan.plan_id,),
        ).fetchone()
        if existing is not None:
            if (
                existing["plan_json"] != plan_json
                or existing["plan_sha256"] != plan_sha
            ):
                raise MigrationConflict(
                    "migration plan ID already exists with different content"
                )
            return
        conn.execute(
            """
            INSERT INTO review_migration_plans (
                id, idempotency_key, schema_version, policy_version,
                reconciliation_input_sha256, reconciliation_report_sha256,
                plan_json, plan_sha256, status, action_count, checkpoint_count,
                created_at, updated_at, completed_at, rollback_completed_at, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'prepared', ?, 0, ?, ?, NULL, NULL, NULL)
            """,
            (
                plan.plan_id,
                plan.idempotency_key,
                SCHEMA_VERSION,
                POLICY_VERSION,
                plan.reconciliation_input_sha256,
                plan.reconciliation_report_sha256,
                plan_json,
                plan_sha,
                len(plan.actions),
                now,
                now,
            ),
        )
        for ordinal, action in enumerate(plan.actions, start=1):
            action_json = _canonical_json(action.to_dict())
            conn.execute(
                """
                INSERT INTO review_migration_actions (
                    plan_id, ordinal, action_id, idempotency_key, action_kind,
                    target_type, target_ids_json, repository, pr_number, head_sha,
                    action_json, action_sha256, status, applied_at, rolled_back_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', NULL, NULL)
                """,
                (
                    plan.plan_id,
                    ordinal,
                    action.action_id,
                    action.idempotency_key,
                    action.kind,
                    action.target_type,
                    _canonical_json(list(action.target_ids)),
                    action.repository,
                    action.pr_number,
                    action.head_sha,
                    action_json,
                    _sha256_text(action_json),
                ),
            )


def load_migration_plan(
    conn: sqlite3.Connection,
    plan_id: str,
) -> Optional[MigrationPlan]:
    row = conn.execute(
        "SELECT plan_json, plan_sha256 FROM review_migration_plans WHERE id=?",
        (plan_id,),
    ).fetchone()
    if row is None:
        return None
    try:
        value = json.loads(row["plan_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise MigrationConflict("stored migration plan JSON is invalid") from exc
    if not isinstance(value, dict):
        raise MigrationConflict("stored migration plan JSON is not an object")
    plan = MigrationPlan.from_dict(value)
    if plan.plan_sha256() != row["plan_sha256"]:
        raise MigrationConflict("stored migration plan hash does not match content")
    return plan


def _validate_snapshot_for_action(
    action: MigrationAction,
    snapshot_provider: MigrationSnapshotProvider,
    *,
    now: int,
) -> None:
    snapshot = snapshot_provider.read_snapshot(
        repository=action.repository,
        pr_number=action.pr_number,
    )
    if snapshot is None:
        raise MigrationConflict(
            f"authoritative snapshot unavailable for {action.repository}#{action.pr_number}"
        )
    if (
        snapshot.repository != action.repository
        or snapshot.pr_number != action.pr_number
    ):
        raise MigrationConflict(
            "authoritative snapshot identity changed after planning"
        )
    if snapshot.observed_at < int(now) - github.MAX_SNAPSHOT_AGE_SECONDS:
        raise MigrationConflict("authoritative snapshot readback is stale")
    if snapshot.observed_at > int(now) + github.MAX_SNAPSHOT_FUTURE_SKEW_SECONDS:
        raise MigrationConflict("authoritative snapshot readback is future-dated")
    if snapshot.head_sha != action.head_sha:
        raise MigrationConflict(
            f"authoritative head changed after planning: expected {action.head_sha}, "
            f"observed {snapshot.head_sha}"
        )
    if snapshot.state != action.required_pr_state:
        raise MigrationConflict(
            f"authoritative PR state changed after planning: expected "
            f"{action.required_pr_state}, observed {snapshot.state}"
        )
    planned_hashes = {
        value.rsplit(":snapshot-sha256:", 1)[1]
        for value in action.source_evidence
        if value.startswith("github:") and ":snapshot-sha256:" in value
    }
    if len(planned_hashes) != 1:
        raise MigrationConflict(
            "migration action lacks one authoritative snapshot digest"
        )
    if snapshot.snapshot_sha256() not in planned_hashes:
        raise MigrationConflict("authoritative snapshot content changed after planning")


def _row_state(conn: sqlite3.Connection, table: str, row_id: str) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    if row is None:
        raise MigrationConflict(f"migration target {table}:{row_id} is missing")
    return row


def _mapping_matches(row: sqlite3.Row, expected: Mapping[str, Any]) -> bool:
    return all(row[key] == value for key, value in expected.items())


def _apply_backfill_delivery(
    conn: sqlite3.Connection,
    action: MigrationAction,
) -> None:
    row = dict(action.after["row"])
    existing = conn.execute(
        "SELECT * FROM review_gate_deliveries WHERE gate_id=? AND channel=?",
        (row["gate_id"], row["channel"]),
    ).fetchone()
    if existing is not None:
        if not _mapping_matches(existing, row):
            raise MigrationConflict(
                "review delivery appeared after planning with different content"
            )
        return
    conn.execute(
        """
        INSERT INTO review_gate_deliveries (
            gate_id, channel, destination, state, attempt_count, next_attempt_at,
            external_id, dedupe_marker, last_error, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        tuple(
            row[key]
            for key in (
                "gate_id",
                "channel",
                "destination",
                "state",
                "attempt_count",
                "next_attempt_at",
                "external_id",
                "dedupe_marker",
                "last_error",
                "created_at",
                "updated_at",
            )
        ),
    )


def _validate_bundle_state(
    conn: sqlite3.Connection,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
) -> Literal["before", "after"]:
    observed: dict[str, Any] = {}
    if "gate" in before:
        gate = before["gate"]
        row = _row_state(conn, "human_review_gates", gate["id"])
        observed["gate"] = {"id": gate["id"], "state": row["state"]}
    if "task" in before:
        task = before["task"]
        row = _row_state(conn, "tasks", task["id"])
        observed["task"] = {"id": task["id"], "status": row["status"]}
    observed["deliveries"] = []
    for item in before.get("deliveries", ()):
        row = conn.execute(
            "SELECT state FROM review_gate_deliveries WHERE gate_id=? AND channel=?",
            (item["gate_id"], item["channel"]),
        ).fetchone()
        if row is None:
            raise MigrationConflict("review delivery disappeared after planning")
        observed["deliveries"].append({**item, "state": row["state"]})
    for key, table in (
        ("github_intents", "github_human_review_outbox"),
        ("slack_intents", "slack_human_review_outbox"),
    ):
        observed[key] = []
        for item in before.get(key, ()):
            row = _row_state(conn, table, item["id"])
            observed[key].append({
                "id": item["id"],
                "state": row["state"],
                "next_attempt_at": row["next_attempt_at"],
            })
    comparable_before = {key: before.get(key, []) for key in observed}
    comparable_after = {key: after.get(key, []) for key in observed}
    if observed == comparable_before:
        return "before"
    if observed == comparable_after:
        return "after"
    raise MigrationConflict("migration bundle changed after planning")


def _apply_suppression_bundle(
    conn: sqlite3.Connection,
    action: MigrationAction,
    *,
    now: int,
) -> None:
    state = _validate_bundle_state(conn, action.before, action.after)
    if state == "after":
        return
    gate = action.before.get("gate")
    if gate:
        conn.execute(
            "UPDATE human_review_gates SET state='superseded', updated_at=? WHERE id=?",
            (now, gate["id"]),
        )
    task = action.before.get("task")
    if task:
        conn.execute(
            "UPDATE tasks SET status='archived', completed_at=COALESCE(completed_at, ?) "
            "WHERE id=?",
            (now, task["id"]),
        )
    for item in action.before.get("deliveries", ()):
        conn.execute(
            "UPDATE review_gate_deliveries SET state='superseded', "
            "next_attempt_at=NULL, updated_at=? WHERE gate_id=? AND channel=?",
            (now, item["gate_id"], item["channel"]),
        )
    for key, table in (
        ("github_intents", "github_human_review_outbox"),
        ("slack_intents", "slack_human_review_outbox"),
    ):
        for item in action.before.get(key, ()):
            conn.execute(
                f"UPDATE {table} SET state='superseded', next_attempt_at=NULL, "
                "updated_at=? WHERE id=?",
                (now, item["id"]),
            )


def _apply_action(
    conn: sqlite3.Connection,
    action: MigrationAction,
    *,
    now: int,
) -> None:
    if action.kind == "backfill_gate_delivery":
        _apply_backfill_delivery(conn, action)
        return
    if action.kind in {"suppress_gate_bundle", "suppress_outbox_bundle"}:
        if action.kind == "suppress_outbox_bundle":
            before = {
                "github_intents": (
                    action.before["rows"]
                    if action.before["surface"] == "github"
                    else []
                ),
                "slack_intents": (
                    action.before["rows"] if action.before["surface"] == "slack" else []
                ),
            }
            after = {
                "github_intents": (
                    action.after["rows"] if action.after["surface"] == "github" else []
                ),
                "slack_intents": (
                    action.after["rows"] if action.after["surface"] == "slack" else []
                ),
            }
            normalized = MigrationAction(
                action_id=action.action_id,
                idempotency_key=action.idempotency_key,
                kind=action.kind,
                target_type=action.target_type,
                target_ids=action.target_ids,
                repository=action.repository,
                pr_number=action.pr_number,
                head_sha=action.head_sha,
                required_pr_state=action.required_pr_state,
                source_evidence=action.source_evidence,
                before=before,
                after=after,
                rollback=action.rollback,
            )
            _apply_suppression_bundle(conn, normalized, now=now)
            return
        _apply_suppression_bundle(conn, action, now=now)
        return
    raise MigrationBoundaryError(f"unsupported action kind: {action.kind}")


def _checkpoint_count(conn: sqlite3.Connection, plan_id: str) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM review_migration_checkpoints WHERE plan_id=?",
            (plan_id,),
        ).fetchone()[0]
    )


def _apply_migration_plan_under_lease(
    conn: sqlite3.Connection,
    plan: MigrationPlan,
    *,
    snapshot_provider: MigrationSnapshotProvider,
    confirmation: str,
    operator: str,
    now: Optional[int] = None,
    max_actions: Optional[int] = None,
) -> MigrationExecutionReceipt:
    """Apply bounded local actions with exact-head readback and durable checkpoints."""
    if confirmation != plan.apply_confirmation:
        raise MigrationConfirmationRequired(
            f"write mode requires exact confirmation {plan.apply_confirmation!r}"
        )
    if plan.blocked:
        raise MigrationBoundaryError(
            "migration plan is blocked by ambiguous, duplicate, or orphaned evidence"
        )
    if not operator.strip():
        raise MigrationBoundaryError("operator identity is required for write mode")
    if max_actions is not None and max_actions < 1:
        raise MigrationBoundaryError("max_actions must be positive")
    changed_at = int(time.time()) if now is None else int(now)
    _persist_plan(conn, plan, now=changed_at)
    status_row = conn.execute(
        "SELECT status FROM review_migration_plans WHERE id=?", (plan.plan_id,)
    ).fetchone()
    if status_row["status"] in {"rolled_back", "rollback_blocked"}:
        return MigrationExecutionReceipt(
            plan.plan_id,
            status_row["status"],
            (),
            tuple(action.action_id for action in plan.actions),
            (),
            _checkpoint_count(conn, plan.plan_id),
        )

    applied: list[str] = []
    skipped: list[str] = []
    for action in plan.actions:
        action_row = conn.execute(
            "SELECT status FROM review_migration_actions "
            "WHERE plan_id=? AND action_id=?",
            (plan.plan_id, action.action_id),
        ).fetchone()
        if action_row is None:
            raise MigrationConflict("persisted migration action disappeared")
        if action_row["status"] == "applied":
            skipped.append(action.action_id)
            continue
        if action_row["status"] != "pending":
            raise MigrationConflict(
                f"migration action {action.action_id} has invalid status "
                f"{action_row['status']!r}"
            )
        if max_actions is not None and len(applied) >= max_actions:
            break
        _validate_snapshot_for_action(action, snapshot_provider, now=changed_at)
        with kb.write_txn(conn):
            current = conn.execute(
                "SELECT status FROM review_migration_actions "
                "WHERE plan_id=? AND action_id=?",
                (plan.plan_id, action.action_id),
            ).fetchone()
            if current["status"] == "applied":
                skipped.append(action.action_id)
                continue
            if current["status"] != "pending":
                raise MigrationConflict(
                    "migration action status changed while applying"
                )
            _apply_action(conn, action, now=changed_at)
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 "
                    "FROM review_migration_checkpoints WHERE plan_id=?",
                    (plan.plan_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO review_migration_checkpoints (
                    plan_id, action_id, sequence, phase, status, operator,
                    before_json, after_json, rollback_json, recorded_at
                ) VALUES (?, ?, ?, 'apply', 'applied', ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    action.action_id,
                    sequence,
                    operator.strip(),
                    _canonical_json(action.before),
                    _canonical_json(action.after),
                    _canonical_json(action.rollback),
                    changed_at,
                ),
            )
            conn.execute(
                "UPDATE review_migration_actions SET status='applied', applied_at=? "
                "WHERE plan_id=? AND action_id=? AND status='pending'",
                (changed_at, plan.plan_id, action.action_id),
            )
            conn.execute(
                "UPDATE review_migration_plans SET status='in_progress', "
                "checkpoint_count=checkpoint_count+1, updated_at=?, last_error=NULL "
                "WHERE id=?",
                (changed_at, plan.plan_id),
            )
        applied.append(action.action_id)

    pending = tuple(
        row["action_id"]
        for row in conn.execute(
            "SELECT action_id FROM review_migration_actions "
            "WHERE plan_id=? AND status='pending' ORDER BY ordinal",
            (plan.plan_id,),
        ).fetchall()
    )
    status: PlanStatus = "in_progress"
    if not pending:
        status = "completed"
        with kb.write_txn(conn):
            conn.execute(
                "UPDATE review_migration_plans SET status='completed', "
                "completed_at=COALESCE(completed_at, ?), updated_at=? WHERE id=?",
                (changed_at, changed_at, plan.plan_id),
            )
    return MigrationExecutionReceipt(
        plan_id=plan.plan_id,
        status=status,
        applied_action_ids=tuple(applied),
        skipped_action_ids=tuple(skipped),
        pending_action_ids=pending,
        checkpoint_count=_checkpoint_count(conn, plan.plan_id),
    )


@contextlib.contextmanager
def _exclusive_review_boundary_lease(
    conn: sqlite3.Connection,
    *,
    owner_id: str,
    now: int,
) -> Iterator[None]:
    """Exclude the outbox runner while migration state is being changed."""
    lease = review_runner.acquire_runner_lease(
        conn,
        owner_id=owner_id,
        now=int(now),
        lease_seconds=review_runner.MAX_LEASE_SECONDS,
    )
    if not lease.acquired:
        raise MigrationConflict(
            "review boundary runner lease is held by "
            f"{lease.previous_owner_id or 'another owner'} until {lease.expires_at}"
        )
    try:
        yield
    except BaseException:
        review_runner.release_runner_lease(conn, owner_id=owner_id)
        raise
    else:
        if not review_runner.release_runner_lease(conn, owner_id=owner_id):
            raise MigrationConflict("migration lost its review boundary runner lease")


def apply_migration_plan(
    conn: sqlite3.Connection,
    plan: MigrationPlan,
    *,
    snapshot_provider: MigrationSnapshotProvider,
    confirmation: str,
    operator: str,
    now: Optional[int] = None,
    max_actions: Optional[int] = None,
) -> MigrationExecutionReceipt:
    """Apply one plan while holding the shared review/outbox runner lease."""
    if confirmation != plan.apply_confirmation:
        raise MigrationConfirmationRequired(
            f"write mode requires exact confirmation {plan.apply_confirmation!r}"
        )
    if plan.blocked:
        raise MigrationBoundaryError(
            "migration plan is blocked by ambiguous, duplicate, or orphaned evidence"
        )
    if not operator.strip():
        raise MigrationBoundaryError("operator identity is required for write mode")
    if max_actions is not None and max_actions < 1:
        raise MigrationBoundaryError("max_actions must be positive")
    changed_at = int(time.time()) if now is None else int(now)
    owner_id = f"review-migration:{plan.plan_id}:{operator.strip()}"
    with _exclusive_review_boundary_lease(conn, owner_id=owner_id, now=changed_at):
        return _apply_migration_plan_under_lease(
            conn,
            plan,
            snapshot_provider=snapshot_provider,
            confirmation=confirmation,
            operator=operator,
            now=changed_at,
            max_actions=max_actions,
        )


def _rollback_migration_plan_under_lease(
    conn: sqlite3.Connection,
    plan: MigrationPlan,
    *,
    confirmation: str,
    operator: str,
    now: Optional[int] = None,
    max_actions: Optional[int] = None,
) -> MigrationRollbackReceipt:
    """Rollback unchanged insert-only actions; suppression requires manual recovery."""
    if confirmation != plan.rollback_confirmation:
        raise MigrationConfirmationRequired(
            f"rollback requires exact confirmation {plan.rollback_confirmation!r}"
        )
    if not operator.strip():
        raise MigrationBoundaryError("operator identity is required for rollback")
    if max_actions is not None and max_actions < 1:
        raise MigrationBoundaryError("max_actions must be positive")
    changed_at = int(time.time()) if now is None else int(now)
    rolled_back: list[str] = []
    recovery_required: list[str] = []
    applied_rows = conn.execute(
        "SELECT action_id FROM review_migration_actions "
        "WHERE plan_id=? AND status='applied' ORDER BY ordinal DESC",
        (plan.plan_id,),
    ).fetchall()
    action_by_id = {action.action_id: action for action in plan.actions}
    for row in applied_rows:
        action = action_by_id[row["action_id"]]
        if action.rollback.get("mode") != "automatic":
            recovery_required.append(action.action_id)
            continue
        if max_actions is not None and len(rolled_back) >= max_actions:
            break
        if action.kind != "backfill_gate_delivery":
            recovery_required.append(action.action_id)
            continue
        expected = action.after["row"]
        with kb.write_txn(conn):
            current = conn.execute(
                "SELECT * FROM review_gate_deliveries WHERE gate_id=? AND channel=?",
                (expected["gate_id"], expected["channel"]),
            ).fetchone()
            if current is None:
                raise MigrationConflict("rollback target disappeared after apply")
            if not _mapping_matches(current, expected):
                raise MigrationConflict("rollback target changed after apply")
            conn.execute(
                "DELETE FROM review_gate_deliveries WHERE gate_id=? AND channel=?",
                (expected["gate_id"], expected["channel"]),
            )
            sequence = int(
                conn.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 "
                    "FROM review_migration_checkpoints WHERE plan_id=?",
                    (plan.plan_id,),
                ).fetchone()[0]
            )
            conn.execute(
                """
                INSERT INTO review_migration_checkpoints (
                    plan_id, action_id, sequence, phase, status, operator,
                    before_json, after_json, rollback_json, recorded_at
                ) VALUES (?, ?, ?, 'rollback', 'rolled_back', ?, ?, ?, ?, ?)
                """,
                (
                    plan.plan_id,
                    action.action_id,
                    sequence,
                    operator.strip(),
                    _canonical_json(action.after),
                    _canonical_json(action.before),
                    _canonical_json(action.rollback),
                    changed_at,
                ),
            )
            conn.execute(
                "UPDATE review_migration_actions SET status='rolled_back', "
                "rolled_back_at=? WHERE plan_id=? AND action_id=? AND status='applied'",
                (changed_at, plan.plan_id, action.action_id),
            )
            conn.execute(
                "UPDATE review_migration_plans SET checkpoint_count=checkpoint_count+1, "
                "updated_at=? WHERE id=?",
                (changed_at, plan.plan_id),
            )
        rolled_back.append(action.action_id)

    remaining_applied = int(
        conn.execute(
            "SELECT COUNT(*) FROM review_migration_actions "
            "WHERE plan_id=? AND status='applied'",
            (plan.plan_id,),
        ).fetchone()[0]
    )
    status: PlanStatus
    if recovery_required:
        status = "rollback_blocked"
    elif remaining_applied:
        status = "in_progress"
    else:
        status = "rolled_back"
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE review_migration_plans SET status=?, updated_at=?, "
            "rollback_completed_at=CASE WHEN ?='rolled_back' THEN ? "
            "ELSE rollback_completed_at END, last_error=? WHERE id=?",
            (
                status,
                changed_at,
                status,
                changed_at,
                (
                    "manual recovery required for: " + ",".join(recovery_required)
                    if recovery_required
                    else None
                ),
                plan.plan_id,
            ),
        )
    return MigrationRollbackReceipt(
        plan_id=plan.plan_id,
        status=status,
        rolled_back_action_ids=tuple(rolled_back),
        recovery_required_action_ids=tuple(recovery_required),
        checkpoint_count=_checkpoint_count(conn, plan.plan_id),
    )


def rollback_migration_plan(
    conn: sqlite3.Connection,
    plan: MigrationPlan,
    *,
    confirmation: str,
    operator: str,
    now: Optional[int] = None,
    max_actions: Optional[int] = None,
) -> MigrationRollbackReceipt:
    """Rollback automatic actions while excluding the review/outbox runner."""
    if confirmation != plan.rollback_confirmation:
        raise MigrationConfirmationRequired(
            f"rollback requires exact confirmation {plan.rollback_confirmation!r}"
        )
    if not operator.strip():
        raise MigrationBoundaryError("operator identity is required for rollback")
    if max_actions is not None and max_actions < 1:
        raise MigrationBoundaryError("max_actions must be positive")
    changed_at = int(time.time()) if now is None else int(now)
    owner_id = f"review-migration-rollback:{plan.plan_id}:{operator.strip()}"
    with _exclusive_review_boundary_lease(conn, owner_id=owner_id, now=changed_at):
        return _rollback_migration_plan_under_lease(
            conn,
            plan,
            confirmation=confirmation,
            operator=operator,
            now=changed_at,
            max_actions=max_actions,
        )


def migration_status(
    conn: sqlite3.Connection,
    *,
    plan_id: Optional[str] = None,
) -> list[dict[str, Any]]:
    where = " WHERE id=?" if plan_id else ""
    params: tuple[Any, ...] = (plan_id,) if plan_id else ()
    rows = conn.execute(
        "SELECT id, idempotency_key, policy_version, status, action_count, "
        "checkpoint_count, created_at, updated_at, completed_at, "
        "rollback_completed_at, last_error FROM review_migration_plans"
        + where
        + " ORDER BY created_at, id",
        params,
    ).fetchall()
    return [dict(row) for row in rows]
