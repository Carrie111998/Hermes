"""H6-2 Police Horitius Persona Kernel contract tests (stdlib only)."""

import copy
import json
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.persona.composer import compose_persona_prompt
from agent.persona.growth import InMemoryGrowthStore, ReflectionCandidate
from agent.persona.handoff import HandoffValidationError, PersonaHandoff
from agent.persona.loader import PersonaCanonError, calculate_checksum, load_persona_kernel
from agent.persona.schema import PersonaKernel, PersonaValidationError
from agent.system_prompt import build_system_prompt_parts


def _candidate(content="Compared two sources and marked uncertainty.", **overrides):
    values = dict(
        kind="comparative_reflection", content=content, source="fake:test",
        observation_date="2026-08-13", confidence="medium",
        supporting_evidence=("source-a",), counter_evidence=("source-b",),
    )
    values.update(overrides)
    return ReflectionCandidate(**values)


def _handoff(**overrides):
    values = dict(
        handoff_id="h-1", from_persona="police_horitius", to_persona="curator_orchestra",
        task_type="adoption_decision", facts=["one observed fact"], hypotheses=["one marked hypothesis"],
        unknowns=["adoption outcome"], requested_output="Owner-controlled strategic decision",
        out_of_scope=["adoption"], evidence_refs=["fake:test"], canon_version="1.0.0",
        approval_required=True, execution_requested=False,
    )
    values.update(overrides)
    return values


class PoliceKernelRequiredTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.kernel = load_persona_kernel("police_horitius")

    def test_01_identity(self):
        self.assertEqual(self.kernel.persona_id, "police_horitius")
        self.assertEqual(self.kernel.canonical_role, "chief_observation_officer")
        with self.assertRaises(FrozenInstanceError):
            self.kernel.canonical_role = "final_decision_maker"

    def test_02_role(self):
        self.assertIn("adoption", self.kernel.non_responsibilities)
        self.assertNotIn("adoption", self.kernel.responsibilities)

    def test_03_handoff(self):
        handoff = PersonaHandoff.from_mapping(_handoff())
        self.assertEqual(handoff.to_persona, "curator_orchestra")
        self.assertTrue(handoff.approval_required)
        self.assertFalse(handoff.execution_requested)

    def test_04_owner_authority(self):
        self.assertIn("may_not_execute_owner_only_changes", self.kernel.owner_relation)
        self.assertIn("canon_change", self.kernel.non_responsibilities)

    def test_05_growth(self):
        store = InMemoryGrowthStore(self.kernel)
        weak = _candidate("A single source was insufficient; compare sources and mark uncertainty.")
        self.assertTrue(store.add(weak))
        self.assertEqual(store.select(["comparative_reflection"]), (weak,))
        self.assertEqual(self.kernel.canonical_role, "chief_observation_officer")

    def test_06_drift(self):
        store = InMemoryGrowthStore(self.kernel)
        for role in ("auditor", "final approver", "engineer", "strategist", "autonomous decision maker"):
            self.assertFalse(store.add(_candidate(f"Police is now the {role}.")))
        self.assertEqual(len(store.quarantined), 5)
        self.assertEqual(self.kernel.canonical_role, "chief_observation_officer")

    def test_07_canon_precedence(self):
        store = InMemoryGrowthStore(self.kernel)
        self.assertFalse(store.add(_candidate("Police Horitius is now the final decision maker.")))
        self.assertEqual(store.candidates, ())
        self.assertEqual(self.kernel.canonical_role, "chief_observation_officer")

    def test_08_unknown(self):
        self.assertIn("unknowns", self.kernel.output_contract)
        prompt = compose_persona_prompt(self.kernel)
        self.assertIn("Mark facts, observations, possible connections, hypotheses, and unknowns distinctly", prompt)

    def test_09_token_context_isolation(self):
        prompt = compose_persona_prompt(self.kernel)
        for other in ("Curator Orchestra", "Doctrina Share", "Persona Gemini", "Mercator Vale", "Exor Verelden", "Ordinator Detailer", "Beg Weag", "Literary Reviser"):
            self.assertNotIn(other, prompt)

    def test_10_skill_interference(self):
        prompt = compose_persona_prompt(self.kernel) + "\nAVAILABLE_CAPABILITY: build, review"
        self.assertIn("RESPONSIBILITIES: observation, trend_analysis, evidence_comparison", prompt)
        self.assertIn("NON_RESPONSIBILITIES: policy_decision, adoption, publication, canon_change", prompt)

    def test_11_permission(self):
        agent = SimpleNamespace(valid_tool_names=("terminal",), _persona_kernel=None)
        before = agent.valid_tool_names
        agent._persona_kernel = self.kernel
        self.assertEqual(agent.valid_tool_names, before)

    def test_12_p5_persona_disabled(self):
        from agent.agent_init import init_agent
        with self.assertRaisesRegex(ValueError, "disabled in isolated runtime"):
            init_agent(SimpleNamespace(), model="fake", isolated_runtime=True, persona_id="police_horitius")

    def test_13_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(__file__).parents[2] / "agent/persona/canon/police_horitius.json"
            data = json.loads(source.read_text(encoding="utf-8"))
            data["purpose"] = "tampered"
            Path(directory, "police_horitius.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(PersonaCanonError, "checksum mismatch"):
                load_persona_kernel("police_horitius", canon_dir=Path(directory))

    def test_14_handoff_injection(self):
        for field in ("canon", "canonical_role", "permissions", "owner_authority"):
            with self.subTest(field=field), self.assertRaises(HandoffValidationError):
                PersonaHandoff.from_mapping(_handoff(**{field: "expanded"}))


class PoliceKernelAdditionalTests(unittest.TestCase):
    def test_malformed_and_missing_fields(self):
        with self.assertRaises(PersonaValidationError):
            PersonaKernel.from_mapping({})

    def test_unsupported_canon_version_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(__file__).parents[2] / "agent/persona/canon/police_horitius.json"
            data = json.loads(source.read_text(encoding="utf-8"))
            data["canon_version"] = "2.0.0"
            data["checksum"] = calculate_checksum(data)
            Path(directory, "police_horitius.json").write_text(json.dumps(data), encoding="utf-8")
            with self.assertRaisesRegex(PersonaCanonError, "unsupported canon_version"):
                load_persona_kernel("police_horitius", canon_dir=Path(directory))

    def test_unknown_persona(self):
        with self.assertRaisesRegex(PersonaCanonError, "unknown persona_id"):
            load_persona_kernel("unknown")

    def test_duplicate_and_contradictory_responsibility(self):
        source = Path(__file__).parents[2] / "agent/persona/canon/police_horitius.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        duplicate = copy.deepcopy(data)
        duplicate["responsibilities"].append("observation")
        with self.assertRaisesRegex(PersonaValidationError, "duplicate"):
            PersonaKernel.from_mapping(duplicate)
        conflict = copy.deepcopy(data)
        conflict["non_responsibilities"].append("observation")
        with self.assertRaisesRegex(PersonaValidationError, "contradiction"):
            PersonaKernel.from_mapping(conflict)

    def test_deterministic_composition_and_checksum(self):
        kernel = load_persona_kernel("police_horitius")
        self.assertEqual(compose_persona_prompt(kernel), compose_persona_prompt(kernel))
        source = Path(__file__).parents[2] / "agent/persona/canon/police_horitius.json"
        data = json.loads(source.read_text(encoding="utf-8"))
        self.assertEqual(calculate_checksum(data), kernel.checksum)

    def test_feature_disabled_preserves_prompt(self):
        base = dict(
            load_soul_identity=False, skip_context_files=True, valid_tool_names=[],
            _task_completion_guidance=False, _tool_use_enforcement=False, _environment_probe=False,
            _kanban_worker_guidance="", _memory_store=None, _memory_manager=None, model="", provider="",
            platform="", pass_session_id=False, session_id="", _persona_kernel=None,
        )
        agent = SimpleNamespace(**base)
        with patch("run_agent.load_soul_md", return_value=""), patch("run_agent.build_nous_subscription_prompt", return_value=""), patch("run_agent.build_environment_hints", return_value=""):
            without = build_system_prompt_parts(agent)["stable"]
        agent._persona_kernel = load_persona_kernel("police_horitius")
        with patch("run_agent.load_soul_md", return_value=""), patch("run_agent.build_nous_subscription_prompt", return_value=""), patch("run_agent.build_environment_hints", return_value=""):
            with_persona = build_system_prompt_parts(agent)["stable"]
        self.assertNotIn("<persona_canon>", without)
        self.assertIn("<persona_canon>", with_persona)
        self.assertTrue(with_persona.startswith(without.split("\n\n")[0]))

    def test_handoff_execution_never_authorized(self):
        with self.assertRaisesRegex(HandoffValidationError, "does not authorize execution"):
            PersonaHandoff.from_mapping(_handoff(execution_requested=True))


if __name__ == "__main__":
    unittest.main()
