"""Integration tests for the intent flow.

These test the wire between components, NOT individual unit behavior.
Uses a live PipelineManager + a stub HTTP server impersonating JobOps API.
"""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from intent_applier import (
    IdempotencyTracker,
    IntentApplier,
    JobOpsClient,
)
from pipeline_state import PipelineManager


class JobOpsStubHandler(BaseHTTPRequestHandler):
    intent_calls: list[dict] = []
    legacy_calls: list[dict] = []
    legacy_response_status = 200

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
        if "/intents/transition" in self.path:
            self.intent_calls.append({"path": self.path, "body": body})
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"queued":true,"messageId":"stub-1"}')
        elif "/legacy/jobs/" in self.path and "/stage" in self.path:
            self.legacy_calls.append({"path": self.path, "body": body})
            self.send_response(self.legacy_response_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"success":true}')
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args, **kwargs):  # silence test noise
        pass


@pytest.fixture
def jobops_stub():
    JobOpsStubHandler.intent_calls = []
    JobOpsStubHandler.legacy_calls = []
    JobOpsStubHandler.legacy_response_status = 200
    server = HTTPServer(("127.0.0.1", 0), JobOpsStubHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield {"port": port, "calls": JobOpsStubHandler}
    server.shutdown()
    server.server_close()


@pytest.fixture
def pipeline_path(tmp_path):
    p = tmp_path / "pipeline.json"
    p.write_text(json.dumps({
        "jobs": [{
            "job_id": "linkedin-1",
            "stage": "review",
            "title": "VP",
            "company": "Acme",
            "history": [],
        }],
        "stats": {},
    }))
    return p


@pytest.fixture
def applier(tmp_path, pipeline_path, jobops_stub):
    inbox = tmp_path / "inbox"; inbox.mkdir()
    processed = tmp_path / "processed"
    partial = tmp_path / "partial"
    dead = tmp_path / "dead-letter"
    return IntentApplier(
        inbox_dir=inbox,
        processed_dir=processed,
        partial_dir=partial,
        dead_letter_dir=dead,
        pipeline_manager=PipelineManager(path=pipeline_path),
        jobops_client=JobOpsClient(base_url=f"http://127.0.0.1:{jobops_stub['port']}"),
        idempotency=IdempotencyTracker(tmp_path / "applier_state.db"),
    )


def write_intent(inbox: Path, name: str, payload: dict) -> Path:
    p = inbox / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


VALID_INTENT = {
    "message_id": "m-1",
    "idempotency_key": "tracker-intent:approval_intent:linkedin-1:approved",
    "protocol_version": "2.0-draft",
    "type": "APPROVAL_INTENT",
    "from": "main",
    "to": "tracker",
    "job_id": "linkedin-1",
    "timestamp": "2026-04-25T10:00:00Z",
    "correlation_id": "c-1",
    "attempt": 1, "max_attempts": 3, "lease_timeout_seconds": 300,
    "reply_expected": False, "intent_only": True,
    "payload": {
        "requested_stage": "approved",
        "actor_id": "diego",
        "source": "legacy_dashboard",
        "notes": "matches",
        "metadata": {},
    },
}


class TestIntentFlowE2E:
    def test_dashboard_style_intent_flows_through_applier(
        self, applier, jobops_stub, pipeline_path,
    ):
        # Dashboard would normally POST /intents/transition; here we drop the
        # mailbox file directly to simulate that endpoint having run.
        f = write_intent(applier.inbox_dir, "intent.json", VALID_INTENT)
        outcome = applier.apply_one(f)
        assert outcome == "applied"
        # pipeline.json updated
        data = json.loads(pipeline_path.read_text())
        job = next(j for j in data["jobs"] if j["job_id"] == "linkedin-1")
        assert job["stage"] == "approved"
        assert job["history"][-1]["source"] == "tracker_mailbox"
        assert job["history"][-1]["agent"] == "diego"
        # JobOps stub received the legacy/stage call
        assert len(jobops_stub["calls"].legacy_calls) == 1
        call = jobops_stub["calls"].legacy_calls[0]
        assert "linkedin-1" in call["path"]
        assert call["body"]["actorId"] == "tracker"
        assert call["body"]["source"] == "tracker_mailbox"

    def test_partial_path_when_jobops_5xx(self, applier, jobops_stub, pipeline_path):
        jobops_stub["calls"].legacy_response_status = 502
        f = write_intent(applier.inbox_dir, "intent.json", VALID_INTENT)
        outcome = applier.apply_one(f)
        assert outcome == "partial"
        assert (applier.partial_dir / "intent.json").exists()
        # pipeline.json still updated (canonical-first)
        data = json.loads(pipeline_path.read_text())
        job = next(j for j in data["jobs"] if j["job_id"] == "linkedin-1")
        assert job["stage"] == "approved"

    def test_dead_letter_when_jobops_4xx(self, applier, jobops_stub):
        jobops_stub["calls"].legacy_response_status = 400
        f = write_intent(applier.inbox_dir, "intent.json", VALID_INTENT)
        outcome = applier.apply_one(f)
        assert outcome == "dead_lettered"
        assert (applier.dead_letter_dir / "intent.json").exists()
        assert (applier.dead_letter_dir / "intent.json.error.json").exists()
