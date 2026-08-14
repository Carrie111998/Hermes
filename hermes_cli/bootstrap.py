"""Side-effect-free console entry that establishes policy before CLI import."""

from __future__ import annotations

import sys

from hermes_cli.bootstrap_policy import classify_argv, set_policy


def main() -> None:
    set_policy(classify_argv(sys.argv[1:]))
    from hermes_cli.main import main as cli_main

    cli_main()
