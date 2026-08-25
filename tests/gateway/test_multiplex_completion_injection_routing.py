"""Regression: completion injection must resolve a SECONDARY profile's adapter.

Under ``gateway.multiplex_profiles`` one gateway process serves the default
profile plus every named profile under ``~/.hermes/profiles/``. Only the
default profile's adapters live in ``GatewayRunner.adapters``; each secondary
profile's adapters live in ``_profile_adapters[profile]``.

``_inject_watch_notification`` — the single funnel every background-process and
async-delegation completion passes through — resolved its adapter from
``self.adapters`` only (the alias-aware ``resolve_delivery_transport`` call and
the legacy literal scan both read that one map). For a secondary profile that
map has no entry for the profile's platform, so resolution fell through to a
bare ``return None``: registration proven, drain proven, and then no delivery,
no synthetic turn, and no log line naming the drop.

The kanban notifier already resolves the same problem correctly via
``_adapter_for_source``/``_authorization_adapter``
(``gateway/authz_mixin.py``), which reads ``source.profile`` and fails closed
rather than replying out of the default profile's bot. Contract under test:
the injection path resolves through that same profile-aware resolver, and a
genuine miss is logged rather than swallowed.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.run import GatewayRunner


class _Adapter:
    """Minimal push-capable adapter stub."""

    def __init__(self, name):
        self.name = name
        self.handle_message = AsyncMock()


def _runner(default_adapters, profile_adapters):
    runner = object.__new__(GatewayRunner)
    runner._running = True
    runner.adapters = default_adapters
    runner._profile_adapters = profile_adapters
    runner.config = SimpleNamespace(platforms={})
    return runner


def _alpha_event():
    """A completion for the alpha profile's Matrix room (served by its own
    ``@alpha-bot:example.org`` connection), as the watcher queues it."""
    return {
        "type": "process_completed",
        "session_id": "proc_alpha_1",
        "session_key": "agent:alpha:matrix:group:!room:example.org",
        "platform": "matrix",
        "chat_type": "group",
        "chat_id": "!room:example.org",
        "status": "completed",
    }


@pytest.mark.asyncio
async def test_injection_resolves_secondary_profile_adapter():
    """The alpha profile's Matrix adapter must receive the completion, even
    though ``self.adapters`` (the default profile's map) has no Matrix entry."""
    alpha_adapter = _Adapter("matrix-alpha")
    telegram = _Adapter("telegram-default")
    runner = _runner(
        {Platform.TELEGRAM: telegram},
        {"alpha": {Platform.MATRIX: alpha_adapter}},
    )

    result = await runner._inject_watch_notification("[task finished]", _alpha_event())

    assert result is True, (
        f"injection returned {result!r} — the alpha completion was dropped at the "
        "bare `return None`, exactly the multiplex symptom this fixes"
    )
    assert alpha_adapter.handle_message.await_count == 1
    assert telegram.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_injection_does_not_leak_to_default_profile_adapter():
    """Fail closed: when the alpha profile has no Matrix adapter, the default
    profile's Matrix adapter must NOT be used — that answers out of the wrong
    bot."""
    default_matrix = _Adapter("matrix-default")
    runner = _runner({Platform.MATRIX: default_matrix}, {"alpha": {}})

    result = await runner._inject_watch_notification("[task finished]", _alpha_event())

    assert result is None
    assert default_matrix.handle_message.await_count == 0


@pytest.mark.asyncio
async def test_default_profile_injection_unchanged():
    """Control: a default-profile (``agent:main``) completion still resolves
    through ``self.adapters`` exactly as before."""
    matrix = _Adapter("matrix-default")
    runner = _runner({Platform.MATRIX: matrix}, {})
    evt = _alpha_event()
    evt["session_key"] = "agent:main:matrix:group:!room:example.org"

    result = await runner._inject_watch_notification("[task finished]", evt)

    assert result is True
    assert matrix.handle_message.await_count == 1


@pytest.mark.asyncio
async def test_unresolvable_adapter_is_logged_not_silent(caplog):
    """The drop that used to be a bare ``return None`` must name the platform
    and profile it could not resolve."""
    runner = _runner({}, {"alpha": {}})

    with caplog.at_level(logging.WARNING, logger="gateway.run"):
        result = await runner._inject_watch_notification("[x]", _alpha_event())

    assert result is None
    assert any(
        "no adapter for" in r.getMessage() and "alpha" in r.getMessage()
        for r in caplog.records
    ), f"expected a WARNING naming platform+profile, got: {[r.getMessage() for r in caplog.records]}"
