"""Tests-as-spec for kanban rework plan §1 — state-model invariants.

Vikunja #137 / kanban t_d3f147f9. The audit deliverable lives at
``/home/b/.hermes/kanban/boards/tasker/workspaces/t_d3f147f9/README.md``.

The plan §1 enumerates four rules:

  1. ``done`` is terminal — no watcher re-arms a done card, no transitions out.
  2. ``blocked`` cards only return to active on an explicit ``kanban_unblock``;
     nothing else flips them back.
  3. ``running`` is owned exclusively by the watcher from the moment it is
     set until it reaches a terminal state. No other actor may transition
     a running card.
  4. Done cards are never re-watched — the watcher must drop them on entry
     and not re-subscribe.

These tests pin the invariants that the kernel currently upholds, document
the rules that still depend on unbuilt kernel gates, and fail loudly if a
future change relaxes them. Two of the four rules (1 and 3) require
upstream ``hermes-agent`` work that is tracked separately:

  - Rule 1 (``done`` terminal) needs a kernel gate (config flag
    ``kanban.done_is_terminal``) plus a ``hermes kanban reanimate``
    override verb. See kanban ``t_7ae1b8cd`` (feature A) for scope.
  - Rule 3 (``running`` owned by watcher) needs both the ``kanban_schedule``
    verb with a ``scheduled_for`` column and a watcher profile that takes
    over ``running`` from the dispatcher's reclaim paths. See kanban
    ``t_7ae1b8cd`` (feature B) for scope.

Until those gates land, the corresponding tests are marked ``xfail`` with a
reference to ``t_7ae1b8cd`` so the kernel change unblocks them in one
step. Rules 2 and 4 are regression tests against the current kernel —
they should pass today and forever.

Rule 4 is also a regression test against ``Mark/hermes-scripts`` commit
that fixes ``scripts/janitor-scan-blocked.py:305-316`` (the latent intent
violation that calls ``hermes kanban unblock <done-card>`` from a QA
re-dispatch branch). That script-side fix is tracked at
``https://forgejo.tail018ac4.ts.net/Mark/hermes-scripts/issues/68``
and the matched kanban card on the ``fixit`` board. The kernel-side
test below asserts the kernel correctly *rejects* the call so that even
if the janitor script re-introduces the call, the kernel blocks it.

Note: these tests do NOT assert the kernel-level "done is terminal" rule
itself (rule 1) — that gate does not yet exist. The audit calls out the
two open kernel gates (rules 1 and 3) and the upstream work that ships
them. The rule-1 and rule-3 tests below are intentionally ``xfail`` so
they will start passing as soon as the gates land.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated HERMES_HOME with an empty kanban DB.

    Mirrors the ``kanban_home`` fixture used in
    ``tests/hermes_cli/test_kanban_blocked_sticky.py`` — a fresh tmp
    HERMES_HOME keeps each test hermetic against the live board state.
    """
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _create_running(conn, title: str = "state-model test") -> str:
    """Helper: create a task in ``running`` for transition tests."""
    tid = kb.create_task(conn, title=title)
    kb.claim_task(conn, tid)
    return tid


# ===========================================================================
# Rule 2: blocked requires kanban_unblock to return
# ===========================================================================
# The kernel already enforces this. ``unblock_task`` SQL gates on
# ``status IN ('blocked', 'scheduled')`` — any other status returns False
# without mutating state. This test pins that gate so a future change
# that relaxes it (e.g. allowing unblock on ``done``) is caught here.


