from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from plugins.context_engine.tiered_pipeline import (
    TieredPipelineEngine,
    build_engine_from_config,
)
from plugins.context_engine.tiered_pipeline.storage import TieredContextStore


def test_engine_triggers_at_fixed_50k_tokens(tmp_path):
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")

    assert engine.name == "tiered_pipeline"
    assert engine.threshold_tokens == 50_000
    assert engine.should_compress(49_999) is False
    assert engine.should_compress(50_000) is True


def test_small_model_uses_85_percent_emergency_trigger_before_physical_overflow(tmp_path):
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.update_model("small-model", context_length=32_000)

    assert engine.threshold_tokens == 27_200
    assert engine.should_compress(27_199) is False
    assert engine.should_compress(27_200) is True


def test_l2_overflow_archives_low_value_capsule_to_l3_and_remains_searchable(tmp_path):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        l2_max_topics=2,
        l2_archive_target_ratio=0.5,
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))

    engine.store_capsule("old", "legacy database migration", importance=0.1)
    engine.store_capsule("keep", "active API design", importance=0.9)
    engine.store_capsule("new", "new test strategy", importance=0.8)

    status = engine.get_status()
    assert status["l2_topics"] == 1
    assert status["l3_topics"] == 2

    hits = engine.search("database migration", limit=5)
    assert hits[0]["topic_id"] == "old"
    assert hits[0]["tier"] == "L3"


def test_l2_retention_prioritizes_unresolved_work(tmp_path):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        l2_max_topics=2,
        l2_archive_target_ratio=0.5,
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))

    engine.store_capsule("pending", "blocked unfinished task", importance=0.1, unresolved=True)
    engine.store_capsule("resolved", "resolved high-value note", importance=0.9)
    engine.store_capsule("new", "new completed note", importance=0.8)

    remaining = engine.store.list_topics(session_id="session-1")
    assert [topic["topic_id"] for topic in remaining if topic["tier"] == "L2"] == ["pending"]


def test_l2_capacity_one_keeps_one_hot_topic(tmp_path):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        l2_max_topics=1,
        l2_archive_target_ratio=0.7,
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))

    engine.store_capsule("first", "first summary")
    engine.store_capsule("second", "second summary")

    assert engine.store.count("L2", "session-1") == 1
    assert engine.store.count("L3", "session-1") == 1


def test_pinned_topic_stays_hot_when_l2_overflows(tmp_path):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        l2_max_topics=1,
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.store_capsule("pinned", "important pinned state", pinned=True)
    engine.store_capsule("ordinary", "ordinary completed state")

    hot = [
        topic["topic_id"]
        for topic in engine.store.list_topics(session_id="session-1")
        if topic["tier"] == "L2"
    ]
    assert hot == ["pinned"]


def test_compress_archives_only_non_active_topic_and_preserves_active_task_verbatim(tmp_path):
    archived_inputs = []

    def summarizer(messages, **_kwargs):
        archived_inputs.extend(messages)
        return "## Goal\nMaintain the legacy database.\n## Key Decisions\nUse SQLite."

    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        protect_last_n=2,
        summarizer=summarizer,
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "Discuss the legacy database migration"},
        {"role": "assistant", "content": "We will migrate it with SQLite."},
        {"role": "user", "content": "NEW TASK: implement the payment API now"},
        {"role": "assistant", "content": "I am implementing the payment API."},
    ]

    compacted = engine.compress(messages, current_tokens=50_000)

    assert messages[-2:] == compacted[-2:]
    assert all("legacy database" not in str(message.get("content", "")) for message in compacted)
    assert archived_inputs == messages[1:3]

    topics = engine.store.list_topics(session_id="session-1")
    assert len(topics) == 1
    raw = engine.store.get_raw_messages(
        topics[0]["topic_id"],
        session_id="session-1",
    )
    assert raw == messages[1:3]


