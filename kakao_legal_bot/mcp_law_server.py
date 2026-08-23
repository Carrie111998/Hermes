#!/usr/bin/env python3
"""MCP stdio server exposing the Korean legal open APIs.

The bot calls these tools in-process; this wrapper makes the same search
available to any MCP client (Claude Code, Hermes, an IDE) so you can look
up 법령·판례 while drafting, without a second implementation.

    claude mcp add korean-law -- python kakao_legal_bot/mcp_law_server.py

Env: LAW_OC (law.go.kr 신청 아이디), DATA_GO_KR_KEY (공공데이터포털 인증키).

Speaks JSON-RPC 2.0 over stdin/stdout directly — no MCP SDK in the image.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from kakao_legal_bot.app.lawapi.client import LawApiClient, LawApiError  # noqa: E402

PROTOCOL_VERSION = "2024-11-05"

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_law",
        "description": "국가법령정보센터에서 현행 법령을 검색합니다 (법령명·키워드).",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "get_law_text",
        "description": "법령 본문을 조회합니다. search_law 결과의 법령일련번호를 넣으세요.",
        "inputSchema": {
            "type": "object",
            "properties": {"law_id": {"type": "string"}},
            "required": ["law_id"],
        },
    },
    {
        "name": "search_precedent",
        "description": "판례 목록을 검색합니다. 사건번호(case_no)나 법원명(court)으로 좁힐 수 있습니다.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "court": {"type": "string"},
                "case_no": {"type": "string"},
                "limit": {"type": "integer"},
            },
        },
    },
    {
        "name": "get_precedent",
        "description": "판례 본문(판시사항·판결요지·이유)을 판례일련번호로 조회합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {"prec_id": {"type": "string"}},
            "required": ["prec_id"],
        },
    },
    {
        "name": "search_ordinance",
        "description": "지방자치단체 조례·규칙(자치법규)을 검색합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_admin_rule",
        "description": "행정규칙(훈령·예규·고시)을 검색합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_legal_forms",
        "description": "법령 별표·서식을 검색합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
    {
        "name": "search_constitutional_decision",
        "description": "헌법재판소 결정례를 검색합니다.",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
        },
    },
]


async def call_tool(client: LawApiClient, name: str, arguments: dict[str, Any]) -> str:
    limit = int(arguments.get("limit") or 5)
    query = str(arguments.get("query") or "")
    try:
        if name == "search_law":
            docs = await client.search_law(query, display=limit)
        elif name == "get_law_text":
            doc = await client.get_law(law_id=str(arguments.get("law_id") or ""))
            docs = [doc] if doc else []
        elif name == "search_precedent":
            docs = await client.search_precedent(
                query,
                court=str(arguments.get("court") or ""),
                case_no=str(arguments.get("case_no") or ""),
                display=limit,
            )
        elif name == "get_precedent":
            doc = await client.get_precedent(str(arguments.get("prec_id") or ""))
            docs = [doc] if doc else []
        elif name == "search_ordinance":
            docs = await client.search_ordinance(query, display=limit)
        elif name == "search_admin_rule":
            docs = await client.search_admin_rule(query, display=limit)
        elif name == "search_legal_forms":
            docs = await client.search_forms(query, display=limit)
        elif name == "search_constitutional_decision":
            docs = await client.search_constitutional_decision(query, display=limit)
        else:
            return f"알 수 없는 도구: {name}"
    except LawApiError as exc:
        return f"조회 실패: {exc}"

    if not docs:
        return "검색 결과가 없습니다."
    return json.dumps([doc.to_dict() for doc in docs], ensure_ascii=False, indent=2)


def _response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def handle(client: LawApiClient, request: dict[str, Any]) -> dict[str, Any] | None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    if method == "initialize":
        return _response(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "korean-law", "version": "1.0.0"},
            },
        )
    if method in {"notifications/initialized", "initialized"}:
        return None  # notification — no reply
    if method == "tools/list":
        return _response(request_id, {"tools": TOOLS})
    if method == "tools/call":
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        text = await call_tool(client, name, arguments)
        return _response(request_id, {"content": [{"type": "text", "text": text}]})
    if method == "ping":
        return _response(request_id, {})
    if request_id is None:
        return None
    return _error(request_id, -32601, f"method not found: {method}")


async def serve() -> int:
    client = LawApiClient(
        oc=os.environ.get("LAW_OC", ""),
        service_key=os.environ.get("DATA_GO_KR_KEY", ""),
        timeout_s=float(os.environ.get("LAW_API_TIMEOUT_S", "12")),
    )
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except json.JSONDecodeError:
                continue
            response = await handle(client, request)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
    finally:
        await client.aclose()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(serve()))
    except KeyboardInterrupt:
        raise SystemExit(0) from None
