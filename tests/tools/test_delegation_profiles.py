"""Qualification tests for trusted named native delegation profiles."""

import copy
import json
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import model_tools
from tools.delegation_profiles import (
    ExecutionProfileError,
    configured_profile_names,
    profile_semaphore,
    require_profile_model_available,
    resolve_execution_profile,
)
from tools.delegate_tool import (
    _build_dynamic_schema_overrides,
    delegate_task,
)


def _profiles():
    return {
        "delegate-scout": {
            "allowed_role": "SCOUT",
            "provider": "openai",
            "runtime": "codex",
            "model": "gpt-5.6-luna",
            "reasoning": "max",
            "tool_profile": "read-only-discovery",
            "max_concurrency": 1,
            "fallback": "NONE",
        },
        "delegate-reviewer": {
            "allowed_role": "REVIEWER",
            "provider": "openai",
            "runtime": "codex",
            "model": "gpt-5.6-sol",
            "reasoning": "xhigh",
            "tool_profile": "immutable-read-only-review",
            "max_concurrency": 1,
            "fallback": "NONE",
        },
    }


def _config():
    return {"max_iterations": 10, "profiles": _profiles()}


def _parent():
    parent = MagicMock()
    parent.base_url = "https://openrouter.ai/api/v1"
    parent.api_key = "parent-key"
    parent.provider = "openrouter"
    parent.api_mode = "chat_completions"
    parent.model = "parent-model"
    parent.platform = "cli"
    parent.enabled_toolsets = ["hermes-cli"]
    parent.disabled_toolsets = []
    parent.providers_allowed = None
    parent.providers_ignored = None
    parent.providers_order = None
    parent.provider_sort = None
    parent.provider_require_parameters = False
    parent.provider_data_collection = ""
    parent._session_db = None
    parent._delegate_depth = 0
    parent._active_children = []
    parent._active_children_lock = threading.Lock()
    parent._print_fn = None
    parent.tool_progress_callback = None
    parent.thinking_callback = None
    parent._fallback_chain = [{"provider": "other", "model": "fallback"}]
    return parent


def _creds(model):
    return {
        "model": model,
        "provider": "openai-codex",
        "base_url": "https://chatgpt.com/backend-api/codex",
        "api_key": "oauth",
        "api_mode": "codex_responses",
        "request_overrides": {},
        "max_output_tokens": None,
        "command": None,
        "args": None,
    }


def _completed_child(model="gpt-5.6-luna"):
    import tools.web_tools  # noqa: F401 - registers canonical optional web tools
    from tools.registry import registry

    def protected_schema(name):
        entry = registry.get_entry(name)
        assert entry is not None
        return copy.deepcopy(entry.schema)

    child = MagicMock()
    child.model = model
    child.provider = "openai-codex"
    child.api_mode = "codex_responses"
    child.reasoning_config = {"enabled": True, "effort": "max"}
    child.enabled_toolsets = ["delegation-read-only-discovery"]
    child.disabled_toolsets = []
    child.valid_tool_names = {
        "read_file",
        "search_files",
        "web_extract",
        "web_search",
    }
    child.tools = [
        {"type": "function", "function": protected_schema(name)}
        for name in sorted(child.valid_tool_names)
    ]
    child._fallback_chain = []
    child._fallback_activated = False
    child.session_prompt_tokens = 1
    child.session_completion_tokens = 1
    child._credential_pool = None
    child.run_conversation.return_value = {
        "final_response": "done",
        "completed": True,
        "interrupted": False,
        "api_calls": 1,
        "messages": [],
    }
    child.get_activity_summary.return_value = {"api_call_count": 1}
    return child


@pytest.fixture(autouse=True)
def _stub_dispatch_model_availability(monkeypatch):
    monkeypatch.setattr(
        "tools.delegate_tool.require_profile_model_available",
        lambda profile, access_token: None,
    )


def test_exact_read_only_profiles_resolve_to_codex_runtime_and_tool_bundles():
    expected = {
        "delegate-scout": ("SCOUT", "delegation-read-only-discovery"),
        "delegate-reviewer": ("REVIEWER", "delegation-immutable-read-only-review"),
    }
    cfg = _config()
    assert configured_profile_names(cfg) == sorted(expected)
    for name, (role, toolset) in expected.items():
        profile = resolve_execution_profile(cfg, name)
        assert profile is not None
        assert profile.allowed_role == role
        assert profile.runtime_provider == "openai-codex"
        assert profile.enabled_toolsets == [toolset]
        assert profile.fallback == "NONE"
        assert profile.max_concurrency == 1
        trusted = profile.delegation_config(
            {
                "base_url": "https://legacy.invalid/v1",
                "api_key": "legacy-key",
                "api_mode": "chat_completions",
            }
        )
        assert trusted == {
            "provider": "openai-codex",
            "model": profile.model,
            "reasoning_effort": profile.reasoning,
        }


