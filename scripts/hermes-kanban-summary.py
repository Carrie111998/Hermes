#!/usr/bin/env python3
"""Executable wrapper for the user-systemd Kanban summary timer."""

from hermes_cli.kanban_summary import main


if __name__ == "__main__":
    raise SystemExit(main())
