"""The webhook delivery guard: an error placeholder is not an answer.

When a turn ends without producing an answer, ``agent/conversation_loop.py``
puts a short placeholder in ``final_response`` ("Response truncated due to
output length limit", ...). Interactive surfaces render that as an error. A
webhook route hands whatever it receives to its delivery target, so before
this guard the recipient got the placeholder itself — a one-line string that
reads like a terse reply — with nothing in the logs saying the request had
produced no output.

`gateway/run.py:_sanitize_gateway_final_response` already does this for the
chat gateways and deliberately exempts `webhook` as a programmatic surface.
That holds for the route's own HTTP response and for `deliver: log`; it does
not hold for a route configured to deliver to a person, which is what the
guard here covers.

Covers:
- every placeholder is caught, and the delivered text says no answer was produced
- the original placeholder text is preserved inside the notice, not swallowed
- a WARNING naming the session and route is logged
- `deliver: log` keeps the raw diagnostic (programmatic surface, unguarded)
- ordinary responses pass through untouched
- the placeholder set has not drifted away from conversation_loop.py
"""

import asyncio
import logging
import pathlib
from unittest.mock import AsyncMock, MagicMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from gateway.platforms.webhook import (
    WebhookAdapter,
    _TERMINAL_ERROR_PLACEHOLDERS,
    _is_terminal_error_placeholder,
)

CHAT_ID = "webhook:daily-report:1785970121225"

# A plausible webhook report: an actual answer, which must pass untouched.
ORDINARY_RESPONSE = (
    "Overnight batch finished at 04:12 UTC.\n"
    "- 1,204 rows ingested, 3 rejected (schema mismatch on `order_ref`)\n"
    "- Retry queue empty\n"
    "No action needed."
)


def _make_adapter() -> WebhookAdapter:
    config = PlatformConfig(
        enabled=True, extra={"host": "127.0.0.1", "port": 0, "routes": {}}
    )
    return WebhookAdapter(config)


def _wire_target(adapter: WebhookAdapter):
    target = AsyncMock()
    target.send = AsyncMock(return_value=SendResult(success=True))
    runner = MagicMock()
    runner.adapters = {Platform("telegram"): target}
    runner.config.get_home_channel.return_value = None
    adapter.gateway_runner = runner
    return target


def _send(adapter: WebhookAdapter, content: str) -> SendResult:
    return asyncio.run(adapter.send(CHAT_ID, content))


# ---------------------------------------------------------------------------
# The classifier
# ---------------------------------------------------------------------------

def test_every_placeholder_is_classified_as_terminal():
    for placeholder in _TERMINAL_ERROR_PLACEHOLDERS:
        assert _is_terminal_error_placeholder(placeholder), placeholder
        # Surrounding whitespace must not smuggle one past the guard.
        assert _is_terminal_error_placeholder(f"\n  {placeholder}  \n")


def test_ordinary_response_is_not_classified_as_terminal():
    assert not _is_terminal_error_placeholder(ORDINARY_RESPONSE)
    assert not _is_terminal_error_placeholder("")
    assert not _is_terminal_error_placeholder(None)
    # A response that merely *quotes* a placeholder is a real answer.
    assert not _is_terminal_error_placeholder(
        "The run failed: Response truncated due to output length limit. Retrying."
    )


def test_placeholder_set_has_not_drifted_from_conversation_loop():
    """Every guarded string must still be produced by conversation_loop.py.

    Without this, a rewording upstream would silently reopen the hole: the
    guard would keep passing its own tests while no longer matching anything
    the loop emits.
    """
    source = (
        pathlib.Path(__file__).resolve().parents[2]
        / "agent"
        / "conversation_loop.py"
    ).read_text(encoding="utf-8")
    # Some are written as adjacent string literals wrapped across source
    # lines, so drop the quote characters and collapse whitespace on both
    # sides before comparing.
    flat = " ".join(source.replace('"', " ").replace("'", " ").split())
    missing = [
        p for p in _TERMINAL_ERROR_PLACEHOLDERS if " ".join(p.split()) not in flat
    ]
    assert not missing, f"no longer emitted by conversation_loop.py: {missing}"


# ---------------------------------------------------------------------------
# The delivered message
# ---------------------------------------------------------------------------

def test_placeholder_is_not_delivered_as_the_answer():
    adapter = _make_adapter()
    target = _wire_target(adapter)
    adapter._delivery_info[CHAT_ID] = {
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "-1001234567890"},
        "route": "daily-report",
    }

    placeholder = "Response truncated due to output length limit"
    result = _send(adapter, placeholder)

    assert result.success
    delivered = target.send.await_args.args[1]
    assert delivered != placeholder
    assert "No answer was produced" in delivered
    # The reason still travels — the guard explains, it does not hide.
    assert placeholder in delivered
    assert "daily-report" in delivered


def test_ordinary_response_is_delivered_untouched():
    adapter = _make_adapter()
    target = _wire_target(adapter)
    adapter._delivery_info[CHAT_ID] = {
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "-1001234567890"},
        "route": "daily-report",
    }

    assert _send(adapter, ORDINARY_RESPONSE).success
    assert target.send.await_args.args[1] == ORDINARY_RESPONSE


def test_guard_logs_a_warning_naming_the_session_and_route(caplog):
    adapter = _make_adapter()
    _wire_target(adapter)
    adapter._delivery_info[CHAT_ID] = {
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "-1001234567890"},
        "route": "daily-report",
    }

    with caplog.at_level(logging.WARNING, logger="gateway.platforms.webhook"):
        _send(adapter, "Request payload too large (413). Cannot compress further.")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings, "the guard must leave a WARNING behind"
    line = warnings[-1].getMessage()
    assert "delivery-guard" in line
    assert CHAT_ID in line
    assert "daily-report" in line


def test_guard_still_fires_when_the_route_is_unknown():
    """A delivery entry that predates the `route` key must not crash the guard."""
    adapter = _make_adapter()
    target = _wire_target(adapter)
    adapter._delivery_info[CHAT_ID] = {
        "deliver": "telegram",
        "deliver_extra": {"chat_id": "-1001234567890"},
    }

    assert _send(adapter, "Incomplete REASONING_SCRATCHPAD after 2 retries").success
    delivered = target.send.await_args.args[1]
    assert "No answer was produced" in delivered


def test_log_delivery_keeps_the_raw_diagnostic(caplog):
    """`deliver: log` is a programmatic surface: no notice, no rewrite."""
    adapter = _make_adapter()
    adapter._delivery_info[CHAT_ID] = {"deliver": "log", "route": "daily-report"}

    placeholder = "Response truncated due to output length limit"
    with caplog.at_level(logging.INFO, logger="gateway.platforms.webhook"):
        assert _send(adapter, placeholder).success

    logged = " ".join(r.getMessage() for r in caplog.records)
    assert placeholder in logged
    assert "delivery-guard" not in logged
