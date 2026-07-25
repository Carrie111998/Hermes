"""Skin tool emoji overrides, including empty clean-skin suppression."""

from agent.display import get_tool_emoji, _format_tool_emoji, get_cute_tool_message


class _FakeSkin:
    def __init__(self, tool_emojis=None, tool_prefix="|"):
        self.tool_emojis = tool_emojis or {}
        self.tool_prefix = tool_prefix


def test_empty_star_override_suppresses_emoji(monkeypatch):
    import agent.display as d

    monkeypatch.setattr(d, "_get_skin", lambda: _FakeSkin({"*": ""}))
    assert get_tool_emoji("web_search", default="🔍") == ""
    assert _format_tool_emoji("web_search", "🔍") == ""


def test_exact_override_beats_star(monkeypatch):
    import agent.display as d

    monkeypatch.setattr(d, "_get_skin", lambda: _FakeSkin({"*": "", "web_search": "🔎"}))
    assert get_tool_emoji("web_search", default="🔍") == "🔎"
    assert get_tool_emoji("terminal", default="💻") == ""


def test_cute_message_honors_clean_prefix_and_no_emoji(monkeypatch):
    import agent.display as d

    monkeypatch.setattr(d, "_get_skin", lambda: _FakeSkin({"*": ""}, tool_prefix="|"))
    msg = get_cute_tool_message("web_search", {"query": "hermes skins"}, 0.4)
    assert msg.startswith("| ")
    assert "🔍" not in msg
    assert "search" in msg
