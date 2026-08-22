from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
from threading import Event, Thread
from typing import Any

import pytest

from agent.transports.codex_app_server import (
    CodexAppServerClient,
    CodexRequestCancelled,
)
from hermes_state import SessionDB
import session_bridge.codex_adapter as codex_adapter_module
from session_bridge.codex_adapter import (
    CodexSourceAdapter,
    CodexThreadSummary,
    _VisibilityInventoryCancelled,
)
from session_bridge.models import (
    BridgeMarkerPayload,
    OriginKind,
    ProjectedMessage,
    Provider,
    SessionProjection,
    encode_bridge_marker,
)
from session_bridge.store import SessionBridgeStore, SidebarSource


FIXTURES = Path(__file__).parent / "fixtures" / "codex"
SECRET = b"codex-adapter-test-secret"


class FakeRequestClient:
    def __init__(self, responses: dict[str, list[dict[str, Any] | Exception]]) -> None:
        self.responses = {key: list(values) for key, values in responses.items()}
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def request(
        self,
        method: str,
        params: dict[str, Any],
        timeout: float,
        *,
        cancel_event: Event | None = None,
    ) -> dict[str, Any]:
        self.calls.append((method, deepcopy(params), timeout))
        response = self.responses[method].pop(0)
        if isinstance(response, Exception):
            raise response
        return deepcopy(response)


class FakeStderrClient(FakeRequestClient):
    def __init__(self, stderr_lines: list[str]) -> None:
        super().__init__({})
        self.stderr_lines = stderr_lines
        self.stderr_tail_calls: list[int] = []

    def stderr_tail(self, n: int = 20) -> list[str]:
        self.stderr_tail_calls.append(n)
        return list(self.stderr_lines)


