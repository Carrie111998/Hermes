"""Persistence redaction boundaries for mutable reasoning and tool output."""

import copy
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest


SECRET_PASSWORD = "fake-db-password-43666"
SECRET_URI = (
    "postgresql+psycopg://postgres:"
    f"{SECRET_PASSWORD}@127.0.0.1:5432/postgres"
)


@pytest.fixture(autouse=True)
def _redaction_enabled(monkeypatch):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", True)


def _make_agent():
    """Build the smallest test double that runs the real response builder."""
    from run_agent import AIAgent

    agent = MagicMock(spec=AIAgent)
    agent._build_assistant_message = AIAgent._build_assistant_message.__get__(agent)
    agent._extract_reasoning = AIAgent._extract_reasoning.__get__(agent)
    agent._strip_think_blocks = AIAgent._strip_think_blocks.__get__(agent)
    agent.verbose_logging = False
    agent.reasoning_callback = None
    agent.stream_delta_callback = None
    agent._stream_callback = None
    agent._needs_thinking_reasoning_pad.return_value = False
    agent._split_responses_tool_id.return_value = (None, None)
    agent._derive_responses_function_call_id.side_effect = (
        lambda call_id, response_id: response_id or call_id
    )
    return agent


def _api_message(content="done", **fields):
    message = SimpleNamespace(
        content=content,
        tool_calls=fields.pop("tool_calls", None),
    )
    for key, value in fields.items():
        setattr(message, key, value)
    return message


def _persist_message(tmp_path, message):
    from hermes_state import SessionDB

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("redaction-test", "cli")
    db.append_message(session_id=session_id, **message)
    replayed = db.get_messages_as_conversation(session_id)
    db.close()
    return replayed


def _make_real_persistence_agent(tmp_path):
    """Bind the production persistence funnel to a real temporary SessionDB."""
    from hermes_state import SessionDB
    from run_agent import AIAgent

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = db.create_session("redaction-production-path", "cli")
    agent = MagicMock(spec=AIAgent)
    for method_name in (
        "_persist_session",
        "_drop_trailing_empty_response_scaffolding",
        "_save_session_log",
        "_flush_messages_to_session_db",
        "_flush_messages_to_session_db_unlocked",
    ):
        setattr(agent, method_name, getattr(AIAgent, method_name).__get__(agent))
    agent._clean_session_content = AIAgent._clean_session_content
    agent._redact_message_content = AIAgent._redact_message_content

    agent._session_db = db
    agent._session_db_created = True
    agent._session_persist_lock = None
    agent._persist_disabled = False
    agent._last_flushed_db_idx = 0
    agent._flushed_db_message_ids = set()
    agent._flushed_db_message_session_id = None
    agent._db_flush_scan_prefix = None
    agent._persist_user_message_idx = None
    agent._persist_user_message_override = None
    agent._persist_user_message_timestamp = None
    agent._pending_cli_user_message = None
    agent._active_compression_lock_holder = None
    agent._inflight_turn_id = None
    agent._inflight_turn_session_id = None

    agent._session_json_enabled = True
    agent.logs_dir = tmp_path / "sessions"
    agent.logs_dir.mkdir()
    agent.session_id = session_id
    agent.model = "test/model"
    agent.base_url = "https://openrouter.ai/api/v1"
    agent.platform = "cli"
    agent.session_start = datetime.now()
    agent._cached_system_prompt = "test system prompt"
    agent.tools = []
    agent.verbose_logging = False
    return agent, db


def test_mutable_reasoning_is_redacted_before_callback_and_persistence(tmp_path):
    agent = _make_agent()
    agent.reasoning_callback = MagicMock()

    built = agent._build_assistant_message(
        _api_message(
            reasoning=f"I connected with {SECRET_URI}",
        ),
        "stop",
    )

    callback_text = agent.reasoning_callback.call_args.args[0]
    assert SECRET_PASSWORD not in callback_text
    assert SECRET_PASSWORD not in built["reasoning"]
    assert SECRET_PASSWORD not in built["reasoning_content"]

    replayed = _persist_message(tmp_path, built)
    assistant = next(item for item in replayed if item["role"] == "assistant")
    assert assistant["reasoning"] == built["reasoning"]
    assert assistant["reasoning_content"] == built["reasoning_content"]

    password_bytes = SECRET_PASSWORD.encode()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert password_bytes not in path.read_bytes(), path.name


