import json
import os
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from intent_applier import IdempotencyTracker, IntentApplier, JobOpsClientPermanentError, JobOpsClientTransientError
from pipeline_state import PipelineManager


VALID_INTENT_PAYLOAD = {
    "message_id": "msg-1",
    "idempotency_key": "tracker-intent:approval_intent:linkedin-1:approved",
    "protocol_version": "2.0-draft",
    "type": "APPROVAL_INTENT",
    "from": "main",
    "to": "tracker",
    "job_id": "linkedin-1",
    "timestamp": "2026-04-25T10:00:00Z",
    "correlation_id": "corr-1",
    "attempt": 1,
    "max_attempts": 3,
    "lease_timeout_seconds": 300,
    "reply_expected": False,
    "intent_only": True,
    "payload": {
        "requested_stage": "approved",
        "actor_id": "diego",
        "source": "legacy_dashboard",
        "notes": "matches",
        "metadata": {},
    },
}


def write_intent(inbox: Path, name: str, payload: dict) -> Path:
    p = inbox / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


@pytest.fixture
def mailbox(tmp_path):
    inbox = tmp_path / "inbox"
    processed = tmp_path / "processed"
    partial = tmp_path / "partial"
    dead_letter = tmp_path / "dead-letter"
    inbox.mkdir()
    return {"inbox": inbox, "processed": processed, "partial": partial, "dead_letter": dead_letter}


@pytest.fixture
def pipeline_path(tmp_path):
    p = tmp_path / "pipeline.json"
    p.write_text(json.dumps({
        "jobs": [{
            "job_id": "linkedin-1",
            "stage": "review",
            "title": "VP AI",
            "company": "Acme",
            "history": [],
        }],
        "stats": {},
    }))
    return p


@pytest.fixture
def applier(tmp_path, mailbox, pipeline_path):
    mgr = PipelineManager(path=pipeline_path)
    jobops = MagicMock()
    jobops.post_legacy_stage.return_value = {"success": True}
    tracker = IdempotencyTracker(tmp_path / "applier_state.db")
    return IntentApplier(
        inbox_dir=mailbox["inbox"],
        processed_dir=mailbox["processed"],
        partial_dir=mailbox["partial"],
        dead_letter_dir=mailbox["dead_letter"],
        pipeline_manager=mgr,
        jobops_client=jobops,
        idempotency=tracker,
    ), jobops, mgr


def _make_applier(tmp_path, mailbox, pipeline_path, *, job_state_reader=None, jobops=None, **kwargs):
    """Build an IntentApplier with optional pre-flight reader / config overrides.

    Returns (applier, jobops, mgr) mirroring the ``applier`` fixture shape when
    the caller wants the jobops mock; callers that ignore it can just take [0].
    """
    mgr = PipelineManager(path=pipeline_path)
    if jobops is None:
        jobops = MagicMock()
        jobops.post_legacy_stage.return_value = {"success": True}
    tracker = IdempotencyTracker(tmp_path / "applier_state.db")
    a = IntentApplier(
        inbox_dir=mailbox["inbox"],
        processed_dir=mailbox["processed"],
        partial_dir=mailbox["partial"],
        dead_letter_dir=mailbox["dead_letter"],
        pipeline_manager=mgr,
        jobops_client=jobops,
        idempotency=tracker,
        job_state_reader=job_state_reader,
        **kwargs,
    )
    a._jobops_mock = jobops
    a._mgr = mgr
    return a


class TestApplierHappyPath:
    def test_dual_write_success_moves_to_processed(self, mailbox, applier, pipeline_path):
        a, jobops, _mgr = applier
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        outcome = a.apply_one(f)
        assert outcome == "applied"
        # File moved
        assert not f.exists()
        assert (mailbox["processed"] / "intent.json").exists()
        # Pipeline.json updated
        data = json.loads(pipeline_path.read_text())
        job = next(j for j in data["jobs"] if j["job_id"] == "linkedin-1")
        assert job["stage"] == "approved"
        last = job["history"][-1]
        assert last["source"] == "tracker_mailbox"
        assert last["agent"] == "diego"
        # JobOps called
        jobops.post_legacy_stage.assert_called_once()
        call = jobops.post_legacy_stage.call_args
        assert call.kwargs["stage"] == "approved"
        assert call.kwargs["actor_id"] == "diego"
        assert call.kwargs["source"] == "tracker_mailbox"

    def test_idempotency_skip(self, mailbox, applier):
        a, jobops, _mgr = applier
        f1 = write_intent(mailbox["inbox"], "intent1.json", VALID_INTENT_PAYLOAD)
        a.apply_one(f1)
        assert jobops.post_legacy_stage.call_count == 1
        # Second file with same idempotency_key -> skip
        f2 = write_intent(mailbox["inbox"], "intent2.json", VALID_INTENT_PAYLOAD)
        outcome = a.apply_one(f2)
        assert outcome == "skipped_idempotent"
        assert jobops.post_legacy_stage.call_count == 1
        assert (mailbox["processed"] / "intent2.json").exists()


