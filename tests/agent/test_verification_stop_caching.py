"""Verification-loop synthetic scaffolding must never reach durable session state.

verify_on_stop / pre_verify inject a synthetic user nudge to keep the agent
going one more turn before it can claim completion. The assistant response is
real content that persists and is emitted to the UI as an interim message.
Only the nudge (the synthetic user message) is flagged, so only the nudge
gets stripped from the durable transcript. This test file verifies:

  - The verification-loop flags remain registered in
    ``_EPHEMERAL_SCAFFOLDING_FLAGS`` (so nudges are stripped).
  - The DB flush drops only the nudge, keeping the assistant candidate.
  - The JSON log drops only the nudge, keeping the assistant candidate.
"""

import json
import sys
from unittest.mock import MagicMock

import pytest


@pytest.fixture(autouse=True)
def _restore_purged_modules():
    """Put ``sys.modules`` back the way ``_fresh_run_agent`` found it.

    ``_fresh_run_agent`` drops every ``run_agent`` / ``agent.*`` / ``tools.*``
    / ``hermes_*`` module and re-imports, which is the right way to pick up a
    changed ``HERMES_HOME``. What it never did is put them back, so every test
    collected after this file ran against freshly-imported duplicates while
    already-imported test modules still held references to the originals.

    Two live copies of a module means two distinct class objects with the same
    name, so ``except SomeError`` and ``isinstance`` stop matching across the
    boundary. That is why the damage landed almost entirely on error-path
    assertions: this file was responsible for all 7 failures in
    ``transports/test_codex_app_server_session.py`` (``*_failure_returns_error``,
    ``oauth_*_failure``, ``*_raises``), confirmed by running the two files
    together — 7 failed — and by the innocent-verdict control on
    ``test_vision_routing_31179.py``, which purges too but happened to hit
    nothing the victim depends on.

    Restoring ``sys.modules`` alone is NOT enough, and this is the part that is
    easy to get wrong: ``import a.b as x`` reads ``sys.modules``, while
    ``from a.b import y`` reads the attribute ``b`` on the parent package. Fix
    only the first and half the imports still resolve to the stale copy. The
    same two-step was needed in
    ``tests/agent/test_empty_tool_name_loop_dampening.py``.
    """
    snapshot = dict(sys.modules)
    yield
    purged = {
        name: module for name, module in snapshot.items()
        if sys.modules.get(name) is not module
    }
    for name, module in purged.items():
        sys.modules[name] = module
    for name, module in purged.items():
        parent_name, _, child = name.rpartition(".")
        if not parent_name:
            continue
        parent = sys.modules.get(parent_name)
        if parent is not None and getattr(parent, child, None) is not module:
            setattr(parent, child, module)


def _fresh_run_agent(hermes_home):
    for mod in list(sys.modules):
        if mod == "run_agent" or mod.startswith("agent.") or mod.startswith("tools.") or mod.startswith("hermes_"):
            del sys.modules[mod]
    import run_agent  # noqa: F401
    return sys.modules["run_agent"]


def test_verification_flags_registered_as_ephemeral(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ra = _fresh_run_agent(tmp_path)

    assert "_verification_stop_synthetic" in ra._EPHEMERAL_SCAFFOLDING_FLAGS
    assert "_pre_verify_synthetic" in ra._EPHEMERAL_SCAFFOLDING_FLAGS

    # The nudge messages ARE scaffolding (they carry the synthetic flag).
    assert ra._is_ephemeral_scaffolding(
        {"role": "user", "content": "[System: run tests]", "_pre_verify_synthetic": True}
    )
    assert ra._is_ephemeral_scaffolding(
        {"role": "user", "content": "[System: run tests]", "_verification_stop_synthetic": True}
    )
    # Real messages (including the assistant candidate) are not.
    assert not ra._is_ephemeral_scaffolding({"role": "user", "content": "hi"})
    assert not ra._is_ephemeral_scaffolding({"role": "assistant", "content": "premature done"})


def _make_agent(ra, session_id, tmp_path):
    agent = ra.AIAgent(
        session_id=session_id,
        api_key="test-key",
        base_url="http://127.0.0.1:8000/v1",
        provider="openai-compat",
        model="test-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
    )
    agent._session_db = MagicMock()
    agent._session_db_created = True
    agent._session_json_enabled = True
    agent.logs_dir = tmp_path / "logs"
    agent.logs_dir.mkdir(parents=True, exist_ok=True)
    return agent


def test_db_flush_drops_only_nudge_keeps_candidate(tmp_path, monkeypatch):
    """The assistant candidate is NOT flagged synthetic, so it persists.
    Only the nudge (flagged synthetic) is dropped from the DB flush."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ra = _fresh_run_agent(tmp_path)
    agent = _make_agent(ra, "sess_db", tmp_path)

    messages = [
        {"role": "user", "content": "hi"},
        # Assistant candidate — NOT flagged synthetic, persists.
        {"role": "assistant", "content": "premature done"},
        # Nudge — flagged synthetic, gets dropped.
        {"role": "user", "content": "[System: run tests]", "_verification_stop_synthetic": True},
        {"role": "assistant", "content": "verified and clean"},
    ]

    agent._flush_messages_to_session_db(messages, conversation_history=[])

    persisted = [
        kwargs.get("content")
        for _args, kwargs in agent._session_db.append_message.call_args_list
    ]
    assert "hi" in persisted
    assert "verified and clean" in persisted
    # The assistant candidate persists — it is real content.
    assert "premature done" in persisted
    # Only the nudge is dropped.
    assert "[System: run tests]" not in persisted


def test_json_log_drops_only_nudge_keeps_candidate(tmp_path, monkeypatch):
    """The assistant candidate is NOT flagged synthetic, so it persists in the
    JSON log. Only the nudge (flagged synthetic) is dropped."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    ra = _fresh_run_agent(tmp_path)
    agent = _make_agent(ra, "sess_json", tmp_path)

    messages = [
        {"role": "user", "content": "hi"},
        # Assistant candidate — NOT flagged synthetic, persists.
        {"role": "assistant", "content": "premature done"},
        # Nudge — flagged synthetic, gets dropped.
        {"role": "user", "content": "[System: run tests]", "_pre_verify_synthetic": True},
        {"role": "assistant", "content": "verified and clean"},
    ]

    agent._save_session_log(messages)

    log_file = agent.logs_dir / "session_sess_json.json"
    assert log_file.exists()
    data = json.loads(log_file.read_text(encoding="utf-8"))
    contents = [m.get("content") for m in data["messages"]]
    # The assistant candidate persists — it is real content.
    assert "premature done" in contents
    assert "verified and clean" in contents
    assert "hi" in contents
    # Only the nudge is dropped.
    assert "[System: run tests]" not in contents
    assert all(not m.get("_pre_verify_synthetic") for m in data["messages"])
