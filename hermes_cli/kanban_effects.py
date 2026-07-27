"""Repository-only **candidate** for a review-required Kanban effect system.

STATUS: repository-only, **default-off**, NOT wired into the live product.

Nothing in this module is created by, imported by, or reachable from the
normal Kanban runtime. The effect / outbox / lane tables it defines are
**only** created by :func:`install_effects_schema`, which the dispatcher,
cron, gateway, plugins, config, ``connect``/``init_db``/migrations, and every
read-only path never call. A fresh production Kanban DB therefore has none of
these tables — see ``tests/hermes_cli/test_kanban_effects.py`` for the guard.

What this candidate models
--------------------------
When a worker finishes (``complete``) or parks (``block``) a task, it may emit
an optional, schema-validated ``review_required`` payload describing a GitHub
PR that must pass review before downstream QA/Release work proceeds. The
producer persists a durable *source/outbox* record inside the SAME transition
transaction as the status change (never comment metadata — comments are not an
authority). A separate, explicitly-invoked reconciler later:

  * re-reads the effect payload hash / revision and a freshly-*injected*
    (never network-fetched-while-a-DB-transaction-is-held) GitHub observation,
  * compare-and-swaps the canonical *lane* for the (repo, PR) pair,
  * creates or reuses exactly one *parentless, exact-head* review card,
  * attaches that authoritative review card as the parent of the pre-created
    downstream QA/Release cards and demotes any ``todo``/``ready`` ones back to
    ``todo``,
  * binds the effect to its target card + lane, marking it durably done.

Determinism, exactly-once and durability
----------------------------------------
Lane, effect and payload identities are deterministic content hashes. A lane is
shared by a (repo, PR); an effect additionally includes its source task, so an
identical replay collapses while independent implementation handoffs retain
provenance. ``UNIQUE`` constraints + compare-and-swap make the producer and the
reconciler idempotent under concurrency. Effects carry a
durable state machine (pending → leased → done / failed → dlq) with a lease
owner/expiry, an attempt counter, a monotonic ``next_attempt_at`` backoff, and
a dead-letter terminal state.

Requirement map (R01–R21)
--------------------------
R01 valid payload persists effect+outbox+lane in the transition txn.
R02 absent payload preserves existing transition behavior byte-for-byte.
R03 invalid payload is rejected with a typed code; no state change.
R04 effect/outbox/lane tables exist only after ``install_effects_schema``.
R05 a normal ``connect``/``init_db`` DB lacks the tables.
R06 canonical lane identity is deterministic for a (repo, PR) pair.
R07 canonical payload identity is deterministic and key-order independent.
R08 duplicate emissions dedupe to one effect via UNIQUE(lane, payload_hash).
R09 lane reconciliation is serialized by a revision compare-and-swap.
R10 an effect can be leased; an expired lease is re-acquirable.
R11 a not-ready effect retries with a monotonic ``next_attempt_at`` backoff.
R12 an effect that exhausts ``max_attempts`` lands in the DLQ terminal state.
R13 a missing required-check policy returns NOT_READY(REQUIRED_CHECK_POLICY_MISSING).
R14 an observation whose head != payload head is STALE_HEAD; no card created.
R15 a closed PR / merge conflict is NOT_READY; no card created.
R16 a ready effect creates exactly one parentless exact-head review card and
    reuses it on re-run for the same head.
R17 reconcile attaches the review parent, demotes precreated todo/ready
    QA/Release cards to todo, and binds the effect target + lane.
R18 a downstream card left running/done without a compatible exact-head
    APPROVE is reported as typed containment (alert only).
R19 a manual / unbound Review card that conflicts with the authoritative lane
    is reported as typed containment (alert only).
R20 a stale running review is drained; no parallel review card is spawned.
R21 legacy untyped review-ish text produces an alert only and never
    auto-creates a review card.

None of this is wired into the dispatcher, cron, plugins, or config. It exists
to be exercised by tests and an explicit CLI/harness readback.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

KIND_REVIEW_REQUIRED = "review_required"

# Durable effect states.
STATE_PENDING = "pending"
STATE_LEASED = "leased"
STATE_DONE = "done"
STATE_FAILED = "failed"
STATE_DLQ = "dlq"

# Source transitions that may emit an effect.
TRANSITION_COMPLETE = "complete"
TRANSITION_BLOCK = "block"
_VALID_TRANSITIONS = frozenset({TRANSITION_COMPLETE, TRANSITION_BLOCK})

# Readiness / not-ready codes (typed, stable strings).
READY = "READY"
REQUIRED_CHECK_POLICY_MISSING = "REQUIRED_CHECK_POLICY_MISSING"
PR_CLOSED = "PR_CLOSED"
HEAD_CONFLICT = "HEAD_CONFLICT"
STALE_HEAD = "STALE_HEAD"
CHECKS_INCOMPLETE = "CHECKS_INCOMPLETE"
CHECKS_FAILED = "CHECKS_FAILED"
OBSERVATION_STALE = "OBSERVATION_STALE"

# Containment finding codes.
CONTAINMENT_DOWNSTREAM_UNAPPROVED = "DOWNSTREAM_RAN_WITHOUT_EXACT_HEAD_APPROVE"
CONTAINMENT_UNBOUND_REVIEW_CONFLICT = "UNBOUND_REVIEW_CONFLICT"
CONTAINMENT_STALE_RUNNING_REVIEW = "STALE_RUNNING_REVIEW"

# Retry policy defaults (seconds). Backoff is deterministic so a fake clock
# in tests fully controls scheduling — no wall-clock sleeps anywhere.
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_LEASE_TTL_SECONDS = 300
_RETRY_BACKOFF_BASE_SECONDS = 30
_RETRY_BACKOFF_CAP_SECONDS = 3600

_SHA40_RE = re.compile(r"[0-9a-f]{40}")
_REPO_RE = re.compile(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+")

# Reviewer assignee for the authoritative card. Deliberately generic; nothing
# in the live dispatcher routes on it because nothing dispatches this candidate.
REVIEW_CARD_ASSIGNEE = "reviewer"


# ---------------------------------------------------------------------------
# Typed errors
# ---------------------------------------------------------------------------


class EffectValidationError(ValueError):
    """A ``review_required`` payload failed schema validation.

    Carries a stable ``code`` so producers can surface a typed rejection to
    the model without state change (R03).
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class EffectsNotInstalledError(RuntimeError):
    """Raised by readback helpers when the effect tables are absent.

    Producers never raise this (they no-op when the schema is not installed so
    the default-off feature cannot break a normal transition); it exists so the
    explicit CLI/harness scanner fails loudly instead of silently reading an
    empty world.
    """


