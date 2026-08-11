"""Deterministic tests for durable TodoStore timing."""

import json

from tools.todo_tool import (
    MAX_TODO_CONTENT_CHARS,
    MAX_TODO_ITEMS,
    MAX_TODO_RESULT_CHARS,
    TodoStore,
    todo_tool,
)


class FakeClock:
    def __init__(self, value: float) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def _item(status: str, task_id: str = "task") -> dict:
    return {"id": task_id, "content": "work", "status": status}


def test_active_time_accumulates_only_while_in_progress() -> None:
    clock = FakeClock(100)
    store = TodoStore(clock=clock)
    store.write([_item("pending")])

    clock.value = 110
    store.write([_item("in_progress")])
    clock.value = 130
    store.write([_item("pending")])
    clock.value = 135
    store.write([_item("in_progress")])
    clock.value = 150
    store.write([_item("completed")])

    timing = store.snapshot()["timing"]
    assert timing["items"]["task"]["active_seconds"] == 35
    assert timing["items"]["task"]["finished_at"] == 150
    assert timing["cycle"]["elapsed_seconds"] == 50

    clock.value = 200
    frozen = store.snapshot()["timing"]
    assert frozen["items"]["task"]["active_seconds"] == 35
    assert frozen["cycle"]["elapsed_seconds"] == 50


def test_timing_survives_tool_result_hydration_and_keeps_running() -> None:
    first_clock = FakeClock(100)
    first = TodoStore(clock=first_clock)
    first.write([_item("in_progress")])
    first_clock.value = 130
    persisted = json.loads(todo_tool(store=first))

    resumed_clock = FakeClock(150)
    resumed = TodoStore(clock=resumed_clock)
    resumed.hydrate(persisted)
    restored = resumed.snapshot()["timing"]
    assert restored["cycle"]["id"] == 1
    assert restored["cycle"]["elapsed_seconds"] == 50
    assert restored["items"]["task"]["active_seconds"] == 50

    resumed_clock.value = 160
    resumed.write([_item("completed")])
    completed = resumed.snapshot()["timing"]
    assert completed["cycle"]["elapsed_seconds"] == 60
    assert completed["items"]["task"]["active_seconds"] == 60


def test_repeated_hydration_preserves_fractional_closed_interval() -> None:
    clock = FakeClock(100.25)
    store = TodoStore(clock=clock)
    store.write([_item("in_progress")])
    clock.value = 101.10
    store.write([_item("pending")])
    expected = store.snapshot()["timing"]["items"]["task"][
        "accumulated_active_seconds"
    ]

    for value in (150.0, 200.0, 250.0):
        resumed = TodoStore(clock=FakeClock(value))
        resumed.hydrate(store.snapshot())
        actual = resumed.snapshot()["timing"]["items"]["task"][
            "accumulated_active_seconds"
        ]
        assert actual == expected
        store = resumed


def test_legacy_hydration_is_unknown_instead_of_fabricating_zero() -> None:
    clock = FakeClock(500)
    store = TodoStore(clock=clock)
    store.hydrate({"todos": [_item("completed")]})

    timing = store.snapshot()["timing"]
    assert timing["cycle"]["known"] is False
    assert timing["cycle"]["elapsed_seconds"] is None
    assert timing["items"]["task"]["known"] is False
    assert timing["items"]["task"]["active_seconds"] is None


def test_terminal_cycle_rotates_only_when_new_active_work_arrives() -> None:
    clock = FakeClock(10)
    store = TodoStore(clock=clock)
    store.write([_item("in_progress")])
    clock.value = 20
    store.write([_item("completed")])
    completed = store.snapshot()["timing"]
    assert completed["cycle"]["id"] == 1
    assert completed["cycle"]["elapsed_seconds"] == 10

    clock.value = 50
    store.write([_item("completed")])
    still_completed = store.snapshot()["timing"]
    assert still_completed["cycle"]["id"] == 1
    assert still_completed["cycle"]["elapsed_seconds"] == 10

    clock.value = 80
    store.write([_item("in_progress")])
    next_cycle = store.snapshot()["timing"]
    assert next_cycle["cycle"]["id"] == 2
    assert next_cycle["cycle"]["started_at"] == 80
    assert next_cycle["cycle"]["elapsed_seconds"] == 0
    assert next_cycle["items"]["task"]["active_seconds"] == 0


def test_cleared_list_closes_cycle_and_next_list_starts_another() -> None:
    clock = FakeClock(10)
    store = TodoStore(clock=clock)
    store.write([_item("in_progress")])
    clock.value = 20
    store.write([])
    clock.value = 40
    store.write([_item("pending", "next")])

    timing = store.snapshot()["timing"]
    assert timing["cycle"]["id"] == 2
    assert timing["cycle"]["started_at"] == 40


