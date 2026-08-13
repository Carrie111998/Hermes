#!/usr/bin/env python3
"""
Tests for the delegate_task workflow mode (parallel/pipeline batch
orchestration with semaphore + caps).

Uses a stubbed runner — no real LLM or AIAgent is ever constructed:
  - `_run_single_child` is patched with a side_effect returning
    structured per-item entries (or raising, to simulate item failure),
  - `_build_child_preserving_parent_tools` is patched to return a
    MagicMock child and to capture the context each child was built with
    (how pipeline context chaining is asserted).

Run with:  python -m pytest tests/tools/test_delegate_workflow.py -v
"""

import json
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from tools.delegate_tool import (
    DELEGATE_TASK_SCHEMA,
    _build_dynamic_schema_overrides,
    _build_top_level_description,
    delegate_task,
)
from tools.delegation_workflow import (
    WORKFLOW_MAX_ITEMS,
    _pipeline_context,
    _strip_workflow_model_hidden_fields,
    _validate_workflow,
    workflow_max_concurrent,
)

GOOD = (
    "Investigate the session expiry watcher and report concrete findings "
    "with file paths and line numbers"
)


def _make_mock_parent(depth=0):
    """Mock parent agent with the fields delegate_task expects."""
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "test-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "anthropic/claude-sonnet-4"
    parent.platform = "cli"
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent._session_db = None
    parent._delegate_depth = depth
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    return parent


def _completed(idx, summary="ok", **overrides):
    entry = {
        "task_index": idx,
        "status": "completed",
        "summary": summary,
        "api_calls": 1,
        "duration_seconds": 1.0,
        "exit_reason": "completed",
        "_child_role": None,
        "_child_cost_usd": 0.0,
    }
    entry.update(overrides)
    return entry


def _call(workflow, parent=None):
    return json.loads(
        delegate_task(workflow=workflow, parent_agent=parent or _make_mock_parent())
    )


class TestWorkflowValidation(unittest.TestCase):
    def test_valid_workflow_normalizes_steps(self):
        wf = {
            "steps": [
                {"parallel": [{"goal": GOOD}, {"goal": GOOD}]},
                {"pipeline": [{"goal": GOOD}, {"goal": GOOD}]},
            ]
        }
        normalized, err = _validate_workflow(wf)
        self.assertIsNone(err)
        self.assertEqual(
            [(s["kind"], len(s["items"])) for s in normalized["steps"]],
            [("parallel", 2), ("pipeline", 2)],
        )

    def test_step_must_have_exactly_one_kind(self):
        both = {"steps": [{"parallel": [{"goal": GOOD}], "pipeline": [{"goal": GOOD}]}]}
        _, err = _validate_workflow(both)
        self.assertIn("exactly one of 'parallel' or 'pipeline'", err)

        none = {"steps": [{"foo": [{"goal": GOOD}]}]}
        _, err = _validate_workflow(none)
        self.assertIn("exactly one of 'parallel' or 'pipeline'", err)

    def test_item_requires_goal(self):
        wf = {"steps": [{"parallel": [{"context": "no goal here"}]}]}
        _, err = _validate_workflow(wf)
        self.assertIn("missing a 'goal'", err)

    def test_workflow_must_be_object_with_steps(self):
        _, err = _validate_workflow([{"goal": GOOD}])
        self.assertIn("object with a 'steps' array", err)
        _, err = _validate_workflow({"steps": []})
        self.assertIn("non-empty array", err)

    def test_total_item_cap_rejected_before_spawn(self):
        """Over-cap workflows fail the whole call — no child is ever built."""
        wf = {
            "steps": [
                {"parallel": [{"goal": GOOD}] * (WORKFLOW_MAX_ITEMS + 1)}
            ]
        }
        with patch("tools.delegate_tool._build_child_preserving_parent_tools") as build:
            result = _call(wf)
        build.assert_not_called()
        self.assertIn(f"{WORKFLOW_MAX_ITEMS}-item cap", result["error"])

    def test_short_and_placeholder_goals_rejected(self):
        for goal in ("TODO", "task 3", "short"):
            _, err = _validate_workflow({"steps": [{"parallel": [{"goal": goal}]}]})
            self.assertIsNotNone(err, f"goal {goal!r} should be rejected")

    def test_pipeline_context_threading(self):
        prev_ok = {"status": "completed", "summary": "STAGE-OUTPUT"}
        ctx = _pipeline_context({"context": "base ctx"}, prev_ok)
        self.assertIn("base ctx", ctx)
        self.assertIn("STAGE-OUTPUT", ctx)

        prev_bad = {"status": "error", "error": "BOOM"}
        ctx = _pipeline_context({"context": "base ctx"}, prev_bad)
        self.assertIn("BOOM", ctx)
        self.assertIn("FAILED", ctx)

        # First stage: no previous output -> context untouched.
        self.assertEqual(_pipeline_context({"context": "base"}, None), "base")