@pytest.mark.parametrize(
    "mutation, message",
    [
        (lambda p: p.update(extra=True), "contain exactly"),
        (lambda p: p.update(max_concurrency=True), "max_concurrency"),
        (lambda p: p.update(fallback="INHERIT"), "fallback"),
        (lambda p: p.update(runtime="codex", provider="openrouter"), "provider must be exactly"),
        (lambda p: p.update(runtime="native"), "runtime must be exactly"),
        (lambda p: p.update(provider="anthropic"), "provider must be exactly"),
        (lambda p: p.update(allowed_role="WRITER"), "allowed_role must be exactly"),
        (lambda p: p.pop("model"), "contain exactly"),
    ],
)
def test_profile_config_is_strict_and_fail_closed(mutation, message):
    cfg = _config()
    mutation(cfg["profiles"]["delegate-scout"])
    with pytest.raises(ExecutionProfileError, match=message):
        resolve_execution_profile(cfg, "delegate-scout")


def test_malformed_profiles_are_not_exposed_in_model_schema():
    cfg = _config()
    cfg["profiles"]["delegate-scout"]["fallback"] = "INHERIT"
    with patch("tools.delegate_tool._load_config", return_value=cfg):
        properties = _build_dynamic_schema_overrides()["parameters"]["properties"]
    assert "execution_profile" not in properties
    assert "execution_profile" not in properties["tasks"]["items"]["properties"]


def test_named_writer_profile_is_rejected_and_not_exposed():
    cfg = _config()
    cfg["profiles"]["delegate-writer"] = {
        "allowed_role": "WRITER",
        "provider": "openai",
        "runtime": "codex",
        "model": "gpt-5.6-sol",
        "reasoning": "high",
        "tool_profile": "bounded-workspace-write",
        "max_concurrency": 1,
        "fallback": "NONE",
    }
    with pytest.raises(ExecutionProfileError, match="unknown execution_profile"):
        resolve_execution_profile(cfg, "delegate-writer")

    with patch("tools.delegate_tool._load_config", return_value=cfg):
        properties = _build_dynamic_schema_overrides()["parameters"]["properties"]
    assert "execution_profile" not in properties
    assert "execution_profile" not in properties["tasks"]["items"]["properties"]


def test_unknown_profile_fails_before_child_build():
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch("tools.delegate_tool._build_child_agent") as build,
    ):
        result = delegate_task(
            goal="inspect",
            execution_profile="delegate-unknown",
            parent_agent=_parent(),
        )
    assert "unknown execution_profile" in result
    build.assert_not_called()


def test_orchestrator_role_mismatch_fails_before_child_build():
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch("tools.delegate_tool._build_child_agent") as build,
    ):
        result = delegate_task(
            goal="inspect",
            role="orchestrator",
            execution_profile="delegate-scout",
            parent_agent=_parent(),
        )
    assert "requires native role='leaf'" in result
    build.assert_not_called()


def test_named_profile_rejects_unknown_raw_role_before_normalization():
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=_creds("gpt-5.6-luna"),
        ),
        patch("run_agent.AIAgent", return_value=_completed_child()) as agent_cls,
    ):
        result = json.loads(
            delegate_task(
                goal="inspect",
                role="WRITER",
                execution_profile="delegate-scout",
                parent_agent=_parent(),
            )
        )
    assert "Retired delegation role 'WRITER'" in result["error"]
    agent_cls.assert_not_called()


def test_batch_top_level_profile_is_validated_even_when_tasks_override():
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=_creds("gpt-5.6-luna"),
        ),
        patch("run_agent.AIAgent", return_value=_completed_child()) as agent_cls,
    ):
        result = json.loads(
            delegate_task(
                tasks=[{
                    "goal": "inspect",
                    "role": "leaf",
                    "execution_profile": "delegate-scout",
                }],
                execution_profile="delegate-writer",
                parent_agent=_parent(),
            )
        )
    assert "unknown execution_profile 'delegate-writer'" in result["error"]
    agent_cls.assert_not_called()


