"""Integration test: CLI verb dispatch table for Feature A + B.

Exercises the ``hermes kanban reanimate`` and ``hermes kanban schedule
--in-seconds N`` CLI verbs by calling the same dispatcher
``_kanban_command_dispatch`` shape the main entry uses, but with the
helper functions directly. Verifies:

- The ``reanimate`` argparse subparser accepts the documented
  ``--reason`` (required) and ``--to-status`` flags.
- The ``schedule`` argparse subparser accepts the new ``--in-seconds``
  flag.
- Both verbs route through the dispatcher table in
  ``hermes_cli/kanban.py``.

This stays in-process (no subprocess, no live DB writes). Pairs with
``test_kanban_state_model_rework_plan_section_1.py``'s kernel tests:
those cover the surface; this covers wiring.

Run with::

    PATH=/home/b/.hermes/hermes-agent/venv/bin:$PATH pytest \\
        tests/hermes_cli/test_kanban_cli_rework_section_1.py -v
"""

from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path
from unittest import mock

import pytest

from hermes_cli import kanban as kb_cli
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: home)
    kb.init_db()
    return home


class TestReanimateParser:
    """Pin the argparse surface for ``hermes kanban reanimate``."""

    def test_reanimate_requires_reason(self) -> None:
        """``reanimate`` is the operator override for the done-terminal
        gate; the audit reason is non-negotiable. argparse
        ``required=True`` enforces this at the CLI level.
        """
        parser = kb_cli._build_kanban_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["reanimate", "t_abcdef"])

    def test_reanimate_accepts_to_status_and_reason(self) -> None:
        """``--to-status`` lets the operator pick the destination; the
        default is ``ready``. The parser must accept every documented
        choice.
        """
        parser = kb_cli._build_kanban_parser()
        for choice in ("ready", "todo", "blocked", "scheduled"):
            args = parser.parse_args(
                ["reanimate", "t_abcdef", "--to-status", choice, "--reason", "x"]
            )
            assert args.to_status == choice
            assert args.reason == "x"
            assert args.task_id == "t_abcdef"

    def test_reanimate_dispatches_to_handler(self) -> None:
        """The kanban verb dispatcher table (``cmd_dispatch_kanban``
        inside :mod:`hermes_cli.kanban`) maps ``reanimate`` to
        ``_cmd_reanimate``. Pin the wiring so a refactor doesn't
        silently drop the new verb.

        Implementation note: the dispatcher is a local
        ``handlers = {...}`` dict inside ``cmd_dispatch_kanban``. We
        inspect the function source for the literal ``"reanimate"``
        string so the test isn't tied to the dispatcher's internal
        shape (function vs class vs dict-of-classes).
        """
        import inspect

        parser = kb_cli._build_kanban_parser()
        verb = None
        for action in parser._subparsers._actions:
            if isinstance(action, argparse._SubParsersAction):
                verb = action.choices.get("reanimate")
        assert verb is not None, "reanimate verb not registered in subparsers"

        # Pin the wiring via the dispatcher source.
        src = inspect.getsource(kb_cli)
        assert '"reanimate": _cmd_reanimate' in src, (
            "reanimate verb not wired in cmd_dispatch_kanban; "
            "the parser registers it but no caller can route to it"
        )


class TestScheduleInSecondsParser:
    """Pin the argparse surface for ``hermes kanban schedule --in-seconds``."""

    def test_schedule_accepts_in_seconds(self) -> None:
        """The new ``--in-seconds`` flag maps to schedule_task_at when
        present; absent → schedule_task (legacy path).
        """
        parser = kb_cli._build_kanban_parser()
        args = parser.parse_args(
            ["schedule", "t_abcdef", "--in-seconds", "60"]
        )
        assert args.in_seconds == 60
        assert args.task_id == "t_abcdef"

    def test_schedule_omits_in_seconds_by_default(self) -> None:
        """No flag = legacy ``schedule_task`` path (park without a
        timestamp). The default of ``None`` is the signal.
        """
        parser = kb_cli._build_kanban_parser()
        args = parser.parse_args(["schedule", "t_abcdef", "legacy park"])
        assert args.in_seconds is None


