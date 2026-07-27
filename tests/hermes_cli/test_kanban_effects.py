"""Tests for the repository-only, default-off review-required effect candidate.

Covers the R01–R21 intent documented in :mod:`hermes_cli.kanban_effects`:

* Producer validation + absence compatibility (R01–R03).
* Explicit installer / default-init leaves no tables (R04, R05).
* Deterministic canonical identities + dedup (R06–R08).
* Lane CAS, lease/expiry, fake-clock retry backoff and DLQ (R09–R12).
* Required-check policy missing, stale head, closed/conflict PR (R13–R15).
* Reconcile creates/reuses exactly one parentless exact-head review card and
  binds downstream QA/Release cards (R16, R17).
* Downstream containment, unbound-review conflict, stale running review,
  legacy untyped text (R18–R21).
* Concurrency (20 workers) exactly-once, and the no-nested-BEGIN regression.

Everything runs against a fresh temp DB and an in-memory (network-free) GitHub
observer, and uses a plain integer fake clock so no test sleeps.
"""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_effects as kfx


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


VALID_HEAD = "a" * 40
OTHER_HEAD = "b" * 40


def _payload(**over) -> dict:
    base = {
        "kind": "review_required",
        "repo": "acme/widget",
        "pr_number": 42,
        "head_sha": VALID_HEAD,
        "required_checks": ["ci"],
    }
    base.update(over)
    return base


def _ready_task(conn, title="t", assignee="worker") -> str:
    """Create a task; with no parents it lands in ``ready`` and is directly
    completable/blockable."""
    return kb.create_task(conn, title=title, assignee=assignee)


def _install(conn) -> None:
    kfx.install_effects_schema(conn)


def _make_all_due(conn) -> None:
    """Zero every effect's ``next_attempt_at`` so it is immediately due under
    the integer fake clock (the producer path stamps real wall-clock time)."""
    with kb.write_txn(conn):
        conn.execute("UPDATE kanban_effects SET next_attempt_at = 0")


def _observation(now: int, **over) -> kfx.GithubObservation:
    base = dict(
        repo="acme/widget",
        pr_number=42,
        head_sha=VALID_HEAD,
        is_open=True,
        has_conflicts=False,
        checks={"ci": "success"},
        required_check_policy=("ci",),
        observed_at=now,
    )
    base.update(over)
    return kfx.GithubObservation(**base)


def _observer(now: int, **over) -> kfx.InMemoryGithubObserver:
    obs = kfx.InMemoryGithubObserver()
    obs.set(_observation(now, **over))
    return obs


# ---------------------------------------------------------------------------
# R04 / R05 — installer only; default init has no tables
# ---------------------------------------------------------------------------