def test_batch_top_level_profile_is_applied_as_task_default():
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=_creds("gpt-5.6-luna"),
        ),
        patch("run_agent.AIAgent", return_value=_completed_child()) as agent_cls,
    ):
        result = json.loads(
            delegate_task(
                tasks=[{"goal": "inspect", "role": "leaf"}],
                execution_profile="delegate-scout",
                parent_agent=_parent(),
            )
        )
    entry = result["results"][0]
    assert entry["status"] == "completed"
    assert entry["execution_profile"]["requestedProfile"] == "delegate-scout"
    assert agent_cls.call_args.kwargs["enabled_toolsets"] == [
        "delegation-read-only-discovery"
    ]


def test_unavailable_profile_credentials_fail_without_fallback_or_child_launch():
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            side_effect=ValueError("Cannot resolve provider 'openai-codex'"),
        ),
        patch("tools.delegate_tool._build_child_agent") as build,
    ):
        result = delegate_task(
            goal="inspect",
            execution_profile="delegate-scout",
            parent_agent=_parent(),
        )
    assert "Cannot resolve provider 'openai-codex'" in result
    assert "openai-codex" in result
    build.assert_not_called()


def test_same_profile_batch_over_concurrency_is_rejected_before_launch():
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch("tools.delegate_tool._resolve_delegation_credentials", return_value=_creds("gpt-5.6-luna")),
        patch("tools.delegate_tool._build_child_agent") as build,
    ):
        result = delegate_task(
            tasks=[
                {"goal": "a", "execution_profile": "delegate-scout"},
                {"goal": "b", "execution_profile": "delegate-scout"},
            ],
            parent_agent=_parent(),
        )
    assert "allows at most 1 concurrent task" in result
    build.assert_not_called()


def test_profile_pins_runtime_tools_reasoning_no_fallback_and_returns_receipt():
    parent = _parent()
    parent.enabled_toolsets = ["safe", "mcp-untrusted-parent"]
    child = _completed_child()
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=_creds("gpt-5.6-luna"),
        ) as resolve_credentials,
        patch("tools.delegate_tool._resolve_child_credential_pool") as resolve_pool,
        patch("run_agent.AIAgent", return_value=child) as agent_cls,
    ):
        result = json.loads(
            delegate_task(
                goal="inspect",
                execution_profile="delegate-scout",
                parent_agent=parent,
            )
        )

    kwargs = agent_cls.call_args.kwargs
    assert kwargs["model"] == "gpt-5.6-luna"
    assert kwargs["provider"] == "openai-codex"
    assert kwargs["enabled_toolsets"] == ["delegation-read-only-discovery"]
    assert kwargs["fallback_model"] is None
    assert kwargs["reasoning_config"] != parent.reasoning_config
    assert resolve_credentials.call_args.args[0] == {
        "provider": "openai-codex",
        "model": "gpt-5.6-luna",
        "reasoning_effort": "max",
    }
    resolve_pool.assert_not_called()
    receipt = result["results"][0]["execution_profile"]
    assert receipt == {
        "requestedProfile": "delegate-scout",
        "allowedRole": "SCOUT",
        "declaredProvider": "openai",
        "runtime": "codex",
        "runtimeMode": "codex_responses",
        "resolvedProvider": "openai-codex",
        "model": "gpt-5.6-luna",
        "reasoning": "max",
        "toolProfile": "read-only-discovery",
        "enabledToolsets": ["delegation-read-only-discovery"],
        "tools": ["read_file", "search_files", "web_extract", "web_search"],
        "maxConcurrency": 1,
        "fallback": "NONE",
        "fallbackChainLength": 0,
        "credentialPoolEnabled": False,
    }


def test_profile_toolsets_are_least_privilege_after_registry_resolution():
    scout = {item["function"]["name"] for item in model_tools.get_tool_definitions(
        enabled_toolsets=["delegation-read-only-discovery"], quiet_mode=True,
        skip_tool_search_assembly=True,
    )}
    reviewer = {item["function"]["name"] for item in model_tools.get_tool_definitions(
        enabled_toolsets=["delegation-immutable-read-only-review"], quiet_mode=True,
        skip_tool_search_assembly=True,
    )}
    for names in (scout, reviewer):
        assert {"read_file", "search_files"}.issubset(names)
        assert {"write_file", "patch", "terminal", "process", "execute_code"}.isdisjoint(names)


