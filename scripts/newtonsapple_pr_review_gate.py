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
BASE_BRANCH = "dev"
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
LOGIN_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$")
LEASE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
LEASE_SECONDS = 2 * 60 * 60
RETRY_DELAY_SECONDS = 5 * 60
MAX_REVIEW_ATTEMPTS = 3
BUZZ_CHANNEL = "b1cb95c9-6a36-4516-abdd-81d853a9412e"
FAILURE_REASONS = {
    "review_evidence_incomplete": "immutable review evidence was incomplete or out of scope",
    "execution_evidence_incomplete": "execution evidence was incomplete or out of scope",
    "live_tuple_changed": "the live pull-request tuple changed before publication",
    "publication_failed": "GitHub did not accept or confirm the formal review",
    "processing_failed": "the review run ended before formal publication",
}
BASELINE_EXECUTION_GATES = ("quality", "integration", "e2e")
EXECUTION_GATE_COMMANDS = {
    "quality": ["npm", "run", "check"],
    "integration": ["npm", "run", "db:verify"],
    "e2e": ["npm", "run", "test:e2e:all:ci"],
}
LOCAL_REVIEW_WORKER_IMAGE = (
    "node@sha256:0557ac14e0d45d02ed563067b82856ca5e7aa3437fa28d98d4350ea9c3d9494a"
)
EXECUTION_GATE_POLICY_VERSION = "newtonsapple-v1"
_EXECUTION_GATE_POLICY = {
    "version": EXECUTION_GATE_POLICY_VERSION,
    "repository": REPOSITORY,
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
    request_id: int = 1
    contract_version: str = CONTRACT_VERSION


def tuple_key(review_tuple: ReviewTuple) -> str:
    return (
        f"{review_tuple.contract_version}:{review_tuple.repository}:"
        f"{review_tuple.pr_number}:{review_tuple.base_sha}:{review_tuple.head_sha}:"
        f"{review_tuple.request_id}"
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
                "lease_until INTEGER NOT NULL, publication_claimed_at INTEGER)"
            )
            lease_columns = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(leases)").fetchall()
            }
            if "lease_token" not in lease_columns:
                connection.execute("ALTER TABLE leases ADD COLUMN lease_token TEXT")
                connection.execute("DELETE FROM leases")
            if "publication_claimed_at" not in lease_columns:
                connection.execute(
                    "ALTER TABLE leases ADD COLUMN publication_claimed_at INTEGER"
                )
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
                "claim_token TEXT, claim_until INTEGER, retry_after INTEGER, "
                "reply_to TEXT)"
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
                ("reply_to", "TEXT"),
            ):
                if column not in outbox_columns:
                    connection.execute(
                        f"ALTER TABLE summary_outbox ADD COLUMN {column} {column_type}"
                    )
            connection.execute(
                "CREATE TABLE IF NOT EXISTS review_threads ("
                "key TEXT PRIMARY KEY, requested_event_id TEXT NOT NULL, "
                "started_event_id TEXT)"
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

    def record_requested_event(
        self, review_tuple: ReviewTuple, event_id: str
    ) -> str:
        if not event_id:
            raise ValueError("missing requested event id")
        key = tuple_key(review_tuple)
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO review_threads(key, requested_event_id) "
                "VALUES (?, ?)",
                (key, event_id),
            )
            row = connection.execute(
                "SELECT requested_event_id FROM review_threads WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None:
            raise ValueError("review thread was not recorded")
        return str(row[0])

    def requested_event_id(self, review_tuple: ReviewTuple) -> Optional[str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT requested_event_id FROM review_threads WHERE key = ?",
                (tuple_key(review_tuple),),
            ).fetchone()
        return None if row is None else str(row[0])

    def record_started_event(
        self, review_tuple: ReviewTuple, event_id: str
    ) -> str:
        if not event_id:
            raise ValueError("missing started event id")
        key = tuple_key(review_tuple)
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE review_threads SET started_event_id = "
                "COALESCE(started_event_id, ?) WHERE key = ?",
                (event_id, key),
            )
            if cursor.rowcount != 1:
                raise ValueError("review thread root is missing")
            row = connection.execute(
                "SELECT started_event_id FROM review_threads WHERE key = ?",
                (key,),
            ).fetchone()
        if row is None or row[0] is None:
            raise ValueError("review start was not recorded")
        return str(row[0])

    def started_event_id(self, review_tuple: ReviewTuple) -> Optional[str]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT started_event_id FROM review_threads WHERE key = ?",
                (tuple_key(review_tuple),),
            ).fetchone()
        return None if row is None or row[0] is None else str(row[0])

    def active_lease(
        self, review_tuple: ReviewTuple, *, lease_token: str, now: int
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM leases WHERE key = ? AND lease_token = ? "
                "AND lease_until > ?",
                (tuple_key(review_tuple), lease_token, now),
            ).fetchone()
        return row is not None

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

    def claim_publication(
        self,
        review_tuple: ReviewTuple,
        *,
        lease_token: str,
        now: int,
        lease_seconds: int,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("invalid publication lease")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE leases SET lease_until = ?, publication_claimed_at = ? "
                "WHERE key = ? AND lease_token = ? AND lease_until > ? "
                "AND publication_claimed_at IS NULL",
                (
                    now + lease_seconds,
                    now,
                    tuple_key(review_tuple),
                    lease_token,
                    now,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("review publication claim not found")

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
        dead_letter_marker: str,
        dead_letter_content: str,
        failure_reason: str = "the review run ended before formal publication",
    ) -> dict:
        if (
            retry_delay <= 0
            or max_attempts <= 0
            or not dead_letter_marker
            or not dead_letter_content
            or not failure_reason
        ):
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
            thread = connection.execute(
                "SELECT requested_event_id FROM review_threads WHERE key = ?",
                (key,),
            ).fetchone()
            reply_to = None if thread is None else str(thread[0])
            if dead_lettered:
                connection.execute(
                    "INSERT OR IGNORE INTO summary_outbox"
                    "(key, marker, content, reply_to) VALUES (?, ?, ?, ?)",
                    (
                        f"blocker:dead-letter:{key}",
                        dead_letter_marker,
                        dead_letter_content,
                        reply_to,
                    ),
                )
            else:
                connection.execute(
                    "INSERT OR IGNORE INTO summary_outbox"
                    "(key, marker, content, reply_to) VALUES (?, ?, ?, ?)",
                    (
                        f"retry:{failures}:{key}",
                        _retry_marker(review_tuple, failures),
                        _retry_content(
                            review_tuple,
                            attempt=failures,
                            max_attempts=max_attempts,
                            retry_after=retry_after,
                            failure_reason=failure_reason,
                        ),
                        reply_to,
                    ),
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
        reply_to: Optional[str] = None,
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
                "INSERT OR IGNORE INTO summary_outbox"
                "(key, marker, content, reply_to) VALUES (?, ?, ?, ?)",
                (key, marker, content, reply_to),
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
        reply_to: Optional[str] = None,
    ) -> int:
        """Atomically settle the current lease and queue its Buzz summary."""
        key = tuple_key(review_tuple)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            deleted = connection.execute(
                "DELETE FROM leases WHERE key = ? AND lease_token = ? "
                "AND publication_claimed_at IS NOT NULL",
                (key, lease_token),
            )
            if deleted.rowcount != 1:
                raise ValueError("claimed review lease not found")
            connection.execute(
                "INSERT OR REPLACE INTO completions(key, completed_at) VALUES (?, ?)",
                (key, now),
            )
            connection.execute("DELETE FROM attempts WHERE key = ?", (key,))
            connection.execute(
                "INSERT OR IGNORE INTO summary_outbox"
                "(key, marker, content, reply_to) VALUES (?, ?, ?, ?)",
                (key, marker, content, reply_to),
            )
            row = connection.execute(
                "SELECT id FROM summary_outbox WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            raise ValueError("summary outbox tuple conflict")
        return int(row[0])

    def enqueue_summary(
        self,
        review_tuple: ReviewTuple,
        *,
        marker: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> int:
        return self._enqueue_outbox(
            tuple_key(review_tuple),
            marker=marker,
            content=content,
            reply_to=reply_to,
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
            reply_to=self.requested_event_id(review_tuple),
        )

    def _enqueue_outbox(
        self,
        key: str,
        *,
        marker: str,
        content: str,
        reply_to: Optional[str] = None,
    ) -> int:
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO summary_outbox"
                "(key, marker, content, reply_to) VALUES (?, ?, ?, ?)",
                (key, marker, content, reply_to),
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
                "SELECT id, key, marker, content, reply_to FROM summary_outbox "
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
            "reply_to": row[4],
            "claim_token": claim_token,
        }

    def renew_summary_claim(
        self,
        outbox_id: int,
        *,
        claim_token: str,
        now: int,
        lease_seconds: int,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("invalid outbox lease")
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE summary_outbox SET claim_until = ? "
                "WHERE id = ? AND claim_token = ? AND sent_event_id IS NULL "
                "AND claim_until > ?",
                (now + lease_seconds, outbox_id, claim_token, now),
            )
            if cursor.rowcount != 1:
                raise ValueError("outbox claim not found")

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
    find_existing: Callable[[str, Optional[str]], Optional[str]],
    send: Callable[[str, Optional[str]], str],
) -> int:
    """Deliver pending summaries effectively once, preserving failures."""
    processed = 0
    while True:
        now = int(time.time())
        item = store.claim_summary(now=now, lease_seconds=5 * 60)
        if item is None:
            break
        try:
            event_id = find_existing(item["marker"], item["reply_to"])
            if event_id is None:
                store.renew_summary_claim(
                    item["id"],
                    claim_token=item["claim_token"],
                    now=int(time.time()),
                    lease_seconds=5 * 60,
                )
                event_id = send(
                    f'{item["content"]}\n\n{item["marker"]}',
                    item["reply_to"],
                )
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


def review_marker(review_tuple: ReviewTuple) -> str:
    return (
        f"<!-- newtonsapple-pr-review:{review_tuple.contract_version} "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} "
        f"request={review_tuple.request_id} -->"
    )


