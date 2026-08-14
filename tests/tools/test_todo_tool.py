"""Tests for the todo tool module."""

import json

from tools.todo_tool import TodoStore, todo_tool


class TestWriteAndRead:
    def test_write_replaces_list(self):
        store = TodoStore()
        items = [
            {"id": "1", "content": "First task", "status": "pending"},
            {"id": "2", "content": "Second task", "status": "in_progress"},
        ]
        result = store.write(items)
        assert len(result) == 2
        assert result[0]["id"] == "1"
        assert result[1]["status"] == "in_progress"


    def test_write_deduplicates_duplicate_ids(self):
        store = TodoStore()
        result = store.write([
            {"id": "1", "content": "First version", "status": "pending"},
            {"id": "2", "content": "Other task", "status": "pending"},
            {"id": "1", "content": "Latest version", "status": "in_progress"},
        ])
        assert result == [
            {"id": "2", "content": "Other task", "status": "pending"},
            {"id": "1", "content": "Latest version", "status": "in_progress"},
        ]


class TestHasItems:
    def test_empty_store(self):
        store = TodoStore()
        assert store.has_items() is False

    def test_non_empty_store(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "x", "status": "pending"}])
        assert store.has_items() is True


class TestFormatForInjection:
    def test_empty_returns_none(self):
        store = TodoStore()
        assert store.format_for_injection() is None

    def test_non_empty_has_markers(self):
        store = TodoStore()
        store.write([
            {"id": "1", "content": "Do thing", "status": "completed"},
            {"id": "2", "content": "Next", "status": "pending"},
            {"id": "3", "content": "Working", "status": "in_progress"},
        ])
        text = store.format_for_injection()
        # Completed items are filtered out of injection
        assert "[x]" not in text
        assert "Do thing" not in text
        # Active items are included
        assert "[ ]" in text
        assert "[>]" in text
        assert "Next" in text
        assert "Working" in text
        assert "context compression" in text.lower()

    def test_compaction_downgrades_active_items_until_revalidated(self):
        store = TodoStore()
        store.write([
            {"id": "keep", "content": "Review evidence", "status": "pending"},
            {"id": "work", "content": "Apply change", "status": "in_progress"},
            {"id": "done", "content": "Finished", "status": "completed"},
        ])

        assert store.mark_active_for_reconfirmation() == 2
        items = store.read()
        assert [item["status"] for item in items] == [
            "needs_reconfirmation",
            "needs_reconfirmation",
            "completed",
        ]

        text = store.format_for_injection()
        assert text.count("(needs_reconfirmation)") == 2
        assert "not actionable" in text
        assert "Apply change" in text

        store.write(
            [{"id": "work", "status": "in_progress"}],
            merge=True,
        )
        assert store.read()[1]["status"] == "in_progress"

    def test_reconfirmation_is_idempotent(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "Task", "status": "pending"}])

        assert store.mark_active_for_reconfirmation() == 1
        assert store.mark_active_for_reconfirmation() == 0

    def test_reconfirmation_guidance_is_emitted_once_for_large_plans(self):
        store = TodoStore()
        store.write([
            {"id": str(i), "content": f"Task {i}", "status": "pending"}
            for i in range(256)
        ])

        store.mark_active_for_reconfirmation()
        text = store.format_for_injection()

        assert text.count("are not actionable") == 1
        assert len(text) < 20_000


class TestMergeMode:
    def test_update_existing_by_id(self):
        store = TodoStore()
        store.write([
            {"id": "1", "content": "Original", "status": "pending"},
        ])
        store.write(
            [{"id": "1", "status": "completed"}],
            merge=True,
        )
        items = store.read()
        assert len(items) == 1
        assert items[0]["status"] == "completed"
        assert items[0]["content"] == "Original"

    def test_merge_appends_new(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "First", "status": "pending"}])
        store.write(
            [{"id": "2", "content": "Second", "status": "pending"}],
            merge=True,
        )
        items = store.read()
        assert len(items) == 2