class TestWorkflowParallel(unittest.TestCase):
    def test_parallel_runs_all_items_under_concurrency_cap(self):
        """6 items, max_concurrent=2: all complete, never more than 2 at once."""
        parent = _make_mock_parent()
        lock = threading.Lock()
        state = {"active": 0, "max_active": 0}

        def fake_run(task_index, goal, child, parent_agent, **_kwargs):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
            time.sleep(0.05)
            try:
                return _completed(task_index, summary=f"result-{task_index}")
            finally:
                with lock:
                    state["active"] -= 1

        wf = {"steps": [{"parallel": [{"goal": GOOD}] * 6}]}
        with (
            patch("tools.delegate_tool._get_max_concurrent_children", return_value=2),
            patch("tools.delegate_tool._build_child_preserving_parent_tools",
                  return_value=MagicMock()),
            patch("tools.delegate_tool._run_single_child", side_effect=fake_run),
        ):
            result = _call(wf, parent=parent)

        self.assertNotIn("error", result)
        self.assertEqual(result["mode"], "workflow")
        self.assertEqual(result["max_concurrent"], 2)
        self.assertEqual(len(result["results"]), 6)
        self.assertTrue(all(r["status"] == "completed" for r in result["results"]))
        # Parallelism actually happened, but never beyond the semaphore cap.
        self.assertGreater(state["max_active"], 1)
        self.assertLessEqual(state["max_active"], 2)
        # Results preserve input order (task_index is the global item index).
        self.assertEqual([r["task_index"] for r in result["results"]], list(range(6)))
        for r in result["results"]:
            self.assertEqual(r["step_kind"], "parallel")
            self.assertEqual(r["step_index"], 0)

    def test_item_error_does_not_kill_batch(self):
        """A raising item becomes a structured error entry; the rest continue."""
        parent = _make_mock_parent()
        calls = []

        def fake_run(task_index, goal, child, parent_agent, **_kwargs):
            calls.append(task_index)
            if task_index == 1:
                raise RuntimeError("child exploded")
            return _completed(task_index, summary=f"ok-{task_index}")

        wf = {"steps": [{"parallel": [{"goal": GOOD}, {"goal": GOOD}, {"goal": GOOD}]}]}
        with (
            patch("tools.delegate_tool._build_child_preserving_parent_tools",
                  return_value=MagicMock()),
            patch("tools.delegate_tool._run_single_child", side_effect=fake_run),
        ):
            result = _call(wf, parent=parent)

        # Every item was attempted; the batch survived the failure.
        self.assertEqual(sorted(calls), [0, 1, 2])
        self.assertEqual(len(result["results"]), 3)
        by_index = {r["task_index"]: r for r in result["results"]}
        self.assertEqual(by_index[1]["status"], "error")
        self.assertIn("child exploded", by_index[1]["error"])
        self.assertEqual(by_index[0]["status"], "completed")
        self.assertEqual(by_index[2]["status"], "completed")

    def test_non_completed_status_normalized_to_error(self):
        """failed/interrupted entries from the runner collapse to status=error."""
        parent = _make_mock_parent()

        def fake_run(task_index, goal, child, parent_agent, **_kwargs):
            if task_index == 0:
                return {
                    "task_index": 0,
                    "status": "failed",
                    "summary": "",
                    "exit_reason": "max_iterations",
                    "api_calls": 3,
                    "duration_seconds": 2.0,
                    "_child_role": None,
                    "_child_cost_usd": 0.0,
                }
            return _completed(task_index)

        wf = {"steps": [{"parallel": [{"goal": GOOD}, {"goal": GOOD}]}]}
        with (
            patch("tools.delegate_tool._build_child_preserving_parent_tools",
                  return_value=MagicMock()),
            patch("tools.delegate_tool._run_single_child", side_effect=fake_run),
        ):
            result = _call(wf, parent=parent)

        by_index = {r["task_index"]: r for r in result["results"]}
        self.assertEqual(by_index[0]["status"], "error")
        self.assertEqual(by_index[0]["exit_reason"], "max_iterations")
        self.assertEqual(by_index[1]["status"], "completed")


