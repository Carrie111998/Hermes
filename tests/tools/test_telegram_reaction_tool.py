"""Contract tests for telegram_react (emoji-only, current-turn, eager)."""
import asyncio, json, queue, subprocess, sys, threading
from gateway.config import Platform
from gateway.session import SessionSource

def _call_on_gateway(monkeypatch, handler, emoji="❤️"):
    from tools import telegram_reaction_tool as module
    values = {
        "HERMES_SESSION_PLATFORM": "telegram", "HERMES_SESSION_KEY": "secondary-session",
    }
    monkeypatch.setattr(module, "get_session_env", lambda n, d="": values.get(n, d))
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="-100", chat_type="group",
        user_id="42", profile="secondary",
    )
    loop, ready = asyncio.new_event_loop(), threading.Event()

    def run_loop():
        asyncio.set_event_loop(loop); ready.set(); loop.run_forever()

    thread = threading.Thread(target=run_loop); thread.start(); ready.wait(timeout=2)
    adapter = type("A", (), {"add_current_reaction": handler})()
    runner = type("R", (), {
        "_gateway_loop": loop,
        "_get_cached_session_source": lambda self, key: source,
        "_adapter_for_source": lambda self, current: adapter,
    })()
    import gateway.run
    monkeypatch.setattr(gateway.run, "_gateway_runner_ref", lambda: runner)
    try:
        return json.loads(module.telegram_reaction_tool(emoji)), loop, thread, source
    finally:
        loop.call_soon_threadsafe(loop.stop); thread.join(timeout=2); loop.close()

def test_tool_target_canonical_fail_closed_scope_and_progress(monkeypatch):
    observed = {}

    async def react(_a, session_key, emoji):
        observed.update(session_key=session_key, emoji=emoji,
                        loop=asyncio.get_running_loop(), thread=threading.get_ident())
        return True

    result, loop, thread, source = _call_on_gateway(monkeypatch, react)
    assert result == {"success": True}
    assert observed == {"session_key": "secondary-session", "emoji": "❤",
                        "loop": loop, "thread": thread.ident}
    assert source.profile == "secondary"

    async def fail(*_a):
        return False

    # Reaction turns / missing ordinary inbound → no outgoing reaction target.
    assert _call_on_gateway(monkeypatch, fail, "👍")[0] == {
        "error": "The current Telegram message context is unavailable."}

    from tools import telegram_reaction_tool as module
    monkeypatch.setattr(module, "get_session_env",
                        lambda n, d="": "discord" if n == "HERMES_SESSION_PLATFORM" else d)
    assert "Telegram session" in json.loads(module.telegram_reaction_tool("👍"))["error"]
    monkeypatch.setattr(module, "get_session_env",
                        lambda n, d="": "telegram" if n == "HERMES_SESSION_PLATFORM" else d)
    monkeypatch.setattr(module, "canonical_standard_emoji", lambda e: None)
    assert json.loads(module.telegram_reaction_tool("🧠")) == {
        "error": "Telegram does not support that standard reaction emoji."}

    from gateway.run import TurnRunner
    from gateway.turn_context import TurnContext
    from hermes_cli.tools_config import _get_platform_tools
    from tools import telegram_reaction_tool
    from tools.registry import registry
    from tools.tool_search import is_deferrable_tool_name
    from toolsets import resolve_toolset

    entry = registry.get_entry("telegram_react")
    assert entry is not None and entry.toolset == "telegram_reactions"
    assert set(telegram_reaction_tool.SCHEMA["parameters"]["properties"]) == {"emoji"}
    emoji_help = telegram_reaction_tool.SCHEMA["parameters"]["properties"]["emoji"]["description"]
    assert "🤣" in emoji_help and "😂" not in emoji_help
    assert "telegram_react" in resolve_toolset("hermes-telegram")
    assert "telegram_react" in resolve_toolset("telegram_reactions")
    assert "telegram_react" not in resolve_toolset("hermes-cli")
    assert "telegram_react" not in resolve_toolset("hermes-discord")
    assert "telegram_reactions" in _get_platform_tools({}, "telegram")
    assert "telegram_reactions" not in _get_platform_tools({}, "discord")
    assert is_deferrable_tool_name("telegram_react") is False
    progress = queue.Queue()
    TurnRunner(  # type: ignore[arg-type]
        type("G", (), {"_adapter_for_source": lambda self, s: None})(),
        TurnContext(progress_queue=progress, progress_mode="all",
                    tool_progress_enabled=True, _run_still_current=lambda: True),
    ).progress_callback("tool.started", "telegram_react", preview="t", args={"emoji": "👍"})
    assert progress.empty()

def test_standard_reaction_canonicalization_and_ptb_fallback(monkeypatch):
    script = r"""
from telegram.constants import ReactionEmoji
from plugins.platforms.telegram.reactions import canonical_standard_emoji
for emoji in [str(getattr(i, "value", i)) for i in ReactionEmoji]:
    assert canonical_standard_emoji(emoji) == emoji
    if "\u200d" not in emoji and not emoji.endswith(("\ufe0e", "\ufe0f")):
        assert canonical_standard_emoji(f"{emoji}\ufe0f") == emoji
    if "\u200d" in emoji and "\ufe0f" in emoji:
        assert canonical_standard_emoji(emoji.replace("\ufe0f", "")) == emoji
assert canonical_standard_emoji("🧠") is None
"""
    subprocess.run([sys.executable, "-c", script], check=True)
    import telegram.constants as tc
    from plugins.platforms.telegram import reactions
    monkeypatch.setattr(tc, "ReactionEmoji", ())
    assert reactions.canonical_standard_emoji("❤️") == "❤"
    assert reactions.canonical_standard_emoji("👨‍💻") == "👨‍💻"
