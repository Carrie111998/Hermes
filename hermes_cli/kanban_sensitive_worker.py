"""Sanitize all output from a sensitive Kanban worker before durable logging."""
from __future__ import annotations

import os
import subprocess
import sys

from hermes_cli.kanban_sensitive import (
    active_secret_values,
    build_sensitive_worker_env,
    redact_exact_secrets,
)


def main(argv: list[str] | None = None) -> int:
    command = list(sys.argv[1:] if argv is None else argv)
    if command[:1] == ["--"]:
        command = command[1:]
    if not command:
        return 2
    proc = subprocess.run(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=build_sensitive_worker_env(os.environ),
        check=False,
        shell=False,
    )
    sanitized = redact_exact_secrets(
        proc.stdout.decode("utf-8", errors="replace"), active_secret_values()
    )
    sys.stdout.write(sanitized)
    return int(proc.returncode)


if __name__ == "__main__":
    raise SystemExit(main())
