"""Timeouts must never be logged as the value that means "no timeout".

``%.0f`` prints any sub-second timeout as ``0``. Where 0 is the knob's own
sentinel for *unlimited* / *disabled*, that is not merely imprecise — it is
INVERTED: the operator reads "no limit was in force" at the exact moment a
limit fired. Observed live in cron on 2026-08-11 and fixed there in
``37ac04c63``; ``gateway/run.py`` carried the same defect on three knobs.

Which sites this covers, and why only these three:

* ``HERMES_AGENT_TIMEOUT`` — documented ``0 = unlimited`` in
  ``hermes_cli/config.py`` (``agent.gateway_timeout``) and implemented as
  such twice in ``run.py``: the run loop resolves ``_agent_timeout_raw > 0
  else None``, and the stale-``_running_agents`` sweep guards on
  ``_raw_stale_timeout > 0`` with a ``float("inf")`` wall-TTL. Both render
  that same value back to the operator.
* ``agent.restart_drain_timeout`` — documented ``0 = no drain, interrupt
  immediately`` (and 0 is the shipped default), rendered in the stale-systemd
  ``TimeoutStopSec`` mismatch warning.

Note the reachability nuance, which mirrors cron's: a *literal* 0 can never
reach either ``HERMES_AGENT_TIMEOUT`` message. In the run loop, 0 resolves to
``None`` and takes the unlimited branch, which never sets
``_inactivity_timeout``; in the sweep, 0 makes the wall-TTL infinite and
``_should_evict`` can never become true. The hazard is reachable only with a
*sub-second configured* limit — which is exactly what makes it dangerous,
since that is the case the operator has no other way to tell apart from
"unlimited".

The other ``%.0f``-on-a-duration sites in ``run.py`` were examined and left
alone deliberately; they are cosmetic, not inverted. See the commit message
for the enumeration.

Anchored on MESSAGE TEXT, not line numbers, so it survives edits above it. No
wall-clock/elapsed-time assertions.
"""

import sys
import time
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Minimal stubs so gateway.run imports without the telegram dependency
# (same shape as tests/gateway/test_busy_session_ack.py).
_tg = types.ModuleType("telegram")
_tg.constants = types.ModuleType("telegram.constants")
_ct = MagicMock()
_ct.SUPERGROUP = "supergroup"
_ct.GROUP = "group"
_ct.PRIVATE = "private"
_tg.constants.ChatType = _ct
sys.modules.setdefault("telegram", _tg)
sys.modules.setdefault("telegram.constants", _tg.constants)
sys.modules.setdefault("telegram.ext", types.ModuleType("telegram.ext"))

from gateway.platforms.base import (  # noqa: E402
    MessageEvent,
    MessageType,
    SessionSource,
    build_session_key,
)


def _make_event(text="hello", chat_id="123", platform_val="telegram"):
    source = SessionSource(
        platform=MagicMock(value=platform_val),
        chat_id=chat_id,
        chat_type="private",
        user_id="user1",
    )
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=source,
        message_id="msg1",
    )


def _make_runner():
    """Minimal GatewayRunner stand-in, enough to reach the staleness sweep."""
    from gateway.run import GatewayRunner

    runner = object.__new__(GatewayRunner)
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._busy_ack_ts = {}
    runner._draining = False
    runner._busy_text_mode = "interrupt"
    runner._busy_input_mode = "interrupt"
    runner.adapters = {}
    runner.config = MagicMock()
    runner.config.group_sessions_per_user = True
    runner.config.thread_sessions_per_user = False
    runner.session_store = None
    runner.hooks = MagicMock()
    runner.hooks.emit = AsyncMock()
    runner.pairing_store = MagicMock()
    runner.pairing_store.is_approved.return_value = True
    runner._is_user_authorized = lambda _source: True
    return runner


