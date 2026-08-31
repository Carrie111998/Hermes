from __future__ import annotations

import os
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from hermes_state import SessionDB


def _agent_with_blocking_compressor(
    db: SessionDB,
    session_id: str,
    started: threading.Event,
    release: threading.Event,
):
    with patch.dict(os.environ, {"OPENROUTER_API_KEY": "test-key"}):
        from run_agent import AIAgent

        agent = AIAgent(
            api_key="test-key",
            base_url="https://openrouter.ai/api/v1",
            model="test/model",
            quiet_mode=True,
            session_db=db,
            session_id=session_id,
            skip_context_files=True,
            skip_memory=True,
        )

    compressor = agent.context_compressor

    def _compress(messages, **_kwargs):
        started.set()
        assert release.wait(2.0)
        return [
            {"role": "user", "content": "[CONTEXT COMPACTION] summary"},
            dict(messages[-1]),
        ]

    compressor.compress = MagicMock(side_effect=_compress)
    compressor.has_content_to_compress = lambda _messages: True
    compressor.compression_count = 0
    compressor.last_prompt_tokens = 80
    compressor.last_completion_tokens = 0
    compressor.context_length = 200
    compressor.threshold_tokens = 100
    compressor._last_summary_error = None
    compressor._last_compress_aborted = False
    compressor._last_aux_model_failure_model = None
    compressor._last_aux_model_failure_error = None

    agent._compression_feasibility_checked = True
    agent.compression_in_place = True
    agent._background_compression_enabled = True
    agent._background_compression_start_ratio = 0.75
    from agent.context_compressor import ContextCompressor

    assert isinstance(agent.context_compressor, ContextCompressor)
    assert agent.context_compressor.threshold_tokens == 100
    assert agent.context_compressor.last_prompt_tokens == 80
    return agent


def test_background_compaction_splices_durable_tail_without_blocking(tmp_path: Path) -> None:
    from agent.background_compression import (
        adopt_completed_background_compression,
        maybe_start_background_compression,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_SPLICE"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "current answer"},
    ]
    db.append_messages_batch(session_id, history)

    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)

    assert maybe_start_background_compression(agent, history, "system") is True
    assert started.wait(3.0)

    concurrent_tail = {"role": "user", "content": "arrived while summarizing"}
    db.append_messages_batch(session_id, [concurrent_tail])
    release.set()

    job = agent._background_compression_job
    assert job.done.wait(3.0)
    assert job.error is None

    adopted = adopt_completed_background_compression(agent, history + [concurrent_tail])

    assert [message["content"] for message in adopted] == [
        "[CONTEXT COMPACTION] summary",
        "current answer",
        "arrived while summarizing",
    ]
    assert agent._last_compaction_in_place is True
    assert agent._last_compression_attempt_in_place is True
    assert db.get_messages_as_conversation(session_id) == adopted


def test_adoption_suppresses_reschedule_until_fresh_real_usage(tmp_path: Path) -> None:
    from agent.background_compression import (
        adopt_completed_background_compression,
        maybe_start_background_compression,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_USAGE_REBASE"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "current question"},
        {"role": "assistant", "content": "current answer"},
    ]
    db.append_messages_batch(session_id, history)
    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)
    agent._usage_anchor = object()

    assert maybe_start_background_compression(agent, history, "system") is True
    assert started.wait(3.0)
    release.set()
    assert agent._background_compression_job.done.wait(3.0)

    adopted = adopt_completed_background_compression(agent, history)

    assert agent.context_compressor.last_compression_rough_tokens > 0
    assert agent.context_compressor.last_prompt_tokens == -1
    assert agent.context_compressor.last_completion_tokens == 0
    assert agent.context_compressor.awaiting_real_usage_after_compression is True
    assert agent._usage_anchor is None
    assert maybe_start_background_compression(agent, adopted, "system") is False

    agent.context_compressor.update_from_response(
        {"prompt_tokens": 80, "completion_tokens": 5}
    )

    assert agent.context_compressor.awaiting_real_usage_after_compression is False
    assert maybe_start_background_compression(agent, adopted, "system") is True
    assert agent._background_compression_job.done.wait(3.0)


