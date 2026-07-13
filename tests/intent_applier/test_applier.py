import json
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

    def test_emission_failure_never_raises(self, tmp_path, mailbox, applier):
        """_emit is best-effort by contract: a broken mailbox path must be
        swallowed (logged), never propagated into the intent outcome."""
        from intent_applier.parser import parse_intent_file

        a, _jobops, _mgr = applier
        f = write_intent(mailbox["inbox"], "intent.json", VALID_INTENT_PAYLOAD)
        msg = parse_intent_file(f)

        blocker = tmp_path / "not-a-dir"
        blocker.write_text("x", encoding="utf-8")
        a.inbox_dir = blocker / "sub"  # parent is a file -> any write fails

        a._emit_canonical_pipeline_update(msg)  # must not raise
