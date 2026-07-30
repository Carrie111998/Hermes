"""Credential-free preparation of the Mac Grover runtime profiles."""

from __future__ import annotations

import argparse
import platform
import shlex
import subprocess
from collections.abc import Sequence


_PREPARE = (
    ("hermes", "profile", "create", "grover-prod", "--no-alias", "--no-skills"),
    ("hermes", "profile", "create", "grover-shadow", "--no-alias", "--no-skills"),
    (
        "hermes",
        "-p",
        "grover-prod",
        "config",
        "set",
        "gateway.platforms.telegram.enabled",
        "false",
    ),
    (
        "hermes",
        "-p",
        "grover-shadow",
        "config",
        "set",
        "gateway.platforms.telegram.enabled",
        "false",
    ),
    (
        "hermes",
        "-p",
        "grover-shadow",
        "config",
        "set",
        "gateway.platforms.telegram.extra.external_effects",
        "false",
    ),
    (
        "hermes",
        "-p",
        "grover-shadow",
        "plugins",
        "enable",
        "grover-shadow-guard",
        "--no-allow-tool-override",
    ),
)


def build_profile_commands(operation: str) -> list[list[str]]:
    """Return the reviewed command sequence without reading or copying secrets."""

    if operation == "prepare":
        commands = _PREPARE
    elif operation == "cutover-prod":
        raise ValueError(
            "cutover requires grover_runtime.operations health and receipt gates"
        )
    else:
        raise ValueError(f"unknown profile operation: {operation}")
    return [list(command) for command in commands]


def run_profile_commands(commands: Sequence[Sequence[str]]) -> None:
    """Run a reviewed sequence on macOS without reusing existing profiles."""

    if platform.system() != "Darwin":
        raise RuntimeError(
            "Grover runtime profiles may only be prepared on the Mac mini"
        )
    for command in commands:
        completed = subprocess.run(
            list(command),
            check=False,
            text=True,
            capture_output=True,
        )
        if completed.returncode == 0:
            continue
        output = " ".join((completed.stdout, completed.stderr)).casefold()
        if (
            tuple(command[:3]) == ("hermes", "profile", "create")
            and "already exists" in output
        ):
            raise RuntimeError(
                "profile already exists; refusing to reuse existing credential state"
            )
        raise RuntimeError(f"profile command failed: {shlex.join(command)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare",))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    commands = build_profile_commands(args.operation)
    if args.dry_run:
        for command in commands:
            print(shlex.join(command))
        return 0
    run_profile_commands(commands)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
