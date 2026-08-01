"""Tests for the specifier module + `hermes kanban specify` CLI surface.

The auxiliary LLM client is mocked — these tests don't hit any network or
real provider. They exercise the prompt plumbing, response parsing, DB
writes, and CLI flag surface.
"""

from __future__ import annotations

import argparse
import json as jsonlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import kanban as kanban_cli
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_specify as spec


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _fake_aux_response(content: str):
    """Build a minimal object shaped like an OpenAI chat.completions result.

    The specifier only reads ``resp.choices[0].message.content``, so we
    avoid importing the openai SDK and build the tree with MagicMock.
    """
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].message.content = content
    return resp


def _mock_client_returning(content: str):
    client = MagicMock()
    client.chat.completions.create = MagicMock(return_value=_fake_aux_response(content))
    return client


def _patch_aux_client(content: str, *, model: str = "test-model"):
    """Patch call_llm at its source module — specify_task now routes through
    it (#35566) instead of building a raw client. Returns (patcher, mock) so
    callers can still assert on the call.
    """
    mock_fn = MagicMock(return_value=_fake_aux_response(content))
    return patch("agent.auxiliary_client.call_llm", mock_fn), mock_fn


# ---------------------------------------------------------------------------
# JSON extraction helpers
# ---------------------------------------------------------------------------





# ---------------------------------------------------------------------------
# specify_task (module-level entry point)
# ---------------------------------------------------------------------------

def test_specify_task_happy_path(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="rough", triage=True)

    content = jsonlib.dumps({
        "title": "Refined rough",
        "body": "**Goal**\nA concrete goal.",
    })
    p, _ = _patch_aux_client(content)
    with p:
        outcome = spec.specify_task(tid, author="ace")

    assert outcome.ok is True
    assert outcome.task_id == tid
    assert outcome.new_title == "Refined rough"

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    # Parent-free → recompute_ready promotes to ready.
    assert task.status == "ready"
    assert task.title == "Refined rough"
    assert "**Goal**" in (task.body or "")






# ---------------------------------------------------------------------------
# CLI wiring — argparse + _cmd_specify
# ---------------------------------------------------------------------------

def _run_cli(*argv: str) -> int:
    """Invoke the `hermes kanban …` argparse surface directly."""
    root = argparse.ArgumentParser()
    subp = root.add_subparsers(dest="cmd")
    kanban_cli.build_parser(subp)
    ns = root.parse_args(["kanban", *argv])
    return kanban_cli.kanban_command(ns)




def test_cli_specify_tenant_filter(kanban_home, capsys):
    with kb.connect() as conn:
        outside = kb.create_task(conn, title="outside", triage=True)
        inside = kb.create_task(
            conn, title="inside", triage=True, tenant="proj-a",
        )

    content = jsonlib.dumps({"title": "spec", "body": "body"})
    p, _ = _patch_aux_client(content)
    with p:
        rc = _run_cli("specify", "--all", "--tenant", "proj-a", "--json")
    assert rc == 0
    lines = [
        jsonlib.loads(l)
        for l in capsys.readouterr().out.strip().splitlines()
        if l
    ]
    ids = {row["task_id"] for row in lines}
    assert ids == {inside}

    # The outside task stays in triage.
    with kb.connect() as conn:
        assert kb.get_task(conn, outside).status == "triage"
        # The inside task was promoted.
        assert kb.get_task(conn, inside).status in {"todo", "ready"}


def test_specifier_refuses_block_loop_recovery_without_calling_llm(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="already executed", assignee="mike")
        assert kb.claim_task(conn, tid, claimer="mike") is not None
        assert kb.block_task(conn, tid, reason="review", kind="needs_input")
        assert kb.unblock_task(conn, tid)
        assert kb.claim_task(conn, tid, claimer="mike") is not None
        assert kb.block_task(conn, tid, reason="review", kind="needs_input")

    with patch("agent.auxiliary_client.call_llm") as call_llm:
        outcome = spec.specify_task(tid, author="auto-specifier")

    assert outcome.ok is False
    assert "explicit continuation required" in outcome.reason
    call_llm.assert_not_called()
    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "triage"
        assert task.assignee == "mike"


def test_specifier_prompt_uses_recent_comments_as_authoritative(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="bounded task",
            body="Old scope: replay bootstrap",
            triage=True,
        )
        kb.add_comment(conn, tid, author="karim", body="New scope: do not replay bootstrap")

    content = jsonlib.dumps({"title": "Bounded task", "body": "Do not replay."})
    patcher, call_llm = _patch_aux_client(content)
    with patcher:
        outcome = spec.specify_task(tid)

    assert outcome.ok is True
    messages = call_llm.call_args.kwargs["messages"]
    assert "Later comments override conflicting" in messages[0]["content"]
    user_prompt = messages[1]["content"]
    assert user_prompt.index("Old scope") < user_prompt.index("New scope")


