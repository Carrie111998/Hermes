"""Regression coverage for explicit-profile delegation capability pre-flight."""

from __future__ import annotations

from contextlib import contextmanager
import json
import threading
import time
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from tools.delegate_tool import delegate_task


def _parent_agent(
    *, platform: str = "cli", enabled_toolsets=None, disabled_toolsets=None
) -> SimpleNamespace:
    """Minimal parent surface required by the real delegate_task entry point."""
    return SimpleNamespace(
        base_url="https://parent.invalid/v1",
        api_key="not-a-real-key",
        provider="openrouter",
        api_mode="chat_completions",
        model="parent-model",
        platform=platform,
        providers_allowed=None,
        providers_ignored=None,
        providers_order=None,
        provider_sort=None,
        enabled_toolsets=(
            ["file", "delegation"] if enabled_toolsets is None else enabled_toolsets
        ),
        disabled_toolsets=[] if disabled_toolsets is None else disabled_toolsets,
        _session_db=None,
        _delegate_depth=0,
        _active_children=[],
        _active_children_lock=threading.Lock(),
        _print_fn=None,
        tool_progress_callback=None,
        thinking_callback=None,
    )


def _write_profile(root, name: str, config: str) -> None:
    profile_home = root / "profiles" / name
    profile_home.mkdir(parents=True)
    (profile_home / "config.yaml").write_text(config, encoding="utf-8")


def test_apple_reminders_capability_mismatch_refuses_before_child_or_provider_work(
    monkeypatch, tmp_path, record_property
):
    """A named profile without computer_use must fail before delegation starts."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_profile(
        hermes_home,
        "reminders-specialist",
        """
platform_toolsets:
  cli:
    - file
agent:
  disabled_toolsets: []
model:
  provider: openrouter
  default: target-default-model