class TestApplierFailures:
    def test_pipeline_succeeds_postgres_5xx_goes_to_partial(self, mailbox, applier):
        a, jobops, _mgr = applier
        jobops.post_legacy_stage.side_effect = JobOpsClientTransientError("502")
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        outcome = a.apply_one(f)
        assert outcome == "partial"
        assert (mailbox["partial"] / "intent.json").exists()

    def test_pipeline_succeeds_postgres_4xx_goes_to_dead_letter(self, mailbox, applier):
        a, jobops, _mgr = applier
        jobops.post_legacy_stage.side_effect = JobOpsClientPermanentError("400 invalid stage")
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        outcome = a.apply_one(f)
        assert outcome == "dead_lettered"
        assert (mailbox["dead_letter"] / "intent.json").exists()
        sidecar = mailbox["dead_letter"] / "intent.json.error.json"
        assert sidecar.exists()
        info = json.loads(sidecar.read_text())
        assert info["error_class"] == "JobOpsClientPermanentError"

    def test_corrupt_json_goes_to_dead_letter(self, mailbox, applier):
        a, _jobops, _mgr = applier
        f = mailbox["inbox"] / "bad.json"
        f.write_text("{not valid", encoding="utf-8")
        outcome = a.apply_one(f)
        assert outcome == "dead_lettered"
        assert (mailbox["dead_letter"] / "bad.json").exists()

    def test_pipeline_write_failure_goes_to_dead_letter(self, mailbox, applier):
        """If PipelineManager.update_stage raises (e.g. lock timeout, disk full),
        the intent is treated as a permanent failure and dead-lettered."""
        a, _jobops, mgr = applier
        # Replace update_stage with one that always raises
        from unittest.mock import patch
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        with patch.object(mgr, "update_stage", side_effect=RuntimeError("simulated lock timeout")):
            outcome = a.apply_one(f)
        assert outcome == "dead_lettered"
        assert (mailbox["dead_letter"] / "intent.json").exists()
        sidecar = mailbox["dead_letter"] / "intent.json.error.json"
        assert sidecar.exists()
        info = json.loads(sidecar.read_text())
        assert info["error_class"] == "RuntimeError"
        assert "simulated lock timeout" in info["error_message"]

    def test_circuit_breaker_open_only_writes_pipeline(self, mailbox, applier):
        a, jobops, _mgr = applier
        # Trip the breaker
        jobops.post_legacy_stage.side_effect = JobOpsClientTransientError("503")
        for i in range(5):
            f = write_intent(
                mailbox["inbox"], f"int-{i}.json",
                {**VALID_INTENT_PAYLOAD,
                 "message_id": f"m-{i}",
                 "idempotency_key": f"key-{i}"},
            )
            a.apply_one(f)
        # All went to partial
        assert len(list(mailbox["partial"].iterdir())) == 5
        # 6th intent: breaker is open -> only pipeline write happens; no JobOps call
        jobops.post_legacy_stage.reset_mock()
        f = write_intent(mailbox["inbox"], "after.json",
                         {**VALID_INTENT_PAYLOAD, "message_id": "m-x", "idempotency_key": "key-x"})
        outcome = a.apply_one(f)
        assert outcome == "partial"
        jobops.post_legacy_stage.assert_not_called()


class TestScanInbox:
    def test_scan_inbox_processes_all_files(self, mailbox, applier):
        a, jobops, _mgr = applier
        for i in range(3):
            # Filename pattern matches what JobOps writes: <ts>_<TYPE>_main.json
            write_intent(mailbox["inbox"], f"20260425T1000{i:02d}_APPROVAL_INTENT_main.json",
                         {**VALID_INTENT_PAYLOAD,
                          "message_id": f"m-{i}",
                          "idempotency_key": f"key-{i}",
                          "job_id": f"linkedin-{i+1}"})
        # PipelineManager.update_stage upserts unknown jobs, so this works.
        outcomes = a.scan_inbox()
        assert len(outcomes) == 3
        assert all(o in {"applied", "partial", "dead_lettered"} for o in outcomes.values())

    def test_scan_inbox_ignores_non_intent_files(self, mailbox, applier):
        """Tracker inbox is shared with sentinel VIP_DISCOVERY, scout job_discovery,
        etc. The applier must NOT consume those — they belong to the tracker LLM
        cron. Bug discovered 2026-04-25: VIP_DISCOVERY messages were being
        dead-lettered because the glob pattern was too broad.
        """
        a, jobops, _mgr = applier
        # An intent file (should be processed)
        write_intent(
            mailbox["inbox"], "20260425T100000_STATE_TRANSITION_INTENT_main.json",
            VALID_INTENT_PAYLOAD,
        )
        # A VIP_DISCOVERY file from sentinel (should be left alone)
        vip_path = mailbox["inbox"] / "20260425T100100Z_VIP_DISCOVERY_sentinel_xyz.json"
        vip_path.write_text(json.dumps({"type": "VIP_DISCOVERY", "from": "sentinel"}), encoding="utf-8")
        # A scout discovery file (should be left alone)
        scout_path = mailbox["inbox"] / "test-discovery.json"
        scout_path.write_text(json.dumps({"type": "job_discovery", "jobs": []}), encoding="utf-8")

        outcomes = a.scan_inbox()

        # Only the intent file got processed
        assert len(outcomes) == 1
        assert "20260425T100000_STATE_TRANSITION_INTENT_main.json" in outcomes
        # Non-intent files are still in inbox (not moved to dead-letter or processed)
        assert vip_path.exists()
        assert scout_path.exists()
        # Dead-letter dir should be empty (no false positives)
        if mailbox["dead_letter"].exists():
            assert list(mailbox["dead_letter"].iterdir()) == []


