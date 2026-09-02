"""Gateway /model --clear drops the session override so yaml/CLI win (#99403)."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource


class _FakeAsyncStore:
    def __init__(self):
        self.overrides = {}
        self.session_id = "sess-1"
        # Satisfy GatewayRunner.async_session_store's cache check
        # (rebuilds when facade._store is not session_store).
        self._store = self

    async def set_model_override(self, session_key, override):
        if override is None:
            self.overrides.pop(session_key, None)
        else:
            self.overrides[session_key] = override

    def get_model_override(self, session_key):
        return self.overrides.get(session_key)

    async def get_or_create_session(self, _source):
        return SimpleNamespace(session_id=self.session_id, was_auto_reset=False)


class _FakeSessionDB:
    def __init__(self):
        self.model = "locked-model"
        self.model_config = {"model": "locked-model", "provider": "locked-provider"}

    async def update_session_model(self, _session_id, model, provider=None):
        self.model = model
        if model:
            self.model_config["model"] = model
        if provider:
            self.model_config["provider"] = provider

    async def patch_session_model_config(self, _session_id, patch):
        for key, value in patch.items():
            if value is None:
                self.model_config.pop(key, None)
            else:
                self.model_config[key] = value

    async def get_session(self, _session_id):
        return {"model": self.model, "model_config": dict(self.model_config)}


def _make_event(text="/model --clear"):
    return MessageEvent(
        text=text,
        message_type=MessageType.TEXT,
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="u1",
        ),
    )


def _make_runner(store, db=None):
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._voice_mode = {}
    runner._running_agents = {}
    runner._session_model_overrides = {}
    runner.session_store = store
    runner._async_session_store = store
    runner._session_db = db
    runner._pending_model_notes = {}
    return runner


@pytest.mark.asyncio
async def test_model_clear_pops_override_and_hydrate_falls_back_to_config(
    tmp_path, monkeypatch
):
    import gateway.run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: yaml-default\n  provider: yaml-provider\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})

    store = _FakeAsyncStore()
    db = _FakeSessionDB()
    runner = _make_runner(store, db)
    event = _make_event("/model locked-model")
    session_key = runner._session_key_for_source(event.source)

    runner._session_model_overrides[session_key] = {
        "model": "locked-model",
        "provider": "locked-provider",
        "base_url": "https://locked.example/v1",
    }
    await store.set_model_override(
        session_key,
        {
            "model": "locked-model",
            "provider": "locked-provider",
            "base_url": "https://locked.example/v1",
        },
    )

    result = await runner._handle_model_command(_make_event("/model --clear"))

    assert "Cleared this session's model override" in result
    assert "yaml-default" in result
    assert session_key not in runner._session_model_overrides
    assert store.get_model_override(session_key) is None
    assert db.model == ""
    assert "model" not in db.model_config
    assert "provider" not in db.model_config

    runner._rehydrate_session_model_override(session_key)
    assert session_key not in runner._session_model_overrides

    with patch(
        "gateway.run._resolve_runtime_agent_kwargs",
        return_value={"provider": "yaml-provider", "api_key": "x"},
    ):
        model, _runtime = runner._resolve_session_agent_runtime(
            session_key=session_key,
            user_config={"model": {"default": "yaml-default"}},
        )
    assert model == "yaml-default"


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", ["--clear", "default", "-"])
async def test_model_clear_aliases(tmp_path, monkeypatch, raw):
    import gateway.run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: yaml-default\n  provider: yaml-provider\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})

    store = _FakeAsyncStore()
    runner = _make_runner(store)
    event = _make_event(f"/model {raw}")
    session_key = runner._session_key_for_source(event.source)
    runner._session_model_overrides[session_key] = {
        "model": "locked-model",
        "provider": "locked-provider",
    }
    await store.set_model_override(
        session_key, {"model": "locked-model", "provider": "locked-provider"}
    )

    result = await runner._handle_model_command(event)

    assert "Cleared this session's model override" in result
    assert session_key not in runner._session_model_overrides
    assert store.get_model_override(session_key) is None


@pytest.mark.asyncio
async def test_model_help_mentions_clear(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text(
        "model:\n  default: yaml-default\n  provider: yaml-provider\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(gateway_run, "_hermes_home", hermes_home)
    monkeypatch.setattr("agent.models_dev.fetch_models_dev", lambda: {})
    monkeypatch.setattr(
        "hermes_cli.model_switch.list_authenticated_providers",
        lambda **_kwargs: [],
    )

    runner = _make_runner(_FakeAsyncStore())
    runner._adapter_for_source = lambda _source: None
    result = await runner._handle_model_command(_make_event("/model"))

    assert "/model --clear" in result


@pytest.mark.asyncio
async def test_model_clear_with_global_is_rejected():
    result = await _make_runner(_FakeAsyncStore())._handle_model_command(
        _make_event("/model --clear --global")
    )
    assert result.startswith("❌")
    assert "--global" in result
