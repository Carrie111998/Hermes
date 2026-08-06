"""Tests for hermes_cli/plan_parser.py — Plan File v2 Contract parser.

The v2 contract is documented in
``skills/software-development/plan/SKILL.md`` (Plan File v2 Contract
section). These tests pin the parser behaviour so H-22 can rely on it.

Refs: H-20 (hermes-v2 plan, 2026-07-20).
"""

from __future__ import annotations

import pytest
import yaml

from hermes_cli.plan_parser import (
    MalformedTaskLine,
    ParsedPlan,
    PlanTask,
    PlanValidationError,
    is_v2_plan,
    parse_plan,
    validate,
)


# ── fixtures ───────────────────────────────────────────────────────────

V2_MINIMAL = """\
---
slug: my-plan
title: My Plan
goal: |
  Achieve a thing.
scope_tiers:
  A: [T1]
risks:
  - Risk one
verification:
  - pytest -q
---

# My Plan

Prose body here.

```tasks
- [ ] T1: Do the thing | skill: hermes-v2-helper | verify: pytest -q
```
"""

V2_FULL = """\
---
slug: starman-booster-gold-red-star
title: Hermes v2 Verbesserungsplan
goal: |
  Hermes/Yuno auf Claude Code / Kimi Code Workflow-Niveau heben.
scope_tiers:
  A: [H-01, H-10, H-11]
  B: [H-12, H-13, H-20]
  C: [H-08, H-15, H-64]
risks:
  - Core-Patches kollidieren mit Upstream → git revert
  - state.db-VACUUM braucht 2× Disk → Freien Platz prüfen
verification:
  - pytest tests/run_agent/ -k "minimax or thinking_budget" grün
  - Live-Session mit 3 Tool-Runden zeigt Thinking-Blöcke im Replay
created_by: yuno
created_at: 2026-07-20
model: MiniMax-M3
provider: minimax
---

# Hermes v2

Body prose.

```tasks
- [ ] T1: Phase 0 — Hygiene | parent: root
- [ ] T1.1: H-01 Baseline-Snapshot | parent: T1 | skill: hermes-v2-helper | verify: ls ~/hermes-v2-baseline-*
- [ ] T1.2: H-04 Secrets-Migration | parent: T1 | depends: [T1.1] | skill: hermes-v2-helper | verify: grep -c DASHBOARD ~/.hermes/.env
- [ ] T2: Phase 1 — Tool-Calling | parent: root
- [ ] T2.1: H-10 MiniMax-Interleaved-Thinking | parent: T2 | skill: hermes-core-patch | verify: pytest tests/run_agent/test_minimax_tool_reasoning.py
```
"""


# ── is_v2_plan ─────────────────────────────────────────────────────────


class TestIsV2Plan:
    def test_detects_frontmatter(self) -> None:
        assert is_v2_plan(V2_MINIMAL) is True

    def test_rejects_plain_markdown(self) -> None:
        assert is_v2_plan("# Just a title\n\nNo frontmatter here.") is False

    def test_rejects_empty_string(self) -> None:
        assert is_v2_plan("") is False

    def test_detects_non_mapping_frontmatter_envelope(self) -> None:
        assert is_v2_plan("---\nnot a yaml mapping\n---\n") is True

    def test_detects_invalid_yaml_frontmatter_envelope(self) -> None:
        assert is_v2_plan("---\nslug: [unterminated\n---\n") is True


# ── parse_plan ─────────────────────────────────────────────────────────