class TestStaleAgentEvictionRendersTheFractionalTimeout:
    """Behavioral: the one inverted site a unit test can reach cheaply."""

    @pytest.mark.asyncio
    async def test_eviction_log_does_not_round_the_timeout_to_the_sentinel(
        self, monkeypatch, caplog
    ):
        from gateway.run import GatewayRunner

        # 0.5s: a real, in-force inactivity bound that %.0f renders as "0s".
        monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0.5")

        runner = _make_runner()
        event = _make_event()
        sk = build_session_key(event.source)

        agent = MagicMock()
        agent.get_activity_summary.return_value = {
            "api_call_count": 3,
            "max_iterations": 60,
            "current_tool": "terminal",
            "last_activity_desc": "terminal",
            # Idle well past the 0.5s bound, so the sweep evicts. Supplied
            # directly rather than measured — nothing here waits on a clock.
            "seconds_since_activity": 120.0,
        }
        runner._running_agents[sk] = agent
        runner._running_agents_ts[sk] = time.time() - 300

        with patch.object(GatewayRunner, "_invalidate_session_run_generation"), \
             patch.object(GatewayRunner, "_release_running_agent_state"), \
             caplog.at_level("WARNING"):
            try:
                await GatewayRunner._handle_message(runner, event)
            except Exception:
                # The sweep sits early in _handle_message; whatever the rest
                # of the handler does with this half-stubbed runner is not
                # what this test is about. The log record is already emitted.
                pass

        rendered = [
            rec.getMessage()
            for rec in caplog.records
            if "Evicting stale _running_agents entry" in rec.getMessage()
        ]
        assert rendered, (
            "Expected the stale-agent eviction to be logged; got: "
            f"{[r.getMessage() for r in caplog.records]}"
        )
        line = rendered[0]
        assert "timeout: 0.5s" in line, (
            f"Timeout bound not rendered faithfully: {line!r}"
        )
        # The specific inversion: HERMES_AGENT_TIMEOUT uses 0 for UNLIMITED,
        # so this exact substring tells the operator the opposite of what
        # happened — an eviction that only a live timeout could have caused,
        # labelled with the value that means there was no timeout.
        assert "timeout: 0s" not in line, (
            f"Sub-second bound rendered as the unlimited sentinel: {line!r}"
        )


class TestNoTimeoutIsRenderedAsTheUnlimitedSentinel:
    """Source-level guard for the sibling sites.

    The run-loop inactivity message is emitted from deep inside the agent
    poll loop and the systemd warning from ``start()`` under a live
    ``INVOCATION_ID`` — neither is reachable from a unit test cheaply. So
    assert on the format strings themselves, anchored on message text.
    """

    # All three format strings are split across implicitly-concatenated
    # string literals, so the anchor and an offending %.0f need not share a
    # line — scan the anchor line plus the continuation lines that follow it.
    _WINDOW = 4

    @staticmethod
    def _source() -> str:
        import gateway.run

        return Path(gateway.run.__file__).read_text(encoding="utf-8")

    @pytest.mark.parametrize(
        "anchor",
        [
            # HERMES_AGENT_TIMEOUT (0 = unlimited), run-loop inactivity branch
            # — the direct sibling of the cron message fixed in 37ac04c63.
            "Agent idle for",
            # HERMES_AGENT_TIMEOUT again, staleness sweep (also covered
            # behaviorally above; kept here so a partial revert is caught).
            "Evicting stale _running_agents entry",
            # agent.restart_drain_timeout (0 = no drain).
            "Stale systemd unit detected",
        ],
    )
    def test_timeout_render_does_not_truncate_to_zero(self, anchor):
        lines = self._source().splitlines()
        offenders = [
            " ".join(part.strip() for part in lines[i:i + self._WINDOW])
            for i, line in enumerate(lines)
            if anchor in line
            and any("%.0f" in part for part in lines[i:i + self._WINDOW])
        ]
        assert not offenders, (
            f"{anchor!r} still renders a timeout with %.0f, which prints any "
            f"sub-second bound as the '0 = unlimited' sentinel: {offenders}"
        )
