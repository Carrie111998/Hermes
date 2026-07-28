"""Production-path integration for invisible context-segment rollover."""

import json

from gateway.config import ContextRolloverPolicy, GatewayConfig, Platform
from gateway.run import _usable_input_budget
from gateway.session import SessionSource, SessionStore
from tools.session_search_tool import session_search


def test_measured_budget_rolls_linked_child_with_checkpoint_and_exact_recall(
    tmp_path, monkeypatch
):
    import hermes_state

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        hermes_state,
        "DEFAULT_DB_PATH",
        tmp_path / "state.db",
    )
    store = SessionStore(
        sessions_dir=tmp_path / "sessions",
        config=GatewayConfig(
            context_rollover=ContextRolloverPolicy(
                enabled=True,
                threshold_ratio=0.70,
                notify=False,
            )
        ),
    )
    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="chat-1",
        chat_type="dm",
        user_id="joel",
    )
    previous = store.get_or_create_session(source)
    store.append_to_transcript(
        previous.session_id,
        {
            "role": "user",
            "content": "The cobalt continuity decision must remain exact.",
        },
    )
    store.append_to_transcript(
        previous.session_id,
        {
            "role": "assistant",
            "content": "Confirmed. The deterministic checkpoint carries it.",
        },
    )
    store.append_to_transcript(
        previous.session_id,
        {
            "role": "user",
            "content": (
                "[CONTEXT SUMMARY]: stale generated claim "
                "integration-exclusion-marker"
            ),
        },
    )
    store.append_to_transcript(
        previous.session_id,
        {
            "role": "tool",
            "content": "integration-tool-exclusion-marker",
            "tool_name": "exec",
        },
    )

    usable_budget = _usable_input_budget(500_000, 32_000)
    store.update_session(
        previous.session_key,
        last_prompt_tokens=int(usable_budget * 0.70),
        last_input_budget_tokens=usable_budget,
    )
    current = store.get_or_create_session(source)

    assert current.session_id != previous.session_id
    assert store._db.get_session(previous.session_id)["end_reason"] == (
        "context_rollover"
    )
    assert store._db.get_session(current.session_id)["parent_session_id"] == (
        previous.session_id
    )
    assert store._db.get_conversation_root(current.session_id) == (
        previous.session_id
    )

    checkpoint = store.build_continuity_checkpoint(current)
    assert "cobalt continuity decision" in checkpoint
    assert "integration-exclusion-marker" not in checkpoint
    assert "integration-tool-exclusion-marker" not in checkpoint

    result = json.loads(
        session_search(
            query="cobalt continuity",
            current_session_id=current.session_id,
            db=store._db,
        )
    )
    assert result["count"] == 1
    assert result["results"][0]["session_id"] == previous.session_id