class TestTodoToolFunction:
    def test_read_mode(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "Task", "status": "pending"}])
        result = json.loads(todo_tool(store=store))
        assert result["summary"]["total"] == 1
        assert result["summary"]["pending"] == 1


    def test_no_store_returns_error(self):
        result = json.loads(todo_tool())
        assert "error" in result

    def test_reconfirmation_status_is_server_managed(self):
        from tools.todo_tool import TODO_SCHEMA

        writable_statuses = TODO_SCHEMA["parameters"]["properties"]["todos"][
            "items"
        ]["properties"]["status"]["enum"]

        assert "needs_reconfirmation" not in writable_statuses

    def test_reconfirmation_uses_backward_compatible_wire_status(self):
        store = TodoStore()
        store.write([{"id": "1", "content": "Task", "status": "pending"}])
        store.mark_active_for_reconfirmation()

        result = json.loads(todo_tool(store=store))

        assert result["todos"] == [
            {
                "id": "1",
                "content": "Task",
                "status": "pending",
                "needs_reconfirmation": True,
            }
        ]
        assert result["summary"]["needs_reconfirmation"] == 1
        assert store.read()[0]["status"] == "needs_reconfirmation"

    def test_wire_flag_rehydrates_internal_reconfirmation_state(self):
        store = TodoStore()

        store.write(
            [
                {
                    "id": "1",
                    "content": "Task",
                    "status": "pending",
                    "needs_reconfirmation": True,
                }
            ]
        )

        assert store.read()[0]["status"] == "needs_reconfirmation"


class TestTodoStoreBounds:
    """Bounds on persisted todo state (GHSA-5g4g-6jrg-mw3g hardening).

    The todo list is re-injected into context after every compression event,
    so an unbounded item — whether authored by the model or replayed from
    caller-supplied history on the API server's _hydrate_todo_store path —
    would defeat the compression it rides through. These pin the caps.
    Not a security boundary (the API surface is authenticated and the caller
    supplies their own history); this is footgun containment / parity.
    """

    def test_oversized_content_is_truncated(self):
        from tools.todo_tool import MAX_TODO_CONTENT_CHARS
        store = TodoStore()
        store.write([{"id": "1", "content": "A" * 50001, "status": "pending"}])
        item = store.read()[0]
        assert len(item["content"]) <= MAX_TODO_CONTENT_CHARS
        assert item["content"].endswith("… [truncated]")

    def test_injection_block_is_bounded(self):
        from tools.todo_tool import MAX_TODO_CONTENT_CHARS
        store = TodoStore()
        store.write([{"id": "1", "content": "A" * 50001, "status": "pending"}])
        inj = store.format_for_injection()
        # Before the fix this was ~50085 chars; now it tracks the cap.
        assert len(inj) < MAX_TODO_CONTENT_CHARS + 200


    def test_item_count_is_bounded(self):
        from tools.todo_tool import MAX_TODO_ITEMS
        store = TodoStore()
        store.write([
            {"id": str(i), "content": f"task {i}", "status": "pending"}
            for i in range(5000)
        ])
        assert len(store.read()) == MAX_TODO_ITEMS

    def test_normal_list_is_unchanged(self):
        """No regression: ordinary plans pass through untouched (no marker,
        same content, same order)."""
        store = TodoStore()
        store.write([
            {"id": "1", "content": "write the report", "status": "in_progress"},
            {"id": "2", "content": "review PR", "status": "pending"},
        ])
        items = store.read()
        assert [i["content"] for i in items] == ["write the report", "review PR"]
        assert "[truncated]" not in items[0]["content"]


class TestTodoPersistenceCallback:
    def test_write_and_state_transition_notify_with_copies(self):
        observed = []
        store = TodoStore(on_change=observed.append)

        store.write([{"id": "a", "content": "A", "status": "pending"}])
        store.mark_active_for_reconfirmation()

        assert [states[0]["status"] for states in observed] == [
            "pending",
            "needs_reconfirmation",
        ]
        observed[0][0]["status"] = "completed"
        assert store.read()[0]["status"] == "needs_reconfirmation"

    def test_notify_can_be_deferred_until_atomic_commit(self):
        observed = []
        store = TodoStore(on_change=observed.append)
        store.write(
            [{"id": "a", "content": "A", "status": "pending"}],
            notify=False,
        )
        store.mark_active_for_reconfirmation(notify=False)

        assert observed == []
        store.persist()
        assert observed[0][0]["status"] == "needs_reconfirmation"