class TestWorkflowPipeline(unittest.TestCase):
    def _run_pipeline(self, side_effect, stages):
        parent = _make_mock_parent()
        built_contexts = []

        def fake_build(**kwargs):
            built_contexts.append(kwargs.get("context"))
            return MagicMock()

        with (
            patch("tools.delegate_tool._build_child_preserving_parent_tools",
                  side_effect=fake_build),
            patch("tools.delegate_tool._run_single_child", side_effect=side_effect),
        ):
            result = _call(
                {"steps": [{"pipeline": [{"goal": GOOD} for _ in stages]}]},
                parent=parent,
            )
        return result, built_contexts

    def test_pipeline_chains_stage_output_into_next_context(self):
        """Output of stage N must appear in the context of stage N+1's prompts."""
        stage_outputs = ["STAGE-0-OUT", "STAGE-1-OUT", "STAGE-2-OUT"]
        side_effect = [
            _completed(i, summary=stage_outputs[i]) for i in range(3)
        ]
        result, built_contexts = self._run_pipeline(side_effect, stages=[1, 2, 3])

        self.assertNotIn("error", result)
        self.assertEqual([r["task_index"] for r in result["results"]], [0, 1, 2])
        for r in result["results"]:
            self.assertEqual(r["step_kind"], "pipeline")
            self.assertEqual(r["step_index"], 0)
        # First stage: no previous output. Later stages: previous output present.
        self.assertNotIn("STAGE-0-OUT", built_contexts[0])
        self.assertIn("STAGE-0-OUT", built_contexts[1])
        self.assertIn("STAGE-1-OUT", built_contexts[2])

    def test_pipeline_failed_stage_feeds_error_forward_and_continues(self):
        """A failed stage must not abort the pipeline: its error text is
        passed to the next stage's context and later stages still run."""
        def side_effect(task_index, goal, child, parent_agent, **_kwargs):
            if task_index == 0:
                raise RuntimeError("stage-0 exploded")
            return _completed(task_index, summary=f"stage-{task_index}-ok")

        result, built_contexts = self._run_pipeline(side_effect, stages=[1, 2, 3])

        by_index = {r["task_index"]: r for r in result["results"]}
        self.assertEqual(by_index[0]["status"], "error")
        self.assertIn("stage-0 exploded", by_index[0]["error"])
        # Later stages ran and received the failure context.
        self.assertEqual(by_index[1]["status"], "completed")
        self.assertEqual(by_index[2]["status"], "completed")
        self.assertIn("stage-0 exploded", built_contexts[1])

    def test_pipeline_stages_are_strictly_sequential(self):
        """Pipeline stages never overlap: stage N+1 starts after stage N ends."""
        parent = _make_mock_parent()
        lock = threading.Lock()
        state = {"active": 0, "max_active": 0}
        entered_order = []

        def fake_run(task_index, goal, child, parent_agent, **_kwargs):
            with lock:
                state["active"] += 1
                state["max_active"] = max(state["max_active"], state["active"])
                entered_order.append(task_index)
            time.sleep(0.03)
            try:
                return _completed(task_index, summary=f"stage-{task_index}")
            finally:
                with lock:
                    state["active"] -= 1

        with (
            patch("tools.delegate_tool._build_child_preserving_parent_tools",
                  return_value=MagicMock()),
            patch("tools.delegate_tool._run_single_child", side_effect=fake_run),
        ):
            result = _call(
                {"steps": [{"pipeline": [{"goal": GOOD} for _ in range(4)]}]},
                parent=parent,
            )

        self.assertEqual(state["max_active"], 1)
        self.assertEqual(entered_order, [0, 1, 2, 3])
        self.assertEqual(len(result["results"]), 4)


class TestWorkflowComposition(unittest.TestCase):
    def test_mixed_steps_run_in_order_with_correct_metadata(self):
        """parallel step then pipeline step: flat ordered results, per-step
        metadata intact, item cap enforced across the WHOLE workflow."""
        parent = _make_mock_parent()

        def fake_run(task_index, goal, child, parent_agent, **_kwargs):
            return _completed(task_index, summary=f"item-{task_index}")

        wf = {
            "steps": [
                {"parallel": [{"goal": GOOD}, {"goal": GOOD}]},
                {"pipeline": [{"goal": GOOD}, {"goal": GOOD}]},
            ]
        }
        with (
            patch("tools.delegate_tool._build_child_preserving_parent_tools",
                  return_value=MagicMock()),
            patch("tools.delegate_tool._run_single_child", side_effect=fake_run),
        ):
            result = _call(wf, parent=parent)

        self.assertNotIn("error", result)
        self.assertEqual(len(result["results"]), 4)
        self.assertEqual([r["task_index"] for r in result["results"]], [0, 1, 2, 3])
        kinds = [(r["step_index"], r["step_kind"]) for r in result["results"]]
        self.assertEqual(kinds, [(0, "parallel"), (0, "parallel"),
                                 (1, "pipeline"), (1, "pipeline")])

    def test_workflow_wins_over_goal_and_tasks(self):
        """When workflow is provided, top-level goal/tasks are ignored."""
        parent = _make_mock_parent()

        def fake_run(task_index, goal, child, parent_agent, **_kwargs):
            return _completed(task_index)

        with (
            patch("tools.delegate_tool._build_child_preserving_parent_tools",
                  return_value=MagicMock()),
            patch("tools.delegate_tool._run_single_child", side_effect=fake_run),
        ):
            result = json.loads(
                delegate_task(
                    goal="ignore me",
                    tasks=[{"goal": "ignore me too"}],
                    workflow={"steps": [{"parallel": [{"goal": GOOD}]}]},
                    parent_agent=parent,
                )
            )
        self.assertNotIn("error", result)
        self.assertEqual(len(result["results"]), 1)


