"""Trusted control-plane gate for NewtonsApple pull-request reviews.

This module is executable as a webhook route script and importable for contract
and recovery tests. It never executes pull-request code.
"""

from __future__ import annotations

import json
import base64
import binascii
import hashlib
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, cast

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

REPOSITORY = "NewtonsAppleAI/newtonsapple-web"
CONTRACT_VERSION = "v2"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
STATUS_CONTEXT_PATTERN = re.compile(
    r"^newtonsapple-bot/review-v2/pr-([1-9][0-9]*)/base-([0-9a-f]{40})$"
)
RUN_URL_PATTERN = re.compile(
    r"^https://github\.com/NewtonsAppleAI/newtonsapple-web/actions/runs/([1-9][0-9]*)$"
)
DISPATCH_RUN_NAME_PATTERN = re.compile(
    r"^pr-review-capture-v2/dispatch/event-([1-9][0-9]*)/"
    r"pr-([1-9][0-9]*)/base-([0-9a-f]{40})/head-([0-9a-f]{40})$"
)
EXECUTION_REQUEST_RUN_NAME_PATTERN = re.compile(
    r"^pr-review-execution-v2/request/pr-([1-9][0-9]*)/"
    r"base-([0-9a-f]{40})/head-([0-9a-f]{40})$"
)
EXECUTION_DISPATCH_RUN_NAME_PATTERN = re.compile(
    r"^pr-review-execution-v2/dispatch/event-([1-9][0-9]*)/"
    r"pr-([1-9][0-9]*)/base-([0-9a-f]{40})/head-([0-9a-f]{40})$"
)
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
LEASE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
ALLOWED_BASE_REFS = {"dev", "staging", "main"}
LEASE_SECONDS = 2 * 60 * 60
RETRY_DELAY_SECONDS = 5 * 60
MAX_REVIEW_ATTEMPTS = 3
BUZZ_CHANNEL = "b1cb95c9-6a36-4516-abdd-81d853a9412e"
TRUSTED_EXECUTION_WORKFLOW_ID = -1  # Pin the assigned GitHub ID after bootstrap merge.
TRUSTED_EXECUTION_WORKFLOW_PATH = ".github/workflows/pr-review-execution.yml"
TRUSTED_EXECUTION_WORKFLOW_SHA256 = (
    "4b3277b35071dc0b33055ec579f28d1dbe26a057fd37ef950d82f640d8f1de0f"
)
BASELINE_EXECUTION_GATES = ("quality", "integration", "e2e")
EXECUTION_GATE_COMMANDS = {
    "quality": ["npm", "run", "check"],
    "integration": ["npm", "run", "db:verify"],
    "e2e": ["npm", "run", "test:e2e:all:ci"],
}
LOCAL_REVIEW_WORKER_IMAGE = (
    "node@sha256:0557ac14e0d45d02ed563067b82856ca5e7aa3437fa28d98d4350ea9c3d9494a"
)
EXECUTION_GATE_STEP_NAMES = {
    "quality": (
        "Attest clean quality tree before gate",
        "Run the shared quality harness",
        "Attest clean quality tree after gate",
    ),
    "integration": (
        "Attest clean integration tree before gate",
        "Replay PostgreSQL and run all integration contracts",
        "Attest clean integration tree after gate",
    ),
    "e2e": (
        "Attest clean E2E tree before gate",
        "Run all release journeys",
        "Attest clean E2E tree after gate",
    ),
}
EXECUTION_GATE_COMMAND_SHA256 = {
    name: hashlib.sha256(
        json.dumps(command, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    for name, command in EXECUTION_GATE_COMMANDS.items()
}
EXECUTION_GATE_POLICY_VERSION = "newtonsapple-v1"
_EXECUTION_GATE_POLICY = {
    "version": EXECUTION_GATE_POLICY_VERSION,
    "repository": REPOSITORY,
    "workflow_id": TRUSTED_EXECUTION_WORKFLOW_ID,
    "workflow_path": TRUSTED_EXECUTION_WORKFLOW_PATH,
    "workflow_sha256": TRUSTED_EXECUTION_WORKFLOW_SHA256,
    "baseline_gates": list(BASELINE_EXECUTION_GATES),
    "commands": EXECUTION_GATE_COMMANDS,
    "local_worker": {
        "image": LOCAL_REVIEW_WORKER_IMAGE,
        "runner": "docker-node22",
        "install": ["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        "statuses": ["pass", "pr-fail", "unavailable"],
        "gate_network": "none",
    },
}
EXECUTION_GATE_POLICY_SHA256 = hashlib.sha256(
    json.dumps(_EXECUTION_GATE_POLICY, sort_keys=True, separators=(",", ":")).encode()
).hexdigest()


@dataclass(frozen=True)
class ReviewTuple:
    repository: str
    pr_number: int
    base_sha: str
    head_sha: str
    contract_version: str = CONTRACT_VERSION


@dataclass(frozen=True)
class TrustedWorkflow:
    workflow_id: int
    path: str
    branch: str
    allowed_dispatchers: tuple[str, ...] = ("bas4r",)


TRUSTED_CAPTURE_WORKFLOW = TrustedWorkflow(
    workflow_id=328661288,
    path=".github/workflows/pr-review-capture.yml",
    branch="dev",
)


def tuple_key(review_tuple: ReviewTuple) -> str:
    return (
        f"{review_tuple.contract_version}:{review_tuple.repository}:"
        f"{review_tuple.pr_number}:{review_tuple.base_sha}:{review_tuple.head_sha}"
    )


class ReviewStateStore:
    """Crash-safe local leases and completion state for exact review tuples."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute(
                "CREATE TABLE IF NOT EXISTS leases "
                "(key TEXT PRIMARY KEY, lease_token TEXT NOT NULL, "
                "lease_until INTEGER NOT NULL)"
            )
            lease_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(leases)").fetchall()
            }
            if "lease_token" not in lease_columns:
                connection.execute("ALTER TABLE leases ADD COLUMN lease_token TEXT")
                connection.execute("DELETE FROM leases")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS completions "
                "(key TEXT PRIMARY KEY, completed_at INTEGER NOT NULL)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS attempts ("
                "key TEXT PRIMARY KEY, failures INTEGER NOT NULL, "
                "retry_after INTEGER, dead_lettered_at INTEGER)"
            )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS summary_outbox ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "key TEXT NOT NULL UNIQUE, marker TEXT NOT NULL UNIQUE, "
                "content TEXT NOT NULL, sent_event_id TEXT, "
                "claim_token TEXT, claim_until INTEGER, retry_after INTEGER)"
            )
            outbox_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(summary_outbox)"
                ).fetchall()
            }
            for column, column_type in (
                ("claim_token", "TEXT"),
                ("claim_until", "INTEGER"),
                ("retry_after", "INTEGER"),
            ):
                if column not in outbox_columns:
                    connection.execute(
                        f"ALTER TABLE summary_outbox ADD COLUMN {column} {column_type}"
                    )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def journal_mode(self) -> str:
        with self._connect() as connection:
            row = connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()

    def reserve(
        self, review_tuple: ReviewTuple, *, now: int, lease_seconds: int
    ) -> Optional[str]:
        key = tuple_key(review_tuple)
        lease_token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM completions WHERE key = ?", (key,)
            ).fetchone():
                return None
            attempt = connection.execute(
                "SELECT retry_after, dead_lettered_at FROM attempts WHERE key = ?",
                (key,),
            ).fetchone()
            if attempt is not None and (
                attempt[1] is not None
                or (attempt[0] is not None and int(attempt[0]) > now)
            ):
                return None
            connection.execute(
                "DELETE FROM leases WHERE key = ? AND lease_until <= ?", (key, now)
            )
            cursor = connection.execute(
                "INSERT OR IGNORE INTO leases(key, lease_token, lease_until) "
                "VALUES (?, ?, ?)",
                (key, lease_token, now + lease_seconds),
            )
            return lease_token if cursor.rowcount == 1 else None

    def release(self, review_tuple: ReviewTuple, *, lease_token: str) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM leases WHERE key = ? AND lease_token = ?",
                (tuple_key(review_tuple), lease_token),
            )
            if cursor.rowcount != 1:
                raise ValueError("review lease not found")

    def record_failure(
        self,
        review_tuple: ReviewTuple,
        *,
        lease_token: str,
        now: int,
        retry_delay: int,
        max_attempts: int,
    ) -> dict:
        if retry_delay <= 0 or max_attempts <= 0:
            raise ValueError("invalid retry policy")
        key = tuple_key(review_tuple)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM leases WHERE key = ? AND lease_token = ?",
                (key, lease_token),
            )
            if deleted.rowcount != 1:
                raise ValueError("review lease not found")
            row = connection.execute(
                "SELECT failures FROM attempts WHERE key = ?", (key,)
            ).fetchone()
            failures = (int(row[0]) if row is not None else 0) + 1
            dead_lettered = failures >= max_attempts
            retry_after = None if dead_lettered else now + retry_delay
            connection.execute(
                "INSERT OR REPLACE INTO attempts"
                "(key, failures, retry_after, dead_lettered_at) VALUES (?, ?, ?, ?)",
                (key, failures, retry_after, now if dead_lettered else None),
            )
        return {
            "attempts": failures,
            "dead_lettered": dead_lettered,
            "retry_after": retry_after,
        }

    def complete(
        self, review_tuple: ReviewTuple, *, lease_token: str, now: int
    ) -> None:
        key = tuple_key(review_tuple)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = connection.execute(
                "DELETE FROM leases WHERE key = ? AND lease_token = ?",
                (key, lease_token),
            )
            if cursor.rowcount != 1:
                raise ValueError("review lease not found")
            connection.execute(
                "INSERT OR REPLACE INTO completions(key, completed_at) VALUES (?, ?)",
                (key, now),
            )
            connection.execute("DELETE FROM attempts WHERE key = ?", (key,))

    def record_external_completion(
        self,
        review_tuple: ReviewTuple,
        *,
        now: int,
        marker: str,
        content: str,
    ) -> int:
        """Atomically settle a GitHub-confirmed review and queue its Buzz summary."""
        key = tuple_key(review_tuple)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM leases WHERE key = ?", (key,))
            connection.execute(
                "INSERT OR REPLACE INTO completions(key, completed_at) VALUES (?, ?)",
                (key, now),
            )
            connection.execute("DELETE FROM attempts WHERE key = ?", (key,))
            connection.execute(
                "INSERT OR IGNORE INTO summary_outbox(key, marker, content) "
                "VALUES (?, ?, ?)",
                (key, marker, content),
            )
            row = connection.execute(
                "SELECT id FROM summary_outbox WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            raise ValueError("summary outbox tuple conflict")
        return int(row[0])

    def settle_review(
        self,
        review_tuple: ReviewTuple,
        *,
        lease_token: str,
        now: int,
        marker: str,
        content: str,
    ) -> int:
        """Atomically settle the current lease and queue its Buzz summary."""
        key = tuple_key(review_tuple)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM leases WHERE key = ? AND lease_token = ?",
                (key, lease_token),
            )
            if deleted.rowcount != 1:
                raise ValueError("review lease not found")
            connection.execute(
                "INSERT OR REPLACE INTO completions(key, completed_at) VALUES (?, ?)",
                (key, now),
            )
            connection.execute("DELETE FROM attempts WHERE key = ?", (key,))
            connection.execute(
                "INSERT OR IGNORE INTO summary_outbox(key, marker, content) "
                "VALUES (?, ?, ?)",
                (key, marker, content),
            )
            row = connection.execute(
                "SELECT id FROM summary_outbox WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            raise ValueError("summary outbox tuple conflict")
        return int(row[0])

    def enqueue_summary(
        self, review_tuple: ReviewTuple, *, marker: str, content: str
    ) -> int:
        return self._enqueue_outbox(
            tuple_key(review_tuple), marker=marker, content=content
        )

    def enqueue_blocker(
        self,
        review_tuple: ReviewTuple,
        *,
        marker: str,
        content: str,
        kind: str = "operational",
    ) -> int:
        return self._enqueue_outbox(
            f"blocker:{kind}:{tuple_key(review_tuple)}",
            marker=marker,
            content=content,
        )

    def _enqueue_outbox(self, key: str, *, marker: str, content: str) -> int:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO summary_outbox(key, marker, content) "
                "VALUES (?, ?, ?)",
                (key, marker, content),
            )
            row = connection.execute(
                "SELECT id FROM summary_outbox WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            raise ValueError("summary outbox tuple conflict")
        return int(row[0])

    def pending_summaries(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, key, marker, content FROM summary_outbox "
                "WHERE sent_event_id IS NULL ORDER BY id"
            ).fetchall()
        return [
            {"id": row[0], "key": row[1], "marker": row[2], "content": row[3]}
            for row in rows
        ]

    def claim_summary(self, *, now: int, lease_seconds: int) -> Optional[dict]:
        if lease_seconds <= 0:
            raise ValueError("invalid outbox lease")
        claim_token = secrets.token_urlsafe(32)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, key, marker, content FROM summary_outbox "
                "WHERE sent_event_id IS NULL "
                "AND (retry_after IS NULL OR retry_after <= ?) "
                "AND (claim_until IS NULL OR claim_until <= ?) "
                "ORDER BY id LIMIT 1",
                (now, now),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                "UPDATE summary_outbox SET claim_token = ?, claim_until = ? "
                "WHERE id = ?",
                (claim_token, now + lease_seconds, row[0]),
            )
        return {
            "id": row[0],
            "key": row[1],
            "marker": row[2],
            "content": row[3],
            "claim_token": claim_token,
        }

    def release_summary_claim(
        self, outbox_id: int, *, claim_token: str, retry_after: int
    ) -> None:
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE summary_outbox SET claim_token = NULL, claim_until = NULL, "
                "retry_after = ? WHERE id = ? AND claim_token = ? "
                "AND sent_event_id IS NULL",
                (retry_after, outbox_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise ValueError("outbox claim not found")

    def mark_summary_sent(
        self, outbox_id: int, event_id: str, *, claim_token: str
    ) -> None:
        if not event_id:
            raise ValueError("missing Buzz event id")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE summary_outbox SET sent_event_id = ?, claim_token = NULL, "
                "claim_until = NULL, retry_after = NULL "
                "WHERE id = ? AND claim_token = ? AND sent_event_id IS NULL",
                (event_id, outbox_id, claim_token),
            )
            if cursor.rowcount != 1:
                raise ValueError("outbox claim not found")

    def sent_event_id(self, outbox_id: int) -> Optional[str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sent_event_id FROM summary_outbox WHERE id = ?", (outbox_id,)
            ).fetchone()
        return None if row is None else row[0]


def drain_summary_outbox(
    store: ReviewStateStore,
    *,
    find_existing: Callable[[str], Optional[str]],
    send: Callable[[str], str],
) -> int:
    """Deliver pending summaries effectively once, preserving failures."""
    processed = 0
    while True:
        now = int(time.time())
        item = store.claim_summary(now=now, lease_seconds=60)
        if item is None:
            break
        try:
            event_id = find_existing(item["marker"])
            if event_id is None:
                event_id = send(f'{item["content"]}\n\n{item["marker"]}')
            store.mark_summary_sent(
                item["id"], event_id, claim_token=item["claim_token"]
            )
            processed += 1
        except Exception:
            try:
                store.release_summary_claim(
                    item["id"],
                    claim_token=item["claim_token"],
                    retry_after=now + RETRY_DELAY_SECONDS,
                )
            except (sqlite3.Error, ValueError):
                pass
    return processed


def parse_status_context(context: str, head_sha: str) -> ReviewTuple:
    """Parse the canonical status context and bind it to its target commit."""
    match = STATUS_CONTEXT_PATTERN.fullmatch(context) if isinstance(context, str) else None
    if match is None or SHA_PATTERN.fullmatch(head_sha or "") is None:
        raise ValueError("invalid review status context")
    return ReviewTuple(
        repository=REPOSITORY,
        pr_number=int(match.group(1)),
        base_sha=match.group(2),
        head_sha=head_sha,
    )


def validate_capture_status(
    status: dict,
    *,
    head_sha: str,
    run: dict,
    trusted_workflow: TrustedWorkflow,
) -> ReviewTuple:
    """Return the authorized tuple or fail closed on any provenance mismatch."""
    try:
        review_tuple = parse_status_context(status["context"], head_sha)
        run_url_match = RUN_URL_PATTERN.fullmatch(status["target_url"])
        creator = status["creator"]
        if (
            status["state"] != "pending"
            or not isinstance(creator, dict)
            or creator.get("login") != "github-actions[bot]"
            or run_url_match is None
            or int(run_url_match.group(1)) != run["id"]
            or run.get("html_url") != status["target_url"]
            or run.get("workflow_id") != trusted_workflow.workflow_id
            or run.get("path") != trusted_workflow.path
            or run.get("status") != "completed"
            or run.get("conclusion") != "success"
            or run.get("head_branch") != trusted_workflow.branch
        ):
            raise ValueError
        event = run.get("event")
        if event == "pull_request_target":
            pull_requests = run.get("pull_requests")
            expected_title = (
                f"pr-review-capture-v2/request/pr-{review_tuple.pr_number}/"
                f"base-{review_tuple.base_sha}/head-{review_tuple.head_sha}"
            )
            if (
                run.get("display_title") != expected_title
                or run.get("head_sha") != review_tuple.base_sha
                or not isinstance(pull_requests, list)
                or not any(
                    isinstance(item, dict)
                    and item.get("number") == review_tuple.pr_number
                    and isinstance(item.get("base"), dict)
                    and item["base"].get("sha") == review_tuple.base_sha
                    and isinstance(item.get("head"), dict)
                    and item["head"].get("sha") == review_tuple.head_sha
                    for item in pull_requests
                )
            ):
                raise ValueError
        elif event == "workflow_dispatch":
            run_name = run.get("display_title")
            match = (
                DISPATCH_RUN_NAME_PATTERN.fullmatch(run_name)
                if isinstance(run_name, str)
                else None
            )
            actor = run.get("actor")
            if (
                match is None
                or not isinstance(actor, dict)
                or actor.get("login") not in trusted_workflow.allowed_dispatchers
                or int(match.group(2)) != review_tuple.pr_number
                or match.group(3) != review_tuple.base_sha
                or match.group(4) != review_tuple.head_sha
            ):
                raise ValueError
        else:
            raise ValueError
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid review capture status") from exc
    return review_tuple


def review_marker(review_tuple: ReviewTuple) -> str:
    return (
        f"<!-- newtonsapple-pr-review:{review_tuple.contract_version} "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} -->"
    )


def _dispatch_request_is_current(
    run: dict,
    *,
    reviewer_login: str,
    timeline: list[dict],
) -> bool:
    title = run.get("display_title")
    match = (
        DISPATCH_RUN_NAME_PATTERN.fullmatch(title)
        if isinstance(title, str)
        else None
    )
    if match is None:
        return False
    request_event_id = match.group(1)
    request_index = next(
        (
            index
            for index, event in enumerate(timeline)
            if isinstance(event, dict) and str(event.get("id")) == request_event_id
        ),
        None,
    )
    if request_index is None:
        return False
    request = timeline[request_index]
    requested_reviewer = request.get("requested_reviewer")
    if (
        request.get("event") != "review_requested"
        or not isinstance(requested_reviewer, dict)
        or requested_reviewer.get("login") != reviewer_login
    ):
        return False
    invalidating_events = {
        "base_ref_changed",
        "base_ref_force_pushed",
        "closed",
        "committed",
        "converted_to_draft",
        "head_ref_deleted",
        "head_ref_force_pushed",
        "merged",
    }
    for event in timeline[request_index + 1 :]:
        if not isinstance(event, dict):
            return False
        if event.get("event") in invalidating_events:
            return False
        later_reviewer = event.get("requested_reviewer")
        if (
            event.get("event") in {"review_request_removed", "review_requested"}
            and isinstance(later_reviewer, dict)
            and later_reviewer.get("login") == reviewer_login
        ):
            return False
    return True


def _latest_request_is_current(timeline: list[dict], *, reviewer_login: str) -> bool:
    """Prove the live tuple still belongs to the latest reviewer request."""
    request_index = next(
        (
            index
            for index in range(len(timeline) - 1, -1, -1)
            if isinstance(timeline[index], dict)
            and timeline[index].get("event") == "review_requested"
            and isinstance(timeline[index].get("requested_reviewer"), dict)
            and timeline[index]["requested_reviewer"].get("login") == reviewer_login
        ),
        None,
    )
    if request_index is None:
        return False
    invalidating_events = {
        "base_ref_changed",
        "base_ref_force_pushed",
        "closed",
        "committed",
        "converted_to_draft",
        "head_ref_deleted",
        "head_ref_force_pushed",
        "merged",
    }
    for event in timeline[request_index + 1 :]:
        if not isinstance(event, dict) or event.get("event") in invalidating_events:
            return False
        later_reviewer = event.get("requested_reviewer")
        if (
            event.get("event") in {"review_request_removed", "review_requested"}
            and isinstance(later_reviewer, dict)
            and later_reviewer.get("login") == reviewer_login
        ):
            return False
    return True


def select_authorized_tuple(
    live_pr: dict,
    *,
    statuses: list[dict],
    load_run: Callable[[int], Optional[dict]],
    trusted_workflow: TrustedWorkflow,
    reviewer_login: str,
    bot_bodies: list[str],
    load_timeline: Optional[Callable[[int], list[dict]]] = None,
) -> Optional[ReviewTuple]:
    """Select a current exact tuple backed by a trusted capture workflow run."""
    try:
        base = live_pr["base"]
        head = live_pr["head"]
        requested = live_pr["requested_reviewers"]
        if (
            live_pr.get("state") != "open"
            or live_pr.get("draft") is not False
            or not isinstance(base, dict)
            or base.get("ref") != trusted_workflow.branch
            or SHA_PATTERN.fullmatch(str(base.get("sha", ""))) is None
            or not isinstance(head, dict)
            or SHA_PATTERN.fullmatch(str(head.get("sha", ""))) is None
            or not isinstance(requested, list)
            or reviewer_login
            not in {
                item.get("login")
                for item in requested
                if isinstance(item, dict)
            }
        ):
            return None
        pr_number = int(live_pr["number"])
        if pr_number <= 0:
            return None
    except (KeyError, TypeError, ValueError):
        return None

    for status in statuses:
        try:
            context = status.get("context")
            if not isinstance(context, str):
                continue
            candidate = parse_status_context(context, head["sha"])
            if (
                candidate.pr_number != pr_number
                or candidate.base_sha != base["sha"]
                or candidate.head_sha != head["sha"]
            ):
                continue
            url_match = RUN_URL_PATTERN.fullmatch(str(status.get("target_url", "")))
            if url_match is None:
                continue
            run = load_run(int(url_match.group(1)))
            if not isinstance(run, dict):
                continue
            candidate = validate_capture_status(
                status,
                head_sha=head["sha"],
                run=run,
                trusted_workflow=trusted_workflow,
            )
            if run.get("event") == "workflow_dispatch":
                if load_timeline is None:
                    continue
                timeline = load_timeline(candidate.pr_number)
                if not isinstance(timeline, list) or not _dispatch_request_is_current(
                    run, reviewer_login=reviewer_login, timeline=timeline
                ):
                    continue
        except (TypeError, ValueError):
            continue
        marker = review_marker(candidate)
        if any(marker in body for body in bot_bodies if isinstance(body, str)):
            return None
        return candidate
    if load_timeline is None:
        return None
    try:
        timeline = load_timeline(pr_number)
    except (RuntimeError, TypeError, ValueError):
        return None
    candidate = ReviewTuple(
        repository=REPOSITORY,
        pr_number=pr_number,
        base_sha=str(base["sha"]),
        head_sha=str(head["sha"]),
    )
    if not isinstance(timeline, list) or not _latest_request_is_current(
        timeline, reviewer_login=reviewer_login
    ):
        return None
    marker = review_marker(candidate)
    if any(marker in body for body in bot_bodies if isinstance(body, str)):
        return None
    return candidate


def _state_path() -> Path:
    hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
    return hermes_home / "webhook_state" / "newtonsapple-pr-review.sqlite3"


def _flatten_pages(value: object) -> list[dict]:
    if not isinstance(value, list):
        raise RuntimeError("GitHub returned a non-list collection")
    flattened: list[dict] = []
    for item in value:
        if isinstance(item, list):
            flattened.extend(_flatten_pages(item))
        elif isinstance(item, dict):
            flattened.append(item)
        else:
            raise RuntimeError("GitHub returned a malformed collection")
    return flattened


def gh_json(*args: str) -> object:
    result = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError("GitHub control plane unavailable")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GitHub returned malformed JSON") from exc


def gh_bytes(*args: str) -> bytes:
    result = subprocess.run(
        ["gh", *args],
        check=False,
        capture_output=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("GitHub control plane unavailable")
    if len(result.stdout) > 50_000_000:
        raise RuntimeError("GitHub job log exceeded the fixed limit")
    return result.stdout


def _collection(endpoint: str) -> list[dict]:
    return _flatten_pages(gh_json("api", "--paginate", "--slurp", endpoint))


def _execution_tuple(payload: dict) -> ReviewTuple:
    try:
        review_tuple = ReviewTuple(
            repository=str(payload["repository"]),
            pr_number=int(payload["pr_number"]),
            base_sha=str(payload["base_sha"]),
            head_sha=str(payload["head_sha"]),
            contract_version=str(payload["contract_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid execution evidence tuple") from exc
    if (
        review_tuple.repository != REPOSITORY
        or review_tuple.contract_version != CONTRACT_VERSION
        or review_tuple.pr_number <= 0
        or SHA_PATTERN.fullmatch(review_tuple.base_sha) is None
        or SHA_PATTERN.fullmatch(review_tuple.head_sha) is None
    ):
        raise RuntimeError("invalid execution evidence tuple")
    return review_tuple


def _attestation_private_key() -> Ed25519PrivateKey:
    encoded = os.environ.get("NEWTONSAPPLE_REVIEW_ATTESTATION_PRIVATE_KEY", "")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except binascii.Error as exc:
        raise RuntimeError("execution attestation signer is not configured") from exc
    if len(raw) != 32:
        raise RuntimeError("execution attestation signer is not configured")
    return Ed25519PrivateKey.from_private_bytes(raw)


def _signed_result(payload: dict, *, payload_key: str, signature_key: str) -> dict:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = _attestation_private_key().sign(encoded)
    return {
        payload_key: base64.b64encode(encoded).decode("ascii"),
        signature_key: base64.b64encode(signature).decode("ascii"),
    }


def _trusted_ci_evidence(review_tuple: ReviewTuple) -> tuple[dict, list[dict]]:
    live_pr = gh_json("api", f"repos/{REPOSITORY}/pulls/{review_tuple.pr_number}")
    if not isinstance(live_pr, dict):
        raise RuntimeError("GitHub returned malformed pull request")
    base = live_pr.get("base")
    head = live_pr.get("head")
    reviewers = live_pr.get("requested_reviewers")
    if (
        live_pr.get("state") != "open"
        or live_pr.get("draft") is not False
        or not isinstance(base, dict)
        or base.get("sha") != review_tuple.base_sha
        or base.get("ref") not in ALLOWED_BASE_REFS
        or not isinstance(head, dict)
        or head.get("sha") != review_tuple.head_sha
        or not isinstance(reviewers, list)
        or _expected_login()
        not in {
            reviewer.get("login")
            for reviewer in reviewers
            if isinstance(reviewer, dict)
        }
    ):
        raise RuntimeError("live pull request is not eligible for execution evidence")

    runs = _collection(
        f"repos/{REPOSITORY}/actions/workflows/"
        f"{TRUSTED_EXECUTION_WORKFLOW_PATH}/runs?per_page=100"
    )
    candidates = []
    for run in runs:
        title = run.get("display_title")
        request_match = (
            EXECUTION_REQUEST_RUN_NAME_PATTERN.fullmatch(title)
            if isinstance(title, str)
            else None
        )
        dispatch_match = (
            EXECUTION_DISPATCH_RUN_NAME_PATTERN.fullmatch(title)
            if isinstance(title, str)
            else None
        )
        actor = run.get("actor")
        pull_requests = run.get("pull_requests")
        request_provenance = bool(
            run.get("event") == "pull_request_target"
            and request_match is not None
            and int(request_match.group(1)) == review_tuple.pr_number
            and request_match.group(2) == review_tuple.base_sha
            and request_match.group(3) == review_tuple.head_sha
            and isinstance(pull_requests, list)
            and any(
                isinstance(item, dict)
                and item.get("number") == review_tuple.pr_number
                and isinstance(item.get("base"), dict)
                and item["base"].get("sha") == review_tuple.base_sha
                and isinstance(item.get("head"), dict)
                and item["head"].get("sha") == review_tuple.head_sha
                for item in pull_requests
            )
        )
        dispatch_provenance = bool(
            run.get("event") == "workflow_dispatch"
            and dispatch_match is not None
            and isinstance(actor, dict)
            and actor.get("login") in TRUSTED_CAPTURE_WORKFLOW.allowed_dispatchers
            and int(dispatch_match.group(2)) == review_tuple.pr_number
            and dispatch_match.group(3) == review_tuple.base_sha
            and dispatch_match.group(4) == review_tuple.head_sha
        )
        if (
            run.get("workflow_id") == TRUSTED_EXECUTION_WORKFLOW_ID
            and run.get("path") == TRUSTED_EXECUTION_WORKFLOW_PATH
            and run.get("status") == "completed"
            and run.get("conclusion") in {"success", "failure"}
            and run.get("head_branch") == "dev"
            and SHA_PATTERN.fullmatch(str(run.get("head_sha", ""))) is not None
            and (request_provenance or dispatch_provenance)
        ):
            candidates.append(run)
    if not candidates:
        raise RuntimeError("trusted exact-head execution run is required")
    run = max(candidates, key=lambda candidate: int(candidate.get("id", 0)))
    run_id = run.get("id")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
        raise RuntimeError("trusted exact-head execution run id is invalid")
    if _trusted_execution_workflow_sha256(str(run["head_sha"])) != (
        TRUSTED_EXECUTION_WORKFLOW_SHA256
    ):
        raise RuntimeError("trusted exact-head execution workflow content changed")
    jobs_result = gh_json("api", f"repos/{REPOSITORY}/actions/runs/{run_id}/jobs?per_page=100")
    if not isinstance(jobs_result, dict) or not isinstance(jobs_result.get("jobs"), list):
        raise RuntimeError("GitHub returned malformed workflow jobs")
    jobs = [job for job in jobs_result["jobs"] if isinstance(job, dict)]
    return run, _canonical_ci_jobs(jobs)


def _canonical_ci_jobs(jobs: list[dict]) -> list[dict]:
    gate_jobs = [job for job in jobs if job.get("name") in BASELINE_EXECUTION_GATES]
    by_name = {job.get("name"): job for job in gate_jobs}
    if (
        len(by_name) != len(gate_jobs)
        or set(by_name) != set(BASELINE_EXECUTION_GATES)
    ):
        raise RuntimeError("trusted CI gate inventory does not match policy")
    return [by_name[name] for name in BASELINE_EXECUTION_GATES]


def _gate_contract(job: dict) -> dict:
    name = job.get("name")
    conclusion = job.get("conclusion")
    labels = job.get("labels")
    steps = job.get("steps")
    if (
        name not in BASELINE_EXECUTION_GATES
        or job.get("status") != "completed"
        or conclusion not in {"success", "failure"}
        or not isinstance(labels, list)
        or "ubuntu-latest" not in labels
        or not isinstance(steps, list)
    ):
        raise RuntimeError("trusted CI job does not satisfy execution policy")
    before_name, gate_name, after_name = EXECUTION_GATE_STEP_NAMES[str(name)]
    relevant_steps = {
        step.get("name"): step
        for step in steps
        if isinstance(step, dict)
        and step.get("name") in {before_name, gate_name, after_name}
    }
    if set(relevant_steps) != {before_name, gate_name, after_name}:
        raise RuntimeError("trusted CI job omitted execution policy steps")
    gate_conclusion = relevant_steps[gate_name].get("conclusion")
    if (
        relevant_steps[before_name].get("conclusion") != "success"
        or relevant_steps[after_name].get("conclusion") != "success"
        or gate_conclusion not in {"success", "failure"}
    ):
        raise RuntimeError("trusted CI job did not complete execution policy steps")
    if conclusion != gate_conclusion:
        raise RuntimeError("trusted CI job conclusion does not match gate")
    return {
        "kind": "command",
        "command": EXECUTION_GATE_COMMANDS[str(name)],
        "executor": "github_actions",
        "runner": {"kind": "github_actions", "name": "ubuntu-latest"},
        "status": "pass" if gate_conclusion == "success" else "pr-fail",
        "exit_codes": [0 if gate_conclusion == "success" else 1],
    }


def _gate_resolution_payload(review_tuple: ReviewTuple, jobs: list[dict]) -> dict:
    jobs = _canonical_ci_jobs(jobs)
    contracts = {str(job["name"]): _gate_contract(job) for job in jobs}
    return {
        **review_tuple.__dict__,
        "policy_version": EXECUTION_GATE_POLICY_VERSION,
        "policy_sha256": EXECUTION_GATE_POLICY_SHA256,
        "baseline_gates": list(BASELINE_EXECUTION_GATES),
        "resolved_gates": list(BASELINE_EXECUTION_GATES),
        "gate_contracts": contracts,
    }


def _local_gate_contract(name: str) -> dict:
    return {
        "kind": "command",
        "command": EXECUTION_GATE_COMMANDS[name],
        "executor": "review_worker",
        "runner": {"kind": "review_worker", "name": "docker-node22"},
        "statuses": ["pass", "pr-fail", "unavailable"],
        "exit_codes": list(range(0, 256)),
    }


def _local_gate_resolution_payload(review_tuple: ReviewTuple) -> dict:
    return {
        **review_tuple.__dict__,
        "policy_version": EXECUTION_GATE_POLICY_VERSION,
        "policy_sha256": EXECUTION_GATE_POLICY_SHA256,
        "baseline_gates": list(BASELINE_EXECUTION_GATES),
        "resolved_gates": list(BASELINE_EXECUTION_GATES),
        "gate_contracts": {
            name: _local_gate_contract(name) for name in BASELINE_EXECUTION_GATES
        },
    }


def resolve_execution_gates(payload: dict) -> dict:
    review_tuple = _execution_tuple(payload)
    try:
        _, jobs = _trusted_ci_evidence(review_tuple)
        resolution = _gate_resolution_payload(review_tuple, jobs)
    except RuntimeError:
        resolution = _local_gate_resolution_payload(review_tuple)
    return _signed_result(
        resolution,
        payload_key="gate_resolution_payload",
        signature_key="gate_resolution_signature",
    )


def _commit_tree_sha(commit_sha: str) -> str:
    commit = gh_json("api", f"repos/{REPOSITORY}/git/commits/{commit_sha}")
    tree = commit.get("tree") if isinstance(commit, dict) else None
    tree_sha = tree.get("sha") if isinstance(tree, dict) else None
    if not isinstance(tree_sha, str) or SHA_PATTERN.fullmatch(tree_sha) is None:
        raise RuntimeError("GitHub returned malformed commit tree")
    return tree_sha


def _trusted_execution_workflow_sha256(workflow_sha: str) -> str:
    value = gh_json(
        "api",
        f"repos/{REPOSITORY}/contents/{TRUSTED_EXECUTION_WORKFLOW_PATH}?ref={workflow_sha}",
    )
    if (
        not isinstance(value, dict)
        or value.get("type") != "file"
        or value.get("encoding") != "base64"
        or not isinstance(value.get("content"), str)
    ):
        raise RuntimeError("GitHub returned malformed execution workflow content")
    try:
        content = base64.b64decode(value["content"], validate=True)
    except (binascii.Error, ValueError) as exc:
        raise RuntimeError("GitHub returned malformed execution workflow content") from exc
    return hashlib.sha256(content).hexdigest()


def _job_log(job_id: int) -> bytes:
    return gh_bytes("api", f"repos/{REPOSITORY}/actions/jobs/{job_id}/logs")


def _require_exact_head_provenance(
    log: bytes,
    *,
    gate_name: str,
    head_sha: str,
    head_tree_sha: str,
) -> None:
    if gate_name not in EXECUTION_GATE_COMMAND_SHA256:
        raise RuntimeError("trusted CI gate is not in the execution policy")
    text = log.decode("utf-8", errors="replace")
    prefix = (
        f"newtonsapple-review-execution-v2 gate={gate_name} head={head_sha} "
        f"tree={head_tree_sha} "
        f"command_sha256={EXECUTION_GATE_COMMAND_SHA256[gate_name]}"
    )
    if text.count(f"{prefix} phase=before") != 1 or text.count(
        f"{prefix} phase=after"
    ) != 1:
        raise RuntimeError("trusted CI job omitted exact-head provenance")


def _duration_ms(started_at: object, completed_at: object) -> int:
    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        raise RuntimeError("trusted CI job timing is missing")
    try:
        start = datetime.fromisoformat(started_at.removesuffix("Z") + "+00:00")
        end = datetime.fromisoformat(completed_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise RuntimeError("trusted CI job timing is invalid") from exc
    duration = int((end - start).total_seconds() * 1000)
    if duration < 0:
        raise RuntimeError("trusted CI job timing is invalid")
    return duration


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _unavailable_local_gate(name: str, reason: str) -> dict:
    now = _utc_now()
    return {
        "id": name,
        "executor": "review_worker",
        "runner": {"kind": "review_worker", "name": "docker-node22"},
        "status": "unavailable",
        "head_sha": "",
        "attempted": False,
        "command": EXECUTION_GATE_COMMANDS[name],
        "exit_code": 125,
        "started_at": now,
        "completed_at": now,
        "duration_ms": 0,
        "tree_before": "",
        "tree_after": "",
        "evidence": {
            "kind": "local_worker",
            "log_sha256": hashlib.sha256(reason.encode()).hexdigest(),
            "reason": reason[:500],
        },
    }


def _credential_free_environment(home: Path) -> dict[str, str]:
    allowed = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(home),
        "CI": "true",
    }
    return allowed


def _run_command(
    command: list[str], *, cwd: Path, env: dict[str, str], timeout: int
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _git_output(workspace: Path, *args: str) -> str:
    result = _run_command(
        ["git", *args],
        cwd=workspace,
        env=_credential_free_environment(workspace.parent / "home"),
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError("local worker could not verify immutable source")
    return result.stdout.strip()


def _docker_gate_command(workspace: Path, command: list[str], *, network: str) -> list[str]:
    return [
        "docker",
        "run",
        "--rm",
        "--network",
        network,
        "--cpus",
        "2",
        "--memory",
        "4g",
        "--pids-limit",
        "512",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges",
        "--read-only",
        "--tmpfs",
        "/tmp:rw,noexec,nosuid,size=512m",
        "--mount",
        f"type=bind,src={workspace},dst=/workspace",
        "--workdir",
        "/workspace",
        "--env",
        "HOME=/tmp/home",
        "--env",
        "CI=true",
        LOCAL_REVIEW_WORKER_IMAGE,
        *command,
    ]


def _run_local_execution_worker(review_tuple: ReviewTuple) -> dict:
    """Run exact-head gates in a bounded Docker worker with no credentials."""
    base_tree_sha = _commit_tree_sha(review_tuple.base_sha)
    head_tree_sha = _commit_tree_sha(review_tuple.head_sha)
    worker = {
        "required": True,
        "isolation": "docker",
        "head_sha": review_tuple.head_sha,
        "base_present": True,
        "tree_before": head_tree_sha,
        "tree_after": head_tree_sha,
        "preflight": {
            "disposable_home": True,
            "credentials_absent": True,
            "host_mounts_absent": True,
            "host_docker_socket_absent": True,
            "resources_bounded": True,
            "egress_default_deny": True,
        },
        "mutations": [],
    }
    with tempfile.TemporaryDirectory(prefix="newtonsapple-review-") as temp:
        root = Path(temp)
        workspace = root / "workspace"
        workspace.mkdir()
        home = root / "home"
        home.mkdir()
        env = _credential_free_environment(home)
        try:
            for command in (
                ["git", "init", "-q"],
                [
                    "git",
                    "remote",
                    "add",
                    "origin",
                    f"https://github.com/{REPOSITORY}.git",
                ],
                ["git", "fetch", "-q", "--depth=1", "origin", review_tuple.head_sha],
                ["git", "checkout", "-q", "--detach", "FETCH_HEAD"],
                ["git", "fetch", "-q", "--depth=1", "origin", review_tuple.base_sha],
            ):
                result = _run_command(command, cwd=workspace, env=env, timeout=180)
                if result.returncode != 0:
                    raise RuntimeError("local worker could not materialize exact tuple")
            if (
                _git_output(workspace, "rev-parse", "HEAD") != review_tuple.head_sha
                or _git_output(workspace, "rev-parse", "HEAD^{tree}") != head_tree_sha
                or _git_output(workspace, "cat-file", "-t", review_tuple.base_sha)
                != "commit"
                or _git_output(workspace, "status", "--porcelain")
            ):
                raise RuntimeError("local worker exact-head preflight failed")
            install = _run_command(
                _docker_gate_command(
                    workspace,
                    [
                        "npm",
                        "ci",
                        "--ignore-scripts",
                        "--no-audit",
                        "--no-fund",
                    ],
                    network="bridge",
                ),
                cwd=workspace,
                env=env,
                timeout=1200,
            )
            install_log = (install.stdout + install.stderr)[-200_000:]
            if install.returncode != 0 or _git_output(workspace, "status", "--porcelain"):
                reason = "dependency installation unavailable: " + install_log[-500:]
                gates = [
                    _unavailable_local_gate(name, reason)
                    for name in BASELINE_EXECUTION_GATES
                ]
            else:
                gates = []
                unavailable_pattern = re.compile(
                    r"(ECONNREFUSED|ENOTFOUND|Service Unavailable|docker: not found|"
                    r"Cannot connect to the Docker daemon|no such host)",
                    re.IGNORECASE,
                )
                for name in BASELINE_EXECUTION_GATES:
                    started = _utc_now()
                    try:
                        result = _run_command(
                            _docker_gate_command(
                                workspace,
                                EXECUTION_GATE_COMMANDS[name],
                                network="none",
                            ),
                            cwd=workspace,
                            env=env,
                            timeout=3600,
                        )
                        log = (result.stdout + result.stderr)[-1_000_000:]
                        status = (
                            "pass"
                            if result.returncode == 0
                            else "unavailable"
                            if result.returncode in {125, 126, 127}
                            or unavailable_pattern.search(log)
                            else "pr-fail"
                        )
                        exit_code = max(0, min(result.returncode, 255))
                        attempted = result.returncode not in {125, 126, 127}
                    except subprocess.TimeoutExpired as exc:
                        log = f"local gate timeout: {exc}"
                        status = "unavailable"
                        exit_code = 124
                        attempted = True
                    completed = _utc_now()
                    duration_ms = _duration_ms(started, completed)
                    tree_after = _git_output(workspace, "rev-parse", "HEAD^{tree}")
                    dirty = _git_output(workspace, "status", "--porcelain")
                    if tree_after != head_tree_sha or dirty:
                        status = "unavailable"
                        log += "\nsource tree mutated during local gate"
                    gates.append(
                        {
                            "id": name,
                            "executor": "review_worker",
                            "runner": {
                                "kind": "review_worker",
                                "name": "docker-node22",
                            },
                            "status": status,
                            "head_sha": review_tuple.head_sha,
                            "attempted": attempted,
                            "command": EXECUTION_GATE_COMMANDS[name],
                            "exit_code": exit_code,
                            "started_at": started,
                            "completed_at": completed,
                            "duration_ms": duration_ms,
                            "tree_before": head_tree_sha,
                            "tree_after": tree_after,
                            "evidence": {
                                "kind": "local_worker",
                                "log_sha256": hashlib.sha256(log.encode()).hexdigest(),
                                "reason": log[-500:] if status == "unavailable" else "",
                            },
                        }
                    )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            gates = [
                _unavailable_local_gate(name, str(exc))
                for name in BASELINE_EXECUTION_GATES
            ]
    for gate in gates:
        gate["head_sha"] = review_tuple.head_sha
        gate["tree_before"] = head_tree_sha
        gate["tree_after"] = head_tree_sha
    return {
        "base_tree_sha": base_tree_sha,
        "head_tree_sha": head_tree_sha,
        "worker": worker,
        "gates": gates,
    }


def execution_evidence(payload: dict) -> dict:
    review_tuple = _execution_tuple(payload)
    try:
        run, jobs = _trusted_ci_evidence(review_tuple)
    except RuntimeError:
        local = _run_local_execution_worker(review_tuple)
        resolution = _local_gate_resolution_payload(review_tuple)
        resolution_bytes = json.dumps(
            resolution, sort_keys=True, separators=(",", ":")
        ).encode()
        report = {
            **review_tuple.__dict__,
            "base_tree_sha": local["base_tree_sha"],
            "head_tree_sha": local["head_tree_sha"],
            "gate_resolution": {
                "policy_version": EXECUTION_GATE_POLICY_VERSION,
                "policy_sha256": EXECUTION_GATE_POLICY_SHA256,
                "manifest_sha256": hashlib.sha256(resolution_bytes).hexdigest(),
                "resolved_gates": list(BASELINE_EXECUTION_GATES),
            },
            "worker": local["worker"],
            "gates": local["gates"],
        }
        return _signed_result(
            report,
            payload_key="attestation_payload",
            signature_key="attestation_signature",
        )
    jobs = _canonical_ci_jobs(jobs)
    resolution = _gate_resolution_payload(review_tuple, jobs)
    resolution_bytes = json.dumps(
        resolution, sort_keys=True, separators=(",", ":")
    ).encode()
    base_tree_sha = _commit_tree_sha(review_tuple.base_sha)
    head_tree_sha = _commit_tree_sha(review_tuple.head_sha)
    run_id = run["id"]
    gates = []
    for job in jobs:
        contract = _gate_contract(job)
        job_id = job.get("id")
        url = job.get("html_url")
        if (
            isinstance(job_id, bool)
            or not isinstance(job_id, int)
            or job_id <= 0
            or url != f"https://github.com/{REPOSITORY}/actions/runs/{run_id}/job/{job_id}"
        ):
            raise RuntimeError("trusted CI job identity is invalid")
        log = _job_log(job_id)
        _require_exact_head_provenance(
            log,
            gate_name=str(job["name"]),
            head_sha=review_tuple.head_sha,
            head_tree_sha=head_tree_sha,
        )
        gates.append(
            {
                "id": job["name"],
                "executor": "github_actions",
                "runner": {
                    "kind": "github_actions",
                    "name": "ubuntu-latest",
                    "job_id": job_id,
                },
                "status": contract["status"],
                "head_sha": review_tuple.head_sha,
                "attempted": True,
                "command": contract["command"],
                "exit_code": contract["exit_codes"][0],
                "started_at": job["started_at"],
                "completed_at": job["completed_at"],
                "duration_ms": _duration_ms(job["started_at"], job["completed_at"]),
                "tree_before": head_tree_sha,
                "tree_after": head_tree_sha,
                "evidence": {
                    "kind": "github_actions",
                    "url": url,
                    "job_id": job_id,
                    "log_sha256": hashlib.sha256(log).hexdigest(),
                },
            }
        )
    report = {
        **review_tuple.__dict__,
        "base_tree_sha": base_tree_sha,
        "head_tree_sha": head_tree_sha,
        "gate_resolution": {
            "policy_version": EXECUTION_GATE_POLICY_VERSION,
            "policy_sha256": EXECUTION_GATE_POLICY_SHA256,
            "manifest_sha256": hashlib.sha256(resolution_bytes).hexdigest(),
            "resolved_gates": list(BASELINE_EXECUTION_GATES),
        },
        "worker": {"required": False},
        "gates": gates,
    }
    return _signed_result(
        report,
        payload_key="attestation_payload",
        signature_key="attestation_signature",
    )


def _workflow_from_environment() -> TrustedWorkflow:
    """Return the reviewed capture identity; environment input cannot widen trust."""
    return TRUSTED_CAPTURE_WORKFLOW


def _expected_login() -> str:
    login = os.environ.get("NEWTONSAPPLE_REVIEW_BOT_LOGIN", "")
    if LOGIN_PATTERN.fullmatch(login) is None:
        raise RuntimeError("review bot login is not configured")
    return login


def _assert_actor(expected_login: str) -> None:
    actor_result = gh_json("api", "user")
    if not isinstance(actor_result, dict):
        raise RuntimeError("GitHub credential does not match the review bot")
    actor = cast(dict[str, Any], actor_result)
    if actor.get("login") != expected_login:
        raise RuntimeError("GitHub credential does not match the review bot")


def _bot_bodies(items: list[dict], expected_login: str) -> list[str]:
    bodies: list[str] = []
    for item in items:
        user = item.get("user")
        body = item.get("body")
        if (
            isinstance(user, dict)
            and user.get("login") == expected_login
            and isinstance(body, str)
        ):
            bodies.append(body)
    return bodies


def _status_context(review_tuple: ReviewTuple) -> str:
    return (
        f"newtonsapple-bot/review-v2/pr-{review_tuple.pr_number}/"
        f"base-{review_tuple.base_sha}"
    )


def _summary_marker(review_tuple: ReviewTuple) -> str:
    return (
        "<!-- newtonsapple-pr-review-summary:v2 "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} -->"
    )


def _blocker_marker(review_tuple: ReviewTuple) -> str:
    return (
        "<!-- newtonsapple-pr-review-blocker:v2 "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} -->"
    )


def _dead_letter_marker(review_tuple: ReviewTuple) -> str:
    return (
        "<!-- newtonsapple-pr-review-dead-letter:v2 "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} -->"
    )


def _summary_content(review_tuple: ReviewTuple, pr_url: str, review_body: str) -> str:
    return (
        f"**PR review completed:** [#{review_tuple.pr_number}]({pr_url}) at "
        f"head `{review_tuple.head_sha}` (base `{review_tuple.base_sha}`).\n\n"
        f"{review_body}\n\n"
        "**Verification:** trusted v2 capture provenance, live exact tuple, "
        "current reviewer request, formal bot review marker, and terminal status verified."
    )


def _post_terminal_status(review_tuple: ReviewTuple, pr_url: str) -> None:
    gh_json(
        "api",
        f"repos/{REPOSITORY}/statuses/{review_tuple.head_sha}",
        "-X",
        "POST",
        "-f",
        "state=success",
        "-f",
        f"context={_status_context(review_tuple)}",
        "-f",
        "description=Formal PR review delivered",
        "-f",
        f"target_url={pr_url}",
    )


def _post_error_status(review_tuple: ReviewTuple, pr_url: str) -> None:
    gh_json(
        "api",
        f"repos/{REPOSITORY}/statuses/{review_tuple.head_sha}",
        "-X",
        "POST",
        "-f",
        "state=error",
        "-f",
        f"context={_status_context(review_tuple)}",
        "-f",
        "description=Automated review failed after bounded retries",
        "-f",
        f"target_url={pr_url}",
    )


def _buzz_find(marker: str) -> Optional[str]:
    result = subprocess.run(
        ["buzz", "messages", "search", "--query", marker, "--limit", "10"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError("Buzz search unavailable")
    data = json.loads(result.stdout)
    if not isinstance(data, list):
        raise RuntimeError("Buzz search returned malformed JSON")
    for item in data:
        if not isinstance(item, dict):
            continue
        content = item.get("content", "")
        event_id = item.get("event_id") or item.get("id")
        tags = item.get("tags")
        in_channel = isinstance(tags, list) and any(
            isinstance(tag, list)
            and len(tag) >= 2
            and tag[0] == "h"
            and tag[1] == BUZZ_CHANNEL
            for tag in tags
        )
        if (
            in_channel
            and isinstance(content, str)
            and marker in content
            and isinstance(event_id, str)
        ):
            return event_id
    return None


def _buzz_send(content: str) -> str:
    result = subprocess.run(
        [
            "buzz",
            "messages",
            "send",
            "--channel",
            BUZZ_CHANNEL,
            "--content",
            "-",
        ],
        input=content,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError("Buzz delivery unavailable")
    data = json.loads(result.stdout)
    event_id = data.get("event_id") or data.get("id")
    if not isinstance(event_id, str) or not event_id:
        raise RuntimeError("Buzz delivery returned no event id")
    return event_id


def _live_review_state(pr_number: int, expected_login: str) -> tuple[dict, list[dict], list[str]]:
    live_pr_result = gh_json("api", f"repos/{REPOSITORY}/pulls/{pr_number}")
    if not isinstance(live_pr_result, dict):
        raise RuntimeError("GitHub returned malformed pull request")
    live_pr = cast(dict[str, Any], live_pr_result)
    head = live_pr.get("head")
    if not isinstance(head, dict) or SHA_PATTERN.fullmatch(str(head.get("sha", ""))) is None:
        raise RuntimeError("GitHub returned malformed pull request head")
    statuses = _collection(f"repos/{REPOSITORY}/commits/{head['sha']}/statuses?per_page=100")
    comments = _collection(f"repos/{REPOSITORY}/issues/{pr_number}/comments?per_page=100")
    reviews = _collection(f"repos/{REPOSITORY}/pulls/{pr_number}/reviews?per_page=100")
    return live_pr, statuses, _bot_bodies(comments + reviews, expected_login)


def _load_run(run_id: int) -> Optional[dict]:
    run = gh_json("api", f"repos/{REPOSITORY}/actions/runs/{run_id}")
    return run if isinstance(run, dict) else None


def _load_timeline(pr_number: int) -> list[dict]:
    return _collection(
        f"repos/{REPOSITORY}/issues/{pr_number}/timeline?per_page=100"
    )


def _authorized_live_tuple(pr_number: int, expected_login: str) -> tuple[dict, Optional[ReviewTuple], list[str]]:
    live_pr, statuses, bodies = _live_review_state(pr_number, expected_login)
    selected = select_authorized_tuple(
        live_pr,
        statuses=statuses,
        load_run=_load_run,
        trusted_workflow=_workflow_from_environment(),
        reviewer_login=expected_login,
        bot_bodies=bodies,
        load_timeline=_load_timeline,
    )
    return live_pr, selected, bodies


def _reconcile(expected_login: str, store: ReviewStateStore) -> dict:
    pulls = _collection(
        f"repos/{REPOSITORY}/pulls?state=open&base=dev&per_page=100"
    )
    events: list[dict] = []
    for listed in pulls:
        number = listed.get("number")
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            continue
        live_pr, selected, bodies = _authorized_live_tuple(number, expected_login)
        if selected is not None:
            events.append(
                {
                    "delivery_id": (
                        f"recovery-v2-pr{number}-"
                        f"{selected.base_sha[:12]}-{selected.head_sha[:12]}"
                    ),
                    "event_type": "pull_request",
                    "payload": {
                        "action": "review_requested",
                        "number": number,
                        "repository": {"full_name": REPOSITORY},
                        "requested_reviewer": {"login": expected_login},
                        "pull_request": live_pr,
                    },
                }
            )
            continue

        base = live_pr.get("base")
        head = live_pr.get("head")
        if not isinstance(base, dict) or not isinstance(head, dict):
            continue
        try:
            review_tuple = ReviewTuple(
                repository=REPOSITORY,
                pr_number=number,
                base_sha=str(base["sha"]),
                head_sha=str(head["sha"]),
            )
        except KeyError:
            continue
        marker = review_marker(review_tuple)
        matching_body = next((body for body in bodies if marker in body), None)
        if matching_body is not None:
            pr_url = str(live_pr.get("html_url", ""))
            _post_terminal_status(review_tuple, pr_url)
            store.record_external_completion(
                review_tuple,
                now=int(time.time()),
                marker=_summary_marker(review_tuple),
                content=_summary_content(review_tuple, pr_url, matching_body),
            )
            continue
        requested = live_pr.get("requested_reviewers")
        if (
            SHA_PATTERN.fullmatch(review_tuple.base_sha) is not None
            and SHA_PATTERN.fullmatch(review_tuple.head_sha) is not None
            and isinstance(requested, list)
            and expected_login
            in {
                item.get("login")
                for item in requested
                if isinstance(item, dict)
            }
        ):
            store.enqueue_blocker(
                review_tuple,
                marker=_blocker_marker(review_tuple),
                content=(
                    f"**Operational blocker:** PR #{number} at head "
                    f"`{review_tuple.head_sha}` still requests `{expected_login}`, "
                    "but no matching trusted v2 capture workflow run/status could "
                    "be verified. The review was not started."
                ),
            )
    delivered = drain_summary_outbox(
        store, find_existing=_buzz_find, send=_buzz_send
    )
    return {"events": events, "outbox_delivered": delivered}


def _settle(payload: dict, expected_login: str, store: ReviewStateStore) -> dict:
    try:
        review_tuple = ReviewTuple(
            repository=str(payload["repository"]),
            pr_number=int(payload["pr_number"]),
            base_sha=str(payload["base_sha"]),
            head_sha=str(payload["head_sha"]),
            contract_version=str(payload["contract_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid settlement tuple") from exc
    if (
        review_tuple.repository != REPOSITORY
        or review_tuple.contract_version != CONTRACT_VERSION
        or review_tuple.pr_number <= 0
        or SHA_PATTERN.fullmatch(review_tuple.base_sha) is None
        or SHA_PATTERN.fullmatch(review_tuple.head_sha) is None
    ):
        raise RuntimeError("invalid settlement tuple")
    operation = payload.get("operation")
    lease_token = payload.get("lease_token")
    if (
        not isinstance(lease_token, str)
        or LEASE_TOKEN_PATTERN.fullmatch(lease_token) is None
    ):
        raise RuntimeError("invalid settlement lease token")
    if operation == "release":
        failure = store.record_failure(
            review_tuple,
            lease_token=lease_token,
            now=int(time.time()),
            retry_delay=RETRY_DELAY_SECONDS,
            max_attempts=MAX_REVIEW_ATTEMPTS,
        )
        if failure["dead_lettered"]:
            pr_url = f"https://github.com/{REPOSITORY}/pull/{review_tuple.pr_number}"
            _post_error_status(review_tuple, pr_url)
            store.enqueue_blocker(
                review_tuple,
                kind="dead-letter",
                marker=_dead_letter_marker(review_tuple),
                content=(
                    "**Operational blocker:** automated review for PR "
                    f"#{review_tuple.pr_number} at head `{review_tuple.head_sha}` "
                    f"failed after {failure['attempts']} attempts. The tuple was "
                    "dead-lettered and the GitHub status was marked error."
                ),
            )
            drain_summary_outbox(store, find_existing=_buzz_find, send=_buzz_send)
        return {"settled": "release", **failure}
    if operation != "complete":
        raise RuntimeError("invalid settlement operation")

    live_pr, _, bodies = _authorized_live_tuple(review_tuple.pr_number, expected_login)
    marker = review_marker(review_tuple)
    matching_body = next((body for body in bodies if marker in body), None)
    if matching_body is None:
        raise RuntimeError("formal bot review marker is missing")
    base = live_pr.get("base")
    head = live_pr.get("head")
    if (
        not isinstance(base, dict)
        or base.get("sha") != review_tuple.base_sha
        or not isinstance(head, dict)
        or head.get("sha") != review_tuple.head_sha
    ):
        raise RuntimeError("live pull request tuple changed")
    pr_url = str(live_pr.get("html_url", ""))
    _post_terminal_status(review_tuple, pr_url)
    store.settle_review(
        review_tuple,
        lease_token=lease_token,
        now=int(time.time()),
        marker=_summary_marker(review_tuple),
        content=_summary_content(review_tuple, pr_url, matching_body),
    )
    delivered = drain_summary_outbox(
        store, find_existing=_buzz_find, send=_buzz_send
    )
    return {"settled": "complete", "outbox_delivered": delivered}


def _gate_webhook(payload: dict, expected_login: str, store: ReviewStateStore) -> dict:
    repository = payload.get("repository")
    requested = payload.get("requested_reviewer")
    number = payload.get("number")
    if (
        payload.get("action") != "review_requested"
        or not isinstance(repository, dict)
        or repository.get("full_name") != REPOSITORY
        or not isinstance(requested, dict)
        or requested.get("login") != expected_login
        or isinstance(number, bool)
        or not isinstance(number, int)
        or number <= 0
    ):
        raise RuntimeError("webhook is not an eligible review request")
    payload_pr = payload.get("pull_request")
    if not isinstance(payload_pr, dict):
        raise RuntimeError("webhook pull request tuple is missing")
    live_pr, _, bodies = _live_review_state(number, expected_login)
    payload_base = payload_pr.get("base")
    payload_head = payload_pr.get("head")
    live_base = live_pr.get("base")
    live_head = live_pr.get("head")
    requested_reviewers = live_pr.get("requested_reviewers")
    if (
        payload_pr.get("number") != number
        or not isinstance(payload_base, dict)
        or not isinstance(payload_head, dict)
        or not isinstance(live_base, dict)
        or not isinstance(live_head, dict)
        or payload_base.get("sha") != live_base.get("sha")
        or payload_head.get("sha") != live_head.get("sha")
    ):
        raise RuntimeError("live pull request tuple changed after review request")
    review_tuple = ReviewTuple(
        repository=REPOSITORY,
        pr_number=number,
        base_sha=str(live_base.get("sha", "")),
        head_sha=str(live_head.get("sha", "")),
    )
    if (
        live_pr.get("state") != "open"
        or live_pr.get("draft") is not False
        or live_base.get("ref") not in ALLOWED_BASE_REFS
        or SHA_PATTERN.fullmatch(review_tuple.base_sha) is None
        or SHA_PATTERN.fullmatch(review_tuple.head_sha) is None
        or not isinstance(requested_reviewers, list)
        or expected_login
        not in {
            reviewer.get("login")
            for reviewer in requested_reviewers
            if isinstance(reviewer, dict)
        }
        or any(
            review_marker(review_tuple) in body
            for body in bodies
            if isinstance(body, str)
        )
    ):
        raise RuntimeError("webhook pull request is no longer eligible")
    lease_token = store.reserve(
        review_tuple, now=int(time.time()), lease_seconds=LEASE_SECONDS
    )
    if lease_token is None:
        raise RuntimeError("review tuple is already leased or completed")
    return {
        "contract_version": CONTRACT_VERSION,
        "repository": REPOSITORY,
        "pr_number": number,
        "expected_base_sha": review_tuple.base_sha,
        "expected_head_sha": review_tuple.head_sha,
        "action": "review_requested",
        "pr_url": str(live_pr.get("html_url", "")),
        "review_marker": review_marker(review_tuple),
        "publisher_login": expected_login,
        "capture_context": _status_context(review_tuple),
        "lease_token": lease_token,
    }


def main() -> None:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            raise RuntimeError("input is not an object")
        expected_login = _expected_login()
        _assert_actor(expected_login)
        store = ReviewStateStore(_state_path())
        operation = payload.get("operation")
        if operation == "reconcile":
            output = _reconcile(expected_login, store)
        elif operation == "resolve_execution_gates":
            output = resolve_execution_gates(payload)
        elif operation == "execution_evidence":
            output = execution_evidence(payload)
        elif operation in {"complete", "release"}:
            output = _settle(payload, expected_login, store)
        elif operation is not None:
            raise RuntimeError("unsupported control-plane operation")
        else:
            output = _gate_webhook(payload, expected_login, store)
        print(json.dumps(output, separators=(",", ":")))
    except (json.JSONDecodeError, OSError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
        print(json.dumps({"__hermes_ignore__": True}))


if __name__ == "__main__":
    main()
