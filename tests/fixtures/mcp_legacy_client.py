import asyncio
import json
import sys

from mcp import ClientSession, StdioServerParameters, stdio_client


def _is_error(result) -> bool:
    return bool(
        getattr(result, "is_error", False)
        or getattr(result, "isError", False)
    )


def _text(result) -> str:
    return "".join(
        getattr(block, "text", "") or ""
        for block in (getattr(result, "content", None) or [])
    )


async def _run(tool_name: str, arguments: dict, server_argv: list[str]) -> dict:
    params = StdioServerParameters(
        command=server_argv[0],
        args=server_argv[1:],
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            initialized = await session.initialize()
            tools = await session.list_tools()
            call = await session.call_tool(tool_name, arguments=arguments)
            missing_error = None
            try:
                missing = await session.call_tool(
                    "__missing_protocol_probe__",
                    arguments={},
                )
                missing_error = _is_error(missing)
            except Exception as exc:
                missing_error = type(exc).__name__
            return {
                "protocol_version": (
                    getattr(initialized, "protocol_version", None)
                    or getattr(initialized, "protocolVersion", None)
                    or getattr(session, "protocol_version", None)
                ),
                "tools": sorted(tool.name for tool in tools.tools),
                "call_error": _is_error(call),
                "call_text": _text(call),
                "missing_error": missing_error,
            }


def main() -> int:
    if len(sys.argv) < 4:
        sys.stderr.write(
            "usage: mcp_legacy_client.py <tool_name> <arguments-json> <server-command...>\n"
        )
        return 2
    try:
        result = asyncio.run(
            _run(
                sys.argv[1],
                json.loads(sys.argv[2]),
                sys.argv[3:],
            )
        )
    except Exception as exc:
        sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
        return 1
    sys.stdout.write(json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
