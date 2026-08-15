from __future__ import annotations

import unittest

from hermes_cli.kanban_store.evidence import classify_evidence
from hermes_cli.kanban_store.types import ReclaimDecision


class EvidenceTests(unittest.TestCase):
    def base(self, **overrides):
        value = {
            "observation_id": "o1",
            "fresh_until": 200,
            "complete": True,
            "coverage": "strong",
            "process": "dead",
            "worker_motion": False,
            "idle_window_complete": True,
            "artifacts": "absent",
            "publication": "absent",
            "freeze_supported": True,
            "generation_match": True,
        }
        value.update(overrides)
        return value

    def test_dead_strong_absent_is_eligible(self):
        vector = classify_evidence([self.base(), self.base(observation_id="o2")], now=100)
        self.assertEqual(vector.decision, ReclaimDecision.ELIGIBLE_DEAD)

    def test_unknown_is_not_absence(self):
        vector = classify_evidence([self.base(artifacts="unknown")], now=100)
        self.assertEqual(vector.decision, ReclaimDecision.UNKNOWN)

    def test_heartbeat_does_not_count_as_motion(self):
        vector = classify_evidence(
            [self.base(process="alive", heartbeat_only=True), self.base(observation_id="o2", process="alive", heartbeat_only=True)],
            now=100,
        )
        self.assertEqual(vector.decision, ReclaimDecision.ELIGIBLE_INERT)

    def test_observer_writes_do_not_count_as_worker_motion(self):
        vector = classify_evidence(
            [
                self.base(source="observer", process="alive", worker_motion=True),
                self.base(observation_id="o2", source="observer", process="alive", worker_motion=True),
            ],
            now=100,
        )
        self.assertEqual(vector.decision, ReclaimDecision.ELIGIBLE_INERT)

    def test_stale_or_incomplete_preserves(self):
        vector = classify_evidence([self.base(fresh_until=50)], now=100)
        self.assertNotIn(vector.decision, {ReclaimDecision.ELIGIBLE_DEAD, ReclaimDecision.ELIGIBLE_INERT})

    def test_publication_presence_preserves(self):
        vector = classify_evidence([self.base(publication="present")], now=100)
        self.assertEqual(vector.decision, ReclaimDecision.PRESERVE)


if __name__ == "__main__":
    unittest.main()
