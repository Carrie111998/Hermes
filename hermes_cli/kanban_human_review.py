"""Typed, exact-head human-review gates for Kanban workflows.

This module owns the workflow-specific transaction that turns a trusted QA run
into one non-dispatchable human task plus an auditable gate and per-destination
outbox. It deliberately contains no network clients, credentials, merge calls,
or branch-write capability; delivery lives behind injected adapters in the
gateway layer.
"""
from __future__ import annotations

import hashlib
import json
import re
import secrets
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Optional

from hermes_cli import kanban_db as kb


SCHEMA_VERSION = 1
EXPECTED_QA_PROFILE = "echlon-qa"
EXPECTED_IMPLEMENTATION_PROFILE = "echlon-coder"
NORMAL_APPROVAL_VERDICT = "APPROVE_FOR_SRDJA_REVIEW"
HUMAN_DECISION_VERDICTS = {
    "ESCALATE_TO_SRDJA_AFTER_DOUBLE_FAILURE",
    "NEEDS_HUMAN_DECISION",
}
VALID_GATE_KINDS = {"srdja_pr_review", "human_decision"}
ACTIVE_GATE_STATES = {
    "pending_delivery",
    "awaiting_human",
    "seen",
    "delivery_failed",
}
TERMINAL_GATE_STATES = {
    "human_approved",
    "changes_requested",
    "superseded",
    "merged",
    "closed",
}
VALID_DELIVERY_CHANNELS = {
    "github_review_request",
    "github_comment",
    "slack",
}
MAX_PR_SNAPSHOT_AGE_SECONDS = 300
MAX_PR_SNAPSHOT_FUTURE_SKEW_SECONDS = 60
DEFAULT_DELIVERY_CHANNELS = (
    "github_comment",
    "github_review_request",
    "slack",
)
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "apikey",
    "password",
    "private_key",
    "secret",
    "token",
)
_PR_SNAPSHOT_PROVIDER: Optional[
    Callable[[Mapping[str, Any]], Mapping[str, Any]]
] = None


def register_pr_snapshot_provider(
    provider: Optional[Callable[[Mapping[str, Any]], Mapping[str, Any]]],
) -> None:
    """Register a process-local, read-only PR snapshot adapter.

    The default is deliberately ``None``. Agent/model input is never accepted
    as a trusted GitHub snapshot; a gateway integration must explicitly install
    a read-only adapter before the model-facing advance tool can be exposed.
    """
    global _PR_SNAPSHOT_PROVIDER
    _PR_SNAPSHOT_PROVIDER = provider


def pr_snapshot_provider_available() -> bool:
    return _PR_SNAPSHOT_PROVIDER is not None


def read_trusted_pr_snapshot(packet: Mapping[str, Any]) -> Mapping[str, Any]:
    provider = _PR_SNAPSHOT_PROVIDER
    if provider is None:
        raise RuntimeError("trusted read-only PR snapshot adapter is not configured")
    snapshot = provider(packet)
    if not isinstance(snapshot, Mapping):
        raise RuntimeError("trusted PR snapshot adapter returned a non-object")
    return snapshot


