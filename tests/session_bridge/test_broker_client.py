from __future__ import annotations

import asyncio

from session_bridge.broker_client import dispatch


def test_broker_client_maps_each_command_to_the_exact_mcp_tool() -> None:
    calls: list[tuple[str, dict[str, object]]] = []

    async def call(tool: str, payload: dict[str, object]) -> dict[str, object]:
        calls.append((tool, payload))
        return {"ok": True}

    cases = [
        (["status"], "session_status", {}),
        (["pending", "--limit", "5"], "session_sidebar_pending", {"limit": 5}),
        (
            ["bind", "--lease-token", "lease", "--thread-id", "thread"],
            "session_sidebar_bind",
            {"lease_token": "lease", "codex_thread_id": "thread"},
        ),
        (
            ["commit", "--lease-token", "lease", "--thread-id", "thread"],
            "session_sidebar_commit",
            {"lease_token": "lease", "codex_thread_id": "thread"},
        ),
        (
            ["fail", "--lease-token", "lease", "--error-code", "sqlite_busy"],
            "session_sidebar_fail",
            {"lease_token": "lease", "error_code": "sqlite_busy"},
        ),
    ]

    for argv, expected_tool, expected_payload in cases:
        calls.clear()
        assert asyncio.run(dispatch(argv, call=call)) == {"ok": True}
        assert calls == [(expected_tool, expected_payload)]


def test_broker_client_rejects_out_of_range_batch_before_transport() -> None:
    async def unavailable(_tool: str, _payload: dict[str, object]):
        raise AssertionError("transport must not run")

    try:
        asyncio.run(dispatch(["pending", "--limit", "6"], call=unavailable))
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("invalid limit must exit")