def test_select_context_recalls_relevant_capsules_without_rewriting_current_request(tmp_path):
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.store_capsule(
        "legacy-db",
        "The legacy database migration uses SQLite and still needs a rollback test.",
        title="Legacy database migration",
    )
    request = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "How should the legacy database migration continue?"},
    ]

    selected = engine.select_context(
        request,
        conversation_messages=request,
        incoming_message=request[-1],
        budget_tokens=200_000,
    )

    assert len(selected) == len(request)
    assert selected[-1]["role"] == "user"
    assert selected[-1]["content"].startswith(request[-1]["content"])
    assert "[TIERED CONTEXT RECALL]" in selected[-1]["content"]
    assert "rollback test" in selected[-1]["content"]
    assert all(
        previous["role"] != current["role"]
        for previous, current in zip(selected, selected[1:])
    )


def test_select_context_honors_token_budget(tmp_path):
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.store_capsule("large", "historical marker " * 500, title="Large history")
    request = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "historical marker"},
    ]

    selected = engine.select_context(
        request,
        incoming_message=request[-1],
        budget_tokens=1,
    )

    assert selected is None


def test_search_matches_chinese_queries_without_whitespace(tmp_path):
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.store_capsule(
        "database-migration",
        "旧数据库迁移使用 SQLite，并且还需要回滚测试。",
        title="数据库迁移",
    )

    hits = engine.search("数据库迁移应该如何继续", limit=5)

    assert hits[0]["topic_id"] == "database-migration"


def test_engine_tools_search_recall_pin_and_list_topics(tmp_path):
    import json

    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    source = [{"role": "user", "content": "exact historical evidence"}]
    engine.store_capsule(
        "topic-1",
        "Historical payment API decision.",
        title="Payment API",
        raw_messages=source,
    )

    names = {schema["name"] for schema in engine.get_tool_schemas()}
    assert names == {
        "context_search",
        "context_recall",
        "context_list_topics",
        "context_pin_topic",
        "context_status",
    }
    search_result = json.loads(engine.handle_tool_call("context_search", {"query": "payment API"}))
    assert search_result["results"][0]["topic_id"] == "topic-1"
    recall_result = json.loads(engine.handle_tool_call("context_recall", {"topic_id": "topic-1"}))
    assert recall_result["messages"] == source
    pin_result = json.loads(engine.handle_tool_call("context_pin_topic", {"topic_id": "topic-1", "pinned": True}))
    assert pin_result["success"] is True


def test_search_and_recall_tools_bound_output_and_page_oversized_raw_messages(tmp_path):
    import json

    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.store_capsule(
        "huge-topic",
        "bounded-search-marker " + ("summary payload " * 10_000),
        title="Huge topic",
        raw_messages=[{"role": "user", "content": "\\" * 20_000}],
    )

    search_text = engine.handle_tool_call(
        "context_search",
        {"query": "bounded-search-marker", "limit": 20},
    )
    recall_text = engine.handle_tool_call(
        "context_recall",
        {"topic_id": "huge-topic", "offset": 0, "limit": 20},
    )
    recall = json.loads(recall_text)

    assert len(search_text) <= 16_000
    assert len(recall_text) <= 16_000
    assert recall["truncated"] is True
    assert recall["fragment"]["message_index"] == 0
    assert recall["fragment"]["next_fragment_offset"] > 0


def test_recall_and_pin_tools_cannot_cross_session_boundary(tmp_path):
    import json

    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.store_capsule(
        "private-topic",
        "private historical summary",
        raw_messages=[{"role": "user", "content": "private source"}],
    )
    engine.on_session_start("session-2", hermes_home=str(tmp_path))

    recall = json.loads(engine.handle_tool_call("context_recall", {"topic_id": "private-topic"}))
    pin = json.loads(
        engine.handle_tool_call(
            "context_pin_topic",
            {"topic_id": "private-topic", "pinned": True},
        )
    )

    assert recall["messages"] == []
    assert pin["success"] is False


def test_same_topic_id_is_isolated_across_sessions(tmp_path):
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.store_capsule(
        "shared-id",
        "first summary",
        raw_messages=[{"role": "user", "content": "first source"}],
    )
    engine.on_session_start("session-2", hermes_home=str(tmp_path))
    engine.store_capsule(
        "shared-id",
        "second summary",
        raw_messages=[{"role": "user", "content": "second source"}],
    )

    assert engine.store.get_raw_messages("shared-id", session_id="session-1") == [
        {"role": "user", "content": "first source"}
    ]
    assert engine.store.get_raw_messages("shared-id", session_id="session-2") == [
        {"role": "user", "content": "second source"}
    ]


