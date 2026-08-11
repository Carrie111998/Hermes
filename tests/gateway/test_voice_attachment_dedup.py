import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class TestDurableVoiceDeduplicator(unittest.TestCase):
    def _make(self, db_path):
        from gateway.platforms.helpers import DurableVoiceDeduplicator

        return DurableVoiceDeduplicator(db_path=db_path)

    def test_first_call_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self._make(Path(tmpdir) / "state.db")
            self.assertFalse(d.is_duplicate("msg1", "att1"))

    def test_second_call_is_duplicate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self._make(Path(tmpdir) / "state.db")
            d.is_duplicate("msg1", "att1")
            self.assertTrue(d.is_duplicate("msg1", "att1"))

    def test_different_attachment_not_duplicate(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            d = self._make(Path(tmpdir) / "state.db")
            d.is_duplicate("msg1", "att1")
            self.assertFalse(d.is_duplicate("msg1", "att2"))

    def test_survives_restart(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "state.db"
            d1 = self._make(db_path)
            d1.is_duplicate("msg1", "att1")
            d2 = self._make(db_path)
            self.assertTrue(d2.is_duplicate("msg1", "att1"))

    def test_db_unavailable_falls_back(self):
        with patch(
            "gateway.platforms.helpers.sqlite3.connect",
            side_effect=sqlite3.OperationalError("db unavailable"),
        ):
            d = self._make(Path("/tmp/ignored-state.db"))
            self.assertFalse(d.is_duplicate("msg1", "att1"))
            self.assertTrue(d.is_duplicate("msg1", "att1"))
