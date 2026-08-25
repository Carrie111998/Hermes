"""``--replace`` must ask the incumbent to stop, not execute it on the spot.

THE DEFECT. ``start_gateway``'s replace path wrote a takeover marker whose own
comment says it exists "so the target's shutdown handler recognises its SIGTERM
as a planned takeover", then called ``terminate_pid(pid, force=False)``. Both
halves are POSIX reasoning:

  * ``terminate_pid`` skips its ``taskkill`` branch when ``force=False`` and
    falls through to ``os.kill(pid, SIGTERM)`` -- and CPython on Windows routes
    every signal except CTRL_C_EVENT/CTRL_BREAK_EVENT through
    ``TerminateProcess``. So the "graceful" path was an unblockable kill: no
    handler, no ``atexit``, no drain. ``gateway/status.py::pid_exists`` already
    documents this hazard for ``os.kill(pid, 0)``; the SIGTERM branch was never
    reconciled with it.
  * the shutdown handler that reads the marker therefore never ran. Measured
    2026-08-24: all four replaced gateways wrote ZERO records to
    ``logs/gateway-exit-diag.log`` after their kill, against a same-log control
    of 627 ``atexit.hook`` and 42 ``gateway.exit_clean`` records.

The cost landed on cron. ``_drain_active_agents`` already waits on
``_active_cron_job_count()`` (#60432) -- the quiesce-and-drain everyone wants is
BUILT and TESTED. It simply never ran on a replace, because the incumbent was
dead before it could start draining. 39 executions across the ledger died that
way, 405 minutes of work, and recovery can only record "whether the side effects
ran is unknown".

Windows already has a working graceful channel: ``write_planned_stop_marker`` +
the in-process watcher in ``run.py``, which translates the marker into the same
shutdown-handler invocation a real SIGTERM produces on POSIX. ``hermes gateway
stop`` uses it. ``--replace`` did not.

These tests pin the request-then-escalate contract.
"""

import pytest

import gateway.run as gateway_run


INCUMBENT_PID = 424242


# --------------------------------------------------------------------------
# The budget
# --------------------------------------------------------------------------

def test_replace_drain_timeout_outlasts_the_gateways_own_drain_budget():
    """The old replace path waited a hardcoded 10s.

    That is shorter than the incumbent's own ``restart_drain_timeout`` (60s by
    default), so even a working graceful request would have been force-killed
    mid-drain -- the same misordered-budget bug ``_windows_stop_drain_timeout``
    was written to fix on the ``stop`` path. A replace must grant at least as
    long as the thing it is asking to shut down believes it has.
    """
    from hermes_cli.gateway import _get_restart_drain_timeout

    granted = gateway_run._replace_drain_timeout()
    own_budget = float(_get_restart_drain_timeout() or 30.0)

    assert granted >= own_budget, (
        f"replace grants {granted}s but the incumbent drains for {own_budget}s"
    )
    assert granted > 10.0, "10s was the old hardcoded value the defect shipped"