def _latest_current_request_id(
    timeline: list[dict], *, reviewer_login: str
) -> Optional[int]:
    """Return the current review-request event ID, or fail closed."""
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
        return None
    request_id = timeline[request_index].get("id")
    if (
        isinstance(request_id, bool)
        or not isinstance(request_id, int)
        or request_id <= 0
    ):
        return None
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
            return None
        later_reviewer = event.get("requested_reviewer")
        if (
            event.get("event") in {"review_request_removed", "review_requested"}
            and isinstance(later_reviewer, dict)
            and later_reviewer.get("login") == reviewer_login
        ):
            return None
    return request_id


def select_authorized_tuple(
    live_pr: dict,
    *,
    reviewer_login: str,
    bot_bodies: list[str],
    load_timeline: Callable[[int], list[dict]],
) -> Optional[ReviewTuple]:
    """Select a live exact tuple backed by the latest current review request."""
    try:
        base = live_pr["base"]
        head = live_pr["head"]
        requested = live_pr["requested_reviewers"]
        if (
            live_pr.get("state") != "open"
            or live_pr.get("draft") is not False
            or not isinstance(base, dict)
            or base.get("ref") != BASE_BRANCH
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

    try:
        timeline = load_timeline(pr_number)
    except (RuntimeError, TypeError, ValueError):
        return None
    if not isinstance(timeline, list):
        return None
    request_id = _latest_current_request_id(
        timeline, reviewer_login=reviewer_login
    )
    if request_id is None:
        return None
    candidate = ReviewTuple(
        repository=REPOSITORY,
        pr_number=pr_number,
        base_sha=str(base["sha"]),
        head_sha=str(head["sha"]),
        request_id=request_id,
    )
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


def _collection(endpoint: str) -> list[dict]:
    return _flatten_pages(gh_json("api", "--paginate", "--slurp", endpoint))


def _execution_tuple(payload: dict) -> ReviewTuple:
    try:
        review_tuple = ReviewTuple(
            repository=str(payload["repository"]),
            pr_number=int(payload["pr_number"]),
            base_sha=str(payload["base_sha"]),
            head_sha=str(payload["head_sha"]),
            request_id=int(payload["review_request_id"]),
            contract_version=str(payload["contract_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid execution evidence tuple") from exc
    if (
        review_tuple.repository != REPOSITORY
        or review_tuple.contract_version != CONTRACT_VERSION
        or review_tuple.pr_number <= 0
        or review_tuple.request_id <= 0
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


def _duration_ms(started_at: object, completed_at: object) -> int:
    if not isinstance(started_at, str) or not isinstance(completed_at, str):
        raise RuntimeError("local gate timing is missing")
    try:
        start = datetime.fromisoformat(started_at.removesuffix("Z") + "+00:00")
        end = datetime.fromisoformat(completed_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise RuntimeError("local gate timing is invalid") from exc
    duration = int((end - start).total_seconds() * 1000)
    if duration < 0:
        raise RuntimeError("local gate timing is invalid")
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


def _local_docker_host() -> str:
    """Resolve the active local Docker socket without allowing a remote daemon."""
    result = subprocess.run(
        ["docker", "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
            "HOME": os.environ.get("HOME", str(Path.home())),
        },
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
    )
    host = result.stdout.strip()
    socket_path = host.removeprefix("unix://")
    if (
        result.returncode != 0
        or not host.startswith("unix:///")
        or not Path(socket_path).is_absolute()
    ):
        raise RuntimeError("local Docker socket is unavailable")
    return host


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


def _fetch_exact_commit(workspace: Path, commit_sha: str, *, home: Path) -> None:
    """Fetch one trusted tuple commit without exposing credentials to PR code."""
    gh_config_dir = os.environ.get("GH_CONFIG_DIR", "")
    if not gh_config_dir or not Path(gh_config_dir).is_absolute():
        raise RuntimeError("local worker GitHub credential source is unavailable")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin"),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "HOME": str(home),
        "GH_CONFIG_DIR": gh_config_dir,
    }
    result = _run_command(
        [
            "git",
            "-c",
            "credential.helper=",
            "-c",
            "credential.helper=!gh auth git-credential",
            "fetch",
            "-q",
            "--depth=1",
            "origin",
            commit_sha,
        ],
        cwd=workspace,
        env=env,
        timeout=180,
    )
    if result.returncode != 0:
        raise RuntimeError("local worker could not materialize exact tuple")


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
    gates: list[dict] = []
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
            ):
                result = _run_command(command, cwd=workspace, env=env, timeout=60)
                if result.returncode != 0:
                    raise RuntimeError("local worker could not initialize workspace")
            _fetch_exact_commit(workspace, review_tuple.head_sha, home=home)
            checkout = _run_command(
                ["git", "checkout", "-q", "--detach", "FETCH_HEAD"],
                cwd=workspace,
                env=env,
                timeout=60,
            )
            if checkout.returncode != 0:
                raise RuntimeError("local worker could not materialize exact tuple")
            _fetch_exact_commit(workspace, review_tuple.base_sha, home=home)
            if (
                _git_output(workspace, "rev-parse", "HEAD") != review_tuple.head_sha
                or _git_output(workspace, "rev-parse", "HEAD^{tree}") != head_tree_sha
                or _git_output(workspace, "cat-file", "-t", review_tuple.base_sha)
                != "commit"
                or _git_output(workspace, "status", "--porcelain")
            ):
                raise RuntimeError("local worker exact-head preflight failed")

            docker_env = {**env, "DOCKER_HOST": _local_docker_host()}
            unavailable_pattern = re.compile(
                r"(ECONNREFUSED|ENOTFOUND|Service Unavailable|docker: not found|"
                r"Cannot connect to the Docker daemon|no such host|"
                r"executable doesn't exist|browserType\.launch)",
                re.IGNORECASE,
            )
            not_started_pattern = re.compile(
                r"(docker: not found|Cannot connect to the Docker daemon|"
                r"failed to connect to the docker API|no such host)",
                re.IGNORECASE,
            )
            for name in BASELINE_EXECUTION_GATES:
                for command in (
                    ["git", "reset", "--hard", review_tuple.head_sha],
                    ["git", "clean", "-fdx"],
                ):
                    cleaned = _run_command(
                        command, cwd=workspace, env=env, timeout=120
                    )
                    if cleaned.returncode != 0:
                        raise RuntimeError("local worker could not reset exact-head source")

                install_log = ""
                install_returncode = 0
                try:
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
                        env=docker_env,
                        timeout=1200,
                    )
                    install_returncode = install.returncode
                    install_log = (install.stdout + install.stderr)[-200_000:]
                except subprocess.TimeoutExpired as exc:
                    install_returncode = 124
                    install_log = f"dependency installation timeout: {exc}"

                started = _utc_now()
                try:
                    result = _run_command(
                        _docker_gate_command(
                            workspace,
                            EXECUTION_GATE_COMMANDS[name],
                            network="none",
                        ),
                        cwd=workspace,
                        env=docker_env,
                        timeout=3600,
                    )
                    log = (result.stdout + result.stderr)[-1_000_000:]
                    attempted = (
                        result.returncode not in {125, 126, 127}
                        and not not_started_pattern.search(log)
                    )
                    if result.returncode == 0:
                        status = "pass"
                    elif (
                        install_returncode != 0
                        or result.returncode in {125, 126, 127}
                        or unavailable_pattern.search(log)
                    ):
                        status = "unavailable"
                    else:
                        status = "pr-fail"
                    exit_code = max(0, min(result.returncode, 255))
                except subprocess.TimeoutExpired as exc:
                    log = f"local gate timeout: {exc}"
                    status = "unavailable"
                    exit_code = 124
                    attempted = True
                completed = _utc_now()

                dirty = _git_output(workspace, "status", "--porcelain")
                if dirty:
                    status = "unavailable"
                    log += "\nsource tree mutated during local gate"
                if install_returncode != 0:
                    log += "\ndependency installation unavailable: " + install_log[-500:]
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
                        "duration_ms": _duration_ms(started, completed),
                        "tree_before": head_tree_sha,
                        "tree_after": head_tree_sha,
                        "evidence": {
                            "kind": "local_worker",
                            "log_sha256": hashlib.sha256(log.encode()).hexdigest(),
                            "reason": log[-500:] if status == "unavailable" else "",
                        },
                    }
                )
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            reason = str(exc)
            gates.extend(
                _unavailable_local_gate(name, reason)
                for name in BASELINE_EXECUTION_GATES[len(gates) :]
            )
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


