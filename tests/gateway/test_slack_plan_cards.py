from __future__ import annotations

import json
import multiprocessing
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from plugins.platforms.slack.plan_cards import (
    PlanCardStore,
    _RetryScheduleKind,
    build_plan_blocks,
    parse_todo_result,
)


def _snapshot(count: int = 4) -> list[dict[str, str]]:
    statuses = ["pending", "in_progress", "completed", "cancelled"]
    return [
        {"id": f"task-{index}", "content": f"Task {index}", "status": statuses[index % 4]}
        for index in range(count)
    ]


def _process_write(home: str, index: int, output) -> None:
    route = {
        "session_key": "sk",
        "session_id": "sid",
        "team_id": "T1",
        "channel_id": "C1",
        "thread_ts": "",
    }
    revision = PlanCardStore(home).record_desired_snapshot(
        route, [{"id": str(index), "content": str(index), "status": "pending"}]
    )["desired_revision"]
    output.put(revision)


def test_parse_todo_result_accepts_trailing_hint_and_rejects_errors() -> None:
    result = json.dumps({"todos": _snapshot()}) + "\n\n[Hint: persisted]"
    parsed = parse_todo_result(result)
    assert parsed == _snapshot()
    assert parse_todo_result('{"error":"bad"}') is None
    assert parse_todo_result("not json") is None


def test_native_renderer_preserves_status_and_rotates_block_ids() -> None:
    first = build_plan_blocks(
        _snapshot(), revision=1, snapshot_hash="hash-1",
    )
    second = build_plan_blocks(
        _snapshot(), revision=2, snapshot_hash="hash-1",
    )

    plan = first.native_blocks[0]
    assert plan["type"] == "plan"
    assert [task["type"] for task in plan["tasks"]] == ["task_card"] * 4
    assert [task["task_id"] for task in plan["tasks"]] == [
        "task-0", "task-1", "task-2", "task-3"
    ]
    assert [task["status"] for task in plan["tasks"]] == [
        "pending", "in_progress", "complete", "error"
    ]
    assert plan["tasks"][3]["title"].startswith("[cancelled] Task 3")
    assert first.text == "Hermes plan: 4 tasks"
    assert first.native_blocks[0]["block_id"] != second.native_blocks[0]["block_id"]
    assert first.native_blocks[0]["tasks"][0]["block_id"] != second.native_blocks[0]["tasks"][0]["block_id"]
    assert all(block["block_id"] != other["block_id"] for block, other in zip(
        first.native_blocks, second.native_blocks
    ))


@pytest.mark.parametrize("count", [0, 1, 10, 100])
def test_renderer_is_read_only_for_all_status_and_capacity_combinations(count) -> None:
    statuses = ["pending", "in_progress", "completed", "cancelled"]
    todos = [
        {
            "id": f"user:{index}" if index % 2 else f"agent:{index}",
            "content": f"Task {index}",
            "status": statuses[index % len(statuses)],
        }
        for index in range(count)
    ]
    rendered = build_plan_blocks(todos, revision=7, snapshot_hash="read-only")

    assert len(rendered.native_blocks) == 1
    assert [task["task_id"] for task in rendered.native_blocks[0]["tasks"]] == [
        task["id"] for task in todos
    ]
    assert all(block["type"] != "actions" for block in rendered.native_blocks)
    assert all(block["type"] != "actions" for block in rendered.fallback_blocks)
    payload = json.dumps(
        [rendered.native_blocks, rendered.fallback_blocks], ensure_ascii=False
    ).lower()
    for forbidden in (
        "checkboxes", "hermes_plan_complete", "hermes_plan_cancel",
        "hermes_plan_add", "hermes_plan_refresh", "add user task",
    ):
        assert forbidden not in payload


def test_fallback_preserves_representable_progress_and_discloses_truncation() -> None:
    todos = [
        {
            "id": f"task-{index}",
            "content": "x" * 3000,
            "status": ("pending", "in_progress", "completed", "cancelled")[index % 4],
        }
        for index in range(80)
    ]
    rendered = build_plan_blocks(todos, revision=8, snapshot_hash="fallback")
    fallback_text = json.dumps(rendered.fallback_blocks, ensure_ascii=False)

    assert len(rendered.native_blocks[0]["tasks"]) == len(todos)
    assert len(rendered.fallback_blocks) <= 50
    assert "Slack display truncated this plan" in fallback_text
    assert "*Pending* `task-0`" in fallback_text
    assert all(block["type"] != "actions" for block in rendered.fallback_blocks)


