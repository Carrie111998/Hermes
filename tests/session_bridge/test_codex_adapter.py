from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

import pytest

from agent.transports.codex_app_server import CodexAppServerClient
from hermes_state import SessionDB
import session_bridge.codex_adapter as codex_adapter_module
from session_bridge.codex_adapter import CodexSourceAdapter, CodexThreadSummary
from session_bridge.models import (
    BridgeMarkerPayload,
    OriginKind,
    Provider,
    encode_bridge_marker,
)
from session_bridge.store import SessionBridgeStore


FIXTURES = Path(__file__).parent / "fixtures" / "codex"
SECRET = b"codex-adapter-test-secret"


class FakeRequestClient:
    def __init__(self, responses: dict[str, list[dict[str, Any] | Exception]]) -> None:
        self.responses = {key: list(values) for key, values in responses.items()}
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def request(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        self.calls.append((method, deepcopy(params), timeout))
        response = self.responses[method].pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


class FakeInitializingClient(FakeRequestClient):
    def __init__(self, responses: dict[str, list[dict[str, Any] | Exception]]) -> None:
        super().__init__(responses)
        self.initialize_calls: list[dict[str, Any]] = []

    def initialize(self, **kwargs: Any) -> dict[str, Any]:
        self.initialize_calls.append(deepcopy(kwargs))
        return {"userAgent": "synthetic"}


class FakeRetryingInitializeClient(FakeInitializingClient):
    def initialize(self, **kwargs: Any) -> dict[str, Any]:
        self.initialize_calls.append(deepcopy(kwargs))
        if len(self.initialize_calls) == 1:
            raise RuntimeError("synthetic initialization failure")
        return {"userAgent": "synthetic"}


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _summary(
    *, native_id: str = "thread-active", archived: bool = False
) -> CodexThreadSummary:
    return CodexThreadSummary(
        native_id=native_id,
        title="Active work",
        cwd="C:/work/active",
        started_at=1783850400.0,
        last_active=1783850700.0,
        archived=archived,
        revision="revision-1",
    )


def _marker(bridge_id: str, *, target: Provider = Provider.CODEX) -> str:
    return encode_bridge_marker(
        BridgeMarkerPayload(
            bridge_id=bridge_id,
            source_session_id="claude:source",
            target_provider=target,
            policy_generation=1,
        ),
        SECRET,
    )


def _read_with_items(*items: dict[str, Any]) -> dict[str, Any]:
    return {
        "thread": {
            "id": "thread-active",
            "turns": [{"id": "turn-1", "items": list(items)}],
        }
    }


class TestInventory:
    def test_find_sidebar_thread_reuses_scanner_cache_without_relisting(self) -> None:
        client = FakeInitializingClient({
            "thread/list": [
                {
                    "data": [
                        {
                            "id": "thread-cached",
                            "title": "Cached registration",
                            "cwd": "C:/work/cached",
                            "createdAt": 1783850400,
                            "updatedAt": 1783850700,
                            "archived": False,
                            "revision": "cached-revision",
                        }
                    ]
                }
            ]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)
        assert [
            summary.native_id
            for summary in adapter.list_full_inventory(archived=False)
        ] == ["thread-cached"]
        client.calls.clear()

        found = adapter.find_sidebar_thread(
            "thread-cached",
            deadline=None,
            page_cap=1,
        )

        assert found is not None
        assert found.native_id == "thread-cached"
        assert found.revision == "cached-revision"
        assert client.calls == []

    def test_initializes_lazily_once_and_pages_aliases(self) -> None:
        pages = _fixture("thread-list-pages.json")
        client = FakeInitializingClient({"thread/list": pages["active"]})
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)
        assert client.initialize_calls == []

        summaries = adapter.list_inventory(archived=False)

        assert [summary.native_id for summary in summaries] == [
            "thread-active",
            "thread-fallback",
        ]
        assert client.initialize_calls == [
            {"capabilities": {"experimentalApi": True}}
        ]
        list_calls = [call for call in client.calls if call[0] == "thread/list"]
        assert list_calls[0][1]["archived"] is False
        assert "cursor" not in list_calls[0][1]
        assert list_calls[1][1]["cursor"] == "active-page-2"

    def test_request_only_client_is_caller_owned_and_never_initialized(self) -> None:
        client = FakeRequestClient({
            "thread/list": [{"data": []}],
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        assert adapter.list_inventory(archived=False) == []

        assert [method for method, _, _ in client.calls] == ["thread/list"]

    def test_already_initialized_real_client_is_reused_safely(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        client = object.__new__(CodexAppServerClient)
        client._initialized = True
        calls: list[tuple[str, dict[str, Any], float]] = []

        def request(
            method: str, params: dict[str, Any], timeout: float
        ) -> dict[str, Any]:
            calls.append((method, params, timeout))
            return {"data": []}

        monkeypatch.setattr(client, "request", request)
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        assert adapter.list_inventory(archived=False) == []
        assert [method for method, _, _ in calls] == ["thread/list"]

    def test_initialize_failure_latches_and_requires_client_replacement(self) -> None:
        client = FakeRetryingInitializeClient({"thread/list": [{"data": []}]})
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        with pytest.raises(RuntimeError, match="replace") as first_failure:
            adapter.list_inventory(archived=False)
        assert isinstance(first_failure.value.__cause__, RuntimeError)
        with pytest.raises(RuntimeError, match="replace") as latched_failure:
            adapter.list_inventory(archived=False)

        assert latched_failure.value.__cause__ is None
        assert client.initialize_calls == [
            {"capabilities": {"experimentalApi": True}}
        ]
        assert client.calls == []

    def test_archived_pass_is_explicit_and_normalizes_inventory(self) -> None:
        pages = _fixture("thread-list-pages.json")
        client = FakeInitializingClient({"thread/list": pages["archived"]})
        summary = CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
            archived=True
        )[0]

        assert summary == CodexThreadSummary(
            native_id="thread-archived",
            title="Archived work",
            cwd="C:/work/archive",
            started_at=1783850000.0,
            last_active=1783850600.0,
            archived=True,
            revision="7",
        )
        assert client.calls[-1][1]["archived"] is True

    def test_null_preferred_aliases_fall_through_to_supported_alternates(self) -> None:
        client = FakeInitializingClient({
            "thread/list": [
                {
                    "data": None,
                    "threads": [
                        {
                            "id": None,
                            "threadId": "alias-fallback",
                            "title": None,
                            "name": "Alias fallback",
                            "createdAt": 1,
                            "updatedAt": 2,
                            "revision": None,
                            "version": 3,
                        }
                    ],
                    "nextCursor": None,
                    "next_cursor": "page-two",
                },
                {"threads": [], "next_cursor": None},
            ]
        })

        summary = CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
            archived=False
        )[0]

        assert summary.native_id == "alias-fallback"
        assert summary.title == "Alias fallback"
        assert summary.revision == "3"
        assert client.calls[-1][1]["cursor"] == "page-two"

    def test_naive_iso_timestamps_are_normalized_as_utc(self) -> None:
        client = FakeInitializingClient({
            "thread/list": [
                {
                    "data": [
                        {
                            "id": "naive-time",
                            "createdAt": "2026-07-12T10:00:00",
                            "updatedAt": "2026-07-12T10:01:00",
                        }
                    ]
                }
            ]
        })

        summary = CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
            archived=False
        )[0]

        assert (
            summary.started_at
            == datetime(2026, 7, 12, 10, 0, tzinfo=timezone.utc).timestamp()
        )

    def test_revision_fallback_is_stable_and_supported_fields_only(self) -> None:
        base = {
            "id": "one",
            "title": "Stable",
            "cwd": "C:/one",
            "createdAt": 100,
            "updatedAt": 200,
            "ephemeral": "first",
        }
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [base]},
                {"data": [{**base, "ephemeral": "changed"}]},
            ]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        first = adapter.list_inventory(archived=False)
        second = adapter.list_inventory(archived=False)

        assert len(first) == 1
        assert len(first[0].revision) == 64
        assert second == []

    def test_only_new_or_changed_threads_return_after_first_inventory(self) -> None:
        first = {
            "id": "one",
            "title": "One",
            "cwd": "C:/one",
            "createdAt": 100,
            "updatedAt": 200,
            "revision": "r1",
        }
        changed = {**first, "updatedAt": 201, "revision": "r2"}
        new = {**first, "id": "two", "revision": "r1"}
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [first]},
                {"data": [first]},
                {"data": [changed, new]},
            ]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        assert [row.native_id for row in adapter.list_inventory(archived=False)] == [
            "one"
        ]
        assert adapter.list_inventory(archived=False) == []
        assert [row.native_id for row in adapter.list_inventory(archived=False)] == [
            "one",
            "two",
        ]

    def test_full_inventory_bypasses_changed_cache_on_every_call(self) -> None:
        row = {
            "id": "one",
            "title": "One",
            "cwd": "C:/one",
            "createdAt": 100,
            "updatedAt": 200,
            "revision": "r1",
        }
        changed = {**row, "updatedAt": 201, "revision": "r2"}
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [row]},
                {"data": [changed]},
                {"data": [changed]},
                {"data": [changed]},
            ]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        assert [row.native_id for row in adapter.list_inventory(archived=False)] == [
            "one"
        ]
        assert [
            row.native_id for row in adapter.list_full_inventory(archived=False)
        ] == ["one"]
        assert [row.native_id for row in adapter.list_inventory(archived=False)] == [
            "one"
        ]
        assert [
            row.native_id for row in adapter.list_full_inventory(archived=False)
        ] == ["one"]

    def test_reused_explicit_revision_does_not_hide_supported_metadata_changes(
        self,
    ) -> None:
        first = {
            "id": "one",
            "title": "Before",
            "cwd": "C:/before",
            "createdAt": 100,
            "updatedAt": 200,
            "revision": "reused",
        }
        changed = {
            **first,
            "title": "After",
            "cwd": "C:/after",
            "createdAt": 90,
            "updatedAt": 210,
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [first]}, {"data": [changed]}]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        assert adapter.list_inventory(archived=False)[0].title == "Before"
        result = adapter.list_inventory(archived=False)

        assert len(result) == 1
        assert result[0].title == "After"
        assert result[0].cwd == "C:/after"
        assert (result[0].started_at, result[0].last_active) == (90.0, 210.0)

    def test_inverted_inventory_activity_is_normalized_deterministically(self) -> None:
        row = {
            "id": "inverted",
            "createdAt": 300,
            "updatedAt": 100,
            "revision": "r1",
        }
        client = FakeInitializingClient({"thread/list": [{"data": [row]}]})

        summary = CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
            archived=False
        )[0]

        assert (summary.started_at, summary.last_active) == (100.0, 300.0)

    def test_archive_move_is_changed_even_when_explicit_revision_is_unchanged(
        self,
    ) -> None:
        active = {
            "id": "moving",
            "createdAt": 100,
            "updatedAt": 200,
            "revision": "same-revision",
        }
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [active]},
                {"data": [{**active, "archived": True}]},
            ]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        assert adapter.list_inventory(archived=False)[0].archived is False
        moved = adapter.list_inventory(archived=True)

        assert len(moved) == 1
        assert moved[0].archived is True

    def test_paging_failure_does_not_commit_partial_revision_state(self) -> None:
        row = {
            "id": "one",
            "createdAt": 100,
            "updatedAt": 200,
            "revision": "r1",
        }
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [row], "nextCursor": "page-2"},
                RuntimeError("page failed"),
                {"data": [row]},
            ]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        with pytest.raises(RuntimeError, match="page failed"):
            adapter.list_inventory(archived=False)
        assert [row.native_id for row in adapter.list_inventory(archived=False)] == [
            "one"
        ]

    def test_invalid_entries_and_conflicting_duplicates_are_skipped(self) -> None:
        valid = {
            "id": "valid",
            "createdAt": "2026-07-12T10:00:00Z",
            "updatedAt": "2026-07-12T10:01:00Z",
        }
        conflict_a = {**valid, "id": "duplicate", "title": "A"}
        conflict_b = {**valid, "id": "duplicate", "title": "B"}
        client = FakeInitializingClient({
            "thread/list": [
                {
                    "data": [
                        valid,
                        {"id": "bad", "createdAt": True, "updatedAt": 2},
                        {"id": "nan", "createdAt": 1, "updatedAt": math.inf},
                        conflict_a,
                        conflict_b,
                        "not-an-object",
                    ]
                }
            ]
        })

        result = CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
            archived=False
        )

        assert [row.native_id for row in result] == ["valid"]

    @pytest.mark.parametrize(
        "response",
        [
            {},
            {"data": [{"id": "missing-timestamps"}]},
            {"threads": ["not-an-object"]},
        ],
    )
    def test_malformed_or_all_invalid_inventory_pages_raise(
        self, response: dict[str, Any]
    ) -> None:
        client = FakeInitializingClient({"thread/list": [response]})

        with pytest.raises(ValueError, match="thread/list"):
            CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
                archived=False
            )

    def test_repeated_cursor_fails_without_committing(self) -> None:
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [], "nextCursor": "repeat"},
                {"data": [], "nextCursor": "repeat"},
            ]
        })
        with pytest.raises(ValueError, match="repeated cursor"):
            CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
                archived=False
            )


