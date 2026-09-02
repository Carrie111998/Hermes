"""Birth-record logging for gateway approval prompts (#91980).

The BLOCKED outcome only lands in logs after the full approval timeout
window, and a notify write onto a dead client transport drops silently —
so a delivery-time failure used to leave no trace at all and look
identical to user silence. ``_await_gateway_decision`` now logs every
prompt raise at INFO level (request_id / session / patterns / surface)
before the notify fires; the request_id joins birth → ack → outcome, and
a notify failure escalates to WARNING carrying the same id.
"""

import logging

from tools import approval as mod


def _resolve_now(_data):
    """Simulate the user answering from inside the notify callback."""
    with mod._lock:
        queue = mod._gateway_queues.get(_resolve_now.session_key, [])
        for entry in queue:
            entry.result = "once"
            entry.event.set()


def _await_with_immediate_reply(session_key, command, pattern_keys=None):
    _resolve_now.session_key = session_key
    return mod._await_gateway_decision(
        session_key,
        _resolve_now,
        {
            "command": command,
            "description": "needs user consent",
            "pattern_key": "shell.dangerous",
            "pattern_keys": pattern_keys or ["shell.dangerous"],
        },
        surface="tui_gateway",
    )


def test_prompt_raise_logs_birth_record(caplog):
    with caplog.at_level(logging.INFO, logger="tools.approval"):
        decision = _await_with_immediate_reply("birth-log-sess", "rm -rf /tmp/x")

    assert decision["resolved"] is True
    records = [r for r in caplog.records if "Approval prompt raised" in r.getMessage()]
    assert records, "expected a birth record at raise time"
    record = records[0]
    assert record.levelno == logging.INFO, "a routine prompt is not a warning"
    line = record.getMessage()
    assert "request_id=" in line
    assert "session=birth-log-sess" in line
    assert "patterns=shell.dangerous" in line
    assert "surface=tui_gateway" in line


def test_birth_record_lists_every_matching_pattern(caplog):
    # Several patterns may match one command; the audit trail must show the
    # full ruleset in play, not just the primary key.
    with caplog.at_level(logging.INFO, logger="tools.approval"):
        _await_with_immediate_reply(
            "birth-log-multi",
            "rm -rf /tmp/x",
            pattern_keys=["shell.dangerous", "fs.wipe"],
        )

    records = [r for r in caplog.records if "Approval prompt raised" in r.getMessage()]
    assert records
    line = records[0].getMessage()
    assert "patterns=shell.dangerous,fs.wipe" in line


def test_notify_failure_birth_record_precedes_and_joins(caplog):
    # The delivery failure is the anomaly this fix targets: the birth record
    # must land BEFORE the failure trace, and both must carry the same
    # request_id so one grep reconstructs raise → drop.
    def exploding_notify(_data):
        raise RuntimeError("dead transport")

    with caplog.at_level(logging.INFO, logger="tools.approval"):
        decision = mod._await_gateway_decision(
            "birth-log-drop",
            exploding_notify,
            {
                "command": "rm -rf /tmp/x",
                "description": "needs user consent",
                "pattern_key": "shell.dangerous",
                "pattern_keys": ["shell.dangerous"],
            },
            surface="tui_gateway",
        )

    assert decision["resolved"] is False
    births = [r for r in caplog.records if "Approval prompt raised" in r.getMessage()]
    failures = [r for r in caplog.records if "notify failed" in r.getMessage()]
    assert births and failures
    assert births[0].levelno == logging.INFO
    assert failures[0].levelno == logging.WARNING
    # Ordering: the whole point of the fix — birth precedes the failure.
    assert births[0].created <= failures[0].created
    # Joinability: same request_id on both lines.
    birth_id = births[0].getMessage().split("request_id=")[1].split(" ")[0]
    assert f"request_id={birth_id}" in failures[0].getMessage()


def test_birth_record_omits_raw_command(caplog):
    # The approval payload carries the RAW command (redaction happens in the
    # transport); the birth record must not echo it into logs.
    with caplog.at_level(logging.INFO, logger="tools.approval"):
        _await_with_immediate_reply("birth-log-nocmd", "secret-token-command")

    raise_lines = [
        r for r in caplog.records if "Approval prompt raised" in r.getMessage()
    ]
    assert raise_lines
    assert "secret-token-command" not in raise_lines[0].getMessage()
