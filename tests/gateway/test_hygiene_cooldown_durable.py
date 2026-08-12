"""Regression coverage for durable gateway-hygiene cooldowns.

A session-hygiene compressor is constructed afresh for each inbound turn.  Its
failure cooldown must therefore survive a gateway restart; otherwise a stuck
summary worker can immediately re-enter compaction after supervision reloads
the gateway.
"""

from types import SimpleNamespace

from gateway.run import (
    _get_hygiene_failure_cooldown,
    _record_hygiene_failure_cooldown,
)


class _DB:
    def __init__(self):
        self.recorded = []
        self.durable = None

    def record_compression_failure_cooldown(self, session_id, deadline, error):
        self.recorded.append((session_id, deadline, error))
        self.durable = {"cooldown_until": deadline, "error": error}

    def get_compression_failure_cooldown(self, session_id):
        return self.durable


def test_hygiene_cooldown_is_written_to_session_db_and_memory():
    db = _DB()
    runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))

    deadline = _record_hygiene_failure_cooldown(runner, "session-1", 120)

    assert runner._hygiene_compression_failure_cooldowns["session-1"] == deadline
    assert db.recorded == [
        ("session-1", deadline, "gateway hygiene compression failed")
    ]


def test_hygiene_cooldown_is_recovered_from_session_db_after_restart():
    db = _DB()
    first_runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))
    deadline = _record_hygiene_failure_cooldown(first_runner, "session-1", 120)

    # A replacement supervised gateway has no old process-local dictionary.
    restarted_runner = SimpleNamespace(_session_db=SimpleNamespace(_db=db))

    assert _get_hygiene_failure_cooldown(restarted_runner, "session-1") == deadline


def test_hygiene_cooldown_uses_later_of_memory_and_durable_deadline():
    db = _DB()
    runner = SimpleNamespace(
        _session_db=SimpleNamespace(_db=db),
        _hygiene_compression_failure_cooldowns={"session-1": 100.0},
    )
    db.durable = {"cooldown_until": 200.0}

    assert _get_hygiene_failure_cooldown(runner, "session-1") == 200.0
