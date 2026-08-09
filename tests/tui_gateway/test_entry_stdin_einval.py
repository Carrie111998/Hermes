"""Regression tests: ``_read_stdin_line`` recovers from Windows EINVAL.

On Windows, ``sys.stdin.readline()`` can raise ``OSError: [Errno 22]
Invalid argument`` when the console/pipe state is disturbed. This used to
crash the stdio TUI gateway child ("gateway exited — recovering your
session"); the helper must retry transient failures and give up cleanly
when a broken stdin exceeds the recovery budget.
"""

import sys
import time as _time
from unittest import mock

from tui_gateway import entry


def _fake_time_module():
    """time module twin with a no-op ``sleep`` so tests don't actually wait."""
    class FakeTime:
        @staticmethod
        def time():
            return _time.time()

        @staticmethod
        def sleep(_seconds):
            return None

    return FakeTime()


def test_reads_line_normally(monkeypatch):
    monkeypatch.setattr(entry, "time", _fake_time_module())
    monkeypatch.setattr(sys, "stdin", mock.Mock())
    entry.sys.stdin.readline.return_value = '{"jsonrpc":"2.0"}\n'

    assert entry._read_stdin_line([], lambda _m: None) == '{"jsonrpc":"2.0"}\n'


def test_recovers_after_transient_oserror(monkeypatch):
    monkeypatch.setattr(entry, "time", _fake_time_module())
    monkeypatch.setattr(sys, "stdin", mock.Mock())
    logs = []
    entry.sys.stdin.readline.side_effect = [
        OSError(22, "Invalid argument"),
        '{"ok":true}\n',
    ]

    assert entry._read_stdin_line([], logs.append) == '{"ok":true}\n'
    assert any("retrying" in m for m in logs)


def test_gives_up_after_repeated_oserror(monkeypatch):
    monkeypatch.setattr(entry, "time", _fake_time_module())
    monkeypatch.setattr(sys, "stdin", mock.Mock())
    logs = []
    entry.sys.stdin.readline.side_effect = OSError(22, "Invalid argument")

    # 11 consecutive failures exceed MAX_RECOVERIES_PER_MINUTE (10).
    assert entry._read_stdin_line([], logs.append) is None
    assert any("giving up" in m for m in logs)


def test_recovery_budget_shared_with_spurious_eof(monkeypatch):
    monkeypatch.setattr(entry, "time", _fake_time_module())
    monkeypatch.setattr(sys, "stdin", mock.Mock())
    logs = []
    # A window already holding 10 recent recoveries (e.g. from
    # handle_spurious_eof) must make the next OSError give up immediately.
    now = _time.time()
    recovery = [now - i for i in range(10)]
    entry.sys.stdin.readline.side_effect = OSError(22, "Invalid argument")

    assert entry._read_stdin_line(recovery, logs.append) is None