def test_replace_drain_timeout_scales_with_a_configured_budget(monkeypatch):
    """Falsifiable version of the assertion above.

    On a box where ``restart_drain_timeout`` is unset it reads 0.0, which makes
    a bare ``granted >= own_budget`` trivially true — a green test proving
    nothing. Force a real budget through the FALLBACK path and pin that the
    grant actually tracks it.
    """
    monkeypatch.setattr(
        "hermes_cli.gateway_windows._windows_stop_drain_timeout",
        lambda: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    monkeypatch.setattr("hermes_cli.gateway._get_restart_drain_timeout", lambda: 60.0)

    granted = gateway_run._replace_drain_timeout()

    assert granted >= 60.0, (
        f"a 60s incumbent budget must not be truncated to {granted}s — "
        "truncation is the mid-drain kill this fix exists to stop"
    )
    assert granted <= gateway_run._REPLACE_DRAIN_CEILING_S


def test_replace_drain_timeout_survives_a_broken_budget_lookup(monkeypatch):
    """Both budget sources failing must still yield a usable, bounded grant —
    never 0, which would make every replace an instant hard kill."""
    monkeypatch.setattr(
        "hermes_cli.gateway_windows._windows_stop_drain_timeout",
        lambda: (_ for _ in ()).throw(RuntimeError("nope")),
    )
    monkeypatch.setattr(
        "hermes_cli.gateway._get_restart_drain_timeout",
        lambda: (_ for _ in ()).throw(RuntimeError("nope")),
    )

    granted = gateway_run._replace_drain_timeout()

    assert granted >= 15.0, "a failed lookup must not collapse the grace to ~0"
    assert granted <= gateway_run._REPLACE_DRAIN_CEILING_S


def test_replace_drain_timeout_is_bounded():
    """A misconfigured budget must not hang a replace forever."""
    assert gateway_run._replace_drain_timeout() <= 600.0


# --------------------------------------------------------------------------
# Windows: marker, never a signal
# --------------------------------------------------------------------------

def test_windows_requests_shutdown_via_the_planned_stop_marker(monkeypatch):
    """On Windows the request MUST go through the marker channel.

    os.kill/SIGTERM is TerminateProcess here, so reaching terminate_pid at all
    on the request path means the incumbent dies without draining.
    """
    calls = {"drain": [], "terminate": []}

    monkeypatch.setattr(gateway_run, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(
        gateway_run, "_drain_incumbent_via_marker",
        lambda pid, timeout: calls["drain"].append((pid, timeout)) or True,
        raising=False,
    )
    monkeypatch.setattr(
        gateway_run, "_terminate_incumbent",
        lambda pid, *, force=False: calls["terminate"].append((pid, force)),
        raising=False,
    )

    exited = gateway_run._request_incumbent_shutdown(INCUMBENT_PID, timeout=42.0)

    assert exited is True
    assert calls["drain"] == [(INCUMBENT_PID, 42.0)]
    assert calls["terminate"] == [], (
        "reaching terminate_pid on the Windows request path is the defect: "
        "os.kill(SIGTERM) is TerminateProcess and skips the drain"
    )


def test_windows_reports_failure_when_the_incumbent_will_not_drain(monkeypatch):
    """A refusal to drain must be reported, not silently swallowed.

    The caller escalates to a force-kill on False. Returning True here would
    leave two live gateways fighting over the same token (#19471).
    """
    monkeypatch.setattr(gateway_run, "_IS_WINDOWS", True, raising=False)
    monkeypatch.setattr(
        gateway_run, "_drain_incumbent_via_marker",
        lambda pid, timeout: False, raising=False,
    )

    assert gateway_run._request_incumbent_shutdown(INCUMBENT_PID, timeout=1.0) is False


# --------------------------------------------------------------------------
# POSIX: a real signal still works there
# --------------------------------------------------------------------------

def test_posix_still_sends_a_real_sigterm(monkeypatch):
    """POSIX signal delivery is genuine, so keep it -- and keep the marker
    handshake meaningful there."""
    sent = []

    monkeypatch.setattr(gateway_run, "_IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(
        gateway_run, "_terminate_incumbent",
        lambda pid, *, force=False: sent.append((pid, force)), raising=False,
    )
    monkeypatch.setattr(gateway_run, "_wait_for_pid_exit", lambda pid, timeout: True,
                        raising=False)

    assert gateway_run._request_incumbent_shutdown(INCUMBENT_PID, timeout=5.0) is True
    assert sent == [(INCUMBENT_PID, False)], "POSIX must get a real SIGTERM, unforced"


def test_posix_already_gone_counts_as_exited(monkeypatch):
    """ProcessLookupError means the incumbent beat us to it -- not a failure."""
    def _boom(pid, *, force=False):
        raise ProcessLookupError

    monkeypatch.setattr(gateway_run, "_IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(gateway_run, "_terminate_incumbent", _boom, raising=False)

    assert gateway_run._request_incumbent_shutdown(INCUMBENT_PID, timeout=5.0) is True


def test_permission_error_propagates(monkeypatch):
    """Cannot-signal is a real failure; start_gateway aborts the replacement
    rather than starting a second gateway."""
    def _denied(pid, *, force=False):
        raise PermissionError("nope")

    monkeypatch.setattr(gateway_run, "_IS_WINDOWS", False, raising=False)
    monkeypatch.setattr(gateway_run, "_terminate_incumbent", _denied, raising=False)

    with pytest.raises((PermissionError, OSError)):
        gateway_run._request_incumbent_shutdown(INCUMBENT_PID, timeout=5.0)


# --------------------------------------------------------------------------
# The marker helper itself
# --------------------------------------------------------------------------

def test_marker_drain_writes_the_marker_then_waits(monkeypatch):
    order = []

    monkeypatch.setattr(
        "gateway.status.write_planned_stop_marker",
        lambda pid: order.append(("write", pid)) or True,
    )
    monkeypatch.setattr(
        "gateway.status._pid_exists",
        lambda pid: order.append(("poll", pid)) is not None and False,
    )

    assert gateway_run._drain_incumbent_via_marker(INCUMBENT_PID, 5.0) is True
    assert order[0] == ("write", INCUMBENT_PID), "marker must precede the wait"
    assert ("poll", INCUMBENT_PID) in order


def test_marker_drain_times_out_when_the_pid_never_exits(monkeypatch):
    monkeypatch.setattr("gateway.status.write_planned_stop_marker", lambda pid: True)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: True)

    assert gateway_run._drain_incumbent_via_marker(INCUMBENT_PID, 0.6) is False