def test_raw_archive_rejects_lossy_values_and_rolls_back(tmp_path):
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("session-1", hermes_home=str(tmp_path))

    with pytest.raises(TypeError):
        engine.store_capsule(
            "invalid-raw",
            "must not persist",
            raw_messages=[{"role": "user", "content": object()}],
        )

    assert engine.store.count("L2", "session-1") == 0


def test_capsule_and_retention_update_are_one_transaction(tmp_path, monkeypatch):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        l2_max_topics=1,
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.store_capsule("first", "first summary")

    monkeypatch.setattr(
        engine.store,
        "_archive_l2_locked",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    with pytest.raises(sqlite3.OperationalError):
        engine.store_capsule("second", "second summary")

    topics = engine.store.list_topics(session_id="session-1")
    assert [topic["topic_id"] for topic in topics] == ["first"]


def test_updating_capsule_without_raw_messages_clears_stale_evidence(tmp_path):
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.store_capsule(
        "topic",
        "first summary",
        raw_messages=[{"role": "user", "content": "old evidence"}],
    )
    engine.store_capsule("topic", "replacement summary", raw_messages=None)

    assert engine.store.get_raw_messages("topic", session_id="session-1") == []


def test_store_can_be_reused_from_a_gateway_worker_thread(tmp_path):
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.store_capsule("threaded", "thread worker search target")

    with ThreadPoolExecutor(max_workers=1) as pool:
        hits = pool.submit(engine.search, "worker search", 5).result()

    assert hits[0]["topic_id"] == "threaded"


def test_incompatible_existing_database_is_rejected_without_mutation(tmp_path):
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE capsules (
                topic_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                title TEXT NOT NULL, summary TEXT NOT NULL, tier TEXT NOT NULL,
                importance REAL NOT NULL, unresolved INTEGER NOT NULL,
                pinned INTEGER NOT NULL, access_count INTEGER NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                last_access_at REAL NOT NULL, source_tokens INTEGER NOT NULL,
                source_message_ids TEXT NOT NULL, metadata TEXT NOT NULL
            );
            CREATE TABLE raw_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, topic_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL, message_json TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        db.execute(
            "INSERT INTO capsules VALUES "
            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-topic", "session-1", "Legacy", "legacy summary", "L2",
                0.5, 0, 0, 0, 1.0, 1.0, 1.0, 10, "[]", "{}",
            ),
        )
        db.execute(
            "INSERT INTO raw_messages(topic_id, ordinal, message_json, created_at) "
            "VALUES (?, ?, ?, ?)",
            ("legacy-topic", 0, '{"role":"user","content":"legacy raw"}', 1.0),
        )

    with pytest.raises(RuntimeError, match="incompatible"):
        TieredContextStore(path)

    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM capsules").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM raw_messages").fetchone()[0] == 1


def test_nested_config_builds_engine_with_requested_budgets(tmp_path):
    engine = build_engine_from_config(
        {
            "tiered_pipeline": {
                "l1": {"trigger_tokens": 50_000, "protect_last_n": 32},
                "l2": {"max_topics": 99, "archive_target_ratio": 0.6},
                "l3": {"path": str(tmp_path / "custom.db")},
                "recall": {"top_k": 7, "max_chars": 9000},
                "prune": {
                    "trigger_tokens": 25_000,
                    "min_result_chars": 7000,
                    "min_reclaim_tokens": 2000,
                },
            }
        }
    )

    assert engine.threshold_tokens == 50_000
    assert engine.protect_last_n == 32
    assert engine.l2_max_topics == 99
    assert engine.storage_path == tmp_path / "custom.db"
    assert engine.recall_top_k == 7
    assert engine.recall_max_chars == 9000
    assert engine.proactive_prune_tokens == 25_000
    assert engine.proactive_prune_min_result_chars == 7000
    assert engine.proactive_prune_min_reclaim_tokens == 2000


def test_nested_config_rejects_malformed_sections():
    with pytest.raises(ValueError, match="tiered_pipeline.l1"):
        build_engine_from_config({"tiered_pipeline": {"l1": ["invalid"]}})


