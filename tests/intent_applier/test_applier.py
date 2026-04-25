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
        assert call.kwargs["actor_id"] == "tracker"
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
            write_intent(mailbox["inbox"], f"int-{i}.json",
                         {**VALID_INTENT_PAYLOAD,
                          "message_id": f"m-{i}",
                          "idempotency_key": f"key-{i}",
                          "job_id": f"linkedin-{i+1}"})
        # PipelineManager.update_stage upserts unknown jobs, so this works.
        outcomes = a.scan_inbox()
        assert len(outcomes) == 3
        assert all(o in {"applied", "partial", "dead_lettered"} for o in outcomes.values())
