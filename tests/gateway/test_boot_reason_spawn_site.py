"""The GATEWAY_STARTED payload must carry the launcher's own spawn label.

Between 2026-08-10 21:46 and 2026-08-11 03:42 the gateway was replaced eight
times, and every one of those boots wrote ``boot_reason: "manual"`` into
``~/.hermes/events/audit.jsonl``. That is because ``_detect_boot_reason()``
*infers* the reason, and both ``hermes gateway start`` and ``hermes gateway
restart`` funnel through the same detached spawn — so inference cannot tell
them apart. The detached parent exits within seconds, so a post-hoc
``ParentProcessId`` lookup reads DEAD and cannot break the tie either.

``_spawn_detached(*, reason=...)`` already stamps ``HERMES_GATEWAY_SPAWN_SITE``
into the child environment. These tests pin the remaining hop: that carried
label has to reach ``boot_reason``, because the diag log is not the artifact an
operator reads when reconstructing a restart cluster — audit.jsonl is.
"""

from __future__ import annotations

import pathlib
import sys

import pytest

from gateway.run import GatewayRunner
from hermes_cli import gateway_diag


@pytest.fixture(autouse=True)
def _no_real_watchdog_log(monkeypatch, tmp_path):
    """Point ``Path.home()`` at an empty dir for every test in this module.

    ``_detect_boot_reason`` falls back to scanning
    ``~/.hermes/watchdog_events.jsonl`` for a restart within the last 120s.
    On the dev box that file is real, so an unstamped-launch assertion could
    read "watchdog_recovery" purely because a watchdog happened to fire while
    the suite was running.
    """
    monkeypatch.setattr(pathlib.Path, "home", classmethod(lambda cls: tmp_path))


def _boot_reason(monkeypatch, *, site=None, argv=None):
    """Evaluate _detect_boot_reason under a controlled env/argv.

    ``_detect_boot_reason`` never touches ``self``, so it is called unbound
    rather than paying for a full GatewayRunner construction.
    """
    monkeypatch.setattr(sys, "argv", argv if argv is not None else ["hermes", "gateway", "run"])
    monkeypatch.delenv(gateway_diag.SPAWN_SITE_ENV, raising=False)
    if site is not None:
        monkeypatch.setenv(gateway_diag.SPAWN_SITE_ENV, site)
    return GatewayRunner._detect_boot_reason(None)


@pytest.mark.parametrize(
    "site",
    ["cli:restart", "cli:start", "windows-task-script", "windows-startup-folder"],
)
def test_carried_spawn_site_becomes_the_boot_reason(monkeypatch, site):
    """A stamped launch reports the site that stamped it, not "manual"."""
    assert _boot_reason(monkeypatch, site=site) == site


def test_restart_and_start_are_distinguishable(monkeypatch):
    """The headline regression: these two used to be the same string."""
    restart = _boot_reason(monkeypatch, site="cli:restart")
    start = _boot_reason(monkeypatch, site="cli:start")
    assert restart != start, "restart and start must not collapse to one boot_reason"
    assert (restart, start) == ("cli:restart", "cli:start")


def test_unstamped_launch_still_falls_back_to_manual(monkeypatch):
    """A launcher that is not ours keeps the pre-existing coarse taxonomy."""
    assert _boot_reason(monkeypatch, site=None) == "manual"


def test_unspecified_placeholder_does_not_leak_into_the_payload(monkeypatch):
    """``unspecified`` means "stamped but unnamed" — it is not a boot reason.

    ``_spawn_detached`` defaults ``reason`` to this placeholder, so it reaches
    the child env on any call site that names nothing. Surfacing it as a
    boot_reason would be a downgrade from "manual": it reads like a real
    classification while carrying strictly less information.
    """
    assert _boot_reason(monkeypatch, site=gateway_diag.SPAWN_SITE_UNSPECIFIED) == "manual"


def test_blank_spawn_site_falls_back_rather_than_reporting_empty(monkeypatch):
    assert _boot_reason(monkeypatch, site="   ") == "manual"