def test_single_active_task_bypasses_normal_pipeline_until_emergency_checkpoint(tmp_path):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        protect_last_n=2,
        summarizer=lambda messages, **_kwargs: "## Active State\nCheckpointed current task.",
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.update_model("test", context_length=100_000)
    messages = [{"role": "system", "content": "system"}]
    for index in range(4):
        messages.extend(
            [
                {"role": "user", "content": f"continue the same task step {index}"},
                {"role": "assistant", "content": f"completed same task step {index}"},
            ]
        )

    assert engine.compress(messages, current_tokens=50_000) == messages

    checkpointed = engine.compress(messages, current_tokens=85_000)
    assert checkpointed[-2:] == messages[-2:]
    assert len(checkpointed) < len(messages)
    topics = engine.store.list_topics(session_id="session-1")
    assert len(topics) == 1


def test_emergency_checkpoint_keeps_a_user_turn_boundary_with_odd_hot_tail(tmp_path):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        protect_last_n=3,
        summarizer=lambda messages, **_kwargs: "## Active State\nCheckpoint.",
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.update_model("test", context_length=100_000)
    messages = [{"role": "system", "content": "system"}]
    for index in range(4):
        messages.extend(
            [
                {"role": "user", "content": f"step {index}"},
                {"role": "assistant", "content": f"result {index}"},
            ]
        )

    checkpointed = engine.compress(messages, current_tokens=85_000)

    assert checkpointed[1]["role"] == "assistant"
    assert "[TIERED ACTIVE TASK CHECKPOINT]" in checkpointed[1]["content"]
    assert checkpointed[2]["role"] == "user"
    assert all(
        previous["role"] != current["role"]
        for previous, current in zip(checkpointed, checkpointed[1:])
    )


def test_emergency_checkpoint_compacts_active_task_after_prior_topic_is_gone(tmp_path):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        protect_last_n=2,
        summarizer=lambda messages, **_kwargs: "## Checkpoint\nPreserve active implementation state.",
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.update_model("test", context_length=100_000)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old unrelated task"},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "NEW TASK: active implementation"},
        {"role": "assistant", "content": "active result 1"},
        {"role": "user", "content": "continue active implementation"},
        {"role": "assistant", "content": "active result 2"},
    ]

    active_only = engine.compress(messages, current_tokens=50_000)
    checkpointed = engine.compress(active_only, current_tokens=85_000)

    assert checkpointed[-2:] == active_only[-2:]
    assert len(checkpointed) < len(active_only)
    assert engine.store.count("L2", "session-1") == 2


def test_emergency_checkpoint_uses_tracked_usage_when_current_tokens_is_none(tmp_path):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        protect_last_n=2,
        summarizer=lambda messages, **_kwargs: "## Checkpoint\nTracked usage.",
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.update_model("test", context_length=100_000)
    engine.update_from_response({"prompt_tokens": 85_000})
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "step one"},
        {"role": "assistant", "content": "result one"},
        {"role": "user", "content": "step two"},
        {"role": "assistant", "content": "result two"},
    ]

    checkpointed = engine.compress(messages, current_tokens=None)

    assert len(checkpointed) < len(messages)


def test_emergency_checkpoint_reclaims_oversized_protected_tail(tmp_path):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        protect_last_n=20,
        summarizer=lambda messages, **_kwargs: "High-fidelity current task checkpoint.",
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.update_model("test", context_length=100_000)
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "current requirements " * 12_000},
        {"role": "assistant", "content": "implementation details " * 12_000},
    ]

    checkpointed = engine.compress(messages, current_tokens=85_000)

    assert checkpointed != messages
    assert any(
        "[TIERED ACTIVE TASK CHECKPOINT]" in str(message.get("content") or "")
        for message in checkpointed
    )
    assert len(str(checkpointed)) < len(str(messages))


def test_storage_failure_during_compression_keeps_original_messages(tmp_path, monkeypatch):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        protect_last_n=2,
        summarizer=lambda messages, **_kwargs: "## Summary\nSafe handoff.",
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "NEW TASK: current"},
        {"role": "assistant", "content": "current result"},
    ]
    monkeypatch.setattr(
        engine.store,
        "put_capsule",
        lambda **_kwargs: (_ for _ in ()).throw(sqlite3.OperationalError("disk full")),
    )

    assert engine.compress(messages, current_tokens=50_000) == messages


