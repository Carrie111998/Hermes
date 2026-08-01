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
        (["pending"], "session_sidebar_pending", {"limit": 1}),
        (
            [
                "reserve",
                "--lease-token",
                "lease",
                "--reconciliation-proof-digest",
                "3" * 64,
                "--reconciliation-generation",
                "scan:1",
            ],
            "session_sidebar_reserve",
            {
                "lease_token": "lease",
                "reconciliation_proof_digest": "3" * 64,
                "reconciliation_generation": "scan:1",
            },
        ),
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
            [
                "fail",
                "--lease-token",
                "lease",
                "--error-code",
                "sqlite_busy",
                "--thread-id",
                "thread",
            ],
            "session_sidebar_fail",
            {
                "lease_token": "lease",
                "error_code": "sqlite_busy",
                "codex_thread_id": "thread",
            },
        ),
        (
            ["fail", "--lease-token", "lease", "--error-code", "sqlite_busy"],
            "session_sidebar_fail",
            {"lease_token": "lease", "error_code": "sqlite_busy"},
        ),
        (
            ["hydration-pending"],
            "session_sidebar_hydration_pending",
            {"limit": 1},
        ),
        (
            ["hydration-reserve", "--lease-token=hydrate-lease"],
            "session_sidebar_hydration_reserve",
            {"lease_token": "hydrate-lease"},
        ),
        (
            [
                "hydration-commit",
                "--lease-token=hydrate-lease",
                "--thread-id=thread",
                "--hydration-marker=marker",
            ],
            "session_sidebar_hydration_commit",
            {
                "lease_token": "hydrate-lease",
                "codex_thread_id": "thread",
                "hydration_marker": "marker",
            },
        ),
        (
            [
                "hydration-fail",
                "--lease-token=hydrate-lease",
                "--error-code=hydration_send_ambiguous",
                "--thread-id=thread",
            ],
            "session_sidebar_hydration_fail",
            {
                "lease_token": "hydrate-lease",
                "error_code": "hydration_send_ambiguous",
                "codex_thread_id": "thread",
            },
        ),
    ]

    for argv, expected_tool, expected_payload in cases:
        calls.clear()
        assert asyncio.run(dispatch(argv, call=call)) == {"ok": True}
        assert calls == [(expected_tool, expected_payload)]


def test_broker_client_rejects_any_batch_size_other_than_one_before_transport() -> None:
    async def unavailable(_tool: str, _payload: dict[str, object]):
        raise AssertionError("transport must not run")

    try:
        asyncio.run(dispatch(["pending", "--limit", "2"], call=unavailable))
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("invalid limit must exit")