""",
    )

    counters = {
        "provider": 0,
        "child": 0,
        "live_transcript": 0,
        "child_execution": 0,
    }

    def _provider_called(*_args, **_kwargs):
        counters["provider"] += 1
        raise AssertionError("provider resolution must not run after pre-flight refusal")

    def _child_constructed(*_args, **_kwargs):
        counters["child"] += 1
        raise AssertionError("child construction must not run after pre-flight refusal")

    def _live_transcript_created(*_args, **_kwargs):
        counters["live_transcript"] += 1
        raise AssertionError("live transcript/session setup must not run after refusal")

    def _child_executed(*_args, **_kwargs):
        counters["child_execution"] += 1
        raise AssertionError("child execution/process work must not run after refusal")

    monkeypatch.setattr(
        "tools.delegate_tool._resolve_delegation_credentials", _provider_called
    )
    monkeypatch.setattr(
        "tools.delegate_tool._resolve_target_profile_credentials", _provider_called
    )
    monkeypatch.setattr(
        "tools.delegation_live_log.create_live_transcripts", _live_transcript_created
    )
    monkeypatch.setattr("tools.delegate_tool._run_single_child", _child_executed)
    monkeypatch.setattr(
        "tools.delegate_tool._build_child_preserving_parent_tools", _child_constructed
    )

    started = time.perf_counter()
    result = json.loads(
        delegate_task(
            goal="Create an Apple Reminder for the dentist appointment",
            profile="reminders-specialist",
            required_toolsets=["computer_use"],
            parent_agent=_parent_agent(),
        )
    )
    elapsed = time.perf_counter() - started
    elapsed_ms = elapsed * 1000
    record_property("preflight_elapsed_ms", f"{elapsed_ms:.3f}")
    record_property("provider_api_calls", counters["provider"])
    record_property(
        "child_session_process_creations",
        counters["child"] + counters["live_transcript"] + counters["child_execution"],
    )

    assert "error" in result
    assert "reminders-specialist" in result["error"]
    assert "computer_use" in result["error"]
    assert elapsed < 1.0
    assert counters == {
        "provider": 0,
        "child": 0,
        "live_transcript": 0,
        "child_execution": 0,
    }


def test_explicit_profile_and_model_use_target_runtime_without_mutating_profile_config(
    monkeypatch, tmp_path
):
    """A named profile owns the child runtime; model override remains ephemeral."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_profile(
        hermes_home,
        "research-specialist",
        """
platform_toolsets:
  cli:
    - file
agent:
  disabled_toolsets: []
model:
  provider: target-provider
  default: profile-default-model
""",
    )
    profile_home = hermes_home / "profiles" / "research-specialist"
    config_path = profile_home / "config.yaml"
    original_config = config_path.read_bytes()
    observed: dict[str, object] = {
        "resolver_home": None,
        "resolver_model": None,
        "child_home": None,
        "child_model": None,
        "run_home": None,
    }

    def _fake_runtime_provider(*, requested=None, target_model=None):
        from hermes_constants import get_hermes_home

        observed["resolver_home"] = get_hermes_home()
        observed["resolver_model"] = target_model
        assert requested == "target-provider"
        return {
            "provider": "target-provider",
            "base_url": "https://target.invalid/v1",
            "api_key": "in-memory-test-key",
            "api_mode": "chat_completions",
        }

    def _fake_run_conversation(**_kwargs):
        from hermes_constants import get_hermes_home

        observed["run_home"] = get_hermes_home()
        return {
            "final_response": "target complete",
            "completed": True,
            "api_calls": 0,
            "messages": [],
        }

    def _fake_child_agent(**kwargs):
        from hermes_constants import get_hermes_home

        observed["child_home"] = get_hermes_home()
        observed["child_model"] = kwargs["model"]
        return SimpleNamespace(
            session_id="target-child",
            model=kwargs["model"],
            _credential_pool=None,
            session_prompt_tokens=0,
            session_completion_tokens=0,
            session_estimated_cost_usd=0.0,
            run_conversation=_fake_run_conversation,
            close=lambda: None,
        )

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", _fake_runtime_provider
    )
    monkeypatch.setattr("run_agent.AIAgent", _fake_child_agent)

    result = json.loads(
        delegate_task(
            goal="Research the target profile behavior",
            profile="research-specialist",
            model="session-only-model",
            required_toolsets=["file"],
            parent_agent=_parent_agent(),
        )
    )

    assert result["results"][0]["status"] == "completed"
    assert observed["resolver_home"] == profile_home
    assert observed["child_home"] == profile_home
    assert observed["run_home"] == profile_home
    assert observed["resolver_model"] == "session-only-model"
    assert observed["child_model"] == "session-only-model"
    assert config_path.read_bytes() == original_config


def test_explicit_computer_use_target_constructs_real_child_with_capability_after_role_filtering(
    monkeypatch, tmp_path
):
    """A durable target keeps computer_use through the real AIAgent constructor."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_profile(
        hermes_home,
        "computer-specialist",
        """
platform_toolsets:
  cli:
    - computer_use
agent:
  disabled_toolsets: []
model:
  provider: integration-provider
  default: profile-default-model
  context_length: 65536
