"""Deterministic, read-only-first cross-system reconciliation for review gates.

The reconciliation boundary joins normalized Linear intent, authoritative
GitHub pull-request readback, CodeRabbit evidence, Kanban human gates, and
GitHub/Slack outbox state.  Building a report is pure and deterministic.
Persisting a run writes only immutable audit rows in ``reconciliation_*``;
there is deliberately no repair, migration, notification, approval, merge,
provider mutation, board transition, or external-delivery capability here.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Iterable, Literal, Optional, Protocol, Sequence

from hermes_cli import kanban_coderabbit as coderabbit
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_github as github
from hermes_cli import kanban_human_review as human_review
from hermes_cli import kanban_linear as linear
from hermes_cli import kanban_slack as slack


SCHEMA_VERSION = 1
POLICY_VERSION = "exact-head-reconciliation-v1"
DEFAULT_MAX_PULL_REQUESTS = 500
DEFAULT_MAX_SOURCE_RECORDS = 10_000

Severity = Literal["info", "warning", "error", "critical"]
ReportStatus = Literal["healthy", "needs_attention", "blocked"]
ReadStatus = Literal["ok", "unavailable", "stale", "future"]
FindingCategory = Literal[
    "current_gates",
    "stale_heads",
    "terminal_prs",
    "missing_qa_evidence",
    "actionable_coderabbit_findings",
    "missing_outbox_rows",
    "duplicate_semantic_rows",
    "orphaned_records",
    "conflicting_source_revisions",
]

FINDING_CATEGORIES = frozenset({
    "current_gates",
    "stale_heads",
    "terminal_prs",
    "missing_qa_evidence",
    "actionable_coderabbit_findings",
    "missing_outbox_rows",
    "duplicate_semantic_rows",
    "orphaned_records",
    "conflicting_source_revisions",
})
SEVERITIES = frozenset({"info", "warning", "error", "critical"})
SEVERITY_RANK = {"critical": 0, "error": 1, "warning": 2, "info": 3}
CATEGORY_RANK = {
    "stale_heads": 0,
    "terminal_prs": 1,
    "conflicting_source_revisions": 2,
    "actionable_coderabbit_findings": 3,
    "missing_qa_evidence": 4,
    "missing_outbox_rows": 5,
    "duplicate_semantic_rows": 6,
    "orphaned_records": 7,
    "current_gates": 8,
}
ACTIVE_GATE_STATES = frozenset(human_review.ACTIVE_GATE_STATES)
ACTIVE_OUTBOX_STATES = frozenset({
    "pending",
    "attempting",
    "retry",
    "sent",
    "permanent_failure",
})
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_REPOSITORY_RE = re.compile(r"^[^/\s]+/[^/\s]+$")


class ReconciliationBoundaryError(ValueError):
    """Normalized reconciliation input or persisted report is inconsistent."""


class ReconciliationSnapshotProvider(Protocol):
    """Read-only provider for authoritative GitHub pull-request snapshots."""

    def read_snapshot(
        self,
        *,
        repository: str,
        pr_number: int,
    ) -> Optional[github.GitHubPullRequestSnapshot]: ...


@dataclass(frozen=True, order=True)
class PullRequestIdentity:
    repository: str
    pr_number: int

    def __post_init__(self) -> None:
        repository = str(self.repository or "").strip().casefold()
        if not _REPOSITORY_RE.fullmatch(repository):
            raise ReconciliationBoundaryError(
                "repository must use the canonical owner/name form"
            )
        if isinstance(self.pr_number, bool):
            raise ReconciliationBoundaryError("pr_number must be a positive integer")
        try:
            number = int(self.pr_number)
        except (TypeError, ValueError) as exc:
            raise ReconciliationBoundaryError(
                "pr_number must be a positive integer"
            ) from exc
        if number < 1:
            raise ReconciliationBoundaryError("pr_number must be a positive integer")
        object.__setattr__(self, "repository", repository)
        object.__setattr__(self, "pr_number", number)


@dataclass(frozen=True)
class LinearIssuePullRequestLink:
    linear_issue_id: str
    ref: PullRequestIdentity
    first_seen_revision: int
    last_seen_revision: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "LinearIssuePullRequestLink":
        return cls(
            linear_issue_id=row["linear_issue_id"],
            ref=PullRequestIdentity(row["repository"], int(row["pr_number"])),
            first_seen_revision=int(row["first_seen_revision"]),
            last_seen_revision=int(row["last_seen_revision"]),
        )


@dataclass(frozen=True)
class CodeRabbitHeadPointer:
    ref: PullRequestIdentity
    current_head_sha: str
    observed_at: int

    def __post_init__(self) -> None:
        head_sha = str(self.current_head_sha or "").strip().casefold()
        if not _FULL_SHA_RE.fullmatch(head_sha):
            raise ReconciliationBoundaryError(
                "CodeRabbit current_head_sha must be a full lowercase commit SHA"
            )
        object.__setattr__(self, "current_head_sha", head_sha)

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "CodeRabbitHeadPointer":
        return cls(
            ref=PullRequestIdentity(row["repository"], int(row["pr_number"])),
            current_head_sha=row["current_head_sha"],
            observed_at=int(row["observed_at"]),
        )


@dataclass(frozen=True)
class KanbanTaskState:
    task_id: str
    status: str
    assignee: Optional[str]
    current_run_id: Optional[int]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "KanbanTaskState":
        return cls(
            task_id=row["id"],
            status=row["status"],
            assignee=row["assignee"],
            current_run_id=(
                int(row["current_run_id"])
                if row["current_run_id"] is not None
                else None
            ),
        )


@dataclass(frozen=True)
class TrustedPullRequestRead:
    requested: PullRequestIdentity
    status: ReadStatus
    snapshot: Optional[github.GitHubPullRequestSnapshot]

    def __post_init__(self) -> None:
        if self.status not in {"ok", "unavailable", "stale", "future"}:
            raise ReconciliationBoundaryError(
                f"unsupported trusted snapshot status: {self.status!r}"
            )
        if self.status == "unavailable" and self.snapshot is not None:
            raise ReconciliationBoundaryError(
                "an unavailable trusted snapshot cannot contain provider data"
            )
        if self.status != "unavailable" and not isinstance(
            self.snapshot,
            github.GitHubPullRequestSnapshot,
        ):
            raise ReconciliationBoundaryError(
                "trusted snapshot reads must contain a GitHubPullRequestSnapshot"
            )


@dataclass(frozen=True)
class ReconciliationInputs:
    coordinators: tuple[linear.LinearIssueCoordinator, ...] = ()
    links: tuple[LinearIssuePullRequestLink, ...] = ()
    stored_pr_aggregates: tuple[linear.PullRequestAggregate, ...] = ()
    trusted_pr_reads: tuple[TrustedPullRequestRead, ...] = ()
    coderabbit_heads: tuple[CodeRabbitHeadPointer, ...] = ()
    coderabbit_assessments: tuple[coderabbit.CodeRabbitAssessment, ...] = ()
    task_states: tuple[KanbanTaskState, ...] = ()
    human_gates: tuple[human_review.HumanReviewGate, ...] = ()
    gate_deliveries: tuple[human_review.ReviewGateDelivery, ...] = ()
    github_intents: tuple[github.GitHubOutboxIntent, ...] = ()
    slack_intents: tuple[slack.SlackOutboxIntent, ...] = ()
    slack_acknowledgements: tuple[slack.SlackAcknowledgementReceipt, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "coordinators",
            tuple(
                sorted(
                    self.coordinators,
                    key=lambda item: (
                        item.issue_id,
                        item.source_revision,
                        item.snapshot_sha256,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "links",
            tuple(
                sorted(
                    self.links,
                    key=lambda item: (
                        item.linear_issue_id,
                        item.ref.repository,
                        item.ref.pr_number,
                        item.first_seen_revision,
                        item.last_seen_revision,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "stored_pr_aggregates",
            tuple(
                sorted(
                    self.stored_pr_aggregates,
                    key=lambda item: (
                        item.ref.repository,
                        item.ref.number,
                        item.provider_revision,
                        item.snapshot_sha256,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "trusted_pr_reads",
            tuple(
                sorted(
                    self.trusted_pr_reads,
                    key=lambda item: (
                        item.requested.repository,
                        item.requested.pr_number,
                        item.status,
                        item.snapshot.snapshot_sha256() if item.snapshot else "",
                        item.snapshot.observation_id if item.snapshot else "",
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "coderabbit_heads",
            tuple(
                sorted(
                    self.coderabbit_heads,
                    key=lambda item: (
                        item.ref.repository,
                        item.ref.pr_number,
                        item.observed_at,
                        item.current_head_sha,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "coderabbit_assessments",
            tuple(
                sorted(
                    self.coderabbit_assessments,
                    key=lambda item: (
                        item.repository,
                        item.pr_number,
                        item.head_sha,
                        item.review_generation,
                        item.snapshot_sha256,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "task_states",
            tuple(sorted(self.task_states, key=lambda item: item.task_id)),
        )
        object.__setattr__(
            self,
            "human_gates",
            tuple(
                sorted(
                    self.human_gates,
                    key=lambda item: (
                        item.repo,
                        item.pr_number,
                        item.approved_head_sha,
                        item.id,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "gate_deliveries",
            tuple(
                sorted(
                    self.gate_deliveries,
                    key=lambda item: (item.gate_id, item.channel, item.dedupe_marker),
                )
            ),
        )
        object.__setattr__(
            self,
            "github_intents",
            tuple(
                sorted(
                    self.github_intents,
                    key=lambda item: (
                        item.repository,
                        item.pr_number,
                        item.head_sha,
                        item.surface,
                        item.operation,
                        item.id,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "slack_intents",
            tuple(
                sorted(
                    self.slack_intents,
                    key=lambda item: (
                        item.repository,
                        item.pr_number,
                        item.head_sha,
                        item.channel_id,
                        item.thread_ts,
                        item.surface,
                        item.operation,
                        item.id,
                    ),
                )
            ),
        )
        object.__setattr__(
            self,
            "slack_acknowledgements",
            tuple(
                sorted(
                    self.slack_acknowledgements,
                    key=lambda item: (
                        item.source_intent_id,
                        item.observed_at,
                        item.id,
                    ),
                )
            ),
        )

    def normalized_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "coordinators": [asdict(item) for item in self.coordinators],
            "links": [asdict(item) for item in self.links],
            "stored_pr_aggregates": [
                asdict(item) for item in self.stored_pr_aggregates
            ],
            "trusted_pr_reads": [asdict(item) for item in self.trusted_pr_reads],
            "coderabbit_heads": [asdict(item) for item in self.coderabbit_heads],
            "coderabbit_assessments": [
                asdict(item) for item in self.coderabbit_assessments
            ],
            "task_states": [asdict(item) for item in self.task_states],
            "human_gates": [asdict(item) for item in self.human_gates],
            "gate_deliveries": [asdict(item) for item in self.gate_deliveries],
            "github_intents": [asdict(item) for item in self.github_intents],
            "slack_intents": [asdict(item) for item in self.slack_intents],
            "slack_acknowledgements": [
                asdict(item) for item in self.slack_acknowledgements
            ],
        }

    def input_sha256(self) -> str:
        return _sha256_text(_canonical_json(self.normalized_dict()))


@dataclass(frozen=True)
class ReconciliationFinding:
    key: str
    code: str
    category: FindingCategory
    severity: Severity
    source: str
    summary: str
    recommendation: str
    entity_ids: tuple[str, ...] = ()
    linear_issue_id: Optional[str] = None
    repository: Optional[str] = None
    pr_number: Optional[int] = None
    expected_head_sha: Optional[str] = None
    observed_head_sha: Optional[str] = None

    def __post_init__(self) -> None:
        if self.category not in FINDING_CATEGORIES:
            raise ReconciliationBoundaryError(
                f"unsupported reconciliation category: {self.category!r}"
            )
        if self.severity not in SEVERITIES:
            raise ReconciliationBoundaryError(
                f"unsupported reconciliation severity: {self.severity!r}"
            )
        if not self.key.startswith("rcf_"):
            raise ReconciliationBoundaryError("finding key must use the rcf_ prefix")
        if not self.code.strip() or not self.source.strip():
            raise ReconciliationBoundaryError("finding code and source are required")
        if not self.summary.strip() or not self.recommendation.strip():
            raise ReconciliationBoundaryError(
                "finding summary and safe recommendation are required"
            )
        object.__setattr__(self, "entity_ids", tuple(sorted(set(self.entity_ids))))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "code": self.code,
            "category": self.category,
            "severity": self.severity,
            "source": self.source,
            "summary": self.summary,
            "recommendation": self.recommendation,
            "entity_ids": list(self.entity_ids),
            "linear_issue_id": self.linear_issue_id,
            "repository": self.repository,
            "pr_number": self.pr_number,
            "expected_head_sha": self.expected_head_sha,
            "observed_head_sha": self.observed_head_sha,
            "automatic_action": "none",
            "external_write_permitted": False,
        }


@dataclass(frozen=True)
class ReconciliationReport:
    input_sha256: str
    status: ReportStatus
    issue_count: int
    pull_request_count: int
    findings: tuple[ReconciliationFinding, ...]

    def category_counts(self) -> dict[str, int]:
        counts = {category: 0 for category in sorted(FINDING_CATEGORIES)}
        for finding in self.findings:
            counts[finding.category] += 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy_version": POLICY_VERSION,
            "input_sha256": self.input_sha256,
            "status": self.status,
            "issue_count": self.issue_count,
            "pull_request_count": self.pull_request_count,
            "finding_count": len(self.findings),
            "category_counts": self.category_counts(),
            "findings": [finding.to_dict() for finding in self.findings],
            "safety": {
                "mode": "read_only_audit_recommendation",
                "automatic_actions": [],
                "external_writes": False,
                "board_mutation": False,
                "merge": False,
                "approval": False,
                "notification": False,
                "migration": False,
            },
        }

    def to_json(self) -> str:
        return _canonical_json(self.to_dict())

    def report_sha256(self) -> str:
        return _sha256_text(self.to_json())

    def to_markdown(self) -> str:
        lines = [
            "# Cross-system reconciliation report",
            "",
            f"- Status: `{self.status}`",
            f"- Policy: `{POLICY_VERSION}`",
            f"- Input SHA-256: `{self.input_sha256}`",
            f"- Linear issues checked: {self.issue_count}",
            f"- Pull requests checked: {self.pull_request_count}",
            f"- Findings: {len(self.findings)}",
            "- Safety: read-only audit/recommendation; no automatic writes or transitions",
            "",
            "## Findings",
            "",
        ]
        if not self.findings:
            lines.append("No findings.")
        else:
            lines.extend([
                "| Severity | Category | Source | Identity | Summary | Safe recommendation |",
                "|---|---|---|---|---|---|",
            ])
            for finding in self.findings:
                identity_parts = []
                if finding.linear_issue_id:
                    identity_parts.append(finding.linear_issue_id)
                if finding.repository and finding.pr_number is not None:
                    identity_parts.append(f"{finding.repository}#{finding.pr_number}")
                if finding.expected_head_sha:
                    identity_parts.append(f"expected {finding.expected_head_sha}")
                if finding.observed_head_sha:
                    identity_parts.append(f"observed {finding.observed_head_sha}")
                identity_parts.extend(finding.entity_ids)
                identity = ", ".join(identity_parts) or finding.key
                lines.append(
                    "| "
                    + " | ".join(
                        _markdown_cell(value)
                        for value in (
                            finding.severity,
                            finding.category,
                            finding.source,
                            identity,
                            finding.summary,
                            finding.recommendation,
                        )
                    )
                    + " |"
                )
        lines.extend([
            "",
            "## Safety boundary",
            "",
            "This report cannot merge, approve, notify, migrate, mutate Linear or GitHub, "
            "change Kanban gate state, post to Slack, or repair source rows. Any later "
            "action must re-read authoritative current-head evidence.",
            "",
        ])
        return "\n".join(lines)


@dataclass(frozen=True)
class ReconciliationRunReceipt:
    run_id: str
    created: bool
    input_sha256: str
    report_sha256: str
    markdown_sha256: str
    status: ReportStatus
    finding_count: int


@dataclass(frozen=True)
class ReconciliationExecution:
    inputs: ReconciliationInputs
    report: ReconciliationReport
    persisted_run: Optional[ReconciliationRunReceipt]


@dataclass(frozen=True)
class _FindingContext:
    code: str
    category: FindingCategory
    severity: Severity
    source: str
    summary: str
    recommendation: str
    entity_ids: tuple[str, ...] = ()
    linear_issue_id: Optional[str] = None
    repository: Optional[str] = None
    pr_number: Optional[int] = None
    expected_head_sha: Optional[str] = None
    observed_head_sha: Optional[str] = None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _markdown_cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _identity(repository: str, pr_number: int) -> PullRequestIdentity:
    return PullRequestIdentity(repository, pr_number)


def _finding(context: _FindingContext) -> ReconciliationFinding:
    identity = {
        "policy_version": POLICY_VERSION,
        "code": context.code,
        "category": context.category,
        "source": context.source,
        "entity_ids": sorted(set(context.entity_ids)),
        "linear_issue_id": context.linear_issue_id,
        "repository": context.repository,
        "pr_number": context.pr_number,
        "expected_head_sha": context.expected_head_sha,
        "observed_head_sha": context.observed_head_sha,
    }
    key = "rcf_" + _sha256_text(_canonical_json(identity))[:24]
    return ReconciliationFinding(
        key=key,
        code=context.code,
        category=context.category,
        severity=context.severity,
        source=context.source,
        summary=context.summary,
        recommendation=context.recommendation,
        entity_ids=context.entity_ids,
        linear_issue_id=context.linear_issue_id,
        repository=context.repository,
        pr_number=context.pr_number,
        expected_head_sha=context.expected_head_sha,
        observed_head_sha=context.observed_head_sha,
    )


def _group_by(
    records: Iterable[Any],
    key: Callable[[Any], Any],
) -> dict[Any, list[Any]]:
    grouped: dict[Any, list[Any]] = {}
    for record in records:
        grouped.setdefault(key(record), []).append(record)
    return grouped


def _ref_for_aggregate(item: linear.PullRequestAggregate) -> PullRequestIdentity:
    return _identity(item.ref.repository, item.ref.number)


def _ref_for_assessment(item: coderabbit.CodeRabbitAssessment) -> PullRequestIdentity:
    return _identity(item.repository, item.pr_number)


def _ref_for_gate(item: human_review.HumanReviewGate) -> PullRequestIdentity:
    return _identity(item.repo, item.pr_number)


def _ref_for_github_intent(item: github.GitHubOutboxIntent) -> PullRequestIdentity:
    return _identity(item.repository, item.pr_number)


def _ref_for_slack_intent(item: slack.SlackOutboxIntent) -> PullRequestIdentity:
    return _identity(item.repository, item.pr_number)


def _selected_by_refs(
    records: Iterable[Any],
    refs: set[PullRequestIdentity],
    ref_fn: Callable[[Any], PullRequestIdentity],
) -> tuple[Any, ...]:
    return tuple(record for record in records if ref_fn(record) in refs)


def collect_reconciliation_inputs(
    conn: sqlite3.Connection,
    *,
    snapshot_provider: ReconciliationSnapshotProvider,
    linear_issue_ids: Optional[Sequence[str]] = None,
    now: Optional[int] = None,
    max_pull_requests: int = DEFAULT_MAX_PULL_REQUESTS,
    max_source_records: int = DEFAULT_MAX_SOURCE_RECORDS,
) -> ReconciliationInputs:
    """Read normalized source state and trusted GitHub snapshots without writes.

    Linear rows only establish issue-to-PR intent. The current state/head for
    every referenced PR is resolved through ``snapshot_provider``; the stored
    Linear PR aggregate is retained only as drift/conflict evidence.
    """
    if max_pull_requests < 1 or max_source_records < 1:
        raise ReconciliationBoundaryError("reconciliation bounds must be positive")
    checked_at = int(time.time()) if now is None else int(now)
    requested_issue_ids = (
        {str(value).strip() for value in linear_issue_ids if str(value).strip()}
        if linear_issue_ids is not None
        else None
    )

    coordinator_rows = conn.execute(
        "SELECT * FROM linear_issue_coordinators ORDER BY linear_issue_id"
    ).fetchall()
    coordinators = tuple(
        linear.LinearIssueCoordinator.from_row(row)
        for row in coordinator_rows
        if requested_issue_ids is None or row["linear_issue_id"] in requested_issue_ids
    )
    link_rows = conn.execute(
        "SELECT * FROM linear_issue_pr_links "
        "ORDER BY linear_issue_id, repository, pr_number"
    ).fetchall()
    links = tuple(
        LinearIssuePullRequestLink.from_row(row)
        for row in link_rows
        if requested_issue_ids is None or row["linear_issue_id"] in requested_issue_ids
    )

    all_gates = tuple(
        human_review.HumanReviewGate.from_row(row)
        for row in conn.execute(
            "SELECT * FROM human_review_gates ORDER BY repo, pr_number, created_at, id"
        ).fetchall()
    )
    human_gates = tuple(
        gate
        for gate in all_gates
        if requested_issue_ids is None or gate.linear_issue_id in requested_issue_ids
    )
    refs = {link.ref for link in links}
    refs.update(_ref_for_gate(gate) for gate in human_gates)

    all_aggregates = tuple(
        linear.PullRequestAggregate.from_row(row)
        for row in conn.execute(
            "SELECT * FROM linear_pr_aggregates ORDER BY repository, pr_number"
        ).fetchall()
    )
    all_heads = tuple(
        CodeRabbitHeadPointer.from_row(row)
        for row in conn.execute(
            "SELECT * FROM coderabbit_pr_heads ORDER BY repository, pr_number"
        ).fetchall()
    )
    all_assessments = coderabbit.list_assessments(conn)
    all_github_intents = tuple(
        github.GitHubOutboxIntent.from_row(row)
        for row in conn.execute(
            "SELECT * FROM github_human_review_outbox "
            "ORDER BY repository, pr_number, created_at, id"
        ).fetchall()
    )
    all_slack_intents = tuple(
        slack.SlackOutboxIntent.from_row(row)
        for row in conn.execute(
            "SELECT * FROM slack_human_review_outbox "
            "ORDER BY repository, pr_number, created_at, id"
        ).fetchall()
    )

    if requested_issue_ids is None:
        stored_pr_aggregates = all_aggregates
        coderabbit_heads = all_heads
        coderabbit_assessments = all_assessments
        github_intents = all_github_intents
        slack_intents = all_slack_intents
        refs.update(_ref_for_aggregate(item) for item in all_aggregates)
        refs.update(item.ref for item in all_heads)
        refs.update(_ref_for_assessment(item) for item in all_assessments)
        refs.update(_ref_for_github_intent(item) for item in all_github_intents)
        refs.update(_ref_for_slack_intent(item) for item in all_slack_intents)
    else:
        stored_pr_aggregates = _selected_by_refs(
            all_aggregates, refs, _ref_for_aggregate
        )
        coderabbit_heads = _selected_by_refs(all_heads, refs, lambda item: item.ref)
        coderabbit_assessments = _selected_by_refs(
            all_assessments,
            refs,
            _ref_for_assessment,
        )
        github_intents = _selected_by_refs(
            all_github_intents,
            refs,
            _ref_for_github_intent,
        )
        slack_intents = _selected_by_refs(
            all_slack_intents,
            refs,
            _ref_for_slack_intent,
        )

    if len(refs) > max_pull_requests:
        raise ReconciliationBoundaryError(
            f"reconciliation scope has {len(refs)} PRs; maximum is {max_pull_requests}"
        )

    gate_ids = {gate.id for gate in human_gates}
    gate_deliveries = tuple(
        human_review.ReviewGateDelivery.from_row(row)
        for row in conn.execute(
            "SELECT * FROM review_gate_deliveries ORDER BY gate_id, channel"
        ).fetchall()
        if requested_issue_ids is None or row["gate_id"] in gate_ids
    )
    slack_intent_ids = {intent.id for intent in slack_intents}
    slack_acknowledgements = tuple(
        slack.SlackAcknowledgementReceipt.from_row(row)
        for row in conn.execute(
            "SELECT * FROM slack_human_review_acknowledgements "
            "ORDER BY source_intent_id, observed_at, id"
        ).fetchall()
        if requested_issue_ids is None or row["source_intent_id"] in slack_intent_ids
    )

    relevant_task_ids = {
        task_id
        for gate in human_gates
        for task_id in (gate.task_id, gate.implementation_task_id, gate.qa_task_id)
    }
    task_states = tuple(
        KanbanTaskState.from_row(row)
        for row in conn.execute(
            "SELECT id, status, assignee, current_run_id FROM tasks ORDER BY id"
        ).fetchall()
        if row["id"] in relevant_task_ids
    )

    source_count = sum(
        len(values)
        for values in (
            coordinators,
            links,
            stored_pr_aggregates,
            coderabbit_heads,
            coderabbit_assessments,
            task_states,
            human_gates,
            gate_deliveries,
            github_intents,
            slack_intents,
            slack_acknowledgements,
        )
    )
    if source_count > max_source_records:
        raise ReconciliationBoundaryError(
            f"reconciliation scope has {source_count} source rows; "
            f"maximum is {max_source_records}"
        )

    trusted_reads: list[TrustedPullRequestRead] = []
    for ref in sorted(refs):
        snapshot = snapshot_provider.read_snapshot(
            repository=ref.repository,
            pr_number=ref.pr_number,
        )
        if snapshot is None:
            trusted_reads.append(TrustedPullRequestRead(ref, "unavailable", None))
            continue
        if not isinstance(snapshot, github.GitHubPullRequestSnapshot):
            raise ReconciliationBoundaryError(
                "snapshot provider returned a non-GitHubPullRequestSnapshot"
            )
        if snapshot.observed_at < checked_at - github.MAX_SNAPSHOT_AGE_SECONDS:
            status: ReadStatus = "stale"
        elif (
            snapshot.observed_at > checked_at + github.MAX_SNAPSHOT_FUTURE_SKEW_SECONDS
        ):
            status = "future"
        else:
            status = "ok"
        trusted_reads.append(TrustedPullRequestRead(ref, status, snapshot))

    return ReconciliationInputs(
        coordinators=coordinators,
        links=links,
        stored_pr_aggregates=stored_pr_aggregates,
        trusted_pr_reads=tuple(trusted_reads),
        coderabbit_heads=coderabbit_heads,
        coderabbit_assessments=coderabbit_assessments,
        task_states=task_states,
        human_gates=human_gates,
        gate_deliveries=gate_deliveries,
        github_intents=github_intents,
        slack_intents=slack_intents,
        slack_acknowledgements=slack_acknowledgements,
    )


def build_reconciliation_report(inputs: ReconciliationInputs) -> ReconciliationReport:
    """Purely reduce one normalized source bundle into deterministic findings."""
    if not isinstance(inputs, ReconciliationInputs):
        raise ReconciliationBoundaryError(
            "inputs must be a ReconciliationInputs instance"
        )
    findings: dict[str, ReconciliationFinding] = {}

    def add(context: _FindingContext) -> None:
        item = _finding(context)
        findings[item.key] = item

    coordinator_groups = _group_by(inputs.coordinators, lambda item: item.issue_id)
    coordinator_by_id: dict[str, linear.LinearIssueCoordinator] = {}
    for issue_id, group in sorted(coordinator_groups.items()):
        coordinator_by_id[issue_id] = group[-1]
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_linear_coordinator",
                    category="duplicate_semantic_rows",
                    severity="error",
                    source="linear",
                    summary="Multiple coordinator rows claim the same stable Linear issue ID.",
                    recommendation="Inspect and repair the duplicate rows in a later migration; do not choose one automatically.",
                    entity_ids=(issue_id,),
                    linear_issue_id=issue_id,
                )
            )

    link_groups = _group_by(
        inputs.links,
        lambda item: (item.linear_issue_id, item.ref),
    )
    linked_issue_ids_by_ref: dict[PullRequestIdentity, set[str]] = {}
    for (issue_id, ref), group in sorted(
        link_groups.items(),
        key=lambda item: (item[0][0], item[0][1]),
    ):
        linked_issue_ids_by_ref.setdefault(ref, set()).add(issue_id)
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_linear_pr_link",
                    category="duplicate_semantic_rows",
                    severity="error",
                    source="linear",
                    summary="The same Linear issue-to-PR association appears more than once.",
                    recommendation="Repair the duplicate association in a later migration; preserve the highest observed revision for human review.",
                    entity_ids=(issue_id,),
                    linear_issue_id=issue_id,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                )
            )
        coordinator = coordinator_by_id.get(issue_id)
        if coordinator is None:
            add(
                _FindingContext(
                    code="linear_link_without_coordinator",
                    category="orphaned_records",
                    severity="error",
                    source="linear",
                    summary="A Linear issue-to-PR link has no coordinator row.",
                    recommendation="Restore or explicitly retire the coordinator in a later migration; do not infer issue intent from the PR.",
                    entity_ids=(issue_id,),
                    linear_issue_id=issue_id,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                )
            )
        elif (
            max(item.last_seen_revision for item in group) > coordinator.source_revision
        ):
            add(
                _FindingContext(
                    code="linear_link_revision_ahead_of_coordinator",
                    category="conflicting_source_revisions",
                    severity="critical",
                    source="linear",
                    summary="A Linear PR link claims a revision newer than its coordinator snapshot.",
                    recommendation="Refresh authoritative Linear readback and audit again; do not roll either revision backward.",
                    entity_ids=(issue_id,),
                    linear_issue_id=issue_id,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                )
            )

    linked_refs_by_issue: dict[str, set[PullRequestIdentity]] = {}
    for link in inputs.links:
        linked_refs_by_issue.setdefault(link.linear_issue_id, set()).add(link.ref)
    for issue_id in sorted(coordinator_by_id):
        if not linked_refs_by_issue.get(issue_id):
            add(
                _FindingContext(
                    code="linear_coordinator_without_pr_link",
                    category="orphaned_records",
                    severity="warning",
                    source="linear",
                    summary="A Linear coordinator has no associated pull-request identity.",
                    recommendation="Confirm whether implementation has started; do not infer or create a PR link from issue prose.",
                    entity_ids=(issue_id,),
                    linear_issue_id=issue_id,
                )
            )
    for ref, issue_ids in sorted(linked_issue_ids_by_ref.items()):
        if len(issue_ids) > 1:
            add(
                _FindingContext(
                    code="pr_linked_to_multiple_linear_issues",
                    category="conflicting_source_revisions",
                    severity="error",
                    source="linear",
                    summary="One PR identity is claimed by multiple Linear coordinators.",
                    recommendation="Confirm the intended coordinator explicitly before migration, delivery, or gate advancement.",
                    entity_ids=tuple(sorted(issue_ids)),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                )
            )

    read_groups = _group_by(inputs.trusted_pr_reads, lambda item: item.requested)
    referenced_refs = set(linked_issue_ids_by_ref)
    referenced_refs.update(
        _ref_for_aggregate(item) for item in inputs.stored_pr_aggregates
    )
    referenced_refs.update(item.ref for item in inputs.coderabbit_heads)
    referenced_refs.update(
        _ref_for_assessment(item) for item in inputs.coderabbit_assessments
    )
    referenced_refs.update(_ref_for_gate(item) for item in inputs.human_gates)
    referenced_refs.update(
        _ref_for_github_intent(item) for item in inputs.github_intents
    )
    referenced_refs.update(_ref_for_slack_intent(item) for item in inputs.slack_intents)
    for ref in sorted(referenced_refs - set(read_groups)):
        add(
            _FindingContext(
                code="missing_trusted_github_read",
                category="orphaned_records",
                severity="critical",
                source="github",
                summary="A referenced PR has no authoritative GitHub snapshot read.",
                recommendation="Read the PR through the trusted GitHub adapter and repeat reconciliation; do not use stored aggregates as current truth.",
                repository=ref.repository,
                pr_number=ref.pr_number,
            )
        )
    for ref in sorted(set(read_groups) - referenced_refs):
        add(
            _FindingContext(
                code="trusted_github_read_without_source_reference",
                category="orphaned_records",
                severity="warning",
                source="github",
                summary="A trusted GitHub snapshot has no Linear, CodeRabbit, gate, or outbox source reference.",
                recommendation="Confirm the intended reconciliation scope; do not attach the snapshot to an issue automatically.",
                repository=ref.repository,
                pr_number=ref.pr_number,
            )
        )
    current_by_ref: dict[PullRequestIdentity, github.GitHubPullRequestSnapshot] = {}
    for ref, group in sorted(read_groups.items()):
        snapshot_digests = {
            item.snapshot.snapshot_sha256()
            if item.snapshot is not None
            else item.status
            for item in group
        }
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_trusted_github_read",
                    category="duplicate_semantic_rows",
                    severity="error",
                    source="github",
                    summary="More than one trusted GitHub snapshot was supplied for the same requested PR.",
                    recommendation="Collapse the provider read to one authoritative snapshot before any later action.",
                    entity_ids=tuple(sorted(snapshot_digests)),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                )
            )
        if len(snapshot_digests) > 1:
            add(
                _FindingContext(
                    code="conflicting_trusted_github_reads",
                    category="conflicting_source_revisions",
                    severity="critical",
                    source="github",
                    summary="Trusted GitHub reads disagree for the same repository and PR number.",
                    recommendation="Refresh from one authoritative GitHub adapter and stop gate advancement until the conflict is resolved.",
                    entity_ids=tuple(sorted(snapshot_digests)),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                )
            )
            continue
        read = group[0]
        if read.status != "ok" or read.snapshot is None:
            add(
                _FindingContext(
                    code=f"github_snapshot_{read.status}",
                    category="orphaned_records",
                    severity="critical" if read.status == "future" else "error",
                    source="github",
                    summary=f"Authoritative GitHub truth is {read.status} for a referenced PR.",
                    recommendation="Refresh the read-only GitHub snapshot and repeat reconciliation; do not use cached gate or review evidence.",
                    entity_ids=(read.status,),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    observed_head_sha=(
                        read.snapshot.head_sha if read.snapshot is not None else None
                    ),
                )
            )
            continue
        snapshot = read.snapshot
        observed_ref = _identity(snapshot.repository, snapshot.pr_number)
        if observed_ref != ref:
            add(
                _FindingContext(
                    code="linear_github_identity_conflict",
                    category="conflicting_source_revisions",
                    severity="critical",
                    source="cross_system",
                    summary="The trusted GitHub provider returned a different repository or PR than Linear/Kanban requested.",
                    recommendation="Correct the provider mapping and re-read both systems; never guess which PR identity was intended.",
                    entity_ids=(
                        f"requested:{ref.repository}#{ref.pr_number}",
                        f"observed:{observed_ref.repository}#{observed_ref.pr_number}",
                        snapshot.observation_id,
                    ),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    observed_head_sha=snapshot.head_sha,
                )
            )
            continue
        current_by_ref[ref] = snapshot

    aggregate_groups = _group_by(
        inputs.stored_pr_aggregates,
        _ref_for_aggregate,
    )
    aggregate_by_ref: dict[PullRequestIdentity, linear.PullRequestAggregate] = {}
    for ref, group in sorted(aggregate_groups.items()):
        aggregate_by_ref[ref] = group[-1]
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_linear_pr_aggregate",
                    category="duplicate_semantic_rows",
                    severity="error",
                    source="linear",
                    summary="Multiple stored Linear-side PR aggregates claim one repository and PR number.",
                    recommendation="Repair the duplicate aggregates in a later migration; use only fresh GitHub readback for current truth.",
                    entity_ids=tuple(item.snapshot_sha256 for item in group),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                )
            )
        if ref not in linked_issue_ids_by_ref:
            add(
                _FindingContext(
                    code="linear_pr_aggregate_without_issue_link",
                    category="orphaned_records",
                    severity="warning",
                    source="linear",
                    summary="A stored PR aggregate is not linked to any Linear coordinator.",
                    recommendation="Confirm historical ownership before retaining or removing the aggregate in a later migration.",
                    entity_ids=tuple(item.snapshot_sha256 for item in group),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                )
            )

    for ref, snapshot in sorted(current_by_ref.items()):
        aggregate = aggregate_by_ref.get(ref)
        issue_ids = sorted(linked_issue_ids_by_ref.get(ref, set()))
        primary_issue = issue_ids[0] if len(issue_ids) == 1 else None
        if snapshot.state in {"closed", "merged"}:
            add(
                _FindingContext(
                    code=f"github_pr_{snapshot.state}",
                    category="terminal_prs",
                    severity="error",
                    source="github",
                    summary=f"GitHub reports that the PR is {snapshot.state}; active gate or delivery state cannot advance it.",
                    recommendation="Review and close or archive related workflow records manually; do not send or merge automatically.",
                    entity_ids=(snapshot.observation_id,),
                    linear_issue_id=primary_issue,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    observed_head_sha=snapshot.head_sha,
                )
            )
        if aggregate is None:
            if issue_ids:
                add(
                    _FindingContext(
                        code="linear_link_without_stored_pr_aggregate",
                        category="orphaned_records",
                        severity="warning",
                        source="linear",
                        summary="A Linear issue-to-PR link has no stored PR aggregate.",
                        recommendation="Refresh the aggregate from trusted GitHub readback in a later ingestion phase; reconciliation must not create it.",
                        entity_ids=tuple(issue_ids),
                        linear_issue_id=primary_issue,
                        repository=ref.repository,
                        pr_number=ref.pr_number,
                        observed_head_sha=snapshot.head_sha,
                    )
                )
            continue
        if aggregate.current_head_sha != snapshot.head_sha:
            add(
                _FindingContext(
                    code="stored_pr_head_is_stale",
                    category="stale_heads",
                    severity="critical",
                    source="linear",
                    summary="The stored PR aggregate head differs from authoritative GitHub current head.",
                    recommendation="Refresh the trusted aggregate from GitHub and invalidate stale evidence; do not silently rewrite it here.",
                    entity_ids=(aggregate.snapshot_sha256, snapshot.observation_id),
                    linear_issue_id=primary_issue,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=aggregate.current_head_sha,
                    observed_head_sha=snapshot.head_sha,
                )
            )
            continue
        conflicting_fields = []
        if aggregate.pr_url.casefold().rstrip("/") != snapshot.pr_url.casefold().rstrip(
            "/"
        ):
            conflicting_fields.append("pr_url")
        if aggregate.base_branch != snapshot.base_ref:
            conflicting_fields.append("base_branch")
        if aggregate.head_branch != snapshot.head_ref:
            conflicting_fields.append("head_branch")
        if aggregate.is_draft != snapshot.is_draft:
            conflicting_fields.append("is_draft")
        if aggregate.state != snapshot.state:
            conflicting_fields.append("state")
        if conflicting_fields:
            add(
                _FindingContext(
                    code="linear_github_same_head_identity_conflict",
                    category="conflicting_source_revisions",
                    severity="error",
                    source="cross_system",
                    summary="Linear-side aggregate and GitHub disagree on PR identity/state at the same full head SHA.",
                    recommendation="Refresh both read-only snapshots and resolve the named fields explicitly; do not guess from prose.",
                    entity_ids=tuple(conflicting_fields),
                    linear_issue_id=primary_issue,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=aggregate.current_head_sha,
                    observed_head_sha=snapshot.head_sha,
                )
            )

    head_groups = _group_by(inputs.coderabbit_heads, lambda item: item.ref)
    head_by_ref: dict[PullRequestIdentity, CodeRabbitHeadPointer] = {}
    for ref, group in sorted(head_groups.items()):
        head_by_ref[ref] = group[-1]
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_coderabbit_head_pointer",
                    category="duplicate_semantic_rows",
                    severity="error",
                    source="coderabbit",
                    summary="Multiple CodeRabbit current-head pointers exist for one PR.",
                    recommendation="Repair the duplicate pointers after refreshing GitHub truth; do not pick by row order.",
                    entity_ids=tuple(item.current_head_sha for item in group),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                )
            )
        if ref not in linked_issue_ids_by_ref:
            add(
                _FindingContext(
                    code="coderabbit_head_without_linear_link",
                    category="orphaned_records",
                    severity="warning",
                    source="coderabbit",
                    summary="A CodeRabbit current-head pointer has no Linear issue-to-PR association.",
                    recommendation="Confirm the PR's coordinator before retaining or migrating this evidence.",
                    entity_ids=tuple(item.current_head_sha for item in group),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                )
            )
        snapshot = current_by_ref.get(ref)
        if snapshot is not None and group[-1].current_head_sha != snapshot.head_sha:
            add(
                _FindingContext(
                    code="coderabbit_head_is_stale",
                    category="stale_heads",
                    severity="critical",
                    source="coderabbit",
                    summary="CodeRabbit's current evidence pointer is for a superseded PR head.",
                    recommendation="Request fresh read-only CodeRabbit evidence for the GitHub head; never reuse the stale assessment.",
                    entity_ids=(snapshot.observation_id,),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=group[-1].current_head_sha,
                    observed_head_sha=snapshot.head_sha,
                )
            )

    assessment_groups = _group_by(
        inputs.coderabbit_assessments,
        lambda item: (_ref_for_assessment(item), item.head_sha),
    )
    assessment_by_ref_head: dict[
        tuple[PullRequestIdentity, str], coderabbit.CodeRabbitAssessment
    ] = {}
    for (ref, head_sha), group in sorted(
        assessment_groups.items(),
        key=lambda item: (item[0][0], item[0][1]),
    ):
        assessment_by_ref_head[(ref, head_sha)] = group[-1]
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_coderabbit_assessment",
                    category="duplicate_semantic_rows",
                    severity="error",
                    source="coderabbit",
                    summary="Multiple CodeRabbit assessments claim the same PR and exact head.",
                    recommendation="Preserve the audit rows and resolve the semantic duplicate in a later migration; do not combine counts automatically.",
                    entity_ids=tuple(item.snapshot_sha256 for item in group),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=head_sha,
                )
            )
        if ref not in linked_issue_ids_by_ref:
            add(
                _FindingContext(
                    code="coderabbit_assessment_without_linear_link",
                    category="orphaned_records",
                    severity="warning",
                    source="coderabbit",
                    summary="CodeRabbit evidence is not associated with a Linear-coordinated PR.",
                    recommendation="Confirm the PR linkage before retaining or migrating the evidence.",
                    entity_ids=tuple(item.snapshot_sha256 for item in group),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=head_sha,
                )
            )

    for ref, issue_ids in sorted(linked_issue_ids_by_ref.items()):
        snapshot = current_by_ref.get(ref)
        if snapshot is None or snapshot.state in {"closed", "merged"}:
            continue
        assessment = assessment_by_ref_head.get((ref, snapshot.head_sha))
        primary_issue = sorted(issue_ids)[0] if len(issue_ids) == 1 else None
        pointer = head_by_ref.get(ref)
        if (
            assessment is None
            or pointer is None
            or pointer.current_head_sha != snapshot.head_sha
        ):
            add(
                _FindingContext(
                    code="current_head_missing_coderabbit_evidence",
                    category="missing_qa_evidence",
                    severity="error",
                    source="coderabbit",
                    summary="The authoritative GitHub head has no complete current CodeRabbit assessment.",
                    recommendation="Collect fresh read-only CodeRabbit evidence for this exact head before any gate advancement.",
                    entity_ids=tuple(sorted(issue_ids)),
                    linear_issue_id=primary_issue,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    observed_head_sha=snapshot.head_sha,
                )
            )
            continue
        if (
            assessment.state == "actionable"
            or assessment.actionable_count
            or assessment.unresolved_count
        ):
            add(
                _FindingContext(
                    code="current_head_has_actionable_coderabbit_findings",
                    category="actionable_coderabbit_findings",
                    severity="critical",
                    source="coderabbit",
                    summary="Current-head CodeRabbit evidence contains actionable or unresolved findings.",
                    recommendation="Return the exact head to the bounded correction/QA path; do not advance or notify the human gate.",
                    entity_ids=(
                        assessment.correction.correction_work_key,
                        *assessment.actionable_finding_ids,
                    ),
                    linear_issue_id=primary_issue,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=assessment.head_sha,
                    observed_head_sha=snapshot.head_sha,
                )
            )
        elif assessment.state not in {"clean", "no_actionable_comments"}:
            add(
                _FindingContext(
                    code=f"current_head_coderabbit_{assessment.state}",
                    category="missing_qa_evidence",
                    severity="error",
                    source="coderabbit",
                    summary=f"Current-head CodeRabbit evidence is {assessment.state}, not a complete clean disposition.",
                    recommendation="Obtain a complete exact-head assessment or an explicit QA disposition before advancement.",
                    entity_ids=(assessment.snapshot_sha256,),
                    linear_issue_id=primary_issue,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=assessment.head_sha,
                    observed_head_sha=snapshot.head_sha,
                )
            )

    gate_groups = _group_by(
        inputs.human_gates,
        lambda item: (
            item.repo.casefold(),
            item.pr_number,
            item.gate_kind,
            item.approved_head_sha,
        ),
    )
    gate_by_id: dict[str, human_review.HumanReviewGate] = {
        gate.id: gate for gate in inputs.human_gates
    }
    task_by_id = {task.task_id: task for task in inputs.task_states}
    current_active_gates: list[human_review.HumanReviewGate] = []
    for identity, group in sorted(gate_groups.items()):
        if len(group) > 1:
            ref = _identity(identity[0], identity[1])
            add(
                _FindingContext(
                    code="duplicate_exact_head_human_gate",
                    category="duplicate_semantic_rows",
                    severity="critical",
                    source="kanban",
                    summary="Multiple human gates claim the same PR, gate kind, and exact approved head.",
                    recommendation="Stop delivery and reconcile gate lineage manually; never choose a gate by creation order.",
                    entity_ids=tuple(gate.id for gate in group),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=identity[3],
                )
            )

    active_gate_groups = _group_by(
        (gate for gate in inputs.human_gates if gate.state in ACTIVE_GATE_STATES),
        lambda item: (item.repo.casefold(), item.pr_number, item.gate_kind),
    )
    for (repository, pr_number, _gate_kind), group in sorted(
        active_gate_groups.items()
    ):
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_active_human_gate",
                    category="duplicate_semantic_rows",
                    severity="critical",
                    source="kanban",
                    summary="More than one active human gate exists for one PR and gate kind.",
                    recommendation="Stop all delivery and repair the active-gate invariant manually.",
                    entity_ids=tuple(gate.id for gate in group),
                    repository=repository,
                    pr_number=pr_number,
                )
            )

    for gate in inputs.human_gates:
        ref = _ref_for_gate(gate)
        issue_id = gate.linear_issue_id
        linked = issue_id is not None and issue_id in linked_issue_ids_by_ref.get(
            ref, set()
        )
        if not linked:
            add(
                _FindingContext(
                    code="human_gate_without_linear_pr_link",
                    category="orphaned_records",
                    severity="error",
                    source="kanban",
                    summary="A human-review gate is not tied to the matching Linear coordinator and PR link.",
                    recommendation="Verify gate lineage and repair it explicitly in a later migration; do not infer the issue from title or URL.",
                    entity_ids=(gate.id, gate.task_id),
                    linear_issue_id=issue_id,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=gate.approved_head_sha,
                )
            )
        for role, task_id in (
            ("human", gate.task_id),
            ("implementation", gate.implementation_task_id),
            ("qa", gate.qa_task_id),
        ):
            if task_id not in task_by_id:
                add(
                    _FindingContext(
                        code=f"human_gate_missing_{role}_task",
                        category="orphaned_records",
                        severity="error",
                        source="kanban",
                        summary=f"The human gate references a missing {role} task row.",
                        recommendation="Restore or explicitly retire the missing lineage row before migration or delivery.",
                        entity_ids=(gate.id, task_id),
                        linear_issue_id=issue_id,
                        repository=ref.repository,
                        pr_number=ref.pr_number,
                        expected_head_sha=gate.approved_head_sha,
                    )
                )
        human_task = task_by_id.get(gate.task_id)
        if (
            gate.state in ACTIVE_GATE_STATES
            and human_task is not None
            and human_task.status != "awaiting_human"
        ):
            add(
                _FindingContext(
                    code="active_gate_task_state_conflict",
                    category="conflicting_source_revisions",
                    severity="error",
                    source="kanban",
                    summary="An active human gate does not have an awaiting_human task.",
                    recommendation="Inspect gate/task events and repair the state transition manually; do not auto-promote either row.",
                    entity_ids=(gate.id, gate.task_id, human_task.status),
                    linear_issue_id=issue_id,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=gate.approved_head_sha,
                )
            )
        if gate.state not in ACTIVE_GATE_STATES:
            continue
        snapshot = current_by_ref.get(ref)
        if snapshot is None:
            continue
        if snapshot.state in {"closed", "merged"}:
            add(
                _FindingContext(
                    code=f"active_gate_on_{snapshot.state}_pr",
                    category="terminal_prs",
                    severity="critical",
                    source="cross_system",
                    summary=f"An active Kanban human gate targets a GitHub PR that is {snapshot.state}.",
                    recommendation="Archive or close the gate after human inspection; do not send any remaining notification.",
                    entity_ids=(gate.id, gate.task_id, snapshot.observation_id),
                    linear_issue_id=issue_id,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=gate.approved_head_sha,
                    observed_head_sha=snapshot.head_sha,
                )
            )
            continue
        if gate.approved_head_sha != snapshot.head_sha:
            add(
                _FindingContext(
                    code="active_gate_head_is_stale",
                    category="stale_heads",
                    severity="critical",
                    source="cross_system",
                    summary="The active human gate approval is for a superseded PR head.",
                    recommendation="Require fresh exact-head QA and a new gate; never reuse or silently retarget this approval.",
                    entity_ids=(gate.id, gate.task_id, snapshot.observation_id),
                    linear_issue_id=issue_id,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=gate.approved_head_sha,
                    observed_head_sha=snapshot.head_sha,
                )
            )
            continue
        current_active_gates.append(gate)
        add(
            _FindingContext(
                code="active_exact_head_human_gate",
                category="current_gates",
                severity="info",
                source="kanban",
                summary="An active human gate is bound to the authoritative open GitHub head.",
                recommendation="Keep review and any merge human-only in GitHub; re-read the head before every later action.",
                entity_ids=(gate.id, gate.task_id, gate.qa_task_id),
                linear_issue_id=issue_id,
                repository=ref.repository,
                pr_number=ref.pr_number,
                expected_head_sha=gate.approved_head_sha,
                observed_head_sha=snapshot.head_sha,
            )
        )

    current_gate_heads = {
        (_ref_for_gate(gate), gate.approved_head_sha) for gate in current_active_gates
    }
    for ref, issue_ids in sorted(linked_issue_ids_by_ref.items()):
        snapshot = current_by_ref.get(ref)
        if snapshot is None or snapshot.state in {"closed", "merged"}:
            continue
        if (ref, snapshot.head_sha) in current_gate_heads:
            continue
        primary_issue = sorted(issue_ids)[0] if len(issue_ids) == 1 else None
        add(
            _FindingContext(
                code="current_head_missing_human_gate",
                category="missing_qa_evidence",
                severity="error",
                source="kanban",
                summary="The authoritative open GitHub head has no active exact-head human-review gate.",
                recommendation="Complete exact-head QA and create a new immutable gate through the approved workflow; do not promote a stale gate.",
                entity_ids=tuple(sorted(issue_ids)),
                linear_issue_id=primary_issue,
                repository=ref.repository,
                pr_number=ref.pr_number,
                observed_head_sha=snapshot.head_sha,
            )
        )

    delivery_groups = _group_by(
        inputs.gate_deliveries,
        lambda item: (item.gate_id, item.channel),
    )
    delivery_by_gate_channel: dict[
        tuple[str, str], human_review.ReviewGateDelivery
    ] = {}
    for (gate_id, channel), group in sorted(delivery_groups.items()):
        delivery_by_gate_channel[(gate_id, channel)] = group[-1]
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_gate_delivery",
                    category="duplicate_semantic_rows",
                    severity="error",
                    source="kanban",
                    summary="A human gate has duplicate semantic delivery rows for one channel.",
                    recommendation="Preserve provider receipts and reconcile the duplicate rows manually before delivery wiring.",
                    entity_ids=(
                        gate_id,
                        channel,
                        *(item.dedupe_marker for item in group),
                    ),
                )
            )
        gate = gate_by_id.get(gate_id)
        if gate is None:
            add(
                _FindingContext(
                    code="gate_delivery_without_gate",
                    category="orphaned_records",
                    severity="error",
                    source="kanban",
                    summary="A review delivery row references a missing human gate.",
                    recommendation="Quarantine the orphaned delivery row for later migration; do not send it.",
                    entity_ids=(gate_id, channel),
                )
            )
            continue
        expected_destination = {
            "github_comment": gate.pr_url,
            "github_review_request": gate.reviewer_principal,
            "slack": gate.notification_principal,
        }.get(channel)
        if (
            expected_destination is not None
            and group[-1].destination != expected_destination
        ):
            ref = _ref_for_gate(gate)
            add(
                _FindingContext(
                    code="gate_delivery_destination_conflict",
                    category="conflicting_source_revisions",
                    severity="critical",
                    source="cross_system",
                    summary="A stored delivery destination conflicts with the gate's immutable route identity.",
                    recommendation="Stop delivery and repair the route explicitly; never infer a replacement destination from ambient state.",
                    entity_ids=(
                        gate_id,
                        channel,
                        group[-1].destination,
                        str(expected_destination),
                    ),
                    linear_issue_id=gate.linear_issue_id,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=gate.approved_head_sha,
                )
            )

    github_semantic_groups = _group_by(
        inputs.github_intents,
        lambda item: (
            item.repository,
            item.pr_number,
            item.head_sha,
            item.surface,
            item.operation,
        ),
    )
    for semantic, group in sorted(github_semantic_groups.items()):
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_github_outbox_intent",
                    category="duplicate_semantic_rows",
                    severity="error",
                    source="github",
                    summary="GitHub outbox contains duplicate semantic intents for one exact head and operation.",
                    recommendation="Resolve the duplicate by idempotency evidence in a later migration; do not replay either intent now.",
                    entity_ids=tuple(item.id for item in group),
                    repository=semantic[0],
                    pr_number=semantic[1],
                    expected_head_sha=semantic[2],
                )
            )

    slack_semantic_groups = _group_by(
        inputs.slack_intents,
        lambda item: (
            item.channel_id,
            item.thread_ts,
            item.repository,
            item.pr_number,
            item.head_sha,
            item.surface,
            item.operation,
        ),
    )
    for semantic, group in sorted(slack_semantic_groups.items()):
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_slack_outbox_intent",
                    category="duplicate_semantic_rows",
                    severity="error",
                    source="slack",
                    summary="Slack outbox contains duplicate semantic intents for one exact route and head.",
                    recommendation="Resolve the duplicate using stored idempotency receipts; do not post either row automatically.",
                    entity_ids=tuple(item.id for item in group),
                    repository=semantic[2],
                    pr_number=semantic[3],
                    expected_head_sha=semantic[4],
                )
            )

    github_by_gate: dict[str, list[github.GitHubOutboxIntent]] = {}
    for intent in inputs.github_intents:
        github_by_gate.setdefault(intent.gate_id, []).append(intent)
        gate = gate_by_id.get(intent.gate_id)
        ref = _ref_for_github_intent(intent)
        if gate is None:
            add(
                _FindingContext(
                    code="github_outbox_without_gate",
                    category="orphaned_records",
                    severity="error",
                    source="github",
                    summary="A GitHub outbox intent references a missing human gate.",
                    recommendation="Quarantine the intent for later migration; do not send or replay it.",
                    entity_ids=(intent.id, intent.gate_id),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=intent.head_sha,
                )
            )
        else:
            gate_ref = _ref_for_gate(gate)
            if gate_ref != ref or gate.approved_head_sha != intent.head_sha:
                add(
                    _FindingContext(
                        code="github_outbox_gate_identity_conflict",
                        category="conflicting_source_revisions",
                        severity="critical",
                        source="cross_system",
                        summary="GitHub outbox identity conflicts with its referenced human gate.",
                        recommendation="Stop the intent and repair the immutable identity manually; never retarget the row.",
                        entity_ids=(intent.id, gate.id),
                        linear_issue_id=gate.linear_issue_id,
                        repository=ref.repository,
                        pr_number=ref.pr_number,
                        expected_head_sha=gate.approved_head_sha,
                        observed_head_sha=intent.head_sha,
                    )
                )
        snapshot = current_by_ref.get(ref)
        if (
            snapshot is not None
            and intent.state in ACTIVE_OUTBOX_STATES
            and intent.head_sha != snapshot.head_sha
        ):
            add(
                _FindingContext(
                    code="github_outbox_head_is_stale",
                    category="stale_heads",
                    severity="critical",
                    source="github",
                    summary="A GitHub outbox intent is bound to a superseded PR head.",
                    recommendation="Keep the intent suppressed and require a new exact-head gate before any later send.",
                    entity_ids=(intent.id, intent.gate_id),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=intent.head_sha,
                    observed_head_sha=snapshot.head_sha,
                )
            )

    slack_by_gate: dict[str, list[slack.SlackOutboxIntent]] = {}
    slack_by_id = {intent.id: intent for intent in inputs.slack_intents}
    for intent in inputs.slack_intents:
        slack_by_gate.setdefault(intent.gate_id, []).append(intent)
        gate = gate_by_id.get(intent.gate_id)
        ref = _ref_for_slack_intent(intent)
        if gate is None:
            add(
                _FindingContext(
                    code="slack_outbox_without_gate",
                    category="orphaned_records",
                    severity="error",
                    source="slack",
                    summary="A Slack outbox intent references a missing human gate.",
                    recommendation="Quarantine the intent for later migration; do not post or replay it.",
                    entity_ids=(intent.id, intent.gate_id),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=intent.head_sha,
                )
            )
        else:
            gate_ref = _ref_for_gate(gate)
            if gate_ref != ref or gate.approved_head_sha != intent.head_sha:
                add(
                    _FindingContext(
                        code="slack_outbox_gate_identity_conflict",
                        category="conflicting_source_revisions",
                        severity="critical",
                        source="cross_system",
                        summary="Slack outbox identity conflicts with its referenced human gate.",
                        recommendation="Stop the intent and repair the immutable route/head explicitly; never retarget it.",
                        entity_ids=(intent.id, gate.id),
                        linear_issue_id=gate.linear_issue_id,
                        repository=ref.repository,
                        pr_number=ref.pr_number,
                        expected_head_sha=gate.approved_head_sha,
                        observed_head_sha=intent.head_sha,
                    )
                )
        if (
            intent.source_intent_id is not None
            and intent.source_intent_id not in slack_by_id
        ):
            add(
                _FindingContext(
                    code="slack_reply_without_source_intent",
                    category="orphaned_records",
                    severity="error",
                    source="slack",
                    summary="A Slack thread reply references a missing source notification intent.",
                    recommendation="Do not post the reply; restore the source route or retire the row in a later migration.",
                    entity_ids=(intent.id, intent.source_intent_id),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=intent.head_sha,
                )
            )
        snapshot = current_by_ref.get(ref)
        if (
            snapshot is not None
            and intent.state in ACTIVE_OUTBOX_STATES
            and intent.head_sha != snapshot.head_sha
        ):
            add(
                _FindingContext(
                    code="slack_outbox_head_is_stale",
                    category="stale_heads",
                    severity="critical",
                    source="slack",
                    summary="A Slack outbox intent is bound to a superseded PR head.",
                    recommendation="Keep the row suppressed and require a new exact-head gate before any later notification.",
                    entity_ids=(intent.id, intent.gate_id),
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=intent.head_sha,
                    observed_head_sha=snapshot.head_sha,
                )
            )

    acknowledgement_groups = _group_by(
        inputs.slack_acknowledgements,
        lambda item: (item.source_intent_id, item.payload_sha256),
    )
    for semantic, group in sorted(acknowledgement_groups.items()):
        if len(group) > 1:
            add(
                _FindingContext(
                    code="duplicate_slack_acknowledgement",
                    category="duplicate_semantic_rows",
                    severity="warning",
                    source="slack",
                    summary="Slack acknowledgement evidence is duplicated for one source intent and semantic payload.",
                    recommendation="Preserve one receipt by provider identity during a later migration; acknowledgements must never approve the gate.",
                    entity_ids=tuple(item.id for item in group),
                )
            )
        if semantic[0] not in slack_by_id:
            add(
                _FindingContext(
                    code="slack_acknowledgement_without_intent",
                    category="orphaned_records",
                    severity="error",
                    source="slack",
                    summary="Slack acknowledgement evidence references a missing notification intent.",
                    recommendation="Quarantine the acknowledgement for audit; never treat it as approval authority.",
                    entity_ids=(semantic[0], *(item.id for item in group)),
                )
            )

    github_operation_for_channel = {
        "github_comment": "create_comment",
        "github_review_request": "request_reviewer",
    }
    for gate in current_active_gates:
        ref = _ref_for_gate(gate)
        for channel in human_review.DEFAULT_DELIVERY_CHANNELS:
            if (gate.id, channel) not in delivery_by_gate_channel:
                add(
                    _FindingContext(
                        code=f"missing_gate_delivery_{channel}",
                        category="missing_outbox_rows",
                        severity="error",
                        source="kanban",
                        summary=f"The current exact-head gate is missing its {channel} delivery row.",
                        recommendation="Create the row only in the approved migration/delivery phase after re-reading the current head.",
                        entity_ids=(gate.id, channel),
                        linear_issue_id=gate.linear_issue_id,
                        repository=ref.repository,
                        pr_number=ref.pr_number,
                        expected_head_sha=gate.approved_head_sha,
                        observed_head_sha=gate.approved_head_sha,
                    )
                )
        for channel, operation in github_operation_for_channel.items():
            matches = [
                intent
                for intent in github_by_gate.get(gate.id, [])
                if intent.repository == ref.repository
                and intent.pr_number == ref.pr_number
                and intent.head_sha == gate.approved_head_sha
                and intent.operation == operation
            ]
            if not matches:
                add(
                    _FindingContext(
                        code=f"missing_github_outbox_{operation}",
                        category="missing_outbox_rows",
                        severity="error",
                        source="github",
                        summary=f"The current exact-head gate has no GitHub {operation} outbox intent.",
                        recommendation="Queue it only in the later delivery phase after fresh exact-head validation; do not send from reconciliation.",
                        entity_ids=(gate.id, channel, operation),
                        linear_issue_id=gate.linear_issue_id,
                        repository=ref.repository,
                        pr_number=ref.pr_number,
                        expected_head_sha=gate.approved_head_sha,
                        observed_head_sha=gate.approved_head_sha,
                    )
                )
        slack_matches = [
            intent
            for intent in slack_by_gate.get(gate.id, [])
            if intent.repository == ref.repository
            and intent.pr_number == ref.pr_number
            and intent.head_sha == gate.approved_head_sha
            and intent.operation == "notify_human_review"
        ]
        if not slack_matches:
            add(
                _FindingContext(
                    code="missing_slack_outbox_notification",
                    category="missing_outbox_rows",
                    severity="error",
                    source="slack",
                    summary="The current exact-head gate has no Slack notification outbox intent.",
                    recommendation="Queue notification only in the approved delivery phase after fresh GitHub validation; do not post from reconciliation.",
                    entity_ids=(gate.id, "slack", "notify_human_review"),
                    linear_issue_id=gate.linear_issue_id,
                    repository=ref.repository,
                    pr_number=ref.pr_number,
                    expected_head_sha=gate.approved_head_sha,
                    observed_head_sha=gate.approved_head_sha,
                )
            )

    ordered = tuple(
        sorted(
            findings.values(),
            key=lambda item: (
                SEVERITY_RANK[item.severity],
                CATEGORY_RANK[item.category],
                item.repository or "",
                item.pr_number or 0,
                item.key,
            ),
        )
    )
    if any(item.severity == "critical" for item in ordered):
        status: ReportStatus = "blocked"
    elif any(item.severity in {"error", "warning"} for item in ordered):
        status = "needs_attention"
    else:
        status = "healthy"
    return ReconciliationReport(
        input_sha256=inputs.input_sha256(),
        status=status,
        issue_count=len(coordinator_by_id),
        pull_request_count=len(current_by_ref),
        findings=ordered,
    )


def record_reconciliation_run(
    conn: sqlite3.Connection,
    report: ReconciliationReport,
    *,
    now: Optional[int] = None,
) -> ReconciliationRunReceipt:
    """Persist one immutable audit run without changing any source-system row."""
    if not isinstance(report, ReconciliationReport):
        raise ReconciliationBoundaryError(
            "report must be a ReconciliationReport instance"
        )
    created_at = int(time.time()) if now is None else int(now)
    report_json = report.to_json()
    report_sha256 = _sha256_text(report_json)
    report_markdown = report.to_markdown()
    markdown_sha256 = _sha256_text(report_markdown)
    run_key = f"{POLICY_VERSION}:{report.input_sha256}"
    run_id = "rcn_" + _sha256_text(run_key)[:24]

    with kb.write_txn(conn):
        existing = conn.execute(
            "SELECT * FROM reconciliation_runs "
            "WHERE policy_version=? AND input_sha256=?",
            (POLICY_VERSION, report.input_sha256),
        ).fetchone()
        if existing is not None:
            if (
                existing["id"] != run_id
                or existing["report_sha256"] != report_sha256
                or existing["markdown_sha256"] != markdown_sha256
                or existing["status"] != report.status
                or int(existing["finding_count"]) != len(report.findings)
            ):
                raise ReconciliationBoundaryError(
                    "idempotent reconciliation input was reused for a different report"
                )
            return ReconciliationRunReceipt(
                run_id=existing["id"],
                created=False,
                input_sha256=existing["input_sha256"],
                report_sha256=existing["report_sha256"],
                markdown_sha256=existing["markdown_sha256"],
                status=existing["status"],
                finding_count=int(existing["finding_count"]),
            )

        conn.execute(
            """
            INSERT INTO reconciliation_runs (
                id, schema_version, policy_version, input_sha256, status,
                finding_count, report_json, report_sha256, report_markdown,
                markdown_sha256, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                SCHEMA_VERSION,
                POLICY_VERSION,
                report.input_sha256,
                report.status,
                len(report.findings),
                report_json,
                report_sha256,
                report_markdown,
                markdown_sha256,
                created_at,
            ),
        )
        for finding in report.findings:
            finding_json = _canonical_json(finding.to_dict())
            conn.execute(
                """
                INSERT INTO reconciliation_findings (
                    run_id, finding_key, category, severity, source,
                    linear_issue_id, repository, pr_number, expected_head_sha,
                    observed_head_sha, finding_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    finding.key,
                    finding.category,
                    finding.severity,
                    finding.source,
                    finding.linear_issue_id,
                    finding.repository,
                    finding.pr_number,
                    finding.expected_head_sha,
                    finding.observed_head_sha,
                    finding_json,
                ),
            )
    return ReconciliationRunReceipt(
        run_id=run_id,
        created=True,
        input_sha256=report.input_sha256,
        report_sha256=report_sha256,
        markdown_sha256=markdown_sha256,
        status=report.status,
        finding_count=len(report.findings),
    )


def reconcile(
    conn: sqlite3.Connection,
    *,
    snapshot_provider: ReconciliationSnapshotProvider,
    linear_issue_ids: Optional[Sequence[str]] = None,
    persist: bool = False,
    now: Optional[int] = None,
    max_pull_requests: int = DEFAULT_MAX_PULL_REQUESTS,
    max_source_records: int = DEFAULT_MAX_SOURCE_RECORDS,
) -> ReconciliationExecution:
    """Run one bounded audit; source/workflow state remains read-only.

    ``persist=False`` is the default and performs no database write at all.
    ``persist=True`` records only immutable reconciliation reports/findings.
    Neither mode exposes an action that can notify, migrate, approve, merge,
    mutate a provider, or advance a Kanban gate.
    """
    inputs = collect_reconciliation_inputs(
        conn,
        snapshot_provider=snapshot_provider,
        linear_issue_ids=linear_issue_ids,
        now=now,
        max_pull_requests=max_pull_requests,
        max_source_records=max_source_records,
    )
    report = build_reconciliation_report(inputs)
    receipt = record_reconciliation_run(conn, report, now=now) if persist else None
    return ReconciliationExecution(inputs, report, receipt)