def test_inflight_job_is_never_adopted_or_started_twice(tmp_path: Path) -> None:
    from agent.background_compression import (
        adopt_completed_background_compression,
        maybe_start_background_compression,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_INFLIGHT"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    db.append_messages_batch(session_id, history)
    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)

    assert maybe_start_background_compression(agent, history, "system") is True
    assert started.wait(3.0)
    assert maybe_start_background_compression(agent, history, "system") is False
    assert adopt_completed_background_compression(agent, history) is history

    release.set()
    assert agent._background_compression_job.done.wait(3.0)


def test_failed_background_compaction_preserves_original_transcript(tmp_path: Path) -> None:
    from agent.background_compression import (
        adopt_completed_background_compression,
        maybe_start_background_compression,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_FAILURE"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    db.append_messages_batch(session_id, history)
    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)

    def _fail(*_args, **_kwargs):
        started.set()
        assert release.wait(2.0)
        raise RuntimeError("summary failed")

    agent.context_compressor.compress.side_effect = _fail
    assert maybe_start_background_compression(agent, history, "system") is True
    assert started.wait(3.0)
    release.set()
    assert agent._background_compression_job.done.wait(3.0)

    adopted = adopt_completed_background_compression(agent, history)

    assert adopted is history
    durable = db.get_messages_as_conversation(session_id)
    assert [(m["role"], m["content"]) for m in durable] == [
        ("user", "question"),
        ("assistant", "answer"),
    ]


def test_completed_job_from_previous_session_is_not_adopted(tmp_path: Path) -> None:
    from agent.background_compression import (
        adopt_completed_background_compression,
        maybe_start_background_compression,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_OLD_SESSION"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    db.append_messages_batch(session_id, history)
    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)

    assert maybe_start_background_compression(agent, history, "system") is True
    assert started.wait(3.0)
    release.set()
    assert agent._background_compression_job.done.wait(3.0)
    agent.session_id = "NEW_SESSION"

    assert adopt_completed_background_compression(agent, history) is history


def test_background_compaction_respects_opt_in_and_pressure_threshold(tmp_path: Path) -> None:
    from agent.background_compression import maybe_start_background_compression

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_GATES"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)

    agent._background_compression_enabled = False
    assert maybe_start_background_compression(agent, history, "system") is False

    agent._background_compression_enabled = True
    agent.context_compressor.last_prompt_tokens = 74
    assert maybe_start_background_compression(agent, history, "system") is False
    assert not started.is_set()


def test_adoption_publishes_prompt_rebuilt_by_background_worker(tmp_path: Path) -> None:
    from agent.background_compression import (
        adopt_completed_background_compression,
        maybe_start_background_compression,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_PROMPT_REBUILD"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    db.append_messages_batch(session_id, history)
    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)
    agent._cached_system_prompt = "OLD PROMPT"
    agent._cached_system_prompt_static = "OLD STATIC"

    def _rebuild_prompt(worker, _system_message):
        worker._cached_system_prompt_static = "NEW STATIC"
        return "NEW PROMPT"

    with patch.object(type(agent), "_build_system_prompt", _rebuild_prompt):
        assert maybe_start_background_compression(agent, history, "system") is True
        assert started.wait(3.0)
        release.set()
        assert agent._background_compression_job.done.wait(3.0)

        # Worker state stays private until the next foreground turn boundary.
        assert agent._cached_system_prompt == "OLD PROMPT"
        assert agent._cached_system_prompt_static == "OLD STATIC"

        adopted = adopt_completed_background_compression(agent, history)

    assert adopted is not history
    assert agent._cached_system_prompt == "NEW PROMPT"
    assert agent._cached_system_prompt_static == "NEW STATIC"


