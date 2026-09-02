"""Regression tests for Desktop-owned ``hermes serve`` lifecycle tracking."""

from hermes_cli.web_server import _is_serve_orphaned, _valid_parent_start_marker


def test_parent_watchdog_tracks_recorded_desktop_pid_not_immediate_ppid():
    """Windows venv launch shims must not make a live Desktop look orphaned."""

    assert _is_serve_orphaned(4242, pid_exists=lambda pid: pid == 4242) is False
    assert _is_serve_orphaned(4242, pid_exists=lambda _pid: False) is True


def test_parent_watchdog_fails_safe_when_liveness_probe_errors():
    def broken_probe(_pid: int) -> bool:
        raise OSError("process table temporarily unavailable")

    assert _is_serve_orphaned(4242, pid_exists=broken_probe) is False


def test_parent_watchdog_accepts_electron_windows_creation_time_marker():
    unix_ms = 1_723_456_789_123
    dotnet_ticks = 621_355_968_000_000_000 + unix_ms * 10_000 + 9_999

    assert _valid_parent_start_marker(f"winms:{unix_ms}") is True
    assert (
        _is_serve_orphaned(
            4242,
            f"winms:{unix_ms}",
            process_start_marker=lambda _pid: f"win:{dotnet_ticks}",
        )
        is False
    )


def test_parent_watchdog_rejects_reused_pid_with_different_windows_creation_time():
    unix_ms = 1_723_456_789_123
    next_process_ticks = 621_355_968_000_000_000 + (unix_ms + 1) * 10_000

    assert (
        _is_serve_orphaned(
            4242,
            f"winms:{unix_ms}",
            process_start_marker=lambda _pid: f"win:{next_process_ticks}",
        )
        is True
    )


def test_parent_watchdog_preserves_legacy_exact_windows_marker():
    marker = "win:638908765432109876"

    assert (
        _is_serve_orphaned(
            4242,
            marker,
            process_start_marker=lambda _pid: marker,
        )
        is False
    )


def test_parent_watchdog_does_not_kill_a_live_parent_on_macos_timezone_drift():
    """Regression for issue #95693: `ps -o lstart=` is a timezone/locale-
    rendered wall-clock string on macOS (the only marker format for
    platforms without a dedicated linux:/win: branch). The exact
    evidence from the report -- the SAME process instant rendered
    under EDT (cached by Electron before a TZ change) vs. CEST (probed
    by a freshly-spawned backend after) -- must not be treated as proof
    the parent died; it must degrade to the PID-only check instead."""
    expected = "ps:Thu Aug 20 22:33:11 2026"  # EDT (UTC-4), Electron's cached marker
    actual = "ps:Fri Aug 21 04:33:11 2026"  # CEST (UTC+2), same instant, freshly probed

    assert (
        _is_serve_orphaned(
            4242,
            expected,
            pid_exists=lambda _pid: True,  # parent is genuinely still alive
            process_start_marker=lambda _pid: actual,
        )
        is False
    )


def test_parent_watchdog_still_detects_a_genuinely_dead_parent_despite_ps_marker_mismatch():
    """The degrade-to-PID-only fallback must still correctly detect a
    genuinely dead parent -- the fix must not silently disable orphan
    detection for macOS, only stop treating a TZ-driven marker mismatch
    as automatic proof of death."""
    expected = "ps:Thu Aug 20 22:33:11 2026"
    actual = "ps:Fri Aug 21 04:33:11 2026"

    assert (
        _is_serve_orphaned(
            4242,
            expected,
            pid_exists=lambda _pid: False,  # parent is genuinely gone
            process_start_marker=lambda _pid: actual,
        )
        is True
    )


def test_parent_watchdog_exact_ps_marker_match_still_short_circuits():
    """Sanity: an exact ps: marker match (the common, no-TZ-change case)
    must still resolve to not-orphaned directly, without needing the
    PID-only fallback at all."""
    marker = "ps:Thu Aug 20 22:33:11 2026"

    assert (
        _is_serve_orphaned(
            4242,
            marker,
            pid_exists=lambda _pid: False,  # must be irrelevant -- short-circuits on match
            process_start_marker=lambda _pid: marker,
        )
        is False
    )


def test_parent_watchdog_still_rejects_recycled_pid_via_stable_linux_marker():
    """Sanity/no-regression: the linux: marker format is a stable,
    timezone-independent integer (starttime jiffies from /proc stat) --
    a mismatch there still correctly proves a dead/recycled parent PID
    and must NOT be softened by this fix, which is scoped specifically
    to the ps: (macOS) format."""
    assert (
        _is_serve_orphaned(
            4242,
            "linux:12345",
            pid_exists=lambda _pid: True,  # even if a PID now exists (reused), still orphaned
            process_start_marker=lambda _pid: "linux:99999",
        )
        is True
    )