def _summary_marker(review_tuple: ReviewTuple) -> str:
    return (
        "<!-- newtonsapple-pr-review-summary:v2 "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} "
        f"request={review_tuple.request_id} -->"
    )


def _requested_marker(review_tuple: ReviewTuple) -> str:
    return (
        "<!-- newtonsapple-pr-review-requested:v2 "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} "
        f"request={review_tuple.request_id} -->"
    )


def _started_marker(review_tuple: ReviewTuple) -> str:
    return (
        "<!-- newtonsapple-pr-review-started:v2 "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} "
        f"request={review_tuple.request_id} -->"
    )


def _blocker_marker(review_tuple: ReviewTuple) -> str:
    return (
        "<!-- newtonsapple-pr-review-blocker:v2 "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} "
        f"request={review_tuple.request_id} -->"
    )


def _dead_letter_marker(review_tuple: ReviewTuple) -> str:
    return (
        "<!-- newtonsapple-pr-review-dead-letter:v2 "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} "
        f"request={review_tuple.request_id} -->"
    )


def _retry_marker(review_tuple: ReviewTuple, attempt: int) -> str:
    return (
        "<!-- newtonsapple-pr-review-retry:v2 "
        f"repo={review_tuple.repository} pr={review_tuple.pr_number} "
        f"base={review_tuple.base_sha} head={review_tuple.head_sha} "
        f"request={review_tuple.request_id} "
        f"attempt={attempt} -->"
    )