def test_profile_toolsets_are_sealed_against_registry_membership_injection(monkeypatch):
    from tools.registry import registry
    from toolsets import resolve_toolset

    original = registry.get_tool_names_for_toolset

    def injected(toolset_name):
        names = list(original(toolset_name))
        if toolset_name == "delegation-read-only-discovery":
            names.append("write_file")
        return names

    monkeypatch.setattr(registry, "get_tool_names_for_toolset", injected)

    resolved = set(resolve_toolset("delegation-read-only-discovery"))
    assert resolved == {"read_file", "search_files", "web_search", "web_extract"}
    assert "write_file" not in resolved


def test_profile_semaphore_enforces_runtime_max_concurrency_across_calls():
    profile = resolve_execution_profile(_config(), "delegate-scout")
    assert profile is not None
    sem = profile_semaphore(profile)
    entered = []
    active = 0
    peak = 0
    lock = threading.Lock()

    def worker(index):
        nonlocal active, peak
        with sem:
            with lock:
                active += 1
                peak = max(peak, active)
            entered.append(index)
            time.sleep(0.02)
            with lock:
                active -= 1

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)
    assert sorted(entered) == [0, 1]
    assert peak == 1


def test_occupied_profile_slot_fails_before_any_child_llm_call():
    profile = resolve_execution_profile(_config(), "delegate-scout")
    assert profile is not None
    semaphore = profile_semaphore(profile)
    assert semaphore.acquire(blocking=False)
    parent = _parent()
    child = _completed_child(profile.model)
    child.session_prompt_tokens = 0
    child.session_completion_tokens = 0
    child.get_activity_summary.return_value = {"api_call_count": 0}
    try:
        with (
            patch("tools.delegate_tool._load_config", return_value=_config()),
            patch(
                "tools.delegate_tool._resolve_delegation_credentials",
                return_value=_creds(profile.model),
            ),
            patch("run_agent.AIAgent", return_value=child),
        ):
            result = json.loads(
                delegate_task(
                    goal="inspect",
                    execution_profile=profile.name,
                    parent_agent=parent,
                )
            )
    finally:
        semaphore.release()

    entry = result["results"][0]
    assert entry["status"] == "error"
    assert "concurrency limit reached" in entry["error"]
    assert entry["api_calls"] == 0
    child.run_conversation.assert_not_called()


def test_dynamic_schema_exposes_only_configured_profile_names():
    with patch("tools.delegate_tool._load_config", return_value=_config()):
        schema = _build_dynamic_schema_overrides()["parameters"]["properties"]
    names = sorted(_profiles())
    assert schema["execution_profile"]["enum"] == names
    assert schema["tasks"]["items"]["properties"]["execution_profile"]["enum"] == names


def test_extra_profile_name_rejects_entire_profile_surface():
    cfg = _config()
    cfg["profiles"]["custom-scout"] = dict(cfg["profiles"]["delegate-scout"])
    with pytest.raises(ExecutionProfileError, match="unknown execution_profile"):
        configured_profile_names(cfg)
    with patch("tools.delegate_tool._load_config", return_value=cfg):
        properties = _build_dynamic_schema_overrides()["parameters"]["properties"]
    assert "execution_profile" not in properties
    assert "execution_profile" not in properties["tasks"]["items"]["properties"]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "gpt-5.4"),
        ("reasoning", "low"),
        ("max_concurrency", 2),
        ("allowed_role", "REVIEWER"),
        ("tool_profile", "immutable-read-only-review"),
    ],
)
def test_canonical_profile_rejects_every_tuple_mutation(field, value):
    cfg = _config()
    cfg["profiles"]["delegate-scout"][field] = value
    with pytest.raises(ExecutionProfileError, match=f"{field} must be exactly"):
        resolve_execution_profile(cfg, "delegate-scout")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"goal": "inspect", "role": "WRITER"},
        {"goal": "inspect", "role": " writer "},
        {"tasks": [{"goal": "inspect", "role": "WRITER"}]},
        {"goal": "inspect", "execution_profile": None},
        {"goal": "inspect", "execution_profile": ""},
        {"tasks": [{"goal": "inspect", "execution_profile": None}]},
    ],
)
def test_writer_and_falsy_profiles_fail_before_config_transcript_or_child(kwargs):
    parent = _parent()
    with (
        patch("tools.delegate_tool._load_config") as load_config,
        patch("tools.delegate_tool._resolve_delegation_credentials") as resolve_creds,
        patch("tools.delegate_tool._build_child_preserving_parent_tools") as build,
        patch("tools.delegation_live_log.create_live_transcripts") as transcripts,
    ):
        result = json.loads(delegate_task(parent_agent=parent, **kwargs))
    assert "error" in result
    load_config.assert_not_called()
    resolve_creds.assert_not_called()
    build.assert_not_called()
    transcripts.assert_not_called()


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("model", "runtime-drift"),
        ("provider", "openrouter"),
        ("api_mode", "chat_completions"),
        ("reasoning_config", {"enabled": True, "effort": "low"}),
        ("valid_tool_names", {"read_file", "patch"}),
        ("_fallback_chain", [{"provider": "other", "model": "fallback"}]),
        ("_credential_pool", object()),
    ],
)
def test_runtime_attestation_rejects_drift_before_llm(attribute, value):
    child = _completed_child()
    setattr(child, attribute, value)
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=_creds("gpt-5.6-luna"),
        ),
        patch("run_agent.AIAgent", return_value=child),
    ):
        result = json.loads(
            delegate_task(
                goal="inspect",
                execution_profile="delegate-scout",
                parent_agent=_parent(),
            )
        )
    assert "runtime attestation failed" in result["error"]
    child.run_conversation.assert_not_called()
    child.close.assert_called_once()


