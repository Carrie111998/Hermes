from datetime import datetime, timezone

from agent.account_usage import AccountUsageSnapshot, AccountUsageWindow
from run_agent import AIAgent


class _ImmediateThread:
    def __init__(self, *, target, name, daemon):
        self.target = target
        self.name = name
        self.daemon = daemon

    def start(self):
        self.target()


def _bare_commandcode_agent():
    agent = object.__new__(AIAgent)
    agent.provider = "commandcode"
    agent.base_url = "https://api.commandcode.ai/provider/v1"
    agent.api_key = "api-key-is-not-used-for-billing"
    agent.notice_callback = None
    agent.notice_clear_callback = None
    agent._credits_notices_enabled = lambda: True
    return agent


def test_commandcode_notice_refresh_is_backgrounded_and_ttl_cached(monkeypatch):
    agent = _bare_commandcode_agent()
    shown = []
    cleared = []
    agent.notice_callback = shown.append
    agent.notice_clear_callback = cleared.append
    snapshot = AccountUsageSnapshot(
        provider="commandcode",
        source="test",
        fetched_at=datetime.now(timezone.utc),
        windows=(AccountUsageWindow(label="5-hour limit", used_percent=91),),
    )

    monkeypatch.setenv("COMMANDCODE_SESSION_COOKIE", "session-token")
    monkeypatch.setattr("threading.Thread", _ImmediateThread)
    monkeypatch.setattr("agent.account_usage.fetch_account_usage", lambda *args, **kwargs: snapshot)

    assert agent._refresh_account_usage_notices() is True
    assert [notice.key for notice in shown] == [
        "account_usage.commandcode.5-hour-limit"
    ]
    assert cleared == []
    assert agent._refresh_account_usage_notices() is False
    assert len(shown) == 1


def test_commandcode_notice_refresh_requires_cookie_and_consumer(monkeypatch):
    agent = _bare_commandcode_agent()
    monkeypatch.delenv("COMMANDCODE_SESSION_COOKIE", raising=False)
    assert agent._refresh_account_usage_notices() is False

    monkeypatch.setenv("COMMANDCODE_SESSION_COOKIE", "session-token")
    assert agent._refresh_account_usage_notices() is False


def test_commandcode_cold_start_routes_to_account_usage_refresh():
    from agent.credits_tracker import seed_credits_at_session_start

    class FakeAgent:
        provider = "commandcode-anthropic"

        def __init__(self):
            self.calls = 0

        def _refresh_account_usage_notices(self):
            self.calls += 1
            return True

    agent = FakeAgent()
    assert seed_credits_at_session_start(agent) is True
    assert agent.calls == 1
