from types import SimpleNamespace

from tui_gateway import server


def test_agent_terminal_owner_survives_session_key_rotation(monkeypatch):
    monkeypatch.setattr(
        server,
        "_sessions",
        {
            "runtime-a": {"session_key": "rotated-key"},
            "runtime-b": {"session_key": "other-key"},
        },
    )
    process = SimpleNamespace(
        origin_ui_session_id="runtime-a",
        session_key="pre-compression-key",
    )

    assert server._agent_terminal_owner_sid(process) == "runtime-a"


def test_agent_terminal_owner_falls_back_to_session_key(monkeypatch):
    monkeypatch.setattr(
        server,
        "_sessions",
        {"runtime-a": {"session_key": "durable-key"}},
    )
    process = SimpleNamespace(
        origin_ui_session_id="stale-runtime",
        session_key="durable-key",
    )

    assert server._agent_terminal_owner_sid(process) == "runtime-a"