def _pipeline_updates(inbox: Path) -> list[Path]:
    return sorted(inbox.glob("*_PIPELINE_UPDATE_*.json"))


class TestCanonicalEmission:
    """Step 3b: every intent is mirrored as a PIPELINE_UPDATE into the tracker
    mailbox inbox, so the tracker LLM applies it to its canonical
    profiles/tracker/workspace/pipeline.json (2026-07-12 approval-loop fix)."""

    def test_happy_path_emits_pipeline_update(self, mailbox, applier):
        a, jobops, _mgr = applier
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "applied"

        files = _pipeline_updates(mailbox["inbox"])
        assert len(files) == 1
        body = json.loads(files[0].read_text(encoding="utf-8"))
        assert body["type"] == "PIPELINE_UPDATE"
        assert body["from"] == "operator"
        assert body["to"] == "tracker"
        assert body["job_id"] == "linkedin-1"
        assert body["correlation_id"] == VALID_INTENT_PAYLOAD["message_id"]
        assert body["payload"]["to_stage"] == "approved"
        md = body["payload"]["metadata"]
        assert md["actor_id"] == "diego"
        assert md["original_source"] == "legacy_dashboard"
        assert md["idempotency_key"] == VALID_INTENT_PAYLOAD["idempotency_key"]
        assert md["emitted_by"] == "tracker-intent-applier"
        assert "pipeline_manager_error" not in md

    def test_emission_is_not_reconsumed_by_applier(self, mailbox, applier):
        """The emitted filename must never match the *_INTENT_* glob — Windows
        globbing is case-insensitive, so this also guards the infix choice."""
        a, _jobops, _mgr = applier
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        a.apply_one(f)
        emitted = _pipeline_updates(mailbox["inbox"])
        assert len(emitted) == 1
        assert "_intent_" not in emitted[0].name.lower()

        outcomes = a.scan_inbox()
        assert outcomes == {}
        assert emitted[0].exists()

    def test_partial_jobops_failure_still_emits(self, mailbox, applier):
        a, jobops, _mgr = applier
        jobops.post_legacy_stage.side_effect = JobOpsClientTransientError("boom")
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "partial"
        assert len(_pipeline_updates(mailbox["inbox"])) == 1

    def test_pipeline_manager_failure_emits_flagged(self, tmp_path, mailbox):
        mgr = MagicMock()
        mgr.update_stage.side_effect = RuntimeError("disk full")
        jobops = MagicMock()
        tracker = IdempotencyTracker(tmp_path / "applier_state.db")
        a = IntentApplier(
            inbox_dir=mailbox["inbox"],
            processed_dir=mailbox["processed"],
            partial_dir=mailbox["partial"],
            dead_letter_dir=mailbox["dead_letter"],
            pipeline_manager=mgr,
            jobops_client=jobops,
            idempotency=tracker,
        )
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "dead_lettered"

        files = _pipeline_updates(mailbox["inbox"])
        assert len(files) == 1
        body = json.loads(files[0].read_text(encoding="utf-8"))
        assert "disk full" in body["payload"]["metadata"]["pipeline_manager_error"]
        # JobOps must not have been reached after the dead-letter
        jobops.post_legacy_stage.assert_not_called()

    def test_emission_failure_reports_false_without_raising(self, tmp_path, mailbox, applier):
        """A failed mirror must be REPORTABLE, not merely survivable.

        CHANGED 2026-08-24 (was test_emission_failure_never_raises). Not raising
        is still required -- a mirror failure must never crash the applier
        thread -- but swallowing it into ``None`` left every caller unable to
        tell a durable write from a lost one, which is the whole hazard. The
        contract is now: never raise, always report.
        """
        from intent_applier.parser import parse_intent_file

        a, _jobops, _mgr = applier
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        msg = parse_intent_file(f)

        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        a.inbox_dir = blocker / "sub"  # parent is a file -> any write fails

        assert a._emit_canonical_pipeline_update(msg) is False   # never raises

    def test_emission_success_reports_true(self, mailbox, applier):
        from intent_applier.parser import parse_intent_file

        a, _jobops, _mgr = applier
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        msg = parse_intent_file(f)
        assert a._emit_canonical_pipeline_update(msg) is True

    def test_emission_creates_a_missing_inbox_dir(self, tmp_path, mailbox, applier):
        """The mirror is the durable write; it must not lose to a missing dir.

        Every other writer here mkdirs its destination (_move_to does), but the
        mirror wrote its .tmp straight into inbox_dir. An absent inbox is a
        FileNotFoundError, i.e. exactly the swallowed-loss shape.
        """
        from intent_applier.parser import parse_intent_file

        a, _jobops, _mgr = applier
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        msg = parse_intent_file(f)

        a.inbox_dir = tmp_path / "vanished" / "inbox"   # does not exist
        assert a._emit_canonical_pipeline_update(msg) is True
        assert len(_pipeline_updates(a.inbox_dir)) == 1


