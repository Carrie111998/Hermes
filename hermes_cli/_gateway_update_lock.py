"""``hermes_cli/_gateway_update_lock.py`` — Subprocess-callable gateway pause/resume.

Invoked by the Desktop Electron app during the update preflight::

    venv\\Scripts\\python.exe -m hermes_cli._gateway_update_lock pause
    venv\\Scripts\\python.exe -m hermes_cli._gateway_update_lock resume <json-token>

``pause`` prints one JSON document to stdout — the resume token — and exits 0.
``resume`` deserialises the token from argv and calls the resume function.

Exit codes:
  0 — success
  1 — probe failure (gateway pause/resume helpers unavailable)
  2 — malformed resume token
"""

from __future__ import annotations

import json
import sys
from typing import NoReturn


def _emit_usage() -> NoReturn:
    print(f"Usage: {sys.argv[0]} pause|resume <token>", file=sys.stderr)
    sys.exit(1)


def _cmd_pause() -> NoReturn:
    """Pause gateways, print JSON resume token to stdout, exit 0."""
    try:
        from hermes_cli.update_cmd import (  # noqa: PLC0415
            _pause_windows_gateways_for_update,
        )

        token = _pause_windows_gateways_for_update()
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        print(f"pause failed: {exc}", file=sys.stderr)
        sys.exit(1)

    # Serialise the token; None → None so the caller can distinguish
    # "no gateways running" from "probe failure".
    payload = token if token is not None else None
    print(json.dumps({"ok": True, "token": payload}))
    sys.exit(0)


def _cmd_resume() -> NoReturn:
    """Resume gateways from a JSON token passed as argv[2]."""
    if len(sys.argv) < 3:
        _emit_usage()

    raw = sys.argv[2]

    try:
        token = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"resume failed: malformed token JSON: {exc}", file=sys.stderr)
        sys.exit(2)

    if not isinstance(token, dict):
        print("resume failed: token must be a JSON object", file=sys.stderr)
        sys.exit(2)

    try:
        from hermes_cli.update_cmd import (  # noqa: PLC0415
            _resume_windows_gateways_after_update,
        )

        _resume_windows_gateways_after_update(token)
    except Exception as exc:
        print(f"resume failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps({"ok": True}))
    sys.exit(0)


def main() -> None:
    if len(sys.argv) < 2:
        _emit_usage()

    command = sys.argv[1]

    if command == "pause":
        _cmd_pause()
    elif command == "resume":
        _cmd_resume()
    else:
        _emit_usage()


if __name__ == "__main__":
    main()