def test_summary_exception_during_compression_keeps_original_messages(tmp_path):
    def failing_summarizer(messages, **_kwargs):
        raise RuntimeError("summary provider unavailable")

    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        protect_last_n=2,
        summarizer=failing_summarizer,
    )
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "old task"},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "NEW TASK: current"},
        {"role": "assistant", "content": "current result"},
    ]

    assert engine.compress(messages, current_tokens=50_000) == messages


def test_compression_rotation_keeps_parent_capsules_in_logical_conversation(tmp_path):
    path = tmp_path / "context.db"
    engine = TieredPipelineEngine(storage_path=path)
    engine.on_session_start("parent", hermes_home=str(tmp_path))
    engine.store_capsule(
        "parent-topic",
        "rotation continuity marker",
        raw_messages=[{"role": "user", "content": "exact parent source"}],
    )
    engine.update_from_response(
        {"prompt_tokens": 60_000, "completion_tokens": 7, "total_tokens": 60_007}
    )
    engine.compression_count = 3

    engine.on_session_start(
        "child",
        hermes_home=str(tmp_path),
        old_session_id="parent",
        boundary_reason="compression",
    )

    assert engine.search("continuity marker")[0]["topic_id"] == "parent-topic"
    assert engine.last_prompt_tokens == 60_000
    assert engine.compression_count == 3

    engine.on_session_end("child", [])
    resumed = TieredPipelineEngine(storage_path=path)
    resumed.on_session_start("child", hermes_home=str(tmp_path))

    assert resumed.search("continuity marker")[0]["topic_id"] == "parent-topic"
    assert resumed.store.get_raw_messages(
        "parent-topic", session_id=resumed.scope_id
    ) == [{"role": "user", "content": "exact parent source"}]
    assert resumed.store.pin(
        "parent-topic", True, session_id=resumed.scope_id
    )
    assert resumed.store.list_topics(session_id=resumed.scope_id)[0]["pinned"] == 1
    assert resumed.get_status()["l2_topics"] == 1
    assert resumed.get_status()["logical_scope"] == "parent"

    resumed.on_session_end("child", [])
    host_resumed = TieredPipelineEngine(storage_path=path)
    host_resumed.bind_session_state(session_db=None, session_id="child")

    assert host_resumed.scope_id == "parent"
    assert host_resumed.search("continuity marker")[0]["topic_id"] == "parent-topic"


def test_independent_session_resets_runtime_counters(tmp_path):
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("first", hermes_home=str(tmp_path))
    engine.update_from_response(
        {"prompt_tokens": 60_000, "completion_tokens": 7, "total_tokens": 60_007}
    )
    engine.compression_count = 3

    engine.on_session_end("first", [])
    engine.on_session_start("second", hermes_home=str(tmp_path))

    assert engine.last_prompt_tokens == 0
    assert engine.last_completion_tokens == 0
    assert engine.last_total_tokens == 0
    assert engine.compression_count == 0
    assert not engine.should_compress()


def test_host_reset_only_switch_rebinds_scope_and_isolates_archives(tmp_path):
    from run_agent import AIAgent

    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("old", hermes_home=str(tmp_path))
    engine.store_capsule("old-topic", "alphaonly archive")

    agent = object.__new__(AIAgent)
    agent.context_compressor = engine
    agent.session_id = "new"
    agent._session_db = None
    agent.reset_session_state()

    assert engine.session_id == "new"
    assert engine.scope_id == "new"
    assert engine.search("alphaonly") == []
    engine.store_capsule("new-topic", "betaonly archive")
    assert engine.search("betaonly")[0]["topic_id"] == "new-topic"

    agent.session_id = "old"
    agent.reset_session_state()

    assert engine.session_id == "old"
    assert engine.scope_id == "old"
    assert engine.search("alphaonly")[0]["topic_id"] == "old-topic"
    assert engine.search("betaonly") == []


