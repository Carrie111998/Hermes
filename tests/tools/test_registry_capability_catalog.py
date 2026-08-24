"""Tests for the read-only tool capability catalog.

The catalog is the machine-readable foundation for capability diffs and
least-tool orchestration.  Its default path must not execute availability
probes or expose environment values/implementation paths.
"""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
from functools import partial
from types import FunctionType
from typing import Callable, cast
from unittest.mock import Mock

import tools.registry as registry_module
from tools.registry import ToolRegistry


def _schema(name: str) -> dict:
    return {
        "name": name,
        "description": f"{name} description",
        "parameters": {"type": "object", "properties": {}},
    }


def _handler():
    return "ok"


def _handler_in_module(module_name: str):
    return FunctionType(_handler.__code__, {"__name__": module_name})


def test_catalog_is_deterministic_redacted_and_does_not_probe(monkeypatch):
    calls = []

    def check():
        calls.append("called")
        return True

    registry = ToolRegistry()
    suspicious_access_key = "AK" + "IA" + "IOSFODNN7EXAMPLE"
    suspicious_temporary_access_key = "AS" + "IA" + "IOSFODNN7EXAMPLE"
    suspicious_github_token = "gh" + "p_" + "exampletoken"
    monkeypatch.setenv("CATALOG_TEST_TOKEN", "do-not-leak")
    registry.register(
        name="zeta",
        toolset="network",
        schema=_schema("zeta"),
        handler=_handler,
        check_fn=check,
        requires_env=[
            "CATALOG_TEST_TOKEN",
            suspicious_access_key,
            suspicious_temporary_access_key,
            suspicious_github_token,
            "/Users/alice/.secrets/token",
            "token=secret-value",
        ],
        is_async=True,
        max_result_size_chars=4096,
    )
    registry.register(
        name="alpha",
        toolset="local",
        schema=_schema("alpha"),
        handler=_handler,
    )

    first = registry.get_capability_catalog()
    second = registry.get_capability_catalog()

    assert first == second
    assert calls == []
    assert first["schema_version"] == 1
    assert [tool["name"] for tool in first["tools"]] == ["alpha", "zeta"]
    assert first["toolsets"] == ["local", "network"]

    zeta = first["tools"][1]
    assert zeta["availability"] == "unknown"
    assert zeta["requires_env"] == ["CATALOG_TEST_TOKEN"]
    assert zeta["requires_env_redacted"] is True
    assert zeta["is_async"] is True
    assert zeta["max_result_size_chars"] == 4096
    assert "description" not in zeta
    assert "probe_error" not in zeta
    assert (
        zeta["schema_sha256"]
        == hashlib.sha256(
            json.dumps(
                {
                    "name": "zeta",
                    "parameters": {"type": "object", "properties": {}},
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
    )

    serialized = json.dumps(first)
    assert "do-not-leak" not in serialized
    assert suspicious_access_key not in serialized
    assert suspicious_temporary_access_key not in serialized
    assert suspicious_github_token not in serialized
    assert "Users/" not in serialized


def test_schema_digest_ignores_annotations_defaults_and_set_order():
    first = ToolRegistry()
    second = ToolRegistry()
    first.register(
        "stable",
        "local",
        {
            "name": "stable",
            "description": "path /Users/private/one",
            "parameters": {
                "type": "object",
                "required": ["beta", "alpha"],
                "properties": {
                    "alpha": {"type": "string", "default": "secret-one"},
                    "beta": {"type": "string", "examples": ["private-one"]},
                },
            },
        },
        _handler,
    )
    second.register(
        "stable",
        "local",
        {
            "name": "stable",
            "description": "path /Users/private/two",
            "parameters": {
                "type": "object",
                "required": ["alpha", "beta"],
                "properties": {
                    "alpha": {"type": "string", "default": "secret-two"},
                    "beta": {"type": "string", "examples": ["private-two"]},
                },
            },
        },
        _handler,
    )

    first_digest = first.get_capability_catalog()["tools"][0]["schema_sha256"]
    second_digest = second.get_capability_catalog()["tools"][0]["schema_sha256"]

    assert first_digest == second_digest


def test_schema_digest_preserves_id_and_ignores_standard_annotations():
    def digest(*, schema_id: str, marker: str) -> str:
        registry = ToolRegistry()
        registry.register(
            "stable",
            "local",
            {
                "name": "stable",
                "parameters": {
                    "$id": schema_id,
                    "$ref": "child.json",
                    "deprecated": marker == "deprecated",
                    "readOnly": marker == "read-only",
                    "writeOnly": marker == "write-only",
                },
            },
            _handler,
        )
        return registry.get_capability_catalog()["tools"][0]["schema_sha256"]

    base = digest(schema_id="https://example.test/a/", marker="base")
    assert base != digest(schema_id="https://example.test/b/", marker="base")
    for marker in ("deprecated", "read-only", "write-only"):
        assert base == digest(schema_id="https://example.test/a/", marker=marker)


def test_schema_digest_preserves_annotation_named_parameters():
    with_description_parameter = ToolRegistry()
    without_description_parameter = ToolRegistry()

    with_description_parameter.register(
        "sample",
        "local",
        {
            "name": "sample",
            "description": "ignored tool annotation",
            "parameters": {
                "type": "object",
                "properties": {
                    "description": {
                        "type": "string",
                        "description": "ignored property annotation",
                    }
                },
            },
        },
        _handler,
    )
    without_description_parameter.register(
        "sample",
        "local",
        {
            "name": "sample",
            "description": "different ignored annotation",
            "parameters": {"type": "object", "properties": {}},
        },
        _handler,
    )

    with_digest = with_description_parameter.get_capability_catalog()["tools"][0][
        "schema_sha256"
    ]
    without_digest = without_description_parameter.get_capability_catalog()["tools"][
        0
    ]["schema_sha256"]
    assert with_digest != without_digest


def test_schema_digest_ignores_annotations_for_named_map_colliding_parameters():
    def digest(parameter_name: str, marker: str) -> str:
        registry = ToolRegistry()
        registry.register(
            "sample",
            "local",
            {
                "name": "sample",
                "parameters": {
                    "type": "object",
                    "properties": {
                        parameter_name: {
                            "type": "string",
                            "description": f"ignored-{marker}",
                            "default": f"ignored-{marker}",
                        }
                    },
                },
            },
            _handler,
        )
        return registry.get_capability_catalog()["tools"][0]["schema_sha256"]

    for parameter_name in (
        "$defs",
        "definitions",
        "dependencies",
        "dependentRequired",
        "dependentSchemas",
        "patternProperties",
        "properties",
    ):
        assert digest(parameter_name, "one") == digest(parameter_name, "two")


def test_schema_digest_preserves_annotation_named_instance_data():
    def digest(constraint: dict) -> str:
        registry = ToolRegistry()
        registry.register(
            "sample",
            "local",
            {
                "name": "sample",
                "parameters": {
                    "type": "object",
                    "properties": {"value": constraint},
                },
            },
            _handler,
        )
        return registry.get_capability_catalog()["tools"][0]["schema_sha256"]

    assert digest({"const": {"description": "one"}}) != digest(
        {"const": {"description": "two"}}
    )
    assert digest({"enum": [{"default": "one"}]}) != digest(
        {"enum": [{"default": "two"}]}
    )
    assert digest(
        {"enum": [{"description": "one"}, {"default": "two"}]}
    ) == digest({"enum": [{"default": "two"}, {"description": "one"}]})


def test_schema_digest_ignores_type_array_order():
    def digest(types: list[str]) -> str:
        registry = ToolRegistry()
        registry.register(
            "sample",
            "local",
            {
                "name": "sample",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": types}},
                },
            },
            _handler,
        )
        return registry.get_capability_catalog()["tools"][0]["schema_sha256"]

    assert digest(["string", "null"]) == digest(["null", "string"])


def test_schema_digest_ignores_dependent_required_order():
    def digest(dependencies: list[str]) -> str:
        registry = ToolRegistry()
        registry.register(
            "sample",
            "local",
            {
                "name": "sample",
                "parameters": {
                    "type": "object",
                    "dependentRequired": {"credit_card": dependencies},
                },
            },
            _handler,
        )
        return registry.get_capability_catalog()["tools"][0]["schema_sha256"]

    assert digest(["billing_address", "security_code"]) == digest(
        ["security_code", "billing_address"]
    )


def test_schema_digest_ignores_legacy_dependency_order():
    def digest(dependencies: list[str]) -> str:
        registry = ToolRegistry()
        registry.register(
            "sample",
            "local",
            {
                "name": "sample",
                "parameters": {
                    "type": "object",
                    "dependencies": {"credit_card": dependencies},
                },
            },
            _handler,
        )
        return registry.get_capability_catalog()["tools"][0]["schema_sha256"]

    assert digest(["billing_address", "security_code"]) == digest(
        ["security_code", "billing_address"]
    )


def test_schema_digest_preserves_each_named_map_grammar():
    def digest(
        map_keyword: str,
        entry_name: str,
        marker: str,
        *,
        entry_type: str = "string",
    ) -> str:
        registry = ToolRegistry()
        if map_keyword == "dependentRequired":
            entry_value = ["sibling"]
        else:
            entry_value = {
                "type": entry_type,
                "description": f"ignored-{marker}",
                "default": f"ignored-{marker}",
            }
        registry.register(
            "sample",
            "local",
            {
                "name": "sample",
                "parameters": {
                    "type": "object",
                    map_keyword: {entry_name: entry_value},
                },
            },
            _handler,
        )
        return registry.get_capability_catalog()["tools"][0]["schema_sha256"]

    named_map_keywords = (
        "$defs",
        "definitions",
        "dependencies",
        "dependentRequired",
        "dependentSchemas",
        "patternProperties",
        "properties",
    )
    colliding_entry_names = ("description", "default", "properties", "$defs")
    for map_keyword in named_map_keywords:
        control = digest(map_keyword, "control", "one")
        for entry_name in colliding_entry_names:
            one = digest(map_keyword, entry_name, "one")
            assert control != one
            if map_keyword != "dependentRequired":
                assert one == digest(map_keyword, entry_name, "two")

    assert digest(
        "dependentSchemas", "description", "one", entry_type="string"
    ) != digest("dependentSchemas", "description", "one", entry_type="integer")


def test_probe_deduplicates_shared_check_and_contains_probe_failure(monkeypatch):
    registry = ToolRegistry()

    def shared_check():
        raise RuntimeError("credential at /Users/alice/.secrets/token")

    for name in ("alpha", "beta"):
        registry.register(
            name=name,
            toolset="network",
            schema=_schema(name),
            handler=_handler,
            check_fn=shared_check,
        )

    calls = []
    original_probe = registry_module._check_fn_cached

    def counting_probe(fn):
        calls.append(fn)
        return original_probe(fn)

    monkeypatch.setattr(registry_module, "_check_fn_cached", counting_probe)
    catalog = registry.get_capability_catalog(probe=True)

    assert calls == [shared_check]
    assert [tool["availability"] for tool in catalog["tools"]] == [
        "unavailable",
        "unavailable",
    ]
    assert all("probe_error" not in tool for tool in catalog["tools"])
    assert "/Users/alice" not in json.dumps(catalog)


def test_default_catalog_ignores_process_global_cached_check_results(monkeypatch):
    registry = ToolRegistry()

    def check():
        return True

    registry.register(
        name="dynamic",
        toolset="network",
        schema=_schema("dynamic"),
        handler=_handler,
        check_fn=check,
    )
    monkeypatch.setattr(
        registry_module,
        "get_cached_check_fn_result",
        lambda _fn: True,
    )

    [entry] = registry.get_capability_catalog()["tools"]
    assert entry["availability"] == "unknown"


def test_origin_classification_does_not_trust_partial_handler_metadata():
    registry = ToolRegistry()

    plugin_handler = FunctionType(
        (lambda prefix, **_kwargs: prefix).__code__,
        {"__name__": "hermes_plugins.example.tool"},
    )
    registry.register(
        name="partial_plugin",
        toolset="plugin_example",
        schema=_schema("partial_plugin"),
        handler=partial(plugin_handler, "ok"),
    )

    [entry] = registry.get_capability_catalog()["tools"]

    assert entry["origin"] == {"kind": "runtime", "id": "runtime"}


def test_origin_classification_does_not_trust_callable_object_metadata():
    registry = ToolRegistry()
    plugin_callable_type = type(
        "PluginCallable",
        (),
        {
            "__module__": "hermes_plugins.example.callables",
            "__call__": lambda self, **_kwargs: "ok",
        },
    )
    registry.register(
        name="callable_plugin",
        toolset="plugin_example",
        schema=_schema("callable_plugin"),
        handler=cast(Callable[..., object], plugin_callable_type()),
    )

    [entry] = registry.get_capability_catalog()["tools"]

    assert entry["origin"] == {"kind": "runtime", "id": "runtime"}


def test_origin_classes_do_not_expose_filesystem_paths(monkeypatch):
    registry = ToolRegistry()

    builtin_handler = _handler_in_module("tools.example")
    plugin_handler = _handler_in_module("hermes_plugins.example.actions")

    monkeypatch.setattr(registry, "_caller_module", lambda: "tools.example")
    registry.register("builtin", "file", _schema("builtin"), builtin_handler)
    monkeypatch.setattr(
        registry, "_caller_module", lambda: "hermes_plugins.example.actions"
    )
    registry.register("plugin", "custom", _schema("plugin"), plugin_handler)
    monkeypatch.setattr(registry, "_caller_module", lambda: "tools.mcp_tool")
    registry.register("trusted_mcp", "mcp-github", _schema("trusted_mcp"), _handler)
    monkeypatch.setattr(registry, "_caller_module", lambda: __name__)
    registry.register("forged_mcp", "mcp-attacker", _schema("forged_mcp"), _handler)

    by_name = {
        tool["name"]: tool for tool in registry.get_capability_catalog()["tools"]
    }
    assert by_name["builtin"]["origin"] == {"kind": "builtin", "id": "tools.example"}
    assert by_name["plugin"]["origin"] == {
        "kind": "plugin",
        "id": "hermes_plugins.example",
    }
    assert by_name["trusted_mcp"]["origin"] == {"kind": "mcp", "id": "github"}
    assert by_name["forged_mcp"]["origin"] == {
        "kind": "runtime",
        "id": "runtime",
    }


def test_origin_prefers_host_registration_owner_over_wrapped_builtin_handler(
    monkeypatch,
):
    registry = ToolRegistry()
    builtin_handler = _handler_in_module("tools.example")
    monkeypatch.setattr(registry, "_caller_module", lambda: "hermes_cli.plugins")
    registry.register(
        "plugin_wrapped_builtin",
        "plugin_example",
        _schema("plugin_wrapped_builtin"),
        builtin_handler,
        _registration_owner="hermes_plugins.example",
    )

    [entry] = registry.get_capability_catalog()["tools"]

    assert entry["origin"] == {
        "kind": "plugin",
        "id": "hermes_plugins.example",
    }


def test_plugin_cannot_forge_host_registration_owner(monkeypatch):
    registry = ToolRegistry()
    monkeypatch.setattr(
        registry, "_caller_module", lambda: "hermes_plugins.attacker.actions"
    )
    monkeypatch.setattr(registry, "current_scope_key", lambda: "global")
    registry.register(
        "forged",
        "custom",
        _schema("forged"),
        _handler,
        scope="global",
        _registration_owner="hermes_plugins.victim",
    )

    [entry] = registry.get_capability_catalog()["tools"]

    assert entry["origin"] == {
        "kind": "plugin",
        "id": "hermes_plugins.attacker",
    }


def test_catalog_redacts_unsafe_identifiers_without_leaking_source_text():
    registry = ToolRegistry()
    unsafe_handler = _handler_in_module("/Users/private/secret_module.py")
    registry.register(
        "/Users/private/API_KEY=secret-tool",
        "mcp-/Users/private/token=secret",
        _schema("unsafe"),
        unsafe_handler,
    )

    catalog = registry.get_capability_catalog()
    encoded = json.dumps(catalog, sort_keys=True)

    assert "/Users/private" not in encoded
    assert "API_KEY" not in encoded
    assert "token=secret" not in encoded
    [entry] = catalog["tools"]
    assert entry["name"].startswith("redacted-")
    assert entry["toolset"].startswith("redacted-")
    assert entry["origin"] == {"kind": "runtime", "id": "runtime"}


def test_catalog_redacts_credential_shaped_identifier_components():
    registry = ToolRegistry()
    temporary_access_key = "AS" + "IA" + "IOSFODNN7EXAMPLE"
    long_lived_access_key = "AK" + "IA" + "IOSFODNN7EXAMPLE"
    github_token = "gh" + "p_exampletoken"
    slack_token = "xo" + "xb-exampletoken"
    registry.register(
        f"tool-{github_token}",
        f"mcp-{temporary_access_key}",
        _schema("credential-shaped"),
        _handler,
    )
    registry.register(
        f"tool:{slack_token}",
        f"custom.{long_lived_access_key}",
        _schema("credential-shaped-two"),
        _handler,
    )

    catalog = registry.get_capability_catalog()
    encoded = json.dumps(catalog, sort_keys=True)

    for credential in (
        temporary_access_key,
        long_lived_access_key,
        github_token,
        slack_token,
    ):
        assert credential not in encoded
    assert all(tool["name"].startswith("redacted-") for tool in catalog["tools"])
    assert all(tool["toolset"].startswith("redacted-") for tool in catalog["tools"])


def test_read_only_discovery_does_not_persist_cache_or_log_exception_text(
    monkeypatch, caplog, tmp_path
):
    (tmp_path / "broken.py").write_text(
        'registry.register("broken", "file", {}, handler)\n',
        encoding="utf-8",
    )
    save_cache = Mock()
    monkeypatch.setattr(registry_module, "_load_discovery_cache", lambda: {})
    monkeypatch.setattr(registry_module, "_save_discovery_cache", save_cache)
    monkeypatch.setattr(
        registry_module.importlib,
        "import_module",
        Mock(side_effect=RuntimeError("/Users/private/API_KEY=secret")),
    )

    registry_module.discover_builtin_tools(tmp_path, read_only=True)

    save_cache.assert_not_called()
    assert "/Users/private" not in caplog.text
    assert "API_KEY" not in caplog.text


def test_catalog_respects_profile_scope_and_does_not_mutate_registry(monkeypatch):
    registry = ToolRegistry()
    dynamic_schema_calls = []

    def dynamic_schema():
        dynamic_schema_calls.append("called")
        return {"properties": {"secret": {"type": "string"}}}

    registry.register("global", "file", _schema("global"), _handler)
    registry.register(
        "profile_a_only",
        "custom",
        _schema("profile_a_only"),
        _handler,
        dynamic_schema_overrides=dynamic_schema,
        scope="profile-a",
    )
    registry.register(
        "profile_b_only",
        "custom",
        _schema("profile_b_only"),
        _handler,
        scope="profile-b",
    )

    generation_before = registry._generation
    monkeypatch.setattr(registry, "current_scope_key", lambda: "profile-a")
    profile_a = registry.get_capability_catalog()
    monkeypatch.setattr(registry, "current_scope_key", lambda: "profile-b")
    profile_b = registry.get_capability_catalog()

    assert [tool["name"] for tool in profile_a["tools"]] == [
        "global",
        "profile_a_only",
    ]
    assert [tool["name"] for tool in profile_b["tools"]] == [
        "global",
        "profile_b_only",
    ]
    assert registry._generation == generation_before
    assert dynamic_schema_calls == []


def test_catalog_keeps_concurrent_profile_snapshots_isolated(monkeypatch):
    registry = ToolRegistry()
    active_scope = ContextVar("catalog_test_scope", default="profile-a")
    registry.register("global", "file", _schema("global"), _handler)
    registry.register(
        "profile_a_only",
        "custom",
        _schema("profile_a_only"),
        _handler,
        scope="profile-a",
    )
    registry.register(
        "profile_b_only",
        "custom",
        _schema("profile_b_only"),
        _handler,
        scope="profile-b",
    )
    monkeypatch.setattr(registry, "current_scope_key", active_scope.get)

    def read_names(scope):
        token = active_scope.set(scope)
        try:
            return [tool["name"] for tool in registry.get_capability_catalog()["tools"]]
        finally:
            active_scope.reset(token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        profile_a = executor.submit(read_names, "profile-a")
        profile_b = executor.submit(read_names, "profile-b")

    assert profile_a.result() == ["global", "profile_a_only"]
    assert profile_b.result() == ["global", "profile_b_only"]
