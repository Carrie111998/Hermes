"""Gateway lazy imports stay within the active source tree."""

import sys
from pathlib import Path
from types import ModuleType

import pytest

from gateway import run as gateway_run


def test_runtime_agent_resolves_from_gateway_source_root():
    agent_class = gateway_run._load_runtime_ai_agent_class()
    runtime_root = Path(gateway_run.__file__).resolve().parent.parent

    assert agent_class.__name__ == "AIAgent"
    assert Path(sys.modules["run_agent"].__file__).resolve() == (
        runtime_root / "run_agent.py"
    )


def test_foreign_sys_path_entry_cannot_win(tmp_path, monkeypatch):
    (tmp_path / "run_agent.py").write_text(
        "class AIAgent: pass\n",
        encoding="utf-8",
    )
    monkeypatch.delitem(sys.modules, "run_agent", raising=False)
    monkeypatch.syspath_prepend(str(tmp_path))

    gateway_run._load_runtime_ai_agent_class()

    runtime_root = Path(gateway_run.__file__).resolve().parent.parent
    assert Path(sys.modules["run_agent"].__file__).resolve() == (
        runtime_root / "run_agent.py"
    )


def test_preloaded_foreign_run_agent_fails_closed(tmp_path, monkeypatch):
    foreign = ModuleType("run_agent")
    foreign.__file__ = str(tmp_path / "run_agent.py")
    foreign.AIAgent = type("AIAgent", (), {})
    monkeypatch.setitem(sys.modules, "run_agent", foreign)

    with pytest.raises(RuntimeError, match="already loaded"):
        gateway_run._load_runtime_ai_agent_class()


def test_preloaded_foreign_agent_init_fails_closed(tmp_path, monkeypatch):
    gateway_run._load_runtime_ai_agent_class()
    foreign = ModuleType("agent.agent_init")
    foreign.__file__ = str(tmp_path / "agent" / "agent_init.py")
    monkeypatch.setitem(sys.modules, "agent.agent_init", foreign)

    with pytest.raises(RuntimeError, match="agent.agent_init resolved"):
        gateway_run._load_runtime_ai_agent_class()


def test_api_server_create_agent_uses_runtime_root_loader(monkeypatch):
    """The API server constructs the class returned by the guarded loader."""
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    created = {}

    class GuardedAgent:
        def __init__(self, **kwargs):
            created.update(kwargs)
            self.model = kwargs["model"]
            self.provider = kwargs.get("provider")

    monkeypatch.setattr(
        gateway_run, "_load_runtime_ai_agent_class", lambda: GuardedAgent
    )
    monkeypatch.setattr(
        gateway_run,
        "_resolve_runtime_agent_kwargs",
        lambda: {"provider": "test-provider"},
    )
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: "test/model")
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_current_max_iterations", lambda: 1)
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_fallback_model",
        staticmethod(lambda: None),
    )
    monkeypatch.setattr(
        gateway_run.GatewayRunner,
        "_load_reasoning_config",
        staticmethod(lambda model="": None),
    )
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools", lambda *_: set()
    )

    adapter = APIServerAdapter(PlatformConfig(enabled=True))
    monkeypatch.setattr(adapter, "_ensure_session_db", lambda: None)

    agent = adapter._create_agent(session_id="guard-test")

    assert isinstance(agent, GuardedAgent)
    assert created["model"] == "test/model"
