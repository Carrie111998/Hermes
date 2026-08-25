"""``--replace`` must not kill a gateway that is still booting.

The replace path terminates the incumbent named by the PID file
unconditionally: it reads ``existing_start_time`` purely as a PID-reuse guard
and never asks how old the incumbent is. So two operators (or two agent
sessions) deploying the same commit minutes apart each truncate the other's
boot — the second ``--replace`` SIGTERMs a process that has not yet bound
:8642, and the port stays dead through both attempts.

Observed 2026-08-14: PID 43052 started a ``--replace`` takeover at 20:14:26Z
and was killed at 20:15:37Z by a sibling session's ``--replace``, 66 seconds
into a boot that needs ~60-100s to bind. Neither launcher wrote a byte to its
log, so both sessions read the outage as a silent launch failure.

The watchdog already refuses to touch a ``gateway run`` younger than
``BOOT_INPROGRESS_GRACE_SECONDS`` for exactly this reason. These tests give
``--replace`` the equivalent.
"""

import os

import pytest

import gateway.run as gateway_run
import gateway.status as gateway_status
from gateway.config import GatewayConfig

INCUMBENT_PID = 424242


def test_get_process_age_seconds_measures_this_process():
    """Wall-clock age, in seconds — NOT ``get_process_start_time()``.

    That function returns Linux ``/proc`` clock-ticks-since-boot on one
    platform and psutil centiseconds-since-epoch on the others, and its
    docstring is explicit that only same-source *equality* is meaningful.
    Subtracting it from anything is a unit error, so age needs its own reader.
    """
    age = gateway_status.get_process_age_seconds(os.getpid())

    assert age is not None
    assert 0 <= age < 3600


def test_get_process_age_seconds_is_none_for_a_dead_pid():
    assert gateway_status.get_process_age_seconds(INCUMBENT_PID) is None


@pytest.fixture(autouse=True)
def _clear_grace_env(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_REPLACE_MIN_AGE_SECONDS", raising=False)


def test_booting_incumbent_blocks_replace_returns_age_when_too_young(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.get_process_age_seconds", lambda pid: 12.0
    )

    assert gateway_run._booting_incumbent_blocks_replace(INCUMBENT_PID) == 12.0


def test_settled_incumbent_does_not_block_replace(monkeypatch):
    monkeypatch.setattr(
        "gateway.status.get_process_age_seconds",
        lambda pid: gateway_run.DEFAULT_REPLACE_MIN_INCUMBENT_AGE_S + 1.0,
    )

    assert gateway_run._booting_incumbent_blocks_replace(INCUMBENT_PID) is None


def test_unknown_age_does_not_block_replace(monkeypatch):
    """Fail open. A replace is a deliberate operator act; missing telemetry
    (psutil denied, process already reaped) must never strand the gateway."""
    monkeypatch.setattr("gateway.status.get_process_age_seconds", lambda pid: None)

    assert gateway_run._booting_incumbent_blocks_replace(INCUMBENT_PID) is None


def test_grace_of_zero_disables_the_guard(monkeypatch):
    """The documented escape hatch, named in the refusal message."""
    monkeypatch.setenv("HERMES_GATEWAY_REPLACE_MIN_AGE_SECONDS", "0")
    monkeypatch.setattr("gateway.status.get_process_age_seconds", lambda pid: 0.5)

    assert gateway_run._booting_incumbent_blocks_replace(INCUMBENT_PID) is None


def test_unparsable_grace_env_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_REPLACE_MIN_AGE_SECONDS", "not-a-number")
    monkeypatch.setattr("gateway.status.get_process_age_seconds", lambda pid: 5.0)

    assert gateway_run._booting_incumbent_blocks_replace(INCUMBENT_PID) == 5.0


@pytest.mark.asyncio
async def test_start_gateway_refuses_to_replace_a_booting_incumbent(
    tmp_path, monkeypatch
):
    """The whole point: nothing reaches a mid-boot gateway.

    Not a SIGTERM, and — since the replace path now asks before it kills — not a
    graceful stop request either. A planned-stop marker would drive a mid-boot
    gateway into shutdown just as effectively as a signal would, so the guard
    has to sit in front of BOTH.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    terminated = []
    markers_written = []
    requested = []

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: INCUMBENT_PID)
    monkeypatch.setattr("gateway.status.get_process_age_seconds", lambda pid: 12.0)
    monkeypatch.setattr(
        "gateway.status.terminate_pid",
        lambda pid, force=False, reason=None: terminated.append((pid, force)),
    )
    monkeypatch.setattr(
        "gateway.status.write_takeover_marker", lambda pid: markers_written.append(pid)
    )
    monkeypatch.setattr(
        gateway_run,
        "_request_incumbent_shutdown",
        lambda pid, *, timeout: requested.append(pid) or True,
    )

    result = await gateway_run.start_gateway(
        config=GatewayConfig(), replace=True, verbosity=None
    )

    assert result is False
    assert terminated == []
    assert markers_written == []
    assert requested == [], "the boot grace must gate the graceful request too"


@pytest.mark.asyncio
async def test_start_gateway_still_replaces_a_settled_incumbent(tmp_path, monkeypatch):
    """Negative control: the guard must not break the ordinary takeover.

    Stops at the shutdown REQUEST — this asserts the guard let the replace path
    through, not that the full takeover completes.

    The stop point used to be ``terminate_pid``, because the replace path killed
    the incumbent directly. It now asks for a graceful stop first
    (``_request_incumbent_shutdown``) and only escalates to ``terminate_pid`` if
    that is refused, so the sentinel moved with it. Asserting on the old seam
    here would have silently stopped exercising anything.
    """
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    requested = []
    terminated = []

    class _Stop(Exception):
        pass

    def _request(pid, *, timeout):
        requested.append((pid, timeout))
        raise _Stop

    monkeypatch.setattr("gateway.status.get_running_pid", lambda: INCUMBENT_PID)
    monkeypatch.setattr("gateway.status.get_process_age_seconds", lambda pid: 9999.0)
    monkeypatch.setattr("gateway.status.write_takeover_marker", lambda pid: None)
    monkeypatch.setattr(
        "gateway.status.terminate_pid",
        lambda pid, force=False, reason=None: terminated.append((pid, force)),
    )
    monkeypatch.setattr(gateway_run, "_request_incumbent_shutdown", _request)

    with pytest.raises(_Stop):
        await gateway_run.start_gateway(
            config=GatewayConfig(), replace=True, verbosity=None
        )

    assert [pid for pid, _ in requested] == [INCUMBENT_PID]
    assert terminated == [], "a settled incumbent must be ASKED before it is killed"
    # And it must be granted a real drain budget, not the old hardcoded 10s.
    assert requested[0][1] > 10.0
