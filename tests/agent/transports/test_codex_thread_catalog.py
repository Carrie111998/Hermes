from __future__ import annotations

from typing import Any, Optional

from agent.transports.codex_thread_catalog import (
    list_codex_threads,
    resolve_codex_thread,
)


class FakeClient:
    def __init__(self, *, codex_bin: str = "codex", codex_home=None) -> None:
        self.initialize_kwargs: dict[str, Any] = {}
        self.requests: list[tuple[str, dict, float]] = []
        self.closed = False

    def initialize(self, **kwargs):
        self.initialize_kwargs = dict(kwargs)
        return {}

    def request(
        self, method: str, params: Optional[dict] = None, timeout: float = 30.0
    ):
        self.requests.append((method, params or {}, timeout))
        return {
            "data": [
                {
                    "id": "thread-new",
                    "cwd": "/repo",
                    "preview": "new work",
                    "name": "Named task",
                    "source": "vscode",
                    "status": {"type": "notLoaded"},
                    "recencyAt": 123,
                    "isPinned": True,
                },
                {
                    "id": "thread-old",
                    "cwd": "/repo",
                    "preview": "old work",
                    "source": "cli",
                    "updatedAt": 100,
                },
                {"cwd": "/repo", "preview": "missing id"},
            ],
            "nextCursor": "next-page",
        }

    def close(self):
        self.closed = True


def test_list_codex_threads_uses_native_catalog_protocol():
    client = FakeClient()
    rows, cursor = list_codex_threads(
        limit=20,
        search_term="work",
        client_factory=lambda **kwargs: client,
    )

    assert [row.thread_id for row in rows] == ["thread-new", "thread-old"]
    assert rows[0].title == "Named task"
    assert rows[0].is_pinned is True
    assert cursor == "next-page"
    assert client.initialize_kwargs["capabilities"] == {
        "experimentalApi": True,
        "requestAttestation": False,
    }
    method, params, timeout = client.requests[0]
    assert method == "thread/list"
    assert params == {
        "limit": 20,
        "sortKey": "recency_at",
        "sortDirection": "desc",
        "archived": False,
        "useStateDbOnly": False,
        "searchTerm": "work",
    }
    assert timeout == 15.0
    assert client.closed is True


def test_resolve_codex_thread_accepts_picker_id_prefix_and_name():
    rows, _ = list_codex_threads(client_factory=lambda **kwargs: FakeClient())

    assert resolve_codex_thread(rows, "1").thread_id == "thread-new"
    assert resolve_codex_thread(rows, "thread-old").thread_id == "thread-old"
    assert resolve_codex_thread(rows, "thread-n").thread_id == "thread-new"
    assert resolve_codex_thread(rows, "named task").thread_id == "thread-new"
    assert resolve_codex_thread(rows, "old work").thread_id == "thread-old"
    assert resolve_codex_thread(rows, "99") is None