class TestReanimateEndToEnd:
    """End-to-end kernel round-trip via the CLI helper. Stays in-process
    against an isolated HERMES_HOME."""

    def test_reanimate_helper_round_trips(self, kanban_home: Path) -> None:
        """``_cmd_reanimate`` flips a done card back to ready and emits
        the audit row. Same shape as the kernel test in the rework §1
        companion file, but driven through the CLI helper.
        """
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="cli reanimate smoke")
            kb.claim_task(conn, tid)
            kb.complete_task(conn, tid, result="done")
            assert kb.get_task(conn, tid).status == "done"

        args = argparse.Namespace(
            task_id=tid,
            to_status="ready",
            reason="cli smoke test override",
        )
        rc = kb_cli._cmd_reanimate(args)
        assert rc == 0, f"_cmd_reanimate returned {rc}"

        with kb.connect() as conn:
            row = kb.get_task(conn, tid)
            assert row is not None
            assert row.status == "ready"
            assert row.terminal is False
            ev = conn.execute(
                "SELECT payload FROM task_events WHERE task_id=? AND kind='reanimated'",
                (tid,),
            ).fetchall()
        assert ev, "expected reanimated audit row"
        payload = ev[0]["payload"]
        import json as _json
        if isinstance(payload, bytes):
            payload = payload.decode("utf-8")
        body = _json.loads(payload) if payload else {}
        assert body.get("reason", "").startswith("cli smoke")
        assert body.get("to_status") == "ready"


class TestScheduleInSecondsEndToEnd:
    """``_cmd_schedule`` with and without ``--in-seconds``."""

    def test_schedule_in_seconds_park_sets_timestamp(
        self, kanban_home: Path
    ) -> None:
        """``--in-seconds 0`` routes to ``schedule_task_at`` so the
        card parks with ``scheduled_for = now``."""
        import time as _time
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="cli schedule smoke")

        args = argparse.Namespace(
            task_id=tid,
            ids=None,
            in_seconds=0,
            reason=["due", "now"],
        )
        rc = kb_cli._cmd_schedule(args)
        assert rc == 0, f"_cmd_schedule returned {rc}"

        with kb.connect() as conn:
            row = kb.get_task(conn, tid)
            assert row is not None
            assert row.status == "scheduled"
            assert row.scheduled_for is not None
            assert row.scheduled_for <= int(_time.time()) + 1

    def test_schedule_no_in_seconds_legacy_park(
        self, kanban_home: Path
    ) -> None:
        """No ``--in-seconds`` flag = legacy schedule_task (park
        without timestamp). ``scheduled_for`` must remain NULL.
        """
        with kb.connect() as conn:
            tid = kb.create_task(conn, title="cli schedule legacy smoke")

        args = argparse.Namespace(
            task_id=tid,
            ids=None,
            in_seconds=None,
            reason=["legacy", "park"],
        )
        rc = kb_cli._cmd_schedule(args)
        assert rc == 0

        with kb.connect() as conn:
            row = kb.get_task(conn, tid)
            assert row is not None
            assert row.status == "scheduled"
            assert row.scheduled_for is None, (
                "legacy schedule_task path must NOT set scheduled_for"
            )


def _build_kanban_parser():
    """Build just the ``hermes kanban`` subparser tree for in-process testing.

    Builds a synthetic parent that has subparsers (the same contract
    ``build_parser`` requires — `parent_subparsers.add_parser`).
    """
    parent = argparse.ArgumentParser()
    subparsers = parent.add_subparsers(dest="top")
    return kb_cli.build_parser(subparsers)


# Override the helper used by the TestReanimateParser / TestSchedule*
# classes so the tests can target the kanban subparser in isolation,
# without spinning up the full hermes CLI surface.
kb_cli._build_kanban_parser = _build_kanban_parser
