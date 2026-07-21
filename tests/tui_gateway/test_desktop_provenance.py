from __future__ import annotations

import threading

from tui_gateway import server


def _context(text: str, event_id: str) -> dict:
    return {
        "surface": "desktop",
        "platform_account": "installation-1",
        "sender_id": "darwin:501",
        "chat_id": "desktop:installation-1",
        "chat_type": "private",
        "thread_id": "",
        "profile": "default",
        "app_identity": "TEAM:io.hermes.desktop@0.17.0",
        "app_instance_id": "instance-1",
        "window_id": "7",
        "gateway_session_id": "runtime-1",
        "accepted_text": text,
        "source_messages": [{"raw_event_id": event_id}],
    }


def test_prompt_hook_accepts_one_exact_plugin_verified_context(monkeypatch):
    text = "log procedure"
    context = _context(text, "event-1")

    def invoke(name, **kwargs):
        assert name == "pre_prompt_submit"
        assert kwargs["session_id"] == "runtime-1"
        assert kwargs["task_id"] == "stored-1"
        assert kwargs["user_message"] == text
        return [{"surface_context": context}]

    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", invoke)
    assert server._prompt_surface_context(
        {"desktop_provenance": {"signed": True}},
        sid="runtime-1",
        session={"session_key": "stored-1"},
        text=text,
    ) == context


def test_prompt_hook_fails_closed_for_mismatch_or_multiple_authorities(monkeypatch):
    text = "log procedure"
    context = _context(text, "event-1")
    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: [
            {"surface_context": context},
            {"surface_context": context},
        ],
    )
    assert server._prompt_surface_context(
        {"desktop_provenance": {}}, sid="r", session={"session_key": "s"}, text=text
    ) is None

    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: [
            {"surface_context": {**context, "source_messages": []}}
        ],
    )
    assert server._prompt_surface_context(
        {"desktop_provenance": {}}, sid="r", session={"session_key": "s"}, text=text
    ) is None

    monkeypatch.setattr(
        "hermes_cli.lifecycle.invoke_hook",
        lambda *_args, **_kwargs: [
            {"surface_context": {**context, "accepted_text": "changed"}}
        ],
    )
    assert server._prompt_surface_context(
        {"desktop_provenance": {}}, sid="r", session={"session_key": "s"}, text=text
    ) is None


def test_queued_prompts_preserve_ordered_sources_only_for_same_principal():
    session = {"queued_prompt": None}
    first = _context("first", "event-1")
    second = _context("second", "event-2")
    server._enqueue_prompt(session, "first", object(), first)
    server._enqueue_prompt(session, "second", object(), second)
    queued = session["queued_prompt"]
    assert queued["text"] == "first\n\nsecond"
    assert queued["surface_context"]["accepted_text"] == "first\n\nsecond"
    assert [row["raw_event_id"] for row in queued["surface_context"]["source_messages"]] == [
        "event-1",
        "event-2",
    ]

    foreign = _context("third", "event-3")
    foreign["profile"] = "other"
    server._enqueue_prompt(session, "third", object(), foreign)
    assert session["queued_prompt"]["surface_context"] is None


def test_verified_busy_prompt_queues_instead_of_losing_context_to_steer(
    monkeypatch,
):
    class Agent:
        def __init__(self):
            self.steer_called = False

        def steer(self, _text):
            self.steer_called = True
            return True

    agent = Agent()
    context = _context("next", "event-1")
    session = {
        "agent": agent,
        "history_lock": threading.Lock(),
        "running": True,
    }
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "steer")

    result = server._handle_busy_submit(
        "rid",
        "sid",
        session,
        "next",
        object(),
        surface_context=context,
    )

    assert result["result"]["status"] == "queued"
    assert agent.steer_called is False
    assert session["queued_prompt"]["surface_context"] == context


def test_compute_host_frame_carries_only_in_memory_surface_context():
    context = _context("hello", "event-1")
    session = {
        "history": [],
        "history_version": 0,
        "attached_images": [],
        "history_lock": threading.Lock(),
        "session_key": "stored-1",
        "cols": 80,
        "cwd": "/tmp",
        "source": "desktop",
    }
    frame = server._compute_host_turn_frame("rid", "sid", session, "hello", context)
    assert frame["surface_context"] == context
    assert frame["text"] == "hello"
