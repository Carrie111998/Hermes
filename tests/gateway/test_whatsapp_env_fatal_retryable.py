"""The environment fatals in ``WhatsAppAdapter.connect()`` must be retryable.

Residual half of the 2026-08-11 outage fixed in 75c408f54.  That commit fixed
the *probe* (``node --version`` under a 5s budget on a loaded box reported a
healthy v24.14.0 as missing).  It left ``retryable=False`` alone, which is what
actually turned a 6-second blip into WhatsApp being down from 23:21 until a
manual gateway restart: ``_platform_reconnect_watcher`` drops non-retryable
platforms from the retry queue on sight.

Every observed occurrence of ``whatsapp_node_missing`` in the gateway log
(2026-06-12, 2026-08-11 01:40, 2026-08-11 23:23) was a transient, never an
install change — so the flag is bounded-retryable now, not permanent.

These tests assert on the FLAG passed to ``_set_fatal_error``, never on elapsed
time or on the number of seconds a retry took: a wall-clock assertion passes on
the broken code whenever the box happens to be idle.
"""

import asyncio
from pathlib import Path

import pytest

from plugins.platforms.whatsapp import adapter as wa


@pytest.fixture(autouse=True)
def _reset_ceiling():
    """The attempt counter is module-level, so tests must not leak into each other."""
    wa._reset_env_fatal_attempts()
    yield
    wa._reset_env_fatal_attempts()


class _StubAdapter:
    """Minimal stand-in that records the fatal instead of touching runtime status."""

    name = "whatsapp"

    def __init__(self, bridge_script: str = "/nonexistent/bridge.js"):
        self._bridge_script = bridge_script
        self.fatals = []

    def _set_fatal_error(self, code, message, *, retryable):
        self.fatals.append((code, retryable))

    # connect() is called unbound against this stub, so only the attributes it
    # reaches before the two environment checks need to exist.
    connect = wa.WhatsAppAdapter.connect


def _connect(adapter) -> bool:
    return asyncio.run(_StubAdapter.connect(adapter))


def test_node_missing_fatal_is_retryable(monkeypatch):
    """A missing Node must leave WhatsApp in the reconnect queue, not fatal."""
    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: False)
    adapter = _StubAdapter()

    assert _connect(adapter) is False
    assert adapter.fatals == [("whatsapp_node_missing", True)]


def test_bridge_missing_fatal_is_retryable(monkeypatch, tmp_path):
    """Same for the sibling fatal — the bridge tree can be absent mid-install."""
    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: True)
    adapter = _StubAdapter(str(tmp_path / "does-not-exist" / "bridge.js"))

    assert _connect(adapter) is False
    assert adapter.fatals == [("whatsapp_bridge_missing", True)]


def test_env_fatal_becomes_non_retryable_at_the_ceiling(monkeypatch):
    """Bounded, not infinite: a genuinely absent Node settles into 'fatal'.

    Asserts the ceiling is enforced by counting fatals, not by waiting.
    """
    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: False)

    flags = []
    for _ in range(wa._ENV_FATAL_RETRY_CEILING + 2):
        adapter = _StubAdapter()
        _connect(adapter)
        flags.append(adapter.fatals[0][1])

    assert flags[: wa._ENV_FATAL_RETRY_CEILING] == [True] * wa._ENV_FATAL_RETRY_CEILING
    assert flags[wa._ENV_FATAL_RETRY_CEILING:] == [False, False]


def test_ceiling_counter_is_shared_across_adapter_instances(monkeypatch):
    """The watcher builds a NEW adapter per attempt; a per-instance counter
    would reset every round and the ceiling would never be reached."""
    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: False)

    first, second = _StubAdapter(), _StubAdapter()
    _connect(first)
    _connect(second)

    assert wa._env_fatal_attempts == 2


def test_passing_env_checks_resets_the_ceiling(monkeypatch, tmp_path):
    """A recovered environment restores a full retry budget for the next outage."""
    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: False)
    for _ in range(3):
        _connect(_StubAdapter())
    assert wa._env_fatal_attempts == 3

    # Now both preconditions hold; connect() proceeds past them (and fails
    # later, on the stub's missing attributes — that is fine, the reset
    # happens before anything else runs).
    bridge = tmp_path / "bridge.js"
    bridge.write_text("// stub", encoding="utf-8")
    monkeypatch.setattr(wa, "check_whatsapp_requirements", lambda: True)
    with pytest.raises(AttributeError):
        _connect(_StubAdapter(str(bridge)))

    assert wa._env_fatal_attempts == 0


def test_not_paired_fatal_stays_non_retryable(monkeypatch, tmp_path):
    """The contrast case: pairing needs a human at a QR code.

    Retrying cannot change it, so that fatal must stay permanent — the flip
    above is specific to conditions the host can fix underneath a running
    gateway.
    """
    source = Path(wa.__file__).read_text(encoding="utf-8")
    marker = '"whatsapp_not_paired",'
    assert marker in source, "whatsapp_not_paired fatal was renamed or removed"
    # Window, not same-line: the code/message/retryable kwargs are on separate
    # lines, and the message itself is sometimes a split literal.
    window = source.split(marker, 1)[1][:400]
    assert "retryable=False" in window, (
        "whatsapp_not_paired must remain non-retryable — pairing needs a human"
    )
