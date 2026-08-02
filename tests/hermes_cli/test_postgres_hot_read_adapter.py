import math

import pytest


def test_make_24h_request_and_invalid_values_do_not_acquire():
    from hermes_cli.postgres_hot_read_adapter import (
        HotReadStatus,
        make_24h_request,
        read_hot_messages,
    )

    request = make_24h_request("session", now_epoch_s=100_000, limit=25, offset=3)
    assert request.cutoff_epoch_s == 13_600

    acquired = False

    def acquire():
        nonlocal acquired
        acquired = True
        raise AssertionError("must not acquire")

    invalid = [
        dict(session_id="", now_epoch_s=1),
        dict(session_id="x" * 1025, now_epoch_s=1),
        dict(session_id="x\ud800", now_epoch_s=1),
        dict(session_id="x", now_epoch_s=math.inf),
        dict(session_id="x", now_epoch_s=True),
        dict(session_id="x", now_epoch_s=1, limit=0),
        dict(session_id="x", now_epoch_s=1, limit=True),
        dict(session_id="x", now_epoch_s=1, offset=10_001),
    ]
    for values in invalid:
        with pytest.raises(ValueError):
            make_24h_request(**values)

    result = awaitable_result(read_hot_messages(None, acquire))
    assert result.status is HotReadStatus.INVALID_REQUEST
    assert acquired is False


def awaitable_result(awaitable):
    import asyncio

    return asyncio.run(awaitable)


def test_query_is_exact_bounded_active_and_ordered():
    from hermes_cli.postgres_hot_read_adapter import MESSAGE_COLUMNS, make_24h_request, read_hot_messages

    class Connection:
        async def fetch(self, query, *args, timeout):
            expected = (
                f"SELECT {', '.join(MESSAGE_COLUMNS)} FROM hermes_hot.messages "
                "WHERE session_id=$1 AND timestamp >= $2 AND ($3::boolean OR active=1) "
                "ORDER BY id ASC LIMIT $4 OFFSET $5"
            )
            assert query == expected
            assert args == ("s", 13_600.0, False, 7, 2)
            assert timeout == 2.0
            return []

    class Context:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, *args): return False

    request = make_24h_request("s", now_epoch_s=100_000, limit=7, offset=2)
    result = awaitable_result(read_hot_messages(request, Context))
    assert result.status.value == "ok"
    assert result.rows == ()


def test_driver_cannot_return_more_rows_than_request_limit():
    from hermes_cli.postgres_hot_read_adapter import make_24h_request, read_hot_messages

    class Connection:
        async def fetch(self, *args, **kwargs):
            return [_row(id=1), _row(id=2)]

    class Context:
        async def __aenter__(self):
            return Connection()

        async def __aexit__(self, *args):
            return False

    request = make_24h_request("s", now_epoch_s=100_000, limit=1)
    result = awaitable_result(read_hot_messages(request, Context))
    assert result.status.value == "malformed_row"
    assert result.rows == ()


def _row(**changes):
    row = {
        "id": 4, "session_id": "s", "role": "assistant", "content": memoryview(b'\x00json:{"a":1}'),
        "tool_call_id": None, "tool_calls": b'[{"id":"call"}]', "tool_name": None,
        "timestamp": 4_000.0, "token_count": 7, "finish_reason": "stop", "reasoning": b"why",
        "reasoning_content": None, "reasoning_details": None, "codex_reasoning_items": None,
        "codex_message_items": None, "platform_message_id": None, "observed": 0, "active": 1,
        "compacted": 0, "effect_disposition": None, "api_content": None, "display_kind": None,
        "display_metadata": None,
    }
    row.update(changes)
    return row


def _read_rows(rows):
    from hermes_cli.postgres_hot_read_adapter import make_24h_request, read_hot_messages

    class Connection:
        async def fetch(self, *args, **kwargs): return rows
    class Context:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, *args): return False
    return awaitable_result(read_hot_messages(make_24h_request("s", now_epoch_s=90_000), Context))


def test_normalizes_bytea_json_content_and_tool_calls():
    result = _read_rows([_row()])
    assert result.status.value == "ok"
    assert result.rows[0]["content"] == {"a": 1}
    assert result.rows[0]["tool_calls"] == [{"id": "call"}]
    assert result.rows[0]["reasoning"] == "why"

    malformed_json = _read_rows([_row(content=b"\x00json:{bad", tool_calls=b"{bad")])
    assert malformed_json.rows[0]["content"] == "\x00json:{bad"
    assert malformed_json.rows[0]["tool_calls"] == []


@pytest.mark.parametrize(
    "rows",
    [
        [_row(session_id="other")],
        [_row(timestamp=3_599)],
        [_row(active=0)],
        [_row(id=2), _row(id=1)],
        [_row(id=1), _row(id=1)],
    ],
)
def test_hot_rows_must_match_request_scope_and_strict_order(rows):
    result = _read_rows(rows)
    assert result.status.value == "malformed_row"
    assert result.rows == ()


