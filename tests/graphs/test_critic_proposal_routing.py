"""Routing evidence emitted by the Critic graph."""

from __future__ import annotations

import json
from pathlib import Path

from graphs import critic


def test_propose_only_event_marks_user_decision_required(tmp_path, monkeypatch):
    mailbox = tmp_path / "mailbox"
    whatsapp_queue = tmp_path / "whatsapp_queue.jsonl"
    captured = {}

    monkeypatch.setattr(critic, "PROPOSAL_MAILBOX", mailbox)
    monkeypatch.setattr(critic, "WHATSAPP_QUEUE", whatsapp_queue)

    def capture_event(event_type_str, source, payload, priority=None):
        captured.update({
            "event_type": event_type_str,
            "source": source,
            "payload": payload,
            "priority": priority,
        })
        return "event-1"

    monkeypatch.setattr(critic, "_emit_event", capture_event)

    result = critic.emit_proposals_node({
        "run_id": "run-1",
        "proposals_classified": [{
            "proposal_id": "choose-x",
            "kind": "structural",
            "summary": "Choose X",
            "specific_change": "Apply X",
            "rationale": "Evidence",
            "expected_effect": "Improvement",
            "risk": "low",
            "cluster_pattern_name": "cluster-x",
            "classification": "propose_only",
            "replay": {"status": "supported"},
        }],
    })

    assert result["emitted"][0]["event_id"] == "event-1"
    assert captured["event_type"] == "critic_proposal"
    assert captured["source"] == "critic.graph"
    assert captured["payload"]["decision_required"] is True

    mailbox_path = Path(result["emitted"][0]["mailbox_path"])
    mailbox_payload = json.loads(mailbox_path.read_text(encoding="utf-8"))
    assert mailbox_payload["payload"]["decision_required"] is True