class TestMirrorFailureDoesNotConsumeTheIntent:
    """THE DURABILITY INVARIANT, enforced rather than merely asserted.

    applier.py Step 2b states it: "NO INTENT IS EVER CONSUMED WITHOUT A
    CANONICAL WRITE". Before this change the mirror was best-effort -- it was
    always *called*, but a failed write was swallowed and the intent went on to
    burn its idempotency key and move to processed/ anyway. That makes the loss
    silent AND permanent: is_applied() then short-circuits every retry, so the
    operator's decision cannot be re-driven by any means.

    A failed mirror must instead route to partial/ with the key unburned, which
    is the lane redrive_partials() already retries with backoff and, once capped
    (production sets TRACKER_APPLIER_REDRIVE_GIVE_UP_ATTEMPTS=5 -- read the env,
    not the code default of 0), PartialBacklogMonitor alerts on. The intent ends
    up loud and recoverable instead of silently gone.
    """

    @staticmethod
    def _break_the_mirror(a, tmp_path):
        blocker = tmp_path / "mirror-blocker"
        blocker.write_text("x", encoding="utf-8")
        a.inbox_dir = blocker / "sub"   # parent is a file -> mkdir and write fail

    def test_satisfied_branch_does_not_burn_the_key_when_the_mirror_fails(
        self, tmp_path, mailbox, pipeline_path
    ):
        """The pre-flight's ONLY durable write is the mirror.

        It deliberately skips the :4100 POST, so if the mirror is also lost the
        intent produces nothing at all -- the exact shape that destroyed five
        operator approvals on 2026-08-23.
        """
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: "approved_for_tailor")
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        self._break_the_mirror(a, tmp_path)

        assert a.apply_one(f) == "partial"
        assert not a.idempotency.is_applied(VALID_INTENT_PAYLOAD["idempotency_key"])
        assert not (mailbox["processed"] / "intent.json").exists()
        assert (mailbox["partial"] / "intent.json").exists()
        a._jobops_mock.post_legacy_stage.assert_not_called()

    def test_normal_path_does_not_burn_the_key_when_the_mirror_fails(
        self, tmp_path, mailbox, pipeline_path
    ):
        """Step 3 and step 4 both succeeding is NOT durability.

        postgres-sync projects canonical ONTO Postgres every 15 minutes, so a
        Postgres row canonical never learned about is reverted within the cycle.
        Losing the mirror loses the approval even when the POST returned 2xx.
        """
        a = _make_applier(tmp_path, mailbox, pipeline_path)
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        self._break_the_mirror(a, tmp_path)

        assert a.apply_one(f) == "partial"
        assert not a.idempotency.is_applied(VALID_INTENT_PAYLOAD["idempotency_key"])
        assert not (mailbox["processed"] / "intent.json").exists()
        assert (mailbox["partial"] / "intent.json").exists()
        # The :4100 POST is never reached: a lost mirror is decided before it.
        a._jobops_mock.post_legacy_stage.assert_not_called()

    def test_redrive_picks_the_mirror_failure_back_up(self, tmp_path, mailbox, pipeline_path):
        """partial/ is only the right home if the retry lane actually drains it."""
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: "approved_for_tailor",
                          redrive_base_backoff=0.0)
        f = write_intent(mailbox["inbox"], "intent_INTENT_x.json", VALID_INTENT_PAYLOAD)
        self._break_the_mirror(a, tmp_path)
        assert a.apply_one(f) == "partial"

        a.inbox_dir = mailbox["inbox"]          # the transient condition clears
        assert list(a.redrive_partials().values()) == ["redriven"]
        redriven = next(mailbox["inbox"].glob("*_INTENT_*.json"))
        assert a.apply_one(redriven) == "satisfied"
        assert len(_pipeline_updates(mailbox["inbox"])) == 1
        assert a.idempotency.is_applied(VALID_INTENT_PAYLOAD["idempotency_key"])