def _retry_content(
    review_tuple: ReviewTuple,
    *,
    attempt: int,
    max_attempts: int,
    retry_after: Optional[int],
    failure_reason: str,
) -> str:
    if retry_after is None:
        raise ValueError("retry timestamp is missing")
    retry_at = datetime.fromtimestamp(retry_after, timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S UTC"
    )
    return (
        f"**PR review retry scheduled:** PR #{review_tuple.pr_number} at head "
        f"`{review_tuple.head_sha[:12]}` failed before publication because "
        f"{failure_reason}. No GitHub review was posted. Attempt {attempt + 1} "
        f"of {max_attempts} will be eligible after {retry_at}."
    )


def _summary_content(review_tuple: ReviewTuple, pr_url: str, review_body: str) -> str:
    findings = re.findall(
        r"(?m)^###\s+(P[0-3])\s+[—-]\s+(.+?)\s*$", review_body
    )
    if findings:
        first_severity, first_title = findings[0]
        finding_summary = (
            f"{len(findings)} actionable finding"
            f"{'s' if len(findings) != 1 else ''}; highest is "
            f"{first_severity}: {first_title.rstrip('.')}"
        )
    else:
        finding_summary = "no actionable findings"
    gate_statuses = re.findall(
        r"(?im)^\|\s*`[^`]+`\s*\|\s*\*\*"
        r"(pass|pr-fail|unavailable)\*\*\s*\|",
        review_body,
    )
    if gate_statuses:
        counts = {
            status: sum(1 for candidate in gate_statuses if candidate.lower() == status)
            for status in ("pass", "pr-fail", "unavailable")
        }
        gate_summary = (
            f" Gates: {counts['pass']} PASS, {counts['pr-fail']} FAIL, "
            f"{counts['unavailable']} UNAVAILABLE."
        )
    else:
        gate_summary = ""
    return (
        f"**PR review completed:** [#{review_tuple.pr_number}]({pr_url}) — "
        f"{finding_summary}.{gate_summary} The full review on GitHub is the "
        "source of truth."
    )