@pytest.mark.parametrize("rows", [
    [_row(content=b"\xff")],
    [dict(_row(), extra="private")],
    [{key: value for key, value in _row().items() if key != "role"}],
    [_row(id=True)],
    [_row(role=4)],
    [_row(content=b"x" * (256 * 1024 + 1))],
    [_row(content=b"x" * 200_000) for _ in range(6)],
])
def test_unsafe_rows_and_byte_bounds_fail_closed(rows):
    result = _read_rows(rows)
    assert result.status.value == "malformed_row"
    assert result.rows == ()


@pytest.mark.parametrize("error,status", [
    (TimeoutError("private timeout"), "timeout"),
    (ConnectionError("postgres://private"), "unavailable"),
    (RuntimeError("private fetch details"), "unavailable"),
])
def test_failures_are_categorical_without_error_leakage(error, status):
    from hermes_cli.postgres_hot_read_adapter import make_24h_request, read_hot_messages

    class Context:
        async def __aenter__(self):
            if isinstance(error, ConnectionError): raise error
            return self
        async def fetch(self, *args, **kwargs): raise error
        async def __aexit__(self, *args): return False
    result = awaitable_result(read_hot_messages(make_24h_request("s", now_epoch_s=90_000), Context))
    assert result.status.value == status
    assert result.rows == ()
    assert "private" not in repr(result)


def test_cancellation_propagates_releases_context_and_leaves_no_adapter_task():
    import asyncio
    from hermes_cli.postgres_hot_read_adapter import make_24h_request, read_hot_messages

    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Context:
            async def __aenter__(self): return self
            async def fetch(self, *args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            async def __aexit__(self, *args):
                release.set()
                return False
        task = asyncio.create_task(read_hot_messages(make_24h_request("s", now_epoch_s=90_000), Context))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert release.is_set()
        assert task.done()
        assert asyncio.all_tasks() == {asyncio.current_task()}
    asyncio.run(scenario())


def test_shadow_match_returns_metadata_only_and_clones_before_await():
    from hermes_cli.postgres_hot_read_adapter import compare_shadow_messages, make_24h_request

    sqlite = [_row(content={"a": 1}, tool_calls=[{"id": "call"}], reasoning="why")]
    original_nested = sqlite[0]["content"]
    class Connection:
        async def fetch(self, *args, **kwargs): return [_row()]
    class Context:
        async def __aenter__(self):
            sqlite[0]["content"]["a"] = 999
            return Connection()
        async def __aexit__(self, *args): return False
    comparison = awaitable_result(compare_shadow_messages(
        make_24h_request("s", now_epoch_s=90_000), sqlite, Context
    ))
    assert comparison.outcome.value == "match"
    assert dict(comparison.metadata) == {
        "scope": "messages_24h", "sqlite_row_count": 1, "postgres_row_count": 1,
        "hot_status": "ok", "outcome": "match",
    }
    assert not hasattr(comparison, "rows")
    assert sqlite[0]["content"] is original_nested


@pytest.mark.parametrize("sqlite,hot,reason,index,field", [
    (
        [_row(session_id="private-session", content="a", tool_calls=[], reasoning="why")],
        [],
        "row_count",
        None,
        None,
    ),
    (
        [
            {
                k: v
                for k, v in _row(
                    session_id="private-session",
                    content="a",
                    tool_calls=[],
                    reasoning="why",
                ).items()
                if k != "role"
            }
        ],
        [_row(session_id="private-session", content=b"a")],
        "row_shape",
        0,
        None,
    ),
    (
        [
            _row(
                session_id="private-session",
                content="secret-sqlite",
                tool_calls=[],
                reasoning="why",
            )
        ],
        [_row(session_id="private-session", content=b"secret-postgres")],
        "field_value",
        0,
        "content",
    ),
])
def test_shadow_mismatches_expose_only_safe_metadata(sqlite, hot, reason, index, field):
    from hermes_cli.postgres_hot_read_adapter import compare_shadow_messages, make_24h_request
    class Connection:
        async def fetch(self, *args, **kwargs): return hot
    class Context:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, *args): return False
    result = awaitable_result(compare_shadow_messages(make_24h_request("private-session", now_epoch_s=90_000), sqlite, Context))
    assert result.outcome.value == "mismatch"
    assert result.metadata["reason"] == reason
    if index is not None: assert result.metadata["first_difference_index"] == index
    if field is not None: assert result.metadata["field"] == field
    rendered = repr(result)
    assert "secret-" not in rendered and "private-session" not in rendered


def test_forged_request_is_revalidated_before_acquire():
    from hermes_cli.postgres_hot_read_adapter import HotReadRequest, read_hot_messages
    acquired = False
    def acquire():
        nonlocal acquired
        acquired = True
        raise AssertionError
    forged = HotReadRequest("s", float("nan"), 101, -1, False)
    result = awaitable_result(read_hot_messages(forged, acquire))
    assert result.status.value == "invalid_request"
    assert acquired is False


