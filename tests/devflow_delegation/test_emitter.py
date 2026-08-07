import json

from devflow_delegation.emitter import DelegationEmitter, DelegationResult
from events.bus import EventBus
from tests.devflow_delegation.conftest import make_delegate_kwargs


def _bus_events(bus):
    rows = bus._get_conn().execute("SELECT event_type, payload FROM events ORDER BY timestamp").fetchall()
    return [(r["event_type"], json.loads(r["payload"])) for r in rows]


def test_queued_full_path(emitter, hermes_root):
    r = emitter.delegate(mode="queue", **make_delegate_kwargs())
    assert r.status == "queued" and r.reason == "queued"
    assert r.request_id and r.request_id.startswith("dwr_")
    row = emitter.ledger.get_request(r.request_id)
    assert row["state"] == "REQUESTED"
    assert row["idempotency_key"] == f"auto:{r.fingerprint}"
    files = list((hermes_root / "mailbox" / "devflow" / "inbox").glob("*.json"))
    assert len(files) == 1
    env = json.loads(files[0].read_text(encoding="utf-8"))
    assert env["request_id"] == r.request_id
    assert not list((hermes_root / "mailbox" / "devflow" / "inbox").glob("*.tmp")), "no tmp residue"
    types = [t for t, _ in _bus_events(emitter.bus)]
    assert "devflow.work_requested" in types


def test_invalid_request_declined_without_side_effects(emitter, hermes_root):
    r = emitter.delegate(mode="queue", **make_delegate_kwargs(evidence=[]))
    assert r.status == "declined"
    assert r.reason.startswith("invalid:") and "missing_evidence" in r.reason
    assert emitter.ledger.summary_counts()["total"] == 0
    assert not (hermes_root / "mailbox" / "devflow" / "inbox").exists() or not list(
        (hermes_root / "mailbox" / "devflow" / "inbox").glob("*.json"))


def test_off_allowlist_target_declined_and_recorded(emitter):
    r = emitter.delegate(mode="queue", **make_delegate_kwargs(target={"repo": "rogue", "subsystem": "x"}))
    assert r.status == "declined" and r.reason == "target_unresolved"
    assert emitter.ledger.summary_counts()["by_state"].get("DECLINED") == 1


def test_missing_target_declined(emitter):
    r = emitter.delegate(mode="queue", **make_delegate_kwargs(target=None))
    assert r.status == "declined" and r.reason == "target_unresolved"


def test_duplicate_appends_evidence_single_row(emitter):
    r1 = emitter.delegate(mode="queue", **make_delegate_kwargs())
    r2 = emitter.delegate(mode="queue", **make_delegate_kwargs(
        evidence=[{"kind": "test_failure", "ref": "tests/test_health.py", "summary": "timeout AGAIN"}]))
    assert r2.status == "duplicate" and r2.request_id == r1.request_id
    assert emitter.ledger.evidence_count(r1.request_id) == 1
    assert emitter.ledger.summary_counts()["total"] == 1
    files = list((emitter.inbox_dir).glob("*.json"))
    assert len(files) == 1
    types = [t for t, _ in _bus_events(emitter.bus)]
    assert types.count("devflow.work_duplicate") == 1


def test_dry_run_classifies_without_side_effects(emitter, hermes_root):
    r = emitter.delegate(**make_delegate_kwargs())  # default mode = dry_run
    assert r.status == "queued" and r.reason == "dry_run"
    assert r.request_id is None and r.fingerprint
    assert emitter.ledger.summary_counts()["total"] == 0
    inbox = hermes_root / "mailbox" / "devflow" / "inbox"
    assert not inbox.exists() or not list(inbox.glob("*.json"))
    assert _bus_events(emitter.bus) == []


def test_rate_limit_suppresses_with_one_summarized_alert(emitter, hermes_root):
    (hermes_root / "devflow").mkdir(parents=True, exist_ok=True)
    (hermes_root / "devflow" / "policy.json").write_text(
        json.dumps({"critic": {"mode": "queue", "max_per_window": 2}}), encoding="utf-8")
    em = DelegationEmitter()  # rebuild to pick up overrides
    a = em.delegate(**make_delegate_kwargs(title="Problem A"))
    b = em.delegate(**make_delegate_kwargs(title="Problem B"))
    c = em.delegate(**make_delegate_kwargs(title="Problem C"))
    assert (a.status, b.status) == ("queued", "queued")
    assert c.status == "suppressed" and c.reason == "rate_limit_source"
    suppressed_events = [t for t, _ in _bus_events(em.bus) if t == "devflow.work_suppressed"]
    assert len(suppressed_events) == 1, "exactly one summarized alert"
    d = em.delegate(**make_delegate_kwargs(title="Problem D"))
    assert d.status == "suppressed"
    assert len([t for t, _ in _bus_events(em.bus) if t == "devflow.work_suppressed"]) == 1


