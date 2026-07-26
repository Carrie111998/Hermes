"""Tests for cua-driver subprocess teardown on interpreter exit."""

import atexit
from unittest.mock import MagicMock, patch

from tools.computer_use import tool as cu_tool


class TestAtexitTeardown:
    def test_shutdown_stops_each_live_task_backend(self):
        """Every still-owned backend is stopped when the interpreter exits."""
        first = MagicMock()
        second = MagicMock()
        with patch.object(cu_tool, "_backends", {"task-a": first, "task-b": second}):
            cu_tool._shutdown_backend_atexit()
            first.stop.assert_called_once()
            second.stop.assert_called_once()

    def test_shutdown_clears_the_task_backend_registry(self):
        """After teardown no task backend remains cached."""
        fake = MagicMock()
        with patch.object(cu_tool, "_backends", {"task-a": fake}):
            cu_tool._shutdown_backend_atexit()
            assert cu_tool._backends == {}

    def test_shutdown_is_a_noop_when_never_started(self):
        """No backend was ever created => nothing to stop, no error."""
        with patch.object(cu_tool, "_backends", {}):
            cu_tool._shutdown_backend_atexit()  # must not raise
            assert cu_tool._backends == {}

    def test_shutdown_swallows_backend_errors(self):
        """A failing stop() must not raise out of an atexit hook."""
        fake = MagicMock()
        fake.stop.side_effect = RuntimeError("driver already dead")
        with patch.object(cu_tool, "_backends", {"task-a": fake}):
            cu_tool._shutdown_backend_atexit()  # must not raise
            assert cu_tool._backends == {}

    def test_hook_is_registered_with_atexit(self):
        """Importing the tool module registers the teardown hook."""
        atexit.unregister(cu_tool._shutdown_backend_atexit)
        try:
            fake = MagicMock()
            with patch.object(cu_tool, "_backends", {"task-a": fake}):
                atexit.register(cu_tool._shutdown_backend_atexit)
                atexit._run_exitfuncs()
                fake.stop.assert_called_once()
        finally:
            atexit.unregister(cu_tool._shutdown_backend_atexit)
            atexit.register(cu_tool._shutdown_backend_atexit)
