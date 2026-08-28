"""`/project` on a messaging platform: read-only, deterministic, no agent turn.

Commit 9 registered `project` as a gateway-visible command but shipped no
handler, so the advertised slash fell through to ordinary agent processing when
idle and hit the generic busy-reject mid-turn. This pins the handler that
closes that, and — more importantly — pins what it refuses.

A messaging platform is not a terminal. `project plan --file PATH` reads a
server-local file, and the folder verbs mutate the filesystem. Neither may be
reachable from Telegram, Slack or Discord just because they share one argparse
tree with `status`.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, build_session_key
from hermes_cli import kanban_db as kb
from hermes_cli.commands import COMMAND_REGISTRY


def _source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1",
                         user_name="tester", chat_type="dm")


def _event(text: str):
    return SimpleNamespace(text=text, source=_source(), message_id="1",
                           reply_to_message_id=None)


def _bare_runner():
    runner = object.__new__(GatewayRunner)
    runner._owns_kanban_dispatcher_lock = lambda: True
    return runner


async def _run(text: str) -> str:
    return await GatewayRunner._handle_project_command(_bare_runner(), _event(text))


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_DB", str(tmp_path / "gw.db"))
    kb.init_db()
    conn = kb.connect()
    try:
        kb.ensure_pm_project(conn, project_id="proj-1", name="Proj One")
        kb.submit_plan(conn, project_id="proj-1", body="step one\nstep two",
                       proposed_by="pm")
        tid = kb.create_task(conn, title="ship it", assignee="pm")
        kb.park_for_plan_approval(conn, tid, project_id="proj-1", revision=1)
    finally:
        conn.close()
    return SimpleNamespace(task_id=tid, secret=tmp_path / "planted.txt")


# ---------------------------------------------------------------------------
# Allowed, read-only verbs
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_status_returns_deterministic_output(board):
    out = await _run("/project status")
    assert board.task_id in out
    assert "proj-1" in out
    assert await _run("/project status") == out, "same input, same output"


@pytest.mark.asyncio
async def test_plan_show_by_project(board):
    out = await _run("/project plan-show proj-1")
    assert "step one" in out and "step two" in out


@pytest.mark.asyncio
async def test_plan_show_by_gated_task(board):
    out = await _run(f"/project plan-show {board.task_id}")
    assert "step one" in out


@pytest.mark.asyncio
async def test_plan_show_accepts_an_integer_revision(board):
    conn = kb.connect()
    try:
        kb.submit_plan(conn, project_id="proj-1", body="second draft")
    finally:
        conn.close()
    out = await _run("/project plan-show proj-1 --revision 1")
    assert "step one" in out
    assert "second draft" not in out


@pytest.mark.asyncio
async def test_a_bare_project_command_lists_what_is_allowed(board):
    out = await _run("/project")
    assert "status" in out and "plan-show" in out


# ---------------------------------------------------------------------------
# Refused: writes, filesystem verbs, and every file-reading form
# ---------------------------------------------------------------------------

REFUSED = [
    "/project plan proj-1 --body hello",
    "/project plan proj-1 --file /etc/passwd",
    "/project plan proj-1 --file -",
    "/project create Something",
    "/project add-folder proj-1 /tmp",
    "/project remove-folder proj-1 /tmp",
    "/project rename proj-1 Other",
    "/project set-primary proj-1 /tmp",
    "/project use proj-1",
    "/project archive proj-1",
    "/project restore proj-1",
    "/project bind-board proj-1 default",
    "/project approve-plan t_abc",
    "/project reject-plan t_abc",
]


@pytest.mark.parametrize("text", REFUSED)
@pytest.mark.asyncio
async def test_write_and_filesystem_verbs_are_refused(board, text):
    out = await _run(text)
    assert "not available" in out.lower() or "read-only" in out.lower()
    assert "terminal" in out.lower(), "the refusal must say where to go instead"


@pytest.mark.asyncio
async def test_a_planted_local_file_is_never_readable(board, tmp_path):
    planted = tmp_path / "planted.txt"
    planted.write_text("TOP-SECRET-PLANTED-CONTENT")
    for text in (
        f"/project plan proj-1 --file {planted}",
        f"/project plan-show proj-1 --file {planted}",
        f"/project status --file {planted}",
        f"/project plan proj-1 --body @{planted}",
        "/project plan proj-1 --file -",
    ):
        out = await _run(text)
        assert "TOP-SECRET-PLANTED-CONTENT" not in out, text


@pytest.mark.parametrize("flag", [
    "--file /etc/hosts", "--body x", "--json", "--name x", "-", "--revision",
])
@pytest.mark.asyncio
async def test_unexpected_flags_are_refused_on_allowed_verbs(board, flag):
    out = await _run(f"/project status {flag}")
    assert "TOP-SECRET" not in out
    assert "not accepted" in out.lower() or "not available" in out.lower()


@pytest.mark.parametrize("bad", ["abc", "1x", "-1", "1 2"])
@pytest.mark.asyncio
async def test_a_non_integer_revision_is_refused(board, bad):
    out = await _run(f"/project plan-show proj-1 --revision {bad}")
    assert "revision" in out.lower()
    assert "step one" not in out


@pytest.mark.asyncio
async def test_an_unknown_verb_is_refused_deterministically(board):
    out = await _run("/project frobnicate")
    assert "frobnicate" in out
    assert "status" in out


@pytest.mark.asyncio
async def test_nothing_the_gateway_can_do_writes_an_approval(board):
    for text in REFUSED + ["/project status", "/project plan-show proj-1"]:
        await _run(text)
    conn = kb.connect()
    try:
        approvals = conn.execute("SELECT COUNT(*) c FROM pm_approvals").fetchone()["c"]
        row = conn.execute("SELECT status, gate_state FROM tasks WHERE id = ?",
                           (board.task_id,)).fetchone()
        plans = conn.execute("SELECT COUNT(*) c FROM pm_plans").fetchone()["c"]
    finally:
        conn.close()
    assert approvals == 0
    assert row["gate_state"] == "plan" and row["status"] == "scheduled"
    assert plans == 1, "no gateway path may author a plan"


# ---------------------------------------------------------------------------
# Wiring: idle and mid-turn
# ---------------------------------------------------------------------------

def _full_runner(running: bool):
    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="***")})
    adapter = MagicMock()
    adapter.send = AsyncMock()
    adapter._pending_messages = {}
    runner.adapters = {Platform.TELEGRAM: adapter}
    entry = SessionEntry(
        session_key=build_session_key(_source()), session_id="s1",
        created_at=datetime.now(), updated_at=datetime.now(),
        platform=Platform.TELEGRAM, chat_type="dm", total_tokens=0)
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = entry
    runner.session_store.load_transcript.return_value = []
    runner.session_store.has_any_sessions.return_value = True
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._pending_approvals = {}
    runner._session_db = MagicMock()
    runner._session_db.get_session_title.return_value = None
    runner._reasoning_config = None
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._show_reasoning = False
    runner._is_user_authorized = lambda _s: True
    runner._set_session_env = lambda _c: None
    runner._should_send_voice_reply = lambda *a, **k: False
    runner._send_voice_reply = AsyncMock()
    runner._capture_gateway_honcho_if_configured = lambda *a, **k: None
    runner._emit_gateway_run_progress = AsyncMock()
    runner._owns_kanban_dispatcher_lock = lambda: True
    if running:
        runner._running_agents[build_session_key(_source())] = MagicMock()
    return runner


@pytest.mark.asyncio
async def test_idle_dispatch_runs_the_command_and_starts_no_agent_turn(board):
    runner = _full_runner(running=False)
    from gateway.platforms.base import MessageEvent, MessageType

    event = MessageEvent(text="/project status", source=_source(),
                         message_type=MessageType.TEXT, message_id="1")
    result = await runner._handle_message(event)
    assert result is not None
    assert board.task_id in result
    assert runner._running_agents == {}, "a slash command must not start a turn"


@pytest.mark.asyncio
async def test_mid_turn_dispatch_runs_the_command_not_a_busy_reject(board):
    runner = _full_runner(running=True)
    cmd_def = next(c for c in COMMAND_REGISTRY if c.name == "project")
    out = await runner._dispatch_busy_slash_command(
        _event("/project status"), cmd_def, "project", _source())
    assert board.task_id in out
    assert "can't run mid-turn" not in out.lower()


def test_the_registry_still_declares_dispatch():
    cmd_def = next(c for c in COMMAND_REGISTRY if c.name == "project")
    assert cmd_def.busy_policy == "dispatch"