class TestPartialDoesNotBurnIdempotencyKey:
    """The idempotency key is a promise that the *Postgres* mirror committed.

    A step-4 transient failure (read-timeout, 5xx, socket error) or an open
    circuit breaker means ``post_legacy_stage`` did NOT commit — the intent
    goes to ``partial/`` for later reconciliation. Burning the key there
    permanently diverges the ledger from Postgres: a future legitimate
    ``(job, stage)`` transition would be silently ``skipped_idempotent`` and
    never land (observed 2026-07-13, job 4de4f9fb :withdrawn). The key must be
    committed ONLY on a confirmed 2xx from ``post_legacy_stage``.
    """

    KEY = VALID_INTENT_PAYLOAD["idempotency_key"]

    def test_transient_partial_does_not_burn_key(self, mailbox, applier):
        a, jobops, _mgr = applier
        jobops.post_legacy_stage.side_effect = JobOpsClientTransientError(
            "JobOps API timed out after 10.0s"
        )
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "partial"
        assert (mailbox["partial"] / "intent.json").exists()
        # Server never committed -> key must remain unburned.
        assert not a.idempotency.is_applied(self.KEY)

    def test_partial_is_redrivable_after_transient_clears(self, mailbox, applier):
        """The core regression: after :4100 recovers, re-issuing the same
        intent must actually APPLY (land Postgres) — not skip_idempotent."""
        a, jobops, _mgr = applier
        # First attempt hits a read-timeout -> partial, key not burned.
        jobops.post_legacy_stage.side_effect = JobOpsClientTransientError("read timeout")
        f1 = write_intent(mailbox["inbox"], "intent1.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f1) == "partial"
        assert not a.idempotency.is_applied(self.KEY)

        # :4100 recovers; a re-driven intent for the same key must land.
        jobops.post_legacy_stage.side_effect = None
        jobops.post_legacy_stage.return_value = {"success": True}
        f2 = write_intent(mailbox["inbox"], "intent2.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f2) == "applied"
        assert a.idempotency.is_applied(self.KEY)
        assert (mailbox["processed"] / "intent2.json").exists()

    def test_circuit_breaker_open_partial_does_not_burn_key(self, mailbox, applier):
        a, jobops, _mgr = applier
        jobops.post_legacy_stage.side_effect = JobOpsClientTransientError("503")
        # Trip the breaker (failure_threshold=5); none of these may burn a key.
        for i in range(5):
            f = write_intent(
                mailbox["inbox"], f"int-{i}.json",
                {**VALID_INTENT_PAYLOAD, "message_id": f"m-{i}", "idempotency_key": f"key-{i}"},
            )
            assert a.apply_one(f) == "partial"
            assert not a.idempotency.is_applied(f"key-{i}")
        # 6th: breaker is open -> JobOps never attempted, so definitely uncommitted.
        jobops.post_legacy_stage.reset_mock()
        f = write_intent(
            mailbox["inbox"], "after.json",
            {**VALID_INTENT_PAYLOAD, "message_id": "m-x", "idempotency_key": "key-x"},
        )
        assert a.apply_one(f) == "partial"
        jobops.post_legacy_stage.assert_not_called()
        assert not a.idempotency.is_applied("key-x")

    def test_successful_apply_burns_key(self, mailbox, applier):
        """Guard against over-correction: a confirmed 2xx MUST burn the key."""
        a, jobops, _mgr = applier
        jobops.post_legacy_stage.return_value = {"success": True}
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "applied"
        assert a.idempotency.is_applied(self.KEY)


def _write_partial(partial_dir: Path, name: str, payload: dict, age_seconds: float) -> Path:
    """Write a synthetic partial intent whose mtime is ``age_seconds`` in the past."""
    partial_dir.mkdir(parents=True, exist_ok=True)
    p = partial_dir / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    past = time.time() - age_seconds
    os.utime(p, (past, past))
    return p


class TestRedriveMarkerParsing:
    def test_no_marker_is_attempt_zero(self, applier):
        a, _j, _m = applier
        assert a._parse_redrive_attempt(Path("20260713T1_APPROVAL_INTENT_main.json")) == 0

    def test_rd1_marker_parses(self, applier):
        a, _j, _m = applier
        assert a._parse_redrive_attempt(Path("x_INTENT_main.rd1.json")) == 1

    def test_rd12_marker_parses(self, applier):
        a, _j, _m = applier
        assert a._parse_redrive_attempt(Path("x_INTENT_main.rd12.json")) == 12

    def test_bump_from_zero_appends_rd1(self, applier):
        a, _j, _m = applier
        assert a._bump_redrive_marker("x_INTENT_main.json", 1) == "x_INTENT_main.rd1.json"

    def test_bump_replaces_existing_marker(self, applier):
        a, _j, _m = applier
        assert a._bump_redrive_marker("x_INTENT_main.rd1.json", 2) == "x_INTENT_main.rd2.json"


class TestRedrivePartials:
    def test_eligible_partial_moves_to_inbox_with_bumped_marker(self, mailbox, applier):
        a, _j, _m = applier
        # N=0 -> backoff 120s; age 200s -> eligible.
        _write_partial(mailbox["partial"], "20260713T1_APPROVAL_INTENT_main.json",
                       VALID_INTENT_PAYLOAD, age_seconds=200)
        result = a.redrive_partials()
        assert result == {"20260713T1_APPROVAL_INTENT_main.json": "redriven"}
        assert not (mailbox["partial"] / "20260713T1_APPROVAL_INTENT_main.json").exists()
        assert (mailbox["inbox"] / "20260713T1_APPROVAL_INTENT_main.rd1.json").exists()

    def test_not_yet_eligible_partial_stays(self, mailbox, applier):
        a, _j, _m = applier
        # N=0 -> backoff 120s; age 30s -> too young.
        _write_partial(mailbox["partial"], "20260713T2_APPROVAL_INTENT_main.json",
                       VALID_INTENT_PAYLOAD, age_seconds=30)
        result = a.redrive_partials()
        assert result == {"20260713T2_APPROVAL_INTENT_main.json": "waiting"}
        assert (mailbox["partial"] / "20260713T2_APPROVAL_INTENT_main.json").exists()
        assert list(mailbox["inbox"].glob("*")) == []

    def test_backoff_grows_with_attempt(self, mailbox, applier):
        a, _j, _m = applier
        # N=1 -> backoff 240s; age 200s -> still waiting (proves 2^N spacing).
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd1.json",
                       VALID_INTENT_PAYLOAD, age_seconds=200)
        assert a.redrive_partials() == {"x_APPROVAL_INTENT_main.rd1.json": "waiting"}

    def test_capped_partial_is_left_in_place_when_give_up_configured(self, tmp_path, mailbox, pipeline_path):
        # Fix B: capping is now opt-in via redrive_give_up_attempts. With it set
        # to 5, N=5 -> terminal "capped" (old default behaviour), even if ancient.
        a = _make_applier(tmp_path, mailbox, pipeline_path, redrive_give_up_attempts=5)
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=99999)
        result = a.redrive_partials()
        assert result == {"x_APPROVAL_INTENT_main.rd5.json": "capped"}
        assert (mailbox["partial"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert list(mailbox["inbox"].glob("*")) == []

    def test_default_never_caps_slow_lane_redrives_past_max_attempts(self, mailbox, applier):
        # Fix B: default redrive_give_up_attempts=0 => never surrender. An
        # ancient rd5 partial (old code: "capped") now re-drives on the slow lane.
        a, _j, _m = applier
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=99999)
        result = a.redrive_partials()
        assert result == {"x_APPROVAL_INTENT_main.rd5.json": "redriven"}
        assert (mailbox["inbox"] / "x_APPROVAL_INTENT_main.rd6.json").exists()

    def test_slow_lane_still_honours_max_backoff_cadence(self, mailbox, applier):
        # Past the give-up attempt count the backoff is pinned at redrive_max_backoff
        # (1800s). An rd8 partial only 1000s old is still "waiting", proving the
        # slow lane retries at the capped cadence rather than hot-looping.
        a, _j, _m = applier
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd8.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1000)
        assert a.redrive_partials() == {"x_APPROVAL_INTENT_main.rd8.json": "waiting"}

    def test_move_to_partial_resets_mtime(self, mailbox, applier):
        a, jobops, _m = applier
        jobops.post_legacy_stage.side_effect = JobOpsClientTransientError("boom")
        # Write intent with an OLD mtime; landing in partial must reset it to now,
        # else the per-attempt backoff would measure from intent creation time.
        f = write_intent(mailbox["inbox"], "20260713T3_APPROVAL_INTENT_main.json",
                         VALID_INTENT_PAYLOAD)
        old = time.time() - 5000
        os.utime(f, (old, old))
        assert a.apply_one(f) == "partial"
        landed = mailbox["partial"] / "20260713T3_APPROVAL_INTENT_main.json"
        assert landed.exists()
        assert time.time() - landed.stat().st_mtime < 60  # reset to ~now

    def test_redriven_intent_reapplies_end_to_end(self, mailbox, applier):
        a, jobops, _m = applier
        # 1) transient failure -> partial (key unburned, mtime reset to now).
        jobops.post_legacy_stage.side_effect = JobOpsClientTransientError("read timeout")
        f = write_intent(mailbox["inbox"], "20260713T4_APPROVAL_INTENT_main.json",
                         VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "partial"
        key = VALID_INTENT_PAYLOAD["idempotency_key"]
        assert not a.idempotency.is_applied(key)
        # 2) age the partial past its 120s backoff, then re-drive.
        landed = mailbox["partial"] / "20260713T4_APPROVAL_INTENT_main.json"
        past = time.time() - 200
        os.utime(landed, (past, past))
        assert a.redrive_partials() == {"20260713T4_APPROVAL_INTENT_main.json": "redriven"}
        # 3) :4100 recovers; the re-driven file re-applies on the next scan.
        jobops.post_legacy_stage.side_effect = None
        jobops.post_legacy_stage.return_value = {"success": True}
        outcomes = a.scan_inbox()
        assert outcomes == {"20260713T4_APPROVAL_INTENT_main.rd1.json": "applied"}
        assert a.idempotency.is_applied(key)


def _stage_payload(stage: str, key_suffix: str = "") -> dict:
    """A VALID_INTENT_PAYLOAD variant requesting a specific stage."""
    p = json.loads(json.dumps(VALID_INTENT_PAYLOAD))
    p["payload"]["requested_stage"] = stage
    p["idempotency_key"] = f"tracker-intent:it:linkedin-1:{stage}{key_suffix}"
    return p


class TestPreflightSatisfied:
    """Fix A — suppress redundant no-op intents whose job is already at/past target."""

    def test_downstream_state_is_a_real_transition_not_a_noop(
        self, tmp_path, mailbox, pipeline_path
    ):
        """REGRESSION (2026-08-23): materials_ready must NOT satisfy 'approved'.

        The operator release review -> approved moves a job BACKWARD along
        BUSINESS_STATE_ORDER (approved_for_tailor 2 < materials_ready 3). Reading
        that as "already past, therefore redundant" discarded five real operator
        approvals -- silently, with the idempotency key burned so no retry could
        ever land. It must flow through the normal dual-write path.
        """
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: "materials_ready")
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "applied"
        a._jobops_mock.post_legacy_stage.assert_called_once()
        assert len(_pipeline_updates(mailbox["inbox"])) == 1

    def test_satisfied_still_emits_the_canonical_mirror(
        self, tmp_path, mailbox, pipeline_path
    ):
        """THE DURABILITY INVARIANT: no intent is consumed without a canonical write.

        Postgres agreeing with the request says nothing about whether the TRACKER
        knows. Canonical is the source of truth and the bridge projects it ONTO
        Postgres every 15 minutes, so consuming an intent without emitting left
        canonical unaware and the next postgres-sync erased the Postgres side too.
        The :4100 POST is the only thing this pre-flight may skip.
        """
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: "approved_for_tailor")
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "satisfied"
        a._jobops_mock.post_legacy_stage.assert_not_called()   # POST skipped
        mirrors = _pipeline_updates(mailbox["inbox"])          # mirror NOT skipped
        assert len(mirrors) == 1
        body = json.loads(mirrors[0].read_text(encoding="utf-8"))
        assert body["payload"]["to_stage"] == "approved"
        assert body["payload"]["metadata"]["emitted_by"] == "tracker-intent-applier"
        assert body["correlation_id"] == VALID_INTENT_PAYLOAD["message_id"]
        # Still consumed + key burned, exactly as before.
        assert (mailbox["processed"] / "intent.json").exists()
        assert a.idempotency.is_applied(VALID_INTENT_PAYLOAD["idempotency_key"])

    def test_at_exact_target_is_satisfied(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: "approved_for_tailor")
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "satisfied"
        a._jobops_mock.post_legacy_stage.assert_not_called()

    def test_archived_target_satisfied_only_by_archived(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: "archived")
        f = write_intent(mailbox["inbox"], "arch.json", _stage_payload("archived"))
        assert a.apply_one(f) == "satisfied"
        a._jobops_mock.post_legacy_stage.assert_not_called()

    def test_before_target_takes_normal_dual_write(self, tmp_path, mailbox, pipeline_path):
        # 'scored' is BEFORE 'approved' -> not satisfied -> normal apply.
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: "scored")
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "applied"
        a._jobops_mock.post_legacy_stage.assert_called_once()

    def test_reader_none_result_takes_normal_path(self, tmp_path, mailbox, pipeline_path):
        # Unknown job (reader returns None) must not short-circuit.
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: None)
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "applied"
        a._jobops_mock.post_legacy_stage.assert_called_once()

    def test_reader_exception_fails_open(self, tmp_path, mailbox, pipeline_path):
        def boom(jid):
            raise RuntimeError("pg down")
        a = _make_applier(tmp_path, mailbox, pipeline_path, job_state_reader=boom)
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "applied"  # never blocks on a reader error
        a._jobops_mock.post_legacy_stage.assert_called_once()

    def test_no_reader_takes_normal_path(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path, job_state_reader=None)
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        assert a.apply_one(f) == "applied"

    def test_unlisted_stage_never_shortcircuits(self, tmp_path, mailbox, pipeline_path):
        # 'scored' is deliberately absent from _STAGE_SATISFIED_BY: even a reader
        # claiming a downstream state must NOT short-circuit an unlisted stage.
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: "materials_ready")
        f = write_intent(mailbox["inbox"], "intent.json", _stage_payload("scored"))
        assert a.apply_one(f) == "applied"
        a._jobops_mock.post_legacy_stage.assert_called_once()


