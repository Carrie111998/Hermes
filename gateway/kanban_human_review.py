"""Test-only human-review outbox and reconciliation harness.

Nothing in gateway startup imports this module. It accepts only the in-memory
``FakeReviewDeliveryAdapter`` below, has no credentials or network clients, and
exposes no merge, branch-write, push, webhook, or auto-merge operation. The
harness exercises the durable gate/outbox contract before any live integration
is separately designed and approved.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_human_review as hr


MAX_DELIVERY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 30
_ALLOWED_FAKE_OUTCOMES = {"success", "error", "timeout_after_send"}


class FakeDeliveryError(RuntimeError):
    """A deterministic fake destination rejection."""


class FakeTimeoutAfterSend(RuntimeError):
    """The fake provider recorded the marker, then simulated a timeout."""


@dataclass(frozen=True)
class FakeDeliveryRecord:
    channel: str
    destination: str
    marker: str
    payload: dict[str, Any]
    external_id: str


class FakeReviewDeliveryAdapter:
    """In-memory adapter used by tests and synthetic readiness checks only."""

    def __init__(self) -> None:
        self.records: list[FakeDeliveryRecord] = []
        self._records_by_key: dict[tuple[str, str, str], FakeDeliveryRecord] = {}
        self._planned_outcomes: dict[str, list[str]] = {}
        self._next_id = 1

    def plan(self, channel: str, *outcomes: str) -> None:
        if channel not in hr.VALID_DELIVERY_CHANNELS:
            raise ValueError(f"unsupported fake delivery channel: {channel!r}")
        invalid = [outcome for outcome in outcomes if outcome not in _ALLOWED_FAKE_OUTCOMES]
        if invalid:
            raise ValueError(f"unsupported fake delivery outcomes: {invalid!r}")
        self._planned_outcomes.setdefault(channel, []).extend(outcomes)

    def find_by_marker(
        self,
        *,
        channel: str,
        destination: str,
        marker: str,
    ) -> Optional[FakeDeliveryRecord]:
        return self._records_by_key.get((channel, destination, marker))

    def send(
        self,
        *,
        channel: str,
        destination: str,
        marker: str,
        payload: Mapping[str, Any],
    ) -> str:
        existing = self.find_by_marker(
            channel=channel,
            destination=destination,
            marker=marker,
        )
        if existing is not None:
            return existing.external_id

        planned = self._planned_outcomes.get(channel)
        outcome = planned.pop(0) if planned else "success"
        if outcome == "error":
            raise FakeDeliveryError(f"fake {channel} rejection")

        external_id = f"fake-{channel}-{self._next_id}"
        self._next_id += 1
        record = FakeDeliveryRecord(
            channel=channel,
            destination=destination,
            marker=marker,
            payload=dict(payload),
            external_id=external_id,
        )
        self.records.append(record)
        self._records_by_key[(channel, destination, marker)] = record
        if outcome == "timeout_after_send":
            raise FakeTimeoutAfterSend(f"fake {channel} timeout after send")
        return external_id


@dataclass(frozen=True)
class FakeProcessResult:
    gate_id: str
    sent: tuple[str, ...]
    deduplicated: tuple[str, ...]
    failed: tuple[str, ...]
    skipped: tuple[str, ...]
    superseded: bool


def _reviewer_login(gate: hr.HumanReviewGate) -> str:
    prefix, separator, login = gate.reviewer_principal.partition(":")
    if prefix != "github" or not separator or not login:
        raise ValueError("gate reviewer_principal is not a GitHub login")
    return login


def _notification_user(gate: hr.HumanReviewGate) -> str:
    principal = gate.notification_principal or ""
    prefix, separator, user_id = principal.partition(":")
    if prefix != "slack" or not separator or not user_id:
        raise ValueError("gate notification_principal is not a Slack user")
    return user_id


def render_fake_delivery(
    gate: hr.HumanReviewGate,
    delivery: hr.ReviewGateDelivery,
) -> dict[str, Any]:
    """Render the exact synthetic payload a future restricted adapter would see."""
    packet = gate.approval_packet
    if delivery.channel == "github_review_request":
        return {
            "operation": "request_review",
            "reviewer": _reviewer_login(gate),
            "repo": gate.repo,
            "pr_number": gate.pr_number,
            "approved_head_sha": gate.approved_head_sha,
            "gate_id": gate.id,
            "marker": delivery.dedupe_marker,
        }
    if delivery.channel == "github_comment":
        reviewer = _reviewer_login(gate)
        text = "\n".join(
            (
                delivery.dedupe_marker,
                f"@{reviewer} QA approved this exact head for human review.",
                f"Head: `{gate.approved_head_sha}`",
                f"Gate: `{gate.id}`",
                f"Packet SHA-256: `{gate.approval_packet_sha256}`",
                "This is not merge approval. Any new push invalidates this packet and requires fresh QA.",
            )
        )
        return {
            "operation": "comment",
            "repo": gate.repo,
            "pr_number": gate.pr_number,
            "body": text,
            "approved_head_sha": gate.approved_head_sha,
            "gate_id": gate.id,
            "marker": delivery.dedupe_marker,
        }
    if delivery.channel == "slack":
        user_id = _notification_user(gate)
        text = "\n".join(
            (
                f"<@{user_id}> PR #{gate.pr_number} is ready for CTO review at {gate.approved_head_sha}.",
                f"Linear: {packet['linear_issue_id']} — {packet['linear_issue_url']}",
                f"PR: {gate.pr_url}",
                f"QA verdict: {gate.qa_verdict}",
                f"Gate: {gate.id}",
                f"Packet SHA-256: {gate.approval_packet_sha256}",
                "Review/approval and merge happen in GitHub. This Slack thread is notification/acknowledgement only. Any new push invalidates this packet.",
            )
        )
        return {
            "operation": "notify",
            "destination": delivery.destination,
            "body": text,
            "approved_head_sha": gate.approved_head_sha,
            "gate_id": gate.id,
            "marker": delivery.dedupe_marker,
        }
    raise ValueError(f"unsupported fake delivery channel: {delivery.channel!r}")


def _claim_delivery(
    conn,
    gate_id: str,
    channel: str,
    *,
    now: int,
) -> Optional[hr.ReviewGateDelivery]:
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT * FROM review_gate_deliveries WHERE gate_id=? AND channel=?",
            (gate_id, channel),
        ).fetchone()
        if row is None:
            return None
        delivery = hr.ReviewGateDelivery.from_row(row)
        if delivery.state not in {"pending", "retry"}:
            return None
        if delivery.next_attempt_at is not None and delivery.next_attempt_at > now:
            return None
        updated = conn.execute(
            "UPDATE review_gate_deliveries "
            "SET state='attempting', attempt_count=attempt_count+1, updated_at=? "
            "WHERE gate_id=? AND channel=? AND state=? AND attempt_count=?",
            (now, gate_id, channel, delivery.state, delivery.attempt_count),
        )
        if updated.rowcount != 1:
            return None
        claimed = conn.execute(
            "SELECT * FROM review_gate_deliveries WHERE gate_id=? AND channel=?",
            (gate_id, channel),
        ).fetchone()
        return hr.ReviewGateDelivery.from_row(claimed)


def _mark_sent(
    conn,
    gate: hr.HumanReviewGate,
    delivery: hr.ReviewGateDelivery,
    *,
    external_id: str,
    now: int,
) -> bool:
    with kb.write_txn(conn):
        updated = conn.execute(
            "UPDATE review_gate_deliveries "
            "SET state='sent', external_id=?, next_attempt_at=NULL, last_error=NULL, updated_at=? "
            "WHERE gate_id=? AND channel=? AND state='attempting'",
            (external_id, now, gate.id, delivery.channel),
        )
        if updated.rowcount != 1:
            return False
        kb._append_event(
            conn,
            gate.task_id,
            "human_gate_delivery_sent",
            {
                "gate_id": gate.id,
                "channel": delivery.channel,
                "external_id": external_id,
                "dedupe_marker": delivery.dedupe_marker,
            },
        )
    return True


def _mark_failed(
    conn,
    gate: hr.HumanReviewGate,
    delivery: hr.ReviewGateDelivery,
    *,
    error: str,
    now: int,
) -> None:
    terminal = delivery.attempt_count >= MAX_DELIVERY_ATTEMPTS
    state = "failed" if terminal else "retry"
    next_attempt_at = None if terminal else now + RETRY_DELAY_SECONDS
    safe_error = str(error).replace("\n", " ")[:500]
    with kb.write_txn(conn):
        updated = conn.execute(
            "UPDATE review_gate_deliveries "
            "SET state=?, next_attempt_at=?, last_error=?, updated_at=? "
            "WHERE gate_id=? AND channel=? AND state='attempting'",
            (state, next_attempt_at, safe_error, now, gate.id, delivery.channel),
        )
        if updated.rowcount != 1:
            return
        kb._append_event(
            conn,
            gate.task_id,
            "human_gate_delivery_failed",
            {
                "gate_id": gate.id,
                "channel": delivery.channel,
                "attempt_count": delivery.attempt_count,
                "retryable": not terminal,
                "error": safe_error,
            },
        )


def _refresh_gate_delivery_state(conn, gate_id: str, *, now: int) -> None:
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT * FROM human_review_gates WHERE id=?",
            (gate_id,),
        ).fetchone()
        if row is None:
            return
        gate = hr.HumanReviewGate.from_row(row)
        if gate.state not in {"pending_delivery", "delivery_failed"}:
            return
        states = [
            row["state"]
            for row in conn.execute(
                "SELECT state FROM review_gate_deliveries WHERE gate_id=? ORDER BY channel",
                (gate_id,),
            ).fetchall()
        ]
        if states and all(state == "sent" for state in states):
            target = "awaiting_human"
        elif "failed" in states:
            target = "delivery_failed"
        else:
            target = "pending_delivery"
        if target == gate.state:
            return
        conn.execute(
            "UPDATE human_review_gates SET state=?, updated_at=? WHERE id=? AND state=?",
            (target, now, gate_id, gate.state),
        )


def process_fake_gate_outbox(
    conn,
    gate_id: str,
    *,
    adapter: FakeReviewDeliveryAdapter,
    snapshot_provider: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    now: Optional[int] = None,
) -> FakeProcessResult:
    """Process due rows through the in-memory adapter, one destination at a time."""
    if type(adapter) is not FakeReviewDeliveryAdapter:
        raise TypeError("only the built-in test-only FakeReviewDeliveryAdapter is accepted")
    attempted_at = int(time.time()) if now is None else int(now)
    gate = hr.get_human_review_gate(conn, gate_id)
    if gate is None:
        raise ValueError(f"human-review gate {gate_id!r} does not exist")
    if gate.state not in hr.ACTIVE_GATE_STATES:
        return FakeProcessResult(gate_id, (), (), (), (), False)

    sent: list[str] = []
    deduplicated: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    superseded = False

    for candidate in hr.list_gate_deliveries(conn, gate_id):
        delivery = _claim_delivery(
            conn,
            gate_id,
            candidate.channel,
            now=attempted_at,
        )
        if delivery is None:
            skipped.append(candidate.channel)
            continue

        snapshot: Mapping[str, Any] = {}
        try:
            snapshot = snapshot_provider(gate.approval_packet)
            hr.validate_pr_snapshot_for_gate(gate, snapshot)
        except hr.PRSnapshotMismatch as exc:
            observed = snapshot.get("head_sha") if isinstance(snapshot, Mapping) else None
            superseded = hr.supersede_human_review_gate(
                conn,
                gate_id,
                reason=str(exc),
                observed_head_sha=str(observed) if observed is not None else None,
                now=attempted_at,
            )
            break
        except Exception as exc:
            _mark_failed(conn, gate, delivery, error=str(exc), now=attempted_at)
            failed.append(delivery.channel)
            continue

        existing = adapter.find_by_marker(
            channel=delivery.channel,
            destination=delivery.destination,
            marker=delivery.dedupe_marker,
        )
        if existing is not None:
            _mark_sent(
                conn,
                gate,
                delivery,
                external_id=existing.external_id,
                now=attempted_at,
            )
            deduplicated.append(delivery.channel)
            continue

        payload = render_fake_delivery(gate, delivery)
        try:
            external_id = adapter.send(
                channel=delivery.channel,
                destination=delivery.destination,
                marker=delivery.dedupe_marker,
                payload=payload,
            )
        except FakeTimeoutAfterSend as exc:
            readback = adapter.find_by_marker(
                channel=delivery.channel,
                destination=delivery.destination,
                marker=delivery.dedupe_marker,
            )
            if readback is None:
                _mark_failed(conn, gate, delivery, error=str(exc), now=attempted_at)
                failed.append(delivery.channel)
                continue
            external_id = readback.external_id
            deduplicated.append(delivery.channel)
        except FakeDeliveryError as exc:
            _mark_failed(conn, gate, delivery, error=str(exc), now=attempted_at)
            failed.append(delivery.channel)
            continue

        if _mark_sent(
            conn,
            gate,
            delivery,
            external_id=external_id,
            now=attempted_at,
        ):
            sent.append(delivery.channel)

    _refresh_gate_delivery_state(conn, gate_id, now=attempted_at)
    return FakeProcessResult(
        gate_id=gate_id,
        sent=tuple(sent),
        deduplicated=tuple(deduplicated),
        failed=tuple(failed),
        skipped=tuple(skipped),
        superseded=superseded,
    )


def record_fake_slack_ack(
    conn,
    gate_id: str,
    *,
    actor_principal: str,
    text: str,
    pr_snapshot: Mapping[str, Any],
    now: Optional[int] = None,
) -> bool:
    """Accept only ``ACK <gate-id>`` from the configured Slack principal."""
    gate = hr.get_human_review_gate(conn, gate_id)
    if gate is None:
        raise ValueError(f"human-review gate {gate_id!r} does not exist")
    if text.strip() != f"ACK {gate_id}":
        return False
    if actor_principal != gate.notification_principal:
        raise ValueError("Slack acknowledgement principal does not match the gate")
    changed_at = int(time.time()) if now is None else int(now)
    try:
        hr.validate_pr_snapshot_for_gate(gate, pr_snapshot)
    except hr.PRSnapshotMismatch as exc:
        hr.supersede_human_review_gate(
            conn,
            gate_id,
            reason=str(exc),
            observed_head_sha=str(pr_snapshot.get("head_sha") or "") or None,
            now=changed_at,
        )
        return False
    with kb.write_txn(conn):
        updated = conn.execute(
            "UPDATE human_review_gates SET state='seen', updated_at=? "
            "WHERE id=? AND state='awaiting_human'",
            (changed_at, gate_id),
        )
        if updated.rowcount != 1:
            return False
        kb._append_event(
            conn,
            gate.task_id,
            "human_gate_seen",
            {
                "gate_id": gate_id,
                "actor_principal": actor_principal,
                "decision_authority": "github_only",
            },
        )
    return True


def reconcile_fake_github_review(
    conn,
    gate_id: str,
    *,
    reviewer_principal: str,
    review_state: str,
    review_head_sha: str,
    external_review_id: str,
    pr_snapshot: Mapping[str, Any],
    now: Optional[int] = None,
) -> bool:
    """Apply a synthetic exact-head GitHub review; no merge operation exists."""
    gate = hr.get_human_review_gate(conn, gate_id)
    if gate is None:
        raise ValueError(f"human-review gate {gate_id!r} does not exist")
    changed_at = int(time.time()) if now is None else int(now)
    try:
        hr.validate_pr_snapshot_for_gate(gate, pr_snapshot)
    except hr.PRSnapshotMismatch as exc:
        hr.supersede_human_review_gate(
            conn,
            gate_id,
            reason=str(exc),
            observed_head_sha=str(pr_snapshot.get("head_sha") or "") or None,
            now=changed_at,
        )
        return False
    return hr.record_human_review_decision(
        conn,
        gate_id,
        reviewer_principal=reviewer_principal,
        review_state=review_state,
        review_head_sha=review_head_sha,
        external_review_id=external_review_id,
        now=changed_at,
    )


def reconcile_fake_pr_terminal_state(
    conn,
    gate_id: str,
    *,
    pr_snapshot: Mapping[str, Any],
    now: Optional[int] = None,
) -> bool:
    """Observe a synthetic merge/close as audit state; never performs it."""
    gate = hr.get_human_review_gate(conn, gate_id)
    if gate is None:
        raise ValueError(f"human-review gate {gate_id!r} does not exist")
    changed_at = int(time.time()) if now is None else int(now)
    if pr_snapshot.get("source") != "github_readback":
        raise hr.PRSnapshotUnavailable("pr_snapshot source is not trusted")
    raw_verified_at = pr_snapshot.get("verified_at")
    try:
        verified_at = int(str(raw_verified_at))
    except (TypeError, ValueError) as exc:
        raise hr.PRSnapshotUnavailable("pr_snapshot verified_at is invalid") from exc
    current_time = int(time.time())
    if verified_at < current_time - hr.MAX_PR_SNAPSHOT_AGE_SECONDS:
        raise hr.PRSnapshotUnavailable("pr_snapshot is stale; refresh the live PR readback")
    if verified_at > current_time + hr.MAX_PR_SNAPSHOT_FUTURE_SKEW_SECONDS:
        raise hr.PRSnapshotUnavailable(
            "pr_snapshot verified_at is implausibly far in the future"
        )
    state = str(pr_snapshot.get("state") or "").upper()
    if state not in {"MERGED", "CLOSED"}:
        raise ValueError("terminal reconciliation requires a MERGED or CLOSED PR")
    expected = {
        "repo": gate.repo,
        "pr_number": gate.pr_number,
        "pr_url": gate.pr_url,
        "base_branch": gate.base_branch,
        "head_branch": gate.head_branch,
        "head_sha": gate.approved_head_sha,
    }
    for key, expected_value in expected.items():
        actual = pr_snapshot.get(key)
        if key == "pr_number":
            try:
                actual = int(str(actual))
            except (TypeError, ValueError):
                pass
        if actual != expected_value:
            hr.supersede_human_review_gate(
                conn,
                gate_id,
                reason=(
                    f"terminal PR {key} does not match the gate "
                    f"({actual!r} != {expected_value!r})"
                ),
                observed_head_sha=str(pr_snapshot.get("head_sha") or "") or None,
                now=changed_at,
            )
            return False

    target = "merged" if state == "MERGED" else "closed"
    with kb.write_txn(conn):
        current_row = conn.execute(
            "SELECT * FROM human_review_gates WHERE id=?",
            (gate_id,),
        ).fetchone()
        current = hr.HumanReviewGate.from_row(current_row)
        if current.state == target:
            return False
        if current.state == "superseded":
            return False
        prior_state = current.state
        updated = conn.execute(
            "UPDATE human_review_gates SET state=?, updated_at=? WHERE id=? AND state=?",
            (target, changed_at, gate_id, prior_state),
        )
        if updated.rowcount != 1:
            return False
        conn.execute(
            "UPDATE tasks SET status='done', result=?, completed_at=? "
            "WHERE id=? AND status='awaiting_human'",
            (f"Observed PR {state.lower()} by an external human action", changed_at, gate.task_id),
        )
        conn.execute(
            "UPDATE review_gate_deliveries SET state='superseded', updated_at=? "
            "WHERE gate_id=? AND state IN ('pending', 'attempting', 'retry', 'failed')",
            (changed_at, gate_id),
        )
        kb._append_event(
            conn,
            gate.task_id,
            f"human_gate_{target}",
            {
                "gate_id": gate_id,
                "approved_head_sha": gate.approved_head_sha,
                "observed_pr_state": state,
            },
        )
        if state == "MERGED" and prior_state != "human_approved":
            kb._append_event(
                conn,
                gate.task_id,
                "merged_without_current_human_approval",
                {
                    "gate_id": gate_id,
                    "approved_head_sha": gate.approved_head_sha,
                    "prior_gate_state": prior_state,
                },
            )
    return True
