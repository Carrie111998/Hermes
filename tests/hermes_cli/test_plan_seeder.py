"""Tests for hermes_cli/plan_seeder.py — plan → kanban seeding.

Refs: H-22 (hermes-v2 plan, 2026-07-20). The seeder is the
integration point that the kimi-mode plugin's ``/plan approve`` slash
command calls to materialise an approved plan as a Kanban board.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest


# ── shared fixtures ────────────────────────────────────────────────────


@pytest.fixture
def fresh_home(tmp_path, monkeypatch):
    """Isolated HERMES_HOME — mirrors tests/hermes_cli/test_kanban_boards.py."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in (
        "HERMES_KANBAN_DB",
        "HERMES_KANBAN_WORKSPACES_ROOT",
        "HERMES_KANBAN_HOME",
        "HERMES_KANBAN_BOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    try:
        import hermes_constants
        hermes_constants._cached_default_hermes_root = None  # type: ignore[attr-defined]
    except Exception:
        pass
    from hermes_cli import kanban_db as kb
    kb._INITIALIZED_PATHS.clear()
    return home


V2_MINIMAL = """\
---
slug: my-plan
title: My Test Plan
goal: |
  A short goal.
scope_tiers:
  A: [T1]
risks:
  - Risk one
verification:
  - pytest -q
---

# My Test Plan

Prose.

```tasks
- [ ] T1: Do the thing | skill: hermes-v2-helper | verify: pytest -q
```
"""


V2_HIERARCHICAL = """\
---
slug: hier-plan
title: Hierarchical Plan
goal: |
  Multi-phase plan.
scope_tiers:
  A: [T1, T2]
  B: [T3]
risks:
  - R1
verification:
  - pytest -q
---

Body.

```tasks
- [ ] T1: Phase 1 | parent: root
- [ ] T1.1: H-01 Snapshot | parent: T1 | verify: ls backup
- [ ] T1.2: H-04 Secrets | parent: T1 | depends: [T1.1] | verify: grep X .env
- [ ] T2: Phase 2 | parent: root
- [ ] T3: Phase 3 | parent: root
```
"""


V2_INVALID = """\
---
slug: bad
title: Bad plan
goal: |
  An invalid plan — duplicate task ids.
scope_tiers:
  A: []
risks: []
verification: []
---

Body.

```tasks
- [ ] T1: First
- [ ] T1: Duplicate
```
"""


FREEFORM_PLAIN = """\
# Free-form plan

Just prose, no frontmatter, no tasks block.

This should fall back to a single triage task.
"""


# ── tests ──────────────────────────────────────────────────────────────


class TestSeedV2Minimal:
    def test_creates_root_task(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_MINIMAL)
        result = seed_plan_to_kanban(plan_file)
        assert result.fallback is False
        assert result.root_task_id.startswith("t_")

        with kb.connect_closing() as conn:
            root = kb.get_task(conn, result.root_task_id)
        assert root.title == "My Test Plan"
        # Root is parked in "blocked" (the dispatcher-inert safe state
        # when a task has no parents — see _seed_v2_plan docstring).
        assert root.status == "blocked"
        assert root.assignee is None

    def test_creates_child_tasks(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_MINIMAL)
        result = seed_plan_to_kanban(plan_file)
        assert len(result.child_task_ids) == 1

        with kb.connect_closing() as conn:
            child = kb.get_task(conn, result.child_task_ids[0])
        assert "T1" in child.title
        assert "Do the thing" in child.title

    def test_child_is_unassigned(self, fresh_home, tmp_path) -> None:
        """The dispatcher-guard invariant: every seeded task must be
        unassigned, so the H-00 incident cannot repeat even if the
        root accidentally promotes to ready."""
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_MINIMAL)
        result = seed_plan_to_kanban(plan_file)
        with kb.connect_closing() as conn:
            child = kb.get_task(conn, result.child_task_ids[0])
        assert child.assignee is None
        # Status can be ready/todo/blocked depending on parent state —
        # but assignee=None is the safety contract, not the status.

    def test_root_has_attachment(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_MINIMAL)
        result = seed_plan_to_kanban(plan_file)
        with kb.connect_closing() as conn:
            attachments = kb.list_attachments(conn, result.root_task_id)
        assert len(attachments) == 1
        assert attachments[0].filename == "plan.md"


