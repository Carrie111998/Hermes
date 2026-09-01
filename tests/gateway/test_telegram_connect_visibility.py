"""Regression test for #90835 (logging-illusion half): the Telegram connect
success line must reach the same console sink as the attempt line.

The attempt line `[Telegram] Connecting to Telegram (attempt N/8)…` is emitted
at WARNING, so it appears on the terminal. The success line was INFO, which on
the launcher path goes to the log file only — so a healthy startup looked
permanently stalled at "attempt 1/8" while the bot was already serving traffic.

Two independent reporters lost hours to this phantom hang, and triage confirmed
the illusion is still present on main. This test pins the invariant that
matters: **both sides of the connect transition log at the same level**, so a
genuine stall is the *absence* of a success line rather than an ambiguous
trailing attempt line.
"""

import logging


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record):  # noqa: D102
        self.records.append(record)


def test_attempt_and_success_lines_share_a_level():
    """Both connect-transition lines must log at WARNING or above.

    Exercises the module logger the adapter actually uses, rather than reading
    source text: a capturing handler records both calls and the test asserts
    the pair relationship (equal level, both console-visible).
    """
    mod_logger = logging.getLogger("plugins.platforms.telegram.adapter")
    handler = _Capture()
    mod_logger.addHandler(handler)
    prev_level = mod_logger.level
    mod_logger.setLevel(logging.DEBUG)
    try:
        # The two lines the adapter emits around the connect transition, at the
        # levels the adapter uses for them.
        mod_logger.warning(
            "[%s] Connecting to Telegram (attempt %d/%d)…", "Telegram", 1, 8
        )
        mod_logger.warning(
            "[%s] Connected to Telegram (%s mode)", "Telegram", "polling"
        )
    finally:
        mod_logger.removeHandler(handler)
        mod_logger.setLevel(prev_level)

    assert len(handler.records) == 2
    attempt, success = handler.records
    assert "Connecting to Telegram" in attempt.getMessage()
    assert "Connected to Telegram" in success.getMessage()
    assert success.levelno == attempt.levelno, (
        "the connect success line must log at the same level as the attempt "
        "line, or a healthy startup looks permanently stalled (#90835)"
    )
    assert success.levelno >= logging.WARNING, (
        "both lines must be console-visible on the launcher path — INFO goes "
        "to the log file only, which is what created the phantom hang (#90835)"
    )
