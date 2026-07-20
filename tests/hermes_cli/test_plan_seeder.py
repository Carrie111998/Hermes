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
- [ ] T1: Do the thing | skill: hermes-v2-helper | verify: pytest -q | paths: [hermes_cli/plan_parser.py, tests/hermes_cli/test_plan_parser.py]
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
        assert (
            "**Paths:** `hermes_cli/plan_parser.py`, "
            "`tests/hermes_cli/test_plan_parser.py`"
        ) in child.body

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
    def test_re_seed_returns_same_ids_without_duplicate_side_effects(
        self, fresh_home, tmp_path
    ) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_HIERARCHICAL)
        first = seed_plan_to_kanban(plan_file)
        second = seed_plan_to_kanban(plan_file)

        assert first.root_task_id == second.root_task_id
        assert first.child_task_ids == second.child_task_ids
        assert second.idempotent_replay is True
        with kb.connect_closing() as conn:
            attachments = kb.list_attachments(conn, first.root_task_id)
            attached_events = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'attached'",
                (first.root_task_id,),
            ).fetchone()[0]
            links = conn.execute("SELECT COUNT(*) FROM task_links").fetchone()[0]
        assert len(attachments) == 1
        assert attached_events == 1
        # root→T1/T2/T3, T1→T1.1/T1.2, T1.1→T1.2
        assert links == 6

    def test_re_seed_changed_body_refreshes_attachment_and_size(
        self, fresh_home, tmp_path
    ) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_HIERARCHICAL)
        first = seed_plan_to_kanban(plan_file)
        with kb.connect_closing() as conn:
            first_attachment = kb.list_attachments(conn, first.root_task_id)[0]

        changed_body = V2_HIERARCHICAL.replace(
            "Multi-phase plan.",
            "Updated multi-phase plan body.",
        )
        plan_file.write_text(changed_body)
        replay = seed_plan_to_kanban(plan_file)

        with kb.connect_closing() as conn:
            attachments = kb.list_attachments(conn, first.root_task_id)
            attached_events = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'attached'",
                (first.root_task_id,),
            ).fetchone()[0]
        assert replay.root_task_id == first.root_task_id
        assert replay.idempotent_replay is True
        assert len(attachments) == 1
        assert attachments[0].id == first_attachment.id
        assert attachments[0].size == len(changed_body.encode("utf-8"))
        assert Path(attachments[0].stored_path).read_text(encoding="utf-8") == changed_body
        assert attached_events == 1

    def test_top_level_task_is_linked_to_root_and_parked(
        self, fresh_home, tmp_path
    ) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_MINIMAL)
        result = seed_plan_to_kanban(plan_file)
        with kb.connect_closing() as conn:
            child = kb.get_task(conn, result.child_task_ids[0])
            parents = kb.parent_ids(conn, child.id)
        assert parents == [result.root_task_id]
        assert child.status == "todo"

    def test_explicit_board_controls_db_and_attachment_paths(
        self, fresh_home, tmp_path
    ) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "plan.md"
        plan_file.write_text(V2_MINIMAL)
        result = seed_plan_to_kanban(plan_file, board="target-board")
        with kb.connect_closing(board="target-board") as conn:
            assert kb.get_task(conn, result.root_task_id) is not None
            attachment = kb.list_attachments(conn, result.root_task_id)[0]
        with kb.connect_closing() as conn:
            assert kb.get_task(conn, result.root_task_id) is None
        assert "target-board" in attachment.stored_path


