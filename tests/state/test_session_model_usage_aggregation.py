"""Read-only parent/child usage aggregation regression tests."""
from pathlib import Path

from hermes_state import SessionDB


def _db(tmp_path: Path) -> SessionDB:
    db = SessionDB(tmp_path / "state.db")
    db.create_session("parent", source="cli", model="luna")
    db.create_session("child", source="subagent", model="deepseek", parent_session_id="parent")
    return db


def test_aggregate_separates_parent_and_child_usage(tmp_path):
    db = _db(tmp_path)
    db.update_token_counts("parent", input_tokens=10, output_tokens=2, api_call_count=1, model="luna", billing_provider="custom")
    db.update_token_counts("child", input_tokens=30, output_tokens=7, reasoning_tokens=4, api_call_count=2, model="deepseek", billing_provider="custom")
    report = db.aggregate_session_usage("parent")
    assert report["parent"]["api_calls"] == 1
    assert report["parent"]["input_tokens"] == 10
    assert report["children"][0]["usage"]["api_calls"] == 2
    assert report["children"][0]["usage"]["reasoning_tokens"] == 4
    assert report["totals"]["api_calls"] == 3
    assert report["totals"]["input_tokens"] == 40


def test_aggregation_does_not_mutate_raw_rows(tmp_path):
    db = _db(tmp_path)
    db.update_token_counts("child", input_tokens=12, output_tokens=3, api_call_count=1, model="deepseek", billing_provider="custom")
    before = db.get_session("child")
    report = db.aggregate_session_usage("parent")
    after = db.get_session("child")
    assert report["children"]
    assert before is not None and after is not None
    assert after["input_tokens"] == before["input_tokens"]
    assert after["api_call_count"] == before["api_call_count"]