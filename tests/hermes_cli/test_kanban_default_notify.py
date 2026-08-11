"""Board-level default notification channel (``kanban.default_notify``).

Task creation paths that carry a live chat context auto-subscribe that
context (``tools/kanban_tools.py::_maybe_auto_subscribe``). Paths that do
NOT — a detached ``hermes kanban create`` from a terminal or cron script,
or ``kanban_create`` called by an orchestrator with no user-facing message
behind it — used to produce tasks with zero notification coverage, so a
later ``blocked`` / ``review_requested`` transition reached nobody.

These tests pin the behaviour of the fallback:

  - configured board  -> exactly one sub, with the configured target
  - unconfigured board -> zero subs, no error (pre-feature behaviour)
  - live chat context  -> still wins; the fallback never overrides it
  - both apply         -> still one row (add_notify_sub is idempotent)
  - board isolation    -> board A's default never lands on board B's task
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "test-profile")
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_SESSION_PLATFORM", raising=False)
    monkeypatch.delenv("HERMES_SESSION_CHAT_ID", raising=False)
    monkeypatch.delenv("HERMES_SESSION_KEY", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _write_config(home: Path, body: str) -> None:
    (home / "config.yaml").write_text(body, encoding="utf-8")


def _subs(task_id: str, board: str | None = None) -> list[dict]:
    conn = kb.connect(board=board)
    try:
        return list(kb.list_notify_subs(conn, task_id))
    finally:
        conn.close()


def _created_id(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("Created "):
            return line.split()[1]
    raise AssertionError(f"no task id in CLI output: {output!r}")


# ---------------------------------------------------------------------------
# CLI create
# ---------------------------------------------------------------------------

def test_cli_create_subscribes_board_default(kanban_home):
    """A board with a configured default gets exactly one subscription row
    on the new task, carrying the configured platform/chat_id/chat_type."""
    _write_config(
        kanban_home,
        "kanban:\n"
        "  default_notify:\n"
        "    default:\n"
        "      platform: slack\n"
        "      chat_id: C0BP91D49CH\n"
        "      chat_type: channel\n",
    )
    out = kc.run_slash("create 'detached create' --assignee worker")
    subs = _subs(_created_id(out))

    assert len(subs) == 1, subs
    assert subs[0]["platform"] == "slack"
    assert subs[0]["chat_id"] == "C0BP91D49CH"
    assert subs[0]["chat_type"] == "channel"


def test_cli_create_without_default_notify_subscribes_nothing(kanban_home):
    """Regression guard for the pre-feature behaviour: no config entry means
    no subscription and no failure — creation still succeeds."""
    out = kc.run_slash("create 'no default' --assignee worker")
    task_id = _created_id(out)

    assert _subs(task_id) == []
    conn = kb.connect()
    try:
        assert kb.get_task(conn, task_id) is not None
    finally:
        conn.close()


def test_cli_create_tolerates_malformed_default_notify(kanban_home):
    """A default_notify entry missing chat_id (or shaped wrong) must not
    fail the create — it degrades to the no-default behaviour."""
    _write_config(
        kanban_home,
        "kanban:\n"
        "  default_notify:\n"
        "    default:\n"
        "      platform: slack\n",
    )
    out = kc.run_slash("create 'half configured' --assignee worker")
    assert _subs(_created_id(out)) == []


def test_cli_create_default_notify_is_idempotent(kanban_home):
    """Explicitly subscribing the same target afterwards must not produce a
    second row — add_notify_sub is INSERT OR IGNORE on
    (task, platform, chat, thread)."""
    _write_config(
        kanban_home,
        "kanban:\n"
        "  default_notify:\n"
        "    default:\n"
        "      platform: slack\n"
        "      chat_id: C0BP91D49CH\n"
        "      chat_type: channel\n",
    )
    task_id = _created_id(
        kc.run_slash("create 'dedupe' --assignee worker")
    )
    kc.run_slash(
        f"notify-subscribe {task_id} --platform slack "
        "--chat-id C0BP91D49CH --chat-type channel"
    )

    assert len(_subs(task_id)) == 1, _subs(task_id)


def test_cli_create_respects_auto_subscribe_kill_switch(kanban_home):
    """``auto_subscribe_on_create: false`` disables every create-time
    subscription source, board defaults included."""
    _write_config(
        kanban_home,
        "kanban:\n"
        "  auto_subscribe_on_create: false\n"
        "  default_notify:\n"
        "    default:\n"
        "      platform: slack\n"
        "      chat_id: C0BP91D49CH\n",
    )
    out = kc.run_slash("create 'gated off' --assignee worker")
    assert _subs(_created_id(out)) == []


# ---------------------------------------------------------------------------
# Board isolation
# ---------------------------------------------------------------------------

def test_default_notify_does_not_leak_across_boards(kanban_home):
    """A default configured for one board must never be applied to a task
    created on a different board."""
    kb.create_board("alt")
    _write_config(
        kanban_home,
        "kanban:\n"
        "  default_notify:\n"
        "    alt:\n"
        "      platform: slack\n"
        "      chat_id: C-ALT\n",
    )

    # Default board: no entry for "default" -> nothing.
    assert kb.resolve_default_notify("default") is None
    # Alt board: its own entry resolves.
    assert kb.resolve_default_notify("alt") == {
        "platform": "slack",
        "chat_id": "C-ALT",
    }

    conn = kb.connect(board="alt")
    try:
        tid = kb.create_task(conn, title="alt task", assignee="worker")
        assert kb.apply_default_notify_sub(conn, tid, board="alt") is True
    finally:
        conn.close()
    alt_subs = _subs(tid, board="alt")
    assert len(alt_subs) == 1
    assert alt_subs[0]["chat_id"] == "C-ALT"

    conn = kb.connect()
    try:
        default_tid = kb.create_task(conn, title="default task", assignee="worker")
        assert kb.apply_default_notify_sub(conn, default_tid) is False
    finally:
        conn.close()
    assert _subs(default_tid) == []


# ---------------------------------------------------------------------------
# Tool path (tools/kanban_tools.py)
# ---------------------------------------------------------------------------

@pytest.fixture
def worker_env(kanban_home, monkeypatch):
    """A kanban worker context, so the gated kanban_* handlers will run."""
    conn = kb.connect()
    try:
        tid = kb.create_task(conn, title="worker-root", assignee="test-profile")
        kb.claim_task(conn, tid)
    finally:
        conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    return tid


def test_tool_create_live_chat_context_still_wins(worker_env, kanban_home, monkeypatch):
    """Regression guard: when the tool call HAS a live chat context, that
    context is subscribed — the board default must not displace it."""
    _write_config(
        kanban_home,
        "kanban:\n"
        "  default_notify:\n"
        "    default:\n"
        "      platform: slack\n"
        "      chat_id: C-BOARD-DEFAULT\n",
    )
    monkeypatch.setenv("HERMES_SESSION_PLATFORM", "telegram")
    monkeypatch.setenv("HERMES_SESSION_CHAT_ID", "chat-live")

    from tools import kanban_tools as kt
    payload = json.loads(
        kt._handle_create({"title": "live ctx", "assignee": "peer"})
    )
    assert payload["ok"] is True
    assert payload["subscribed"] is True

    subs = _subs(payload["task_id"])
    assert len(subs) == 1, subs
    assert subs[0]["platform"] == "telegram"
    assert subs[0]["chat_id"] == "chat-live"


def test_tool_create_without_chat_context_falls_back_to_board_default(
    worker_env, kanban_home
):
    """No live chat context (orchestrator / cron) + a configured board
    default -> the default channel is subscribed instead of nothing."""
    _write_config(
        kanban_home,
        "kanban:\n"
        "  default_notify:\n"
        "    default:\n"
        "      platform: slack\n"
        "      chat_id: C-BOARD-DEFAULT\n"
        "      chat_type: channel\n",
    )

    from tools import kanban_tools as kt
    payload = json.loads(
        kt._handle_create({"title": "no ctx", "assignee": "peer"})
    )
    assert payload["ok"] is True
    assert payload["subscribed"] is True

    subs = _subs(payload["task_id"])
    assert len(subs) == 1, subs
    assert subs[0]["platform"] == "slack"
    assert subs[0]["chat_id"] == "C-BOARD-DEFAULT"
    assert subs[0]["chat_type"] == "channel"


def test_tool_create_without_chat_context_or_default_is_still_a_noop(
    worker_env, kanban_home
):
    """Regression guard: the unconfigured case keeps reporting
    subscribed=False and writing no rows."""
    from tools import kanban_tools as kt
    payload = json.loads(
        kt._handle_create({"title": "nothing at all", "assignee": "peer"})
    )
    assert payload["ok"] is True
    assert payload["subscribed"] is False
    assert _subs(payload["task_id"]) == []


# ---------------------------------------------------------------------------
# `hermes config set` must not flag the new key as unrecognized
# ---------------------------------------------------------------------------

def test_default_notify_paths_validate_as_known_config_keys():
    """Board slugs are user-supplied, so everything below the
    ``kanban.default_notify`` container has to validate as known — otherwise
    `hermes config set` warns on a key the runtime does read."""
    from hermes_cli.config import _validate_config_key

    assert _validate_config_key("kanban.default_notify")[0] is True
    assert _validate_config_key(
        "kanban.default_notify.assemblywatch.platform"
    )[0] is True
    assert _validate_config_key(
        "kanban.default_notify.some-other-board.chat_id"
    )[0] is True
    # The container is scoped: a typo'd sibling of default_notify is still
    # reported as unknown.
    assert _validate_config_key("kanban.default_notifyy.x")[0] is False