def test_explicit_replace_outranks_the_carried_label(monkeypatch):
    """``--replace`` is an operator-chosen takeover mode, already in the taxonomy.

    A restart never passes ``--replace`` (``restart()`` drains first, then
    spawns), so this is not a live collision — it is a guard that wiring the
    carried label in cannot silently retire an existing signal.
    """
    reason = _boot_reason(
        monkeypatch,
        site="cli:restart",
        argv=["hermes", "gateway", "run", "--replace"],
    )
    assert reason == "replace"


# --- the raw label must survive even when it is not the boot_reason ---------


class _StubRunner:
    """Minimal stand-in carrying only what _build_boot_payload reads."""

    adapters: dict = {}
    _previous_shutdown_was_clean = True

    _detect_boot_reason = GatewayRunner._detect_boot_reason
    _build_boot_payload = GatewayRunner._build_boot_payload


def test_boot_payload_carries_the_raw_spawn_site_alongside_boot_reason(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
    monkeypatch.setenv(gateway_diag.SPAWN_SITE_ENV, "cli:restart")

    payload = _StubRunner()._build_boot_payload()

    assert payload["boot_reason"] == "cli:restart"
    assert payload["spawn_site"] == "cli:restart"


def test_boot_payload_records_a_null_spawn_site_for_a_foreign_launcher(monkeypatch):
    """A null spawn_site is evidence: nothing of ours launched this gateway."""
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
    monkeypatch.delenv(gateway_diag.SPAWN_SITE_ENV, raising=False)

    payload = _StubRunner()._build_boot_payload()

    assert payload["spawn_site"] is None


def test_boot_payload_keeps_raw_spawn_site_when_replace_wins(monkeypatch):
    """The two fields answer different questions and must not overwrite each other."""
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run", "--replace"])
    monkeypatch.setenv(gateway_diag.SPAWN_SITE_ENV, "cli:restart")

    payload = _StubRunner()._build_boot_payload()

    assert payload["boot_reason"] == "replace"
    assert payload["spawn_site"] == "cli:restart"


def test_boot_payload_keeps_the_fields_audit_consumers_already_read(monkeypatch):
    """Guard against the extraction dropping a pre-existing payload field."""
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
    monkeypatch.delenv(gateway_diag.SPAWN_SITE_ENV, raising=False)

    payload = _StubRunner()._build_boot_payload()

    for key in ("argv", "parent_pid", "boot_reason", "platforms_connected",
                "previous_clean_shutdown"):
        assert key in payload, f"{key} disappeared from the GATEWAY_STARTED payload"


# --- the whole chain: spawn stamp -> payload -> emitted event ----------------


def test_spawn_label_reaches_the_emitted_gateway_started_event(monkeypatch):
    """End-to-end: the stamp has to survive into what audit.jsonl is built from.

    ``boot_reason`` is only worth changing if the value lands in the event the
    audit-logger subscriber persists. Asserting on ``_detect_boot_reason``
    alone would leave the payload -> emit hop free to drop it.
    """
    import events.gateway_integration as gi

    class _RecordingBus:
        def __init__(self):
            self.events = []

        def emit(self, *, event_type, source, payload, **kwargs):
            self.events.append({"event_type": event_type, "payload": dict(payload)})
            return "1"

    bus = _RecordingBus()
    monkeypatch.setattr(gi, "_bus", bus)
    monkeypatch.setattr(gi, "_gateway_started_at_monotonic", None)
    monkeypatch.setattr(sys, "argv", ["hermes", "gateway", "run"])
    monkeypatch.setenv(gateway_diag.SPAWN_SITE_ENV, "cli:restart")

    gi.emit_gateway_started(_StubRunner()._build_boot_payload())

    assert len(bus.events) == 1
    payload = bus.events[0]["payload"]
    assert payload["boot_reason"] == "cli:restart"
    assert payload["spawn_site"] == "cli:restart"
    assert payload["pid"] > 0