# ---------------------------------------------------------------------------
# Canonical identities (R06, R07)
# ---------------------------------------------------------------------------


def _sha16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def canonical_lane_id(repo: str, pr_number: int, kind: str = KIND_REVIEW_REQUIRED) -> str:
    """Deterministic lane identity for a (repo, PR, kind) triple.

    A *lane* is the serialization point for all effects that concern one PR:
    at most one authoritative review card ever exists per lane, and lane
    reconciliation is CAS-guarded by a revision counter (R09).
    """
    canon = json.dumps(
        {"repo": repo, "pr_number": int(pr_number), "kind": kind},
        sort_keys=True,
        separators=(",", ":"),
    )
    return "lane_" + _sha16(canon)


def canonical_payload_hash(payload: "ReviewRequiredPayload") -> str:
    """Deterministic, key-order-independent identity of *what review is required*.

    Only the review-identity fields participate (repo, PR, exact head, required
    checks). Binding hints such as ``downstream_task_ids`` are intentionally
    excluded so re-emitting the same review with a different downstream hint
    dedupes to the same effect rather than minting a new one.
    """
    canon = json.dumps(
        {
            "kind": payload.kind,
            "repo": payload.repo,
            "pr_number": payload.pr_number,
            "head_sha": payload.head_sha,
            "required_checks": (
                sorted(payload.required_checks)
                if payload.required_checks is not None
                else None
            ),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return _sha16(canon)


def canonical_effect_id(lane_id: str, payload_hash: str, source_task_id: str) -> str:
    """Deterministic effect identity: one effect per source + lane + payload.

    ``source_task_id`` is intentionally part of the identity: two independent
    implementation handoffs must retain distinct provenance even when they
    target the same PR/head. Replays of *one* source handoff compute the same
    id, so ``INSERT OR IGNORE`` still collapses concurrent duplicate emissions
    without using ``tasks.idempotency_key`` for correctness.
    """
    return "eff_" + _sha16(source_task_id + ":" + lane_id + ":" + payload_hash)


# ---------------------------------------------------------------------------
# Payload schema (R03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReviewRequiredPayload:
    """A validated ``review_required`` effect payload.

    ``required_checks`` is the producer's declared required-check *policy*.
    ``None`` means the producer did not declare one; the reconciler still
    requires the *observation* to carry a policy (R13) — this field only
    tightens the gate, it cannot relax it.
    ``downstream_task_ids`` are the pre-created QA/Release cards the reconciler
    should re-parent under the authoritative review card (binding hint only;
    not part of payload identity).
    """

    kind: str
    repo: str
    pr_number: int
    head_sha: str
    required_checks: Optional[tuple[str, ...]] = None
    downstream_task_ids: tuple[str, ...] = ()

    def to_json(self) -> str:
        return json.dumps(
            {
                "kind": self.kind,
                "repo": self.repo,
                "pr_number": self.pr_number,
                "head_sha": self.head_sha,
                "required_checks": (
                    list(self.required_checks)
                    if self.required_checks is not None
                    else None
                ),
                "downstream_task_ids": list(self.downstream_task_ids),
            },
            sort_keys=True,
            separators=(",", ":"),
        )

    @staticmethod
    def from_json(text: str) -> "ReviewRequiredPayload":
        return validate_review_required(json.loads(text))


def validate_review_required(raw: Any) -> ReviewRequiredPayload:
    """Validate a raw ``review_required`` mapping into a typed payload.

    Raises :class:`EffectValidationError` with a stable ``code`` on any
    violation and never has a side effect, so a producer can validate before
    mutating anything (R03). Missing/absent payloads are the caller's concern —
    passing ``None`` here is itself an error.
    """
    if not isinstance(raw, Mapping):
        raise EffectValidationError("BAD_PAYLOAD", "payload must be an object")

    kind = raw.get("kind")
    if kind is None:
        raise EffectValidationError("MISSING_KIND", "kind is required")
    if kind != KIND_REVIEW_REQUIRED:
        raise EffectValidationError(
            "BAD_KIND", f"kind must be {KIND_REVIEW_REQUIRED!r}, got {kind!r}"
        )

    repo = raw.get("repo")
    if not repo or not isinstance(repo, str):
        raise EffectValidationError("MISSING_REPO", "repo (owner/name) is required")
    repo = repo.strip()
    if not _REPO_RE.fullmatch(repo):
        raise EffectValidationError(
            "BAD_REPO", f"repo must be 'owner/name', got {repo!r}"
        )

    pr_raw = raw.get("pr_number")
    if pr_raw is None:
        raise EffectValidationError("MISSING_PR", "pr_number is required")
    try:
        pr_number = int(pr_raw)
    except (TypeError, ValueError):
        raise EffectValidationError("BAD_PR", f"pr_number must be an int, got {pr_raw!r}")
    if pr_number <= 0:
        raise EffectValidationError("BAD_PR", "pr_number must be positive")

    head = raw.get("head_sha")
    if not head or not isinstance(head, str):
        raise EffectValidationError("MISSING_HEAD", "head_sha is required")
    head = head.strip().lower()
    if not _SHA40_RE.fullmatch(head):
        raise EffectValidationError(
            "BAD_HEAD", "head_sha must be an exact 40-character hex commit sha"
        )

    required_checks: Optional[tuple[str, ...]] = None
    rc_raw = raw.get("required_checks")
    if rc_raw is not None:
        if not isinstance(rc_raw, (list, tuple)):
            raise EffectValidationError(
                "BAD_REQUIRED_CHECKS", "required_checks must be a list of strings"
            )
        cleaned: list[str] = []
        for item in rc_raw:
            if not isinstance(item, str) or not item.strip():
                raise EffectValidationError(
                    "BAD_REQUIRED_CHECKS", "required_checks entries must be non-empty strings"
                )
            cleaned.append(item.strip())
        required_checks = tuple(cleaned)

    downstream: tuple[str, ...] = ()
    ds_raw = raw.get("downstream_task_ids")
    if ds_raw is not None:
        if isinstance(ds_raw, str):
            ds_raw = [ds_raw]
        if not isinstance(ds_raw, (list, tuple)):
            raise EffectValidationError(
                "BAD_DOWNSTREAM", "downstream_task_ids must be a list of task ids"
            )
        seen: set[str] = set()
        cleaned_ds: list[str] = []
        for item in ds_raw:
            s = str(item).strip()
            if s and s not in seen:
                seen.add(s)
                cleaned_ds.append(s)
        downstream = tuple(cleaned_ds)

    return ReviewRequiredPayload(
        kind=KIND_REVIEW_REQUIRED,
        repo=repo,
        pr_number=pr_number,
        head_sha=head,
        required_checks=required_checks,
        downstream_task_ids=downstream,
    )


# ---------------------------------------------------------------------------
# Injected GitHub observation (no network I/O — R13, R14, R15)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GithubObservation:
    """A point-in-time, *injected* view of a PR. No network I/O ever.

    ``required_check_policy`` is ``None`` when the repo/PR does not declare a
    required-check policy — the reconciler treats that as NOT_READY rather than
    silently approving (R13). ``checks`` maps check name → conclusion (e.g.
    ``"success"``, ``"failure"``, ``"pending"``).
    """

    repo: str
    pr_number: int
    head_sha: str
    is_open: bool
    has_conflicts: bool
    checks: Mapping[str, str]
    required_check_policy: Optional[tuple[str, ...]]
    observed_at: int

    def __post_init__(self) -> None:
        # Freeze the head at exactly 40 lowercase hex chars so downstream
        # exact-head comparisons are total.
        normalized = (self.head_sha or "").strip().lower()
        object.__setattr__(self, "head_sha", normalized)


@runtime_checkable
class GithubObserver(Protocol):
    """Injected observation source. Implementations MUST NOT perform network
    I/O while any DB transaction is held; the reconciler calls ``observe``
    strictly *before* opening its write transaction."""

    def observe(self, repo: str, pr_number: int) -> Optional[GithubObservation]:
        ...


class InMemoryGithubObserver:
    """Deterministic, in-memory observer for tests and the offline harness."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, int], GithubObservation] = {}

    def set(self, obs: GithubObservation) -> None:
        self._by_key[(obs.repo, obs.pr_number)] = obs

    def observe(self, repo: str, pr_number: int) -> Optional[GithubObservation]:
        return self._by_key.get((repo, int(pr_number)))


@dataclass(frozen=True)
class ReadinessResult:
    """Typed readiness verdict for a (payload, observation) pair."""

    code: str
    ready: bool
    detail: Optional[str] = None

    @property
    def is_policy_missing(self) -> bool:
        return self.code == REQUIRED_CHECK_POLICY_MISSING


def evaluate_readiness(
    payload: ReviewRequiredPayload,
    observation: Optional[GithubObservation],
    *,
    now: int,
    freshness_seconds: int = 900,
) -> ReadinessResult:
    """Pure readiness evaluation. No I/O, no DB, no mutation.

    Order matters: a missing required-check policy short-circuits to
    NOT_READY(REQUIRED_CHECK_POLICY_MISSING) (R13) so an unpoliced repo can
    never be approved by omission. Head mismatch is STALE_HEAD (R14); a closed
    PR or a merge conflict is NOT_READY (R15).
    """
    if observation is None:
        return ReadinessResult(OBSERVATION_STALE, False, "no observation available")

    if observation.observed_at > now:
        return ReadinessResult(OBSERVATION_STALE, False, "observation from the future")
    if (now - observation.observed_at) > freshness_seconds:
        return ReadinessResult(
            OBSERVATION_STALE, False, "observation older than freshness window"
        )

    # A missing required-check policy is a hard NOT_READY — never approve an
    # unpoliced PR by omission.
    if observation.required_check_policy is None:
        return ReadinessResult(
            REQUIRED_CHECK_POLICY_MISSING, False, "no required-check policy declared"
        )

    if not observation.is_open:
        return ReadinessResult(PR_CLOSED, False, "PR is not open")
    if observation.has_conflicts:
        return ReadinessResult(HEAD_CONFLICT, False, "PR has merge conflicts")

    # Exact-head gate: the observation must be for the exact commit the effect
    # was emitted against, else it is stale (R14).
    if observation.head_sha != payload.head_sha:
        return ReadinessResult(
            STALE_HEAD,
            False,
            f"observed head {observation.head_sha[:8]} != payload head {payload.head_sha[:8]}",
        )

    # Every required check (union of observation policy and payload policy)
    # must have concluded successfully.
    required = set(observation.required_check_policy)
    if payload.required_checks:
        required |= set(payload.required_checks)
    for name in sorted(required):
        conclusion = observation.checks.get(name)
        if conclusion is None or conclusion in {"pending", "queued", "in_progress"}:
            return ReadinessResult(CHECKS_INCOMPLETE, False, f"check {name!r} not concluded")
        if conclusion != "success":
            return ReadinessResult(CHECKS_FAILED, False, f"check {name!r} = {conclusion}")

    return ReadinessResult(READY, True, None)


# ---------------------------------------------------------------------------
# Schema installer (R04, R05) — the ONLY place these tables are created.
# ---------------------------------------------------------------------------

_EFFECT_TABLES = ("kanban_effect_lanes", "kanban_effects", "kanban_effect_outbox")

_INSTALL_SQL = """
CREATE TABLE IF NOT EXISTS kanban_effect_lanes (
    lane_id        TEXT PRIMARY KEY,
    repo           TEXT NOT NULL,
    pr_number      INTEGER NOT NULL,
    kind           TEXT NOT NULL,
    -- CAS revision guarding lane reconciliation (R09).
    revision       INTEGER NOT NULL DEFAULT 0,
    -- Bound authoritative review card + the exact head it reviews.
    review_task_id TEXT,
    head_sha       TEXT,
    created_at     INTEGER NOT NULL,
    updated_at     INTEGER NOT NULL,
    UNIQUE (repo, pr_number, kind)
);

CREATE TABLE IF NOT EXISTS kanban_effects (
    effect_id         TEXT PRIMARY KEY,
    lane_id           TEXT NOT NULL,
    source_task_id    TEXT NOT NULL,
    source_transition TEXT NOT NULL,
    kind              TEXT NOT NULL,
    payload_json      TEXT NOT NULL,
    payload_hash      TEXT NOT NULL,
    -- CAS revision guarding effect state transitions.
    revision          INTEGER NOT NULL DEFAULT 0,
    state             TEXT NOT NULL,
    attempts          INTEGER NOT NULL DEFAULT 0,
    max_attempts      INTEGER NOT NULL DEFAULT 5,
    lease_owner       TEXT,
    lease_expires_at  INTEGER,
    next_attempt_at   INTEGER NOT NULL DEFAULT 0,
    last_code         TEXT,
    last_error        TEXT,
    target_task_id    TEXT,
    created_at        INTEGER NOT NULL,
    updated_at        INTEGER NOT NULL,
    -- One effect per source + (lane, payload). Different implementation
    -- sources preserve provenance; replay of one source handoff dedupes (R08).
    UNIQUE (source_task_id, lane_id, payload_hash)
);

CREATE TABLE IF NOT EXISTS kanban_effect_outbox (
    effect_id    TEXT PRIMARY KEY,
    lane_id      TEXT NOT NULL,
    kind         TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    created_at   INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_effects_state_next
    ON kanban_effects(state, next_attempt_at);
CREATE INDEX IF NOT EXISTS idx_effects_lane
    ON kanban_effects(lane_id);
"""


def install_effects_schema(conn: sqlite3.Connection) -> None:
    """Create the effect/outbox/lane tables. THE ONLY creation path.

    Never call this from ``connect``, ``init_db``, migrations, the dispatcher,
    cron, gateway, plugins, config, or any read path. It exists for tests and
    the explicit offline harness so the feature stays default-off: a normal DB
    has none of these tables (R04, R05).
    """
    conn.executescript(_INSTALL_SQL)


def effects_installed(conn: sqlite3.Connection) -> bool:
    """True iff every effect table exists in this DB.

    Producers consult this so that emitting a payload against a normal
    (un-installed) DB is a safe no-op rather than an error — the whole point of
    default-off.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
        "(?, ?, ?)",
        _EFFECT_TABLES,
    ).fetchall()
    present = {r[0] for r in rows}
    return all(t in present for t in _EFFECT_TABLES)


# ---------------------------------------------------------------------------
# Producer — persist the source/outbox record in the transition txn (R01)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectRow:
    effect_id: str
    lane_id: str
    source_task_id: str
    source_transition: str
    kind: str
    payload_json: str
    payload_hash: str
    revision: int
    state: str
    attempts: int
    max_attempts: int
    lease_owner: Optional[str]
    lease_expires_at: Optional[int]
    next_attempt_at: int
    last_code: Optional[str]
    last_error: Optional[str]
    target_task_id: Optional[str]
    created_at: int
    updated_at: int

    @property
    def payload(self) -> ReviewRequiredPayload:
        return ReviewRequiredPayload.from_json(self.payload_json)

    @property
    def repo(self) -> str:
        return self.payload.repo

    @property
    def pr_number(self) -> int:
        return self.payload.pr_number


def record_effect_locked(
    conn: sqlite3.Connection,
    *,
    source_task_id: str,
    source_transition: str,
    payload: ReviewRequiredPayload,
    now: Optional[int] = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> Optional[str]:
    """Persist the source/outbox record for a valid effect. **Assumes the
    caller already holds an open write transaction** (the same transaction as
    the ``complete``/``block`` status change) — it opens no ``BEGIN`` of its
    own, so it can never nest one (R01, no-nested-BEGIN).

    Returns the (deterministic) effect id, or ``None`` when the effect schema
    is not installed — a normal, default-off DB, where recording is a safe
    no-op that leaves the transition untouched (R02 for the un-installed case).
    """
    if source_transition not in _VALID_TRANSITIONS:
        raise ValueError(
            f"source_transition must be one of {sorted(_VALID_TRANSITIONS)}"
        )
    if not effects_installed(conn):
        return None

    now = int(time.time()) if now is None else int(now)
    lane_id = canonical_lane_id(payload.repo, payload.pr_number, payload.kind)
    payload_hash = canonical_payload_hash(payload)
    effect_id = canonical_effect_id(lane_id, payload_hash, source_task_id)

    # Upsert the lane (deterministic identity; created lazily so the effect's
    # lane_id always resolves). Never clobbers an existing lane's binding.
    conn.execute(
        "INSERT OR IGNORE INTO kanban_effect_lanes "
        "(lane_id, repo, pr_number, kind, revision, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 0, ?, ?)",
        (lane_id, payload.repo, payload.pr_number, payload.kind, now, now),
    )
    # One effect per (lane, payload) — concurrent duplicate emissions collapse.
    conn.execute(
        "INSERT OR IGNORE INTO kanban_effects "
        "(effect_id, lane_id, source_task_id, source_transition, kind, "
        " payload_json, payload_hash, revision, state, attempts, max_attempts, "
        " next_attempt_at, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, 0, ?, ?, ?, ?)",
        (
            effect_id,
            lane_id,
            source_task_id,
            source_transition,
            payload.kind,
            payload.to_json(),
            payload_hash,
            STATE_PENDING,
            int(max_attempts),
            now,
            now,
            now,
        ),
    )
    conn.execute(
        "INSERT OR IGNORE INTO kanban_effect_outbox "
        "(effect_id, lane_id, kind, payload_hash, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (effect_id, lane_id, payload.kind, payload_hash, now),
    )
    return effect_id


# ---------------------------------------------------------------------------
# Readback / scanner (explicit harness only — no dispatcher/cron wiring)
# ---------------------------------------------------------------------------


def _effect_from_row(row: sqlite3.Row) -> EffectRow:
    return EffectRow(
        effect_id=row["effect_id"],
        lane_id=row["lane_id"],
        source_task_id=row["source_task_id"],
        source_transition=row["source_transition"],
        kind=row["kind"],
        payload_json=row["payload_json"],
        payload_hash=row["payload_hash"],
        revision=int(row["revision"]),
        state=row["state"],
        attempts=int(row["attempts"]),
        max_attempts=int(row["max_attempts"]),
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        next_attempt_at=int(row["next_attempt_at"]),
        last_code=row["last_code"],
        last_error=row["last_error"],
        target_task_id=row["target_task_id"],
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


def get_effect(conn: sqlite3.Connection, effect_id: str) -> Optional[EffectRow]:
    if not effects_installed(conn):
        raise EffectsNotInstalledError("effect schema not installed on this DB")
    row = conn.execute(
        "SELECT * FROM kanban_effects WHERE effect_id = ?", (effect_id,)
    ).fetchone()
    return _effect_from_row(row) if row else None


def list_effects(
    conn: sqlite3.Connection, *, state: Optional[str] = None
) -> list[EffectRow]:
    if not effects_installed(conn):
        raise EffectsNotInstalledError("effect schema not installed on this DB")
    if state is None:
        rows = conn.execute(
            "SELECT * FROM kanban_effects ORDER BY created_at, effect_id"
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM kanban_effects WHERE state = ? ORDER BY created_at, effect_id",
            (state,),
        ).fetchall()
    return [_effect_from_row(r) for r in rows]


def scan_due_effects(conn: sqlite3.Connection, *, now: int) -> list[EffectRow]:
    """Return effects eligible to be reconciled right now: pending and past
    their ``next_attempt_at`` backoff, or leased with an expired lease.

    Explicit readback for the offline harness — nothing calls this on a timer.
    """
    if not effects_installed(conn):
        raise EffectsNotInstalledError("effect schema not installed on this DB")
    rows = conn.execute(
        "SELECT * FROM kanban_effects "
        "WHERE (state = ? AND next_attempt_at <= ?) "
        "   OR (state = ? AND (lease_expires_at IS NULL OR lease_expires_at <= ?)) "
        "ORDER BY created_at, effect_id",
        (STATE_PENDING, now, STATE_LEASED, now),
    ).fetchall()
    return [_effect_from_row(r) for r in rows]


@dataclass(frozen=True)
class LaneRow:
    lane_id: str
    repo: str
    pr_number: int
    kind: str
    revision: int
    review_task_id: Optional[str]
    head_sha: Optional[str]
    created_at: int
    updated_at: int


def get_lane(conn: sqlite3.Connection, lane_id: str) -> Optional[LaneRow]:
    if not effects_installed(conn):
        raise EffectsNotInstalledError("effect schema not installed on this DB")
    row = conn.execute(
        "SELECT * FROM kanban_effect_lanes WHERE lane_id = ?", (lane_id,)
    ).fetchone()
    if not row:
        return None
    return LaneRow(
        lane_id=row["lane_id"],
        repo=row["repo"],
        pr_number=int(row["pr_number"]),
        kind=row["kind"],
        revision=int(row["revision"]),
        review_task_id=row["review_task_id"],
        head_sha=row["head_sha"],
        created_at=int(row["created_at"]),
        updated_at=int(row["updated_at"]),
    )


# ---------------------------------------------------------------------------
# Lease / retry / DLQ (R10, R11, R12)
# ---------------------------------------------------------------------------


def _backoff_seconds(attempts: int) -> int:
    """Deterministic exponential backoff, capped. No jitter → fully
    reproducible under a fake clock."""
    exp = min(attempts, 12)
    return min(_RETRY_BACKOFF_BASE_SECONDS * (2 ** exp), _RETRY_BACKOFF_CAP_SECONDS)


def lease_effect(
    conn: sqlite3.Connection,
    effect_id: str,
    *,
    owner: str,
    now: int,
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> Optional[EffectRow]:
    """Atomically acquire a lease on a due effect via CAS. Opens its own
    write transaction — callers must NOT already hold one.

    Wins only when the effect is pending-and-due, or leased-but-expired. The
    revision is bumped so a stale holder's later CAS write fails. Returns the
    leased row, or ``None`` if it could not be leased (R10).
    """
    from hermes_cli import kanban_db as kb

    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT * FROM kanban_effects WHERE effect_id = ?", (effect_id,)
        ).fetchone()
        if row is None:
            return None
        state = row["state"]
        rev = int(row["revision"])
        leasable = (
            (state == STATE_PENDING and int(row["next_attempt_at"]) <= now)
            or (
                state == STATE_LEASED
                and (row["lease_expires_at"] is None or int(row["lease_expires_at"]) <= now)
            )
        )
        if not leasable:
            return None
        cur = conn.execute(
            "UPDATE kanban_effects "
            "SET state = ?, lease_owner = ?, lease_expires_at = ?, "
            "    revision = revision + 1, updated_at = ? "
            "WHERE effect_id = ? AND revision = ?",
            (STATE_LEASED, owner, now + int(lease_ttl_seconds), now, effect_id, rev),
        )
        if cur.rowcount != 1:
            return None
    return get_effect(conn, effect_id)


def _record_attempt_failure(
    conn: sqlite3.Connection,
    effect_id: str,
    *,
    code: str,
    detail: Optional[str],
    now: int,
    expected_revision: int,
) -> str:
    """Record a not-ready/failed reconcile attempt and schedule the next one,
    or move the effect to the DLQ once ``max_attempts`` is exhausted (R11, R12).

    Opens its own write transaction; CAS-guarded on ``expected_revision`` so a
    stale reconciler cannot overwrite a newer state. Returns the resulting
    state (``pending``, ``failed`` or ``dlq``).
    """
    from hermes_cli import kanban_db as kb

    result_state = STATE_PENDING
    with kb.write_txn(conn):
        row = conn.execute(
            "SELECT attempts, max_attempts, revision FROM kanban_effects "
            "WHERE effect_id = ?",
            (effect_id,),
        ).fetchone()
        if row is None:
            return STATE_FAILED
        if int(row["revision"]) != expected_revision:
            # Someone else advanced the effect; do not clobber.
            return row_state(conn, effect_id) or STATE_FAILED
        attempts = int(row["attempts"]) + 1
        max_attempts = int(row["max_attempts"])
        if attempts >= max_attempts:
            result_state = STATE_DLQ
            next_at = now
        else:
            result_state = STATE_PENDING
            next_at = now + _backoff_seconds(attempts)
        conn.execute(
            "UPDATE kanban_effects "
            "SET state = ?, attempts = ?, next_attempt_at = ?, "
            "    lease_owner = NULL, lease_expires_at = NULL, "
            "    last_code = ?, last_error = ?, revision = revision + 1, "
            "    updated_at = ? "
            "WHERE effect_id = ? AND revision = ?",
            (result_state, attempts, next_at, code, detail, now, effect_id, expected_revision),
        )
    return result_state


def row_state(conn: sqlite3.Connection, effect_id: str) -> Optional[str]:
    row = conn.execute(
        "SELECT state FROM kanban_effects WHERE effect_id = ?", (effect_id,)
    ).fetchone()
    return row["state"] if row else None


# ---------------------------------------------------------------------------
# Reconciler — one outer write txn; network strictly outside it (R09, R16, R17)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReconcileResult:
    status: str  # "reconciled" | "not_ready" | "skipped" | "stale"
    code: str
    effect_id: str
    review_task_id: Optional[str] = None
    created_review: bool = False
    detail: Optional[str] = None


class _StaleHeadInsideTxn(Exception):
    """Internal: head drifted between lease and the reconcile txn.

    Raised inside the reconcile write transaction so ``write_txn`` rolls back
    cleanly (no partial review card is ever committed); the public wrapper
    converts it into a scheduled retry.
    """


def _reconcile_effect_impl(
    conn: sqlite3.Connection,
    effect_id: str,
    observer: GithubObserver,
    *,
    now: int,
    owner: str = "reconciler",
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> ReconcileResult:
    """Drive one effect toward its terminal state.

    Steps, in order, honoring "no network while a DB transaction is held":

      1. Lease the effect (own txn, CAS).
      2. Observe GitHub via the injected observer — OUTSIDE any transaction.
      3. Evaluate readiness (pure). Not ready → schedule retry / DLQ.
      4. In ONE outer write transaction: re-read the effect payload hash /
         revision, CAS the lane, create-or-reuse exactly one parentless
         exact-head review card, re-parent + demote the downstream QA/Release
         cards, and bind the effect target + lane (R16, R17).
    """
    from hermes_cli import kanban_db as kb

    if not effects_installed(conn):
        raise EffectsNotInstalledError("effect schema not installed on this DB")

    leased = lease_effect(
        conn, effect_id, owner=owner, now=now, lease_ttl_seconds=lease_ttl_seconds
    )
    if leased is None:
        return ReconcileResult("skipped", "NOT_LEASABLE", effect_id)

    eff = leased
    payload = eff.payload

    # --- network / observation strictly OUTSIDE any DB transaction ---
    observation = observer.observe(payload.repo, payload.pr_number)
    readiness = evaluate_readiness(payload, observation, now=now)

    if not readiness.ready:
        state = _record_attempt_failure(
            conn,
            effect_id,
            code=readiness.code,
            detail=readiness.detail,
            now=now,
            expected_revision=eff.revision,
        )
        return ReconcileResult("not_ready", readiness.code, effect_id, detail=state)

    # --- one outer write transaction ---
    with kb.write_txn(conn):
        # Re-read under the lock and CAS-guard on the exact payload hash +
        # revision we leased. Any drift ⇒ bail without side effects.
        row = conn.execute(
            "SELECT * FROM kanban_effects WHERE effect_id = ? AND payload_hash = ? "
            "AND revision = ? AND state = ? AND lease_owner = ?",
            (effect_id, eff.payload_hash, eff.revision, STATE_LEASED, owner),
        ).fetchone()
        if row is None:
            return ReconcileResult("stale", "REVISION_DRIFT", effect_id)

        # Belt-and-suspenders: the just-observed head must still equal the
        # payload head (freshness re-check inside the lock; no re-fetch).
        assert observation is not None  # readiness.ready implies non-None
        if observation.head_sha != payload.head_sha:
            # Fall out of the txn without mutation; schedule a retry after.
            raise _StaleHeadInsideTxn()

        lane_id = eff.lane_id
        lane = conn.execute(
            "SELECT lane_id, revision, review_task_id, head_sha "
            "FROM kanban_effect_lanes WHERE lane_id = ?",
            (lane_id,),
        ).fetchone()
        if lane is None:
            return ReconcileResult("stale", "LANE_MISSING", effect_id)
        lane_rev = int(lane["revision"])

        # Reuse the bound review card iff it exists AND reviews the exact head
        # (R16). Otherwise mint a fresh parentless review card.
        review_task_id = lane["review_task_id"]
        created_review = False
        reuse = (
            review_task_id is not None
            and lane["head_sha"] == payload.head_sha
            and kb.get_task(conn, review_task_id) is not None
        )
        if not reuse:
            review_task_id = kb._new_task_id()
            kb._create_task_locked(
                conn,
                task_id=review_task_id,
                now=now,
                title=(
                    f"Review {payload.repo}#{payload.pr_number} "
                    f"@ {payload.head_sha[:8]}"
                ),
                body=(
                    "Authoritative review card for PR "
                    f"{payload.repo}#{payload.pr_number} at exact head "
                    f"{payload.head_sha}. Auto-created by the review-required "
                    "effect reconciler (repository-only candidate)."
                ),
                assignee=REVIEW_CARD_ASSIGNEE,
                parents=(),
                triage=False,
                # A newly-created authoritative Review is ready to be claimed;
                # it must not masquerade as already-running before any review
                # worker has actually claimed it.
                initial_status="ready",
                priority=0,
                created_by="kanban_effects",
                workspace_kind="scratch",
                workspace_path=None,
                branch_name=None,
                project_id=None,
                project_obj=None,
                project_repo=None,
                tenant=None,
                idempotency_key=None,
                max_runtime_seconds=None,
                skills_list=None,
                max_retries=None,
                model_override=None,
                provider_override=None,
                goal_mode=False,
                goal_max_turns=None,
                session_id=None,
            )
            created_review = True

        # Attach the authoritative review card as the parent of the
        # pre-created downstream QA/Release cards and demote todo/ready to
        # todo. Running/done downstream cards are NOT demoted here — that is a
        # containment concern surfaced by the scanner (R18).
        for child_id in payload.downstream_task_ids:
            child = kb.get_task(conn, child_id)
            if child is None:
                continue
            if not kb._link_exists(conn, review_task_id, child_id):
                kb._link_tasks_locked(conn, parent_id=review_task_id, child_id=child_id)
            # _link_tasks_locked already demotes ready→todo; make the
            # todo/ready→todo demotion explicit and idempotent.
            conn.execute(
                "UPDATE tasks SET status = 'todo' WHERE id = ? AND status IN ('todo','ready')",
                (child_id,),
            )

        # CAS the lane forward: bind the review card + head, bump revision.
        cur = conn.execute(
            "UPDATE kanban_effect_lanes "
            "SET review_task_id = ?, head_sha = ?, revision = revision + 1, updated_at = ? "
            "WHERE lane_id = ? AND revision = ?",
            (review_task_id, payload.head_sha, now, lane_id, lane_rev),
        )
        if cur.rowcount != 1:
            return ReconcileResult("stale", "LANE_CAS_LOST", effect_id)

        # Bind + terminally complete the effect (CAS on its revision).
        cur = conn.execute(
            "UPDATE kanban_effects "
            "SET state = ?, target_task_id = ?, lease_owner = NULL, "
            "    lease_expires_at = NULL, last_code = ?, revision = revision + 1, "
            "    updated_at = ? "
            "WHERE effect_id = ? AND revision = ?",
            (STATE_DONE, review_task_id, READY, now, effect_id, eff.revision),
        )
        if cur.rowcount != 1:
            return ReconcileResult("stale", "EFFECT_CAS_LOST", effect_id)

        kb._append_event(
            conn,
            review_task_id,
            "review_effect_reconciled",
            {
                "effect_id": effect_id,
                "lane_id": lane_id,
                "repo": payload.repo,
                "pr_number": payload.pr_number,
                "head_sha": payload.head_sha,
                "created_review": created_review,
                "downstream_task_ids": list(payload.downstream_task_ids),
            },
        )

    return ReconcileResult(
        "reconciled",
        READY,
        effect_id,
        review_task_id=review_task_id,
        created_review=created_review,
    )


def reconcile_effect(
    conn: sqlite3.Connection,
    effect_id: str,
    observer: GithubObserver,
    *,
    now: int,
    owner: str = "reconciler",
    lease_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS,
) -> ReconcileResult:
    """Public reconcile entry point.

    Delegates to :func:`_reconcile_effect_impl` and translates a head drift
    detected inside the write transaction (``_StaleHeadInsideTxn``) into a
    scheduled retry, so the caller never sees the internal sentinel and no
    partial review card is committed on a late head change (R14).
    """
    try:
        return _reconcile_effect_impl(
            conn,
            effect_id,
            observer,
            now=now,
            owner=owner,
            lease_ttl_seconds=lease_ttl_seconds,
        )
    except _StaleHeadInsideTxn:
        eff = get_effect(conn, effect_id)
        expected_rev = eff.revision if eff else 0
        state = _record_attempt_failure(
            conn,
            effect_id,
            code=STALE_HEAD,
            detail="head drifted between lease and reconcile",
            now=now,
            expected_revision=expected_rev,
        )
        return ReconcileResult("not_ready", STALE_HEAD, effect_id, detail=state)


# ---------------------------------------------------------------------------
# Downstream containment + legacy detection (alert only — R18, R19, R20, R21)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContainmentFinding:
    code: str
    task_id: Optional[str]
    detail: str


def scan_downstream_containment(
    conn: sqlite3.Connection,
    lane_id: str,
    *,
    observation: Optional[GithubObservation],
    approved_head_shas: Iterable[str] = (),
) -> list[ContainmentFinding]:
    """Report — never mutate — downstream cards that advanced without a
    compatible exact-head APPROVE, plus unbound/stale review conflicts.

    This is a readback for the offline harness. It emits alerts (R18/R19/R20);
    it does not auto-remediate, demote, or spawn anything.
    """
    from hermes_cli import kanban_db as kb

    if not effects_installed(conn):
        raise EffectsNotInstalledError("effect schema not installed on this DB")

    approved = {s.strip().lower() for s in approved_head_shas if s}
    findings: list[ContainmentFinding] = []
    lane = get_lane(conn, lane_id)
    if lane is None:
        return findings

    exact_head = observation.head_sha if observation is not None else lane.head_sha
    head_approved = bool(exact_head and exact_head in approved)

    review_task_id = lane.review_task_id
    if review_task_id is None:
        return findings

    # R20: a running review card whose reviewed head no longer matches the
    # current observation is stale — it should be drained, and no *parallel*
    # review card should be spawned (the lane binds exactly one).
    review = kb.get_task(conn, review_task_id)
    if (
        review is not None
        and review.status == "running"
        and observation is not None
        and lane.head_sha is not None
        and lane.head_sha != observation.head_sha
    ):
        findings.append(
            ContainmentFinding(
                CONTAINMENT_STALE_RUNNING_REVIEW,
                review_task_id,
                f"running review reviews {lane.head_sha[:8]} but head is "
                f"{observation.head_sha[:8]}; drain, do not spawn a parallel review",
            )
        )

    # R18: downstream cards that ran/finished without a compatible exact-head
    # APPROVE for the current head.
    for child_id in kb.child_ids(conn, review_task_id):
        child = kb.get_task(conn, child_id)
        if child is None:
            continue
        if child.status in {"running", "done"} and not head_approved:
            findings.append(
                ContainmentFinding(
                    CONTAINMENT_DOWNSTREAM_UNAPPROVED,
                    child_id,
                    f"downstream {child_id} is {child.status} but there is no "
                    f"exact-head APPROVE for {exact_head[:8] if exact_head else '?'}",
                )
            )
    return findings


def scan_unbound_review_conflict(
    conn: sqlite3.Connection,
    lane_id: str,
    candidate_review_task_ids: Iterable[str],
) -> list[ContainmentFinding]:
    """R19: report manually-created / unbound review cards that conflict with
    the lane's single authoritative review card. Alert only."""
    if not effects_installed(conn):
        raise EffectsNotInstalledError("effect schema not installed on this DB")
    lane = get_lane(conn, lane_id)
    if lane is None or lane.review_task_id is None:
        return []
    findings: list[ContainmentFinding] = []
    for tid in candidate_review_task_ids:
        if tid and tid != lane.review_task_id:
            findings.append(
                ContainmentFinding(
                    CONTAINMENT_UNBOUND_REVIEW_CONFLICT,
                    tid,
                    f"review card {tid} conflicts with authoritative "
                    f"{lane.review_task_id} for lane {lane_id}",
                )
            )
    return findings


_LEGACY_REVIEW_HINTS = re.compile(
    r"\b(needs?\s+review|please\s+review|review\s+required|awaiting\s+review|"
    r"ready\s+for\s+review|pr\s+review)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class LegacyReviewAlert:
    matched: bool
    hint: Optional[str]
    detail: str


def detect_legacy_review_text(text: Optional[str]) -> LegacyReviewAlert:
    """R21: legacy untyped, free-text review requests produce an ALERT ONLY.

    This never returns a payload, never creates a review card, and never
    touches the DB. It exists so a scanner can *surface* that a worker asked
    for review in prose (instead of the typed ``review_required`` payload) and
    a human can decide — the effect system refuses to infer authority from
    untyped text.
    """
    if not text:
        return LegacyReviewAlert(False, None, "no text")
    m = _LEGACY_REVIEW_HINTS.search(text)
    if not m:
        return LegacyReviewAlert(False, None, "no legacy review phrasing detected")
    return LegacyReviewAlert(
        True,
        m.group(0),
        "legacy untyped review request detected — alert only; no review card "
        "is auto-created. Re-emit as a typed review_required effect to bind a lane.",
    )