@pytest.mark.parametrize("mode,reason", [
    ("timeout", "hot_timeout"),
    ("unavailable", "hot_unavailable"),
    ("malformed", "hot_malformed_row"),
    ("invalid", "invalid_request"),
    ("sqlite_large", "sqlite_out_of_bounds"),
])
def test_shadow_fail_open_is_skipped_safe_and_does_not_mutate_input(mode, reason):
    import copy
    from hermes_cli.postgres_hot_read_adapter import compare_shadow_messages, make_24h_request
    sqlite = [_row(content={"private": [1]}, tool_calls=[])]
    if mode == "sqlite_large": sqlite[0]["content"] = "x" * (256 * 1024 + 1)
    before = copy.deepcopy(sqlite)
    identities = (id(sqlite), id(sqlite[0]), id(sqlite[0]["content"]))
    class Connection:
        async def fetch(self, *args, **kwargs):
            if mode == "timeout": raise TimeoutError("private")
            if mode == "unavailable": raise OSError("private")
            if mode == "malformed": return [{"private": "row"}]
            return []
    class Context:
        async def __aenter__(self): return Connection()
        async def __aexit__(self, *args): return False
    request = None if mode == "invalid" else make_24h_request("s", now_epoch_s=90_000)
    result = awaitable_result(compare_shadow_messages(request, sqlite, Context))
    assert result.outcome.value == "skipped"
    assert result.metadata["reason"] == reason
    assert sqlite == before
    assert identities == (id(sqlite), id(sqlite[0]), id(sqlite[0]["content"]))


@pytest.mark.parametrize(
    "sqlite_rows",
    [
        [_row(content="safe", tool_calls=[], reasoning="why", timestamp=3_599, active=1)],
        [_row(content="safe", tool_calls=[], reasoning="why", timestamp=4_000, active=0)],
        [
            _row(id=1, content="safe", tool_calls=[], reasoning="why", timestamp=4_000),
            _row(id=2, content="safe", tool_calls=[], reasoning="why", timestamp=4_000),
        ],
    ],
)
def test_shadow_rejects_sqlite_outside_request_bounds_before_acquire(sqlite_rows):
    from hermes_cli.postgres_hot_read_adapter import compare_shadow_messages, make_24h_request

    acquired = False

    def acquire():
        nonlocal acquired
        acquired = True
        raise AssertionError("must not acquire")

    request = make_24h_request("s", now_epoch_s=90_000, limit=1)
    result = awaitable_result(compare_shadow_messages(request, sqlite_rows, acquire))
    assert result.outcome.value == "skipped"
    assert result.metadata["reason"] == "sqlite_out_of_bounds"
    assert acquired is False


def test_shadow_rejects_unordered_sqlite_slice_before_acquire():
    from hermes_cli.postgres_hot_read_adapter import compare_shadow_messages, make_24h_request

    acquired = False

    def acquire():
        nonlocal acquired
        acquired = True
        raise AssertionError("must not acquire")

    sqlite_rows = [
        _row(id=2, content="safe", tool_calls=[], reasoning="why"),
        _row(id=1, content="safe", tool_calls=[], reasoning="why"),
    ]
    request = make_24h_request("s", now_epoch_s=90_000, limit=2)
    result = awaitable_result(compare_shadow_messages(request, sqlite_rows, acquire))
    assert result.outcome.value == "skipped"
    assert result.metadata["reason"] == "sqlite_out_of_bounds"
    assert acquired is False


def test_shadow_rejects_nested_or_cyclic_sqlite_payload_before_acquire():
    from hermes_cli.postgres_hot_read_adapter import (
        MAX_ROW_BYTES,
        compare_shadow_messages,
        make_24h_request,
    )

    cycle = {}
    cycle["self"] = cycle
    payloads = [{"private": "x" * MAX_ROW_BYTES}, cycle]
    for content in payloads:
        acquired = False

        def acquire():
            nonlocal acquired
            acquired = True
            raise AssertionError("must not acquire")

        sqlite_rows = [
            _row(
                content=content,
                tool_calls=[],
                reasoning="why",
                timestamp=4_000,
                active=1,
            )
        ]
        request = make_24h_request("s", now_epoch_s=90_000, limit=1)
        result = awaitable_result(compare_shadow_messages(request, sqlite_rows, acquire))
        assert result.outcome.value == "skipped"
        assert result.metadata["reason"] == "sqlite_out_of_bounds"
        assert acquired is False


def test_all_metadata_is_allowlisted_and_only_shadow_runtime_wires_adapter():
    from pathlib import Path
    from hermes_cli.postgres_hot_read_adapter import METADATA_KEYS

    assert METADATA_KEYS == frozenset({
        "scope", "reason", "sqlite_row_count", "postgres_row_count",
        "first_difference_index", "field", "hot_status", "outcome",
    })
    source_root = Path(__file__).parents[2]
    wired = sorted(
        path.name
        for path in (source_root / "hermes_cli").glob("*.py")
        if path.name != "postgres_hot_read_adapter.py"
        and "postgres_hot_read_adapter" in path.read_text()
    )
    assert wired == ["postgres_hot_shadow_runtime.py"]