def test_fallback_splits_overlong_lines_without_undisclosed_loss() -> None:
    todos = [
        {"id": "agent:long", "content": "a" * 3000, "status": "pending"},
        {"id": "user:long", "content": "u" * 3000, "status": "in_progress"},
    ]
    rendered = build_plan_blocks(todos, revision=9, snapshot_hash="long-lines")
    section_text = "".join(
        block["text"]["text"]
        for block in rendered.fallback_blocks
        if block["type"] == "section"
    )
    expected = "\n".join([
        "*Hermes plan* (2 tasks)",
        f"• *Pending* `agent:long` — {'a' * 3000}",
        f"• *In progress* `user:long` — {'u' * 3000}",
    ])

    assert section_text == expected
    assert all(
        len(block["text"]["text"]) <= 3000
        for block in rendered.fallback_blocks
        if block["type"] == "section"
    )
    assert "display truncated" not in json.dumps(rendered.fallback_blocks).lower()


@pytest.mark.parametrize("task_prefix", ["agent", "user"])
def test_native_task_titles_disclose_content_truncation_at_boundary(task_prefix) -> None:
    exact_content = "x" * 3000
    over_content = "y" * 3001
    rendered = build_plan_blocks(
        [
            {"id": f"{task_prefix}:exact", "content": exact_content, "status": "pending"},
            {"id": f"{task_prefix}:over", "content": over_content, "status": "pending"},
        ],
        revision=10,
        snapshot_hash=f"title-{task_prefix}",
    )
    exact_title, over_title = [
        task["title"] for task in rendered.native_blocks[0]["tasks"]
    ]

    assert exact_title == exact_content
    assert len(over_title) <= 3000
    assert over_title.endswith("... [truncated]")


def test_store_revision_reverse_index_and_restart_persistence(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk",
        "session_id": "sid",
        "team_id": "T1",
        "channel_id": "C1",
        "thread_ts": "10.0",
    }
    one = store.record_desired_snapshot(route, _snapshot(2))
    two = store.record_desired_snapshot(route, _snapshot(3))
    assert one["desired_revision"] == 1
    assert two["desired_revision"] == 2
    assert two["applied_revision"] == 0

    store.mark_applied("sk", revision=2, snapshot_hash=two["desired_hash"], message_ts="20.0")
    restarted = PlanCardStore(tmp_path)
    state = restarted.get_session("sk")
    assert state["desired_revision"] == state["applied_revision"] == 2
    assert restarted.lookup_route("T1", "C1", "20.0")["session_key"] == "sk"
    assert store.lock_path != store.state_path
    assert store.lock_path.exists()
    first_lock_inode = store.lock_path.stat().st_ino
    store.record_desired_snapshot(route, _snapshot(1))
    assert store.lock_path.stat().st_size >= 1
    if first_lock_inode:
        assert store.lock_path.stat().st_ino == first_lock_inode


def test_stale_apply_cannot_advance_or_overwrite_newer_desired_state(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    first = store.record_desired_snapshot(route, _snapshot(1))
    second = store.record_desired_snapshot(route, _snapshot(2))

    assert not store.mark_applied(
        "sk", revision=first["desired_revision"],
        snapshot_hash=first["desired_hash"], message_ts="stale",
    )
    current = store.get_session("sk")
    assert current["desired_revision"] == second["desired_revision"]
    assert current["applied_revision"] == 0
    assert current["message_ts"] == ""


def test_create_identity_is_stable_uuid_until_route_generation_changes(tmp_path) -> None:
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0", "route_user_id": "U1",
        "chat_type": "group",
    }
    store = PlanCardStore(tmp_path)
    state = store.record_desired_snapshot(route, _snapshot(1))
    prepared = store.prepare_create("sk", expected_route=state)
    first_id = prepared["client_msg_id"]
    assert prepared["was_attempted"] is False
    assert str(uuid.UUID(first_id)) == first_id

    restarted = PlanCardStore(tmp_path)
    prepared_again = restarted.prepare_create(
        "sk", expected_route=restarted.get_session("sk")
    )
    assert prepared_again["was_attempted"] is True
    assert prepared_again["client_msg_id"] == first_id

    moved = restarted.record_desired_snapshot({
        **route, "session_id": "sid-2", "channel_id": "C2", "thread_ts": "11.0",
    }, _snapshot(2))
    assert moved["client_msg_id"] == ""
    assert moved["retired_anchors"][0]["client_msg_id"] == first_id
    next_generation = restarted.prepare_create("sk", expected_route=moved)
    assert str(uuid.UUID(next_generation["client_msg_id"])) == next_generation["client_msg_id"]
    assert next_generation["client_msg_id"] != first_id


