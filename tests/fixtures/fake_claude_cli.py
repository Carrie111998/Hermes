"""Deterministic Claude CLI fixture used by subprocess integration tests."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path


def _emit(payload):
    print(json.dumps(payload), flush=True)


args = sys.argv[1:]
mode = os.environ.get("FAKE_CLAUDE_MODE", "success")

if args == ["--version"]:
    print("2.1.220 (Claude Code)", flush=True)
    raise SystemExit(0)

if args[:2] == ["auth", "status"]:
    _emit(
        {
            "loggedIn": mode != "logged-out",
            "authMethod": "claude.ai",
            "apiProvider": "firstParty",
            "subscriptionType": "max",
        }
    )
    raise SystemExit(0)

prompt = sys.stdin.read()
log_path = os.environ.get("FAKE_CLAUDE_LOG")
if log_path:
    record = {
        "argv": args,
        "prompt": prompt,
        "environment": {
            key: os.environ.get(key)
            for key in (
                "ANTHROPIC_API_KEY",
                "ANTHROPIC_TOKEN",
                "CLAUDE_CODE_OAUTH_TOKEN",
                "OPENAI_API_KEY",
                "PATH",
            )
        },
    }
    path = Path(log_path)
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    existing.append(record)
    path.write_text(json.dumps(existing), encoding="utf-8")

if mode == "timeout":
    time.sleep(30)
elif mode == "auth-error":
    print("Please run /login. Authentication required.", file=sys.stderr, flush=True)
    raise SystemExit(1)
elif mode == "quota":
    print("You've hit your usage limit. Try again later.", file=sys.stderr, flush=True)
    raise SystemExit(1)
elif mode == "stale-session":
    print("No conversation found with session ID", file=sys.stderr, flush=True)
    raise SystemExit(1)
elif mode == "execution-error":
    print("unexpected child failure", file=sys.stderr, flush=True)
    raise SystemExit(7)
elif mode == "malformed":
    print("{not-json", flush=True)
    raise SystemExit(0)

session_id = "11111111-1111-4111-8111-111111111111"
if "--session-id" in args:
    session_id = args[args.index("--session-id") + 1]
elif "--resume" in args:
    session_id = args[args.index("--resume") + 1]

if mode == "dump-env":
    secret_names = (
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "OPENAI_API_KEY",
    )
    text = (
        "secrets-absent"
        if all(not os.environ.get(name) for name in secret_names)
        else "secret-leaked"
    )
else:
    text = "ok"

decision = {"kind": "final", "text": text}
_emit(
    {
        "type": "result",
        "subtype": "success",
        "session_id": session_id,
        "result": json.dumps(decision),
        "structured_output": decision,
        "model": "claude-opus-5",
    }
)
