"""Tests for the Wave 2 TRANSITIONS state machine assertion (step 2f)."""
import warnings
import pytest
from hermes_cli.kanban_db import TRANSITIONS, _assert_transition


class TestTransitionsMap:
    """Tests for the TRANSITIONS adjacency map and _assert_transition."""

    def test_all_valid_statuses_have_transitions(self):
        from hermes_cli.kanban_db import VALID_STATUSES
        for status in VALID_STATUSES:
            assert status in TRANSITIONS, f"Status '{status}' missing from TRANSITIONS map"

    def test_legal_transition_no_warning(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _assert_transition("ready", "running")
            _assert_transition("running", "done")
            _assert_transition("running", "blocked")
            _assert_transition("blocked", "ready")
            _assert_transition("done", "archived")
            _assert_transition("done", "ready")  # reopen

    def test_illegal_transition_warns(self):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _assert_transition("done", "running")
            assert len(w) == 1
            assert "illegal transition" in str(w[0].message)
            assert "done→running" in str(w[0].message)

    def test_unknown_source_warns(self):
        """Unknown source state should warn (defensive — it's suspicious)."""
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            _assert_transition("unknown_state", "ready")
            assert len(w) == 1
            assert "illegal transition" in str(w[0].message)

    def test_running_can_crash(self):
        """running→crashed is a legal retry-eligible transition."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _assert_transition("running", "crashed")

    def test_running_can_timeout(self):
        """running→timed_out is a legal retry-eligible transition."""
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _assert_transition("running", "timed_out")

    def test_archived_can_unarchive(self):
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            _assert_transition("archived", "todo")