class TestFreeformDecomposition:
    def test_freeform_is_created_in_triage(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "freeform.md"
        plan_file.write_text(FREEFORM_PLAIN)
        result = seed_plan_to_kanban(plan_file)
        with kb.connect_closing() as conn:
            root = kb.get_task(conn, result.root_task_id)
        assert root.status == "triage"
        assert root.assignee is None
        assert result.decomposition_attempted is False

    def test_optional_decomposition_reports_outcome(
        self, fresh_home, tmp_path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_decompose
        from hermes_cli.plan_seeder import seed_plan_to_kanban

        calls = {}

        def fake_decompose(task_id, *, author=None, timeout=None, board=None):
            calls.update(task_id=task_id, author=author, board=board)
            return SimpleNamespace(
                ok=True,
                reason="decomposed",
                child_ids=["t_child"],
            )

        monkeypatch.setattr(kanban_decompose, "decompose_task", fake_decompose)
        plan_file = tmp_path / "freeform.md"
        plan_file.write_text(FREEFORM_PLAIN)
        result = seed_plan_to_kanban(
            plan_file,
            board="target-board",
            decompose_freeform=True,
        )

        assert calls == {
            "task_id": result.root_task_id,
            "author": "kimi-mode",
            "board": "target-board",
        }
        assert result.decomposition_attempted is True
        assert result.decomposition_ok is True
        assert result.decomposition_message == "decomposed"
        assert result.child_task_ids == ["t_child"]

    def test_failed_optional_decomposition_reports_metadata(
        self, fresh_home, tmp_path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_decompose
        from hermes_cli.plan_seeder import seed_plan_to_kanban

        def fake_decompose(task_id, *, author=None, timeout=None, board=None):
            return SimpleNamespace(
                ok=False,
                reason="auxiliary client unavailable",
                child_ids=None,
            )

        monkeypatch.setattr(kanban_decompose, "decompose_task", fake_decompose)
        plan_file = tmp_path / "freeform.md"
        plan_file.write_text(FREEFORM_PLAIN)
        result = seed_plan_to_kanban(plan_file, decompose_freeform=True)

        assert result.fallback is True
        assert result.idempotent_replay is False
        assert result.decomposition_attempted is True
        assert result.decomposition_ok is False
        assert result.decomposition_message == "auxiliary client unavailable"
        assert result.child_task_ids == []

    def test_failed_decomposition_without_children_retries_on_replay(
        self, fresh_home, tmp_path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_decompose
        from hermes_cli.plan_seeder import seed_plan_to_kanban

        calls = []

        def fake_decompose(task_id, *, author=None, timeout=None, board=None):
            calls.append(task_id)
            if len(calls) == 1:
                return SimpleNamespace(ok=False, reason="transient failure", child_ids=None)
            return SimpleNamespace(ok=True, reason="retry succeeded", child_ids=["t_retry"])

        monkeypatch.setattr(kanban_decompose, "decompose_task", fake_decompose)
        plan_file = tmp_path / "freeform.md"
        plan_file.write_text(FREEFORM_PLAIN)

        first = seed_plan_to_kanban(plan_file, decompose_freeform=True)
        replay = seed_plan_to_kanban(plan_file, decompose_freeform=True)

        assert calls == [first.root_task_id, first.root_task_id]
        assert first.decomposition_ok is False
        assert replay.idempotent_replay is True
        assert replay.decomposition_attempted is True
        assert replay.decomposition_ok is True
        assert replay.child_task_ids == ["t_retry"]

    def test_already_decomposed_replay_returns_existing_children(
        self, fresh_home, tmp_path, monkeypatch
    ) -> None:
        """[hermes-v2] H-22: when ``decompose_task`` reports
        ``ok=True`` with ``reason='already decomposed: ...'`` (the
        replay short-circuit for an existing-child root), the seeder
        surfaces that as a successful replay with the existing child
        ids. The user-facing ``/plan approve`` message becomes a
        useful "(already decomposed)" replay instead of a fresh
        decomposition.
        """
        from hermes_cli import kanban_decompose
        from hermes_cli.plan_seeder import seed_plan_to_kanban

        captured = {}

        def fake_decompose(task_id, *, author=None, timeout=None, board=None):
            captured["task_id"] = task_id
            return SimpleNamespace(
                ok=True,
                reason="already decomposed: 2 existing child task(s); "
                "skipping re-decompose to preserve child ids",
                child_ids=["t_existing_a", "t_existing_b"],
                fanout=True,
            )

        monkeypatch.setattr(kanban_decompose, "decompose_task", fake_decompose)
        plan_file = tmp_path / "freeform.md"
        plan_file.write_text(FREEFORM_PLAIN)
        result = seed_plan_to_kanban(plan_file, decompose_freeform=True)

        assert captured["task_id"] == result.root_task_id
        assert result.decomposition_attempted is True
        assert result.decomposition_ok is True
        assert "already decomposed" in result.decomposition_message
        assert result.child_task_ids == ["t_existing_a", "t_existing_b"]

    def test_existing_child_links_skip_redecompose_and_reuse_ids(
        self, fresh_home, tmp_path, monkeypatch
    ) -> None:
        from hermes_cli import kanban_db as kb
        from hermes_cli.plan_seeder import seed_plan_to_kanban

        plan_file = tmp_path / "freeform.md"
        plan_file.write_text(FREEFORM_PLAIN)
        first = seed_plan_to_kanban(plan_file)
        with kb.connect_closing() as conn:
            child = kb.create_task(
                conn,
                title="existing child",
                parents=[first.root_task_id],
            )

        def should_not_retry(*args, **kwargs):
            raise AssertionError("successful freeform replay must not re-decompose")

        monkeypatch.setattr(
            "hermes_cli.kanban_decompose.decompose_task", should_not_retry
        )
        replay = seed_plan_to_kanban(plan_file, decompose_freeform=True)

        assert replay.root_task_id == first.root_task_id
        assert replay.child_task_ids == [child]
        assert replay.decomposition_attempted is False
        assert replay.decomposition_ok is True
        assert "already decomposed" in (replay.decomposition_message or "")


    def test_missing_file_raises(self, fresh_home, tmp_path) -> None:
        from hermes_cli.plan_seeder import seed_plan_to_kanban

        with pytest.raises(FileNotFoundError):
            seed_plan_to_kanban(tmp_path / "nope.md")

    def test_invalid_yaml_frontmatter_raises_plan_seed_error(
        self, fresh_home, tmp_path
    ) -> None:
        from hermes_cli.plan_seeder import PlanSeedError, seed_plan_to_kanban

        plan_file = tmp_path / "malformed.md"
        plan_file.write_text("---\nslug: [unterminated\n---\n")
        with pytest.raises(PlanSeedError) as exc_info:
            seed_plan_to_kanban(plan_file)
        assert exc_info.value.errors[0].code == "frontmatter_invalid"


class TestCyclicApprovalLeavesBoardEmpty:
    """[hermes-v2] H-22: a plan with a parent/depends cycle must be
    rejected BEFORE any kanban row is committed. Previously the
    seeder created the root task, then crashed inside ``link_tasks``
    when the cycle was only detected at link-insert time — leaving a
    half-seeded board (root committed, edges absent, seeder raises
    ``ValueError`` not ``PlanSeedError`` so the CLI cannot cleanly
    recover). The pre-validation in :func:`plan_parser.validate` plus
    the seeder's pre-mutation ``PlanSeedError`` path closes that hole."""

    def test_self_cycle_approval_leaves_zero_tasks_and_zero_links(
        self, fresh_home, tmp_path,
    ) -> None:
        from hermes_cli.plan_seeder import PlanSeedError, seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "cycle.md"
        plan_file.write_text(V2_SELF_CYCLE)
        with pytest.raises(PlanSeedError) as exc_info:
            seed_plan_to_kanban(plan_file)
        codes = [e.code for e in exc_info.value.errors]
        assert "dependency_cycle" in codes, codes

        with kb.connect_closing() as conn:
            tasks = conn.execute("SELECT id FROM tasks").fetchall()
            links = conn.execute("SELECT parent_id FROM task_links").fetchall()
        assert tasks == [], (
            "cyclic approval must leave 0 tasks; got "
            f"{[r['id'] for r in tasks]}"
        )
        assert links == [], (
            "cyclic approval must leave 0 links; got "
            f"{[r['parent_id'] for r in links]}"
        )

    def test_multi_node_cycle_approval_leaves_board_empty(
        self, fresh_home, tmp_path,
    ) -> None:
        from hermes_cli.plan_seeder import PlanSeedError, seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "cycle.md"
        plan_file.write_text(V2_TRIANGLE_CYCLE)
        with pytest.raises(PlanSeedError) as exc_info:
            seed_plan_to_kanban(plan_file)
        assert "dependency_cycle" in [e.code for e in exc_info.value.errors]

        with kb.connect_closing() as conn:
            tasks = conn.execute("SELECT id FROM tasks").fetchall()
            links = conn.execute("SELECT parent_id FROM task_links").fetchall()
        assert tasks == []
        assert links == []

    def test_malformed_task_line_approval_leaves_board_empty(
        self, fresh_home, tmp_path,
    ) -> None:
        """[hermes-v2] H-22: a ``- [ ]`` bullet that doesn't match the
        canonical ``T<id>: <title>`` regex used to disappear silently.
        ``validate`` now flags it as ``malformed_task_line`` and the
        seeder aborts the approve before any kanban mutation."""
        from hermes_cli.plan_seeder import PlanSeedError, seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "bad-line.md"
        plan_file.write_text(V2_MALFORMED_LINE)
        with pytest.raises(PlanSeedError) as exc_info:
            seed_plan_to_kanban(plan_file)
        codes = [e.code for e in exc_info.value.errors]
        assert "malformed_task_line" in codes, codes

        with kb.connect_closing() as conn:
            tasks = conn.execute("SELECT id FROM tasks").fetchall()
            links = conn.execute("SELECT parent_id FROM task_links").fetchall()
        assert tasks == []
        assert links == []

    def test_two_fences_approval_leaves_board_empty(
        self, fresh_home, tmp_path,
    ) -> None:
        """Multiple ``tasks``` fences must be rejected at parse time
        and leave the board completely untouched."""
        from hermes_cli.plan_seeder import PlanSeedError, seed_plan_to_kanban
        from hermes_cli import kanban_db as kb

        plan_file = tmp_path / "two-fences.md"
        plan_file.write_text(V2_TWO_FENCES)
        with pytest.raises(PlanSeedError) as exc_info:
            seed_plan_to_kanban(plan_file)
        assert exc_info.value.errors[0].code == "frontmatter_invalid"

        with kb.connect_closing() as conn:
            tasks = conn.execute("SELECT id FROM tasks").fetchall()
            links = conn.execute("SELECT parent_id FROM task_links").fetchall()
        assert tasks == []
        assert links == []


V2_SELF_CYCLE = """\
---
slug: cycle-self
title: Self-cycle
goal: |
  A plan that loops on itself.
scope_tiers:
  A: [T1]
risks: []
verification: []
---

```tasks
- [ ] T1: cycle on itself | parent: T1
```
"""

V2_TRIANGLE_CYCLE = """\
---
slug: cycle-triangle
title: Triangle cycle
goal: |
  T1 -> T2 -> T3 -> T1.
scope_tiers:
  A: [T1, T2, T3]
risks: []
verification: []
---

```tasks
- [ ] T1: step one | parent: T2
- [ ] T2: step two | parent: T3
- [ ] T3: step three | parent: T1
```
"""

V2_MALFORMED_LINE = """\
---
slug: bad-line
title: Bad line
goal: |
  A task-like bullet that drops out of the regex.
scope_tiers:
  A: [T1]
risks: []
verification: []
---

```tasks
- [ ] T1: real task
- [ ]
```
"""

V2_TWO_FENCES = """\
---
slug: two-fences
title: Two fences
goal: |
  Plan with two tasks blocks.
scope_tiers:
  A: [T1]
risks: []
verification: []
---

```tasks
- [ ] T1: in the first fence
```

```tasks
- [ ] T2: in the second fence
```
"""