def test_retired_anchor_retry_survives_restart_until_cleanup_succeeds(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    state = store.record_desired_snapshot(route, _snapshot(1))
    prepared = store.prepare_create("sk", expected_route=state)
    assert store.mark_applied(
        "sk", revision=state["desired_revision"], snapshot_hash=state["desired_hash"],
        message_ts="20.0", rendered_revision=state["desired_revision"],
        expected_message_ts="", expected_client_msg_id=prepared["client_msg_id"],
    )
    moved = store.record_desired_snapshot({
        **route, "session_id": "sid-2", "channel_id": "C2", "thread_ts": "11.0",
    }, _snapshot(2))
    retired = moved["retired_anchors"][0]
    store.mark_retired_retry("sk", retired["anchor_id"], base_seconds=0)

    restarted = PlanCardStore(tmp_path)
    pending = restarted.list_retired("sk", now=float("inf"))
    assert pending[0]["message_ts"] == "20.0"
    assert pending[0]["retry_count"] == 1
    assert restarted.complete_retired_cleanup("sk", pending[0]["anchor_id"])
    assert restarted.list_retired("sk", now=float("inf")) == []


def test_conflict_orphan_retirement_is_deduped_by_remote_identity(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0", "route_user_id": "U1",
        "chat_type": "group",
    }
    state = store.record_desired_snapshot(route, _snapshot(1))
    prepared = store.prepare_create("sk", expected_route=state)
    orphan = {
        **prepared,
        "message_ts": "99.0",
        "client_msg_id": prepared["client_msg_id"],
    }

    assert store.retire_orphan_anchor("sk", orphan) is True
    assert store.retire_orphan_anchor("sk", orphan) is False
    retired = store.list_retired("sk", now=float("inf"))
    assert len(retired) == 1
    assert retired[0]["message_ts"] == "99.0"
    assert retired[0]["client_msg_id"] == prepared["client_msg_id"]


def test_route_change_invalidates_old_reverse_index(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    first = store.record_desired_snapshot({
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }, _snapshot(1))
    store.mark_applied("sk", revision=1, snapshot_hash=first["desired_hash"], message_ts="20.0")
    store.record_desired_snapshot({
        "session_key": "sk", "session_id": "sid-2", "team_id": "T1",
        "channel_id": "C2", "thread_ts": "11.0",
    }, _snapshot(1))
    assert store.lookup_route("T1", "C1", "20.0") is None
    assert store.get_session("sk")["message_ts"] == ""
    assert store.get_session("sk")["retired_anchors"][0]["message_ts"] == "20.0"


def test_explicit_empty_route_fields_clear_prior_values_and_omitted_fields_inherit(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0", "route_user_id": "U1",
        "chat_type": "group",
    }
    first = store.record_desired_snapshot(route, _snapshot(1))
    prepared = store.prepare_create("sk", expected_route=first)
    assert store.mark_applied(
        "sk", revision=first["desired_revision"], snapshot_hash=first["desired_hash"],
        message_ts="20.0", rendered_revision=first["desired_revision"],
        expected_message_ts="", expected_client_msg_id=prepared["client_msg_id"],
    )

    moved = store.record_desired_snapshot({
        "session_key": "sk", "thread_ts": "", "route_user_id": "",
    }, _snapshot(2))
    assert moved["thread_ts"] == ""
    assert moved["route_user_id"] == ""
    assert moved["session_id"] == "sid"
    assert moved["team_id"] == "T1"
    assert moved["channel_id"] == "C1"
    assert moved["chat_type"] == "group"
    assert moved["message_ts"] == ""
    assert moved["client_msg_id"] == ""
    assert moved["retired_anchors"][0]["thread_ts"] == "10.0"
    next_generation = store.prepare_create("sk", expected_route=moved)
    assert next_generation["client_msg_id"] != prepared["client_msg_id"]


def test_store_concurrent_increments_do_not_get_lost(tmp_path) -> None:
    route = {
        "session_key": "sk",
        "session_id": "sid",
        "team_id": "T1",
        "channel_id": "C1",
        "thread_ts": "",
    }

    def write(index: int) -> int:
        return PlanCardStore(tmp_path).record_desired_snapshot(
            route, [{"id": str(index), "content": str(index), "status": "pending"}]
        )["desired_revision"]

    with ThreadPoolExecutor(max_workers=8) as pool:
        revisions = sorted(pool.map(write, range(24)))
    assert revisions == list(range(1, 25))


def test_store_cross_process_increments_do_not_get_lost(tmp_path) -> None:
    ctx = multiprocessing.get_context("spawn")
    output = ctx.Queue()
    processes = [ctx.Process(target=_process_write, args=(str(tmp_path), index, output)) for index in range(8)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0
    assert sorted(output.get(timeout=2) for _ in processes) == list(range(1, 9))


def test_store_corruption_fails_closed_and_recovers_on_next_snapshot(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    store.state_path.parent.mkdir(parents=True, exist_ok=True)
    store.state_path.write_text("{broken", encoding="utf-8")
    assert store.get_session("sk") is None
    state = store.record_desired_snapshot(
        {"session_key": "sk", "session_id": "sid", "team_id": "T", "channel_id": "C"},
        _snapshot(1),
    )
    assert state["desired_revision"] == 1
    assert store.get_session("sk")["last_desired_snapshot"] == _snapshot(1)
    assert list(store.state_path.parent.glob("slack_plan_cards.json.corrupt.*"))




def _converged_session_with_retired_anchor(store, route):
    first = store.record_desired_snapshot(route, _snapshot(1))
    first_create = store.prepare_create("sk", expected_route=first)
    assert store.mark_applied(
        "sk", revision=first["desired_revision"], snapshot_hash=first["desired_hash"],
        message_ts="20.0", expected_message_ts="",
        expected_client_msg_id=first_create["client_msg_id"],
    )
    moved = store.record_desired_snapshot(
        {**route, "channel_id": "C2"}, _snapshot(2)
    )
    moved_create = store.prepare_create("sk", expected_route=moved)
    assert store.mark_applied(
        "sk", revision=moved["desired_revision"], snapshot_hash=moved["desired_hash"],
        message_ts="30.0", expected_message_ts="",
        expected_client_msg_id=moved_create["client_msg_id"],
    )
    return store.list_retired("sk")[0]


def test_retry_schedule_distinguishes_no_work_from_due_now(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    assert store.retry_schedule(now=100).kind is _RetryScheduleKind.NO_WORK

    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    store.record_desired_snapshot(route, _snapshot(1))
    assert store.retry_schedule(now=100).kind is _RetryScheduleKind.DUE_NOW


def test_retry_schedule_ignores_converged_current_retry_metadata(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    state = store.record_desired_snapshot(route, _snapshot(1))
    assert store.mark_applied(
        "sk", revision=state["desired_revision"], snapshot_hash=state["desired_hash"],
        message_ts="20.0",
    )
    with (
        patch("plugins.platforms.slack.plan_cards.time.time", return_value=100),
        patch("plugins.platforms.slack.plan_cards.random.uniform", return_value=0),
    ):
        store.mark_retry("sk", base_seconds=10, max_seconds=10)

    assert store.retry_schedule(now=105).kind is _RetryScheduleKind.NO_WORK


def test_retry_schedule_deadline_crossing_after_dirty_scan_is_due_now(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    store.record_desired_snapshot(route, _snapshot(1))
    with (
        patch("plugins.platforms.slack.plan_cards.time.time", return_value=100),
        patch("plugins.platforms.slack.plan_cards.random.uniform", return_value=0),
    ):
        store.mark_retry("sk", base_seconds=10, max_seconds=10)

    assert store.list_dirty(now=109) == []
    assert store.retry_schedule(now=111).kind is _RetryScheduleKind.DUE_NOW


def test_retry_schedule_current_future_is_due_at(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    store.record_desired_snapshot(route, _snapshot(1))
    with (
        patch("plugins.platforms.slack.plan_cards.time.time", return_value=100),
        patch("plugins.platforms.slack.plan_cards.random.uniform", return_value=0),
    ):
        store.mark_retry("sk", base_seconds=20, max_seconds=20)

    schedule = store.retry_schedule(now=105)
    assert schedule.kind is _RetryScheduleKind.DUE_AT
    assert schedule.deadline == 120


def test_retry_schedule_retired_future_is_due_at(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    retired = _converged_session_with_retired_anchor(store, route)
    with (
        patch("plugins.platforms.slack.plan_cards.time.time", return_value=100),
        patch("plugins.platforms.slack.plan_cards.random.uniform", return_value=0),
    ):
        store.mark_retired_retry(
            "sk", retired["anchor_id"], base_seconds=5, max_seconds=5
        )

    schedule = store.retry_schedule(now=101)
    assert schedule.kind is _RetryScheduleKind.DUE_AT
    assert schedule.deadline == 105


def test_retry_schedule_mixed_work_picks_global_earliest(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    retired = _converged_session_with_retired_anchor(store, route)
    current = store.get_session("sk")
    store.record_desired_snapshot(current, current["last_desired_snapshot"])
    with (
        patch("plugins.platforms.slack.plan_cards.time.time", return_value=100),
        patch("plugins.platforms.slack.plan_cards.random.uniform", return_value=0),
    ):
        store.mark_retry("sk", base_seconds=20, max_seconds=20)
        store.mark_retired_retry(
            "sk", retired["anchor_id"], base_seconds=5, max_seconds=5
        )

    schedule = store.retry_schedule(now=101)
    assert schedule.kind is _RetryScheduleKind.DUE_AT
    assert schedule.deadline == 105


def test_retry_schedule_multiple_due_current_and_retired_is_due_now(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    _converged_session_with_retired_anchor(store, route)
    current = store.get_session("sk")
    store.record_desired_snapshot(current, current["last_desired_snapshot"])

    assert store.retry_schedule(now=time.time()).kind is _RetryScheduleKind.DUE_NOW