@dataclass(frozen=True)
class HumanReviewGate:
    id: str
    task_id: str
    schema_version: int
    gate_kind: str
    reviewer_principal: str
    notification_principal: Optional[str]
    repo: str
    pr_number: int
    pr_url: str
    linear_issue_id: Optional[str]
    base_branch: str
    head_branch: str
    approved_head_sha: str
    implementation_task_id: str
    qa_task_id: str
    qa_run_id: int
    qa_worker_session_id: Optional[str]
    qa_verdict: str
    qa_attempt_count: int
    coder_correction_attempt_count: int
    qa_approved_at: int
    approval_packet: dict[str, Any]
    approval_packet_sha256: str
    state: str
    superseded_by_gate_id: Optional[str]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "HumanReviewGate":
        try:
            packet = json.loads(row["approval_packet_json"])
        except (TypeError, json.JSONDecodeError):
            packet = {}
        return cls(
            id=row["id"],
            task_id=row["task_id"],
            schema_version=int(row["schema_version"]),
            gate_kind=row["gate_kind"],
            reviewer_principal=row["reviewer_principal"],
            notification_principal=row["notification_principal"],
            repo=row["repo"],
            pr_number=int(row["pr_number"]),
            pr_url=row["pr_url"],
            linear_issue_id=row["linear_issue_id"],
            base_branch=row["base_branch"],
            head_branch=row["head_branch"],
            approved_head_sha=row["approved_head_sha"],
            implementation_task_id=row["implementation_task_id"],
            qa_task_id=row["qa_task_id"],
            qa_run_id=int(row["qa_run_id"]),
            qa_worker_session_id=row["qa_worker_session_id"],
            qa_verdict=row["qa_verdict"],
            qa_attempt_count=int(row["qa_attempt_count"]),
            coder_correction_attempt_count=int(row["coder_correction_attempt_count"]),
            qa_approved_at=int(row["qa_approved_at"]),
            approval_packet=packet if isinstance(packet, dict) else {},
            approval_packet_sha256=row["approval_packet_sha256"],
            state=row["state"],
            superseded_by_gate_id=row["superseded_by_gate_id"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass(frozen=True)
class ReviewGateDelivery:
    gate_id: str
    channel: str
    destination: str
    state: str
    attempt_count: int
    next_attempt_at: Optional[int]
    external_id: Optional[str]
    dedupe_marker: str
    last_error: Optional[str]
    created_at: int
    updated_at: int

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "ReviewGateDelivery":
        return cls(
            gate_id=row["gate_id"],
            channel=row["channel"],
            destination=row["destination"],
            state=row["state"],
            attempt_count=int(row["attempt_count"]),
            next_attempt_at=(
                int(row["next_attempt_at"])
                if row["next_attempt_at"] is not None
                else None
            ),
            external_id=row["external_id"],
            dedupe_marker=row["dedupe_marker"],
            last_error=row["last_error"],
            created_at=int(row["created_at"]),
            updated_at=int(row["updated_at"]),
        )


@dataclass(frozen=True)
class AdvanceResult:
    gate_id: str
    task_id: str
    approval_packet_sha256: str
    created: bool


def get_human_review_gate(
    conn: sqlite3.Connection,
    gate_id: str,
) -> Optional[HumanReviewGate]:
    row = conn.execute(
        "SELECT * FROM human_review_gates WHERE id = ?", (gate_id,)
    ).fetchone()
    return HumanReviewGate.from_row(row) if row else None


def get_gate_for_task(
    conn: sqlite3.Connection,
    task_id: str,
) -> Optional[HumanReviewGate]:
    row = conn.execute(
        "SELECT * FROM human_review_gates WHERE task_id = ?", (task_id,)
    ).fetchone()
    return HumanReviewGate.from_row(row) if row else None


def list_gate_deliveries(
    conn: sqlite3.Connection,
    gate_id: str,
) -> list[ReviewGateDelivery]:
    rows = conn.execute(
        "SELECT * FROM review_gate_deliveries WHERE gate_id = ? "
        "ORDER BY channel",
        (gate_id,),
    ).fetchall()
    return [ReviewGateDelivery.from_row(row) for row in rows]


def _required_text(data: Mapping[str, Any], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"approval packet field {key!r} is required")
    return value.strip()


def _nonnegative_int(data: Mapping[str, Any], key: str) -> int:
    value = data.get(key)
    if isinstance(value, bool):
        raise ValueError(f"approval packet field {key!r} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"approval packet field {key!r} must be an integer"
        ) from exc
    if parsed < 0:
        raise ValueError(f"approval packet field {key!r} must be non-negative")
    return parsed


def _ensure_no_sensitive_keys(value: Any, *, path: str = "approval_packet") -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key)
            normalized = key.casefold().replace("-", "_")
            if any(part in normalized for part in _SENSITIVE_KEY_PARTS):
                raise ValueError(f"sensitive field is not allowed in approval packet: {path}.{key}")
            _ensure_no_sensitive_keys(nested, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, nested in enumerate(value):
            _ensure_no_sensitive_keys(nested, path=f"{path}[{index}]")


def _validate_coderabbit(packet: Mapping[str, Any]) -> None:
    raw = packet.get("coderabbit")
    if not isinstance(raw, Mapping):
        raise ValueError("approval packet must include a typed CodeRabbit disposition")
    status = _required_text(raw, "status").casefold()
    if status not in {"clean", "skipped", "actionable", "rate_limited"}:
        raise ValueError(f"unsupported CodeRabbit status: {status!r}")
    disposition = str(raw.get("disposition") or "").strip()
    actionable_count = _nonnegative_int(raw, "actionable_count")
    unresolved_count = _nonnegative_int(raw, "unresolved_count")
    if unresolved_count:
        raise ValueError(
            "CodeRabbit has unresolved actionable findings; QA approval is not allowed"
        )
    if status == "actionable" and actionable_count < 1:
        raise ValueError("CodeRabbit actionable status requires actionable_count > 0")
    if status in {"skipped", "actionable", "rate_limited"} and not disposition:
        raise ValueError(
            f"CodeRabbit {status} status requires an explicit QA disposition"
        )
    if status == "actionable" and disposition.casefold() in {
        "ignored",
        "pending",
        "unreviewed",
    }:
        raise ValueError(
            "CodeRabbit actionable findings require an explicit resolved disposition"
        )


def _canonicalize_packet(
    approval_packet: Mapping[str, Any],
    *,
    qa_task_id: str,
    qa_run_id: int,
    qa_worker_session_id: Optional[str],
    qa_profile: str,
    verified_at: int,
) -> tuple[dict[str, Any], str, str]:
    if not isinstance(approval_packet, Mapping):
        raise ValueError("approval_packet must be an object")
    if "approval_packet_sha256" in approval_packet:
        raise ValueError("approval_packet_sha256 is kernel-generated and must be omitted")
    _ensure_no_sensitive_keys(approval_packet)
    try:
        packet = json.loads(json.dumps(dict(approval_packet), ensure_ascii=False))
    except (TypeError, ValueError) as exc:
        raise ValueError("approval_packet must be JSON serializable") from exc
    encoded_probe = json.dumps(packet, ensure_ascii=False).encode("utf-8")
    if len(encoded_probe) > 128 * 1024:
        raise ValueError("approval_packet exceeds the 128 KiB audit limit")

    schema_version = _nonnegative_int(packet, "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"approval packet schema_version must be {SCHEMA_VERSION}"
        )
    gate_kind = _required_text(packet, "gate_kind")
    if gate_kind not in VALID_GATE_KINDS:
        raise ValueError(f"unsupported gate_kind: {gate_kind!r}")
    verdict = _required_text(packet, "qa_verdict")
    if gate_kind == "srdja_pr_review" and verdict != NORMAL_APPROVAL_VERDICT:
        raise ValueError(
            f"qa_verdict must be exactly {NORMAL_APPROVAL_VERDICT} for normal review"
        )
    if gate_kind == "human_decision" and verdict not in HUMAN_DECISION_VERDICTS:
        raise ValueError("qa_verdict is not valid for a human_decision gate")

    for key in (
        "board",
        "reviewer_principal",
        "human_assignee",
        "linear_issue_id",
        "linear_title",
        "linear_issue_url",
        "repo",
        "pr_url",
        "base_branch",
        "head_branch",
        "approved_head_sha",
        "implementation_task_id",
        "claimed_fix_summary",
        "external_side_effects",
        "merge_policy",
    ):
        _required_text(packet, key)
    if not str(packet["reviewer_principal"]).startswith("github:"):
        raise ValueError("reviewer_principal must be a github: principal")
    if packet.get("notification_principal") is not None:
        notification = _required_text(packet, "notification_principal")
        if not notification.startswith("slack:"):
            raise ValueError("notification_principal must be a slack: principal")
    if not str(packet["pr_url"]).startswith("https://"):
        raise ValueError("pr_url must use https")
    if not str(packet["linear_issue_url"]).startswith("https://"):
        raise ValueError("linear_issue_url must use https")

    try:
        pr_number = int(packet.get("pr_number"))
    except (TypeError, ValueError) as exc:
        raise ValueError("approval packet pr_number must be a positive integer") from exc
    if pr_number < 1:
        raise ValueError("approval packet pr_number must be a positive integer")
    packet["pr_number"] = pr_number

    head_sha = str(packet["approved_head_sha"]).casefold()
    if not _FULL_SHA_RE.fullmatch(head_sha):
        raise ValueError("approved_head_sha must be a full 40-character lowercase SHA")
    packet["approved_head_sha"] = head_sha

    qa_attempt_count = _nonnegative_int(packet, "qa_attempt_count")
    correction_count = _nonnegative_int(packet, "coder_correction_attempt_count")
    if correction_count > 1:
        raise ValueError("coder_correction_attempt_count exceeds the one-correction policy")
    if gate_kind == "srdja_pr_review" and qa_attempt_count != correction_count:
        raise ValueError(
            "qa_attempt_count must match coder_correction_attempt_count for normal approval"
        )
    packet["qa_attempt_count"] = qa_attempt_count
    packet["coder_correction_attempt_count"] = correction_count

    for key in (
        "changed_files",
        "tests_or_checks_run",
        "verification_output",
        "regression_checks",
        "blockers",
        "known_risks",
        "unchecked_items",
    ):
        if not isinstance(packet.get(key), list):
            raise ValueError(f"approval packet field {key!r} must be a list")
    if packet.get("requires_srdja_review") is not True:
        raise ValueError("requires_srdja_review must be true")
    if packet.get("merge_policy") != "human_only":
        raise ValueError("merge_policy must be exactly 'human_only'")
    _validate_coderabbit(packet)

    packet.update(
        {
            "qa_task_id": qa_task_id,
            "qa_run_id": int(qa_run_id),
            "qa_worker_session_id": qa_worker_session_id,
            "qa_profile": qa_profile,
            "generated_at": int(verified_at),
            "verified_at": int(verified_at),
        }
    )
    canonical_without_digest = json.dumps(
        packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(canonical_without_digest.encode("utf-8")).hexdigest()
    stored_packet = dict(packet)
    stored_packet["approval_packet_sha256"] = digest
    stored_json = json.dumps(
        stored_packet,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return stored_packet, stored_json, digest


def _validate_pr_snapshot(
    packet: Mapping[str, Any],
    pr_snapshot: Mapping[str, Any],
) -> int:
    if not isinstance(pr_snapshot, Mapping):
        raise ValueError("pr_snapshot must be an object from a read-only PR adapter")
    source = _required_text(pr_snapshot, "source")
    if source != "github_readback":
        raise ValueError("pr_snapshot source is not trusted")
    verified_at = _nonnegative_int(pr_snapshot, "verified_at")
    if verified_at < 1:
        raise ValueError("pr_snapshot verified_at must be positive")
    current_time = int(time.time())
    if verified_at < current_time - MAX_PR_SNAPSHOT_AGE_SECONDS:
        raise ValueError("pr_snapshot is stale; refresh the live PR readback")
    if verified_at > current_time + MAX_PR_SNAPSHOT_FUTURE_SKEW_SECONDS:
        raise ValueError("pr_snapshot verified_at is implausibly far in the future")
    if str(pr_snapshot.get("state") or "").upper() != "OPEN":
        raise ValueError("PR must be OPEN before advancing to human review")
    if pr_snapshot.get("is_draft") is not False:
        raise ValueError("draft PRs cannot advance to human review")

    expected = {
        "repo": packet["repo"],
        "pr_number": int(packet["pr_number"]),
        "pr_url": packet["pr_url"],
        "base_branch": packet["base_branch"],
        "head_branch": packet["head_branch"],
        "head_sha": packet["approved_head_sha"],
    }
    for key, expected_value in expected.items():
        actual = pr_snapshot.get(key)
        if key == "pr_number":
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                pass
        if actual != expected_value:
            label = "head" if key == "head_sha" else key
            raise ValueError(
                f"live PR {label} does not match the approval packet "
                f"({actual!r} != {expected_value!r})"
            )
    return verified_at


def _validate_lineage(
    conn: sqlite3.Connection,
    *,
    qa_task_id: str,
    packet: Mapping[str, Any],
) -> None:
    implementation_task_id = str(packet["implementation_task_id"])
    linked = conn.execute(
        "SELECT 1 FROM task_links WHERE parent_id = ? AND child_id = ?",
        (implementation_task_id, qa_task_id),
    ).fetchone()
    if linked is None:
        raise ValueError(
            "approval packet implementation_task_id does not match QA lineage"
        )
    implementation = kb.get_task(conn, implementation_task_id)
    if (
        implementation is None
        or implementation.assignee != EXPECTED_IMPLEMENTATION_PROFILE
        or implementation.status != "done"
    ):
        raise ValueError("QA lineage does not contain a completed echlon-coder task")
    implementation_run = kb.latest_run(conn, implementation_task_id)
    metadata = implementation_run.metadata if implementation_run else None
    if (
        implementation_run is None
        or implementation_run.outcome != "completed"
        or not isinstance(metadata, dict)
    ):
        raise ValueError("implementation lineage has no trusted completion metadata")

    aliases = {
        "linear_issue_id": ("linear_issue_id",),
        "repo": ("repo",),
        "pr_number": ("pr_number",),
        "pr_url": ("pr_url",),
        "base_branch": ("pr_base", "base_branch"),
        "head_branch": ("branch", "head_branch"),
        "approved_head_sha": ("pr_head_sha", "head_sha"),
    }
    for packet_key, metadata_keys in aliases.items():
        expected = packet[packet_key]
        actual = next(
            (metadata[key] for key in metadata_keys if metadata.get(key) is not None),
            None,
        )
        if packet_key == "pr_number":
            try:
                actual = int(actual)
            except (TypeError, ValueError):
                pass
        if actual != expected:
            raise ValueError(
                f"implementation lineage {packet_key} does not match approval packet"
            )
    changed_files = metadata.get("changed_files")
    if not isinstance(changed_files, list) or changed_files != packet["changed_files"]:
        raise ValueError("implementation lineage changed_files do not match approval packet")


def _new_gate_id() -> str:
    return "g_" + secrets.token_hex(8)


def _canonical_dedupe_key(packet: Mapping[str, Any]) -> str:
    return (
        f"echlon-srdja-review:v1:{packet['repo']}:pr:{packet['pr_number']}:"
        f"head:{packet['approved_head_sha']}"
    )


def _delivery_marker(channel: str, gate_id: str, packet: Mapping[str, Any]) -> str:
    if channel == "github_comment":
        return f"<!-- echlon-human-review-gate:v1:{gate_id} -->"
    return _canonical_dedupe_key(packet)


def _delivery_destination(channel: str, packet: Mapping[str, Any]) -> str:
    if channel.startswith("github_"):
        return f"{packet['repo']}#{packet['pr_number']}"
    notification = str(packet.get("notification_principal") or "")
    return notification.split(":", 1)[1] if ":" in notification else notification


def _insert_gate_deliveries(
    conn: sqlite3.Connection,
    *,
    gate_id: str,
    packet: Mapping[str, Any],
    created_at: int,
    channels: Iterable[str] = DEFAULT_DELIVERY_CHANNELS,
) -> None:
    for channel in channels:
        if channel not in VALID_DELIVERY_CHANNELS:
            raise ValueError(f"unsupported human-review delivery channel: {channel!r}")
        destination = _delivery_destination(channel, packet)
        if not destination:
            raise ValueError(f"delivery channel {channel!r} has no destination")
        conn.execute(
            """
            INSERT INTO review_gate_deliveries (
                gate_id, channel, destination, state, attempt_count,
                next_attempt_at, external_id, dedupe_marker, last_error,
                created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', 0, NULL, NULL, ?, NULL, ?, ?)
            """,
            (
                gate_id,
                channel,
                destination,
                _delivery_marker(channel, gate_id, packet),
                created_at,
                created_at,
            ),
        )


def _supersede_active_gate(
    conn: sqlite3.Connection,
    *,
    replacing_gate_id: str,
    packet: Mapping[str, Any],
    now: int,
) -> None:
    placeholders = ",".join("?" for _ in ACTIVE_GATE_STATES)
    row = conn.execute(
        f"""
        SELECT * FROM human_review_gates
         WHERE repo = ? AND pr_number = ? AND gate_kind = ?
           AND state IN ({placeholders})
         LIMIT 1
        """,
        (
            packet["repo"],
            int(packet["pr_number"]),
            packet["gate_kind"],
            *sorted(ACTIVE_GATE_STATES),
        ),
    ).fetchone()
    if row is None:
        return
    old = HumanReviewGate.from_row(row)
    if old.approved_head_sha == packet["approved_head_sha"]:
        return
    updated = conn.execute(
        f"""
        UPDATE human_review_gates
           SET state = 'superseded', superseded_by_gate_id = ?, updated_at = ?
         WHERE id = ? AND state IN ({placeholders})
        """,
        (replacing_gate_id, now, old.id, *sorted(ACTIVE_GATE_STATES)),
    )
    if updated.rowcount != 1:
        return
    conn.execute(
        "UPDATE tasks SET status='archived', completed_at=? "
        "WHERE id=? AND status='awaiting_human'",
        (now, old.task_id),
    )
    conn.execute(
        "UPDATE review_gate_deliveries SET state='superseded', updated_at=? "
        "WHERE gate_id=? AND state IN ('pending', 'retry', 'failed')",
        (now, old.id),
    )
    kb._append_event(
        conn,
        old.task_id,
        "human_gate_superseded",
        {
            "gate_id": old.id,
            "replacing_gate_id": replacing_gate_id,
            "old_head_sha": old.approved_head_sha,
            "new_head_sha": packet["approved_head_sha"],
        },
    )


def _human_task_body(gate_id: str, packet: Mapping[str, Any], digest: str) -> str:
    return "\n".join(
        (
            f"Linear: {packet['linear_issue_id']} — {packet['linear_title']}",
            f"PR: {packet['pr_url']}",
            f"Approved head: {packet['approved_head_sha']}",
            f"QA verdict: {packet['qa_verdict']}",
            f"Gate: {gate_id}",
            f"Packet SHA-256: {digest}",
            "Review and merge remain human-only in GitHub. Any new push invalidates this gate.",
        )
    )


def advance_linear_pr_after_qa(
    conn: sqlite3.Connection,
    *,
    qa_task_id: str,
    expected_run_id: int,
    approval_packet: Mapping[str, Any],
    pr_snapshot: Mapping[str, Any],
    board: str,
    worker_session_id: Optional[str] = None,
) -> AdvanceResult:
    """Atomically complete trusted QA into one current-head human-review gate.

    ``pr_snapshot`` must come from a read-only GitHub adapter. The kernel checks
    it before opening the write transaction and re-checks every DB invariant
    inside ``BEGIN IMMEDIATE``. Network delivery is intentionally post-commit.
    """
    if not isinstance(board, str) or not board.strip():
        raise ValueError("board is required")
    if not qa_task_id:
        raise ValueError("qa_task_id is required")
    try:
        expected_run_id = int(expected_run_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("expected_run_id must be an integer") from exc

    # Validate the caller-supplied immutable facts before taking the write lock.
    preflight_packet = dict(approval_packet)
    if preflight_packet.get("board") != board:
        raise ValueError("approval packet board does not match the explicit board")
    verified_at = _validate_pr_snapshot(preflight_packet, pr_snapshot)
    now = int(time.time())
    created_result: Optional[AdvanceResult] = None
    completed_run_id: Optional[int] = None

    with kb.write_txn(conn):
        qa_task = kb.get_task(conn, qa_task_id)
        if qa_task is None:
            raise ValueError(f"QA task {qa_task_id!r} does not exist")
        qa_run = kb.get_run(conn, expected_run_id)
        if qa_run is None or qa_run.task_id != qa_task_id:
            raise ValueError("expected_run_id is not the QA task's current run")
        if qa_task.assignee != EXPECTED_QA_PROFILE or qa_run.profile != EXPECTED_QA_PROFILE:
            raise ValueError("human-review advancement requires a trusted echlon-qa run")

        packet, packet_json, packet_digest = _canonicalize_packet(
            preflight_packet,
            qa_task_id=qa_task_id,
            qa_run_id=expected_run_id,
            qa_worker_session_id=worker_session_id,
            qa_profile=qa_run.profile,
            verified_at=verified_at,
        )
        _validate_lineage(conn, qa_task_id=qa_task_id, packet=packet)

        existing_row = conn.execute(
            """
            SELECT * FROM human_review_gates
             WHERE repo = ? AND pr_number = ? AND gate_kind = ?
               AND approved_head_sha = ?
            """,
            (
                packet["repo"],
                packet["pr_number"],
                packet["gate_kind"],
                packet["approved_head_sha"],
            ),
        ).fetchone()
        if existing_row is not None:
            existing = HumanReviewGate.from_row(existing_row)
            if existing.qa_task_id != qa_task_id or existing.qa_run_id != expected_run_id:
                raise ValueError("exact-head gate already belongs to a different QA lineage")
            if existing.approval_packet_sha256 != packet_digest:
                raise ValueError("exact-head gate already exists with a different approval packet")
            if existing.state in TERMINAL_GATE_STATES:
                raise ValueError(
                    f"exact-head gate is already terminal ({existing.state}); fresh QA is required"
                )
            return AdvanceResult(
                gate_id=existing.id,
                task_id=existing.task_id,
                approval_packet_sha256=existing.approval_packet_sha256,
                created=False,
            )

        if qa_task.status != "running" or qa_task.current_run_id != expected_run_id:
            raise ValueError("expected_run_id is not the QA task's current run")
        if qa_run.status != "running" or qa_run.ended_at is not None:
            raise ValueError("trusted QA run is no longer active")

        gate_id = _new_gate_id()
        human_task_id = kb._new_task_id()
        _supersede_active_gate(
            conn,
            replacing_gate_id=gate_id,
            packet=packet,
            now=now,
        )

        human_title = f"Human review: {packet['linear_issue_id']} PR #{packet['pr_number']}"
        conn.execute(
            """
            INSERT INTO tasks (
                id, title, body, assignee, status, priority,
                created_by, created_at, workspace_kind, tenant, idempotency_key
            ) VALUES (?, ?, ?, ?, 'awaiting_human', 0, ?, ?, 'scratch', ?, ?)
            """,
            (
                human_task_id,
                human_title,
                _human_task_body(gate_id, packet, packet_digest),
                kb._canonical_assignee(str(packet["human_assignee"])),
                EXPECTED_QA_PROFILE,
                now,
                qa_task.tenant,
                _canonical_dedupe_key(packet),
            ),
        )
        conn.execute(
            "INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)",
            (qa_task_id, human_task_id),
        )
        conn.execute(
            """
            INSERT INTO human_review_gates (
                id, task_id, schema_version, gate_kind,
                reviewer_principal, notification_principal,
                repo, pr_number, pr_url, linear_issue_id,
                base_branch, head_branch, approved_head_sha,
                implementation_task_id, qa_task_id, qa_run_id,
                qa_worker_session_id, qa_verdict, qa_attempt_count,
                coder_correction_attempt_count, qa_approved_at,
                approval_packet_json, approval_packet_sha256,
                state, superseded_by_gate_id, created_at, updated_at
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, 'pending_delivery', NULL, ?, ?
            )
            """,
            (
                gate_id,
                human_task_id,
                SCHEMA_VERSION,
                packet["gate_kind"],
                packet["reviewer_principal"],
                packet.get("notification_principal"),
                packet["repo"],
                packet["pr_number"],
                packet["pr_url"],
                packet.get("linear_issue_id"),
                packet["base_branch"],
                packet["head_branch"],
                packet["approved_head_sha"],
                packet["implementation_task_id"],
                qa_task_id,
                expected_run_id,
                worker_session_id,
                packet["qa_verdict"],
                packet["qa_attempt_count"],
                packet["coder_correction_attempt_count"],
                now,
                packet_json,
                packet_digest,
                now,
                now,
            ),
        )
        _insert_gate_deliveries(
            conn,
            gate_id=gate_id,
            packet=packet,
            created_at=now,
        )

        completion_result = f"Advanced exact head to human review gate {gate_id}"
        updated = conn.execute(
            """
            UPDATE tasks
               SET status='done', result=?, completed_at=?,
                   claim_lock=NULL, claim_expires=NULL, worker_pid=NULL,
                   block_kind=NULL, block_recurrences=0
             WHERE id=? AND status='running' AND current_run_id=?
            """,
            (completion_result, now, qa_task_id, expected_run_id),
        )
        if updated.rowcount != 1:
            raise ValueError("QA task changed while advancing to human review")
        completion_metadata = {
            "human_review_gate_id": gate_id,
            "human_review_task_id": human_task_id,
            "created_child_ids": [human_task_id],
            "child_assignees": [str(packet["human_assignee"])],
            "approval_packet_sha256": packet_digest,
            "approved_head_sha": packet["approved_head_sha"],
            "qa_verdict": packet["qa_verdict"],
            "requires_srdja_review": True,
            "merge_policy": "human_only",
        }
        completed_run_id = kb._end_run(
            conn,
            qa_task_id,
            outcome="completed",
            status="done",
            summary=completion_result,
            metadata=completion_metadata,
        )
        if completed_run_id != expected_run_id:
            raise ValueError("QA run identity changed while closing the human-review gate")
        kb._append_event(
            conn,
            qa_task_id,
            "human_gate_created",
            {
                "gate_id": gate_id,
                "task_id": human_task_id,
                "approved_head_sha": packet["approved_head_sha"],
                "approval_packet_sha256": packet_digest,
            },
            run_id=expected_run_id,
        )
        kb._append_event(
            conn,
            human_task_id,
            "human_gate_created",
            {
                "gate_id": gate_id,
                "qa_task_id": qa_task_id,
                "qa_run_id": expected_run_id,
                "approved_head_sha": packet["approved_head_sha"],
                "approval_packet_sha256": packet_digest,
            },
        )
        kb._append_event(
            conn,
            human_task_id,
            "human_gate_delivery_enqueued",
            {
                "gate_id": gate_id,
                "channels": list(DEFAULT_DELIVERY_CHANNELS),
            },
        )
        kb._append_event(
            conn,
            qa_task_id,
            "completed",
            {
                "result_len": len(completion_result),
                "summary": completion_result,
                "verified_cards": [human_task_id],
                "gate_id": gate_id,
                "approval_packet_sha256": packet_digest,
            },
            run_id=expected_run_id,
        )
        created_result = AdvanceResult(
            gate_id=gate_id,
            task_id=human_task_id,
            approval_packet_sha256=packet_digest,
            created=True,
        )

    # Match normal completion side effects only after the atomic state change is
    # durable. None of these paths performs network I/O.
    kb._clear_failure_counter(conn, qa_task_id)
    kb.recompute_ready(conn)
    kb._cleanup_workspace(conn, qa_task_id)
    kb._fire_kanban_lifecycle_hook(
        "kanban_task_completed",
        qa_task_id,
        board=board,
        assignee=EXPECTED_QA_PROFILE,
        run_id=completed_run_id,
        summary=f"Advanced to human review gate {created_result.gate_id}",
    )
    return created_result
