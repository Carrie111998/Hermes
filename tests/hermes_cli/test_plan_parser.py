"""Tests for hermes_cli/plan_parser.py — Plan File v2 Contract parser.

The v2 contract is documented in
``skills/software-development/plan/SKILL.md`` (Plan File v2 Contract
section). These tests pin the parser behaviour so H-22 can rely on it.

Refs: H-20 (hermes-v2 plan, 2026-07-20).
"""

from __future__ import annotations

import pytest

from hermes_cli.plan_parser import (
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

    def test_rejects_text_starting_with_dashes_but_no_yaml(self) -> None:
        # ``--`` alone is not frontmatter.
        assert is_v2_plan("---\nnot a yaml mapping\n---\n") is False


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

    def test_missing_required_key_raises(self) -> None:
        bad = """\
---
title: Only title
goal: A goal
scope_tiers:
  A: []
risks: []
verification: []
---

```tasks
- [ ] T1: something
```
"""
        with pytest.raises(ValueError, match="missing required frontmatter key: slug"):
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

    def test_slug_with_spaces_flagged(self) -> None:
        plan = ParsedPlan(
            slug="bad slug", title="t", goal="g",
            scope_tiers={}, risks=[], verification=[],
        )
        errors = validate(plan)
        assert any(e.code == "slug_invalid" for e in errors)

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