class FakeInitializingClient(FakeRequestClient):
    def __init__(self, responses: dict[str, list[dict[str, Any] | Exception]]) -> None:
        super().__init__(responses)
        self.initialize_calls: list[dict[str, Any]] = []

    def initialize(
        self, *, cancel_event: Event | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        self.initialize_calls.append(deepcopy(kwargs))
        return {"userAgent": "synthetic"}


class FakeRetryingInitializeClient(FakeInitializingClient):
    def initialize(self, **kwargs: Any) -> dict[str, Any]:
        self.initialize_calls.append(deepcopy(kwargs))
        if len(self.initialize_calls) == 1:
            raise RuntimeError("synthetic initialization failure")
        return {"userAgent": "synthetic"}


class _ClockAdvancingClient(FakeInitializingClient):
    def __init__(
        self,
        responses: dict[str, list[dict[str, Any] | Exception]],
        clock: dict[str, float],
        costs: dict[str, float],
    ) -> None:
        super().__init__(responses)
        self._clock = clock
        self._costs = costs

    def request(
        self, method: str, params: dict[str, Any], timeout: float
    ) -> dict[str, Any]:
        self._clock["now"] += self._costs.get(method, 0.0)
        return super().request(method, params, timeout)


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


def test_source_adapter_forwards_bounded_stderr_tail() -> None:
    client = FakeStderrClient(["first", "second"])
    adapter = CodexSourceAdapter(client, marker_secret=SECRET)

    assert adapter.stderr_tail(12) == ["first", "second"]
    assert client.stderr_tail_calls == [12]


def test_source_adapter_treats_missing_stderr_accessor_as_empty() -> None:
    adapter = CodexSourceAdapter(FakeRequestClient({}), marker_secret=SECRET)

    assert adapter.stderr_tail(12) == []


def test_source_adapter_stderr_tail_bounds_consumption_when_client_ignores_n() -> None:
    consumed: list[int] = []

    class UnboundedStderrClient(FakeRequestClient):
        def __init__(self) -> None:
            super().__init__({})

        def stderr_tail(self, n: int = 20):
            del n
            for index in range(100):
                consumed.append(index)
                yield f"line-{index}"

    adapter = CodexSourceAdapter(UnboundedStderrClient(), marker_secret=SECRET)

    assert adapter.stderr_tail(3) == ["line-0", "line-1", "line-2"]
    assert consumed == [0, 1, 2]


def test_source_adapter_stderr_tail_non_positive_limit_consumes_nothing() -> None:
    consumed: list[int] = []

    class UnboundedStderrClient(FakeRequestClient):
        def __init__(self) -> None:
            super().__init__({})

        def stderr_tail(self, n: int = 20):
            del n
            consumed.append(1)
            yield "line"

    adapter = CodexSourceAdapter(UnboundedStderrClient(), marker_secret=SECRET)

    assert adapter.stderr_tail(0) == []
    assert consumed == []


class TestInventory:
    def test_recent_inventory_uses_state_db_and_stops_at_watermark(self) -> None:
        client = FakeInitializingClient({
            "thread/list": [
                {
                    "data": [
                        {
                            "id": "recent",
                            "createdAt": 290,
                            "updatedAt": 300,
                        }
                    ],
                    "nextCursor": "page-2",
                },
                {
                    "data": [
                        {
                            "id": "older",
                            "createdAt": 190,
                            "updatedAt": 200,
                        }
                    ],
                    "nextCursor": "page-3",
                },
            ]
        })

        summaries = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
        ).list_recent_inventory(archived=False, after=250)

        assert [summary.native_id for summary in summaries] == ["recent"]
        assert len(client.calls) == 2
        for method, params, timeout in client.calls:
            assert method == "thread/list"
            assert params["useStateDbOnly"] is True
            assert params["limit"] == 100
            assert params["sortKey"] == "updated_at"
            assert params["sortDirection"] == "desc"
            assert timeout == 30.0

    def test_recent_inventory_stops_on_a_full_page_of_known_tasks(self) -> None:
        client = FakeInitializingClient({
            "thread/list": [
                {
                    "data": [
                        {
                            "id": "known",
                            "createdAt": 290,
                            "updatedAt": 300,
                        }
                    ],
                    "nextCursor": "must-not-be-read",
                }
            ]
        })

        summaries = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
        ).list_recent_inventory(
            archived=False,
            after=0,
            known_native_ids=frozenset({"known"}),
        )

        assert [summary.native_id for summary in summaries] == ["known"]
        assert len(client.calls) == 1

    def test_projection_reuses_trusted_origin_snapshot_from_inventory(self) -> None:
        native_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        calls = 0

        def trusted_origins() -> dict[str, str]:
            nonlocal calls
            calls += 1
            return {}

        client = FakeInitializingClient({
            "thread/list": [
                {"data": [{"id": native_id, "createdAt": 1, "updatedAt": 2}]}
            ],
            "thread/read": [
                {
                    "thread": {
                        "id": native_id,
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "agentMessage",
                                        "id": "answer",
                                        "text": "ok",
                                    }
                                ]
                            }
                        ],
                    }
                }
            ],
        })
        adapter = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            trusted_origins=trusted_origins,
        )

        [summary] = adapter.list_inventory(archived=False)
        inventory_calls = calls
        projection = adapter.project_thread(summary)

        assert projection.origin_kind is OriginKind.NATIVE
        assert inventory_calls == 2
        assert calls == inventory_calls

    def test_inventory_refreshes_provenance_after_provider_race(self) -> None:
        native_id = "019f8621-4d36-7fe0-9419-319ee7ec09dd"
        bridge_id = (
            "characterization-0e831788-1bc1-4324-a58f-0343bcde25b7-codex"
        )
        snapshots = iter(({}, {native_id: bridge_id}))
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [{"id": native_id, "createdAt": 1, "updatedAt": 2}]}
            ]
        })
        adapter = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            trusted_origins=lambda: next(snapshots),
        )

        [summary] = adapter.list_inventory(archived=False)

        assert summary.trusted_origins_checked is True
        assert summary.trusted_origin_bridge_id == bridge_id

    def test_inventory_reports_trusted_origin_change_for_same_native_summary(
        self,
    ) -> None:
        native_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
        row = {"id": native_id, "createdAt": 1, "updatedAt": 2}
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": [row]}]
        })
        origins: dict[str, str] = {}
        adapter = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            trusted_origins=lambda: origins,
        )

        [before] = adapter.list_inventory(archived=False)
        origins[native_id] = "characterization-trusted-codex"
        [after] = adapter.list_inventory(archived=False)

        assert before.trusted_origin_bridge_id is None
        assert after.trusted_origin_bridge_id == "characterization-trusted-codex"

    def test_claude_visibility_refreshes_combined_inventory_before_projection(
        self,
    ) -> None:
        native_id = "019f8621-4d36-7fe0-9419-319ee7ec09dd"
        bridge_id = (
            "characterization-0e831788-1bc1-4324-a58f-0343bcde25b7-codex"
        )
        snapshots = iter(({}, {}, {}, {}, {native_id: bridge_id}))
        row = {
            "id": native_id,
            "createdAt": 1,
            "updatedAt": 2,
            "source": "vscode",
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [
                {
                    "thread": {
                        **row,
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "request",
                                        "content": [
                                            {"type": "text", "text": "native text"}
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                }
            ],
        })
        adapter = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            trusted_origins=lambda: next(snapshots),
        )

        [source] = adapter.list_claude_visibility_sources(after=0)

        assert source.projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
        assert source.projection.origin_bridge_id == bridge_id

    def test_inventory_accepts_equal_values_for_every_supported_cwd_alias(self) -> None:
        row = {
            "id": "equal-cwd-aliases",
            "cwd": "C:/work/equal",
            "workingDirectory": "C:/work/equal",
            "working_directory": "C:/work/equal",
            "createdAt": 1,
            "updatedAt": 2,
        }
        client = FakeInitializingClient({"thread/list": [{"data": [row]}]})

        [summary] = CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
            archived=False
        )

        assert summary.cwd == "C:/work/equal"

    def test_inventory_rejects_conflicting_cwd_and_working_directory(self) -> None:
        row = {
            "id": "conflicting-cwd-aliases",
            "cwd": "C:/work/first",
            "workingDirectory": "C:/work/second",
            "createdAt": 1,
            "updatedAt": 2,
        }
        client = FakeInitializingClient({"thread/list": [{"data": [row]}]})

        with pytest.raises(ValueError, match="thread/list"):
            CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
                archived=False
            )

    @pytest.mark.parametrize("malformed", [None, 7, "", "relative/path", "C:/bad\0cwd"])
    def test_inventory_rejects_malformed_later_cwd_alias(self, malformed: Any) -> None:
        row = {
            "id": "malformed-cwd-alias",
            "cwd": "C:/work/valid",
            "workingDirectory": malformed,
            "createdAt": 1,
            "updatedAt": 2,
        }
        client = FakeInitializingClient({"thread/list": [{"data": [row]}]})

        with pytest.raises(ValueError, match="thread/list"):
            CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
                archived=False
            )

    @pytest.mark.parametrize("alias", ["workingDirectory", "working_directory"])
    def test_inventory_accepts_each_alternate_cwd_alias(self, alias: str) -> None:
        row = {
            "id": "alternate-cwd",
            alias: "C:/work/alternate",
            "createdAt": 1,
            "updatedAt": 2,
        }
        client = FakeInitializingClient({"thread/list": [{"data": [row]}]})

        [summary] = CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
            archived=False
        )

        assert summary.cwd == "C:/work/alternate"

    @pytest.mark.skipif(os.name != "nt", reason="Windows path-equivalence policy")
    def test_inventory_cwd_aliases_follow_windows_normalization_and_case_rules(
        self,
    ) -> None:
        row = {
            "id": "windows-cwd-aliases",
            "cwd": "C:/Work/Repo/.",
            "workingDirectory": r"c:\work\repo",
            "createdAt": 1,
            "updatedAt": 2,
        }
        client = FakeInitializingClient({"thread/list": [{"data": [row]}]})

        [summary] = CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
            archived=False
        )

        assert summary.cwd == "C:/Work/Repo/."

    @pytest.mark.skipif(os.name != "nt", reason="Windows path-equivalence policy")
    def test_thread_read_equivalent_cwd_alias_becomes_reconciled_canonical_value(
        self,
    ) -> None:
        row = {
            "id": "windows-read-cwd",
            "cwd": "C:/Work/Repo/.",
            "createdAt": 1,
            "updatedAt": 2,
            "source": "vscode",
        }
        read_cwd = r"c:\work\repo"
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [
                {
                    "thread": {
                        "id": "windows-read-cwd",
                        "workingDirectory": read_cwd,
                        "source": "vscode",
                        "turns": [],
                    }
                }
            ],
        })

        [candidate] = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=0)

        assert candidate.projection.cwd == read_cwd

    def test_claude_visibility_inventory_is_complete_paginated_and_preserves_metadata(
        self,
    ) -> None:
        def entry(native_id, updated, *, archived=False):
            return {
                "id": native_id,
                "title": "ignored title",
                "cwd": f"C:/work/{native_id}",
                "createdAt": updated - 10,
                "updatedAt": updated,
                "archived": archived,
                "gitRoot": "C:/work",
                "gitBranch": f"feature/{native_id}",
                "gitHead": f"head-{native_id}",
                "worktreeId": f"wt-{native_id}",
                "source": "vscode",
            }

        client = FakeInitializingClient({
            "thread/list": [
                {"data": [entry("linked-or-uncataloged", 300)], "nextCursor": "p2"},
                {"data": [entry("older", 100)]},
                {"data": [entry("archived", 200, archived=True)]},
            ],
            "thread/read": [
                {
                    "thread": {
                        "id": "linked-or-uncataloged",
                        "source": "vscode",
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "u1",
                                        "content": [
                                            {"type": "text", "text": "Build API"}
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                },
                {
                    "thread": {
                        "id": "archived",
                        "source": "vscode",
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "u3",
                                        "content": [
                                            {"type": "text", "text": "Archived request"}
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                },
                {
                    "thread": {
                        "id": "older",
                        "source": "vscode",
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "u2",
                                        "content": [
                                            {"type": "text", "text": "Older request"}
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                },
            ],
        })

        sources = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=50)

        assert [source.source_session_id for source in sources] == [
            "codex:linked-or-uncataloged",
            "codex:archived",
            "codex:older",
        ]
        newest = sources[0]
        assert newest.projection.messages[0].content == "Build API"
        assert newest.git_root == "C:/work"
        assert newest.projection.git_branch == "feature/linked-or-uncataloged"
        assert newest.git_head == "head-linked-or-uncataloged"
        assert newest.worktree_id == "wt-linked-or-uncataloged"
        assert sources[1].projection.native_status == "archived"
        assert [call[0] for call in client.calls] == [
            "thread/list",
            "thread/list",
            "thread/list",
            "thread/read",
            "thread/read",
            "thread/read",
        ]

    def test_continuous_visibility_uses_fast_recency_inventory_and_stops_at_cutoff(
        self,
    ) -> None:
        def entry(native_id: str, updated: int):
            return {
                "id": native_id,
                "cwd": f"C:/work/{native_id}",
                "createdAt": updated - 10,
                "updatedAt": updated,
                "source": "vscode",
            }

        client = FakeInitializingClient({
            "thread/list": [
                {
                    "data": [entry("new", 300), entry("before-cutoff", 200)],
                    "nextCursor": "must-not-be-read",
                },
                {"data": [entry("older-page", 100)]},
                {"data": []},
            ],
            "thread/read": [
                {
                    "thread": {
                        **entry("new", 300),
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "request",
                                        "content": [
                                            {"type": "text", "text": "New request"}
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                }
            ],
        })

        sources = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=250, state_db_only=True)

        assert [source.projection.native_id for source in sources] == ["new"]
        assert [call[0] for call in client.calls] == [
            "thread/list",
            "thread/list",
            "thread/read",
        ]
        for method, params, _timeout in client.calls[:2]:
            assert method == "thread/list"
            assert params["limit"] == 100
            assert params["sortKey"] == "updated_at"
            assert params["sortDirection"] == "desc"
            assert params["useStateDbOnly"] is True
            assert "cursor" not in params

    def test_precancelled_visibility_inventory_does_not_initialize_client(
        self,
    ) -> None:
        client = FakeInitializingClient({})
        stop = Event()
        stop.set()

        with pytest.raises(_VisibilityInventoryCancelled):
            CodexSourceAdapter(client, marker_secret=SECRET).list_claude_visibility_sources(
                after=250, state_db_only=True, stop=stop
            )

        assert client.initialize_calls == []
        assert client.calls == []

    def test_continuous_visibility_cancels_active_initialization(self) -> None:
        stop = Event()
        entered = Event()

        class BlockingInitializeClient(FakeRequestClient):
            def __init__(self) -> None:
                super().__init__({})
                self.received_stop: Event | None = None

            def initialize(
                self,
                *,
                cancel_event: Event | None = None,
                **_kwargs: Any,
            ) -> dict[str, Any]:
                self.received_stop = cancel_event
                entered.set()
                assert cancel_event is not None
                assert cancel_event.wait(1.0)
                raise CodexRequestCancelled()

        client = BlockingInitializeClient()
        result: list[BaseException] = []

        def inventory() -> None:
            try:
                CodexSourceAdapter(
                    client, marker_secret=SECRET
                ).list_claude_visibility_sources(
                    after=250, state_db_only=True, stop=stop
                )
            except BaseException as exc:
                result.append(exc)

        thread = Thread(target=inventory)
        thread.start()
        assert entered.wait(1.0)
        stop.set()
        thread.join(1.0)

        assert thread.is_alive() is False
        assert client.received_stop is stop
        assert len(result) == 1
        assert isinstance(result[0], _VisibilityInventoryCancelled)
        assert client.calls == []

    def test_continuous_visibility_cancels_between_inventory_pages(self) -> None:
        stop = Event()

        class CancellingClient(FakeInitializingClient):
            def request(
                self,
                method: str,
                params: dict[str, Any],
                timeout: float,
                *,
                cancel_event: Event | None = None,
            ) -> dict[str, Any]:
                response = super().request(method, params, timeout)
                stop.set()
                return response

        client = CancellingClient({
            "thread/list": [
                {"data": [], "nextCursor": "must-not-be-read"},
                {"data": []},
            ]
        })

        with pytest.raises(_VisibilityInventoryCancelled):
            CodexSourceAdapter(client, marker_secret=SECRET).list_claude_visibility_sources(
                after=250, state_db_only=True, stop=stop
            )

        assert [call[0] for call in client.calls] == ["thread/list"]

    def test_continuous_visibility_cancels_active_thread_read(self) -> None:
        stop = Event()
        entered = Event()

        class BlockingReadClient(FakeInitializingClient):
            def request(
                self,
                method: str,
                params: dict[str, Any],
                timeout: float,
                *,
                cancel_event: Event | None = None,
            ) -> dict[str, Any]:
                if method == "thread/read":
                    self.calls.append((method, params, timeout))
                    entered.set()
                    assert stop.wait(1.0)
                    raise CodexRequestCancelled()
                return super().request(method, params, timeout)

        row = {
            "id": "active-read",
            "cwd": "C:/work/active-read",
            "createdAt": 290,
            "updatedAt": 300,
            "source": "vscode",
        }
        client = BlockingReadClient({
            "thread/list": [{"data": [row]}, {"data": []}],
        })
        result: list[BaseException] = []

        def inventory() -> None:
            try:
                CodexSourceAdapter(
                    client, marker_secret=SECRET
                ).list_claude_visibility_sources(
                    after=250, state_db_only=True, stop=stop
                )
            except BaseException as exc:
                result.append(exc)

        thread = Thread(target=inventory)
        thread.start()
        assert entered.wait(1.0)
        stop.set()
        thread.join(1.0)

        assert thread.is_alive() is False
        assert len(result) == 1
        assert isinstance(result[0], _VisibilityInventoryCancelled)
        assert [call[0] for call in client.calls] == [
            "thread/list",
            "thread/list",
            "thread/read",
        ]

    def test_continuous_visibility_uses_preview_when_full_read_times_out(
        self,
    ) -> None:
        row = {
            "id": "oversized-thread",
            "name": "Long-running bridge rollout",
            "preview": "Finish the cross-harness session bridge rollout",
            "path": "C:/codex/sessions/oversized-thread.jsonl",
            "cwd": "C:/work/session-bridge",
            "createdAt": 100,
            "updatedAt": 300,
            "source": "vscode",
            "gitInfo": {
                "branch": "codex/session-bridge-ship",
                "sha": "abc123",
            },
        }
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [row]},
                {"data": []},
            ],
            "thread/read": [TimeoutError("synthetic oversized thread")],
        })

        [source] = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=250, state_db_only=True)

        assert source.source_session_id == "codex:oversized-thread"
        assert source.projection.title == "Long-running bridge rollout"
        assert source.projection.native_path == row["path"]
        assert source.projection.git_branch == "codex/session-bridge-ship"
        assert [(message.role, message.content) for message in source.projection.messages] == [
            ("user", "Finish the cross-harness session bridge rollout")
        ]
        assert source.git_head == "abc123"
        assert source.automation_only is False
        assert source.subagent_only is False

    def test_visibility_reuses_indexed_projection_and_only_hydrates_unknown_native(
        self,
    ) -> None:
        def entry(
            native_id: str,
            updated: int,
            *,
            source: object = "vscode",
        ) -> dict[str, object]:
            return {
                "id": native_id,
                "name": native_id,
                "preview": f"preview for {native_id}",
                "path": f"C:/codex/sessions/{native_id}.jsonl",
                "cwd": f"C:/work/{native_id}",
                "createdAt": updated - 10,
                "updatedAt": updated,
                "source": source,
            }

        indexed_projection = SessionProjection(
            provider=Provider.CODEX,
            native_id="indexed",
            title="Indexed title",
            cwd="C:/work/indexed",
            started_at=100.0,
            last_active=400.0,
            messages=(
                ProjectedMessage(
                    native_event_id="indexed-user",
                    ordinal=0,
                    role="user",
                    content="Indexed full request",
                    timestamp=110.0,
                ),
            ),
            native_path="C:/codex/sessions/indexed.jsonl",
            native_status="active",
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id="bridge:indexed",
        )
        indexed = SidebarSource(
            source_session_id="codex:indexed",
            projection=indexed_projection,
            git_root="C:/indexed-root",
            git_head="indexed-head",
            worktree_id="indexed-worktree",
            automation_only=False,
            subagent_only=False,
        )
        client = FakeInitializingClient({
            "thread/list": [
                {
                    "data": [
                        entry("indexed", 400),
                        entry(
                            "unindexed-subagent",
                            350,
                            source={"subAgent": "review"},
                        ),
                        entry("unindexed-native", 300),
                    ]
                },
                {"data": []},
            ],
            "thread/read": [
                {
                    "thread": {
                        **entry("unindexed-native", 300),
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "native-user",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "Hydrate only this request",
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                }
            ],
        })

        sources = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(
            after=250,
            state_db_only=True,
            indexed_sources={"indexed": indexed},
            known_visibility_source_ids=frozenset(),
        )

        by_id = {source.projection.native_id: source for source in sources}
        assert set(by_id) == {"indexed", "unindexed-subagent", "unindexed-native"}
        assert by_id["indexed"].projection.messages == indexed_projection.messages
        assert by_id["indexed"].projection.last_active == 400.0
        assert (
            by_id["indexed"].projection.origin_kind
            is OriginKind.BRIDGE_PLACEHOLDER
        )
        assert by_id["indexed"].git_root == "C:/indexed-root"
        assert by_id["unindexed-subagent"].subagent_only is True
        assert by_id["unindexed-native"].projection.messages[0].content == (
            "Hydrate only this request"
        )
        assert [call[0] for call in client.calls] == [
            "thread/list",
            "thread/list",
            "thread/read",
        ]

    def test_visibility_exact_reads_new_or_stale_indexed_native_sources(self) -> None:
        def entry(native_id: str, updated: int) -> dict[str, object]:
            return {
                "id": native_id,
                "name": native_id,
                "preview": f"preview for {native_id}",
                "path": f"C:/codex/sessions/{native_id}.jsonl",
                "cwd": f"C:/work/{native_id}",
                "createdAt": updated - 10,
                "updatedAt": updated,
                "source": "vscode",
            }

        def indexed_source(
            native_id: str,
            last_active: float,
            *,
            parser_version: int = 1,
        ) -> SidebarSource:
            return SidebarSource(
                source_session_id=f"codex:{native_id}",
                projection=SessionProjection(
                    provider=Provider.CODEX,
                    native_id=native_id,
                    title=f"Indexed {native_id}",
                    cwd=f"C:/work/{native_id}",
                    started_at=100.0,
                    last_active=last_active,
                    messages=(
                        ProjectedMessage(
                            native_event_id=f"{native_id}-cached-user",
                            ordinal=0,
                            role="user",
                            content=f"Cached request for {native_id}",
                            timestamp=110.0,
                        ),
                    ),
                    native_path=f"C:/codex/sessions/{native_id}.jsonl",
                    native_status="active",
                    parser_version=parser_version,
                    origin_kind=OriginKind.NATIVE,
                ),
                git_root=None,
                git_head=None,
                worktree_id=None,
                automation_only=False,
                subagent_only=False,
            )

        known = indexed_source("known", 400.0)
        new = indexed_source("new", 350.0)
        stale = indexed_source("stale", 200.0)
        old_parser = indexed_source("old-parser", 275.0, parser_version=0)
        client = FakeInitializingClient({
            "thread/list": [
                {
                    "data": [
                        entry("known", 400),
                        entry("new", 350),
                        entry("stale", 300),
                        entry("old-parser", 275),
                    ]
                },
                {"data": []},
            ],
            "thread/read": [
                {
                    "thread": {
                        **entry("new", 350),
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "new-live-user",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "Freshly confirm the new source",
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                },
                {
                    "thread": {
                        **entry("stale", 300),
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "stale-live-user",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "Refresh the stale source",
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                },
                {
                    "thread": {
                        **entry("old-parser", 275),
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "old-parser-live-user",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": "Refresh the old parser source",
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                },
            ],
        })

        sources = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(
            after=250,
            state_db_only=True,
            indexed_sources={
                "known": known,
                "new": new,
                "stale": stale,
                "old-parser": old_parser,
            },
            known_visibility_source_ids=frozenset({"codex:known"}),
        )

        by_id = {source.projection.native_id: source for source in sources}
        assert by_id["known"].projection.messages[0].content == (
            "Cached request for known"
        )
        assert by_id["new"].projection.messages[0].content == (
            "Freshly confirm the new source"
        )
        assert by_id["stale"].projection.messages[0].content == (
            "Refresh the stale source"
        )
        assert by_id["old-parser"].projection.messages[0].content == (
            "Refresh the old parser source"
        )
        assert [call[0] for call in client.calls] == [
            "thread/list",
            "thread/list",
            "thread/read",
            "thread/read",
            "thread/read",
        ]

    def test_manual_visibility_uses_preview_when_full_read_times_out(self) -> None:
        row = {
            "id": "oversized-manual-thread",
            "name": "Long-running manual rollout",
            "preview": "Finish the reviewed native visibility rollout",
            "path": "C:/codex/sessions/oversized-manual-thread.jsonl",
            "cwd": "C:/work/session-bridge",
            "createdAt": 100,
            "updatedAt": 300,
            "source": "vscode",
            "gitInfo": {
                "branch": "codex/session-bridge-ship",
                "sha": "def456",
            },
        }
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [row]},
                {"data": []},
            ],
            "thread/read": [TimeoutError("synthetic oversized thread")],
        })

        [source] = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=250)

        assert source.source_session_id == "codex:oversized-manual-thread"
        assert source.projection.native_path == row["path"]
        assert [(message.role, message.content) for message in source.projection.messages] == [
            ("user", "Finish the reviewed native visibility rollout")
        ]
        assert source.git_head == "def456"

    def test_visibility_inventory_initialization_uses_remaining_deadline(self) -> None:
        clock = {"now": 100.0}

        class InitializingClient(FakeInitializingClient):
            def initialize(self, **kwargs: Any) -> dict[str, Any]:
                self.initialize_calls.append(deepcopy(kwargs))
                clock["now"] += 2.0
                self._initialized = True
                return {"userAgent": "synthetic"}

        client = InitializingClient({
            "thread/list": [{"data": []}, {"data": []}],
        })

        assert CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            monotonic=lambda: clock["now"],
        ).list_claude_visibility_sources(
            after=0,
            discovery_timeout=10.0,
        ) == ()

        assert client.initialize_calls == [
            {
                "capabilities": {"experimentalApi": True},
                "timeout": 10.0,
            }
        ]
        assert [timeout for _method, _params, timeout in client.calls] == [
            8.0,
            8.0,
        ]

    def test_visibility_inventory_uses_one_deadline_for_lists_and_reads(self) -> None:
        clock = {"now": 100.0}

        def entry(native_id: str, updated: int) -> dict[str, object]:
            return {
                "id": native_id,
                "cwd": f"C:/work/{native_id}",
                "createdAt": updated - 10,
                "updatedAt": updated,
                "source": "vscode",
            }

        rows = [entry("first", 300), entry("second", 290)]
        client = _ClockAdvancingClient(
            {
                "thread/list": [{"data": rows}, {"data": []}],
                "thread/read": [
                    {"thread": {**rows[0], "turns": []}},
                    {"thread": {**rows[1], "turns": []}},
                ],
            },
            clock,
            {"thread/list": 2.0, "thread/read": 3.0},
        )

        sources = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            monotonic=lambda: clock["now"],
        ).list_claude_visibility_sources(
            after=250,
            state_db_only=True,
            discovery_timeout=15.0,
        )

        assert [source.projection.native_id for source in sources] == [
            "first",
            "second",
        ]
        assert [(method, timeout) for method, _params, timeout in client.calls] == [
            ("thread/list", 15.0),
            ("thread/list", 13.0),
            ("thread/read", 11.0),
            ("thread/read", 8.0),
        ]

    def test_visibility_inventory_rejects_indexed_partial_result_at_deadline(
        self,
    ) -> None:
        clock = {"now": 100.0}
        row = {
            "id": "late",
            "cwd": "C:/work/late",
            "createdAt": 290,
            "updatedAt": 300,
            "source": "vscode",
        }
        client = _ClockAdvancingClient(
            {
                "thread/list": [{"data": [row]}, {"data": []}],
                "thread/read": [{"thread": {**row, "turns": []}}],
            },
            clock,
            {"thread/read": 6.0},
        )

        with pytest.raises(RuntimeError, match="Codex sidebar deadline exhausted"):
            CodexSourceAdapter(
                client,
                marker_secret=SECRET,
                monotonic=lambda: clock["now"],
            ).list_claude_visibility_sources(
                after=250,
                state_db_only=True,
                indexed_sources={},
                discovery_timeout=5.0,
            )

        assert [method for method, _params, _timeout in client.calls] == [
            "thread/list",
            "thread/list",
            "thread/read",
        ]

    def test_visibility_inventory_previews_unindexed_remainder_after_deadline(
        self,
    ) -> None:
        clock = {"now": 100.0}
        rows = [
            {
                "id": native_id,
                "name": native_id,
                "preview": f"Preview {native_id}",
                "cwd": f"C:/work/{native_id}",
                "createdAt": 290 - index,
                "updatedAt": 300 - index,
                "source": "vscode",
            }
            for index, native_id in enumerate(("first", "second", "third"))
        ]
        client = _ClockAdvancingClient(
            {
                "thread/list": [{"data": rows}, {"data": []}],
                "thread/read": [
                    {"thread": {**rows[0], "turns": []}},
                    {"thread": {**rows[1], "turns": []}},
                ],
            },
            clock,
            {"thread/read": 3.0},
        )

        sources = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            monotonic=lambda: clock["now"],
        ).list_claude_visibility_sources(
            after=250,
            state_db_only=True,
            discovery_timeout=5.0,
        )

        assert [source.projection.native_id for source in sources] == [
            "first",
            "second",
            "third",
        ]
        assert [method for method, _params, _timeout in client.calls].count(
            "thread/read"
        ) == 2
        assert sources[1].projection.messages[0].content == "Preview second"
        assert sources[2].projection.messages[0].content == "Preview third"

    def test_visibility_inventory_stops_hydrating_after_raw_timeout_uses_budget(
        self,
    ) -> None:
        clock = {"now": 100.0}
        rows = [
            {
                "id": native_id,
                "name": native_id,
                "preview": f"Preview {native_id}",
                "cwd": f"C:/work/{native_id}",
                "createdAt": 290 - index,
                "updatedAt": 300 - index,
                "source": "vscode",
            }
            for index, native_id in enumerate(("first", "second"))
        ]

        class TimingOutClient(_ClockAdvancingClient):
            def request(
                self, method: str, params: dict[str, Any], timeout: float
            ) -> dict[str, Any]:
                if method == "thread/read":
                    self.calls.append((method, params, timeout))
                    clock["now"] += timeout
                    raise TimeoutError("fixed transport timeout")
                return super().request(method, params, timeout)

        client = TimingOutClient(
            {"thread/list": [{"data": rows}, {"data": []}]},
            clock,
            {},
        )

        sources = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            monotonic=lambda: clock["now"],
        ).list_claude_visibility_sources(
            after=250,
            state_db_only=True,
            discovery_timeout=5.0,
        )

        assert [source.projection.native_id for source in sources] == [
            "first",
            "second",
        ]
        assert [method for method, _params, _timeout in client.calls].count(
            "thread/read"
        ) == 1
        assert sources[1].projection.messages[0].content == "Preview second"

    def test_visibility_inventory_stops_paging_when_deadline_expires(self) -> None:
        clock = {"now": 100.0}
        page = {
            "data": [
                {
                    "id": "first-page",
                    "cwd": "C:/work/first-page",
                    "createdAt": 290,
                    "updatedAt": 300,
                    "source": "vscode",
                }
            ],
            "nextCursor": "next-page",
        }
        client = _ClockAdvancingClient(
            {"thread/list": [page], "thread/read": []},
            clock,
            {"thread/list": 6.0},
        )

        with pytest.raises(RuntimeError, match="Codex sidebar deadline exhausted"):
            CodexSourceAdapter(
                client,
                marker_secret=SECRET,
                monotonic=lambda: clock["now"],
            ).list_claude_visibility_sources(
                after=250,
                state_db_only=True,
                discovery_timeout=5.0,
            )

        assert [method for method, _params, _timeout in client.calls] == [
            "thread/list"
        ]

    def test_claude_visibility_inventory_preserves_normal_automation_and_subagent_kinds(
        self,
    ) -> None:
        def entry(native_id: str, updated: int, source: Any, *, archived=False):
            return {
                "id": native_id,
                "cwd": f"C:/work/{native_id}",
                "createdAt": updated - 1,
                "updatedAt": updated,
                "archived": archived,
                "source": source,
            }

        rows = [
            entry("cli", 600, "cli"),
            entry("vscode", 550, "vscode"),
            entry("app-server", 500, "appServer"),
            entry("exec", 450, "exec"),
            entry("automation", 400, {"custom": "automation"}),
        ]
        subagent = entry("subagent", 350, {"subAgent": "review"}, archived=True)
        client = FakeInitializingClient({
            "thread/list": [
                {"data": rows},
                {"data": [subagent]},
            ],
            "thread/read": [
                {"thread": {"id": "cli", "source": "cli", "turns": []}},
                {"thread": {"id": "vscode", "source": "vscode", "turns": []}},
                {
                    "thread": {
                        "id": "app-server",
                        "source": "appServer",
                        "turns": [],
                    }
                },
                {"thread": {"id": "exec", "source": "exec", "turns": []}},
                {
                    "thread": {
                        "id": "automation",
                        "source": {"custom": "automation"},
                        "turns": [],
                    }
                },
                {
                    "thread": {
                        "id": "subagent",
                        "source": {"subAgent": "review"},
                        "turns": [],
                    }
                },
            ],
        })

        sources = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=0)

        by_id = {source.projection.native_id: source for source in sources}
        for native_id in ("cli", "vscode", "app-server"):
            assert by_id[native_id].automation_only is False
            assert by_id[native_id].subagent_only is False
        assert by_id["exec"].automation_only is True
        assert by_id["exec"].subagent_only is False
        assert by_id["automation"].automation_only is True
        assert by_id["automation"].subagent_only is False
        assert by_id["subagent"].automation_only is False
        assert by_id["subagent"].subagent_only is True
        assert by_id["subagent"].projection.native_status == "archived"

    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("<codex_delegation>\nReview this bounded subtask", True),
            ("Discuss the literal <codex_delegation> tag", False),
        ],
    )
    def test_claude_visibility_detects_only_delegation_prompt_prefix(
        self, text: str, expected: bool
    ) -> None:
        row = {
            "id": "delegated",
            "cwd": "C:/work/delegated",
            "createdAt": 1,
            "updatedAt": 2,
            "source": "vscode",
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [
                {
                    "thread": {
                        **row,
                        "turns": [
                            {
                                "items": [
                                    {
                                        "type": "userMessage",
                                        "id": "delegation-request",
                                        "content": [
                                            {
                                                "type": "text",
                                                "text": text,
                                            }
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                }
            ],
        })

        [candidate] = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=0)

        assert candidate.automation_only is False
        assert candidate.subagent_only is expected

    @pytest.mark.parametrize(
        "optional_fields",
        [
            {"agent_nickname": "scout"},
            {"agent_path": "reviewer/worker"},
            {"agent_role": "reviewer"},
            {
                "agent_nickname": "scout",
                "agent_path": "reviewer/worker",
                "agent_role": "reviewer",
            },
            {
                "agent_nickname": None,
                "agent_path": None,
                "agent_role": None,
            },
        ],
    )
    def test_claude_visibility_accepts_protocol_thread_spawn_optional_fields(
        self, optional_fields: dict[str, str | None]
    ) -> None:
        spawn = {
            "depth": 1,
            "parent_thread_id": "parent-thread",
            **optional_fields,
        }
        source = {"subAgent": {"thread_spawn": spawn}}
        row = {
            "id": "spawned",
            "cwd": "C:/work/spawned",
            "createdAt": 1,
            "updatedAt": 2,
            "source": source,
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [
                {"thread": {"id": "spawned", "source": source, "turns": []}}
            ],
        })

        [candidate] = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=0)

        assert candidate.subagent_only is True
        assert candidate.automation_only is False

    @pytest.mark.parametrize(
        "invalid_fields",
        [
            {"agent_nickname": 7},
            {"agent_path": ["reviewer", "worker"]},
            {"agent_role": False},
            {"future_field": "unsupported"},
        ],
    )
    def test_claude_visibility_rejects_malformed_thread_spawn_optional_fields(
        self, invalid_fields: dict[str, Any]
    ) -> None:
        source = {
            "subAgent": {
                "thread_spawn": {
                    "depth": 1,
                    "parent_thread_id": "parent-thread",
                    **invalid_fields,
                }
            }
        }
        row = {
            "id": "bad-spawn",
            "cwd": "C:/work/bad-spawn",
            "createdAt": 1,
            "updatedAt": 2,
            "source": source,
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [
                {"thread": {"id": "bad-spawn", "source": source, "turns": []}}
            ],
        })

        with pytest.raises(ValueError, match="thread/list"):
            CodexSourceAdapter(
                client, marker_secret=SECRET
            ).list_claude_visibility_sources(after=0)

    def test_claude_visibility_maps_git_info_sha_to_git_head(self) -> None:
        sha = "0123456789abcdef0123456789abcdef01234567"
        row = {
            "id": "native-git",
            "cwd": "C:/work/native-git",
            "createdAt": 1,
            "updatedAt": 2,
            "source": "vscode",
            "gitInfo": {"branch": "feature/native", "sha": sha},
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [{"thread": {**row, "turns": []}}],
        })

        [candidate] = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=0)

        assert candidate.git_head == sha
        assert candidate.projection.git_branch == "feature/native"

    def test_claude_visibility_rejects_conflicting_native_and_legacy_git_heads(
        self,
    ) -> None:
        row = {
            "id": "conflicting-head",
            "createdAt": 1,
            "updatedAt": 2,
            "source": "vscode",
            "gitInfo": {"sha": "a" * 40},
            "gitHead": "b" * 40,
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}],
        })

        with pytest.raises(ValueError, match="thread/list"):
            CodexSourceAdapter(
                client, marker_secret=SECRET
            ).list_claude_visibility_sources(after=0)

    @pytest.mark.parametrize(
        ("list_metadata", "read_metadata"),
        [
            ({"cwd": "C:/work/list"}, {"cwd": "C:/work/read"}),
            ({"gitRoot": "C:/repo/list"}, {"gitRoot": "C:/repo/read"}),
            ({"gitBranch": "feature/list"}, {"gitBranch": "feature/read"}),
            ({"gitHead": "a" * 40}, {"gitInfo": {"sha": "b" * 40}}),
            ({"worktreeId": "wt-list"}, {"worktreeId": "wt-read"}),
        ],
    )
    def test_claude_visibility_metadata_conflicts_fail_closed(
        self,
        list_metadata: dict[str, Any],
        read_metadata: dict[str, Any],
    ) -> None:
        row = {
            "id": "metadata-conflict",
            "createdAt": 1,
            "updatedAt": 2,
            "source": "vscode",
            **list_metadata,
        }
        read = {
            "id": "metadata-conflict",
            "source": "vscode",
            "turns": [],
            **read_metadata,
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [{"thread": read}],
        })

        with pytest.raises(ValueError, match="metadata_conflict") as error:
            CodexSourceAdapter(
                client, marker_secret=SECRET
            ).list_claude_visibility_sources(after=0)

        assert getattr(error.value, "code", None) == "metadata_conflict"

    def test_claude_visibility_fills_metadata_present_only_in_thread_read(self) -> None:
        sha = "0123456789abcdef0123456789abcdef01234567"
        row = {
            "id": "read-fill",
            "createdAt": 1,
            "updatedAt": 2,
        }
        read = {
            "id": "read-fill",
            "cwd": "C:/work/read-fill",
            "source": "vscode",
            "gitRoot": "C:/work",
            "gitInfo": {"branch": "feature/read-fill", "sha": sha},
            "worktreeId": "wt-read-fill",
            "turns": [],
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [{"thread": read}],
        })

        [candidate] = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=0)

        assert candidate.projection.cwd == "C:/work/read-fill"
        assert candidate.projection.git_branch == "feature/read-fill"
        assert candidate.git_root == "C:/work"
        assert candidate.git_head == sha
        assert candidate.worktree_id == "wt-read-fill"
        assert candidate.automation_only is False
        assert candidate.subagent_only is False

    def test_claude_visibility_accepts_exact_list_and_read_metadata_match(self) -> None:
        sha = "0123456789abcdef0123456789abcdef01234567"
        metadata = {
            "cwd": "C:/work/exact",
            "source": {"subAgent": "review"},
            "gitRoot": "C:/work",
            "gitInfo": {"branch": "feature/exact", "sha": sha},
            "worktreeId": "wt-exact",
        }
        row = {
            "id": "exact",
            "createdAt": 1,
            "updatedAt": 2,
            **metadata,
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [{"thread": {"id": "exact", "turns": [], **metadata}}],
        })

        [candidate] = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).list_claude_visibility_sources(after=0)

        assert candidate.git_head == sha
        assert candidate.subagent_only is True

    @pytest.mark.parametrize(
        "source_kind",
        ["unknown", {"custom": "future-source"}, None, {"subAgent": None}],
    )
    def test_claude_visibility_unknown_or_malformed_source_kind_fails_closed(
        self, source_kind: Any
    ) -> None:
        row = {
            "id": "bad-source-kind",
            "cwd": "C:/work/bad-source-kind",
            "createdAt": 1,
            "updatedAt": 2,
            "source": source_kind,
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [
                {
                    "thread": {
                        "id": "bad-source-kind",
                        "source": source_kind,
                        "turns": [],
                    }
                }
            ],
        })

        with pytest.raises(ValueError, match="thread/list"):
            CodexSourceAdapter(
                client, marker_secret=SECRET
            ).list_claude_visibility_sources(after=0)

    def test_claude_visibility_source_kind_conflict_between_list_and_read_fails_closed(
        self,
    ) -> None:
        row = {
            "id": "conflict",
            "cwd": "C:/work/conflict",
            "createdAt": 1,
            "updatedAt": 2,
            "source": "vscode",
        }
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": []}],
            "thread/read": [
                {
                    "thread": {
                        "id": "conflict",
                        "source": {"subAgent": "review"},
                        "turns": [],
                    }
                }
            ],
        })

        with pytest.raises(ValueError, match="source kind"):
            CodexSourceAdapter(
                client, marker_secret=SECRET
            ).list_claude_visibility_sources(after=0)

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
            summary.native_id for summary in adapter.list_full_inventory(archived=False)
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
        assert client.initialize_calls == [{"capabilities": {"experimentalApi": True}}]
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
        assert client.initialize_calls == [{"capabilities": {"experimentalApi": True}}]
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

    def test_invalid_entries_and_conflicting_duplicates_fail_closed(self) -> None:
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

        with pytest.raises(ValueError, match="thread/list"):
            CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
                archived=False
            )

    @pytest.mark.parametrize(
        "invalid",
        [
            "not-an-object",
            {"id": 123, "createdAt": 1, "updatedAt": 2},
            {"id": "bad-cwd", "cwd": 123, "createdAt": 1, "updatedAt": 2},
            {"id": "bad-time", "createdAt": "not-a-date", "updatedAt": 2},
            {
                "id": "bad-source",
                "createdAt": 1,
                "updatedAt": 2,
                "source": {"custom": "future-source"},
            },
        ],
    )
    def test_single_malformed_inventory_row_fails_closed(self, invalid: Any) -> None:
        client = FakeInitializingClient({"thread/list": [{"data": [invalid]}]})

        with pytest.raises(ValueError, match="thread/list"):
            CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
                archived=False
            )

    def test_conflicting_duplicate_across_pages_fails_closed(self) -> None:
        row = {"id": "duplicate", "createdAt": 1, "updatedAt": 2}
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [{**row, "title": "first"}], "nextCursor": "page-2"},
                {"data": [{**row, "title": "second"}]},
            ]
        })

        with pytest.raises(ValueError, match="thread/list"):
            CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
                archived=False
            )

    def test_later_page_malformed_row_discards_the_whole_inventory(self) -> None:
        valid = {"id": "valid", "createdAt": 1, "updatedAt": 2}
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [valid], "nextCursor": "page-2"},
                {"data": [{"id": "broken", "createdAt": True, "updatedAt": 3}]},
            ]
        })

        with pytest.raises(ValueError, match="thread/list"):
            CodexSourceAdapter(client, marker_secret=SECRET).list_inventory(
                archived=False
            )

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
    def test_read_exact_thread_reuses_cached_metadata_for_lean_response(self) -> None:
        row = {
            "id": "thread-active",
            "title": "Cached title",
            "cwd": "C:/cached",
            "createdAt": 1,
            "updatedAt": 2,
            "revision": "cached-revision",
        }
        response = _fixture("thread-read.json")
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": [row]}],
            "thread/read": [response],
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)
        assert adapter.list_inventory(archived=False)[0].native_id == "thread-active"

        projection = adapter.read_native_thread("thread-active")

        assert projection.native_id == "thread-active"
        assert projection.title == "Cached title"
        assert projection.cwd == "C:/cached"
        assert [method for method, _params, _timeout in client.calls] == [
            "thread/list",
            "thread/read",
            "thread/list",
        ]

    def test_read_exact_thread_does_not_page_full_inventory(self) -> None:
        response = _fixture("thread-read.json")
        response["thread"]["createdAt"] = 1
        response["thread"]["updatedAt"] = 2
        client = FakeInitializingClient({"thread/read": [response]})
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        projection = adapter.read_native_thread("thread-active")

        assert projection.native_id == "thread-active"
        assert [
            method for method, _params, _timeout in client.calls
        ] == ["thread/read"]

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

    def test_cached_index_opt_in_serves_many_lookups_from_one_fetch(self) -> None:
        """The scan's fast path: one inventory fetch resolves many native ids."""

        rows = [
            {"id": "wanted", "createdAt": 1, "updatedAt": 2, "revision": "r1"},
            {"id": "other", "createdAt": 1, "updatedAt": 2, "revision": "r1"},
        ]
        client = FakeInitializingClient({"thread/list": [{"data": rows}]})
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        first = adapter.find_native_thread("wanted", allow_cached_index=True)
        second = adapter.find_native_thread("other", allow_cached_index=True)

        assert first is not None and first.native_id == "wanted"
        assert second is not None and second.native_id == "other"
        assert len([m for m, _p, _t in client.calls if m == "thread/list"]) == 1

    def test_default_lookup_ignores_a_warm_index_so_archiving_is_seen(self) -> None:
        """A warm active index must never answer an authoritative lookup.

        Regression guard for 2026-08-13: find_native_thread consulted the TTL'd
        index unconditionally, so a thread archived inside the 900s window kept
        resolving from the stale ACTIVE index and never reached the archived
        search -- refresh_session reported native_status 'active' for an archived
        thread.
        """

        row = {"id": "wanted", "createdAt": 1, "updatedAt": 2, "revision": "r1"}
        client = FakeInitializingClient({
            "thread/list": [
                {"data": [row]},
                {"data": []},
                {"threads": [{**row, "archived": True}]},
            ]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)
        assert adapter.find_native_thread("wanted", allow_cached_index=True) is not None

        found = adapter.find_native_thread("wanted")

        assert found is not None and found.archived is True

    def test_bounded_state_db_lookup_stays_one_page_even_when_opted_in(self) -> None:
        """state_db_only exists to be a single bounded page; the index pages all."""

        row = {"id": "wanted", "createdAt": 1, "updatedAt": 2, "revision": "r1"}
        client = FakeInitializingClient({
            "thread/list": [{"data": [row], "nextCursor": "must-not-be-read"}]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        found = adapter.find_native_thread(
            "wanted",
            state_db_only=True,
            allow_cached_index=True,
        )

        assert found is not None and found.native_id == "wanted"
        assert len([m for m, _p, _t in client.calls if m == "thread/list"]) == 1

    def test_find_does_not_hide_thread_from_next_inventory(self) -> None:
        row = {"id": "wanted", "createdAt": 1, "updatedAt": 2}
        client = FakeInitializingClient({
            "thread/list": [{"data": [row]}, {"data": [row]}]
        })
        adapter = CodexSourceAdapter(client, marker_secret=SECRET)

        assert adapter.find_native_thread("wanted") is not None
        assert adapter.list_inventory(archived=False)[0].native_id == "wanted"


class TestProjection:
    def test_projection_uses_inventory_preview_when_full_read_times_out(self) -> None:
        summary = CodexThreadSummary(
            native_id="oversized-catalog-thread",
            title="Long-running catalog task",
            cwd="C:/work/catalog",
            started_at=100,
            last_active=300,
            archived=False,
            revision="state-db-revision",
            git_branch="codex/catalog",
            git_head="deadbeef",
            source_kind="vscode",
            preview="Keep this meaningful session discoverable",
            native_path="C:/codex/sessions/oversized-catalog-thread.jsonl",
        )
        client = FakeInitializingClient({
            "thread/read": [TimeoutError("synthetic oversized thread")]
        })

        projection = CodexSourceAdapter(
            client, marker_secret=SECRET
        ).project_thread(summary)

        assert projection.native_id == summary.native_id
        assert projection.native_path == summary.native_path
        assert projection.native_cursor == summary.revision
        assert [(message.role, message.content) for message in projection.messages] == [
            ("user", summary.preview)
        ]

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
    @pytest.mark.parametrize(
        ("native_id", "characterization_id"),
        (
            (
                "019f8621-4d36-7fe0-9419-319ee7ec09dd",
                "0e831788-1bc1-4324-a58f-0343bcde25b7",
            ),
            (
                "019f8610-36b9-79e3-bc2d-3d4d057582d5",
                "c19dd390-9f40-494d-9e2e-d8ffbc1265fb",
            ),
        ),
    )
    def test_report_backed_thread_overrides_ephemeral_marker_and_keeps_path(
        self,
        native_id: str,
        characterization_id: str,
    ) -> None:
        bridge_id = f"characterization-{characterization_id}-codex"
        invalid_ephemeral_marker = _marker("ephemeral")[:-1] + "x"
        response = {
            "thread": {
                "id": native_id,
                "turns": [
                    {
                        "id": "registration",
                        "items": [
                            {
                                "type": "userMessage",
                                "id": "registration-message",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": (
                                            "Hermes Session Bridge registration only.\n"
                                            f"{invalid_ephemeral_marker}"
                                        ),
                                    }
                                ],
                            },
                            {
                                "type": "agentMessage",
                                "id": "ready",
                                "text": "READY",
                            },
                        ],
                    }
                ],
            }
        }
        summary = CodexThreadSummary(
            **{
                **_summary(native_id=native_id, archived=True).__dict__,
                "native_path": f"C:/codex/archived/{native_id}.jsonl",
            }
        )
        client = FakeInitializingClient({"thread/read": [response]})

        projection = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            trusted_origins={native_id: bridge_id},
        ).project_thread(summary)

        assert projection.origin_kind is OriginKind.BRIDGE_PLACEHOLDER
        assert projection.origin_bridge_id == bridge_id
        assert projection.native_path == summary.native_path

    def test_unreported_registration_text_with_invalid_marker_remains_native(
        self,
    ) -> None:
        invalid_marker = _marker("forged")[:-1] + "x"
        client = FakeInitializingClient({
            "thread/read": [
                _read_with_items({
                    "type": "userMessage",
                    "id": "forged-registration",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Hermes Session Bridge registration only.\n"
                                f"{invalid_marker}"
                            ),
                        }
                    ],
                })
            ]
        })

        projection = CodexSourceAdapter(
            client,
            marker_secret=SECRET,
            trusted_origins={},
        ).project_thread(_summary())

        assert projection.origin_kind is OriginKind.NATIVE
        assert projection.origin_bridge_id is None

    def test_unchecked_summary_cannot_assert_trusted_origin(self) -> None:
        client = FakeInitializingClient({
            "thread/read": [
                _read_with_items({
                    "type": "agentMessage",
                    "id": "answer",
                    "text": "normal native work",
                })
            ]
        })
        summary = CodexThreadSummary(
            **{
                **_summary().__dict__,
                "trusted_origin_bridge_id": "characterization-forged-codex",
            }
        )

        with pytest.raises(ValueError, match="mapping conflicts with summary"):
            CodexSourceAdapter(
                client,
                marker_secret=SECRET,
                trusted_origins={},
            ).project_thread(summary)

    def test_report_origin_conflicting_with_valid_marker_fails_closed(self) -> None:
        native_id = "thread-active"
        client = FakeInitializingClient({
            "thread/read": [
                _read_with_items({
                    "type": "userMessage",
                    "id": "production-marker",
                    "content": [{"type": "text", "text": _marker("production")}],
                })
            ]
        })

        with pytest.raises(ValueError, match="trusted origin conflicts"):
            CodexSourceAdapter(
                client,
                marker_secret=SECRET,
                trusted_origins={native_id: "characterization-other-codex"},
            ).project_thread(_summary(native_id=native_id))

    def test_store_repairs_native_row_to_exact_report_provenance(
        self, tmp_path: Path
    ) -> None:
        native_id = "019f8621-4d36-7fe0-9419-319ee7ec09dd"
        bridge_id = (
            "characterization-0e831788-1bc1-4324-a58f-0343bcde25b7-codex"
        )
        native = CodexSourceAdapter(
            FakeInitializingClient({
                "thread/read": [
                    {
                        "thread": {
                            "id": native_id,
                            "turns": [
                                {
                                    "items": [
                                        {
                                            "type": "agentMessage",
                                            "id": "ready",
                                            "text": "READY",
                                        }
                                    ]
                                }
                            ],
                        }
                    }
                ]
            }),
            marker_secret=SECRET,
        ).project_thread(_summary(native_id=native_id))
        repaired = replace(
            native,
            origin_kind=OriginKind.BRIDGE_PLACEHOLDER,
            origin_bridge_id=bridge_id,
        )
        db = SessionDB(tmp_path / "state.db")
        try:
            store = SessionBridgeStore(db, clock=lambda: 500.0)
            store.upsert_projection(native)
            store.upsert_projection(repaired)

            row = store.get_external_session(f"codex:{native_id}")
            assert row is not None
            assert row["origin_kind"] == OriginKind.BRIDGE_PLACEHOLDER.value
            assert row["origin_bridge_id"] == bridge_id
            assert len(db.list_sessions_rich(source="codex")) == 1
        finally:
            db.close()

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
        assert (
            adapter.projection_has_marker_payload(
                projection,
                BridgeMarkerPayload(
                    bridge_id=payload.bridge_id,
                    source_session_id="claude:different-source",
                    target_provider=payload.target_provider,
                    policy_generation=payload.policy_generation,
                ),
            )
            is False
        )

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
