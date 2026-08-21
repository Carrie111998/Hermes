import contextlib
import types

import pytest

from gateway.slash_commands import GatewaySlashCommandsMixin


class Entry:
    def __init__(self, credential_id, label, priority=0, status=None):
        self.id = credential_id
        self.label = label
        self.priority = priority
        self.last_status = status


class Pool:
    def __init__(self, entries):
        self._entries = entries
        self.selected = None

    def entries(self):
        return list(self._entries)

    def prioritize(self, credential_id):
        if not any(entry.id == credential_id for entry in self._entries):
            return False
        self.selected = credential_id
        selected = next(entry for entry in self._entries if entry.id == credential_id)
        self._entries.remove(selected)
        self._entries.insert(0, selected)
        for priority, entry in enumerate(self._entries):
            entry.priority = priority
        return True


def runner(multiplex=False):
    return types.SimpleNamespace(config=types.SimpleNamespace(multiplex_profiles=multiplex))


def event(text, *, chat_type="dm", profile="default", user_id="admin"):
    return types.SimpleNamespace(
        text=text,
        source=types.SimpleNamespace(
            chat_type=chat_type,
            platform=types.SimpleNamespace(value="telegram"),
            profile=profile,
            user_id=user_id,
        ),
    )


@pytest.fixture(autouse=True)
def admin_policy(monkeypatch):
    policy = types.SimpleNamespace(enabled=True, is_admin=lambda user_id: user_id == "admin")
    monkeypatch.setattr("gateway.slash_access.policy_for_source", lambda config, source: policy)


@pytest.mark.asyncio
async def test_account_command_lists_redacted_provider_accounts(monkeypatch):
    pools = {
        "openai-codex": Pool([Entry("oa-1", "person@example.com")]),
        "anthropic": Pool([Entry("cl-1", "work@example.com", status="exhausted")]),
    }
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: pools[provider])

    result = await GatewaySlashCommandsMixin._handle_account_command(runner(), event("/account@hermes_bot"))

    assert "OpenAI" in result
    assert "Account 1" in result
    assert "oa-1" in result
    assert "Claude" in result
    assert "exhausted" in result
    assert "person@example.com" not in result
    assert "work@example.com" not in result
    assert "access_token" not in result
    assert "refresh_token" not in result


@pytest.mark.asyncio
async def test_account_command_switches_and_persists_preference(monkeypatch):
    pool = Pool([Entry("first", "First"), Entry("second", "Second", priority=1)])
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda provider: pool if provider == "openai-codex" else Pool([]),
    )
    monkeypatch.setattr(
        "hermes_cli.auth.prefer_eligible_credential",
        lambda provider, credential_id: setattr(pool, "selected", credential_id) or "saved",
    )

    result = await GatewaySlashCommandsMixin._handle_account_command(
        runner(), event("/account use openai-codex second")
    )

    assert pool.selected == "second"
    assert "preference saved" in result.lower()
    assert "active turn keeps its current credential" in result.lower()


@pytest.mark.asyncio
async def test_account_command_rejects_unavailable_or_malformed_selection(monkeypatch):
    exhausted = Pool([Entry("dead-one", "Dead", status="exhausted")])
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda provider: exhausted if provider == "anthropic" else Pool([]),
    )
    monkeypatch.setattr(
        "hermes_cli.auth.prefer_eligible_credential",
        lambda provider, credential_id: "unavailable" if credential_id == "dead-one" else "missing",
    )

    bad_provider = await GatewaySlashCommandsMixin._handle_account_command(
        runner(), event("/account use evil id")
    )
    malformed = await GatewaySlashCommandsMixin._handle_account_command(
        runner(), event("/account use anthropic bad:id trailing")
    )
    unavailable = await GatewaySlashCommandsMixin._handle_account_command(
        runner(), event("/account use anthropic dead-one")
    )

    assert "Unknown provider" in bad_provider
    assert "Usage:" in malformed
    assert "unavailable" in unavailable
    assert exhausted.selected is None


@pytest.mark.asyncio
async def test_account_command_requires_admin_and_direct_message(monkeypatch):
    monkeypatch.setattr("agent.credential_pool.load_pool", lambda provider: Pool([]))

    not_admin = await GatewaySlashCommandsMixin._handle_account_command(
        runner(), event("/account", user_id="user")
    )
    group = await GatewaySlashCommandsMixin._handle_account_command(
        runner(), event("/account", chat_type="group")
    )

    assert "Admin authorization" in not_admin
    assert "direct message" in group

    missing_type = await GatewaySlashCommandsMixin._handle_account_command(
        runner(), event("/account", chat_type="")
    )
    assert "direct message" in missing_type


@pytest.mark.asyncio
async def test_account_command_rejects_list_arguments_and_omits_malformed_stored_ids(monkeypatch):
    pool = Pool([
        Entry("safe-id", "Safe"),
        Entry("status-id", "Status", priority=1, status="secret\nleak"),
        Entry("bad\nformat", "Bad", priority=2),
    ])
    monkeypatch.setattr(
        "agent.credential_pool.load_pool",
        lambda provider: pool if provider == "openai-codex" else Pool([]),
    )

    malformed = await GatewaySlashCommandsMixin._handle_account_command(
        runner(), event("/account list unexpected")
    )
    listed = await GatewaySlashCommandsMixin._handle_account_command(
        runner(), event("/account list")
    )

    assert "Usage:" in malformed
    assert "safe-id" in listed
    assert "status-id" in listed
    assert "unknown" in listed
    assert "secret\nleak" not in listed
    assert "bad\nformat" not in listed


@pytest.mark.asyncio
async def test_account_command_scopes_pool_access_to_multiplex_profile(monkeypatch, tmp_path):
    active_home = []
    profile_home = tmp_path / "station"
    profile_home.mkdir()
    monkeypatch.setattr(
        "gateway.run._multiplex_profile_homes",
        lambda config: [("station", profile_home)],
    )

    @contextlib.contextmanager
    def profile_scope(home):
        active_home.append(home)
        try:
            yield
        finally:
            active_home.pop()

    seen = []

    def load_pool(provider):
        seen.append((provider, list(active_home)))
        return Pool([])

    monkeypatch.setattr("gateway.run._profile_runtime_scope", profile_scope)
    monkeypatch.setattr("agent.credential_pool.load_pool", load_pool)

    await GatewaySlashCommandsMixin._handle_account_command(
        runner(multiplex=True), event("/account", profile="station")
    )

    assert seen == [
        ("openai-codex", [profile_home]),
        ("anthropic", [profile_home]),
    ]


@pytest.mark.asyncio
async def test_account_command_rejects_unserved_multiplex_profile(monkeypatch):
    monkeypatch.setattr("gateway.run._multiplex_profile_homes", lambda config: [])
    load_pool = types.SimpleNamespace(called=False)

    def unexpected_load(_provider):
        load_pool.called = True
        return Pool([])

    monkeypatch.setattr("agent.credential_pool.load_pool", unexpected_load)

    result = await GatewaySlashCommandsMixin._handle_account_command(
        runner(multiplex=True), event("/account", profile="stale")
    )

    assert "Unable to resolve" in result
    assert load_pool.called is False