""",
    )

    # Make the host capability deterministic while retaining the registry,
    # toolset filtering, role filtering, and actual AIAgent initialization.
    import tools.computer_use_tool  # noqa: F401
    from model_tools import _clear_tool_defs_cache
    from run_agent import AIAgent
    from tools.registry import invalidate_check_fn_cache, registry

    monkeypatch.setattr(registry._tools["computer_use"], "check_fn", lambda: True)
    invalidate_check_fn_cache()
    _clear_tool_defs_cache()

    provider_calls: list[tuple[object, object]] = []
    api_calls = 0
    metadata_resolutions = 0
    observed: dict[str, object] = {}

    def _local_context_length(*_args, **_kwargs):
        """Keep construction offline even when the model override lacks catalog metadata."""
        nonlocal metadata_resolutions
        metadata_resolutions += 1
        return 65536

    def _api_forbidden(*_args, **_kwargs):
        nonlocal api_calls
        api_calls += 1
        raise AssertionError("real provider API calls are forbidden in this construction test")

    class _NoNetworkOpenAI:
        """Minimal client surface needed by real construction, never by execution."""

        def __init__(self, **kwargs):
            self.base_url = kwargs.get("base_url")
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=_api_forbidden)
            )
            self.responses = SimpleNamespace(create=_api_forbidden)

        def close(self):
            return None

    def _fake_runtime_provider(*, requested=None, target_model=None):
        provider_calls.append((requested, target_model))
        return {
            "provider": "openai",
            "base_url": "http://127.0.0.1:9/v1",
            "api_key": "in-memory-test-key",
            "api_mode": "chat_completions",
        }

    def _capture_real_child(task_index, goal, child, parent_agent):
        observed["child"] = child
        assert isinstance(child, AIAgent)
        child = cast(Any, child)
        assert child._delegate_role == "leaf"
        assert child._delegate_profile_name == "computer-specialist"
        assert "computer_use" in child.valid_tool_names
        assert "delegation" not in child.valid_tool_names
        return {
            "task_index": task_index,
            "status": "completed",
            "summary": "constructed without a provider call",
            "api_calls": 0,
            "duration_seconds": 0,
            "_child_role": child._delegate_role,
            "_child_cost_usd": 0.0,
        }

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", _fake_runtime_provider
    )
    monkeypatch.setattr("run_agent.OpenAI", _NoNetworkOpenAI)
    monkeypatch.setattr(
        "agent.context_compressor.get_model_context_length", _local_context_length
    )
    monkeypatch.setattr("tools.delegate_tool._run_single_child", _capture_real_child)

    result = json.loads(
        delegate_task(
            goal="Inspect the desktop safely",
            profile="computer-specialist",
            model="integration-session-model",
            required_toolsets=["computer_use"],
            parent_agent=_parent_agent(),
        )
    )

    child = cast(Any, observed["child"])
    assert result["results"][0]["status"] == "completed"
    assert provider_calls == [
        ("integration-provider", "integration-session-model"),
        ("integration-provider", "profile-default-model"),
    ]
    assert child.model == "integration-session-model"
    assert metadata_resolutions >= 1
    assert api_calls == 0


def test_delegate_schema_exposes_profile_model_and_required_toolsets_for_single_and_batch():
    """The one existing delegation mechanism advertises both targeting forms."""
    from tools.delegate_tool import DELEGATE_TASK_SCHEMA

    properties = DELEGATE_TASK_SCHEMA["parameters"]["properties"]
    task_properties = properties["tasks"]["items"]["properties"]

    for schema in (properties, task_properties):
        assert schema["profile"]["type"] == "string"
        assert schema["model"]["type"] == "string"
        assert schema["required_toolsets"]["type"] == "array"
        assert schema["required_toolsets"]["items"]["type"] == "string"


def test_missing_target_profile_refuses_before_provider_or_child_creation(monkeypatch, tmp_path):
    """A typo in an explicit target must not fall back to a generic clone."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    counters = {"provider": 0, "child": 0}

    def _forbidden_provider(*_args, **_kwargs):
        counters["provider"] += 1
        raise AssertionError("no provider lookup expected")

    def _forbidden_child(*_args, **_kwargs):
        counters["child"] += 1
        raise AssertionError("no child expected")

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", _forbidden_provider)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _forbidden_child)

    result = json.loads(
        delegate_task(
            goal="Do not run as a clone",
            profile="does-not-exist",
            parent_agent=_parent_agent(),
        )
    )

    assert "error" in result
    assert "Target profile 'does-not-exist' does not exist" in result["error"]
    assert counters == {"provider": 0, "child": 0}


def test_disabled_toolsets_are_subtracted_from_target_profile_capabilities(
    monkeypatch, tmp_path
):
    """Configured disables win even when a platform toolset lists the capability."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_profile(
        hermes_home,
        "limited-specialist",
        """
