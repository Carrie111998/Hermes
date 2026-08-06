from hermes_cli.plugins import PluginManager


def test_hook_envelope_includes_current_ui_scope(monkeypatch):
    from agent import runtime_cwd
    from gateway import session_context

    values = {
        "HERMES_UI_SESSION_ID": "ui-7",
        "HERMES_SESSION_PROFILE": "work",
    }
    monkeypatch.setattr(session_context, "get_session_env", lambda name, default="": values.get(name, default))
    monkeypatch.setattr(runtime_cwd, "get_session_cwd", lambda: "/workspace")

    manager = PluginManager()
    seen = []
    manager._hooks["post_tool_call"] = [lambda **kwargs: seen.append(kwargs)]
    manager.invoke_hook("post_tool_call", session_id="durable-1", tool_name="todo")

    assert seen[0]["ui_session_id"] == "ui-7"
    assert seen[0]["session_profile"] == "work"
    assert seen[0]["session_cwd"] == "/workspace"


def test_explicit_hook_scope_is_not_overwritten(monkeypatch):
    from agent import runtime_cwd
    from gateway import session_context

    monkeypatch.setattr(session_context, "get_session_env", lambda name, default="": "ambient")
    monkeypatch.setattr(runtime_cwd, "get_session_cwd", lambda: "/ambient")

    manager = PluginManager()
    seen = []
    manager._hooks["post_tool_call"] = [lambda **kwargs: seen.append(kwargs)]
    manager.invoke_hook(
        "post_tool_call",
        session_id="durable-1",
        ui_session_id="ui-explicit",
        session_profile="profile-explicit",
        session_cwd="/explicit",
    )

    assert seen[0]["ui_session_id"] == "ui-explicit"
    assert seen[0]["session_profile"] == "profile-explicit"
    assert seen[0]["session_cwd"] == "/explicit"


def test_post_tool_call_producer_forwards_turn_id(monkeypatch):
    from hermes_cli import lifecycle
    import model_tools

    seen = []
    monkeypatch.setattr(lifecycle, "has_hook", lambda hook: hook == "post_tool_call")
    monkeypatch.setattr(lifecycle, "invoke_hook", lambda hook, **kwargs: seen.append((hook, kwargs)))

    model_tools._emit_post_tool_call_hook(
        function_name="todo",
        function_args={"todos": []},
        result={"todos": []},
        session_id="durable-1",
        turn_id="turn-7",
        status="ok",
    )

    assert seen[0][0] == "post_tool_call"
    assert seen[0][1]["turn_id"] == "turn-7"