def test_provider_reasoning_content_is_preserved_for_replay(tmp_path):
    provider_reasoning = f"provider-owned bytes: {SECRET_URI}"
    built = _make_agent()._build_assistant_message(
        _api_message(
            reasoning=f"mutable copy: {SECRET_URI}",
            reasoning_content=provider_reasoning,
        ),
        "stop",
    )

    assert SECRET_PASSWORD not in built["reasoning"]
    assert built["reasoning_content"] == provider_reasoning

    replayed = _persist_message(tmp_path, built)
    assistant = next(item for item in replayed if item["role"] == "assistant")
    assert assistant["reasoning_content"] == provider_reasoning


def test_mutable_reasoning_stays_redacted_through_production_persistence(tmp_path):
    built = _make_agent()._build_assistant_message(
        _api_message(reasoning=f"production path: {SECRET_URI}"),
        "stop",
    )
    persistence_agent, db = _make_real_persistence_agent(tmp_path)

    persistence_agent._persist_session([built], conversation_history=[])
    replayed = db.get_messages_as_conversation(persistence_agent.session_id)
    db.close()

    assistant = next(item for item in replayed if item["role"] == "assistant")
    assert SECRET_PASSWORD not in assistant["reasoning"]
    assert SECRET_PASSWORD not in assistant["reasoning_content"]

    snapshot = (
        persistence_agent.logs_dir
        / f"session_{persistence_agent.session_id}.json"
    )
    assert snapshot.exists()
    assert SECRET_PASSWORD not in snapshot.read_text(encoding="utf-8")

    password_bytes = SECRET_PASSWORD.encode()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert password_bytes not in path.read_bytes(), path.name


def test_native_gemini_reasoning_cannot_bypass_persistence_redaction(tmp_path):
    from agent.gemini_native_adapter import translate_gemini_response
    from agent.transports import get_transport

    response = translate_gemini_response(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "thought": True,
                                "text": f"native Gemini thought: {SECRET_URI}",
                            },
                            {"text": "done"},
                        ]
                    },
                    "finishReason": "STOP",
                }
            ]
        },
        model="gemini-2.5-flash",
    )
    normalized = get_transport("chat_completions").normalize_response(response)
    built = _make_agent()._build_assistant_message(normalized, "stop")

    assert SECRET_PASSWORD not in built["reasoning"]
    assert SECRET_PASSWORD not in built["reasoning_content"]

    persistence_agent, db = _make_real_persistence_agent(tmp_path)
    persistence_agent._persist_session([built], conversation_history=[])
    db.close()

    password_bytes = SECRET_PASSWORD.encode()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert password_bytes not in path.read_bytes(), path.name


def test_bedrock_reasoning_cannot_bypass_persistence_redaction(tmp_path):
    from agent.bedrock_adapter import normalize_converse_response
    from agent.transports import get_transport

    response = normalize_converse_response(
        {
            "output": {
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "reasoningContent": {
                                "text": f"Bedrock thought: {SECRET_URI}"
                            }
                        },
                        {"text": "done"},
                    ],
                }
            },
            "stopReason": "end_turn",
        }
    )
    normalized = get_transport("bedrock_converse").normalize_response(response)
    built = _make_agent()._build_assistant_message(normalized, "stop")

    assert SECRET_PASSWORD not in built["reasoning"]
    assert SECRET_PASSWORD not in built["reasoning_content"]

    persistence_agent, db = _make_real_persistence_agent(tmp_path)
    persistence_agent._persist_session([built], conversation_history=[])
    db.close()

    password_bytes = SECRET_PASSWORD.encode()
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert password_bytes not in path.read_bytes(), path.name


def test_provider_reasoning_details_are_preserved_for_next_turn(tmp_path):
    details = [
        {
            "type": "reasoning.summary",
            "summary": [
                {"type": "summary_text", "text": f"summary: {SECRET_URI}"}
            ],
        },
        {
            "type": "reasoning.text",
            "content": {"text": f"nested content: {SECRET_URI}"},
            "data": "provider-data",
        },
    ]
    original = copy.deepcopy(details)

    built = _make_agent()._build_assistant_message(
        _api_message(reasoning=f"mutable: {SECRET_URI}", reasoning_details=details),
        "stop",
    )

    assert details == original
    assert built["reasoning_details"] == original
    assert SECRET_PASSWORD not in built["reasoning"]

    replayed = _persist_message(tmp_path, built)
    assistant = next(item for item in replayed if item["role"] == "assistant")
    assert assistant["reasoning_details"] == original

    from agent.transports import get_transport

    outgoing = get_transport("chat_completions").convert_messages(
        [assistant], model="openrouter/test-model"
    )
    assert outgoing[0]["reasoning_details"] == original