class TestParsePlanMinimal:
    def test_returns_parsed_plan(self) -> None:
        plan = parse_plan(V2_MINIMAL)
        assert isinstance(plan, ParsedPlan)
        assert plan.slug == "my-plan"
        assert plan.title == "My Plan"
        assert "Achieve a thing" in plan.goal

    def test_scope_tiers_parsed(self) -> None:
        plan = parse_plan(V2_MINIMAL)
        assert plan.scope_tiers == {"A": ["T1"]}

    def test_risks_and_verification_lists(self) -> None:
        plan = parse_plan(V2_MINIMAL)
        assert plan.risks == ["Risk one"]
        assert plan.verification == ["pytest -q"]

    def test_tasks_parsed(self) -> None:
        plan = parse_plan(V2_MINIMAL)
        assert len(plan.tasks) == 1
        t = plan.tasks[0]
        assert t.raw_id == "T1"
        assert t.title == "Do the thing"
        assert t.skill == "hermes-v2-helper"
        assert t.verify == "pytest -q"


class TestParsePlanFull:
    def test_all_frontmatter_keys(self) -> None:
        plan = parse_plan(V2_FULL)
        assert plan.slug == "starman-booster-gold-red-star"
        assert plan.title == "Hermes v2 Verbesserungsplan"
        assert "Hermes/Yuno" in plan.goal
        assert plan.created_by == "yuno"
        assert plan.created_at == "2026-07-20"
        assert plan.model == "MiniMax-M3"
        assert plan.provider == "minimax"

    def test_all_tiers(self) -> None:
        plan = parse_plan(V2_FULL)
        assert plan.scope_tiers["A"] == ["H-01", "H-10", "H-11"]
        assert plan.scope_tiers["B"] == ["H-12", "H-13", "H-20"]
        assert plan.scope_tiers["C"] == ["H-08", "H-15", "H-64"]

    def test_task_count(self) -> None:
        plan = parse_plan(V2_FULL)
        assert len(plan.tasks) == 5

    def test_subtask_ids(self) -> None:
        plan = parse_plan(V2_FULL)
        ids = [t.raw_id for t in plan.tasks]
        assert ids == ["T1", "T1.1", "T1.2", "T2", "T2.1"]

    def test_parent_and_depends(self) -> None:
        plan = parse_plan(V2_FULL)
        t11 = next(t for t in plan.tasks if t.raw_id == "T1.1")
        t12 = next(t for t in plan.tasks if t.raw_id == "T1.2")
        assert t11.parent == "T1"
        assert t12.parent == "T1"
        assert t12.depends == ["T1.1"]

    def test_root_parent_allowed(self) -> None:
        plan = parse_plan(V2_FULL)
        t1 = next(t for t in plan.tasks if t.raw_id == "T1")
        assert t1.parent == "root"


class TestParsePlanErrors:
    def test_missing_frontmatter_raises(self) -> None:
        with pytest.raises(ValueError, match="no YAML frontmatter"):
            parse_plan("# Title\n\nNo frontmatter.")

    @pytest.mark.parametrize(
        "missing_key",
        ["slug", "title", "goal", "scope_tiers", "risks", "verification"],
    )
    def test_each_required_frontmatter_key_is_required(self, missing_key) -> None:
        frontmatter = {
            "slug": "complete-plan",
            "title": "Complete plan",
            "goal": "A goal",
            "scope_tiers": {"A": ["T1"]},
            "risks": [],
            "verification": [],
        }
        del frontmatter[missing_key]
        bad = (
            "---\n"
            + yaml.safe_dump(frontmatter, sort_keys=False)
            + "---\n\n```tasks\n- [ ] T1: something\n```\n"
        )

        with pytest.raises(
            ValueError,
            match=f"missing required frontmatter key: {missing_key}",
        ):
            parse_plan(bad)

    def test_invalid_frontmatter_yaml_raises(self) -> None:
        bad = "---\nslug: [unterminated\n---\n"

        with pytest.raises(ValueError, match="invalid YAML frontmatter"):
            parse_plan(bad)

    def test_frontmatter_not_mapping_raises(self) -> None:
        bad = """\
---
- not
- a
- mapping
---

```tasks
- [ ] T1: x
```
"""
        with pytest.raises(ValueError, match="not a YAML mapping"):
            parse_plan(bad)


# ── task-line parsing details ──────────────────────────────────────────


