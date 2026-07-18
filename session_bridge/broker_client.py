"""Authenticated local transport fallback for the Codex sidebar broker."""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable, Sequence
import json
from pathlib import Path
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

BrokerCall = Callable[[str, dict[str, object]], Awaitable[dict[str, Any]]]


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="session-bridge-broker")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status")
    pending = commands.add_parser("pending")
    pending.add_argument("--limit", type=int, choices=range(1, 6), default=5)
    for name in ("bind", "commit"):
        command = commands.add_parser(name)
        command.add_argument("--lease-token", required=True)
        command.add_argument("--thread-id", required=True)
    fail = commands.add_parser("fail")
    fail.add_argument("--lease-token", required=True)
    fail.add_argument("--error-code", required=True)
    return parser


async def dispatch(
    argv: Sequence[str], *, call: BrokerCall
) -> dict[str, Any]:
    args = _parser().parse_args(list(argv))
    if args.command == "status":
        return await call("session_status", {})
    if args.command == "pending":
        return await call("session_sidebar_pending", {"limit": args.limit})
    if args.command in {"bind", "commit"}:
        return await call(
            f"session_sidebar_{args.command}",
            {
                "lease_token": args.lease_token,
                "codex_thread_id": args.thread_id,
            },
        )
    return await call(
        "session_sidebar_fail",
        {"lease_token": args.lease_token, "error_code": args.error_code},
    )


async def _call_tool(tool: str, payload: dict[str, object]) -> dict[str, Any]:
    token = (Path.home() / ".hermes" / "session-bridge" / "token").read_text(
        encoding="utf-8"
    ).strip()
    timeout = httpx.Timeout(120.0)
    async with httpx.AsyncClient(
        headers={"Authorization": f"Bearer {token}"}, timeout=timeout
    ) as client:
        async with streamable_http_client(
            "http://127.0.0.1:7484/mcp", http_client=client
        ) as (read, write, _session_id):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool, payload)
                content = result.structuredContent
                if not isinstance(content, dict):
                    raise RuntimeError("broker response is malformed")
                return content


def main(argv: Sequence[str] | None = None) -> int:
    import sys

    try:
        result = asyncio.run(dispatch(sys.argv[1:] if argv is None else argv, call=_call_tool))
    except Exception:
        print(json.dumps({"error": "broker_unavailable"}))
        return 3
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