def test_adoption_rebuilds_prompt_and_tools_from_live_runtime_after_model_switch(
    tmp_path: Path,
) -> None:
    from agent.background_compression import (
        adopt_completed_background_compression,
        maybe_start_background_compression,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_MODEL_SWITCH"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    db.append_messages_batch(session_id, history)
    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)
    agent.model = "model-a"

    def _rebuild_prompt(target, _system_message):
        target._cached_system_prompt_static = f"STATIC:{target.model}"
        return f"PROMPT:{target.model}"

    def _refresh_tools(target):
        name = f"tool-{target.model}"
        target.tools = [
            {
                "type": "function",
                "function": {"name": name, "description": "", "parameters": {}},
            }
        ]
        target.valid_tool_names = {name}
        target._context_engine_tool_names = set()
        target._tool_snapshot_generation += 1
        return True

    with (
        patch.object(type(agent), "_build_system_prompt", _rebuild_prompt),
        patch(
            "agent.conversation_compression._refresh_agent_tool_definitions",
            side_effect=_refresh_tools,
        ),
    ):
        assert maybe_start_background_compression(agent, history, "system") is True
        assert started.wait(3.0)
        agent.model = "model-b"
        agent._cached_system_prompt = None
        release.set()
        assert agent._background_compression_job.done.wait(3.0)

        adopted = adopt_completed_background_compression(agent, history)

    assert adopted is not history
    assert agent._cached_system_prompt == "PROMPT:model-b"
    assert agent._cached_system_prompt_static == "STATIC:model-b"
    assert agent.valid_tool_names == {"tool-model-b"}
    assert agent.tools[0]["function"]["name"] == "tool-model-b"
    assert db.get_session(session_id)["system_prompt"] == "PROMPT:model-b"


def test_external_memory_provider_disables_background_compaction(tmp_path: Path) -> None:
    from agent.background_compression import maybe_start_background_compression

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_EXTERNAL_MEMORY_GATE"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)
    provider = SimpleNamespace(name="external")
    agent._memory_manager = SimpleNamespace(providers=[provider])

    assert maybe_start_background_compression(agent, history, "system") is False
    assert agent._background_compression_job is None
    assert not started.is_set()


def test_adoption_retries_after_transient_history_reload_failure(tmp_path: Path) -> None:
    from agent.background_compression import (
        adopt_completed_background_compression,
        maybe_start_background_compression,
    )

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_ADOPTION_RETRY"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "old question"},
        {"role": "assistant", "content": "old answer"},
    ]
    db.append_messages_batch(session_id, history)
    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)

    assert maybe_start_background_compression(agent, history, "system") is True
    assert started.wait(3.0)
    release.set()
    job = agent._background_compression_job
    assert job.done.wait(3.0)
    durable = db.get_messages_as_conversation(session_id)

    with patch.object(
        db,
        "get_messages_as_conversation",
        side_effect=[RuntimeError("transient read failure"), durable],
    ):
        assert adopt_completed_background_compression(agent, history) is history
        assert agent._background_compression_job is job
        adopted = adopt_completed_background_compression(agent, history)

    assert adopted == durable
    assert agent._background_compression_job is None
    assert agent._last_compaction_in_place is True


def test_thread_start_failure_restores_scheduling_retryability(tmp_path: Path) -> None:
    from agent.background_compression import maybe_start_background_compression

    db = SessionDB(db_path=tmp_path / "state.db")
    session_id = "BACKGROUND_THREAD_START_RETRY"
    db.create_session(session_id, source="test")
    history = [
        {"role": "user", "content": "question"},
        {"role": "assistant", "content": "answer"},
    ]
    db.append_messages_batch(session_id, history)
    started = threading.Event()
    release = threading.Event()
    agent = _agent_with_blocking_compressor(db, session_id, started, release)

    with patch("agent.background_compression.threading.Thread.start", side_effect=RuntimeError("no thread")):
        assert maybe_start_background_compression(agent, history, "system") is False

    assert agent._background_compression_job is None
    release.set()
    assert maybe_start_background_compression(agent, history, "system") is True
    assert agent._background_compression_job.done.wait(3.0)
