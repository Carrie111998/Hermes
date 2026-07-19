"""Behavior tests for append-only cron delivery receipts."""

from __future__ import annotations

import json


def _receipt(*, execution_id: str, chat_id: str = "123", thread_id: str | None = None) -> dict:
    return {
        "execution_id": execution_id,
        "job_id": "job-1",
        "target": {"platform": "telegram", "chat_id": chat_id, "thread_id": thread_id},
        "state": "accepted",
        "output_sha256": "a" * 64,
    }


def test_receipt_store_appends_once_per_execution_target(tmp_path):
    from cron.delivery_receipts import append_receipt

    path = tmp_path / "delivery-receipts.jsonl"
    first = append_receipt(path, _receipt(execution_id="execution-1"))
    duplicate = append_receipt(path, _receipt(execution_id="execution-1"))

    assert first == {"written": True, "reason": None}
    assert duplicate == {"written": False, "reason": "duplicate_execution_target"}
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 1
    assert records[0]["execution_id"] == "execution-1"


def test_receipt_store_allows_distinct_targets_for_same_execution(tmp_path):
    from cron.delivery_receipts import append_receipt

    path = tmp_path / "delivery-receipts.jsonl"
    assert append_receipt(path, _receipt(execution_id="execution-1", chat_id="123"))["written"] is True
    assert append_receipt(path, _receipt(execution_id="execution-1", chat_id="456"))["written"] is True

    assert len(path.read_text(encoding="utf-8").splitlines()) == 2


def test_get_delivery_trace_returns_full_chain(monkeypatch, tmp_path):
    from cron.delivery_receipts import append_receipt, get_delivery_trace

    receipt_path = tmp_path / "trace-test.jsonl"
    append_receipt(receipt_path, {
        "execution_id": "trace-exec",
        "state": "accepted",
        "mode": "native",
        "target": {"platform": "telegram", "chat_id": "123", "thread_id": None},
        "message_id": "42",
    })

    trace = get_delivery_trace(receipt_path, "trace-exec")
    assert len(trace) == 1
    assert trace[0]["state"] == "accepted"
    assert trace[0]["message_id"] == "42"


def test_get_delivery_trace_empty_for_unknown_execution(monkeypatch, tmp_path):
    from cron.delivery_receipts import append_receipt, get_delivery_trace

    receipt_path = tmp_path / "trace-test.jsonl"
    append_receipt(receipt_path, {
        "execution_id": "other-exec",
        "state": "accepted",
        "mode": "native",
        "target": {"platform": "telegram", "chat_id": "123", "thread_id": None},
    })

    trace = get_delivery_trace(receipt_path, "unknown-exec")
    assert trace == []


def test_get_delivery_trace_multiple_targets(monkeypatch, tmp_path):
    from cron.delivery_receipts import append_receipt, get_delivery_trace

    receipt_path = tmp_path / "trace-test.jsonl"
    append_receipt(receipt_path, {
        "execution_id": "fanout-exec",
        "state": "accepted",
        "mode": "native",
        "target": {"platform": "telegram", "chat_id": "111", "thread_id": None},
        "message_id": "100",
    })
    append_receipt(receipt_path, {
        "execution_id": "fanout-exec",
        "state": "failed",
        "mode": "native",
        "target": {"platform": "discord", "chat_id": "222", "thread_id": None},
    })

    trace = get_delivery_trace(receipt_path, "fanout-exec")
    assert len(trace) == 2
    states = [r["state"] for r in trace]
    assert "accepted" in states
    assert "failed" in states


def test_execution_aggregate_preserves_failure_when_later_target_is_accepted(tmp_path):
    from cron.delivery_receipts import aggregate_execution_state, append_receipt

    path = tmp_path / "delivery-receipts.jsonl"
    failed = _receipt(execution_id="execution-1", chat_id="123")
    failed["state"] = "failed"
    assert append_receipt(path, failed)["written"] is True
    assert append_receipt(path, _receipt(execution_id="execution-1", chat_id="456"))["written"] is True

    assert aggregate_execution_state(path, "execution-1") == "failed"


def test_execution_aggregate_prioritizes_uncertain_over_accepted_regardless_of_target_order(tmp_path):
    from cron.delivery_receipts import aggregate_execution_state, append_receipt

    path = tmp_path / "delivery-receipts.jsonl"
    assert append_receipt(path, _receipt(execution_id="execution-1", chat_id="123"))["written"] is True
    uncertain = _receipt(execution_id="execution-1", chat_id="456")
    uncertain["state"] = "uncertain_in_flight"
    assert append_receipt(path, uncertain)["written"] is True

    assert aggregate_execution_state(path, "execution-1") == "uncertain_in_flight"