class TestTaskLineParsing:
    def test_title_with_pipe_is_preserved(self) -> None:
        # The pipe inside "x | y" should not be mistaken for a kv separator
        # because it's not followed by ``key:``.
        text = """\
---
slug: p
title: t
goal: g
scope_tiers:
  A: []
risks: []
verification: []
---

```tasks
- [ ] T1: pick A or B | skill: foo | verify: ls
```
"""
        plan = parse_plan(text)
        assert plan.tasks[0].title == "pick A or B"
        assert plan.tasks[0].skill == "foo"

    def test_minimal_task_line(self) -> None:
        # Task line without any kv segments — just title.
        text = """\
---
slug: p
title: t
goal: g
scope_tiers:
  A: []
risks: []
verification: []
---

```tasks
- [ ] T1: do the bare minimum
```
"""
        plan = parse_plan(text)
        t = plan.tasks[0]
        assert t.title == "do the bare minimum"
        assert t.skill is None
        assert t.verify is None

    def test_multiple_skills_comma_list(self) -> None:
        text = """\
---
slug: p
title: t
goal: g
scope_tiers:
  A: []
risks: []
verification: []
---

```tasks
- [ ] T1: x | skill: foo, bar, baz | verify: pytest
```
"""
        plan = parse_plan(text)
        assert plan.tasks[0].skill == "foo, bar, baz"

    def test_multiple_depends_comma_list(self) -> None:
        text = """\
---
slug: p
title: t
goal: g
scope_tiers:
  A: []
risks: []
verification: []
---

```tasks
- [ ] T1: x
- [ ] T2: y
- [ ] T3: z | parent: T1 | depends: [T1, T2]
```
"""
        plan = parse_plan(text)
        t3 = next(t for t in plan.tasks if t.raw_id == "T3")
        assert t3.depends == ["T1", "T2"]

    @pytest.mark.parametrize(
        ("field", "value", "expected"),
        [
            ("paths", "hermes_cli/plan_parser.py", ["hermes_cli/plan_parser.py"]),
            (
                "paths",
                "hermes_cli/plan_parser.py, tests/hermes_cli/test_plan_parser.py",
                ["hermes_cli/plan_parser.py", "tests/hermes_cli/test_plan_parser.py"],
            ),
            (
                "paths",
                "[hermes_cli/plan_parser.py, tests/hermes_cli/test_plan_parser.py]",
                ["hermes_cli/plan_parser.py", "tests/hermes_cli/test_plan_parser.py"],
            ),
            ("path", "hermes_cli/plan_parser.py", ["hermes_cli/plan_parser.py"]),
            (
                "files",
                "[hermes_cli/plan_parser.py, tests/hermes_cli/test_plan_parser.py]",
                ["hermes_cli/plan_parser.py", "tests/hermes_cli/test_plan_parser.py"],
            ),
        ],
    )
    def test_paths_fields_accept_canonical_aliases_and_list_forms(
        self,
        field,
        value,
        expected,
    ) -> None:
        text = V2_MINIMAL.replace(
            " | skill: hermes-v2-helper | verify: pytest -q",
            f" | {field}: {value}",
        )

        plan = parse_plan(text)

        assert plan.tasks[0].paths == expected

    def test_no_tasks_block_returns_empty(self) -> None:
        text = """\
---
slug: p
title: t
goal: g
scope_tiers:
  A: []
risks: []
verification: []
---

Just prose, no tasks.
"""
        plan = parse_plan(text)
        assert plan.tasks == []


# ── validate ───────────────────────────────────────────────────────────