platform_toolsets:
  cli:
    - file
    - computer_use
agent:
  disabled_toolsets:
    - computer_use
model:
  provider: fake
  default: fake-model
""",
    )
    counters = {"provider": 0, "child": 0}

    def _forbidden_provider(*_args, **_kwargs):
        counters["provider"] += 1
        raise AssertionError("no provider lookup expected")

    def _forbidden_child(*_args, **_kwargs):
        counters["child"] += 1
        raise AssertionError("no child expected")

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", _forbidden_provider)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _forbidden_child)

    result = json.loads(
        delegate_task(
            goal="Use the unavailable computer",
            profile="limited-specialist",
            required_toolsets=["computer_use"],
            parent_agent=_parent_agent(),
        )
    )

    assert "limited-specialist" in result["error"]
    assert "computer_use" in result["error"]
    assert counters == {"provider": 0, "child": 0}


def test_target_profile_capabilities_resolve_for_originating_platform(monkeypatch, tmp_path):
    """A target's telegram capability is not replaced by the parent's CLI clone."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_profile(
        hermes_home,
        "platform-specialist",
        """
platform_toolsets:
  cli:
    - file
  telegram:
    - computer_use
agent:
  disabled_toolsets: []
model:
  provider: target-provider
  default: target-default
""",
    )
    observed: dict[str, object] = {}

    def _fake_runtime_provider(*, requested=None, target_model=None):
        assert requested == "target-provider"
        return {
            "provider": "target-provider",
            "base_url": "https://target.invalid/v1",
            "api_key": "in-memory-test-key",
            "api_mode": "chat_completions",
        }

    def _fake_build(**kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            model=kwargs["model"],
            _delegate_role="leaf",
            session_id="target-platform-child",
        )

    monkeypatch.setattr(
        "hermes_cli.runtime_provider.resolve_runtime_provider", _fake_runtime_provider
    )
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _fake_build)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda task_index, goal, child, parent_agent: {
            "task_index": task_index,
            "status": "completed",
            "summary": "done",
            "api_calls": 0,
            "duration_seconds": 0,
            "_child_role": "leaf",
            "_child_cost_usd": 0.0,
        },
    )

    result = json.loads(
        delegate_task(
            goal="Operate the selected target",
            profile="platform-specialist",
            required_toolsets=["computer_use"],
            parent_agent=_parent_agent(platform="telegram", enabled_toolsets=["file"]),
        )
    )

    assert result["results"][0]["status"] == "completed"
    assert observed["target_platform"] == "telegram"
    assert "computer_use" in observed["target_toolsets"]
    assert "computer_use" not in _parent_agent(enabled_toolsets=["file"]).enabled_toolsets


def test_batch_preflight_uses_top_level_defaults_and_reports_every_missing_toolset(
    monkeypatch, tmp_path
):
    """One bad batch target prevents every child from being constructed."""
    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_profile(
        hermes_home,
        "capable-specialist",
        """
platform_toolsets:
  cli:
    - file
agent:
  disabled_toolsets: []
""",
    )
    _write_profile(
        hermes_home,
        "limited-specialist",
        """
platform_toolsets:
  cli:
    - file
agent:
  disabled_toolsets: []
""",
    )
    counters = {"provider": 0, "child": 0}

    def _forbidden_provider(*_args, **_kwargs):
        counters["provider"] += 1
        raise AssertionError("all batch pre-flight checks must finish first")

    def _forbidden_child(*_args, **_kwargs):
        counters["child"] += 1
        raise AssertionError("no partial batch construction allowed")

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", _forbidden_provider)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _forbidden_child)

    result = json.loads(
        delegate_task(
            profile="limited-specialist",
            required_toolsets=["computer_use", "web"],
            tasks=[
                {
                    "goal": "Use the capable profile override",
                    "profile": "capable-specialist",
                    "required_toolsets": ["file"],
                },
                {"goal": "Use the top-level limited target"},
            ],
            parent_agent=_parent_agent(),
        )
    )

    assert "Task 1 target profile 'limited-specialist'" in result["error"]
    assert "computer_use" in result["error"]
    assert "web" in result["error"]
    assert counters == {"provider": 0, "child": 0}