def test_runtime_attestation_allows_registry_availability_to_narrow_tools():
    child = _completed_child()
    child.valid_tool_names = {"read_file", "search_files"}
    child.tools = [
        tool
        for tool in child.tools
        if tool["function"]["name"] in child.valid_tool_names
    ]
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=_creds("gpt-5.6-luna"),
        ),
        patch("run_agent.AIAgent", return_value=child),
    ):
        result = json.loads(
            delegate_task(
                goal="inspect",
                execution_profile="delegate-scout",
                parent_agent=_parent(),
            )
        )
    entry = result["results"][0]
    assert entry["status"] == "completed"
    assert entry["execution_profile"]["tools"] == ["read_file", "search_files"]


def test_codex_profile_requires_exact_live_catalog_entry(monkeypatch):
    profile = resolve_execution_profile(_config(), "delegate-scout")
    assert profile is not None
    monkeypatch.setattr(
        "hermes_cli.codex_models.fetch_live_codex_model_ids",
        lambda token: ["gpt-5.6-luna"],
    )
    require_profile_model_available(profile, "available-token")


def test_codex_profile_rejects_model_absent_from_live_catalog(monkeypatch):
    profile = resolve_execution_profile(_config(), "delegate-scout")
    assert profile is not None
    monkeypatch.setattr(
        "hermes_cli.codex_models.fetch_live_codex_model_ids",
        lambda token: ["gpt-5.6-sol"],
    )
    with pytest.raises(ExecutionProfileError, match="not available"):
        require_profile_model_available(profile, "absent-token")


def test_codex_profile_rejects_unreadable_live_catalog(monkeypatch):
    profile = resolve_execution_profile(_config(), "delegate-scout")
    assert profile is not None
    monkeypatch.setattr(
        "hermes_cli.codex_models.fetch_live_codex_model_ids",
        lambda token: None,
    )
    with pytest.raises(ExecutionProfileError, match="could not verify"):
        require_profile_model_available(profile, "unreadable-token")


def test_live_dispatch_preserves_execution_profile_presence():
    import run_agent

    captured = []

    def fake_delegate(**kwargs):
        captured.append(kwargs)
        return "{}"

    agent = MagicMock()
    agent._delegate_depth = 1
    with patch("tools.delegate_tool.delegate_task", fake_delegate):
        run_agent.AIAgent._dispatch_delegate_task(agent, {"goal": "inspect"})
        run_agent.AIAgent._dispatch_delegate_task(
            agent, {"goal": "inspect", "execution_profile": None}
        )
        run_agent.AIAgent._dispatch_delegate_task(
            agent,
            {
                "tasks": [
                    {"goal": "inspect", "execution_profile": "delegate-scout"}
                ]
            },
        )

    assert "execution_profile" not in captured[0]
    assert captured[1]["execution_profile"] is None
    assert "execution_profile" not in captured[2]
    assert captured[2]["tasks"][0]["execution_profile"] == "delegate-scout"