class TestBlockedRequiresUnblock:
    """Rule 2 — only ``kanban_unblock`` returns a blocked card to active."""

    def test_unblock_from_blocked_succeeds(self, kanban_home: Path) -> None:
        """Blocked → ready via ``kanban_unblock`` is the only legal path."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="needs unblock")
            kb.claim_task(conn, tid)
            assert kb.block_task(
                conn,
                tid,
                reason="needs-input: waiting on operator",
                expected_run_id=kb.get_task(conn, tid).current_run_id,
            )
            assert kb.get_task(conn, tid).status == "blocked"

            assert kb.unblock_task(conn, tid) is True
            assert kb.get_task(conn, tid).status == "ready"

    def test_unblock_rejects_done_card(self, kanban_home: Path) -> None:
        """Rule 2 + rule 4 intersection: ``kanban_unblock`` must not flip
        a ``done`` card. The janitor-script intent violation
        (``Mark/hermes-scripts#68``) is the script-side bug; this test
        pins the kernel-side gate that makes the violation a no-op.
        """
        with kb.connect() as conn:
            tid = _create_running(conn, title="completed task")
            assert kb.complete_task(conn, tid, result="done")
            assert kb.get_task(conn, tid).status == "done"

            # The kernel must refuse — the janitor script's intent bug
            # (which would call this) is caught at the gate, not at the
            # janitor script. Forgejo Mark/hermes-scripts#68 is the
            # script-side fix.
            assert kb.unblock_task(conn, tid) is False
            assert kb.get_task(conn, tid).status == "done"

    def test_unblock_rejects_ready_card(self, kanban_home: Path) -> None:
        """``kanban_unblock`` on a never-blocked ``ready`` card is a no-op.
        Unblocking is only meaningful from blocked/scheduled.
        """
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="fresh task")
            # Never claimed, never blocked — sitting in ready.
            assert kb.get_task(conn, tid).status == "ready"
            assert kb.unblock_task(conn, tid) is False
            assert kb.get_task(conn, tid).status == "ready"

    def test_unblock_rejects_running_card(self, kanban_home: Path) -> None:
        """``kanban_unblock`` on a running card is a no-op (it must be
        ``completed`` or ``blocked`` first).
        """
        with kb.connect() as conn:
            tid = _create_running(conn, title="running task")
            assert kb.get_task(conn, tid).status == "running"
            assert kb.unblock_task(conn, tid) is False
            assert kb.get_task(conn, tid).status == "running"

    def test_recompute_ready_does_not_promote_blocked(
        self,
        kanban_home: Path,
    ) -> None:
        """``recompute_ready`` (the dispatcher's tick) must not flip a
        worker-blocked task back to ``ready``. The same rule that the
        ``#28712`` regression covers for sticky blocks. Belt-and-braces:
        if a future change broke that, plan §1 rule 2 would also break.
        """
        with kb.connect() as conn:
            tid = _create_running(conn, title="sticky block")
            kb.block_task(
                conn,
                tid,
                reason="needs-input: janitor misfire",
                expected_run_id=kb.get_task(conn, tid).current_run_id,
            )
            assert kb.get_task(conn, tid).status == "blocked"

            for _ in range(5):
                assert kb.recompute_ready(conn) == 0
                assert kb.get_task(conn, tid).status == "blocked"


# ===========================================================================
# Rule 4: no re-watching of done cards (kernel-side gate)
# ===========================================================================
# The kernel rejects done → ready / done → todo / done → running via the
# ``unblock_task`` gate (above) and the ``complete_task`` gate. The
# remaining kernel surface is ``invalidate_descendants_for_parent_reopen``
# which intentionally DOES reopen ``done`` descendants when an ancestor
# is reopened — that is the documented carve-out. The test below pins the
# negative case: a done card with no parent-reopen context stays done.


class TestDoneIsTerminalKernelGate:
    """Rule 4 — kernel-side ``done`` is terminal except via parent reopen."""

    def test_done_card_survives_recompute_ready(self, kanban_home: Path) -> None:
        """A done card with no ancestors being reopened must stay done
        across an arbitrary number of dispatcher ticks. Pin: any
        promotion path that flips done → * in ``recompute_ready`` is a
        regression of plan §1 rule 4.
        """
        with kb.connect() as conn:
            tid = _create_running(conn, title="done survivor")
            assert kb.complete_task(conn, tid, result="done")
            assert kb.get_task(conn, tid).status == "done"

            for _ in range(10):
                kb.recompute_ready(conn)
                assert kb.get_task(conn, tid).status == "done"

    def test_double_complete_is_rejected(self, kanban_home: Path) -> None:
        """``complete_task`` on a done card must be a no-op. ``done``
        is not in the SQL ``status IN ('running','ready','blocked',
        'review')`` set; the gate returns False without mutating state.
        """
        with kb.connect() as conn:
            tid = _create_running(conn, title="double-complete")
            assert kb.complete_task(conn, tid, result="done")
            first_completed_at = kb.get_task(conn, tid).completed_at

            # Second call: kernel must reject. State must remain ``done``
            # with the original ``completed_at``.
            assert kb.complete_task(conn, tid, result="again") is False
            row = kb.get_task(conn, tid)
            assert row.status == "done"
            assert row.completed_at == first_completed_at


# ===========================================================================
# Rule 1: done is done (KERNEL GATE PENDING — t_7ae1b8cd feature A)
# ===========================================================================
# These tests describe the invariants the kernel gate MUST enforce after
# the ``done_is_terminal`` config flag and ``hermes kanban reanimate``
# override verb land (kanban t_7ae1b8cd feature A). Until that work
# ships, they are marked xfail so the suite documents the expected
# behavior without failing CI on a known-unbuilt kernel surface.


_RULE_1_REASON = (
    "Rule 1 kernel gate (kanban.done_is_terminal config flag + reanimate "
    "verb) not yet shipped; tracked at kanban t_7ae1b8cd feature A. "
    "Once the gate lands, these tests pin the invariant that no done → * "
    "transition is permitted without explicit operator override."
)


@pytest.mark.xfail(reason=_RULE_1_REASON, strict=True)
class TestDoneIsTerminalKernelGateFeatureA:
    """Rule 1 — full kernel gate (pending t_7ae1b8cd feature A)."""

    def test_kernel_rejects_done_to_todo_without_reanimate(
        self,
        kanban_home: Path,
    ) -> None:
        """Direct status flips on done cards (e.g. dashboard PATCH or
        drag-drop) must return 409 ``done_is_terminal``. Today the
        kernel allows these via ``_set_status_direct`` (called by the
        dashboard); feature A gates them behind the config flag.
        """
        pytest.xfail(_RULE_1_REASON)

    def test_reanimate_verb_can_override_done_to_ready(
        self,
        kanban_home: Path,
    ) -> None:
        """``hermes kanban reanimate <id> --reason '...'`` is the
        explicit operator escape hatch. After feature A it bypasses the
        gate and emits a ``task_events`` audit row.
        """
        pytest.xfail(_RULE_1_REASON)


# ===========================================================================
# Rule 3: running owned by watcher (KERNEL GATE PENDING — t_7ae1b8cd feature B)
# ===========================================================================
# Rule 3 is unbuildable until BOTH the ``kanban_schedule(task, +N)`` API
# AND a watcher profile exist. The dispatcher reclaim paths
# (``release_stale_claims``, ``reconcile_orphaned_running``,
# ``detect_stale_running``, ``detect_crashed_workers``,
# ``enforce_max_runtime``) are the current operating model; the watcher
# is a replacement, not an addition. Feature B (t_7ae1b8cd) lays the
# schedule column + dispatcher promotion step that the watcher hooks
# into. Once both ship, the watcher profile's SOUL.md drives running
# cards to terminal and the dispatcher reclaim paths can be retired.


_RULE_3_REASON = (
    "Rule 3 needs both kanban_schedule(task, +N) + scheduled_for column "
    "(kernel) and a watcher profile (t_7ae1b8cd feature B). Neither exists "
    "today. The dispatcher reclaim paths are the current operating model; "
    "these tests describe the post-feature-B invariant."
)


@pytest.mark.xfail(reason=_RULE_3_REASON, strict=True)
class TestRunningOwnedByWatcher:
    """Rule 3 — running cards belong to the watcher until terminal."""

    def test_running_rejects_foreign_transition(self, kanban_home: Path) -> None:
        """Once feature B lands, ``running → *`` writes from a non-watcher
        actor must return 409 ``foreign_running_write``. The watcher
        profile is the only writer. This test pins that invariant.
        """
        pytest.xfail(_RULE_3_REASON)

    def test_dispatcher_reclaim_paths_disabled(self, kanban_home: Path) -> None:
        """After feature B, the dispatcher's reclaim paths
        (``release_stale_claims``, ``reconcile_orphaned_running``,
        ``detect_stale_running``, ``detect_crashed_workers``,
        ``enforce_max_runtime``) are retired. The watcher is the sole
        authority on running cards. This test asserts they are no-ops.
        """
        pytest.xfail(_RULE_3_REASON)


# ===========================================================================
# Plan §1 audit cross-references
# ===========================================================================
# Rule-by-rule status, mirrored from the audit deliverable at
# /home/b/.hermes/kanban/boards/tasker/workspaces/t_d3f147f9/README.md.
# The four-rule matrix:
#
#   Rule | Kernel gate today  | Pending work
#   -----+--------------------+----------------------------------------------
#     1  | carve-out (parent) | kanban.done_is_terminal + reanimate verb
#     2  | enforced (unblock) | — (already clean)
#     3  | not built          | kanban_schedule(task, +N) + watcher profile
#     4  | enforced (gates)   | Mark/hermes-scripts#68 script-side fix
#
# This file documents that matrix as executable code: rules 2 and 4 are
# regression tests against the current kernel; rules 1 and 3 are xfail
# tests awaiting the upstream hermes-agent work.


# ===========================================================================
# Schema additions for rework §1 (kanban t_002084ad)
# ===========================================================================
# These tests pin the schema + dataclass + round-trip plumbing for the two
# rework §1 fields. They are PASSING tests: the columns exist, the migration
# adds them safely, and ``create_task`` round-trips both fields through
# storage.
#
# Why this lives here, not in a new test file: the parent deliverable
# already established this file as "tests-as-spec" for §1. Adding the
# schema plumbing here keeps the spec (tests) and the implementation
# (columns + dataclass + migration) co-located for the verifier who reads
# the diff.
#
# These tests are NOT the §1 rule guards (1 + 3) — those live in
# ``t_7ae1b8cd`` and remain xfail until the kernel gate ships. These are
# the *plumbing* that makes those guards possible.


class TestTerminalOwnerSchema:
    """Schema + dataclass + round-trip plumbing for rework §1 fields.

    The acceptance criteria for kanban ``t_002084ad``:

      * migration runs cleanly against an existing dataset with no data loss;
      * reading any existing card returns ``terminal=False`` and
        ``owner=None``;
      * writing a card with the new fields round-trips through storage.

    These tests pin all three. They run against the kernel today (no
    feature gate required) because they only assert the schema plumbing,
    not the state-transition guards in ``_set_status_direct`` (those
    land in ``t_7ae1b8cd``).
    """

    def test_terminal_column_present_on_fresh_db(self, kanban_home: Path) -> None:
        """Fresh-DB ``PRAGMA table_info(tasks)`` must include ``terminal``.

        Implemented by adding the column to ``SCHEMA_SQL`` in
        ``hermes_cli/kanban_db.py`` (so fresh DBs get it on init).
        """
        with kb.connect() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert (
            "terminal" in cols
        ), f"terminal column missing on fresh DB; cols={sorted(cols)}"

    def test_owner_column_present_on_fresh_db(self, kanban_home: Path) -> None:
        """Fresh-DB ``PRAGMA table_info(tasks)`` must include ``owner``."""
        with kb.connect() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
        assert "owner" in cols, f"owner column missing on fresh DB; cols={sorted(cols)}"

    def test_terminal_column_default_is_false(self, kanban_home: Path) -> None:
        """Default value for ``terminal`` is 0 (false), NOT NULL.

        Pins the ``NOT NULL DEFAULT 0`` declaration on the column.
        """
        with kb.connect() as conn:
            row = conn.execute(
                "SELECT dflt_value, [notnull] FROM pragma_table_info('tasks') "
                "WHERE name = 'terminal'"
            ).fetchone()
        assert row is not None
        assert (
            row["notnull"] == 1
        ), f"terminal should be NOT NULL; got notnull={row['notnull']}"
        # Default is stored as a string by SQLite; "0" maps to False.
        assert (
            row["dflt_value"] == "0"
        ), f"terminal default should be 0; got {row['dflt_value']!r}"

    def test_owner_column_is_nullable(self, kanban_home: Path) -> None:
        """``owner`` is nullable (no NOT NULL constraint).

        Pins the ``TEXT`` declaration on the column with no ``NOT NULL``.
        """
        with kb.connect() as conn:
            row = conn.execute(
                "SELECT dflt_value, [notnull] FROM pragma_table_info('tasks') "
                "WHERE name = 'owner'"
            ).fetchone()
        assert row is not None
        assert (
            row["notnull"] == 0
        ), f"owner should be nullable; got notnull={row['notnull']}"
        # Default is NULL (no DEFAULT clause) — dflt_value is None.
        assert (
            row["dflt_value"] is None
        ), f"owner default should be NULL; got {row['dflt_value']!r}"

    def test_legacy_db_migration_adds_columns_with_safe_defaults(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Migration adds ``terminal`` and ``owner`` to a pre-existing
        schema-only DB and backfills safe defaults for all rows.

        Simulates the legacy DB shape by creating a fresh DB, dropping the
        additive columns, inserting a row, then re-running init_db. The
        row must survive with ``terminal=0`` and ``owner=NULL`` and the
        new columns must appear on ``PRAGMA table_info``.
        """
        home = tmp_path / ".hermes"
        home.mkdir()
        monkeypatch.setenv("HERMES_HOME", str(home))
        monkeypatch.setattr(Path, "home", lambda: tmp_path)

        kb.init_db()

        with kb.connect() as conn:
            # Snapshot the live column list (so we can rebuild the table
            # without the additive columns).
            live_cols = [
                row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
            ]
            assert "terminal" in live_cols and "owner" in live_cols

            # Rebuild the tasks table WITHOUT terminal/owner to mimic the
            # pre-§1 schema. SQLite does not support DROP COLUMN before
            # 3.35, so use the legacy rename-and-recreate dance the
            # codebase already uses (see kanban_db.py:2986 area).
            to_drop = {"terminal", "owner"}
            kept = [c for c in live_cols if c not in to_drop]
            # SQLite stores PRAGMA table_info with `type`, `notnull`,
            # `dflt_value` keys; rebuild each kept column's declaration
            # so the new ``tasks`` table has the same column shape minus
            # the two we want to simulate missing.
            type_map = {
                row["name"]: (row["type"], row["notnull"], row["dflt_value"])
                for row in conn.execute("PRAGMA table_info(tasks)")
            }
            defs = []
            for c in kept:
                t, nn, dv = type_map[c]
                decl = f"{c} {t}".strip()
                if nn:
                    decl += " NOT NULL"
                if dv is not None:
                    decl += f" DEFAULT {dv}"
                defs.append(decl)

            # PK constraint isn't captured by PRAGMA, so re-add it on the
            # ``id`` column by name.
            rebuild_sql_parts = []
            for d in defs:
                if d.startswith("id "):
                    rebuild_sql_parts.append(d + " PRIMARY KEY")
                else:
                    rebuild_sql_parts.append(d)
            rebuild_sql = ", ".join(rebuild_sql_parts)

            conn.execute("ALTER TABLE tasks RENAME TO tasks_legacy_mig")
            conn.execute(f"CREATE TABLE tasks ({rebuild_sql})")
            conn.execute(
                "INSERT INTO tasks (id, title, body, assignee, status, priority, "
                "created_by, created_at, workspace_kind) "
                "VALUES ('legacy-1', 'pre-migration row', NULL, 'tasker-worker', "
                "'ready', 0, 'operator', 1700000000, 'scratch')"
            )
            conn.execute("DROP TABLE tasks_legacy_mig")
            # Confirm the legacy row exists without terminal/owner columns.
            live_cols2 = [
                row["name"] for row in conn.execute("PRAGMA table_info(tasks)")
            ]
            assert "terminal" not in live_cols2 and "owner" not in live_cols2

        # Re-run init_db on the same path. The migration should add the
        # missing columns WITHOUT dropping the legacy row.
        kb.init_db()

        with kb.connect() as conn:
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
            assert (
                "terminal" in cols
            ), f"migration did not add 'terminal'; cols={sorted(cols)}"
            assert (
                "owner" in cols
            ), f"migration did not add 'owner'; cols={sorted(cols)}"
            row = conn.execute(
                "SELECT id, status, terminal, owner FROM tasks WHERE id = 'legacy-1'"
            ).fetchone()
            assert row is not None, "legacy row was lost during migration"
            # Migration must not silently change ``status`` — that's the
            # "no data loss" half of the acceptance criterion.
            assert row["status"] == "ready"
            # And the backfilled defaults must be the safe ones.
            assert row["terminal"] == 0
            assert row["owner"] is None

    def test_create_task_default_terminal_false_owner_none(
        self, kanban_home: Path
    ) -> None:
        """Default ``create_task()`` round-trips ``terminal=False`` and
        ``owner=None`` — preserves pre-§1 behavior for existing callers.
        """
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="default round-trip")
            task = kb.get_task(conn, tid)
        assert task.terminal is False
        assert task.owner is None

    def test_create_task_round_trips_terminal_true(self, kanban_home: Path) -> None:
        """``create_task(terminal=True)`` persists and reads back as True.

        Acceptance: "writing a card with the new fields round-trips
        through storage."
        """
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="terminal=true round-trip", terminal=True)
            task = kb.get_task(conn, tid)
        assert task.terminal is True

        # And via the raw SELECT path (the watcher loop reads via
        # ``Task.from_row`` over a SELECT — same code path, but pin it
        # explicitly so a future refactor of get_task can't silently
        # hide the round-trip).
        with kb.connect() as conn:
            row = conn.execute(
                "SELECT terminal, owner FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["terminal"] == 1
        assert row["owner"] is None

    def test_create_task_round_trips_owner_string(self, kanban_home: Path) -> None:
        """``create_task(owner='watcher')`` persists and reads back as 'watcher'."""
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="owner round-trip", owner="watcher")
            task = kb.get_task(conn, tid)
        assert task.owner == "watcher"
        assert task.terminal is False

        with kb.connect() as conn:
            row = conn.execute(
                "SELECT terminal, owner FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
        assert row["terminal"] == 0
        assert row["owner"] == "watcher"

    def test_create_task_round_trips_both_fields_together(
        self, kanban_home: Path
    ) -> None:
        """Both fields persist together when set at create time."""
        with kb.connect() as conn:
            tid = kb.create_task(
                conn,
                title="both-fields round-trip",
                terminal=True,
                owner="dispatcher:worker-7",
            )
            task = kb.get_task(conn, tid)
        assert task.terminal is True
        assert task.owner == "dispatcher:worker-7"

    def test_existing_cards_read_as_terminal_false_owner_none(
        self, kanban_home: Path
    ) -> None:
        """Reading any pre-existing card returns the safe defaults.

        Acceptance: "reading any existing card returns terminal=false and
        owner=null". Cards created WITHOUT passing the new kwargs (the
        legacy creation path) must read back with the backfilled values.
        """
        with kb.connect() as conn:
            # create_task without terminal/owner args — the legacy shape.
            tid = kb.create_task(conn, title="legacy-create")
            task = kb.get_task(conn, tid)
        assert task.terminal is False
        assert task.owner is None

    def test_dataclass_field_defaults_match_schema_defaults(
        self, kanban_home: Path
    ) -> None:
        """The ``Task`` dataclass defaults match the SQL column defaults.

        Pin: a future change that loosens the dataclass (e.g. makes
        ``terminal: Optional[bool] = None``) would break downstream
        serializers that rely on ``bool``.
        """
        from dataclasses import fields as dc_fields

        field_defaults = {f.name: f.default for f in dc_fields(kb.Task)}
        assert field_defaults["terminal"] is False, (
            f"Task.terminal default drifted from schema (got "
            f"{field_defaults['terminal']!r}, expected False)"
        )
        assert field_defaults["owner"] is None, (
            f"Task.owner default drifted from schema (got "
            f"{field_defaults['owner']!r}, expected None)"
        )

    def test_from_row_tolerates_missing_terminal_owner_columns(
        self, kanban_home: Path
    ) -> None:
        """``Task.from_row`` is forward-compatible with rows that lack the
        new columns (defensive ``if 'col' in keys`` guard).

        Simulates a pre-migration SELECT — manually strip the new columns
        from the row object and confirm from_row returns sensible
        defaults instead of KeyError. This pins the row-dict guard so a
        future refactor that removes it gets caught.
        """
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="forward-compat")
            full_row = conn.execute(
                "SELECT * FROM tasks WHERE id = ?", (tid,)
            ).fetchone()
            assert "terminal" in full_row.keys()
            assert "owner" in full_row.keys()

            # Build a Row-like dict missing the new keys, mimicking a
            # pre-§1 SELECT * result on a legacy DB.
            class _LegacyRow:
                def __init__(self, src: sqlite3.Row, drop: set) -> None:
                    self._keys = [k for k in src.keys() if k not in drop]
                    self._data = {k: src[k] for k in self._keys}

                def __getitem__(self, key: str):
                    return self._data[key]

                def keys(self) -> list:
                    return list(self._keys)

            legacy = _LegacyRow(full_row, {"terminal", "owner"})
            task = kb.Task.from_row(legacy)
        assert task.terminal is False
        assert task.owner is None
