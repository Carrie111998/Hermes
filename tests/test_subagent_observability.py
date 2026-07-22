from tui_gateway import server


def test_subagent_events_remain_visible_when_tool_progress_is_off(monkeypatch):
    events = []

    monkeypatch.setattr(server, "_tool_progress_enabled", lambda _sid: False)
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload=None: events.append((event, sid, payload)),
    )
    monkeypatch.setattr(server, "_mirror_subagent_to_child", lambda *_args: None)

    server._on_tool_progress(
        "desktop-session",
        "subagent.start",
        preview="Inspect the repository",
        subagent_id="subagent-1",
    )
    server._on_tool_progress(
        "desktop-session",
        "tool.started",
        name="read_file",
        preview="internal detail",
    )

    assert [event[0] for event in events] == ["subagent.start"]
    assert events[0][1] == "desktop-session"
    assert events[0][2]["subagent_id"] == "subagent-1"