class TestWorkflowSchema(unittest.TestCase):
    def test_schema_exposes_workflow_param(self):
        props = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
        self.assertIn("workflow", props)
        wf_props = props["workflow"]["properties"]["steps"]["items"]["properties"]
        self.assertIn("parallel", wf_props)
        self.assertIn("pipeline", wf_props)
        item_props = wf_props["parallel"]["items"]["properties"]
        self.assertIn("goal", item_props)
        self.assertIn("context", item_props)
        self.assertIn("role", item_props)
        # v1 limitation: workflow items are goal/context/role only.
        self.assertNotIn("output_schema", item_props)

    def test_dynamic_overrides_inject_current_limits(self):
        with patch("tools.delegate_tool._get_max_concurrent_children", return_value=10):
            overrides = _build_dynamic_schema_overrides()
        wf_desc = overrides["parameters"]["properties"]["workflow"]["description"]
        self.assertIn("parallel", wf_desc)
        self.assertIn("pipeline", wf_desc)
        self.assertIn("up to 8 at a time", wf_desc)  # min(8, 10)
        self.assertIn(str(WORKFLOW_MAX_ITEMS), wf_desc)

    def test_registry_definition_includes_workflow(self):
        """The registry get_definitions() pass must ship the workflow param
        with the user's actual limits (same path the model sees)."""
        from tools.registry import registry

        with (
            patch("tools.delegate_tool._get_max_concurrent_children", return_value=7),
            patch("tools.delegate_tool._get_max_spawn_depth", return_value=4),
            patch("tools.delegate_tool._get_orchestrator_enabled", return_value=True),
        ):
            definition = registry.get_definitions({"delegate_task"})[0]["function"]
        props = definition["parameters"]["properties"]
        self.assertIn("workflow", props)
        self.assertIn("up to 7", props["tasks"]["description"])
        self.assertIn("up to 7 at a time", props["workflow"]["description"])

    def test_top_level_description_stays_within_budget(self):
        desc = _build_top_level_description()
        self.assertLessEqual(len(desc), 2200)
        self.assertIn("workflow", desc)

    def test_workflow_max_concurrent_clamped(self):
        with patch("tools.delegate_tool._get_max_concurrent_children", return_value=10):
            self.assertEqual(workflow_max_concurrent(), 8)
        with patch("tools.delegate_tool._get_max_concurrent_children", return_value=2):
            self.assertEqual(workflow_max_concurrent(), 2)
        with patch("tools.delegate_tool._get_max_concurrent_children", return_value=0):
            self.assertEqual(workflow_max_concurrent(), 1)


class TestWorkflowModelHiddenFields(unittest.TestCase):
    def test_hidden_fields_stripped_from_items(self):
        wf = {
            "steps": [
                {"parallel": [{"goal": GOOD, "acp_command": "x", "acp_args": ["y"]}]},
                {"pipeline": [{"goal": GOOD}]},
            ]
        }
        stripped = _strip_workflow_model_hidden_fields(wf)
        self.assertNotIn("acp_command", stripped["steps"][0]["parallel"][0])
        self.assertNotIn("acp_args", stripped["steps"][0]["parallel"][0])
        self.assertEqual(stripped["steps"][0]["parallel"][0]["goal"], GOOD)
        # Unchanged steps returned as-is (identity preserved when nothing stripped).
        unchanged = {"steps": [{"pipeline": [{"goal": GOOD}]}]}
        self.assertIs(_strip_workflow_model_hidden_fields(unchanged), unchanged)


if __name__ == "__main__":
    unittest.main()
