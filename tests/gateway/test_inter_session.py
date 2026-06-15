"""Tier-1 tests for generic Hermes inter-session messaging."""

import json
import asyncio
import contextlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.inter_session import configured_session_key, parse_inter_session_config, render_prompt_for_turn
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore, build_session_key
from gateway.session_context import clear_session_vars, set_session_vars
from hermes_state import SessionDB
from tools import inter_session_tool


MGMT_ID = "120363407903158826@g.us"
AMK_ID = "120363421424519051@g.us"
PG_ID = "120363423568509280@g.us"
HG_ID = "120363422582425366@g.us"
SK_ID = "120363403845802098@g.us"


def _raw_config():
    return {
        "group_sessions_per_user": False,
        "inter_session": {
            "enabled": True,
            "agent_id": "christopher",
            "poll_interval_seconds": 0.1,
            "sessions": {
                "management": {
                    "label": "Christopher x TGG Management",
                    "role": "management",
                    "source": {
                        "platform": "whatsapp",
                        "chat_type": "group",
                        "chat_id": MGMT_ID,
                        "user_id": "system:inter-session",
                        "user_name": "Christopher",
                    },
                    "pa_job_type": "tgg_management",
                    "can_send_to": ["amk_ops", "pg_ops", "hg_ops", "sk_ops"],
                    "external_output": "normal",
                    "description": "Visible management session.",
                },
                "amk_ops": {
                    "label": "AMK maintenance",
                    "role": "ops",
                    "source": {
                        "platform": "whatsapp",
                        "chat_type": "group",
                        "chat_id": AMK_ID,
                        "user_id": "system:inter-session",
                        "user_name": "Christopher",
                    },
                    "pa_job_type": "tgg_ops_ingest",
                    "can_send_to": ["management"],
                    "external_output": "never",
                    "description": "Silent AMK perceiver.",
                },
                "pg_ops": {
                    "label": "PG maintenance",
                    "role": "ops",
                    "source": {"platform": "whatsapp", "chat_type": "group", "chat_id": PG_ID},
                    "pa_job_type": "tgg_ops_ingest",
                    "can_send_to": ["management"],
                    "external_output": "never",
                },
                "hg_ops": {
                    "label": "HG maintenance",
                    "role": "ops",
                    "source": {"platform": "whatsapp", "chat_type": "group", "chat_id": HG_ID},
                    "pa_job_type": "tgg_ops_ingest",
                    "can_send_to": ["management"],
                    "external_output": "never",
                },
                "sk_ops": {
                    "label": "SK maintenance",
                    "role": "ops",
                    "source": {"platform": "whatsapp", "chat_type": "group", "chat_id": SK_ID},
                    "pa_job_type": "tgg_ops_ingest",
                    "can_send_to": ["management"],
                    "external_output": "never",
                },
            },
        },
    }


@pytest.fixture()
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(json.dumps(_raw_config()), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))

    # SessionDB.DEFAULT_DB_PATH is computed at import time; point the process at
    # the fixture DB so the public tool path can instantiate SessionDB().
    import hermes_state
    import gateway.run as gateway_run

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", home / "state.db")
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    monkeypatch.setattr(inter_session_tool, "_load_raw_config", _raw_config)
    yield home
    clear_session_vars([])


def _configured_source(name: str) -> SessionSource:
    cfg = parse_inter_session_config(_raw_config())
    return cfg.sessions[name].source


def _configured_key(name: str) -> str:
    cfg = parse_inter_session_config(_raw_config())
    return configured_session_key(cfg.sessions[name], _raw_config())


def test_prompt_context_includes_current_and_other_sessions():
    raw = _raw_config()
    amk_source = SessionSource(
        platform=Platform.WHATSAPP,
        chat_type="group",
        chat_id=AMK_ID,
        user_id="sky-live-user",
        user_name="Sky",
    )
    amk_key = build_session_key(amk_source, group_sessions_per_user=False)

    prompt = render_prompt_for_turn(raw, session_key=amk_key, source=amk_source)

    assert "## Inter-Session Messaging" in prompt
    assert "Current Hermes session:" in prompt
    assert "- name: amk_ops" in prompt
    assert "- can_send_to: management" in prompt
    assert "Other christopher sessions:" in prompt
    assert "management: Christopher x TGG Management" in prompt
    assert "pg_ops: PG maintenance" in prompt
    assert "send_session_message(to, body)" in prompt