def _requested_content(review_tuple: ReviewTuple, pr_url: str) -> str:
    return (
        f"**PR review requested:** [#{review_tuple.pr_number}]({pr_url}) at "
        f"head `{review_tuple.head_sha[:12]}`. Hermany is queued to review it locally."
    )


def _started_content(review_tuple: ReviewTuple, pr_url: str) -> str:
    return (
        f"**PR review started:** Hermany is reviewing "
        f"[#{review_tuple.pr_number}]({pr_url}) at head "
        f"`{review_tuple.head_sha[:12]}`."
    )


def _buzz_own_pubkey() -> str:
    result = subprocess.run(
        ["buzz", "users", "get"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError("Buzz identity lookup unavailable")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Buzz identity lookup returned malformed JSON") from exc
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise RuntimeError("Buzz identity lookup returned an ambiguous identity")
    pubkey = data[0].get("pubkey")
    if not isinstance(pubkey, str) or re.fullmatch(r"[0-9a-f]{64}", pubkey) is None:
        raise RuntimeError("Buzz identity lookup returned an invalid identity")
    return pubkey


def _buzz_find(marker: str, reply_to: Optional[str] = None) -> Optional[str]:
    own_pubkey = _buzz_own_pubkey()
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
        author_pubkey = item.get("pubkey")
        tags = item.get("tags")
        in_channel = isinstance(tags, list) and any(
            isinstance(tag, list)
            and len(tag) >= 2
            and tag[0] == "h"
            and tag[1] == BUZZ_CHANNEL
            for tag in tags
        )
        is_expected_reply = reply_to is None or (
            isinstance(tags, list)
            and any(
                isinstance(tag, list)
                and len(tag) >= 2
                and tag[0] == "e"
                and tag[1] == reply_to
                for tag in tags
            )
        )
        if (
            in_channel
            and is_expected_reply
            and author_pubkey == own_pubkey
            and isinstance(content, str)
            and content.rstrip().endswith(marker)
            and isinstance(event_id, str)
        ):
            return event_id
    return None


def _buzz_send(content: str, reply_to: Optional[str] = None) -> str:
    command = [
        "buzz",
        "messages",
        "send",
        "--channel",
        BUZZ_CHANNEL,
    ]
    if reply_to is not None:
        command.extend(["--reply-to", reply_to])
    command.extend(["--content", "-"])
    result = subprocess.run(
        command,
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


def _ensure_requested_message(
    store: ReviewStateStore, review_tuple: ReviewTuple, pr_url: str
) -> str:
    event_id = store.requested_event_id(review_tuple)
    if event_id is not None:
        return event_id
    marker = _requested_marker(review_tuple)
    event_id = _buzz_find(marker, None)
    if event_id is None:
        event_id = _buzz_send(
            f"{_requested_content(review_tuple, pr_url)}\n\n{marker}", None
        )
    return store.record_requested_event(review_tuple, event_id)


def _ensure_started_message(
    store: ReviewStateStore, review_tuple: ReviewTuple, pr_url: str
) -> str:
    event_id = store.started_event_id(review_tuple)
    if event_id is not None:
        return event_id
    reply_to = _ensure_requested_message(store, review_tuple, pr_url)
    marker = _started_marker(review_tuple)
    event_id = _buzz_find(marker, reply_to)
    if event_id is None:
        event_id = _buzz_send(
            f"{_started_content(review_tuple, pr_url)}\n\n{marker}", reply_to
        )
    return store.record_started_event(review_tuple, event_id)


def _live_review_state(pr_number: int, expected_login: str) -> tuple[dict, list[str]]:
    live_pr_result = gh_json("api", f"repos/{REPOSITORY}/pulls/{pr_number}")
    if not isinstance(live_pr_result, dict):
        raise RuntimeError("GitHub returned malformed pull request")
    live_pr = cast(dict[str, Any], live_pr_result)
    head = live_pr.get("head")
    if not isinstance(head, dict) or SHA_PATTERN.fullmatch(str(head.get("sha", ""))) is None:
        raise RuntimeError("GitHub returned malformed pull request head")
    comments = _collection(f"repos/{REPOSITORY}/issues/{pr_number}/comments?per_page=100")
    reviews = _collection(f"repos/{REPOSITORY}/pulls/{pr_number}/reviews?per_page=100")
    return live_pr, _bot_bodies(comments + reviews, expected_login)


def _load_timeline(pr_number: int) -> list[dict]:
    return _collection(
        f"repos/{REPOSITORY}/issues/{pr_number}/timeline?per_page=100"
    )


def _recoverable_live_tuple(
    pr_number: int, expected_login: str
) -> tuple[dict, Optional[ReviewTuple], list[str]]:
    """Return a tuple backed by the latest non-invalidated review request."""
    live_pr, bodies = _live_review_state(pr_number, expected_login)
    selected = select_authorized_tuple(
        live_pr,
        reviewer_login=expected_login,
        bot_bodies=[],
        load_timeline=_load_timeline,
    )
    return live_pr, selected, bodies


def _authorized_live_tuple(
    pr_number: int, expected_login: str
) -> tuple[dict, Optional[ReviewTuple], list[str]]:
    live_pr, selected, bodies = _recoverable_live_tuple(pr_number, expected_login)
    if selected is not None:
        marker = review_marker(selected)
        if any(marker in body for body in bodies if isinstance(body, str)):
            selected = None
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
        try:
            live_pr, selected, bodies = _recoverable_live_tuple(
                number, expected_login
            )
        except (RuntimeError, TypeError, ValueError, sqlite3.Error):
            base = listed.get("base")
            head = listed.get("head")
            requested = listed.get("requested_reviewers")
            if (
                isinstance(base, dict)
                and isinstance(head, dict)
                and base.get("ref") == BASE_BRANCH
                and isinstance(base.get("sha"), str)
                and SHA_PATTERN.fullmatch(base["sha"]) is not None
                and isinstance(head.get("sha"), str)
                and SHA_PATTERN.fullmatch(head["sha"]) is not None
                and isinstance(requested, list)
                and expected_login
                in {
                    item.get("login")
                    for item in requested
                    if isinstance(item, dict)
                }
            ):
                blocked_tuple = ReviewTuple(
                    repository=REPOSITORY,
                    pr_number=number,
                    base_sha=base["sha"],
                    head_sha=head["sha"],
                    request_id=0,
                )
                _ensure_requested_message(
                    store, blocked_tuple, str(listed.get("html_url", ""))
                )
                store.enqueue_blocker(
                    blocked_tuple,
                    marker=_blocker_marker(blocked_tuple),
                    content=(
                        f"**Operational blocker:** PR #{number} at head "
                        f"`{blocked_tuple.head_sha}` could not safely verify current "
                        "request provenance. The review was not started."
                    ),
                )
            continue
        if selected is not None:
            matching_body = next(
                (
                    body
                    for body in bodies
                    if review_marker(selected) in body
                ),
                None,
            )
            if matching_body is not None:
                pr_url = str(live_pr.get("html_url", ""))
                thread_root = _ensure_requested_message(store, selected, pr_url)
                _ensure_started_message(store, selected, pr_url)
                store.record_external_completion(
                    selected,
                    now=int(time.time()),
                    marker=_summary_marker(selected),
                    content=_summary_content(selected, pr_url, matching_body),
                    reply_to=thread_root,
                )
                continue
            events.append(
                {
                    "delivery_id": (
                        f"recovery-v2-pr{number}-"
                        f"{selected.base_sha[:12]}-{selected.head_sha[:12]}-"
                        f"req{selected.request_id}"
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
                request_id=0,
            )
        except KeyError:
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
            _ensure_requested_message(
                store, review_tuple, str(live_pr.get("html_url", ""))
            )
            store.enqueue_blocker(
                review_tuple,
                marker=_blocker_marker(review_tuple),
                content=(
                    f"**Operational blocker:** PR #{number} at head "
                    f"`{review_tuple.head_sha}` still requests `{expected_login}`, "
                    "but its latest current review request could not be verified from "
                    "the timeline. The review was not started."
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
            request_id=int(payload["review_request_id"]),
            contract_version=str(payload["contract_version"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("invalid settlement tuple") from exc
    if (
        review_tuple.repository != REPOSITORY
        or review_tuple.contract_version != CONTRACT_VERSION
        or review_tuple.pr_number <= 0
        or review_tuple.request_id <= 0
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
    if operation == "started":
        if not store.active_lease(
            review_tuple, lease_token=lease_token, now=int(time.time())
        ):
            raise RuntimeError("review lease is not active")
        pr_url = payload.get("pr_url")
        if not isinstance(pr_url, str) or not pr_url.startswith(
            f"https://github.com/{REPOSITORY}/pull/"
        ):
            raise RuntimeError("invalid pull request URL")
        _ensure_started_message(store, review_tuple, pr_url)
        return {"settled": "started"}
    if operation == "claim_publish":
        _, selected, _ = _recoverable_live_tuple(
            review_tuple.pr_number, expected_login
        )
        if selected != review_tuple:
            raise RuntimeError("review request generation changed before publication")
        store.claim_publication(
            review_tuple,
            lease_token=lease_token,
            now=int(time.time()),
            lease_seconds=15 * 60,
        )
        return {"settled": "claim_publish"}
    if operation == "release":
        failure_code = payload.get("failure_code", "processing_failed")
        if not isinstance(failure_code, str) or failure_code not in FAILURE_REASONS:
            failure_code = "processing_failed"
        failure_reason = FAILURE_REASONS[failure_code]
        failure = store.record_failure(
            review_tuple,
            lease_token=lease_token,
            now=int(time.time()),
            retry_delay=RETRY_DELAY_SECONDS,
            max_attempts=MAX_REVIEW_ATTEMPTS,
            dead_letter_marker=_dead_letter_marker(review_tuple),
            dead_letter_content=(
                "**Operational blocker:** automated review for PR "
                f"#{review_tuple.pr_number} at head `{review_tuple.head_sha}` "
                f"failed after {MAX_REVIEW_ATTEMPTS} attempts. The tuple was "
                f"dead-lettered because {failure_reason}. No GitHub review was "
                "published; retry the "
                "review request after resolving this blocker."
            ),
            failure_reason=failure_reason,
        )
        drain_summary_outbox(store, find_existing=_buzz_find, send=_buzz_send)
        return {"settled": "release", **failure}
    if operation != "complete":
        raise RuntimeError("invalid settlement operation")

    # GitHub normally removes a requested reviewer when that reviewer submits
    # the formal review. The generation was revalidated and publication-fenced
    # immediately before the side effect, so post-publication settlement must
    # verify the exact bot marker and live SHAs without requiring the reviewer
    # request to remain current.
    live_pr, bodies = _live_review_state(review_tuple.pr_number, expected_login)
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
    thread_root = _ensure_requested_message(store, review_tuple, pr_url)
    _ensure_started_message(store, review_tuple, pr_url)
    store.settle_review(
        review_tuple,
        lease_token=lease_token,
        now=int(time.time()),
        marker=_summary_marker(review_tuple),
        content=_summary_content(review_tuple, pr_url, matching_body),
        reply_to=thread_root,
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
    live_pr, bodies = _live_review_state(number, expected_login)
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
        or payload_base.get("ref") != BASE_BRANCH
        or live_base.get("ref") != BASE_BRANCH
        or payload_base.get("sha") != live_base.get("sha")
        or payload_head.get("sha") != live_head.get("sha")
    ):
        raise RuntimeError("live pull request tuple changed after review request")
    review_tuple = select_authorized_tuple(
        live_pr,
        reviewer_login=expected_login,
        bot_bodies=bodies,
        load_timeline=_load_timeline,
    )
    if (
        live_pr.get("state") != "open"
        or live_pr.get("draft") is not False
        or review_tuple is None
        or review_tuple.base_sha != live_base.get("sha")
        or review_tuple.head_sha != live_head.get("sha")
        or not isinstance(requested_reviewers, list)
        or expected_login
        not in {
            reviewer.get("login")
            for reviewer in requested_reviewers
            if isinstance(reviewer, dict)
        }
    ):
        raise RuntimeError("webhook pull request is no longer eligible")
    lease_token = store.reserve(
        review_tuple, now=int(time.time()), lease_seconds=LEASE_SECONDS
    )
    if lease_token is None:
        raise RuntimeError("review tuple is already leased or completed")
    try:
        _ensure_requested_message(
            store, review_tuple, str(live_pr.get("html_url", ""))
        )
    except Exception:
        store.release(review_tuple, lease_token=lease_token)
        raise
    return {
        "contract_version": CONTRACT_VERSION,
        "repository": REPOSITORY,
        "pr_number": number,
        "expected_base_sha": review_tuple.base_sha,
        "expected_base_ref": BASE_BRANCH,
        "expected_head_sha": review_tuple.head_sha,
        "review_request_id": review_tuple.request_id,
        "action": "review_requested",
        "pr_url": str(live_pr.get("html_url", "")),
        "review_marker": review_marker(review_tuple),
        "publisher_login": expected_login,
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
        elif operation in {"claim_publish", "complete", "release", "started"}:
            output = _settle(payload, expected_login, store)
        elif operation is not None:
            raise RuntimeError("unsupported control-plane operation")
        else:
            output = _gate_webhook(payload, expected_login, store)
        print(json.dumps(output, separators=(",", ":")))
    except (
        json.JSONDecodeError,
        OSError,
        RuntimeError,
        TypeError,
        ValueError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ):
        print(json.dumps({"__hermes_ignore__": True}))


if __name__ == "__main__":
    main()
