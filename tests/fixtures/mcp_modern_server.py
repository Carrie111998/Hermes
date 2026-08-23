import asyncio
import json
import os
from pathlib import Path
import sys
import threading

from mcp.server import MCPServer


server = MCPServer("modern-fixture")


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


class CaptureMiddleware:
    def __init__(self, app, path: Path) -> None:
        self.app = app
        self.path = path
        self.lock = threading.Lock()

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        messages = []
        body = bytearray()
        while True:
            message = await receive()
            messages.append(message)
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                break

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        try:
            payload = json.loads(bytes(body) or b"{}")
        except (TypeError, ValueError):
            payload = {}
        record = {
            "method": payload.get("method"),
            "session_id": headers.get("mcp-session-id"),
        }
        with self.lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(record, sort_keys=True) + "\n")

        index = 0

        async def replay():
            nonlocal index
            if index < len(messages):
                message = messages[index]
                index += 1
                return message
            return {"type": "http.disconnect"}

        await self.app(scope, replay, send)


def main() -> int:
    if "--http" not in sys.argv:
        server.run()
        return 0

    import uvicorn

    port = int(sys.argv[sys.argv.index("--http") + 1])
    capture = Path(sys.argv[sys.argv.index("--capture") + 1])
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        host="127.0.0.1",
    )
    uvicorn.run(
        CaptureMiddleware(app, capture),
        host="127.0.0.1",
        port=port,
        log_level="critical",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
