#!/usr/bin/env python3
"""Read-only JAY OS v1 bootstrap probe.

Collects non-secret node/tool/repo state for capacity routing and evidence.
Does not perform login, mutate config, or touch remote checkouts.
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def run(cmd: list[str], timeout: int = 8) -> dict:
    try:
        p = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        return {"cmd": cmd, "returncode": p.returncode, "stdout": p.stdout.strip(), "stderr": p.stderr.strip()}
    except Exception as exc:  # noqa: BLE001 - probe must not crash on missing tools
        return {"cmd": cmd, "error": type(exc).__name__, "message": str(exc)}


def tool(name: str) -> dict:
    path = shutil.which(name)
    out = {"name": name, "path": path}
    if path:
        version_flags = {
            "node": ["--version"],
            "npm": ["--version"],
            "pnpm": ["--version"],
            "uv": ["--version"],
            "docker": ["--version"],
            "gh": ["--version"],
            "hermes": ["--version"],
        }
        if name in version_flags:
            out["version_probe"] = run([name, *version_flags[name]], timeout=5)
    return out


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    snapshot = {
        "schema": "jay-os-v1.probe.snapshot/1",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "host": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            "user": os.environ.get("USER"),
        },
        "repo": {
            "path": str(repo),
            "branch": run(["git", "-C", str(repo), "branch", "--show-current"]),
            "head": run(["git", "-C", str(repo), "rev-parse", "--short", "HEAD"]),
            "status": run(["git", "-C", str(repo), "status", "--short"]),
            "remote": run(["git", "-C", str(repo), "remote", "get-url", "origin"]),
        },
        "tools": {name: tool(name) for name in ["tailscale", "ssh", "gh", "node", "npm", "pnpm", "uv", "docker", "hermes", "ollama"]},
        "network": {
            "tailscale_status": run(["tailscale", "status"], timeout=8) if shutil.which("tailscale") else None,
            "known_ping": {name: run(["ping", "-c", "1", "-W", "1000", name], timeout=3) for name in ["m1", "rtx", "m5"]},
        },
    }
    print(json.dumps(snapshot, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