def test_omitted_targeting_fields_preserve_legacy_child_construction(monkeypatch):
    """Existing calls without profile/model/capability contracts still execute."""
    observed: dict[str, object] = {}

    def _fake_legacy_credentials(_cfg, _parent, model_override=None):
        assert model_override is None
        return {
            "model": "legacy-model",
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }

    def _fake_build(**kwargs):
        observed.update(kwargs)
        return SimpleNamespace(
            model=kwargs["model"],
            _delegate_role="leaf",
            session_id="legacy-child",
        )

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", _fake_legacy_credentials)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _fake_build)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda task_index, goal, child, parent_agent: {
            "task_index": task_index,
            "status": "completed",
            "summary": "legacy complete",
            "api_calls": 0,
            "duration_seconds": 0,
            "_child_role": "leaf",
            "_child_cost_usd": 0.0,
        },
    )

    result = json.loads(delegate_task(goal="Keep historical behavior", parent_agent=_parent_agent()))

    assert result["results"][0]["status"] == "completed"
    assert observed["model"] == "legacy-model"
    assert "target_profile_name" not in observed


def test_required_toolsets_use_loaded_parent_capabilities_when_enabled_toolsets_is_none(
    monkeypatch,
):
    """All-tools parents pre-flight against loaded tools, not an empty set."""
    parent = _parent_agent()
    parent.enabled_toolsets = None
    parent.valid_tool_names = {"computer_use"}
    counters = {"provider": 0, "child": 0}

    def _fake_legacy_credentials(_cfg, _parent, model_override=None):
        counters["provider"] += 1
        return {
            "model": "legacy-model",
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }

    def _fake_build(**kwargs):
        counters["child"] += 1
        return SimpleNamespace(
            model=kwargs["model"],
            _delegate_role="leaf",
            session_id="loaded-parent-child",
        )

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", _fake_legacy_credentials)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _fake_build)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda task_index, goal, child, parent_agent: {
            "task_index": task_index,
            "status": "completed",
            "summary": "loaded capability complete",
            "api_calls": 0,
            "duration_seconds": 0,
            "_child_role": "leaf",
            "_child_cost_usd": 0.0,
        },
    )

    result = json.loads(
        delegate_task(
            goal="Use the available computer capability",
            required_toolsets=["computer_use"],
            parent_agent=parent,
        )
    )

    assert result["results"][0]["status"] == "completed"
    assert counters == {"provider": 1, "child": 1}


def test_required_toolsets_expand_configured_parent_composites_before_preflight(
    monkeypatch,
):
    """A hermes-cli parent provides file through the real child expansion path."""
    parent = _parent_agent(enabled_toolsets=["hermes-cli"])
    counters = {"provider": 0, "child": 0}

    def _fake_legacy_credentials(_cfg, _parent, model_override=None):
        counters["provider"] += 1
        return {
            "model": "legacy-model",
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }

    def _fake_build(**kwargs):
        counters["child"] += 1
        return SimpleNamespace(
            model=kwargs["model"],
            _delegate_role="leaf",
            session_id="composite-parent-child",
        )

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", _fake_legacy_credentials)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _fake_build)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda task_index, goal, child, parent_agent: {
            "task_index": task_index,
            "status": "completed",
            "summary": "composite capability complete",
            "api_calls": 0,
            "duration_seconds": 0,
            "_child_role": "leaf",
            "_child_cost_usd": 0.0,
        },
    )

    result = json.loads(
        delegate_task(
            goal="Read the local project files",
            required_toolsets=["file"],
            parent_agent=parent,
        )
    )

    assert "error" not in result, result
    assert result["results"][0]["status"] == "completed"
    assert counters == {"provider": 1, "child": 1}


