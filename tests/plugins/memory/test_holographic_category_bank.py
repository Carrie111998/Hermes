"""Regression tests for Holographic category-bank maintenance."""

import pytest

pytest.importorskip("numpy")

from plugins.memory.holographic.store import MemoryStore


def test_category_move_rebuilds_source_and_destination_banks(tmp_path) -> None:
    with MemoryStore(db_path=tmp_path / "category_move.db", hrr_dim=64) as store:
        moved_id = store.add_fact("Move this banked fact", category="source")
        store.add_fact("Keep this source fact", category="source")
        store.add_fact("Keep this destination fact", category="destination")

        assert store.update_fact(moved_id, category="destination")

        counts = {
            row["bank_name"]: row["fact_count"]
            for row in store._conn.execute(
                "SELECT bank_name, fact_count FROM memory_banks"
            ).fetchall()
        }

    assert counts["cat:source"] == 1
    assert counts["cat:destination"] == 2
