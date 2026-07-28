import inspect
from unittest.mock import MagicMock

from hermes_state import SessionDB
from run_agent import AIAgent
from agent.agent_runtime_helpers import sanitize_api_messages


def test_flush_persists_inbound_platform_metadata():
    agent = AIAgent.__new__(AIAgent)
    session_db = MagicMock()
    attributes = {
        "_persist_disabled": False,
        "_session_db": session_db,
        "_session_db_created": True,
        "session_id": "session-1",
        "_persist_user_message_idx": None,
        "_persist_user_message_override": None,
        "_persist_user_message_timestamp": None,
        "_flushed_db_message_session_id": "session-1",
        "_flushed_db_message_ids": set(),
        "_last_flushed_db_idx": 0,
        "_active_compression_lock_holder": None,
    }
    for name, value in attributes.items():
        setattr(agent, name, value)

    metadata = {
        "platform": "telegram",
        "chat_id": "8531920232",
        "message_id": "4456",
        "reply_to_message_id": "4455",
    }
    message = {
        "role": "user",
        "content": "ack",
        "platform_message_id": "4456",
        "platform_metadata": metadata,
    }

    assert agent._flush_messages_to_session_db_unlocked([message], []) is True

    kwargs = session_db.append_message.call_args.kwargs
    assert kwargs["platform_message_id"] == "4456"
    assert kwargs["platform_metadata"] == metadata


def test_provider_payload_strips_platform_correlation_fields():
    message = {
        "role": "user",
        "content": "ack",
        "message_id": "4456",
        "platform_message_id": "4456",
        "_db_persisted": True,
        "platform_metadata": {
            "platform": "telegram",
            "chat_id": "8531920232",
            "message_id": "4456",
            "reply_to_message_id": "4455",
        },
    }

    sanitized = sanitize_api_messages([message])

    assert sanitized == [{"role": "user", "content": "ack"}]
    assert "platform_metadata" in message


def test_new_run_argument_does_not_shift_existing_moa_position():
    parameters = list(inspect.signature(AIAgent.run_conversation).parameters)

    assert parameters.index("persist_user_platform_metadata") == (
        parameters.index("moa_config") + 1
    )


def test_new_append_argument_does_not_shift_existing_positions():
    parameters = list(inspect.signature(SessionDB.append_message).parameters)

    assert parameters[-1] == "platform_metadata"


def test_replace_from_model_safe_replay_preserves_platform_metadata(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    db.create_session(session_id="session-1", source="telegram")
    metadata = {
        "platform": "telegram",
        "chat_id": "8531920232",
        "message_id": "4456",
        "reply_to_message_id": "4455",
    }
    db.append_message(
        "session-1",
        role="user",
        content="ack",
        platform_message_id="4456",
        platform_metadata=metadata,
    )

    replay = db.get_messages_as_conversation("session-1")
    assert "platform_metadata" not in replay[0]

    db.replace_messages("session-1", replay)

    assert db.get_messages("session-1")[0]["platform_metadata"] == metadata