def test_required_toolsets_respect_disabled_toolsets_after_composite_expansion(
    monkeypatch,
):
    """A disabled file toolset remains unavailable even through hermes-cli."""
    parent = _parent_agent(
        enabled_toolsets=["hermes-cli"], disabled_toolsets=["file"]
    )
    counters = {"provider": 0, "child": 0}

    def _provider_called(*_args, **_kwargs):
        counters["provider"] += 1
        raise AssertionError("pre-flight refusal must precede provider resolution")

    def _child_constructed(*_args, **_kwargs):
        counters["child"] += 1
        raise AssertionError("pre-flight refusal must precede child construction")

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", _provider_called)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _child_constructed)

    result = json.loads(
        delegate_task(
            goal="Read a disabled local file",
            required_toolsets=["file"],
            parent_agent=parent,
        )
    )

    assert "error" in result
    assert "file" in result["error"]
    assert counters == {"provider": 0, "child": 0}


def test_empty_loaded_parent_capabilities_do_not_fall_back_to_global_tool_discovery(
    monkeypatch,
):
    """An explicitly empty loaded-tool snapshot means no inherited capability."""
    parent = _parent_agent()
    parent.enabled_toolsets = None
    parent.valid_tool_names = set()
    counters = {"provider": 0, "child": 0, "fallback": 0}

    def _provider_called(*_args, **_kwargs):
        counters["provider"] += 1
        raise AssertionError("pre-flight refusal must precede provider resolution")

    def _child_constructed(*_args, **_kwargs):
        counters["child"] += 1
        raise AssertionError("pre-flight refusal must precede child construction")

    def _incorrect_global_fallback(**_kwargs):
        counters["fallback"] += 1
        return [{"function": {"name": "computer_use"}}]

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", _provider_called)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _child_constructed)
    monkeypatch.setattr("model_tools.get_tool_definitions", _incorrect_global_fallback)

    result = json.loads(
        delegate_task(
            goal="Use a computer capability that this parent did not load",
            required_toolsets=["computer_use"],
            parent_agent=parent,
        )
    )

    assert "error" in result
    assert "computer_use" in result["error"]
    assert counters == {"provider": 0, "child": 0, "fallback": 0}


def test_matching_profile_marker_and_canonical_path_enter_worker_scope(
    monkeypatch, tmp_path
):
    """A trusted marker may re-enter only its canonical custom-home profile."""
    import tools.delegate_tool as delegate_module
    from hermes_cli.profiles import get_profile_dir

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_profile(hermes_home, "worker-specialist", "agent: {}\n")
    profile_home = get_profile_dir("worker-specialist")
    child = SimpleNamespace(
        _delegate_profile_name="worker-specialist",
        _delegate_profile_home=str(profile_home),
    )
    scope_homes: list[object] = []
    fallback_calls = 0

    @contextmanager
    def _capture_scope(profile_path):
        scope_homes.append(profile_path)
        yield

    def _capture_fallback(task_index, goal, child, parent_agent, **_kwargs):
        nonlocal fallback_calls
        fallback_calls += 1
        return {"task_index": task_index, "status": "completed"}

    monkeypatch.setattr(delegate_module, "_delegated_profile_runtime_scope", _capture_scope)
    monkeypatch.setattr(
        delegate_module, "_run_single_child_in_profile_scope", _capture_fallback
    )

    result = delegate_module._run_single_child(
        0, "Run under the matching durable profile", child=child, parent_agent=_parent_agent()
    )

    assert result["status"] == "completed"
    assert scope_homes == [profile_home]
    assert fallback_calls == 1