def test_cooldown_suppresses_reopen_of_declined_fingerprint(emitter, hermes_root):
    # The min_confidence floor makes the first (low-confidence) call terminalize
    # as DECLINED with a REAL fingerprint (a resolved on-allowlist target),
    # exercising the below_confidence decline path. A re-open of that SAME
    # fingerprint inside the declined-cooldown window must then be suppressed
    # with reason "cooldown_declined" (single-sourced at emitter.py:174 — the
    # only path that yields it, so it is a positive control for the 4c gate).
    # NOTE: an off-allowlist target short-circuits at target resolution (step 2)
    # and never reaches the cooldown gate — that was the prior false-green here.
    (hermes_root / "devflow").mkdir(parents=True, exist_ok=True)
    (hermes_root / "devflow" / "policy.json").write_text(
        json.dumps({"critic": {"mode": "queue", "min_confidence": 0.9,
                               "cooldown_declined_hours": 24}}), encoding="utf-8")
    em = DelegationEmitter()
    r1 = em.delegate(**make_delegate_kwargs(confidence=0.5))
    assert r1.status == "declined" and r1.reason == "below_confidence"
    r2 = em.delegate(**make_delegate_kwargs(confidence=0.95))
    assert r2.status == "suppressed" and r2.reason == "cooldown_declined"
    assert r2.fingerprint


def test_declined_fingerprint_reopens_after_cooldown_without_raising(emitter, hermes_root):
    # Regression: the auto idempotency key is auto:{fingerprint}, so it is stored
    # on the terminal DECLINED row. Dedup at 4b lets terminal rows through so a
    # fingerprint may re-open once its cooldown expires (policy: "DECLINED rows
    # gate re-opens"). With the cooldown elapsed, the re-open reaches the queue
    # insert and MUST NOT collide on the UNIQUE idempotency_key — a raised
    # sqlite3.IntegrityError would escape delegate(), violating "never raise for
    # policy outcomes". cooldown_declined_hours=0 => the window is already past.
    (hermes_root / "devflow").mkdir(parents=True, exist_ok=True)
    (hermes_root / "devflow" / "policy.json").write_text(
        json.dumps({"critic": {"mode": "queue", "min_confidence": 0.9,
                               "cooldown_declined_hours": 0}}), encoding="utf-8")
    em = DelegationEmitter()
    r1 = em.delegate(**make_delegate_kwargs(confidence=0.5))
    assert r1.status == "declined" and r1.reason == "below_confidence"
    r2 = em.delegate(**make_delegate_kwargs(confidence=0.95))
    assert r2.status == "queued" and r2.reason == "queued"
    assert r2.request_id and r2.request_id != r1.request_id
    # exactly one active REQUESTED row for the re-opened fingerprint
    assert em.ledger.summary_counts()["by_state"].get("REQUESTED") == 1


def test_explicit_idempotency_key_dedups(emitter):
    kw = make_delegate_kwargs(idempotency_key="critic:gw-timeout:2026-08-06:v1")
    r1 = emitter.delegate(mode="queue", **kw)
    r2 = emitter.delegate(mode="queue", **make_delegate_kwargs(
        idempotency_key="critic:gw-timeout:2026-08-06:v1", title="A different framing"))
    assert r1.status == "queued"
    assert r2.status == "duplicate" and r2.request_id == r1.request_id


def test_reconcile_rewrites_missing_envelope(emitter, hermes_root):
    r = emitter.delegate(mode="queue", **make_delegate_kwargs())
    inbox = hermes_root / "mailbox" / "devflow" / "inbox"
    only = next(inbox.glob("*.json"))
    only.unlink()
    counts = emitter.reconcile()
    assert counts["rewritten"] == 1
    assert any(r.request_id in f.name for f in inbox.glob("*.json"))


def test_reconcile_adopts_orphan_envelope(emitter, hermes_root):
    r = emitter.delegate(mode="queue", **make_delegate_kwargs())
    row = emitter.ledger.get_request(r.request_id)
    env = json.loads(row["envelope_json"])
    inbox = hermes_root / "mailbox" / "devflow" / "inbox"
    for f in inbox.glob("*.json"):
        f.unlink()
    emitter.ledger.close()  # release the WAL handle so the db can be deleted (Windows)
    for f in (hermes_root / "devflow").glob("delegation_ledger.db*"):
        f.unlink()
    em2 = DelegationEmitter()
    orphan = inbox / "orphan_DEVFLOW_WORK_REQUEST.json"
    orphan.write_text(json.dumps(env), encoding="utf-8")
    counts = em2.reconcile()
    assert counts["adopted"] == 1
    assert em2.ledger.get_request(r.request_id) is not None


def test_missing_allowlist_fails_closed(emitter, allowlist_file):
    allowlist_file.unlink()
    em = DelegationEmitter()
    r = em.delegate(mode="queue", **make_delegate_kwargs())
    assert r.status == "declined" and r.reason == "target_unresolved"