def test_new_cycle_preserves_carried_terminal_membership() -> None:
    clock = FakeClock(100)
    store = TodoStore(clock=clock)
    old = {"id": "old", "content": "old", "status": "pending"}
    store.write([old])
    clock.value = 110
    store.write([{**old, "status": "in_progress"}])
    clock.value = 120
    store.write([{**old, "status": "completed"}])
    clock.value = 130
    store.write([
        {**old, "status": "completed"},
        {"id": "new", "content": "new", "status": "pending"},
    ])

    timing = store.snapshot()["timing"]
    assert timing["cycle"]["id"] == 2
    assert timing["items"]["old"]["cycle_id"] == 1
    assert timing["items"]["old"]["active_seconds"] == 10
    assert timing["items"]["new"]["cycle_id"] == 2


def test_first_observation_of_terminal_item_has_unknown_duration() -> None:
    store = TodoStore(clock=FakeClock(100))
    store.write([_item("completed")])
    timing = store.snapshot()["timing"]
    assert timing["cycle"]["known"] is False
    assert timing["items"]["task"]["known"] is False


def test_terminal_first_clear_restart_rotates_cycle_for_new_work() -> None:
    first = TodoStore(clock=FakeClock(100))
    first.write([_item("completed")])
    assert first.snapshot()["timing"]["cycle"]["id"] == 1
    first.write([])

    resumed = TodoStore(clock=FakeClock(200))
    resumed.hydrate(first.snapshot())
    resumed.write([_item("pending", "next")])

    timing = resumed.snapshot()["timing"]
    assert timing["cycle"]["id"] == 2
    assert timing["cycle"]["known"] is True
    assert timing["cycle"]["started_at"] == 200
    assert timing["items"]["next"]["cycle_id"] == 2


def test_model_supplied_timing_fields_are_ignored() -> None:
    clock = FakeClock(100)
    store = TodoStore(clock=clock)
    store.write([{**_item("in_progress"), "timing": {"active_seconds": 999999}}])
    timing = store.snapshot()["timing"]
    assert timing["items"]["task"]["active_seconds"] == 0
    assert timing["items"]["task"]["created_at"] == 100


def test_hydration_rejects_impossible_timestamp_order() -> None:
    snapshot = {
        "todos": [_item("completed")],
        "timing": {
            "schema_version": 1,
            "cycle": {
                "id": 1,
                "known": True,
                "started_at": 100.0,
                "finished_at": 90.0,
            },
            "items": {
                "task": {
                    "known": True,
                    "cycle_id": 1,
                    "created_at": 100.0,
                    "started_at": 110.0,
                    "finished_at": 90.0,
                    "accumulated_active_seconds": 5.0,
                    "active_since": None,
                }
            },
        },
    }
    store = TodoStore(clock=FakeClock(200))
    store.hydrate(snapshot)
    timing = store.snapshot()["timing"]
    assert timing["cycle"]["known"] is False
    assert timing["items"]["task"]["known"] is False


def test_hydration_marks_forged_excessive_item_timing_unknown() -> None:
    snapshot = {
        "todos": [_item("in_progress")],
        "timing": {
            "schema_version": 1,
            "source": "live_runtime",
            "cycle": {
                "id": 1,
                "known": True,
                "started_at": 100.0,
                "finished_at": None,
            },
            "items": {
                "task": {
                    "known": True,
                    "cycle_id": 1,
                    "created_at": 100.0,
                    "started_at": 110.0,
                    "finished_at": None,
                    "accumulated_active_seconds": 1_000_000_000.0,
                    "active_since": 110.0,
                }
            },
        },
    }
    store = TodoStore(clock=FakeClock(200))
    store.hydrate(snapshot)
    timing = store.snapshot()["timing"]
    assert timing["source"] == "paired_history"
    assert timing["cycle"]["known"] is True
    assert timing["items"]["task"]["known"] is False


def test_hydration_rejects_future_cycle_timestamp() -> None:
    snapshot = {
        "todos": [_item("pending")],
        "timing": {
            "schema_version": 1,
            "cycle": {
                "id": 1,
                "known": True,
                "started_at": 1000.0,
                "finished_at": None,
            },
            "items": {},
        },
    }
    store = TodoStore(clock=FakeClock(200))
    store.hydrate(snapshot)
    assert store.snapshot()["timing"]["cycle"]["known"] is False


