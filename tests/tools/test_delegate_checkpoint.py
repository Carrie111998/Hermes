#!/usr/bin/env python3
"""Tests for subagent checkpoint / orphan adoption (live upgrade).

Verifies that:
  - checkpoint_active_subagents() writes a valid JSON file
  - adopt_orphaned_subagents() consumes and removes the checkpoint
  - No checkpoint file means adopt_orphaned_subagents() is a no-op
  - Corrupted checkpoint is handled gracefully

Run with:  python -m pytest tests/tools/test_delegate_checkpoint.py -v
"""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.delegate_tool import (
    _CHECKPOINT_FILE_NAME,
    _checkpoint_dir,
    _checkpoint_path,
    _remove_checkpoint_safe,
    adopt_orphaned_subagents,
    checkpoint_active_subagents,
    list_active_subagents,
    _register_subagent,
    _active_subagents,
    _active_subagents_lock,
)


def _clean_active_subagents():
    """Remove all entries from the module-level active subagents dict."""
    with _active_subagents_lock:
        _active_subagents.clear()


class TestCheckpointPath(unittest.TestCase):
    """Path resolution for the checkpoint file."""

    @patch("hermes_constants.get_hermes_home")
    def test_checkpoint_path_under_hermes_home(self, mock_home):
        mock_home.return_value = Path("/tmp/hermes_test")
        path = _checkpoint_path()
        self.assertIn("state", path)
        self.assertIn(_CHECKPOINT_FILE_NAME, path)
        self.assertTrue(path.endswith(_CHECKPOINT_FILE_NAME))


class TestCheckpointWrite(unittest.TestCase):
    """checkpoint_active_subagents serialization."""

    def setUp(self):
        _clean_active_subagents()

    def tearDown(self):
        _clean_active_subagents()
        p = _checkpoint_path()
        if p and os.path.isfile(p):
            try:
                os.remove(p)
            except OSError:
                pass

    @patch("hermes_constants.get_hermes_home")
    def test_no_subagents_returns_none(self, mock_home):
        with tempfile.TemporaryDirectory() as td:
            mock_home.return_value = Path(td)
            result = checkpoint_active_subagents()
            self.assertIsNone(result)

    @patch("hermes_constants.get_hermes_home")
    def test_writes_checkpoint_file(self, mock_home):
        with tempfile.TemporaryDirectory() as td:
            mock_home.return_value = Path(td)
            # Register a fake subagent
            _register_subagent({
                "subagent_id": "test-sa-1",
                "parent_id": None,
                "depth": 0,
                "goal": "Test task",
                "model": "test-model",
                "started_at": time.time(),
                "status": "running",
                "tool_count": 0,
            })
            result = checkpoint_active_subagents()
            self.assertIsNotNone(result)
            self.assertTrue(os.path.isfile(result))

            # Validate JSON content
            with open(result) as f:
                payload = json.load(f)
            self.assertIn("pid", payload)
            self.assertIn("parent_pid", payload)
            self.assertIn("created_at", payload)
            self.assertIn("subagents", payload)
            self.assertEqual(len(payload["subagents"]), 1)
            self.assertEqual(payload["subagents"][0]["subagent_id"], "test-sa-1")
            self.assertEqual(payload["subagents"][0]["goal"], "Test task")

    @patch("hermes_constants.get_hermes_home")
    def test_multiple_subagents(self, mock_home):
        with tempfile.TemporaryDirectory() as td:
            mock_home.return_value = Path(td)
            _register_subagent({
                "subagent_id": "sa-a",
                "parent_id": None,
                "depth": 0,
                "goal": "Research A",
                "model": "gpt-4",
                "started_at": time.time(),
                "status": "running",
                "tool_count": 2,
            })
            _register_subagent({
                "subagent_id": "sa-b",
                "parent_id": "sa-a",
                "depth": 1,
                "goal": "Research B",
                "model": "claude-3",
                "started_at": time.time(),
                "status": "running",
                "tool_count": 5,
            })
            path = checkpoint_active_subagents()
            self.assertIsNotNone(path)
            with open(path) as f:
                payload = json.load(f)
            self.assertEqual(len(payload["subagents"]), 2)
            ids = {sa["subagent_id"] for sa in payload["subagents"]}
            self.assertEqual(ids, {"sa-a", "sa-b"})