def test_state_db_mailbox_state_machine(isolated_home):
    db = SessionDB(db_path=Path(isolated_home) / "state.db")
    row = db.create_session_mailbox_message(
        agent_id="christopher",
        from_session_name="amk_ops",
        from_session_key="from-key",
        to_session_name="management",
        to_session_key="to-key",
        body="case facts",
        source_turn_id="turn-1",
    )

    pending = db.list_pending_session_mailbox(agent_id="christopher")
    assert [r["id"] for r in pending] == [row["id"]]

    assert db.claim_session_mailbox(row["id"]) is True
    claimed = db.get_session_mailbox_message(row["id"])
    assert claimed["status"] == "running"
    assert claimed["attempts"] == 1

    db.defer_session_mailbox(row["id"], "target busy")
    deferred = db.get_session_mailbox_message(row["id"])
    assert deferred["status"] == "pending"
    assert deferred["last_error"] == "target busy"

    assert db.claim_session_mailbox(row["id"]) is True
    db.complete_session_mailbox(row["id"], to_session_key="to-key-final", to_session_id="sid-final")
    delivered = db.get_session_mailbox_message(row["id"])
    assert delivered["status"] == "delivered"
    assert delivered["to_session_key"] == "to-key-final"
    assert delivered["to_session_id"] == "sid-final"
    assert delivered["delivered_at"] is not None
    db.close()


class _FakeAdapter:
    def __init__(self):
        self.sent = []
        self.send = AsyncMock(side_effect=self._send)

    async def _send(self, *, chat_id, content, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content, "metadata": metadata})
        return SimpleNamespace(success=True)


async def _exercise_tool_and_delivery(*, from_name: str, to_name: str, body: str, runner):
    source = _configured_source(from_name)
    key = _configured_key(from_name)
    tokens = set_session_vars(
        platform=source.platform.value,
        chat_id=source.chat_id,
        chat_name=source.chat_name or "",
        thread_id=source.thread_id or "",
        user_id="live-operator",
        user_name="Operator",
        session_key=key,
        session_id=f"sid-{from_name}",
    )
    try:
        result = json.loads(inter_session_tool.send_session_message({"to": to_name, "body": body}, task_id="turn-x"))
    finally:
        clear_session_vars(tokens)
    assert result["success"] is True
    assert result["from"] == from_name
    assert result["to"] == to_name

    row = runner._session_db.get_session_mailbox_message(result["id"])
    assert row["status"] == "pending"

    runner._running = True
    watcher = asyncio.create_task(runner._inter_session_mailbox_watcher(interval=0.05))
    try:
        deadline = asyncio.get_running_loop().time() + 3.0
        while asyncio.get_running_loop().time() < deadline:
            current = runner._session_db.get_session_mailbox_message(row["id"])
            if current["status"] in {"delivered", "failed"}:
                assert current["status"] == "delivered", current.get("last_error")
                return current
            await asyncio.sleep(0.05)
        pytest.fail(f"mailbox row {row['id']} was not delivered by watcher")
    finally:
        runner._running = False
        watcher.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watcher


@pytest.mark.asyncio
async def test_tool_mailbox_watcher_path_delivers_ops_to_management_and_management_to_specific_session(isolated_home):
    gateway_config = GatewayConfig()
    gateway_config.sessions_dir = Path(isolated_home) / "sessions"
    gateway_config.group_sessions_per_user = False
    gateway_config.inter_session = _raw_config()["inter_session"]

    runner = object.__new__(GatewayRunner)
    runner.config = gateway_config
    runner._session_db = SessionDB(db_path=Path(isolated_home) / "state.db")
    runner.session_store = SessionStore(gateway_config.sessions_dir, gateway_config)
    runner._running_agents = {}
    fake_adapter = _FakeAdapter()
    runner.adapters = {Platform.WHATSAPP: fake_adapter}

    captured = []

    async def _handle_message(event):
        captured.append(event)
        if event.pa_job_type == "tgg_management":
            return "Noted by management."
        return "This ops session stays silent."

    runner._handle_message = AsyncMock(side_effect=_handle_message)

    # Ops → management: the tool persists a mailbox row; the delivery path
    # creates an internal management turn and sends the management response to
    # the configured management WhatsApp chat.
    delivered_ops = await _exercise_tool_and_delivery(
        from_name="amk_ops",
        to_name="management",
        body="AMK case 7 has a new closure note.",
        runner=runner,
    )
    assert delivered_ops["status"] == "delivered"
    assert captured[0].internal is True
    assert captured[0].source.chat_id == MGMT_ID
    assert captured[0].pa_job_type == "tgg_management"
    assert captured[0].pa_context["inter_session"]["from_session_name"] == "amk_ops"
    assert "[Message from christopher session: amk_ops / AMK maintenance]" in captured[0].text
    assert fake_adapter.sent[-1]["chat_id"] == MGMT_ID
    assert fake_adapter.sent[-1]["content"] == "Noted by management."

    sent_count_after_management = len(fake_adapter.sent)

    # Management → specific ops session: target is the configured AMK session,
    # and its external_output=never suppresses external WhatsApp output.
    delivered_mgmt = await _exercise_tool_and_delivery(
        from_name="management",
        to_name="amk_ops",
        body="Please re-check the AMK closure facts before I reply.",
        runner=runner,
    )
    assert delivered_mgmt["status"] == "delivered"
    assert captured[1].internal is True
    assert captured[1].source.chat_id == AMK_ID
    assert captured[1].pa_job_type == "tgg_ops_ingest"
    assert captured[1].pa_context["inter_session"]["from_session_name"] == "management"
    assert "[Message from christopher session: management / Christopher x TGG Management]" in captured[1].text
    assert len(fake_adapter.sent) == sent_count_after_management