def test_mismatched_profile_marker_and_absolute_path_do_not_enter_worker_scope(
    monkeypatch, tmp_path
):
    """A valid marker cannot switch scope to a different profile directory."""
    import tools.delegate_tool as delegate_module
    from hermes_cli.profiles import get_profile_dir

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_profile(hermes_home, "trusted-specialist", "agent: {}\n")
    _write_profile(hermes_home, "different-specialist", "agent: {}\n")
    trusted_home = get_profile_dir("trusted-specialist")
    different_home = get_profile_dir("different-specialist")
    child = SimpleNamespace(
        _delegate_profile_name="trusted-specialist",
        _delegate_profile_home=str(different_home),
    )
    scope_homes: list[object] = []
    fallback_calls = 0

    @contextmanager
    def _capture_scope(profile_path):
        scope_homes.append(profile_path)
        yield

    def _safe_fallback(task_index, goal, child, parent_agent, **_kwargs):
        nonlocal fallback_calls
        fallback_calls += 1
        return {"task_index": task_index, "status": "completed"}

    monkeypatch.setattr(delegate_module, "_delegated_profile_runtime_scope", _capture_scope)
    monkeypatch.setattr(
        delegate_module, "_run_single_child_in_profile_scope", _safe_fallback
    )

    result = delegate_module._run_single_child(
        0, "Do not trust a mismatched profile path", child=child, parent_agent=_parent_agent()
    )

    assert result["status"] == "completed"
    assert scope_homes == []
    assert fallback_calls == 1
    for profile_home in (trusted_home, different_home):
        assert not (profile_home / "cache").exists()
        assert not (profile_home / "SOUL.md").exists()


def test_malformed_profile_marker_cannot_establish_worker_scope(monkeypatch, tmp_path):
    """Traversal-like markers cannot turn an absolute path into a profile scope."""
    import tools.delegate_tool as delegate_module
    from hermes_cli.profiles import get_profile_dir

    hermes_home = tmp_path / "hermes-home"
    hermes_home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    _write_profile(hermes_home, "worker-specialist", "agent: {}\n")
    profile_home = get_profile_dir("worker-specialist")
    scope_homes: list[object] = []
    fallback_calls = 0

    @contextmanager
    def _capture_scope(profile_path):
        scope_homes.append(profile_path)
        yield

    def _safe_fallback(task_index, goal, child, parent_agent, **_kwargs):
        nonlocal fallback_calls
        fallback_calls += 1
        return {"task_index": task_index, "status": "completed"}

    monkeypatch.setattr(delegate_module, "_delegated_profile_runtime_scope", _capture_scope)
    monkeypatch.setattr(
        delegate_module, "_run_single_child_in_profile_scope", _safe_fallback
    )

    for marker in ("../worker-specialist", "worker-specialist/../../other"):
        child = SimpleNamespace(
            _delegate_profile_name=marker,
            _delegate_profile_home=str(profile_home),
        )
        result = delegate_module._run_single_child(
            0, "Do not trust a traversal marker", child=child, parent_agent=_parent_agent()
        )
        assert result["status"] == "completed"

    assert scope_homes == []
    assert fallback_calls == 2
    assert not (profile_home / "cache").exists()
    assert not (profile_home / "SOUL.md").exists()


def test_required_toolsets_accept_configured_atomic_parent_capability(monkeypatch):
    """Configured atomic parent toolsets retain their existing preflight behavior."""
    parent = _parent_agent(enabled_toolsets=["file"])
    counters = {"provider": 0, "child": 0}

    def _fake_credentials(_cfg, _parent, model_override=None):
        counters["provider"] += 1
        return {
            "model": "legacy-model",
            "provider": None,
            "base_url": None,
            "api_key": None,
            "api_mode": None,
        }

    def _fake_child(**kwargs):
        counters["child"] += 1
        return SimpleNamespace(
            model=kwargs["model"], _delegate_role="leaf", session_id="atomic-child"
        )

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", _fake_credentials)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _fake_child)
    monkeypatch.setattr(
        "tools.delegate_tool._run_single_child",
        lambda task_index, goal, child, parent_agent: {
            "task_index": task_index,
            "status": "completed",
            "summary": "atomic capability complete",
            "api_calls": 0,
            "duration_seconds": 0,
            "_child_role": "leaf",
            "_child_cost_usd": 0.0,
        },
    )

    result = json.loads(
        delegate_task(
            goal="Read a configured local file",
            required_toolsets=["file"],
            parent_agent=parent,
        )
    )

    assert result["results"][0]["status"] == "completed"
    assert counters == {"provider": 1, "child": 1}