class TestReapConvergedPartials:
    """Convergence-reaper: auto-clear CAPPED partials PG+canonical both show done."""

    def _capped_kwargs(self, **extra):
        # give_up=5 so a .rd5 partial is "capped"; both readers wired.
        # "Converged" for a requested 'approved' now means exactly
        # approved_for_tailor -- see _STAGE_SATISFIED_BY.
        base = dict(redrive_give_up_attempts=5,
                    job_state_reader=lambda jid: "approved_for_tailor",
                    canonical_state_reader=lambda: {"linkedin-1": "approved_for_tailor"})
        base.update(extra)
        return base

    def test_capped_and_both_gates_converged_is_reaped(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path, **self._capped_kwargs())
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        result = a.reap_converged_partials()
        assert result == {"x_APPROVAL_INTENT_main.rd5.json": "reaped"}
        # Moved to processed (never inbox), key burned.
        assert (mailbox["processed"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert not (mailbox["inbox"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert not (mailbox["partial"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert a.idempotency.is_applied(VALID_INTENT_PAYLOAD["idempotency_key"])

    def test_gate_b_disagrees_is_not_reaped(self, tmp_path, mailbox, pipeline_path):
        # PG says approved_for_tailor (gate A pass) but canonical still shows scored.
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          **self._capped_kwargs(
                              canonical_state_reader=lambda: {"linkedin-1": "scored"}))
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {"x_APPROVAL_INTENT_main.rd5.json": "not_converged"}
        assert (mailbox["partial"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert not a.idempotency.is_applied(VALID_INTENT_PAYLOAD["idempotency_key"])

    def test_gate_a_behind_never_parses_canonical(self, tmp_path, mailbox, pipeline_path):
        from unittest.mock import MagicMock
        canonical = MagicMock(return_value={"linkedin-1": "materials_ready"})
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          **self._capped_kwargs(
                              job_state_reader=lambda jid: "scored",   # gate A fails
                              canonical_state_reader=canonical))
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {"x_APPROVAL_INTENT_main.rd5.json": "not_converged"}
        canonical.assert_not_called()  # gate B never touched

    def test_canonical_parsed_once_per_sweep(self, tmp_path, mailbox, pipeline_path):
        from unittest.mock import MagicMock
        canonical = MagicMock(return_value={"linkedin-1": "approved_for_tailor",
                                            "linkedin-2": "approved_for_tailor"})
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          **self._capped_kwargs(canonical_state_reader=canonical))
        p2 = json.loads(json.dumps(VALID_INTENT_PAYLOAD))
        p2["job_id"] = "linkedin-2"
        p2["idempotency_key"] = "tracker-intent:it:linkedin-2:approved"
        _write_partial(mailbox["partial"], "a_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        _write_partial(mailbox["partial"], "b_APPROVAL_INTENT_main.rd5.json",
                       p2, age_seconds=1)
        result = a.reap_converged_partials()
        assert result == {"a_APPROVAL_INTENT_main.rd5.json": "reaped",
                          "b_APPROVAL_INTENT_main.rd5.json": "reaped"}
        assert canonical.call_count == 1

    def test_gate_a_pass_but_job_absent_from_canonical_not_reaped(self, tmp_path, mailbox, pipeline_path):
        # Gate A passes (PG at materials_ready) but the canonical map is wired
        # and non-empty yet lacks this job_id -> canonical.get(...) is None ->
        # fail-closed "not_converged" (distinct branch from unwired-reader).
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          **self._capped_kwargs(
                              canonical_state_reader=lambda: {"some-other-job": "materials_ready"}))
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {"x_APPROVAL_INTENT_main.rd5.json": "not_converged"}
        assert (mailbox["partial"] / "x_APPROVAL_INTENT_main.rd5.json").exists()
        assert not a.idempotency.is_applied(VALID_INTENT_PAYLOAD["idempotency_key"])

    def test_non_capped_partial_is_ignored(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path, **self._capped_kwargs())
        # rd1 < give_up=5 -> not capped -> reaper leaves it entirely.
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd1.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {}
        assert (mailbox["partial"] / "x_APPROVAL_INTENT_main.rd1.json").exists()

    def test_give_up_zero_reaps_nothing(self, tmp_path, mailbox, pipeline_path):
        # Default give_up=0 => nothing is ever capped => reaper is a no-op.
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          job_state_reader=lambda jid: "materials_ready",
                          canonical_state_reader=lambda: {"linkedin-1": "materials_ready"})
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {}

    def test_archived_incident_case_reaped(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          redrive_give_up_attempts=5,
                          job_state_reader=lambda jid: "archived",
                          canonical_state_reader=lambda: {"linkedin-1": "archived"})
        _write_partial(mailbox["partial"], "z_STATE_TRANSITION_INTENT_main.rd5.json",
                       _stage_payload("archived"), age_seconds=1)
        assert a.reap_converged_partials() == {"z_STATE_TRANSITION_INTENT_main.rd5.json": "reaped"}

    def test_canonical_reader_unwired_fails_closed(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path,
                          redrive_give_up_attempts=5,
                          job_state_reader=lambda jid: "materials_ready",
                          canonical_state_reader=None)
        _write_partial(mailbox["partial"], "x_APPROVAL_INTENT_main.rd5.json",
                       VALID_INTENT_PAYLOAD, age_seconds=1)
        assert a.reap_converged_partials() == {"x_APPROVAL_INTENT_main.rd5.json": "not_converged"}

    def test_corrupt_capped_file_is_skipped(self, tmp_path, mailbox, pipeline_path):
        a = _make_applier(tmp_path, mailbox, pipeline_path, **self._capped_kwargs())
        mailbox["partial"].mkdir(parents=True, exist_ok=True)
        bad = mailbox["partial"] / "bad_APPROVAL_INTENT_main.rd5.json"
        bad.write_text("{not valid", encoding="utf-8")
        assert a.reap_converged_partials() == {"bad_APPROVAL_INTENT_main.rd5.json": "skipped"}
        assert bad.exists()