def test_hydration_rejects_finished_cycle_with_active_item() -> None:
    source_clock = FakeClock(100)
    source = TodoStore(clock=source_clock)
    source.write([_item("in_progress")])
    snapshot = source.snapshot()
    snapshot["timing"]["cycle"]["finished_at"] = 120.0

    restored = TodoStore(clock=FakeClock(130))
    restored.hydrate(snapshot)
    timing = restored.snapshot()["timing"]
    assert timing["cycle"]["known"] is False
    assert timing["items"]["task"]["known"] is False


def test_hydration_rejects_terminal_list_without_finished_cycle() -> None:
    clock = FakeClock(100)
    source = TodoStore(clock=clock)
    source.write([_item("in_progress")])
    clock.value = 120
    source.write([_item("completed")])
    snapshot = source.snapshot()
    snapshot["timing"]["cycle"]["finished_at"] = None

    restored = TodoStore(clock=FakeClock(200))
    restored.hydrate(snapshot)
    timing = restored.snapshot()["timing"]
    assert timing["cycle"]["known"] is False
    assert timing["items"]["task"]["known"] is False


def test_hydration_rejects_active_item_from_prior_cycle() -> None:
    source = TodoStore(clock=FakeClock(100))
    source.write([_item("pending")])
    snapshot = source.snapshot()
    snapshot["timing"]["cycle"]["id"] = 2

    restored = TodoStore(clock=FakeClock(200))
    restored.hydrate(snapshot)
    timing = restored.snapshot()["timing"]
    assert timing["cycle"]["known"] is True
    assert timing["items"]["task"]["known"] is False


def test_hydration_rejects_active_time_beyond_terminal_window() -> None:
    clock = FakeClock(100)
    source = TodoStore(clock=clock)
    source.write([_item("in_progress")])
    clock.value = 110
    source.write([_item("completed")])
    snapshot = source.snapshot()
    snapshot["timing"]["items"]["task"]["accumulated_active_seconds"] = 90.0

    restored = TodoStore(clock=FakeClock(200))
    restored.hydrate(snapshot)
    timing = restored.snapshot()["timing"]
    assert timing["cycle"]["known"] is True
    assert timing["items"]["task"]["known"] is False


def test_legacy_unknown_cycle_never_emits_partially_known_items() -> None:
    clock = FakeClock(500)
    store = TodoStore(clock=clock)
    store.hydrate({"todos": [_item("pending")]})
    clock.value = 510
    store.write([
        _item("pending"),
        {"id": "new", "content": "new", "status": "pending"},
    ])
    persisted = store.snapshot()
    assert persisted["timing"]["cycle"]["id"] is None
    assert persisted["timing"]["items"]["new"]["known"] is False

    resumed = TodoStore(clock=FakeClock(520))
    resumed.hydrate(persisted)
    assert resumed.snapshot()["timing"]["items"]["new"]["known"] is False


def test_cancelled_item_records_terminal_timestamp_and_active_time() -> None:
    clock = FakeClock(100)
    store = TodoStore(clock=clock)
    store.write([_item("pending")])
    clock.value = 110
    store.write([_item("in_progress")])
    clock.value = 120
    store.write([_item("cancelled")])
    item = store.snapshot()["timing"]["items"]["task"]
    assert item["finished_at"] == 120
    assert item["active_seconds"] == 10


def test_todo_result_keeps_item_shape_and_adds_timing_envelope() -> None:
    store = TodoStore(clock=FakeClock(100))
    result = json.loads(todo_tool(todos=[_item("pending")], store=store))
    assert result["todos"] == [_item("pending")]
    assert result["timing"]["schema_version"] == 1
    assert result["timing"]["cycle"]["known"] is True


def test_timing_envelope_has_bounded_result_overhead() -> None:
    items = [
        {"id": str(index), "content": "task", "status": "pending"}
        for index in range(MAX_TODO_ITEMS)
    ]
    store = TodoStore(clock=FakeClock(100))
    result = json.loads(todo_tool(todos=items, store=store))
    full = len(json.dumps(result))
    legacy_shape = len(
        json.dumps({"todos": result["todos"], "summary": result["summary"]})
    )
    assert full - legacy_shape <= 64_000


def test_maximum_valid_todo_result_fits_durable_snapshot_bound() -> None:
    items = [
        {
            "id": f"{index}:" + "\\" * 400,
            "content": "\\" * MAX_TODO_CONTENT_CHARS,
            "status": "pending",
        }
        for index in range(MAX_TODO_ITEMS)
    ]
    result = todo_tool(todos=items, store=TodoStore(clock=FakeClock(100)))
    assert len(result) <= MAX_TODO_RESULT_CHARS
    parsed = json.loads(result)
    ids = [item["id"] for item in parsed["todos"]]
    assert len(ids) == MAX_TODO_ITEMS
    assert len(set(ids)) == MAX_TODO_ITEMS
    assert all(len(item_id) <= 128 for item_id in ids)