def test_required_toolsets_refuse_explicit_empty_parent_configuration_without_discovery(
    monkeypatch,
):
    """An explicit empty configured list is restrictive rather than all-tools."""
    parent = _parent_agent(enabled_toolsets=[])
    counters = {"provider": 0, "child": 0, "fallback": 0}

    def _provider_called(*_args, **_kwargs):
        counters["provider"] += 1
        raise AssertionError("empty configured parents must refuse before provider resolution")

    def _child_constructed(*_args, **_kwargs):
        counters["child"] += 1
        raise AssertionError("empty configured parents must refuse before child construction")

    def _incorrect_global_fallback(**_kwargs):
        counters["fallback"] += 1
        return [{"function": {"name": "file"}}]

    monkeypatch.setattr("tools.delegate_tool._resolve_delegation_credentials", _provider_called)
    monkeypatch.setattr("tools.delegate_tool._build_child_preserving_parent_tools", _child_constructed)
    monkeypatch.setattr("model_tools.get_tool_definitions", _incorrect_global_fallback)

    result = json.loads(
        delegate_task(
            goal="Use a capability that was explicitly disabled by an empty configuration",
            required_toolsets=["file"],
            parent_agent=parent,
        )
    )

    assert "error" in result
    assert "file" in result["error"]
    assert counters == {"provider": 0, "child": 0, "fallback": 0}


def test_generic_mock_child_does_not_create_a_profile_scope_or_cache_path(monkeypatch):
    """Only a real absolute target path may activate durable-profile scoping."""
    import tools.delegate_tool as delegate_module

    child = MagicMock()
    child._delegate_profile_home = "/tmp/generic-child-does-not-own-a-profile"
    scope_calls = 0

    def _unexpected_scope(_profile_home):
        nonlocal scope_calls
        scope_calls += 1
        raise AssertionError("a generic mock child is not a durable profile target")

    monkeypatch.setattr(delegate_module, "_delegated_profile_runtime_scope", _unexpected_scope)
    monkeypatch.setattr(
        delegate_module,
        "_run_single_child_in_profile_scope",
        lambda task_index, goal, child, parent_agent, **_kwargs: {
            "task_index": task_index,
            "status": "completed",
        },
    )

    result = delegate_module._run_single_child(
        0,
        "Verify generic child execution remains unscoped",
        child=child,
        parent_agent=_parent_agent(),
    )

    assert result["status"] == "completed"
    assert scope_calls == 0


def test_run_agent_dispatcher_forwards_new_targeting_fields(monkeypatch):
    """The real model-facing dispatch path must not drop the new schema fields."""
    from run_agent import AIAgent

    captured: dict[str, object] = {}
    agent = object.__new__(AIAgent)
    agent._delegate_depth = 1

    def _capture_delegate(**kwargs):
        captured.update(kwargs)
        return "delegated"

    monkeypatch.setattr("tools.delegate_tool.delegate_task", _capture_delegate)

    result = AIAgent._dispatch_delegate_task(
        agent,
        {
            "goal": "Dispatch through the real tool path",
            "profile": "specialist",
            "model": "one-session-model",
            "required_toolsets": ["computer_use"],
        },
    )

    assert result == "delegated"
    assert captured["profile"] == "specialist"
    assert captured["model"] == "one-session-model"
    assert captured["required_toolsets"] == ["computer_use"]