class TestSeedV2Hierarchical:
    def test_parent_links(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_HIERARCHICAL)
        result = seed_plan_to_kanban(plan_file)
        assert len(result.child_task_ids) == 5

        with kb.connect_closing() as conn:
            children = {kb.get_task(conn, tid).id: kb.get_task(conn, tid) for tid in result.child_task_ids}
            t1 = next(c for c in children.values() if "Phase 1" in c.title)
            t11 = next(c for c in children.values() if "H-01" in c.title)
            t12 = next(c for c in children.values() if "H-04" in c.title)
            # T1.1 (parent: T1) — single parent edge.
            t11_parents = kb.parent_ids(conn, t11.id)
            # T1.2 has TWO parent edges: parent: T1 + depends: [T1.1].
            # Both go into task_links — kanban doesn't distinguish
            # hierarchical parents from depends-on edges at the table
            # level; both are ordering constraints.
            t12_parents = kb.parent_ids(conn, t12.id)
        assert t11_parents == [t1.id]
        assert t1.id in t12_parents  # parent edge
        # T12 also depends on T1.1
        t11_id = t11.id
        assert any(p == t11_id for p in t12_parents)

    def test_depends_links(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_HIERARCHICAL)
        result = seed_plan_to_kanban(plan_file)
        with kb.connect_closing() as conn:
            children_by_id = {
                kb.get_task(conn, tid).title: kb.get_task(conn, tid)
                for tid in result.child_task_ids
            }
        t11 = next(c for c in children_by_id.values() if "H-01" in c.title)
        t12 = next(c for c in children_by_id.values() if "H-04" in c.title)
        # T1.2 should be linked as depending on T1.1.
        # Use task_links query.
        with kb.connect_closing() as conn:
            links = conn.execute(
                "SELECT parent_id, child_id FROM task_links"
            ).fetchall()
        link_set = {(p, c) for p, c in links}
        assert (t11.id, t12.id) in link_set

    def test_priority_from_tier_a(self, fresh_home, tmp_path) -> None:
        """Root priority reflects the highest tier (A=10)."""
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_HIERARCHICAL)
        result = seed_plan_to_kanban(plan_file)
        with kb.connect_closing() as conn:
            root = kb.get_task(conn, result.root_task_id)
        assert root.priority == 10  # A tier


class TestSeedInvalidV2:
    def test_raises_plan_seed_error(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import PlanSeedError, seed_plan_to_kanban

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_INVALID)
        with pytest.raises(PlanSeedError) as exc_info:
            seed_plan_to_kanban(plan_file)
        assert any(e.code == "duplicate_id" for e in exc_info.value.errors)


class TestSeedFreeform:
    def test_fallback_to_single_triage_task(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "freeform.md"
        plan_file.write_text(FREEFORM_PLAIN)
        result = seed_plan_to_kanban(plan_file)
        assert result.fallback is True
        assert result.child_task_ids == []

        with kb.connect_closing() as conn:
            root = kb.get_task(conn, result.root_task_id)
        assert "Decompose" in root.title
        assert root.assignee is None

    def test_freeform_has_attachment(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "freeform.md"
        plan_file.write_text(FREEFORM_PLAIN)
        result = seed_plan_to_kanban(plan_file)
        with kb.connect_closing() as conn:
            attachments = kb.list_attachments(conn, result.root_task_id)
        assert len(attachments) == 1
        assert attachments[0].filename == "freeform.md"


class TestSeedIdempotency:
    def test_re_seed_returns_same_ids(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_MINIMAL)
        first = seed_plan_to_kanban(plan_file)
        second = seed_plan_to_kanban(plan_file)
        assert first.root_task_id == second.root_task_id
        assert first.child_task_ids == second.child_task_ids


class TestSeedErrors:
    def test_missing_file_raises(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban

        with pytest.raises(FileNotFoundError):
            seed_plan_to_kanban(tmp_path / "nope.md")