@pytest.mark.parametrize(
    "detail",
    [
        {
            "type": "thinking",
            "thinking": f"signed: {SECRET_URI}",
            "signature": "provider-signature",
        },
        {
            "type": "reasoning.text",
            "text": f"encrypted: {SECRET_URI}",
            "encrypted_content": "provider-ciphertext",
        },
        {
            "type": "reasoning.encrypted",
            "text": f"opaque: {SECRET_URI}",
            "data": "provider-data",
        },
        {
            "type": "redacted_thinking",
            "text": f"opaque: {SECRET_URI}",
            "data": "provider-data",
        },
    ],
)
def test_all_provider_reasoning_detail_shapes_are_preserved(detail):
    original = copy.deepcopy(detail)
    built = _make_agent()._build_assistant_message(
        _api_message(reasoning="mutable", reasoning_details=[detail]),
        "stop",
    )

    assert detail == original
    assert built["reasoning_details"][0] == original


def test_sdk_shaped_reasoning_details_are_preserved():
    dict_backed = SimpleNamespace(
        type="reasoning.text",
        text=f"provider text: {SECRET_URI}",
        provider_field="keep-me",
    )

    class ModelDumpOnly:
        __slots__ = ()

        def model_dump(self):
            return {
                "type": "reasoning.summary",
                "summary": f"provider summary: {SECRET_URI}",
                "provider_field": "keep-me-too",
            }

    built = _make_agent()._build_assistant_message(
        _api_message(
            reasoning="mutable",
            reasoning_details=[dict_backed, ModelDumpOnly()],
        ),
        "stop",
    )

    assert built["reasoning_details"] == [
        dict_backed.__dict__,
        ModelDumpOnly().model_dump(),
    ]


def test_nested_summary_is_extracted_as_mutable_redacted_reasoning():
    details = [
        {
            "type": "reasoning.summary",
            "summary": [
                {"type": "summary_text", "text": f"summary: {SECRET_URI}"}
            ],
        }
    ]

    built = _make_agent()._build_assistant_message(
        _api_message(reasoning_details=details),
        "stop",
    )

    assert built["reasoning"].startswith("summary: ")
    assert SECRET_PASSWORD not in built["reasoning"]
    assert built["reasoning_details"] == details


def test_deep_reasoning_summary_is_extracted_without_recursion_failure():
    nested = {"text": "deep summary"}
    for _ in range(1_500):
        nested = {"summary": [nested]}

    built = _make_agent()._build_assistant_message(
        _api_message(
            reasoning_details=[
                {"type": "reasoning.summary", "summary": [nested]}
            ]
        ),
        "stop",
    )

    assert built["reasoning"] == "deep summary"


def test_tool_call_arguments_remain_exact():
    arguments = f'{{"command":"psql {SECRET_URI}"}}'
    tool_call = SimpleNamespace(
        id="call-1",
        call_id="call-1",
        response_item_id="response-1",
        type="function",
        function=SimpleNamespace(name="terminal", arguments=arguments),
        extra_content=None,
    )

    built = _make_agent()._build_assistant_message(
        _api_message(
            content="",
            reasoning=f"mutable: {SECRET_URI}",
            tool_calls=[tool_call],
        ),
        "tool_calls",
    )

    assert built["tool_calls"][0]["function"]["arguments"] == arguments
    assert SECRET_PASSWORD not in built["reasoning"]


def test_redaction_opt_out_preserves_mutable_reasoning(monkeypatch):
    monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
    details = [{"type": "reasoning.text", "text": f"detail: {SECRET_URI}"}]

    built = _make_agent()._build_assistant_message(
        _api_message(
            content=f"content: {SECRET_URI}",
            reasoning=f"reasoning: {SECRET_URI}",
            reasoning_details=details,
        ),
        "stop",
    )

    assert SECRET_URI in built["content"]
    assert SECRET_URI in built["reasoning"]
    assert SECRET_URI in built["reasoning_details"][0]["text"]


def test_redacted_terminal_output_leaves_no_password_in_new_database(tmp_path):
    from agent.redact import redact_terminal_output

    raw_output = f"connected successfully: {SECRET_URI}"
    safe_output = redact_terminal_output(raw_output, "printf database-uri")
    assert SECRET_PASSWORD not in safe_output

    _persist_message(
        tmp_path,
        {
            "role": "tool",
            "content": safe_output,
            "tool_name": "terminal",
            "tool_call_id": "call-1",
        },
    )

    password_bytes = SECRET_PASSWORD.encode()
    database_files = [path for path in tmp_path.rglob("*") if path.is_file()]
    assert database_files
    for path in database_files:
        assert password_bytes not in path.read_bytes(), path.name