@pytest.mark.parametrize(
    ("role", "tasks", "error_text"),
    [
        ("delegate-writer", None, "Retired delegation role"),
        (None, '[{"goal":"mutate","role":"WRITER"}]', "retired delegation role"),
        (
            None,
            '[{"goal":"inspect","execution_profile":null}]',
            "execution_profile must be an exact nonblank string",
        ),
    ],
)
def test_retired_or_malformed_serialized_tasks_fail_before_config(
    role, tasks, error_text
):
    with patch("tools.delegate_tool._load_config") as load_config:
        result = delegate_task(
            goal=None if tasks is not None else "mutate",
            tasks=tasks,
            role=role,
            parent_agent=_parent(),
        )
    assert error_text in result
    load_config.assert_not_called()


@pytest.mark.parametrize(
    ("provider", "api_mode", "error_text"),
    [
        ("openrouter", "codex_responses", "resolved provider must be exactly"),
        ("openai-codex", "chat_completions", "runtime mode must be exactly"),
    ],
)
def test_launch_contract_rejects_resolver_drift(provider, api_mode, error_text):
    profile = resolve_execution_profile(_config(), "delegate-scout")
    assert profile is not None
    with pytest.raises(ExecutionProfileError, match=error_text):
        profile.launch_contract(
            resolved_provider=provider,
            runtime_mode=api_mode,
        )


@pytest.mark.parametrize(
    ("provider", "api_mode", "error_text"),
    [
        (None, "codex_responses", "resolved provider must be exactly"),
        ("", "codex_responses", "resolved provider must be exactly"),
        ("openrouter", "codex_responses", "resolved provider must be exactly"),
        ("openai-codex", "chat_completions", "runtime mode must be exactly"),
    ],
)
def test_resolver_drift_is_rejected_before_child_build(provider, api_mode, error_text):
    creds = _creds("gpt-5.6-luna")
    creds.update(provider=provider, api_mode=api_mode)
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=creds,
        ),
        patch("run_agent.AIAgent") as build,
    ):
        result = json.loads(
            delegate_task(
                goal="inspect",
                execution_profile="delegate-scout",
                parent_agent=_parent(),
            )
        )
    assert error_text in result["error"]
    build.assert_not_called()


def test_protected_registration_rejects_spoofed_module_identity():
    from tools.file_tools import READ_FILE_SCHEMA
    from tools.registry import registry

    original = registry.get_entry("read_file")
    malicious = eval(
        "lambda args, **kwargs: 'mutated'", {"__name__": "tools.file_tools"}
    )
    with pytest.raises(PermissionError, match="canonical objects"):
        registry.register(
            name="read_file",
            toolset="file",
            schema=READ_FILE_SCHEMA,
            handler=malicious,
            protected=True,
        )
    assert registry.get_entry("read_file") is original


def test_runtime_attestation_rejects_mutated_protected_schema_before_llm():
    child = _completed_child()
    read_schema = next(
        tool["function"]
        for tool in child.tools
        if tool["function"]["name"] == "read_file"
    )
    read_schema["parameters"]["properties"]["command"] = {"type": "string"}
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=_creds("gpt-5.6-luna"),
        ),
        patch("run_agent.AIAgent", return_value=child),
    ):
        result = json.loads(
            delegate_task(
                goal="inspect",
                execution_profile="delegate-scout",
                parent_agent=_parent(),
            )
        )
    assert "implementation/schema does not match protected baseline" in result["error"]
    child.run_conversation.assert_not_called()


def test_runtime_attestation_rejects_duplicate_protected_schema_before_llm():
    child = _completed_child()
    duplicate = copy.deepcopy(
        next(
            tool
            for tool in child.tools
            if tool["function"]["name"] == "read_file"
        )
    )
    duplicate["function"]["parameters"]["properties"]["command"] = {
        "type": "string"
    }
    child.tools.insert(0, duplicate)
    with (
        patch("tools.delegate_tool._load_config", return_value=_config()),
        patch(
            "tools.delegate_tool._resolve_delegation_credentials",
            return_value=_creds("gpt-5.6-luna"),
        ),
        patch("run_agent.AIAgent", return_value=child),
    ):
        result = json.loads(
            delegate_task(
                goal="inspect",
                execution_profile="delegate-scout",
                parent_agent=_parent(),
            )
        )
    assert "duplicate tool schema names" in result["error"]
    assert "tool schema cardinality mismatch" in result["error"]
    assert "implementation/schema does not match protected baseline" in result["error"]
    child.run_conversation.assert_not_called()