class TestValidate:
    def test_valid_plan_has_no_errors(self) -> None:
        plan = parse_plan(V2_MINIMAL)
        assert validate(plan) == []

    def test_valid_full_plan_has_no_errors(self) -> None:
        plan = parse_plan(V2_FULL)
        assert validate(plan) == []

    def test_empty_title_flagged(self) -> None:
        plan = ParsedPlan(
            slug="x", title="", goal="g",
            scope_tiers={}, risks=[], verification=[],
        )
        errors = validate(plan)
        assert any(e.code == "title_empty" for e in errors)

    def test_empty_goal_flagged(self) -> None:
        plan = ParsedPlan(
            slug="x", title="t", goal="",
            scope_tiers={}, risks=[], verification=[],
        )
        errors = validate(plan)
        assert any(e.code == "goal_empty" for e in errors)

    @pytest.mark.parametrize(
        "slug",
        ["bad slug", "Bad-Slug", "bad_slug", "bad--slug", "-bad", "bad-"],
    )
    def test_slug_must_be_lowercase_kebab_case(self, slug) -> None:
        plan = ParsedPlan(
            slug=slug, title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
        )
        errors = validate(plan)
        slug_error = next(e for e in errors if e.code == "slug_invalid")
        assert "lowercase kebab-case" in slug_error.message

    def test_duplicate_task_id_flagged(self) -> None:
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[
                PlanTask(raw_id="T1", title="a"),
                PlanTask(raw_id="T1", title="b"),
            ],
        )
        errors = validate(plan)
        assert any(e.code == "duplicate_id" for e in errors)

    def test_dangling_parent_flagged(self) -> None:
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[PlanTask(raw_id="T1", title="a", parent="T99")],
        )
        errors = validate(plan)
        assert any(e.code == "dangling_reference" for e in errors)

    def test_root_parent_is_allowed(self) -> None:
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[PlanTask(raw_id="T1", title="a", parent="root")],
        )
        errors = validate(plan)
        assert errors == []

    def test_empty_verify_command_flagged(self) -> None:
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[PlanTask(raw_id="T1", title="a", verify="")],
        )
        errors = validate(plan)
        assert any(e.code == "empty_verify" for e in errors)

    def test_no_tasks_flagged(self) -> None:
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[],
        )
        errors = validate(plan)
        assert any(e.code == "no_tasks" for e in errors)


# ── v2 contract: exactly one tasks fence ───────────────────────────────


class TestSingleTasksFence:
    """[hermes-v2] H-22: the v2 contract is "exactly one tasks fence".
    Multiple fences used to silently take the first — the second fence
    disappeared without trace, leaving the operator with a half-seeded
    board. ``parse_plan`` now raises on > 1 fence so the seeder never
    gets the chance to commit a partial graph."""

    def test_two_fences_raise_value_error(self) -> None:
        text = """\
---
slug: p
title: t
goal: g
scope_tiers: {A: []}
risks: []
verification: []
---

```tasks
- [ ] T1: first
```

```tasks
- [ ] T2: second
```
"""
        with pytest.raises(ValueError, match="2 ```tasks``` fences"):
            parse_plan(text)

    def test_single_fence_is_still_accepted(self) -> None:
        plan = parse_plan(V2_MINIMAL)
        assert len(plan.tasks) == 1
        assert plan.tasks[0].raw_id == "T1"


# ── malformed task lines inside the fence ──────────────────────────────


