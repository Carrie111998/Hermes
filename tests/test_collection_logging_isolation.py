"""Regression coverage for collection-time Hermes log isolation."""

import logging
import os
import subprocess
import sys
from pathlib import Path

# Importing cli initializes centralized logging at module import time. This is
# intentionally above the test function: collection is the vulnerable phase.
import cli  # noqa: F401, E402

import hermes_logging


def test_collection_time_logging_targets_test_home_not_live_home():
    session_home = Path(os.environ["HERMES_TEST_SESSION_HOME"]).resolve()
    live_home = (Path.home() / ".hermes").resolve()
    targets = {
        Path(handler.baseFilename).resolve()
        for handler in hermes_logging.rotating_file_handlers()
    }

    assert targets
    assert all(path.is_relative_to(session_home / "logs") for path in targets)
    assert all(not path.is_relative_to(live_home / "logs") for path in targets)

    logging.getLogger(__name__).warning("S1_004_TEST_LOG_ISOLATION_CANARY")
    hermes_logging.flush_log_queue()
    assert "S1_004_TEST_LOG_ISOLATION_CANARY" in (
        session_home / "logs" / "errors.log"
    ).read_text(encoding="utf-8")


def test_test_session_home_rejects_live_home_descendants(tmp_path):
    live_home = tmp_path / "would-be-live"
    nested_test_home = live_home / "pytest"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(live_home)
    env["HERMES_TEST_SESSION_HOME"] = str(nested_test_home)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            str(Path(__file__).resolve()),
        ],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "must be disjoint from the live HERMES_HOME" in (
        result.stdout + result.stderr
    )
    assert not nested_test_home.exists()
