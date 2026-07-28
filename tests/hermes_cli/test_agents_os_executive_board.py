import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from hermes_cli.agents_os import AgentsOSPaths, AgentsOSService
from hermes_cli.agents_os_executive_board import ExecutiveBoardService, ensure_executive_board_schema
from hermes_cli.agents_os_web import executive_board_action, executive_board_payload, mission_control_html


class ExecutiveBoardProtocolTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "board.sqlite"
        self.conn = sqlite3.connect(self.db)
        self.conn.row_factory = sqlite3.Row
        ensure_executive_board_schema(self.conn)
        self.service = ExecutiveBoardService(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def test_schema_and_disabled_future_adapters(self):
        tables = {r[0] for r in self.conn.execute("select name from sqlite_master where type='table'")}
        self.assertTrue({"board_meetings", "board_proposals", "board_challenges", "board_recommendations", "board_decisions", "board_memory_candidates", "board_shared_memory", "company_projects", "company_ideas"}.issubset(tables))
        roster = {item["id"]: item for item in self.service.agent_roster()}
        self.assertEqual("available", roster["doni"]["status"])
        self.assertEqual("available", roster["kodi"]["status"])
        for agent_id in ("openclaw", "claude"):
            self.assertEqual("disabled", roster[agent_id]["status"])
            self.assertFalse(roster[agent_id]["activation_enabled"])
            self.assertEqual("no-private-memory-access", roster[agent_id]["memory_boundary"])
            self.assertEqual("no-credentials-configured", roster[agent_id]["auth_boundary"])

    def test_pair_protocol_requires_dual_proposals_and_mutual_challenge(self):
        meeting = self.service.create_meeting("Izgraditi Executive Board", project_id="agents-os", risk_class="safe-local")
        self.service.submit_proposal(meeting, "doni", "Strategija i poslovni cilj", {"value": 9, "feasibility": 8, "risk": 3, "cost": 4, "time": 6, "revenue": 8})
        with self.assertRaises(ValueError):
            self.service.finalize_recommendation(meeting, "prerano", "/goal prerano")
        self.service.submit_proposal(meeting, "kodi", "Tehnička izvedba i testovi", {"value": 8, "feasibility": 9, "risk": 2, "cost": 4, "time": 7, "revenue": 7})
        self.service.submit_challenge(meeting, "doni", "kodi", "Dodati revenue i operator UX kriterije")
        self.service.submit_challenge(meeting, "kodi", "doni", "Zaključati schema i cold-start test")
        recommendation = self.service.finalize_recommendation(
            meeting,
            "Prvo parity, zatim pair protocol, board i shared memory.",
            "/goal Implementiraj odobreni slijed uz TDD i approval gateove.",
            consensus="consensus",
        )
        self.assertEqual("needs-owner-decision", recommendation["status"])
        self.assertEqual({"doni", "kodi"}, set(recommendation["proposal_agents"]))
        self.assertEqual(2, recommendation["challenge_count"])

    def test_dissent_and_owner_decision_are_auditable(self):
        meeting = self.service.create_meeting("Odabrati smjer", project_id="agents-os")
        score = {"value": 8, "feasibility": 8, "risk": 4, "cost": 5, "time": 5, "revenue": 7}
        self.service.submit_proposal(meeting, "doni", "Smjer A", score)
        self.service.submit_proposal(meeting, "kodi", "Smjer B", score)
        self.service.submit_challenge(meeting, "doni", "kodi", "Rizik B")
        self.service.submit_challenge(meeting, "kodi", "doni", "Rizik A")
        recommendation = self.service.finalize_recommendation(meeting, "Preporuka A uz rezervu", "/goal A", consensus="dissent", dissent="Kodi preferira B")
        with self.assertRaises(ValueError):
            self.service.record_owner_decision(meeting, "approved", decided_by="doni", reason="nije vlasnik")
        decision = self.service.record_owner_decision(meeting, "approved", decided_by="goran", reason="Prihvaćen A")
        self.assertEqual("approved", decision["decision"])
        snapshot = self.service.meeting_snapshot(meeting)
        self.assertEqual("Kodi preferira B", snapshot["recommendation"]["dissent"])
        self.assertEqual("approved", snapshot["decision"]["decision"])
        self.assertEqual(recommendation["recommendation_id"], snapshot["recommendation"]["recommendation_id"])

    def test_reviewed_shared_memory_requires_hash_provenance_and_owner_approval(self):
        with self.assertRaises(ValueError):
            self.service.stage_memory_candidate(
                "capsule-private", "d" * 64, "P0", "private memory dump",
                [{"source_type": "artifact", "source_ref": "plan.md", "sha256": "d" * 64}],
            )
        with self.assertRaises(ValueError):
            self.service.stage_memory_candidate(
                "capsule-secret", "e" * 64, "P1", "sigurna činjenica",
                [{"source_type": "artifact", "source_ref": "token=abcdefghijk", "sha256": "e" * 64}],
            )
        candidate = self.service.stage_memory_candidate(
            capsule_id="capsule-1",
            capsule_sha256="a" * 64,
            classification="P1",
            summary="Odobrena projektna činjenica bez raw razgovora.",
            provenance=[{"source_type": "artifact", "source_ref": "artifact://agents-os/plan", "sha256": "b" * 64}],
        )
        self.assertEqual("pending-review", candidate["status"])
        with self.assertRaises(ValueError):
            self.service.promote_memory_candidate(candidate["candidate_id"], approved_by="doni", reason="nije vlasnik")
        promoted = self.service.promote_memory_candidate(candidate["candidate_id"], approved_by="goran", reason="Pregledano")
        self.assertEqual("approved", promoted["status"])
        hits = self.service.search_shared_memory("projektna")
        self.assertEqual(1, len(hits))
        self.assertEqual("capsule-1", hits[0]["capsule_id"])
        self.assertEqual(1, self.service.company_snapshot()["shared_knowledge"]["approved_count"])
        dumped = json.dumps(hits, ensure_ascii=False).lower()
        self.assertNotIn("raw transcript", dumped)
        with self.assertRaises(ValueError):
            self.service.stage_memory_candidate("capsule-2", "c" * 64, "P2", "private", [])

    def test_company_snapshot_covers_projects_ideas_money_execution_and_decisions(self):
        self.service.upsert_project("agents-os", "Agents OS", status="active", owner="goran", next_action="Dovršiti Board", revenue_potential=8, strategic_value=10, risk=3)
        idea = self.service.add_idea("idea-1", "AI operativna firma", source="goran", status="board-review", value=10, feasibility=8, risk=4, cost=5, time=6, revenue=9)
        self.assertEqual(74, idea["opportunity_score"])
        snapshot = self.service.company_snapshot()
        self.assertEqual(1, snapshot["company_overview"]["project_count"])
        self.assertEqual(1, snapshot["idea_pipeline"]["count"])
        self.assertEqual("idea-1", snapshot["money_opportunity"][0]["id"])
        self.assertIn("execution_room", snapshot)
        self.assertIn("decision_desk", snapshot)

    def test_web_payload_actions_and_ui_surface_are_functional(self):
        root = Path(self.tmp.name) / "agents-os"
        paths = AgentsOSPaths(
            home=Path(self.tmp.name), root=root, db=root / "state.sqlite",
            artifacts=root / "artifacts", outbox=root / "outbox", vault_root=root / "vault",
        )
        service = AgentsOSService(paths)
        created = executive_board_action(service, {"action": "create_meeting", "objective": "Zajednički pregled plana", "project_id": "agents-os"})
        self.assertEqual("created", created["status"])
        payload = executive_board_payload(paths)
        self.assertEqual(1, payload["execution_room"]["active_count"])
        self.assertEqual({"doni", "kodi", "openclaw", "claude"}, {a["id"] for a in payload["agent_roster"]})
        html = mission_control_html(service)
        for marker in ('data-tab="executiveBoard"', 'id="executiveBoard"', 'id="executiveObjective"',
                       'id="executiveMeetingId"', 'id="executiveDoniProposal"', 'id="executiveKodiProposal"',
                       'id="executiveDoniChallenge"', 'id="executiveKodiChallenge"',
                       'id="executiveRecommendation"', 'id="executiveDissent"',
                       'id="executiveOwnerDecision"', 'id="executiveMemoryCandidate"',
                       '/api/executive-board', '/api/executive-board/action', 'Okrugli stol'):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