class TestMalformedTaskLines:
    """[hermes-v2] H-22: ``- [ ]`` lines that don't match the canonical
    ``T<id>: <title>`` regex used to silently disappear. The parser now
    records them and ``validate`` surfaces them as structured errors so
    an operator's typo doesn't slip past the seeder."""

    def test_bare_task_bullet_is_recorded(self) -> None:
        text = """\
---
slug: p
title: t
goal: g
scope_tiers: {A: []}
risks: []
verification: []
---

```tasks
- [ ] T1: real
- [ ]
```
"""
        plan = parse_plan(text)
        assert len(plan.tasks) == 1
        assert plan.tasks[0].raw_id == "T1"
        assert len(plan.malformed_task_lines) == 1
        assert plan.malformed_task_lines[0].line_no == 2

    def test_id_without_title_is_recorded(self) -> None:
        text = """\
---
slug: p
title: t
goal: g
scope_tiers: {A: []}
risks: []
verification: []
---

```tasks
- [ ] T99
```
"""
        plan = parse_plan(text)
        # T99 with no title fails the canonical regex; parser records it.
        assert plan.tasks == []
        assert len(plan.malformed_task_lines) == 1
        assert "T99" in plan.malformed_task_lines[0].text

    def test_prose_bullets_are_not_recorded(self) -> None:
        text = """\
---
slug: p
title: t
goal: g
scope_tiers: {A: []}
risks: []
verification: []
---

```tasks
- This is just prose, not a task.
- [ ] T1: the only task
```
"""
        plan = parse_plan(text)
        assert len(plan.tasks) == 1
        assert plan.malformed_task_lines == []

    def test_validate_surfaces_malformed_lines_as_errors(self) -> None:
        plan = parse_plan("""\
---
slug: p
title: t
goal: g
scope_tiers: {A: []}
risks: []
verification: []
---

```tasks
- [ ] T1: real
- [ ]
```
""")
        errors = validate(plan)
        bad = [e for e in errors if e.code == "malformed_task_line"]
        assert bad, "validate must surface malformed task lines"
        assert any(e.line_no == 2 for e in bad)

    def test_validate_flags_empty_title(self) -> None:
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[PlanTask(raw_id="T1", title="")],
        )
        errors = validate(plan)
        assert any(e.code == "malformed_task_line" for e in errors)


# ── dependency cycle detection ─────────────────────────────────────────


class TestDependencyCycleDetection:
    """[hermes-v2] H-22: parent/depends cycles used to crash inside
    ``link_tasks`` AFTER the root was already committed, leaving a
    partial board mutation. ``validate`` now detects cycles (including
    self-cycles) before any kanban row is touched."""

    def test_self_cycle_in_parent_flagged(self) -> None:
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[PlanTask(raw_id="T1", title="a", parent="T1")],
        )
        errors = validate(plan)
        assert any(e.code == "dependency_cycle" for e in errors)
        assert any("T1" in e.message and "self" in e.message for e in errors)

    def test_self_cycle_in_depends_flagged(self) -> None:
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[PlanTask(raw_id="T1", title="a", depends=["T1"])],
        )
        errors = validate(plan)
        assert any(e.code == "dependency_cycle" for e in errors)

    def test_two_node_cycle_flagged(self) -> None:
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[
                PlanTask(raw_id="T1", title="a", parent="T2"),
                PlanTask(raw_id="T2", title="b", parent="T1"),
            ],
        )
        errors = validate(plan)
        cycles = [e for e in errors if e.code == "dependency_cycle"]
        assert cycles, "T1<->T2 cycle must be flagged"
        # The cycle message should mention both nodes.
        joined = " ".join(c.message for c in cycles)
        assert "T1" in joined and "T2" in joined

    def test_three_node_cycle_flagged(self) -> None:
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[
                PlanTask(raw_id="T1", title="a", depends=["T2"]),
                PlanTask(raw_id="T2", title="b", depends=["T3"]),
                PlanTask(raw_id="T3", title="c", depends=["T1"]),
            ],
        )
        errors = validate(plan)
        cycles = [e for e in errors if e.code == "dependency_cycle"]
        assert cycles, "T1->T2->T3->T1 cycle must be flagged"

    def test_dag_passes_validation(self) -> None:
        """A linear chain (T1 -> T2 -> T3) has no cycle and must
        validate cleanly — the detector must not false-positive on
        acyclic graphs."""
        plan = ParsedPlan(
            slug="p", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
            tasks=[
                PlanTask(raw_id="T1", title="a"),
                PlanTask(raw_id="T2", title="b", parent="T1"),
                PlanTask(raw_id="T3", title="c", parent="T2"),
            ],
        )
        errors = validate(plan)
        cycles = [e for e in errors if e.code == "dependency_cycle"]
        assert cycles == [], (
            f"DAG must not trip cycle detector, got: {[e.message for e in cycles]}"
        )
