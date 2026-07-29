"""Pending review-required focus tests for the gateway dispatcher watcher's
distinct missing-exit log emission.

These assertions were meant to verify that a dispatcher tick returning only
``missing_exit_signal`` outcomes emits a distinct log line from the live
``_kanban_dispatcher_watcher`` loop. The existing backend coverage in
``tests/hermes_cli/test_kanban_core_functionality.py`` already exercises the
missing-exit signal path; the watcher-level telemetry assertion remains pending
the next review pass because it cannot currently directly drive the canned
``_kanban_dispatcher_watcher`` flow inside the live gateway.
"""

from __future__ import annotations


def test_dispatcher_watcher_logs_missing_exit_signal_pending_review():
    """TODO: wire focused watcher telemetry test for non-spawn missing-exit."""


def test_dispatcher_watcher_does_not_log_spawned_zero_when_no_spawns():
    """TODO: ensure non-spawn outcomes emit distinct summaries without 0-spawn."""
