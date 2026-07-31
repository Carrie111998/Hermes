"""
Logging configuration for Hermes Agent.

Provides a single, centralised place to configure logging for all Hermes
components (CLI, GUI, Desktop, Gateway, background jobs, etc.).

The default format is pipe‑separated, UTC‑timestamped, and includes PID/TID.
JSON output can be enabled via the HERMES_LOG_JSON environment variable.
"""

import logging
import logging.handlers
import os
import threading
from typing import Dict

from hermes_constants import get_hermes_home


class _ContextFilter(logging.Filter):
    """Adds UTC timestamp, process id, and thread id to each log record."""
    def filter(self, record: logging.LogRecord) -> bool:
        from datetime import datetime, timezone
        # UTC timestamp with milliseconds
        record.asctime_utc = datetime.fromtimestamp(
            record.created, tz=timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        record.pid = os.getpid()
        record.tid = threading.get_ident()
        return True


def _build_formatter(*, use_json: bool = False) -> logging.Formatter:
    if use_json:
        # Requires: pip install pythonjsonlogger
        from pythonjsonlogger import jsonlogger
        fmt = "%(asctime_utc)s %(levelname)s %(name)s %(pid)s %(tid)s %(message)s"
        return jsonlogger.JsonFormatter(fmt)
    else:
        fmt = "%(asctime_utc)s|%(levelname)-8s|%(name)s|%(pid)s|%(tid)s|%(message)s"
        return logging.Formatter(fmt)


def setup_logging(
    *,
    log_dir: str | None = None,
    use_json: bool = False,
    backup_count: int = 7,
    when: str = "midnight",
) -> None:
    """
    Initialise the root logger with rotating file handlers for all Hermes logs.

    Parameters
    ----------
    log_dir : str | None
        Directory for log files. Defaults to $HERMES_HOME/logs.
    use_json : bool
        If True, emit JSON Lines; otherwise pipe‑separated text.
    backup_count : int
        Number of rotated files to keep.
    when : str
        Rotation interval for TimedRotatingFileHandler (see logging docs).
    """
    if log_dir is None:
        log_dir = os.path.join(get_hermes_home(), "logs")
    os.makedirs(log_dir, exist_ok=True)

    formatter = _build_formatter(use_json=use_json)

    # Define the log files we want to rotate
    log_files: Dict[str, str] = {
        "agent": "agent.log",
        "error": "errors.log",
        "gui": "gui.log",
        "desktop": "desktop.log",          # <-- desktop log entry
        "dashboard_auth": "dashboard-auth.log",
        "gateway": "gateway.log",
        "action_backup": "action-backup.log",
        "action_curator_run": "action-curator-run.log",
        "action_prompt_size": "action-prompt-size.log",
        "action_skills_update": "action-skills-update.log",
        # Add more as needed
    }

    handlers = {}
    for name, filename in log_files.items():
        path = os.path.join(log_dir, filename)
        handler = logging.handlers.TimedRotatingFileHandler(
            path,
            when=when,
            backupCount=backup_count,
            encoding="utf-8",
            utc=True,
        )
        handler.setFormatter(formatter)
        handler.addFilter(_ContextFilter())
        handlers[name] = handler

    # Configure the root logger
    logging.root.setLevel(logging.INFO)
    logging.root.handlers.clear()  # Remove any existing basicConfig handlers
    for h in handlers.values():
        logging.root.addHandler(h)

    # Optional console echo (useful during development)
    if os.getenv("HERMES_LOG_TO_CONSOLE", "").lower() in ("1", "true", "yes"):
        console = logging.StreamHandler()
        console.setFormatter(formatter)
        console.addFilter(_ContextFilter())
        logging.root.addHandler(console)


if __name__ == "__main__":
    # Quick test: python -m hermes_cli.logging_setup --json
    import argparse
    parser = argparse.ArgumentParser(description="Test Hermes logging setup")
    parser.add_argument("--json", action="store_true", help="Emit JSON Lines")
    args = parser.parse_args()
    setup_logging(use_json=args.json)
    logging.info("Logging test – hello from Hermes!")