def test_host_reset_rebinds_after_old_session_end_closed_the_store(tmp_path):
    from run_agent import AIAgent

    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db")
    engine.on_session_start("old", hermes_home=str(tmp_path))
    engine.store_capsule("old-topic", "alphaonly archive")
    engine.on_session_end("old", [{"role": "user", "content": "finished"}])

    agent = object.__new__(AIAgent)
    agent.context_compressor = engine
    agent.session_id = "new"
    agent._session_db = None
    agent.reset_session_state()

    assert engine.session_id == "new"
    assert engine.scope_id == "new"
    engine.store_capsule("new-topic", "betaonly archive")
    assert engine.search("betaonly")[0]["topic_id"] == "new-topic"
    assert engine.search("alphaonly") == []


def test_search_scans_the_complete_archive_not_only_the_newest_rows(tmp_path):
    store = TieredContextStore(tmp_path / "context.db")
    with sqlite3.connect(store.path) as db:
        db.executemany(
            """
            INSERT INTO capsules (
                session_id, topic_id, title, summary, tier, importance,
                unresolved, pinned, access_count, created_at, updated_at,
                last_access_at, source_tokens, source_message_ids, metadata
            ) VALUES (?, ?, ?, ?, 'L3', 0.5, 0, 0, 0, ?, ?, ?, 0, '[]', '{}')
            """,
            (
                (
                    "session-1",
                    f"topic-{index}",
                    f"Topic {index}",
                    (
                        "oldest durable archive marker"
                        if index == 0
                        else f"ordinary archived summary {index}"
                    ),
                    float(index),
                    float(index),
                    float(index),
                )
                for index in range(5_001)
            ),
        )

    hits = store.search(
        "oldest durable archive marker",
        session_id="session-1",
        limit=1,
    )

    assert hits[0]["topic_id"] == "topic-0"


def test_independent_topic_summaries_do_not_inherit_delegate_state(tmp_path, monkeypatch):
    instances = []

    class FakeCompressor:
        def __init__(self, **_kwargs):
            self.previous = ""
            instances.append(self)

        def _generate_summary(self, messages, **_kwargs):
            source = str(messages[0]["content"])
            result = f"{self.previous}{source}"
            self.previous = result
            return result

        def prune_tool_results_only(self, messages, current_tokens=None):
            return messages, 0

    monkeypatch.setattr("agent.context_compressor.ContextCompressor", FakeCompressor)
    engine = TieredPipelineEngine(storage_path=tmp_path / "context.db", protect_last_n=2)
    engine.on_session_start("session-1", hermes_home=str(tmp_path))
    engine.update_model("test", context_length=100_000)

    for old_topic in ("FIRST PRIVATE TOPIC", "SECOND INDEPENDENT TOPIC"):
        engine.compress(
            [
                {"role": "system", "content": "system"},
                {"role": "user", "content": old_topic},
                {"role": "assistant", "content": "old result"},
                {"role": "user", "content": "NEW TASK: current"},
                {"role": "assistant", "content": "current result"},
            ],
            current_tokens=50_000,
        )

    second = engine.search("SECOND INDEPENDENT", limit=1)[0]["summary"]
    assert "SECOND INDEPENDENT TOPIC" in second
    assert "FIRST PRIVATE TOPIC" not in second
    assert len(instances) == 2


def test_proactive_prune_reclaims_old_large_tool_results_before_full_compaction(tmp_path):
    engine = TieredPipelineEngine(
        storage_path=tmp_path / "context.db",
        protect_last_n=2,
        proactive_prune_tokens=25_000,
        proactive_prune_min_result_chars=1000,
        proactive_prune_min_reclaim_tokens=1,
    )
    engine.update_model("test", context_length=200_000)
    huge = "large tool payload " * 1000
    messages = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "first operation"},
        {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": huge},
        {"role": "assistant", "content": "first result"},
        {"role": "user", "content": "second operation"},
        {"role": "assistant", "tool_calls": [{"id": "b", "function": {"name": "read", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "b", "content": huge},
        {"role": "user", "content": "current task"},
        {"role": "assistant", "content": "working"},
    ]

    unchanged, count = engine.prune_tool_results_only(messages, current_tokens=24_999)
    assert unchanged is messages
    assert count == 0

    pruned, count = engine.prune_tool_results_only(messages, current_tokens=25_000)
    assert count >= 1
    assert len(str(pruned[7]["content"])) < len(huge)
    assert pruned[-2:] == messages[-2:]
