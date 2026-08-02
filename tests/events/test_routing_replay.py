import json

from events.routing_replay import replay_audit
from events.schema import Event, EventType, Priority


def _event(event_type, payload=None, *, priority=None, source="test"):
    return Event.create(
        event_type=event_type,
        source=source,
        payload=payload or {},
        priority=priority,
    ).to_dict()


def test_replay_is_no_send_and_enforces_attention_invariants(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    records = [
        _event(
            EventType.AGENT_ITERATION,
            {"agent": "tracker", "counters": {"exit_code": 1}},
        ),
        _event(
            EventType.AGENT_ITERATION,
            {"agent": "scout", "result": "partial"},
        ),
        _event(EventType.CRITIC_PROPOSAL, {"summary": "ordinary proposal"}),
        _event(
            EventType.CRITIC_PROPOSAL,
            {"decision_required": True, "summary": "pick one"},
        ),
        _event(
            EventType.AGENT_ITERATION,
            {
                "agent": "scout",
                "status": "failed",
                "action_required": True,
                "action_kind": "credits",
            },
        ),
        _event(
            EventType.GATEWAY_HEALTH,
            {"before": "down", "status": "up"},
        ),
        _event(EventType.CRON_COMPLETED, {"output_summary": "all checks passed"}),
        {
            "event_id": "future-event",
            "event_type": "future_event_type",
            "source": "future",
            "timestamp": "2026-08-02T12:00:00+00:00",
            "priority": "normal",
            "payload": {},
        },
    ]
    audit_path.write_text(
        "\n".join(json.dumps(record) for record in records),
        encoding="utf-8",
    )

    report = replay_audit(audit_path, limit=100)

    assert report["sample_size"] == len(records)
    assert report["unknown_event_types"] == {"future_event_type": 1}
    assert report["violations"] == []
    assert report["destinations"] == {
        "action_required": 2,
        "critic": 1,
        "watchdog_alerts": 3,
        "cron_firehose": 1,
    }
    assert report["outcomes"] == {
        "failed": 2,
        "degraded": 1,
        "unknown": 1,
        "pending": 1,
        "recovered": 1,
        "succeeded": 1,
    }
    assert len(report["rows"]) == len(records) - 1
    assert all(row["destination_count"] == 1 for row in report["rows"])
    assert all("header" in row for row in report["rows"])

    tracker_failure = report["rows"][0]
    assert tracker_failure["attention"] == "warn"
    assert tracker_failure["topic"] == "watchdog_alerts"
    assert not tracker_failure["marker"].startswith("🟢")

    recovery = next(row for row in report["rows"] if row["outcome"] == "recovered")
    assert recovery["marker"] == "🟢"


def test_replay_reads_only_the_bounded_tail(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    audit_path.write_text(
        "\n".join(
            json.dumps(_event(EventType.CRON_COMPLETED, {"sequence": index}))
            for index in range(5)
        ),
        encoding="utf-8",
    )

    report = replay_audit(audit_path, limit=2)

    assert report["sample_size"] == 2
    assert [row["payload"]["sequence"] for row in report["rows"]] == [3, 4]