class TestOrphanAdoption(unittest.TestCase):
    """adopt_orphaned_subagents consuming checkpoints."""

    def _create_checkpoint(self, directory, subagents, pid=12345):
        """Helper: write a valid checkpoint under *directory*."""
        state_dir = os.path.join(directory, "state")
        os.makedirs(state_dir, exist_ok=True)
        path = os.path.join(state_dir, _CHECKPOINT_FILE_NAME)
        payload = {
            "pid": pid,
            "parent_pid": pid - 1,
            "created_at": time.time(),
            "subagents": subagents,
        }
        with open(path, "w") as f:
            json.dump(payload, f)
        return path

    @patch("hermes_constants.get_hermes_home")
    def test_no_checkpoint_returns_zero(self, mock_home):
        with tempfile.TemporaryDirectory() as td:
            mock_home.return_value = Path(td)
            count = adopt_orphaned_subagents()
            self.assertEqual(count, 0)

    @patch("hermes_constants.get_hermes_home")
    def test_adopts_and_removes_checkpoint(self, mock_home):
        with tempfile.TemporaryDirectory() as td:
            mock_home.return_value = Path(td)
            path = self._create_checkpoint(td, [
                {
                    "subagent_id": "orphan-1",
                    "parent_id": None,
                    "depth": 0,
                    "goal": "Lost research task",
                    "model": "gpt-4",
                    "started_at": time.time(),
                    "status": "running",
                    "tool_count": 3,
                }
            ])
            self.assertTrue(os.path.isfile(path))
            count = adopt_orphaned_subagents()
            self.assertEqual(count, 1)
            # Checkpoint must be removed after adoption
            self.assertFalse(os.path.isfile(path))

    @patch("hermes_constants.get_hermes_home")
    def test_adopts_multiple_orphans(self, mock_home):
        with tempfile.TemporaryDirectory() as td:
            mock_home.return_value = Path(td)
            self._create_checkpoint(td, [
                {
                    "subagent_id": "orphan-1",
                    "parent_id": None,
                    "depth": 0,
                    "goal": "Task 1",
                    "model": "gpt-4",
                    "started_at": time.time() - 3600,
                    "status": "running",
                    "tool_count": 5,
                },
                {
                    "subagent_id": "orphan-2",
                    "parent_id": "orphan-1",
                    "depth": 1,
                    "goal": "Task 2",
                    "model": "claude-3",
                    "started_at": time.time() - 1800,
                    "status": "running",
                    "tool_count": 2,
                },
                {
                    "subagent_id": "orphan-3",
                    "parent_id": None,
                    "depth": 0,
                    "goal": "Task 3",
                    "model": "gemini-pro",
                    "started_at": time.time() - 600,
                    "status": "running",
                    "tool_count": 0,
                },
            ])
            count = adopt_orphaned_subagents()
            self.assertEqual(count, 3)

    @patch("hermes_constants.get_hermes_home")
    def test_corrupt_checkpoint_does_not_crash(self, mock_home):
        with tempfile.TemporaryDirectory() as td:
            mock_home.return_value = Path(td)
            # Write invalid JSON
            state_dir = os.path.join(td, "state")
            os.makedirs(state_dir, exist_ok=True)
            bad_path = os.path.join(state_dir, _CHECKPOINT_FILE_NAME)
            with open(bad_path, "w") as f:
                f.write("{invalid json!!!}")
            self.assertTrue(os.path.isfile(bad_path))
            count = adopt_orphaned_subagents()
            self.assertEqual(count, 0)
            # Should have cleaned up the corrupt file
            self.assertFalse(os.path.isfile(bad_path))

    @patch("hermes_constants.get_hermes_home")
    def test_empty_subagents_list_cleans_up(self, mock_home):
        with tempfile.TemporaryDirectory() as td:
            mock_home.return_value = Path(td)
            self._create_checkpoint(td, [])
            count = adopt_orphaned_subagents()
            self.assertEqual(count, 0)
            # Empty list should have been cleaned up
            path = os.path.join(td, "state", _CHECKPOINT_FILE_NAME)
            self.assertFalse(os.path.isfile(path))

    @patch("hermes_constants.get_hermes_home")
    def test_minimal_goal_does_not_crash(self, mock_home):
        """Edge case: subagent with minimal fields (just goal)."""
        with tempfile.TemporaryDirectory() as td:
            mock_home.return_value = Path(td)
            path = self._create_checkpoint(td, [
                {"goal": "Short task"},
            ])
            count = adopt_orphaned_subagents()
            self.assertEqual(count, 1)
            self.assertFalse(os.path.isfile(path))


class TestRemoveCheckpointSafe(unittest.TestCase):
    """_remove_checkpoint_safe should never raise."""

    def test_nonexistent_file(self):
        _remove_checkpoint_safe("/nonexistent/path.json")

    def test_valid_file(self):
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp = f.name
        self.assertTrue(os.path.isfile(tmp))
        _remove_checkpoint_safe(tmp)
        self.assertFalse(os.path.isfile(tmp))


if __name__ == "__main__":
    unittest.main()
