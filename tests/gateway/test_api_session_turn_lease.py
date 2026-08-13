"""Regression tests for serialising API turns on one persisted session."""

import asyncio

import pytest

from gateway.config import PlatformConfig
from gateway.platforms.api_server import APIServerAdapter
from hermes_state import SessionDB


def test_api_turns_for_one_session_are_serialised(session_db, monkeypatch):
    async def scenario():
        adapter = APIServerAdapter(PlatformConfig(enabled=True))
        adapter._session_db = session_db
        session_id = session_db.create_session("api-lease-session", "api_server")
        events = []

        class FakeAgent:
            session_prompt_tokens = 0
            session_completion_tokens = 0
            session_total_tokens = 0

            def __init__(self, **kwargs):
                self.session_id = kwargs["session_id"]

            def run_conversation(self, user_message, conversation_history, task_id):
                events.append(f"start:{user_message}")
                if user_message == "first":
                    # Keep the first transcript turn in flight while the second
                    # request tries to enter the same persisted session.
                    import time

                    time.sleep(0.05)
                events.append(f"finish:{user_message}")
                return {"final_response": user_message}

        monkeypatch.setattr(adapter, "_create_agent", FakeAgent)

        first = asyncio.create_task(
            adapter._run_agent(
                user_message="first",
                conversation_history=[],
                session_id=session_id,
            )
        )
        await asyncio.sleep(0.01)
        second = asyncio.create_task(
            adapter._run_agent(
                user_message="second",
                conversation_history=[],
                session_id=session_id,
            )
        )
        await asyncio.gather(first, second)

        assert events == ["start:first", "finish:first", "start:second", "finish:second"]
        assert not adapter._session_turn_leases._leases[session_id].lock.locked()

    asyncio.run(scenario())


@pytest.fixture
def session_db(tmp_path):
    db = SessionDB(tmp_path / "state.db")
    try:
        yield db
    finally:
        db.close()