class TestFindThread:
    def test_searches_active_then_archived_without_changed_filtering(self) -> None:
        row = {
            "id": "wanted",
            "createdAt": 1,
            "updatedAt": 2,
            "revision": "r1",
        }
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [row]},
                {"data": [row]},
                {"data": []},
                {"threads": [{**row, "archived": True}]},
            ]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)
        assert adapter.list_inventory(archived=False)[0].native_id == "wanted"

        assert adapter.find_native_thread("wanted") is not None
        archived = adapter.find_native_thread("wanted")

        assert archived is not None and archived.archived is True
        policies = [
            params["archived"]
            for method, params, _ in client.calls
            if method == "thread/list"
        ]
        assert policies == [False, False, False, True]

    def test_find_does_not_hide_thread_from_next_inventory(self) -> None:
        row = {"id": "wanted", "createdAt": 1, "updatedAt": 2}
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": [row]}]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        assert adapter.find_native_thread("wanted") is not None
        assert adapter.list_inventory(archived=False)[0].native_id == "wanted"


class TestProjection:
    def test_thread_read_projects_turn_items_and_diagnostic_path(self) -> None:
        client = FakeInitializingClient({"thread/read": [_fixture("thread-read.json")]})
        projection = CodexSourceAdapter(client, marker_secret=SECRET).project_thread(
            _summary()
        )

        assert client.calls[-1][0:2] == (
            "thread/read",
            {"threadId": "thread-active", "includeTurns": True},
        )
        assert projection.provider is Provider.CODEX
        assert projection.native_id == "thread-active"
        assert projection.native_path == "C:/diagnostics/thread-active.jsonl"
        assert projection.native_cursor == "revision-1"
        assert projection.native_status == "active"
        assert projection.title == "Active work"
        assert projection.cwd == "C:/work/active"
        assert projection.started_at == 1783850400.0
        assert projection.last_active == 1783850700.0
        assert projection.native_hash and len(projection.native_hash) == 64

        messages = list(projection.messages)
        assert [message.native_event_id for message in messages] == [
            "user-1",
            "command-1:0",
            "command-1:1",
            "agent-1",
        ]
        assert [message.ordinal for message in messages] == [0, 0, 1, 0]
        assert messages[1].reasoning == "Need to inspect\nUse a command"
        assert all("private" not in (message.content or "") for message in messages)

    def test_direct_thread_alias_and_fallback_item_identity_are_stable(self) -> None:
        item = {"type": "agentMessage", "text": "same"}
        direct = {
            "id": "thread-active",
            "rollout_path": "C:/diagnostics/alias.jsonl",
            "turns": [{"items": [item]}],
        }
        client = FakeInitializingClient({"thread/read": [direct, direct]})
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        first = adapter.project_thread(_summary())
        second = adapter.project_thread(_summary())

        assert first.native_path == "C:/diagnostics/alias.jsonl"
        assert first.messages[0].native_event_id == second.messages[0].native_event_id
        assert len(first.messages[0].native_event_id) == 64
        assert first.native_hash == second.native_hash

    def test_duplicate_fallback_item_hashes_are_stably_disambiguated(self) -> None:
        item = {"type": "agentMessage", "text": "same"}
        response = _read_with_items(item, item)
        client = FakeInitializingClient({"thread/read": [response, response]})
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        first = adapter.project_thread(_summary())
        second = adapter.project_thread(_summary())
        first_ids = [message.native_event_id for message in first.messages]
        second_ids = [message.native_event_id for message in second.messages]

        assert len(set(first_ids)) == 2
        assert first_ids == second_ids

    def test_unrelated_earlier_item_does_not_change_idless_event_identities(
        self,
    ) -> None:
        duplicate = {"type": "agentMessage", "text": "same"}
        unrelated = {"type": "agentMessage", "text": "unrelated"}
        before = _read_with_items(duplicate, duplicate)
        after = _read_with_items(unrelated, duplicate, duplicate)
        client = FakeInitializingClient({"thread/read": [before, after]})
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        before_projection = adapter.project_thread(_summary())
        after_projection = adapter.project_thread(_summary())
        before_ids = [message.native_event_id for message in before_projection.messages]
        after_ids = [
            message.native_event_id
            for message in after_projection.messages
            if message.content == "same"
        ]

        assert before_ids == after_ids

    def test_idless_timestamp_identity_survives_incremental_store_replay(
        self, tmp_path: Path
    ) -> None:
        first_response = _read_with_items({
            "type": "agentMessage",
            "text": "same",
            "createdAt": 200,
        })
        expanded_response = _read_with_items(
            {"type": "agentMessage", "text": "same", "createdAt": 100},
            {"type": "agentMessage", "text": "same", "createdAt": 200},
        )
        client = FakeInitializingClient({
            "thread/read": [first_response, expanded_response]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)
        first = adapter.project_thread(_summary())
        expanded = adapter.project_thread(_summary())

        original_id = first.messages[0].native_event_id
        expanded_by_timestamp = {
            message.timestamp: message.native_event_id for message in expanded.messages
        }
        assert expanded_by_timestamp[200.0] == original_id

        db = SessionDB(tmp_path / "state.db")
        try:
            store = SessionBridgeStore(db, clock=lambda: 500.0)
            store.upsert_projection(first)
            store.upsert_projection(expanded)

            persisted_timestamps = sorted(
                message["timestamp"]
                for message in db.get_messages("codex:thread-active")
            )
            assert persisted_timestamps == [100.0, 200.0]
        finally:
            db.close()

    def test_item_then_turn_then_summary_timestamp_fallbacks(self) -> None:
        response = {
            "thread": {
                "id": "thread-active",
                "turns": [
                    {
                        "createdAt": "2026-07-12T10:00:10Z",
                        "items": [
                            {
                                "type": "agentMessage",
                                "id": "item-time",
                                "createdAt": 123.0,
                                "text": "item",
                            },
                            {"type": "agentMessage", "id": "turn-time", "text": "turn"},
                        ],
                    },
                    {
                        "items": [
                            {
                                "type": "agentMessage",
                                "id": "fallback",
                                "text": "fallback",
                            }
                        ]
                    },
                ],
            }
        }
        client = FakeInitializingClient({"thread/read": [response]})
        messages = (
            CodexSourceAdapter(client, marker_secret=SECRET)
            .project_thread(_summary())
            .messages
        )

        assert [message.timestamp for message in messages] == [
            123.0,
            datetime(2026, 7, 12, 10, 0, 10, tzinfo=timezone.utc).timestamp(),
            1783850400.0,
        ]

    def test_projection_activity_reconciles_inverted_summary_and_message_times(
        self,
    ) -> None:
        response = _read_with_items(
            {
                "type": "agentMessage",
                "id": "early",
                "createdAt": 50,
                "text": "early",
            },
            {
                "type": "agentMessage",
                "id": "late",
                "createdAt": 350,
                "text": "late",
            },
        )
        client = FakeInitializingClient({"thread/read": [response]})
        summary = CodexThreadSummary(
            native_id="thread-active",
            title=None,
            cwd=None,
            started_at=300,
            last_active=100,
            archived=False,
            revision="r1",
        )

        projection = CodexSourceAdapter(client, marker_secret=SECRET).project_thread(
            summary
        )

        assert (projection.started_at, projection.last_active) == (50.0, 350.0)

    @pytest.mark.parametrize(
        "response",
        [
            {},
            {"thread": {}},
            {"thread": {"id": "thread-active"}},
            {"thread": {"id": "thread-active", "turns": {}}},
            {"thread": {"id": "different", "turns": []}},
            {"thread": {"id": "thread-active", "turns": [None]}},
            {"thread": {"id": "thread-active", "turns": [{"id": "turn"}]}},
            {
                "thread": {
                    "id": "thread-active",
                    "turns": [{"id": "turn", "items": {}}],
                }
            },
        ],
    )
    def test_malformed_thread_read_shapes_raise_for_retry(
        self, response: dict[str, Any]
    ) -> None:
        client = FakeInitializingClient({"thread/read": [response]})

        with pytest.raises(ValueError, match="thread/read"):
            CodexSourceAdapter(client, marker_secret=SECRET).project_thread(_summary())

    def test_explicit_empty_thread_is_a_valid_empty_projection(self) -> None:
        client = FakeInitializingClient({
            "thread/read": [{"thread": {"id": "thread-active", "turns": []}}]
        })

        projection = CodexSourceAdapter(client, marker_secret=SECRET).project_thread(
            _summary()
        )

        assert list(projection.messages) == []

    def test_malformed_item_is_skipped_without_losing_valid_neighbors(self) -> None:
        response = {
            "thread": {
                "id": "thread-active",
                "turns": [
                    {
                        "items": [
                            {"type": "agentMessage", "id": "before", "text": "before"},
                            None,
                            {"type": "agentMessage", "id": "after", "text": "after"},
                        ]
                    }
                ],
            }
        }
        client = FakeInitializingClient({"thread/read": [response]})
        projection = CodexSourceAdapter(client, marker_secret=SECRET).project_thread(
            _summary()
        )
        assert [message.content for message in projection.messages] == [
            "before",
            "after",
        ]

    def test_malformed_reasoning_does_not_poison_following_items(self) -> None:
        response = _read_with_items(
            {
                "type": "reasoning",
                "id": "malformed-reasoning",
                "summary": [{"not": "text"}],
            },
            {"type": "agentMessage", "id": "answer", "text": "still visible"},
        )
        client = FakeInitializingClient({"thread/read": [response]})

        projection = CodexSourceAdapter(client, marker_secret=SECRET).project_thread(
            _summary()
        )

        assert [message.content for message in projection.messages] == ["still visible"]
        assert projection.messages[0].reasoning is None

    def test_unhashable_fallback_item_is_skipped_without_aborting_read(self) -> None:
        response = _read_with_items(
            {"type": "unknown", "unstable": math.nan},
            {"type": "agentMessage", "id": "answer", "text": "still visible"},
        )
        client = FakeInitializingClient({"thread/read": [response]})

        projection = CodexSourceAdapter(client, marker_secret=SECRET).project_thread(
            _summary()
        )

        assert [message.content for message in projection.messages] == ["still visible"]

    def test_read_failure_propagates_and_retry_works(self) -> None:
        response = _read_with_items({
            "type": "agentMessage",
            "id": "answer",
            "text": "ok",
        })
        client = FakeInitializingClient({
            "thread/read": [RuntimeError("read failed"), response]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        with pytest.raises(RuntimeError, match="read failed"):
            adapter.project_thread(_summary())
        assert adapter.project_thread(_summary()).messages[0].content == "ok"

    def test_archived_summary_sets_archived_native_status(self) -> None:
        client = FakeInitializingClient({"thread/read": [_read_with_items()]})
        projection = CodexSourceAdapter(client, marker_secret=SECRET).project_thread(
            _summary(archived=True)
        )
        assert projection.native_status == "archived"

    def test_unknown_private_items_never_enter_searchable_projection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        secret = "password=hunter2"
        marker = _marker("private-marker")
        unknown = {
            "type": "internalPrompt",
            "internalPrompt": f"{secret} {marker} " + ("x" * 1_000_000),
        }
        response = _read_with_items(
            unknown,
            {
                "type": "agentMessage",
                "text": "safe answer",
                "internalPrompt": secret,
            },
        )
        original_canonical_json = codex_adapter_module._canonical_json

        def contains_secret(value: Any) -> bool:
            if isinstance(value, str):
                return secret in value
            if isinstance(value, dict):
                return any(
                    contains_secret(key) or contains_secret(item)
                    for key, item in value.items()
                )
            if isinstance(value, (list, tuple)):
                return any(contains_secret(item) for item in value)
            return False

        def bounded_canonical_json(value: Any) -> str:
            assert not contains_secret(value)
            return original_canonical_json(value)

        monkeypatch.setattr(
            codex_adapter_module, "_canonical_json", bounded_canonical_json
        )
        client = FakeInitializingClient({"thread/read": [response]})

        projection = CodexSourceAdapter(client, marker_secret=SECRET).project_thread(
            _summary()
        )

        assert [message.content for message in projection.messages] == ["safe answer"]
        assert all(
            secret not in (message.content or "") for message in projection.messages
        )
        assert projection.origin_kind is OriginKind.NATIVE


class TestBridgeMarkers:
    def test_projection_marker_payload_requires_exact_signed_payload(self) -> None:
        payload = BridgeMarkerPayload(
            bridge_id="bridge-exact",
            source_session_id="claude:source",
            target_provider=Provider.CODEX,
            policy_generation=1,
        )
        client = FakeInitializingClient({
            "thread/read": [
                _read_with_items({
                    "type": "userMessage",
                    "id": "marker",
                    "content": [
                        {
                            "type": "text",
                            "text": encode_bridge_marker(payload, SECRET),
                        }
                    ],
                })
            ]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)
        projection = adapter.project_thread(_summary())

        assert adapter.projection_has_marker_payload(projection, payload) is True
        assert adapter.projection_has_marker_payload(
            projection,
            BridgeMarkerPayload(
                bridge_id=payload.bridge_id,
                source_session_id="claude:different-source",
                target_provider=payload.target_provider,
                policy_generation=payload.policy_generation,
            ),
        ) is False

    def test_codex_marker_is_placeholder_until_later_human_user_text(self) -> None:
        marker = _marker("bridge-1")
        placeholder_client = FakeInitializingClient({
            "thread/read": [
                _read_with_items(
                    {
                        "type": "userMessage",
                        "id": "marker",
                        "content": [{"type": "text", "text": marker}],
                    },
                    {"type": "agentMessage", "id": "answer", "text": "ready"},
                )
            ]
        })
        placeholder = CodexSourceAdapter(
            placeholder_client, marker_secret=SECRET
        ).project_thread(_summary())
        assert placeholder.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
        assert placeholder.origin_bridge_id == "bridge-1"

        continuation_client = FakeInitializingClient({
            "thread/read": [
                _read_with_items(
                    {
                        "type": "userMessage",
                        "id": "marker",
                        "content": [{"type": "text", "text": marker}],
                    },
                    {"type": "mcpToolCall", "id": "tool", "result": {"text": "x"}},
                    {
                        "type": "userMessage",
                        "id": "human",
                        "content": [{"type": "text", "text": "continue"}],
                    },
                )
            ]
        })
        continuation = CodexSourceAdapter(
            continuation_client, marker_secret=SECRET
        ).project_thread(_summary())
        assert continuation.origin_kind is OriginKind.BRIDGE_CONTINUATION
        assert continuation.origin_bridge_id == "bridge-1"

    def test_wrong_target_invalid_and_title_only_markers_are_native(self) -> None:
        wrong = _marker("wrong", target=Provider.CLAUDE)
        invalid = _marker("invalid")[:-1] + "x"
        client = FakeInitializingClient({
            "thread/read": [
                _read_with_items({
                    "type": "userMessage",
                    "id": "user",
                    "content": [{"type": "text", "text": f"{wrong}\n{invalid}"}],
                })
            ]
        })
        titled = CodexThreadSummary(**{
            **_summary().__dict__,
            "title": _marker("title"),
        })
        projection = CodexSourceAdapter(client, marker_secret=SECRET).project_thread(
            titled
        )
        assert projection.origin_kind is OriginKind.NATIVE
        assert projection.origin_bridge_id is None

    def test_conflicting_valid_bridge_markers_raise(self) -> None:
        client = FakeInitializingClient({
            "thread/read": [
                _read_with_items(
                    {
                        "type": "userMessage",
                        "id": "one",
                        "content": [{"type": "text", "text": _marker("one")}],
                    },
                    {
                        "type": "userMessage",
                        "id": "two",
                        "content": [{"type": "text", "text": _marker("two")}],
                    },
                )
            ]
        })
        with pytest.raises(ValueError, match="conflicting bridge markers"):
            CodexSourceAdapter(client, marker_secret=SECRET).project_thread(_summary())


def test_adapter_never_enumerates_or_mutates_rollout_files(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("rollout filesystem access is forbidden")

    for name in ("rglob", "glob", "open", "write_text", "write_bytes"):
        monkeypatch.setattr(Path, name, forbidden)

    client = FakeInitializingClient({
        "thread/list": [{"data": []}],
        "thread/read": [
            _read_with_items({"type": "agentMessage", "id": "answer", "text": "ok"})
        ],
    })
    adapter = CodexSourceAdapter(client, marker_secret=SECRET)
    assert adapter.list_inventory(archived=False) == []
    assert adapter.project_thread(_summary()).messages[0].content == "ok"
