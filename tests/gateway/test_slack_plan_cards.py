from __future__ import annotations

import json
import multiprocessing
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import pytest

from plugins.platforms.slack.plan_cards import (
    MAX_INTERACTIVE_TASKS,
    PlanCardStore,
    _RetryScheduleKind,
    build_plan_blocks,
    decode_action_value,
    encode_action_value,
    is_user_task_id,
    parse_todo_result,
    sign_private_metadata,
    verify_private_metadata,
)
from tools.todo_tool import MAX_TODO_ITEMS, TodoStore


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
        _snapshot(), revision=1, snapshot_hash="hash-1", signing_secret=b"secret"
    )
    second = build_plan_blocks(
        _snapshot(), revision=2, snapshot_hash="hash-1", signing_secret=b"secret"
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


def _action_elements(rendered, *, fallback: bool = False):
    return [
        element
        for block in (rendered.fallback_blocks if fallback else rendered.native_blocks)
        if block["type"] == "actions"
        for element in block["elements"]
    ]


def test_user_task_id_namespace_is_strict_over_canonical_ids() -> None:
    assert is_user_task_id("user:work")
    assert not is_user_task_id("user:")
    assert not is_user_task_id("task-1")
    assert not is_user_task_id("User:work")
    assert not is_user_task_id("սser:work")


def test_todo_store_canonical_ids_drive_plan_classification_and_controls(tmp_path) -> None:
    todo_store = TodoStore()
    todo_store.write([
        {
            "id": "  user:approve  ",
            "content": "Approve release",
            "status": "in_progress",
        },
        {
            "id": "  User:review  ",
            "content": "Wrong ASCII case",
            "status": "in_progress",
        },
        {
            "id": "  սser:lookalike  ",
            "content": "Unicode lookalike",
            "status": "in_progress",
        },
    ])
    canonical_snapshot = todo_store.read()
    assert [task["id"] for task in canonical_snapshot] == [
        "user:approve", "User:review", "սser:lookalike",
    ]

    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    state = store.record_desired_snapshot(route, canonical_snapshot)
    rendered = build_plan_blocks(
        state["last_desired_snapshot"],
        revision=state["desired_revision"],
        snapshot_hash=state["desired_hash"],
        signing_secret=b"secret",
    )

    assert [
        task["task_id"] for task in rendered.native_blocks[0]["tasks"]
    ] == ["user:approve", "User:review", "սser:lookalike"]
    checkbox = next(
        element for element in _action_elements(rendered)
        if element["action_id"] == "hermes_plan_complete"
    )
    cancel = next(
        element for element in _action_elements(rendered)
        if element["action_id"] == "hermes_plan_cancel"
    )
    assert [option["value"] for option in checkbox["options"]] == ["user:approve"]
    assert [option["value"] for option in cancel["options"]] == ["user:approve"]
    assert is_user_task_id(state["last_desired_snapshot"][0]["id"])
    assert not is_user_task_id(state["last_desired_snapshot"][1]["id"])
    assert not is_user_task_id(state["last_desired_snapshot"][2]["id"])


def test_renderer_shows_full_plan_but_controls_only_actionable_user_tasks() -> None:
    todos = [
        {"id": "agent:research", "content": "Agent research", "status": "pending"},
        {"id": "user:pending", "content": "Human pending", "status": "pending"},
        {"id": "user:done", "content": "Human done", "status": "completed"},
        {"id": "user:active", "content": "Human active", "status": "in_progress"},
        {"id": "user:cancelled", "content": "Human cancelled", "status": "cancelled"},
        {"id": "User:case", "content": "Wrong case", "status": "pending"},
        {"id": "սser:lookalike", "content": "Lookalike", "status": "pending"},
    ]
    rendered = build_plan_blocks(
        todos, revision=3, snapshot_hash="hash", signing_secret=b"secret"
    )
    assert [task["task_id"] for task in rendered.native_blocks[0]["tasks"]] == [
        task["id"] for task in todos
    ]
    assert "`user:pending`" in json.dumps(rendered.fallback_blocks)
    elements = _action_elements(rendered)
    checkbox = next(item for item in elements if item["action_id"] == "hermes_plan_complete")
    cancel = next(item for item in elements if item["action_id"] == "hermes_plan_cancel")
    assert [option["value"] for option in checkbox["options"]] == [
        "user:done", "user:active"
    ]
    assert [option["value"] for option in checkbox["initial_options"]] == ["user:done"]
    assert [option["value"] for option in cancel["options"]] == ["user:active"]
    assert next(item for item in elements if item["action_id"] == "hermes_plan_add")["text"]["text"] == "Add user task"


def test_pending_user_task_becomes_actionable_with_same_id_when_in_progress() -> None:
    pending = build_plan_blocks(
        [{"id": "user:stable", "content": "Stable", "status": "pending"}],
        revision=1,
        snapshot_hash="pending",
        signing_secret=b"secret",
    )
    assert [item["action_id"] for item in _action_elements(pending)] == [
        "hermes_plan_add", "hermes_plan_refresh",
    ]

    active = build_plan_blocks(
        [{"id": "user:stable", "content": "Stable", "status": "in_progress"}],
        revision=2,
        snapshot_hash="active",
        signing_secret=b"secret",
    )
    checkbox = next(
        item for item in _action_elements(active)
        if item["action_id"] == "hermes_plan_complete"
    )
    cancel = next(
        item for item in _action_elements(active)
        if item["action_id"] == "hermes_plan_cancel"
    )
    assert [option["value"] for option in checkbox["options"]] == ["user:stable"]
    assert "initial_options" not in checkbox
    assert [option["value"] for option in cancel["options"]] == ["user:stable"]


def test_renderer_limits_total_user_set_not_total_plan_and_keeps_refresh() -> None:
    large_agent_plan = [
        {"id": f"agent:{index}", "content": f"Agent {index}", "status": "pending"}
        for index in range(40)
    ] + [
        {"id": "user:one", "content": "One", "status": "in_progress"},
        {"id": "user:two", "content": "Two", "status": "completed"},
    ]
    normal = build_plan_blocks(
        large_agent_plan, revision=3, snapshot_hash="hash", signing_secret=b"secret"
    )
    assert len(normal.native_blocks[0]["tasks"]) == 42
    action_ids = [element["action_id"] for element in _action_elements(normal)]
    assert action_ids == [
        "hermes_plan_complete",
        "hermes_plan_cancel",
        "hermes_plan_add",
        "hermes_plan_refresh",
    ]
    assert [element["action_id"] for element in _action_elements(normal, fallback=True)] == action_ids

    at_capacity = build_plan_blocks(
        [{"id": f"user:{index}", "content": str(index), "status": "in_progress"}
         for index in range(MAX_INTERACTIVE_TASKS)],
        revision=4, snapshot_hash="capacity", signing_secret=b"secret",
    )
    assert "hermes_plan_add" not in [item["action_id"] for item in _action_elements(at_capacity)]

    oversized = build_plan_blocks(
        [{"id": f"user:{index}", "content": str(index), "status": "in_progress"}
         for index in range(MAX_INTERACTIVE_TASKS + 1)],
        revision=5, snapshot_hash="oversized", signing_secret=b"secret",
    )
    assert [item["action_id"] for item in _action_elements(oversized)] == [
        "hermes_plan_refresh"
    ]
    assert "controls unavailable" in json.dumps(oversized.fallback_blocks).lower()

    extreme = build_plan_blocks(
        [
            {"id": f"long-{index}", "content": "x" * 3000, "status": "pending"}
            for index in range(80)
        ],
        revision=6,
        snapshot_hash="extreme",
        signing_secret=b"secret",
    )
    fallback_text = json.dumps(extreme.fallback_blocks).lower()
    assert len(extreme.native_blocks[0]["tasks"]) == 80
    assert len(extreme.fallback_blocks) <= 50
    assert "display truncated" in fallback_text
    assert "no tasks were omitted" not in fallback_text
    assert [item["action_id"] for item in _action_elements(extreme, fallback=True)] == [
        "hermes_plan_add", "hermes_plan_refresh",
    ]

    extreme_with_users = build_plan_blocks(
        [
            {"id": f"agent:long-{index}", "content": "x" * 3000, "status": "pending"}
            for index in range(80)
        ] + [
            {"id": "user:active", "content": "Active", "status": "in_progress"},
            {"id": "user:done", "content": "Done", "status": "completed"},
        ],
        revision=7,
        snapshot_hash="extreme-users",
        signing_secret=b"secret",
    )
    assert len(extreme_with_users.native_blocks[0]["tasks"]) == 82
    assert len(extreme_with_users.fallback_blocks) <= 50
    assert "display truncated" in json.dumps(extreme_with_users.fallback_blocks).lower()
    assert [
        item["action_id"] for item in _action_elements(extreme_with_users, fallback=True)
    ] == [
        "hermes_plan_complete",
        "hermes_plan_cancel",
        "hermes_plan_add",
        "hermes_plan_refresh",
    ]


@pytest.mark.parametrize(
    ("count", "expect_add"),
    [(MAX_TODO_ITEMS - 1, True), (MAX_TODO_ITEMS, False)],
)
def test_renderer_add_control_follows_authoritative_todo_capacity(
    count: int, expect_add: bool,
) -> None:
    todos = [
        {"id": f"agent:{index}", "content": str(index), "status": "pending"}
        for index in range(count - 2)
    ] + [
        {"id": "user:active", "content": "Active", "status": "in_progress"},
        {"id": "user:done", "content": "Done", "status": "completed"},
    ]
    rendered = build_plan_blocks(
        todos, revision=1, snapshot_hash="capacity", signing_secret=b"secret",
    )
    action_ids = [item["action_id"] for item in _action_elements(rendered)]
    assert ("hermes_plan_add" in action_ids) is expect_add
    assert "hermes_plan_complete" in action_ids
    assert "hermes_plan_cancel" in action_ids
    assert "hermes_plan_refresh" in action_ids
    checkbox = next(
        item for item in _action_elements(rendered)
        if item["action_id"] == "hermes_plan_complete"
    )
    cancel = next(
        item for item in _action_elements(rendered)
        if item["action_id"] == "hermes_plan_cancel"
    )
    assert [option["value"] for option in checkbox["options"]] == [
        "user:active", "user:done",
    ]
    assert [option["value"] for option in checkbox["initial_options"]] == [
        "user:done",
    ]
    assert [option["value"] for option in cancel["options"]] == ["user:active"]


def test_future_and_cancelled_user_tasks_do_not_consume_mutation_capacity() -> None:
    read_only = [
        {
            "id": f"user:read-only-{index}",
            "content": f"Read only {index}",
            "status": "cancelled" if index % 2 else "pending",
        }
        for index in range(MAX_INTERACTIVE_TASKS + 5)
    ]
    rendered = build_plan_blocks(
        read_only + [{"id": "user:active", "content": "Active", "status": "in_progress"}],
        revision=7,
        snapshot_hash="inactive-capacity",
        signing_secret=b"secret",
    )

    assert len(rendered.native_blocks[0]["tasks"]) == MAX_INTERACTIVE_TASKS + 6
    elements = _action_elements(rendered)
    assert [item["action_id"] for item in elements] == [
        "hermes_plan_complete",
        "hermes_plan_cancel",
        "hermes_plan_add",
        "hermes_plan_refresh",
    ]
    checkbox = next(item for item in elements if item["action_id"] == "hermes_plan_complete")
    cancel = next(item for item in elements if item["action_id"] == "hermes_plan_cancel")
    assert [option["value"] for option in checkbox["options"]] == ["user:active"]
    assert [option["value"] for option in cancel["options"]] == ["user:active"]
    assert "controls unavailable" not in json.dumps(rendered.fallback_blocks).lower()
    assert [item["action_id"] for item in _action_elements(rendered, fallback=True)] == [
        item["action_id"] for item in elements
    ]


def test_fallback_splits_single_overlong_lines_without_undisclosed_loss() -> None:
    todos = [
        {"id": "agent:long", "content": "a" * 3000, "status": "pending"},
        {"id": "user:long", "content": "u" * 3000, "status": "in_progress"},
    ]
    rendered = build_plan_blocks(
        todos,
        revision=8,
        snapshot_hash="long-lines",
        signing_secret=b"secret",
    )
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
    assert len(rendered.fallback_blocks) <= 50
    assert "display truncated" not in json.dumps(rendered.fallback_blocks).lower()
    assert [item["action_id"] for item in _action_elements(rendered, fallback=True)] == [
        "hermes_plan_complete",
        "hermes_plan_cancel",
        "hermes_plan_add",
        "hermes_plan_refresh",
    ]


@pytest.mark.parametrize("task_prefix", ["agent", "user"])
def test_native_task_titles_disclose_content_truncation_at_boundary(task_prefix) -> None:
    exact_content = "x" * 3000
    over_content = "y" * 3001
    rendered = build_plan_blocks(
        [
            {"id": f"{task_prefix}:exact", "content": exact_content, "status": "pending"},
            {"id": f"{task_prefix}:over", "content": over_content, "status": "pending"},
        ],
        revision=9,
        snapshot_hash=f"title-{task_prefix}",
        signing_secret=b"secret",
    )
    exact_title, over_title = [
        task["title"] for task in rendered.native_blocks[0]["tasks"]
    ]

    assert exact_title == exact_content
    assert len(exact_title) == 3000
    assert len(over_title) <= 3000
    assert over_title.endswith("... [truncated]")
    assert over_title != over_content[:3000]


def test_add_task_is_disabled_without_signing_material() -> None:
    rendered = build_plan_blocks(
        [{"id": "user:a", "content": "A", "status": "pending"}],
        revision=1, snapshot_hash="hash", signing_secret=None
    )
    actions = rendered.native_blocks[-1]["elements"]
    assert "hermes_plan_add" not in [item["action_id"] for item in actions]


def test_action_values_and_modal_metadata_are_tamper_evident() -> None:
    payload = {
        "session_key": "agent:main:slack:group:C1:1.0",
        "revision": 7,
        "snapshot_hash": "abc",
    }
    value = encode_action_value(payload)
    assert decode_action_value(value) == payload

    signed = sign_private_metadata(payload, b"secret")
    assert verify_private_metadata(signed, b"secret") == payload
    raw = json.loads(signed)
    raw["payload"]["revision"] = 8
    assert verify_private_metadata(json.dumps(raw), b"secret") is None
    assert sign_private_metadata(payload, None) is None


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


def test_validate_action_returns_exact_matching_snapshot(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    original = store.record_desired_snapshot(route, _snapshot(1))
    assert store.mark_applied(
        "sk", revision=original["desired_revision"],
        snapshot_hash=original["desired_hash"], message_ts="20.0",
    )
    metadata = {
        **route, "message_ts": "20.0", "revision": original["desired_revision"],
        "snapshot_hash": original["desired_hash"], "task_ids": ["user:new"],
        "action_kind": "add_user_task", "add_task_ids": ["user:new"],
        "add_task_content": "New", "add_task_status": "in_progress",
        "action_user_id": "U1",
        "action_dedupe_id": "dedupe",
    }
    assert store.consume_action_id("dedupe")

    validated = store.validate_action(metadata)
    store.record_desired_snapshot(
        {**route, "session_id": "sid-new", "channel_id": "C2", "thread_ts": "11.0"},
        _snapshot(2),
    )

    assert validated["session_id"] == "sid"
    assert validated["channel_id"] == "C1"
    assert validated["desired_revision"] == original["desired_revision"]


def test_validate_action_rejects_duplicate_snapshot_ids_and_forged_metadata(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    state = store.record_desired_snapshot(route, [
        {"id": "agent:a", "content": "Agent", "status": "pending"},
        {"id": "user:a", "content": "User", "status": "in_progress"},
    ])
    assert store.mark_applied(
        "sk", revision=state["desired_revision"], snapshot_hash=state["desired_hash"],
        message_ts="20.0",
    )
    base = {
        **route, "message_ts": "20.0", "revision": state["desired_revision"],
        "snapshot_hash": state["desired_hash"], "action_user_id": "U1",
        "action_dedupe_id": "dedupe",
    }
    assert store.consume_action_id("dedupe")
    assert store.validate_action({
        **base, "action_kind": "complete_reopen", "task_ids": ["agent:a", "user:a"],
        "complete_task_ids": ["agent:a", "user:a"], "complete_task_status": "completed",
        "reopen_task_ids": [], "reopen_task_status": "in_progress",
    }) is None
    assert store.validate_action({
        **base, "action_kind": "cancel", "task_ids": ["agent:a"],
        "cancel_task_ids": ["agent:a"], "cancel_task_status": "cancelled",
    }) is None
    assert store.validate_action({
        **base, "action_kind": "add_user_task", "task_ids": ["user:new"],
        "add_task_ids": ["user:replacement"], "add_task_content": "New",
        "add_task_status": "in_progress",
    }) is None

    data = json.loads(store.state_path.read_text(encoding="utf-8"))
    duplicate = dict(data["sessions"]["sk"]["last_desired_snapshot"][1])
    data["sessions"]["sk"]["last_desired_snapshot"].append(duplicate)
    store.state_path.write_text(json.dumps(data), encoding="utf-8")
    assert store.validate_action({
        **base, "action_kind": "cancel", "task_ids": ["user:a"],
        "cancel_task_ids": ["user:a"], "cancel_task_status": "cancelled",
    }) is None


def test_validate_add_capacity_counts_only_in_progress_and_completed_user_tasks(tmp_path) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    read_only = [
        {
            "id": f"user:read-only-{index}",
            "content": str(index),
            "status": "cancelled" if index % 2 else "pending",
        }
        for index in range(MAX_INTERACTIVE_TASKS + 3)
    ]
    below_capacity = store.record_desired_snapshot(route, read_only + [
        {"id": "user:active", "content": "Active", "status": "in_progress"},
    ])
    assert store.mark_applied(
        "sk", revision=below_capacity["desired_revision"],
        snapshot_hash=below_capacity["desired_hash"], message_ts="20.0",
    )
    metadata = {
        **route, "message_ts": "20.0", "revision": below_capacity["desired_revision"],
        "snapshot_hash": below_capacity["desired_hash"], "task_ids": ["user:new"],
        "action_kind": "add_user_task", "add_task_ids": ["user:new"],
        "add_task_content": "New", "add_task_status": "in_progress",
        "action_user_id": "U1",
        "action_dedupe_id": "below-capacity",
    }
    assert store.consume_action_id("below-capacity")
    assert store.validate_action(metadata) is not None

    at_capacity = store.record_desired_snapshot(route, read_only + [
        {
            "id": f"user:mutable-{index}",
            "content": str(index),
            "status": "in_progress" if index % 2 else "completed",
        }
        for index in range(MAX_INTERACTIVE_TASKS)
    ])
    assert store.mark_applied(
        "sk", revision=at_capacity["desired_revision"],
        snapshot_hash=at_capacity["desired_hash"], message_ts="20.0",
    )
    at_capacity_metadata = {
        **metadata,
        "revision": at_capacity["desired_revision"],
        "snapshot_hash": at_capacity["desired_hash"],
        "action_dedupe_id": "at-capacity",
    }
    assert store.consume_action_id("at-capacity")
    assert store.validate_action(at_capacity_metadata) is None


@pytest.mark.parametrize(
    ("count", "accepted"),
    [(MAX_TODO_ITEMS - 1, True), (MAX_TODO_ITEMS, False)],
)
def test_validate_add_rejects_authoritative_todo_capacity(
    tmp_path, count: int, accepted: bool,
) -> None:
    store = PlanCardStore(tmp_path)
    route = {
        "session_key": "sk", "session_id": "sid", "team_id": "T1",
        "channel_id": "C1", "thread_ts": "10.0",
    }
    snapshot = [
        {"id": f"agent:{index}", "content": str(index), "status": "pending"}
        for index in range(count)
    ]
    state = store.record_desired_snapshot(route, snapshot)
    assert store.mark_applied(
        "sk", revision=state["desired_revision"],
        snapshot_hash=state["desired_hash"], message_ts="20.0",
    )
    dedupe_id = f"capacity-{count}"
    assert store.consume_action_id(dedupe_id)
    metadata = {
        **route, "message_ts": "20.0", "revision": state["desired_revision"],
        "snapshot_hash": state["desired_hash"], "task_ids": ["user:new"],
        "action_kind": "add_user_task", "add_task_ids": ["user:new"],
        "add_task_content": "New", "add_task_status": "in_progress",
        "action_user_id": "U1", "action_dedupe_id": dedupe_id,
    }
    assert (store.validate_action(metadata) is not None) is accepted


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
    store.request_refresh("sk")
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
    store.request_refresh("sk")

    assert store.retry_schedule(now=time.time()).kind is _RetryScheduleKind.DUE_NOW