def test_default_db_has_no_effect_tables(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        assert not kfx.effects_installed(conn)
        # The tables genuinely do not exist.
        import sqlite3

        with pytest.raises(sqlite3.OperationalError):
            conn.execute("SELECT * FROM kanban_effects").fetchall()
        # Readback helpers refuse to operate on an un-installed DB.
        with pytest.raises(kfx.EffectsNotInstalledError):
            kfx.list_effects(conn)


def test_installer_creates_tables_idempotently(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        _install(conn)
        assert kfx.effects_installed(conn)
        # Idempotent: a second install does not raise or duplicate.
        _install(conn)
        assert kfx.effects_installed(conn)
        assert kfx.list_effects(conn) == []


def test_reinit_db_does_not_create_effect_tables(kanban_home: Path) -> None:
    # init_db re-runs migrations; it must never add the effect tables.
    kb.init_db()
    with kb.connect_closing() as conn:
        assert not kfx.effects_installed(conn)


# ---------------------------------------------------------------------------
# R06 / R07 — canonical identities
# ---------------------------------------------------------------------------


def test_canonical_lane_id_deterministic() -> None:
    a = kfx.canonical_lane_id("acme/widget", 42)
    b = kfx.canonical_lane_id("acme/widget", 42)
    c = kfx.canonical_lane_id("acme/widget", 43)
    assert a == b
    assert a != c
    assert a.startswith("lane_")


def test_canonical_payload_hash_key_order_independent() -> None:
    p1 = kfx.validate_review_required(
        {"kind": "review_required", "repo": "acme/widget", "pr_number": 42,
         "head_sha": VALID_HEAD, "required_checks": ["ci", "lint"]}
    )
    p2 = kfx.validate_review_required(
        {"required_checks": ["lint", "ci"], "head_sha": VALID_HEAD,
         "pr_number": 42, "repo": "acme/widget", "kind": "review_required"}
    )
    assert kfx.canonical_payload_hash(p1) == kfx.canonical_payload_hash(p2)
    # A different head is a different payload identity.
    p3 = kfx.validate_review_required(_payload(head_sha=OTHER_HEAD))
    assert kfx.canonical_payload_hash(p1) != kfx.canonical_payload_hash(p3)
    # downstream_task_ids are NOT part of payload identity (binding hint only).
    p4 = kfx.validate_review_required(_payload(downstream_task_ids=["t_x"]))
    p5 = kfx.validate_review_required(_payload(downstream_task_ids=["t_y"]))
    assert kfx.canonical_payload_hash(p4) == kfx.canonical_payload_hash(p5)


# ---------------------------------------------------------------------------
# R03 — producer validation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad, code",
    [
        ({"repo": "acme/widget", "pr_number": 1, "head_sha": VALID_HEAD}, "MISSING_KIND"),
        (_payload(kind="deploy"), "BAD_KIND"),
        (_payload(repo=""), "MISSING_REPO"),
        (_payload(repo="not-a-repo"), "BAD_REPO"),
        ({"kind": "review_required", "repo": "a/b", "head_sha": VALID_HEAD}, "MISSING_PR"),
        (_payload(pr_number=0), "BAD_PR"),
        (_payload(pr_number="x"), "BAD_PR"),
        ({"kind": "review_required", "repo": "a/b", "pr_number": 1}, "MISSING_HEAD"),
        (_payload(head_sha="abc"), "BAD_HEAD"),
        (_payload(head_sha="g" * 40), "BAD_HEAD"),
        (_payload(required_checks=[""]), "BAD_REQUIRED_CHECKS"),
    ],
)
def test_validate_review_required_rejects(bad, code) -> None:
    with pytest.raises(kfx.EffectValidationError) as ei:
        kfx.validate_review_required(bad)
    assert ei.value.code == code


def test_validate_normalizes_head_and_downstream() -> None:
    p = kfx.validate_review_required(
        _payload(head_sha=VALID_HEAD.upper(), downstream_task_ids=["t_a", "t_a", " t_b "])
    )
    assert p.head_sha == VALID_HEAD  # lowercased
    assert p.downstream_task_ids == ("t_a", "t_b")  # deduped + stripped


# ---------------------------------------------------------------------------
# R01 / R02 — producer records effect in the transition txn; absence unchanged
# ---------------------------------------------------------------------------


def test_complete_absent_payload_unchanged(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        _install(conn)
        tid = _ready_task(conn)
        assert kb.complete_task(conn, tid, summary="done") is True
        assert kb.get_task(conn, tid).status == "done"
        # No effect emitted when the payload is absent.
        assert kfx.list_effects(conn) == []


def test_complete_records_effect_in_same_txn(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        _install(conn)
        tid = _ready_task(conn)
        assert kb.complete_task(
            conn, tid, summary="done", review_required=_payload()
        ) is True
        assert kb.get_task(conn, tid).status == "done"
        effects = kfx.list_effects(conn)
        assert len(effects) == 1
        eff = effects[0]
        assert eff.state == kfx.STATE_PENDING
        assert eff.source_transition == kfx.TRANSITION_COMPLETE
        assert eff.source_task_id == tid
        assert eff.repo == "acme/widget" and eff.pr_number == 42
        # Lane + outbox rows exist and share the canonical identity.
        lane = kfx.get_lane(conn, eff.lane_id)
        assert lane is not None and lane.repo == "acme/widget"
        outbox = conn.execute("SELECT * FROM kanban_effect_outbox").fetchall()
        assert len(outbox) == 1 and outbox[0]["effect_id"] == eff.effect_id


def test_block_records_effect_in_same_txn(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        _install(conn)
        tid = _ready_task(conn)
        assert kb.block_task(
            conn, tid, reason="needs review", kind="needs_input",
            review_required=_payload(),
        ) is True
        assert kb.get_task(conn, tid).status == "blocked"
        effects = kfx.list_effects(conn)
        assert len(effects) == 1
        assert effects[0].source_transition == kfx.TRANSITION_BLOCK


def test_complete_invalid_payload_rejected_no_state_change(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        _install(conn)
        tid = _ready_task(conn)
        with pytest.raises(kfx.EffectValidationError):
            kb.complete_task(conn, tid, summary="x", review_required=_payload(head_sha="bad"))
        # Task untouched, no effect written.
        assert kb.get_task(conn, tid).status == "ready"
        assert kfx.list_effects(conn) == []


def test_producer_noop_when_schema_not_installed(kanban_home: Path) -> None:
    # Default-off: a valid payload against a normal DB validates but records
    # nothing and never crashes the completion.
    with kb.connect_closing() as conn:
        assert not kfx.effects_installed(conn)
        tid = _ready_task(conn)
        assert kb.complete_task(conn, tid, summary="done", review_required=_payload()) is True
        assert kb.get_task(conn, tid).status == "done"
        assert not kfx.effects_installed(conn)  # still no tables


# ---------------------------------------------------------------------------
# R08 — dedup
# ---------------------------------------------------------------------------


def test_duplicate_emissions_from_same_source_dedupe_to_one_effect(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        _install(conn)
        payload = kfx.validate_review_required(_payload())
        with kb.write_txn(conn):
            first = kfx.record_effect_locked(
                conn, source_task_id="src_1", source_transition=kfx.TRANSITION_COMPLETE,
                payload=payload, now=1,
            )
            replay = kfx.record_effect_locked(
                conn, source_task_id="src_1", source_transition=kfx.TRANSITION_COMPLETE,
                payload=payload, now=2,
            )
        assert first == replay
        assert len(kfx.list_effects(conn)) == 1


def test_distinct_sources_keep_distinct_effect_provenance(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        _install(conn)
        payload = kfx.validate_review_required(_payload())
        with kb.write_txn(conn):
            first = kfx.record_effect_locked(
                conn, source_task_id="src_1", source_transition=kfx.TRANSITION_COMPLETE,
                payload=payload, now=1,
            )
            second = kfx.record_effect_locked(
                conn, source_task_id="src_2", source_transition=kfx.TRANSITION_COMPLETE,
                payload=payload, now=1,
            )
        assert first != second
        assert {effect.source_task_id for effect in kfx.list_effects(conn)} == {"src_1", "src_2"}


# ---------------------------------------------------------------------------
# R13 / R14 / R15 — readiness gates
# ---------------------------------------------------------------------------


def _emit(conn, **over) -> str:
    """Emit an effect via the real producer path, then normalize its
    ``next_attempt_at`` to 0 so it is immediately due under the integer fake
    clock the reconcile tests use (the producer stamps real wall-clock time)."""
    tid = _ready_task(conn)
    kb.complete_task(conn, tid, summary="s", review_required=_payload(**over))
    eff_id = kfx.list_effects(conn)[0].effect_id
    with kb.write_txn(conn):
        conn.execute(
            "UPDATE kanban_effects SET next_attempt_at = 0, created_at = 0, updated_at = 0 "
            "WHERE effect_id = ?",
            (eff_id,),
        )
    return eff_id


def test_required_check_policy_missing(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        eff_id = _emit(conn)
        observer = _observer(now, required_check_policy=None)
        r = kfx.reconcile_effect(conn, eff_id, observer, now=now)
        assert r.status == "not_ready"
        assert r.code == kfx.REQUIRED_CHECK_POLICY_MISSING
        eff = kfx.get_effect(conn, eff_id)
        assert eff.state == kfx.STATE_PENDING and eff.attempts == 1
        assert eff.target_task_id is None
        # No review card was created.
        assert kfx.get_lane(conn, eff.lane_id).review_task_id is None


def test_stale_head_no_card(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        eff_id = _emit(conn)
        observer = _observer(now, head_sha=OTHER_HEAD)
        r = kfx.reconcile_effect(conn, eff_id, observer, now=now)
        assert r.code == kfx.STALE_HEAD and r.status == "not_ready"
        assert kfx.get_lane(conn, kfx.get_effect(conn, eff_id).lane_id).review_task_id is None


def test_closed_and_conflicting_pr_not_ready(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        eff_id = _emit(conn)
        r = kfx.reconcile_effect(conn, eff_id, _observer(now, is_open=False), now=now)
        assert r.code == kfx.PR_CLOSED
        # advance clock past backoff, then present a conflicting observation
        now2 = kfx.get_effect(conn, eff_id).next_attempt_at
        r2 = kfx.reconcile_effect(conn, eff_id, _observer(now2, has_conflicts=True), now=now2)
        assert r2.code == kfx.HEAD_CONFLICT


def test_checks_incomplete_and_failed(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        eff_id = _emit(conn)
        r = kfx.reconcile_effect(conn, eff_id, _observer(now, checks={"ci": "pending"}), now=now)
        assert r.code == kfx.CHECKS_INCOMPLETE
        now2 = kfx.get_effect(conn, eff_id).next_attempt_at
        r2 = kfx.reconcile_effect(conn, eff_id, _observer(now2, checks={"ci": "failure"}), now=now2)
        assert r2.code == kfx.CHECKS_FAILED


# ---------------------------------------------------------------------------
# R16 / R17 — reconcile creates/reuses one review card + binds downstream
# ---------------------------------------------------------------------------


def test_reconcile_creates_one_parentless_review_card(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        eff_id = _emit(conn)
        r = kfx.reconcile_effect(conn, eff_id, _observer(now), now=now)
        assert r.status == "reconciled" and r.created_review
        review = kb.get_task(conn, r.review_task_id)
        assert review is not None
        assert review.status == "ready"
        assert kb.parent_ids(conn, r.review_task_id) == []  # parentless
        eff = kfx.get_effect(conn, eff_id)
        assert eff.state == kfx.STATE_DONE and eff.target_task_id == r.review_task_id
        lane = kfx.get_lane(conn, eff.lane_id)
        assert lane.review_task_id == r.review_task_id
        assert lane.head_sha == VALID_HEAD


def test_reconcile_reuses_review_card_for_same_head(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        # Two effects for the same lane + head but different required_checks →
        # distinct payloads, same lane.
        t1 = _ready_task(conn)
        kb.complete_task(conn, t1, summary="s", review_required=_payload(required_checks=["ci"]))
        t2 = _ready_task(conn)
        kb.complete_task(conn, t2, summary="s", review_required=_payload(required_checks=["ci", "lint"]))
        effs = kfx.list_effects(conn)
        assert len(effs) == 2
        _make_all_due(conn)
        observer = _observer(now, checks={"ci": "success", "lint": "success"},
                             required_check_policy=("ci", "lint"))
        r1 = kfx.reconcile_effect(conn, effs[0].effect_id, observer, now=now)
        r2 = kfx.reconcile_effect(conn, effs[1].effect_id, observer, now=now)
        assert r1.review_task_id == r2.review_task_id
        assert r1.created_review is True and r2.created_review is False
        # Exactly one review card exists for the lane.
        cards = conn.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind='review_effect_reconciled'"
        ).fetchone()["c"]
        assert cards == 2  # two effects reconciled, one shared card


def test_reconcile_binds_and_demotes_downstream(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        qa = _ready_task(conn, title="qa", assignee="qa")
        rel = _ready_task(conn, title="release", assignee="release")
        assert kb.get_task(conn, qa).status == "ready"
        tid = _ready_task(conn)
        kb.complete_task(
            conn, tid, summary="s",
            review_required=_payload(downstream_task_ids=[qa, rel]),
        )
        eff_id = kfx.list_effects(conn)[0].effect_id
        _make_all_due(conn)
        r = kfx.reconcile_effect(conn, eff_id, _observer(now), now=now)
        assert r.status == "reconciled"
        # Review card is the parent of both downstream cards, both demoted.
        assert r.review_task_id in kb.parent_ids(conn, qa)
        assert r.review_task_id in kb.parent_ids(conn, rel)
        assert kb.get_task(conn, qa).status == "todo"
        assert kb.get_task(conn, rel).status == "todo"


# ---------------------------------------------------------------------------
# R18 / R19 / R20 — containment (alert only)
# ---------------------------------------------------------------------------


def test_downstream_containment_without_exact_head_approve(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        qa = _ready_task(conn, title="qa", assignee="qa")
        tid = _ready_task(conn)
        kb.complete_task(conn, tid, summary="s",
                         review_required=_payload(downstream_task_ids=[qa]))
        eff_id = kfx.list_effects(conn)[0].effect_id
        _make_all_due(conn)
        r = kfx.reconcile_effect(conn, eff_id, _observer(now), now=now)
        lane_id = kfx.get_effect(conn, eff_id).lane_id
        # Downstream card advanced to running with no approval for the head.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (qa,))
        findings = kfx.scan_downstream_containment(
            conn, lane_id, observation=_observation(now), approved_head_shas=[]
        )
        assert any(f.code == kfx.CONTAINMENT_DOWNSTREAM_UNAPPROVED for f in findings)
        # With an exact-head approval, no containment finding.
        ok = kfx.scan_downstream_containment(
            conn, lane_id, observation=_observation(now), approved_head_shas=[VALID_HEAD]
        )
        assert not any(f.code == kfx.CONTAINMENT_DOWNSTREAM_UNAPPROVED for f in ok)


def test_unbound_review_conflict_alert(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        eff_id = _emit(conn)
        kfx.reconcile_effect(conn, eff_id, _observer(now), now=now)
        lane_id = kfx.get_effect(conn, eff_id).lane_id
        findings = kfx.scan_unbound_review_conflict(conn, lane_id, ["t_manual"])
        assert findings and findings[0].code == kfx.CONTAINMENT_UNBOUND_REVIEW_CONFLICT


def test_stale_running_review_no_parallel(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        eff_id = _emit(conn)
        r = kfx.reconcile_effect(conn, eff_id, _observer(now), now=now)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (r.review_task_id,))
        lane_id = kfx.get_effect(conn, eff_id).lane_id
        # Head has moved on; the running review is stale.
        findings = kfx.scan_downstream_containment(
            conn, lane_id, observation=_observation(now, head_sha=OTHER_HEAD)
        )
        assert any(f.code == kfx.CONTAINMENT_STALE_RUNNING_REVIEW for f in findings)


# ---------------------------------------------------------------------------
# R21 — legacy untyped text is alert-only
# ---------------------------------------------------------------------------


def test_legacy_review_text_alert_only(kanban_home: Path) -> None:
    alert = kfx.detect_legacy_review_text("Please review this before merge")
    assert alert.matched and alert.hint
    assert kfx.detect_legacy_review_text("nothing to see") .matched is False
    # Purely a text scan — creates nothing, so a fresh DB stays empty.
    with kb.connect_closing() as conn:
        _install(conn)
        assert kfx.list_effects(conn) == []


# ---------------------------------------------------------------------------
# R10 / R11 / R12 — lease, retry backoff, DLQ (fake clock)
# ---------------------------------------------------------------------------


def test_lease_and_expiry(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        eff_id = _emit(conn)
        leased = kfx.lease_effect(conn, eff_id, owner="w1", now=now, lease_ttl_seconds=100)
        assert leased is not None and leased.state == kfx.STATE_LEASED
        # A second lease before expiry loses.
        assert kfx.lease_effect(conn, eff_id, owner="w2", now=now, lease_ttl_seconds=100) is None
        # After the lease expires it can be re-acquired.
        assert kfx.lease_effect(conn, eff_id, owner="w2", now=now + 101) is not None


def test_retry_backoff_and_dlq_fake_clock(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        # Emit directly with a small max_attempts for a fast DLQ.
        tid = _ready_task(conn)
        payload = kfx.validate_review_required(_payload())
        with kb.write_txn(conn):
            eff_id = kfx.record_effect_locked(
                conn, source_task_id=tid, source_transition=kfx.TRANSITION_COMPLETE,
                payload=payload, now=now, max_attempts=3,
            )
        prev_next = -1
        state = None
        for _ in range(10):
            observer = _observer(now, required_check_policy=None)  # always not ready
            kfx.reconcile_effect(conn, eff_id, observer, now=now)
            eff = kfx.get_effect(conn, eff_id)
            state = eff.state
            if state == kfx.STATE_DLQ:
                break
            # Backoff strictly advances the next attempt time.
            assert eff.next_attempt_at > now
            assert eff.next_attempt_at > prev_next
            prev_next = eff.next_attempt_at
            now = eff.next_attempt_at
        assert state == kfx.STATE_DLQ
        assert kfx.get_effect(conn, eff_id).attempts == 3


# ---------------------------------------------------------------------------
# Concurrency — 20 workers, exactly once (R08 / R09 / R16 under load)
# ---------------------------------------------------------------------------


def test_20_concurrent_producers_one_effect(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        _install(conn)
    payload = kfx.validate_review_required(_payload())

    def worker(i: int) -> None:
        with kb.connect_closing() as c:
            with kb.write_txn(c):
                kfx.record_effect_locked(
                    c, source_task_id="shared-source",
                    source_transition=kfx.TRANSITION_COMPLETE, payload=payload,
                )

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    with kb.connect_closing() as conn:
        assert len(kfx.list_effects(conn)) == 1


def test_20_concurrent_reconcilers_one_review_card(kanban_home: Path) -> None:
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        eff_id = _emit(conn)

    results: list[kfx.ReconcileResult] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        observer = _observer(now)
        with kb.connect_closing() as c:
            r = kfx.reconcile_effect(c, eff_id, observer, now=now, owner=f"w{i}")
        with lock:
            results.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    reconciled = [r for r in results if r.status == "reconciled"]
    assert len(reconciled) == 1
    with kb.connect_closing() as conn:
        eff = kfx.get_effect(conn, eff_id)
        assert eff.state == kfx.STATE_DONE
        # Exactly one review card was ever created for the lane.
        cards = conn.execute(
            "SELECT COUNT(*) c FROM task_events WHERE kind='review_effect_reconciled'"
        ).fetchone()["c"]
        assert cards == 1


# ---------------------------------------------------------------------------
# No-nested-BEGIN regression — locked helpers compose in an outer txn
# ---------------------------------------------------------------------------


def test_locked_helpers_compose_without_nested_begin(kanban_home: Path) -> None:
    with kb.connect_closing() as conn:
        # Public wrappers still behave as before.
        parent = kb.create_task(conn, title="p", assignee="worker")
        child = kb.create_task(conn, title="c", assignee="worker")
        kb.link_tasks(conn, parent_id=parent, child_id=child)
        assert parent in kb.parent_ids(conn, child)

        # The transaction-internal helpers compose inside ONE outer write txn
        # with no "cannot start a transaction within a transaction" error.
        with kb.write_txn(conn):
            new_id = kb._new_task_id()
            kb._create_task_locked(
                conn, task_id=new_id, now=1234, title="inner", body=None,
                assignee="worker", priority=0, created_by="test",
                workspace_kind="scratch", workspace_path=None, branch_name=None,
                project_id=None, project_obj=None, project_repo=None, tenant=None,
                idempotency_key=None, max_runtime_seconds=None, skills_list=None,
                max_retries=None, model_override=None, provider_override=None,
                goal_mode=False, goal_max_turns=None, session_id=None,
                parents=(), triage=False, initial_status="running",
            )
            kb._link_tasks_locked(conn, parent_id=parent, child_id=new_id)
        assert kb.get_task(conn, new_id) is not None
        assert parent in kb.parent_ids(conn, new_id)


def test_reconcile_never_holds_txn_during_observation(kanban_home: Path) -> None:
    """The observer is consulted strictly before the reconcile write txn.

    We assert this by having the observer verify that no write transaction is
    in progress on the connection at observe() time.
    """
    now = 1000
    with kb.connect_closing() as conn:
        _install(conn)
        eff_id = _emit(conn)

        observed = {"in_txn": None}

        class _AssertObserver:
            def observe(self, repo, pr_number):
                observed["in_txn"] = conn.in_transaction
                return _observation(now)

        r = kfx.reconcile_effect(conn, eff_id, _AssertObserver(), now=now)
        assert r.status == "reconciled"
        assert observed["in_txn"] is False  # no DB txn held during observation
