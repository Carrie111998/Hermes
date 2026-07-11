"""Console entry point for the standalone product API."""
from __future__ import annotations

import argparse

import uvicorn


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="interfaze-api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true")
    args = parser.parse_args(argv)
    uvicorn.run("server.app:app_factory", factory=True, host=args.host,
                port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()

