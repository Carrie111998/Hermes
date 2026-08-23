import asyncio
import os

from mcp.server.fastmcp import FastMCP


server = FastMCP("legacy-fixture")


@server.tool(name="ping_echo", description="Return a deterministic pong response.")
def ping_echo() -> str:
    return "pong"


@server.tool(name="application_error", description="Raise a deterministic application error.")
def application_error() -> str:
    raise RuntimeError("fixture application error")


@server.tool(name="slow_echo", description="Return after a bounded delay.")
async def slow_echo(delay_ms: int = 5_000) -> str:
    await asyncio.sleep(delay_ms / 1_000)
    return "slow-pong"


@server.tool(name="crash_transport", description="Terminate the fixture transport.")
def crash_transport() -> str:
    os._exit(17)


if __name__ == "__main__":
    server.run()
