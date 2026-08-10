"""Tests for named subagent personas (``delegate_task(agent=...)``).

These assert BEHAVIOR CONTRACTS, not the current contents of any persona file:
- a persona supplies the child's standing prompt,
- a persona can only ever REDUCE capability (never widen), and
- a persona that cannot get its declared requirements fails loudly.
"""

import unittest
from pathlib import Path
from unittest.mock import patch

from agent.subagent_personas import (
    PersonaError,
    discover_personas,
    get_persona_dirs,
    load_persona,
)


def _write(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


class TestPersonaParsing(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        self.agents = self.home / "agents"
        patcher = patch(
            "agent.subagent_personas.get_hermes_home", return_value=self.home
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_body_becomes_the_prompt(self):
        _write(
            self.agents,
            "scout",
            "---\nname: scout\ndescription: Recon.\n---\nYou are a read-only scout.\n",
        )
        persona = load_persona("scout")
        self.assertEqual(persona["name"], "scout")
        self.assertEqual(persona["prompt"], "You are a read-only scout.")

    def test_name_defaults_to_filename(self):
        _write(self.agents, "critic", "---\ndescription: Adversary.\n---\nBreak it.\n")
        self.assertEqual(load_persona("critic")["name"], "critic")

    def test_lookup_is_case_insensitive(self):
        _write(self.agents, "scout", "---\nname: scout\n---\nBody.\n")
        self.assertEqual(load_persona("SCOUT")["name"], "scout")

    def test_toolsets_accepts_list_or_csv(self):
        _write(self.agents, "a", "---\nname: a\ntoolsets: [file, web]\n---\nBody.\n")
        _write(self.agents, "b", "---\nname: b\ntoolsets: file, web\n---\nBody.\n")
        self.assertEqual(load_persona("a")["toolsets"], ["file", "web"])
        self.assertEqual(load_persona("b")["toolsets"], ["file", "web"])

    def test_empty_body_is_rejected(self):
        _write(self.agents, "hollow", "---\nname: hollow\n---\n\n")
        with self.assertRaises(PersonaError):
            load_persona("hollow")

    def test_required_toolsets_must_be_a_subset_of_toolsets(self):
        """A persona that could never satisfy its own contract is invalid."""
        _write(
            self.agents,
            "impossible",
            "---\nname: impossible\ntoolsets: [file]\nrequired_toolsets: [web]\n---\nBody.\n",
        )
        with self.assertRaises(PersonaError):
            load_persona("impossible")

    def test_unknown_name_error_lists_available(self):
        """The error must name alternatives — otherwise the model retries blind."""
        _write(self.agents, "scout", "---\nname: scout\n---\nBody.\n")
        with self.assertRaises(PersonaError) as ctx:
            load_persona("scot")
        self.assertIn("scout", str(ctx.exception))

    def test_one_malformed_persona_does_not_hide_the_others(self):
        _write(self.agents, "good", "---\nname: good\n---\nBody.\n")
        _write(self.agents, "bad", "---\nname: bad\ntoolsets: 7\n---\nBody.\n")
        self.assertIn("good", discover_personas())

    def test_invalid_max_iterations_rejected(self):
        _write(self.agents, "z", "---\nname: z\nmax_iterations: 0\n---\nBody.\n")
        with self.assertRaises(PersonaError):
            load_persona("z")


class TestPersonaScopePriority(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.home = root / "home"
        self.project = root / "project"
        patcher = patch(
            "agent.subagent_personas.get_hermes_home", return_value=self.home
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def test_project_scope_wins_over_user_scope(self):
        _write(self.home / "agents", "scout", "---\nname: scout\n---\nUSER COPY.\n")
        _write(
            self.project / ".hermes" / "agents",
            "scout",
            "---\nname: scout\n---\nPROJECT COPY.\n",
        )
        self.assertEqual(load_persona("scout", self.project)["prompt"], "PROJECT COPY.")
        self.assertEqual(load_persona("scout")["prompt"], "USER COPY.")

    def test_project_dir_precedes_user_dir(self):
        dirs = get_persona_dirs(self.project)
        self.assertEqual(len(dirs), 2)
        self.assertTrue(str(dirs[0]).endswith("project/.hermes/agents"))


class TestPersonaCannotWidenCapability(unittest.TestCase):
    """The ba0bc01d1f invariant: capability scoping is never widened."""

    def test_schema_still_hides_toolsets_and_max_iterations(self):
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA

        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertNotIn("toolsets", props)
        self.assertNotIn("max_iterations", props)
        self.assertNotIn("toolsets", props["tasks"]["items"]["properties"])
        # The persona name IS exposed — it selects among human-authored files.
        self.assertIn("agent", props)
        self.assertEqual(props["agent"]["type"], "string")

    def test_persona_toolsets_are_intersected_with_parent(self):
        """A persona naming a toolset the parent lacks must not gain it."""
        from tools.delegate_tool import _expand_parent_toolsets, _resolve_parent_toolsets

        class _Parent:
            enabled_toolsets = ["file"]

        parent = _resolve_parent_toolsets(_Parent())
        available = _expand_parent_toolsets(parent)
        self.assertNotIn("web", available)
        # This is the same rule _build_child_agent applies to persona toolsets.
        self.assertEqual([t for t in ["file", "web"] if t in available], ["file"])

    def test_resolve_parent_toolsets_falls_back_to_defaults(self):
        from tools.delegate_tool import DEFAULT_TOOLSETS, _resolve_parent_toolsets

        self.assertEqual(_resolve_parent_toolsets(None), set(DEFAULT_TOOLSETS))


class TestPersonaReachesChildBuilder(unittest.TestCase):
    """The persona's declared settings must actually reach the child."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        _write(
            self.home / "agents",
            "lowscout",
            "---\nname: lowscout\ntoolsets: [file]\nreasoning_effort: low\n"
            "max_iterations: 7\n---\nBreadth recon only.\n",
        )
        patcher = patch(
            "agent.subagent_personas.get_hermes_home", return_value=self.home
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _spawn_and_capture(self, **kwargs):
        import tools.delegate_tool as dt

        captured = {}

        def _fake_build(**kw):
            captured.update(kw)
            raise RuntimeError("stop-after-build")

        class _Parent:
            enabled_toolsets = ["file", "terminal", "web"]
            valid_tool_names = ["read_file"]
            session_id = "s"
            model = "m"
            provider = "p"
            reasoning_config = {"enabled": True, "effort": "xhigh"}

        with patch.object(
            dt, "_build_child_preserving_parent_tools", side_effect=_fake_build
        ):
            try:
                dt.delegate_task(parent_agent=_Parent(), **kwargs)
            except Exception:
                pass
        return captured

    def test_persona_narrows_toolsets_and_sets_effort_and_iterations(self):
        cap = self._spawn_and_capture(goal="find X", context="ctx", agent="lowscout")
        self.assertEqual(cap.get("toolsets"), ["file"])
        self.assertEqual(cap.get("reasoning_effort"), "low")
        self.assertEqual(cap.get("max_iterations"), 7)

    def test_persona_prompt_leads_context_and_task_context_survives(self):
        cap = self._spawn_and_capture(goal="find X", context="TASKCTX", agent="lowscout")
        context = str(cap.get("context"))
        self.assertTrue(context.startswith("Breadth recon only."))
        self.assertIn("TASKCTX", context)
        self.assertEqual(cap.get("goal"), "find X")

    def test_no_persona_leaves_routing_untouched(self):
        """Without agent=, nothing about the existing path changes."""
        cap = self._spawn_and_capture(goal="find X")
        self.assertIsNone(cap.get("toolsets"))
        self.assertIsNone(cap.get("reasoning_effort"))


class TestPerTaskPersona(unittest.TestCase):
    """A per-task `agent` must be honored, not silently ignored."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name) / "home"
        agents = self.home / "agents"
        _write(
            agents,
            "reader",
            "---\nname: reader\ntoolsets: [file]\nmax_iterations: 7\n---\nRead only.\n",
        )
        _write(
            agents,
            "runner",
            "---\nname: runner\ntoolsets: [terminal]\nmax_iterations: 9\n---\nRun things.\n",
        )
        patcher = patch(
            "agent.subagent_personas.get_hermes_home", return_value=self.home
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self._tmp.cleanup)

    def _spawn_batch(self, tasks, **kwargs):
        import tools.delegate_tool as dt

        captured = []

        def _fake_build(**kw):
            captured.append(kw)
            return type(
                "_C",
                (),
                {
                    "enabled_toolsets": kw.get("toolsets"),
                    "session_id": "c",
                    "_session_init_model_config": None,
                },
            )()

        class _Parent:
            enabled_toolsets = ["file", "terminal", "web"]
            valid_tool_names = ["read_file"]
            session_id = "s"
            model = "m"
            provider = "p"
            reasoning_config = None

        with patch.object(
            dt, "_build_child_preserving_parent_tools", side_effect=_fake_build
        ):
            try:
                dt.delegate_task(tasks=tasks, parent_agent=_Parent(), **kwargs)
            except Exception:
                pass
        return captured

    def test_one_batch_can_fan_out_different_personas(self):
        cap = self._spawn_batch(
            [
                {"goal": "read the upload retry path", "agent": "reader"},
                {"goal": "run the retry test suite", "agent": "runner"},
            ]
        )
        self.assertEqual(len(cap), 2)
        self.assertEqual(cap[0]["toolsets"], ["file"])
        self.assertEqual(cap[0]["max_iterations"], 7)
        self.assertEqual(cap[1]["toolsets"], ["terminal"])
        self.assertEqual(cap[1]["max_iterations"], 9)

    def test_per_task_agent_overrides_top_level(self):
        cap = self._spawn_batch(
            [
                {"goal": "read the upload retry path", "agent": "runner"},
                {"goal": "read the download retry path"},
            ],
            agent="reader",
        )
        self.assertEqual(cap[0]["toolsets"], ["terminal"])  # per-task wins
        self.assertEqual(cap[1]["toolsets"], ["file"])  # falls back to top-level

    def test_unknown_per_task_agent_fails_the_call(self):
        """Must error, not silently ignore — the caller can't see a no-op."""
        import tools.delegate_tool as dt

        class _Parent:
            enabled_toolsets = ["file"]
            valid_tool_names = ["read_file"]
            session_id = "s"
            model = "m"
            provider = "p"

        result = dt.delegate_task(
            tasks=[
                {"goal": "a genuine first task here", "agent": "nope"},
                {"goal": "a genuine second task here"},
            ],
            parent_agent=_Parent(),
        )
        self.assertIn("nope", result)
        self.assertIn("reader", result)  # lists what IS available

    def test_per_task_agent_is_in_the_schema(self):
        from tools.delegate_tool import DELEGATE_TASK_SCHEMA

        item_props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]["tasks"]["items"][
            "properties"
        ]
        self.assertIn("agent", item_props)


if __name__ == "__main__":
    unittest.main()
