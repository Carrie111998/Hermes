"""Fail-closed contract tests for host-authenticated gateway dispatch.

The authenticated context is host-owned state.  It is never model input, is
valid only while its issuing lease is live, and is separately advertised for
plugin commands and model tools.
"""

from __future__ import annotations

import copy
import json
from unittest.mock import patch

import pytest


def _lease(**overrides):
    from gateway.authenticated_dispatch import issue_authenticated_gateway_dispatch

    values = {
        "dispatch_kind": "tool",
        "platform": "telegram",
        "user_id": "user-1",
        "chat_id": "chat-1",
        "message_id": "message-1",
        "thread_id": None,
    }
    values.update(overrides)
    return issue_authenticated_gateway_dispatch(**values)


def _schema(name: str, properties: dict | None = None) -> dict:
    return {
        "name": name,
        "description": "authenticated test tool",
        "parameters": {
            "type": "object",
            "properties": properties or {"value": {"type": "string"}},
        },
    }


class TestSealedAuthenticatedGatewayDispatch:
    def test_direct_construction_is_rejected(self):
        from gateway.authenticated_dispatch import AuthenticatedGatewayDispatch

        with pytest.raises(
            TypeError,
            match="Authenticated gateway dispatch is host-issued only",
        ):
            AuthenticatedGatewayDispatch(
                platform="telegram",
                user_id="user-1",
                chat_id="chat-1",
                message_id="message-1",
                dispatch_kind="tool",
                thread_id=None,
            )

    def test_live_context_has_immutable_exact_host_fields(self):
        from gateway.authenticated_dispatch import validate_authenticated_gateway_dispatch

        with _lease(thread_id="thread-1") as context:
            assert validate_authenticated_gateway_dispatch(context) is True
            assert context.authorized is True
            assert context.platform == "telegram"
            assert context.user_id == "user-1"
            assert context.chat_id == "chat-1"
            assert context.message_id == "message-1"
            assert context.thread_id == "thread-1"
            assert type(context.provenance_version) is int
            assert context.provenance_version == 1
            assert type(context.turn_id) is str and context.turn_id
            with pytest.raises((AttributeError, TypeError)):
                context.platform = "forged"

        assert validate_authenticated_gateway_dispatch(context) is False

    def test_copy_and_lookalike_cannot_forge_a_live_record(self):
        from gateway.authenticated_dispatch import validate_authenticated_gateway_dispatch

        class Lookalike:
            authorized = True
            platform = "telegram"
            user_id = "user-1"
            chat_id = "chat-1"
            message_id = "message-1"
            thread_id = None
            provenance_version = 1
            turn_id = "forged"

        assert validate_authenticated_gateway_dispatch(Lookalike()) is False
        with _lease() as context:
            try:
                cloned = copy.copy(context)
            except TypeError:
                cloned = None
            assert cloned is None or validate_authenticated_gateway_dispatch(cloned) is False
            assert validate_authenticated_gateway_dispatch(context) is True

    def test_parallel_leases_are_isolated_and_revoked_independently(self):
        from gateway.authenticated_dispatch import validate_authenticated_gateway_dispatch

        with _lease(message_id="message-a") as first:
            with _lease(message_id="message-b") as second:
                assert first is not second
                assert first.turn_id != second.turn_id
                assert validate_authenticated_gateway_dispatch(first) is True
                assert validate_authenticated_gateway_dispatch(second) is True
            assert validate_authenticated_gateway_dispatch(first) is True
            assert validate_authenticated_gateway_dispatch(second) is False
        assert validate_authenticated_gateway_dispatch(first) is False

    @pytest.mark.parametrize(
        "field,value",
        [
            ("platform", ""),
            ("platform", True),
            ("user_id", ""),
            ("user_id", None),
            ("chat_id", ""),
            ("chat_id", 1),
            ("message_id", ""),
            ("message_id", False),
            ("thread_id", ""),
            ("thread_id", 1),
        ],
    )
    def test_invalid_source_fields_are_rejected(self, field, value):
        with pytest.raises((TypeError, ValueError)):
            with _lease(**{field: value}):
                pass


class TestGatewayInterruptRevocation:
    def test_interrupt_taints_turn_and_revokes_before_agent_mutation(self):
        import threading

        from gateway.authenticated_dispatch import (
            issue_authenticated_gateway_dispatch,
            validate_authenticated_gateway_tool_dispatch,
        )
        from gateway.run import GatewayRunner

        observations = []
        taint = threading.Event()

        class Agent:
            _authenticated_gateway_turn_tainted = taint

            def interrupt(self, reason):
                observations.append(
                    (
                        reason,
                        taint.is_set(),
                        validate_authenticated_gateway_tool_dispatch(
                            self._authenticated_gateway_context
                        ),
                    )
                )

        agent = Agent()
        with issue_authenticated_gateway_dispatch(
            dispatch_kind="tool",
            platform="telegram",
            user_id="user-1",
            chat_id="chat-1",
            message_id="message-1",
        ) as context:
            agent._authenticated_gateway_context = context
            GatewayRunner._interrupt_authenticated_gateway_turn(agent, "correction")

        assert observations == [("correction", True, False)]

    def test_preissuance_taint_permanently_denies_authority_for_that_turn(self):
        import threading

        from gateway.run import GatewayRunner

        class Agent:
            authenticated_gateway_tool_dispatch_version = 1

            def interrupt(self, _reason):
                pass

        agent = Agent()
        agent._authenticated_gateway_turn_tainted = threading.Event()
        GatewayRunner._interrupt_authenticated_gateway_turn(agent, "early correction")

        assert GatewayRunner._authenticated_gateway_request_for_turn(True, agent) is False

    def test_mutation_cannot_miss_a_lease_between_issue_and_publication(self, monkeypatch):
        import threading
        from contextlib import contextmanager
        from types import SimpleNamespace

        import gateway.authenticated_dispatch as dispatch

        lease_issued = threading.Event()
        publish_release = threading.Event()
        mutation_done = threading.Event()
        observations = []
        real_issue = dispatch.issue_authenticated_gateway_dispatch

        @contextmanager
        def delayed_issue(**kwargs):
            with real_issue(**kwargs) as context:
                lease_issued.set()
                assert publish_release.wait(timeout=2)
                yield context

        monkeypatch.setattr(
            dispatch,
            "issue_authenticated_gateway_dispatch",
            delayed_issue,
        )

        class Agent:
            authenticated_gateway_tool_dispatch_version = 1
            _authenticated_gateway_turn_tainted = threading.Event()

        agent = Agent()
        source = SimpleNamespace(
            platform="telegram",
            user_id="user-1",
            chat_id="chat-1",
            thread_id=None,
            message_id="message-1",
        )

        def run_turn():
            with dispatch.authenticated_gateway_turn(
                agent,
                source,
                authenticated_gateway_request=True,
                event_message_id="message-1",
            ) as context:
                assert mutation_done.wait(timeout=2)
                observations.append(
                    (
                        context is not None,
                        dispatch.validate_authenticated_gateway_tool_dispatch(context),
                        agent._authenticated_gateway_turn_tainted.is_set(),
                    )
                )

        turn_thread = threading.Thread(target=run_turn)
        turn_thread.start()
        assert lease_issued.wait(timeout=2)

        mutation_thread = threading.Thread(
            target=lambda: (
                dispatch.taint_and_revoke_authenticated_gateway_dispatch(agent),
                mutation_done.set(),
            )
        )
        mutation_thread.start()
        assert not mutation_done.wait(timeout=0.1)
        publish_release.set()

        turn_thread.join(timeout=3)
        mutation_thread.join(timeout=3)
        assert not turn_thread.is_alive()
        assert not mutation_thread.is_alive()
        assert observations == [(True, False, True)]


class TestAtomicLeaseUse:
    def test_failed_claim_releases_global_lock_before_yielding_denial(self):
        import threading

        from gateway.authenticated_dispatch import (
            issue_authenticated_gateway_dispatch,
            revoke_authenticated_gateway_dispatch,
            use_authenticated_gateway_tool_dispatch,
        )

        entered_denied_scope = threading.Event()
        release_denied_scope = threading.Event()
        second_issuance_finished = threading.Event()

        with issue_authenticated_gateway_dispatch(
            dispatch_kind="tool",
            platform="telegram",
            user_id="user-1",
            chat_id="chat-1",
            message_id="message-1",
        ) as context:
            assert revoke_authenticated_gateway_dispatch(context) is True

            def hold_denied_scope():
                with use_authenticated_gateway_tool_dispatch(
                    context, "protected"
                ) as claim:
                    assert claim is None
                    entered_denied_scope.set()
                    assert release_denied_scope.wait(timeout=2)

            denied_thread = threading.Thread(target=hold_denied_scope)
            denied_thread.start()
            assert entered_denied_scope.wait(timeout=1)

            def issue_second_lease():
                with issue_authenticated_gateway_dispatch(
                    dispatch_kind="tool",
                    platform="telegram",
                    user_id="user-2",
                    chat_id="chat-2",
                    message_id="message-2",
                ):
                    second_issuance_finished.set()

            issuance_thread = threading.Thread(target=issue_second_lease)
            issuance_thread.start()
            try:
                assert second_issuance_finished.wait(timeout=0.5)
            finally:
                release_denied_scope.set()
                denied_thread.join(timeout=2)
                issuance_thread.join(timeout=2)

            assert not denied_thread.is_alive()
            assert not issuance_thread.is_alive()

    def test_revocation_returns_immediately_and_blocks_later_entry(self):
        import threading

        from gateway.authenticated_dispatch import (
            issue_authenticated_gateway_dispatch,
            revoke_authenticated_gateway_dispatch,
        )
        from tools.registry import ToolRegistry

        handler_started = threading.Event()
        release_handler = threading.Event()
        revocation_finished = threading.Event()
        entered = []
        registry = ToolRegistry()

        def handler(args, *, tool_context):
            entered.append(args["value"])
            handler_started.set()
            assert release_handler.wait(timeout=2)
            return "ok"

        registry.register(
            name="protected",
            toolset="test",
            schema=_schema("protected"),
            handler=handler,
            requires_authenticated_gateway=True,
        )

        with issue_authenticated_gateway_dispatch(
            dispatch_kind="tool",
            platform="telegram",
            user_id="user-1",
            chat_id="chat-1",
            message_id="message-1",
        ) as context:
            dispatch_thread = threading.Thread(
                target=lambda: registry.dispatch(
                    "protected",
                    {"value": "first"},
                    authenticated_gateway_context=context,
                )
            )
            dispatch_thread.start()
            assert handler_started.wait(timeout=2)

            def revoke():
                revoke_authenticated_gateway_dispatch(context)
                revocation_finished.set()

            revoke_thread = threading.Thread(target=revoke)
            revoke_thread.start()
            assert revocation_finished.wait(timeout=1)

            release_handler.set()
            dispatch_thread.join(timeout=2)
            revoke_thread.join(timeout=2)
            assert revocation_finished.is_set()

            denied = json.loads(
                registry.dispatch(
                    "protected",
                    {"value": "second"},
                    authenticated_gateway_context=context,
                )
            )
            assert denied["error_type"] == "authenticated_gateway_required"

        assert entered == ["first"]

    def test_existing_claim_cannot_admit_a_new_call_after_revocation(self):
        from contextlib import ExitStack
        from types import SimpleNamespace

        from agent.tool_executor import _authenticated_gateway_tool_denial
        from gateway.authenticated_dispatch import (
            issue_authenticated_gateway_dispatch,
            revoke_authenticated_gateway_dispatch,
        )
        from tools.registry import registry

        name = "protected-no-claim-piggyback"
        registry.register(
            name=name,
            toolset="test",
            schema=_schema(name),
            handler=lambda args, **kwargs: "ok",
            requires_authenticated_gateway=True,
        )
        try:
            with issue_authenticated_gateway_dispatch(
                dispatch_kind="tool",
                platform="telegram",
                user_id="user-1",
                chat_id="chat-1",
                message_id="message-1",
            ) as context:
                agent = SimpleNamespace(_authenticated_gateway_context=context)
                with ExitStack() as claims:
                    assert _authenticated_gateway_tool_denial(
                        agent,
                        function_name=name,
                        function_args={},
                        lease_claims=claims,
                    ) is None
                    assert revoke_authenticated_gateway_dispatch(context) is True
                    denied = _authenticated_gateway_tool_denial(
                        agent,
                        function_name=name,
                        function_args={},
                        lease_claims=claims,
                    )
                    assert isinstance(denied, str)
                    assert json.loads(denied)["error_type"] == (
                        "authenticated_gateway_required"
                    )
        finally:
            registry.deregister(name)

    def test_admitted_claim_authorizes_only_one_matching_registry_dispatch(self):
        from gateway.authenticated_dispatch import (
            bind_authenticated_gateway_tool_claim,
            issue_authenticated_gateway_dispatch,
            revoke_authenticated_gateway_dispatch,
            use_authenticated_gateway_tool_dispatch,
        )
        from tools.registry import ToolRegistry

        local_registry = ToolRegistry()
        calls = []
        local_registry.register(
            name="protected-once",
            toolset="test",
            schema=_schema("protected-once"),
            handler=lambda args, **kwargs: calls.append(args["value"]) or "ok",
            requires_authenticated_gateway=True,
        )

        with issue_authenticated_gateway_dispatch(
            dispatch_kind="tool",
            platform="telegram",
            user_id="user-1",
            chat_id="chat-1",
            message_id="message-1",
        ) as context:
            with use_authenticated_gateway_tool_dispatch(
                context,
                "protected-once",
            ) as claim:
                assert claim is not None
                with bind_authenticated_gateway_tool_claim(claim):
                    assert local_registry.dispatch(
                        "protected-once",
                        {"value": "first"},
                        authenticated_gateway_context=context,
                    ) == "ok"
                    denied = local_registry.dispatch(
                        "protected-once",
                        {"value": "second"},
                        authenticated_gateway_context=context,
                    )

        assert isinstance(denied, str)
        assert json.loads(denied)["error_type"] == "authenticated_gateway_required"
        assert calls == ["first"]

    def test_nested_binding_exposes_only_the_innermost_call_claim(self):
        from gateway.authenticated_dispatch import (
            bind_authenticated_gateway_tool_claim,
            issue_authenticated_gateway_dispatch,
            revoke_authenticated_gateway_dispatch,
            use_authenticated_gateway_tool_dispatch,
        )
        from tools.registry import ToolRegistry

        local_registry = ToolRegistry()
        calls = []
        local_registry.register(
            name="protected-sibling",
            toolset="test",
            schema=_schema("protected-sibling"),
            handler=lambda args, **kwargs: calls.append(args["value"]) or "ok",
            requires_authenticated_gateway=True,
        )

        with issue_authenticated_gateway_dispatch(
            dispatch_kind="tool",
            platform="telegram",
            user_id="user-1",
            chat_id="chat-1",
            message_id="message-1",
        ) as context:
            with use_authenticated_gateway_tool_dispatch(
                context, "protected-sibling", tool_call_id="call-1"
            ) as first_claim, use_authenticated_gateway_tool_dispatch(
                context, "protected-sibling", tool_call_id="call-2"
            ) as second_claim:
                assert first_claim is not None
                assert second_claim is not None
                with bind_authenticated_gateway_tool_claim(
                    first_claim
                ), bind_authenticated_gateway_tool_claim(second_claim):
                    assert local_registry.dispatch(
                        "protected-sibling",
                        {"value": "second"},
                        authenticated_gateway_context=context,
                        authenticated_gateway_tool_call_id="call-2",
                    ) == "ok"
                    denied = local_registry.dispatch(
                        "protected-sibling",
                        {"value": "first-from-sibling"},
                        authenticated_gateway_context=context,
                        authenticated_gateway_tool_call_id="call-1",
                    )

        assert json.loads(denied)["error_type"] == "authenticated_gateway_required"
        assert calls == ["second"]


class TestSeparateCommandAndToolCapabilities:
    def test_command_and_tool_leases_are_not_interchangeable(self):
        from hermes_cli import plugins
        from tools.registry import ToolRegistry
        from gateway.authenticated_dispatch import issue_authenticated_gateway_dispatch

        command_seen = []
        tool_seen = []
        manager = plugins.PluginManager()
        manager._plugin_commands["protected-command"] = {
            "handler": lambda args, *, command_context: command_seen.append(
                command_context
            ) or "command-ok",
            "description": "protected command",
            "plugin": "test",
            "args_hint": "",
            "requires_authenticated_gateway": True,
        }
        registry = ToolRegistry()
        registry.register(
            name="protected-tool",
            toolset="test",
            schema=_schema("protected-tool"),
            handler=lambda args, *, tool_context, **kwargs: tool_seen.append(
                tool_context
            ) or "tool-ok",
            requires_authenticated_gateway=True,
        )
        identity = {
            "platform": "telegram",
            "user_id": "user-1",
            "chat_id": "chat-1",
            "message_id": "message-1",
        }

        with patch("hermes_cli.plugins._plugin_manager", manager):
            with issue_authenticated_gateway_dispatch(
                dispatch_kind="command", **identity
            ) as command_context:
                assert plugins.invoke_plugin_command(
                    "protected-command",
                    "x",
                    authenticated_gateway_context=command_context,
                ) == "command-ok"
                denied = json.loads(
                    registry.dispatch(
                        "protected-tool",
                        {"value": "x"},
                        authenticated_gateway_context=command_context,
                    )
                )
                assert denied["error_type"] == "authenticated_gateway_required"

            with issue_authenticated_gateway_dispatch(
                dispatch_kind="tool", **identity
            ) as tool_context:
                with pytest.raises(PermissionError):
                    plugins.invoke_plugin_command(
                        "protected-command",
                        "x",
                        authenticated_gateway_context=tool_context,
                    )
                assert registry.dispatch(
                    "protected-tool",
                    {"value": "x"},
                    authenticated_gateway_context=tool_context,
                ) == "tool-ok"

        assert command_seen == [command_context]
        assert tool_seen == [tool_context]


class TestToolContextCompatibility:
    def test_registry_forwards_legacy_tool_context_to_ordinary_handler(self):
        from tools.registry import ToolRegistry

        seen = []
        registry = ToolRegistry()
        registry.register(
            name="ordinary",
            toolset="test",
            schema=_schema("ordinary"),
            handler=lambda args, *, tool_context: seen.append(tool_context) or "ok",
        )

        assert registry.dispatch(
            "ordinary",
            {"value": "x"},
            tool_context="legacy-host-context",
        ) == "ok"
        assert seen == ["legacy-host-context"]

    def test_plugin_context_cannot_upgrade_legacy_context_for_protected_tool(self):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        from tools.registry import ToolRegistry

        entered = []
        registry = ToolRegistry()
        registry.register(
            name="protected",
            toolset="test",
            schema=_schema("protected"),
            handler=lambda args, *, tool_context: entered.append(tool_context) or "ok",
            requires_authenticated_gateway=True,
        )
        context = PluginContext(
            PluginManifest(name="test-plugin", source="user"),
            PluginManager(),
        )

        with patch("tools.registry.registry", registry):
            denied = json.loads(
                context.dispatch_tool(
                    "protected",
                    {"value": "x"},
                    tool_context="forged-legacy-context",
                )
            )

        assert denied["error_type"] == "authenticated_gateway_required"
        assert entered == []


class TestProtectedPluginCommandContract:
    def _context(self):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest

        manager = PluginManager()
        context = PluginContext(
            PluginManifest(name="authenticated-test", source="user"), manager
        )
        return manager, context

    def test_command_and_tool_capabilities_are_separate_exact_integers(self):
        _, context = self._context()

        assert type(context.authenticated_gateway_dispatch_version) is int
        assert context.authenticated_gateway_dispatch_version == 1
        assert type(context.authenticated_gateway_tool_dispatch_version) is int
        assert context.authenticated_gateway_tool_dispatch_version == 1

    def test_protected_command_metadata_is_registered(self):
        manager, context = self._context()
        context.register_command(
            "protected",
            lambda _args, *, command_context=None: "ok",
            requires_authenticated_gateway=True,
        )

        assert manager._plugin_commands["protected"]["requires_authenticated_gateway"] is True

    def test_unprotected_command_keeps_legacy_one_argument_call(self):
        from hermes_cli.plugins import invoke_plugin_command

        manager, context = self._context()
        seen = []
        context.register_command("legacy", lambda args: seen.append(args) or "ok")

        with patch("hermes_cli.plugins._plugin_manager", manager):
            assert invoke_plugin_command("legacy", "hello") == "ok"
        assert seen == ["hello"]

    def test_absent_forged_and_stale_contexts_do_not_enter_handler(self):
        from hermes_cli.plugins import invoke_plugin_command

        manager, context = self._context()
        seen = []
        context.register_command(
            "protected",
            lambda args, *, command_context=None: seen.append((args, command_context)) or "ok",
            requires_authenticated_gateway=True,
        )

        class Forged:
            authorized = True

        with patch("hermes_cli.plugins._plugin_manager", manager):
            with pytest.raises(PermissionError):
                invoke_plugin_command("protected", "x")
            with pytest.raises(PermissionError):
                invoke_plugin_command(
                    "protected", "x", authenticated_gateway_context=Forged()
                )
            with _lease(dispatch_kind="command") as live:
                assert invoke_plugin_command(
                    "protected", "x", authenticated_gateway_context=live
                ) == "ok"
                assert seen == [("x", live)]
            with pytest.raises(PermissionError):
                invoke_plugin_command(
                    "protected", "x", authenticated_gateway_context=live
                )

        assert seen == [("x", live)]

    def test_legacy_handler_lookup_wraps_protected_commands_fail_closed(self):
        from hermes_cli.plugins import get_plugin_command_handler

        manager, context = self._context()
        entered = []
        context.register_command(
            "protected",
            lambda _args, *, command_context=None: entered.append(command_context) or "ok",
            requires_authenticated_gateway=True,
        )

        with patch("hermes_cli.plugins._plugin_manager", manager):
            handler = get_plugin_command_handler("protected")
            assert handler is not None
            with pytest.raises(PermissionError):
                handler("forged local/TUI invocation")
        assert entered == []


class TestAuthenticatedPluginToolContract:
    def test_reserved_provenance_fields_are_rejected_recursively_at_registration(self):
        from tools.registry import ToolRegistry

        reserved_schemas = [
            _schema("protected_top_tool", {"tool_context": {"type": "object"}}),
            _schema(
                "protected_top_auth",
                {"authenticated_gateway_context": {"type": "object"}},
            ),
            _schema(
                "protected_nested_object",
                {
                    "request": {
                        "type": "object",
                        "properties": {"tool_context": {"type": "object"}},
                    }
                },
            ),
            _schema(
                "protected_array_item",
                {
                    "requests": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "authenticated_gateway_context": {"type": "object"}
                            },
                        },
                    }
                },
            ),
            {
                "name": "protected_combinator",
                "description": "authenticated test tool",
                "parameters": {
                    "allOf": [
                        {
                            "type": "object",
                            "properties": {"tool_context": {"type": "string"}},
                        }
                    ]
                },
            },
        ]

        for index, schema in enumerate(reserved_schemas):
            registry = ToolRegistry()
            with pytest.raises(ValueError):
                registry.register(
                    name=f"protected_{index}",
                    toolset="plugin-test",
                    schema=schema,
                    handler=lambda _args, **_kwargs: "ok",
                    requires_authenticated_gateway=True,
                )

    def test_dynamic_schema_overrides_cannot_expose_reserved_provenance_fields(self):
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(
            name="protected_dynamic",
            toolset="plugin-test",
            schema=_schema("protected_dynamic"),
            handler=lambda _args, **_kwargs: "ok",
            dynamic_schema_overrides=lambda: {
                "parameters": {
                    "type": "object",
                    "properties": {"tool_context": {"type": "object"}},
                }
            },
            requires_authenticated_gateway=True,
        )

        assert registry.get_definitions({"protected_dynamic"}) == []

    def test_domain_identity_fields_remain_valid_protected_tool_arguments(self):
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        seen = []
        registry.register(
            name="protected_domain_fields",
            toolset="plugin-test",
            schema=_schema(
                "protected_domain_fields",
                {
                    "platform": {"type": "string"},
                    "request": {
                        "type": "object",
                        "properties": {"message_id": {"type": "string"}},
                    },
                },
            ),
            handler=lambda args, **kwargs: seen.append(args) or "ok",
            requires_authenticated_gateway=True,
        )
        args = {
            "platform": "business-domain",
            "request": {"message_id": "domain-record"},
        }
        with _lease() as live:
            assert registry.dispatch(
                "protected_domain_fields",
                args,
                authenticated_gateway_context=live,
            ) == "ok"
        assert seen == [args]

    def test_unprotected_tools_may_use_transport_looking_domain_fields(self):
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        seen = []
        registry.register(
            name="ordinary_domain_fields",
            toolset="plugin-test",
            schema=_schema(
                "ordinary_domain_fields",
                {"tool_context": {"type": "string"}},
            ),
            handler=lambda args, **kwargs: seen.append(args) or "ok",
        )
        args = {"tool_context": "ordinary-domain-value"}
        assert registry.dispatch("ordinary_domain_fields", args) == "ok"
        assert seen == [args]

    def test_unprotected_registry_dispatch_forwards_legacy_tool_context_kwarg(self):
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        seen = []
        registry.register(
            name="ordinary_legacy_context",
            toolset="plugin-test",
            schema=_schema("ordinary_legacy_context"),
            handler=lambda args, **kwargs: seen.append(kwargs) or "ok",
        )

        assert registry.dispatch(
            "ordinary_legacy_context",
            {},
            tool_context="legacy-host-context",
        ) == "ok"
        assert seen == [{"tool_context": "legacy-host-context"}]

    def test_absent_forged_stale_and_model_supplied_context_do_not_enter_handler(self):
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        seen = []
        registry.register(
            name="protected",
            toolset="plugin-test",
            schema=_schema("protected"),
            handler=lambda args, **kwargs: seen.append((args, kwargs)) or json.dumps({"ok": True}),
            requires_authenticated_gateway=True,
        )

        class Forged:
            authorized = True

        denied = json.loads(registry.dispatch("protected", {"value": "x"}))
        assert denied["error_type"] == "authenticated_gateway_required"
        denied = json.loads(
            registry.dispatch(
                "protected",
                {"value": "x"},
                authenticated_gateway_context=Forged(),
            )
        )
        assert denied["error_type"] == "authenticated_gateway_required"
        denied = json.loads(
            registry.dispatch(
                "protected",
                {"value": "x", "platform": "telegram"},
            )
        )
        assert denied["error_type"] == "authenticated_gateway_required"

        with _lease() as live:
            result = json.loads(
                registry.dispatch(
                    "protected",
                    {"value": "x"},
                    authenticated_gateway_context=live,
                )
            )
            assert result == {"ok": True}
            args, kwargs = seen[-1]
            assert args == {"value": "x"}
            assert kwargs == {"tool_context": live}

        denied = json.loads(
            registry.dispatch(
                "protected",
                {"value": "x"},
                authenticated_gateway_context=live,
            )
        )
        assert denied["error_type"] == "authenticated_gateway_required"
        assert len(seen) == 1

    def test_unprotected_tool_never_receives_internal_gateway_context(self):
        from tools.registry import ToolRegistry

        registry = ToolRegistry()
        seen = []
        registry.register(
            name="legacy",
            toolset="plugin-test",
            schema=_schema("legacy"),
            handler=lambda args, **kwargs: seen.append((args, kwargs)) or "ok",
        )

        with _lease() as live:
            assert registry.dispatch(
                "legacy", {"value": "x"}, authenticated_gateway_context=live
            ) == "ok"
        assert seen == [({"value": "x"}, {})]

    def test_plugin_context_dispatch_cannot_forward_a_gateway_lease(self):
        from hermes_cli.plugins import PluginContext, PluginManager, PluginManifest
        from tools.registry import registry

        name = "test_plugin_context_protected_dispatch"
        seen = []
        registry.register(
            name=name,
            toolset="plugin-test",
            schema=_schema(name),
            handler=lambda args, **kwargs: seen.append((args, kwargs)) or "ok",
            requires_authenticated_gateway=True,
        )
        try:
            context = PluginContext(
                PluginManifest(name="authenticated-test", source="user"),
                PluginManager(),
            )
            with _lease() as live:
                denied = json.loads(
                    context.dispatch_tool(
                        name,
                        {"value": "x"},
                        authenticated_gateway_context=live,
                    )
                )
            assert denied["error_type"] == "authenticated_gateway_required"
            assert seen == []
        finally:
            registry.deregister(name)


def test_api_tool_surface_requires_live_tool_lease():
    from types import SimpleNamespace

    from agent.chat_completion_helpers import build_api_kwargs
    from tools.registry import registry

    protected = "test_surface_protected"
    ordinary = "test_surface_ordinary"
    registry.register(
        name=protected,
        toolset="plugin-test",
        schema=_schema(protected),
        handler=lambda args, **kwargs: "protected",
        requires_authenticated_gateway=True,
    )
    registry.register(
        name=ordinary,
        toolset="plugin-test",
        schema=_schema(ordinary),
        handler=lambda args, **kwargs: "ordinary",
    )
    try:
        definitions = [
            {"type": "function", "function": _schema(protected)},
            {"type": "function", "function": _schema(ordinary)},
        ]

        class CaptureTransport:
            def build_kwargs(self, **kwargs):
                return kwargs

        agent = SimpleNamespace(
            tools=definitions,
            _authenticated_gateway_context=None,
            api_mode="bedrock_converse",
            model="test-model",
            max_tokens=100,
            _bedrock_region=None,
            _bedrock_guardrail_config=None,
            _get_transport=lambda: CaptureTransport(),
        )

        def exposed_names():
            payload = build_api_kwargs(agent, [{"role": "user", "content": "hi"}])
            return [item["function"]["name"] for item in payload["tools"]]

        assert exposed_names() == [ordinary]
        with _lease(dispatch_kind="command") as command_context:
            agent._authenticated_gateway_context = command_context
            assert exposed_names() == [ordinary]
        with _lease() as tool_context:
            agent._authenticated_gateway_context = tool_context
            assert exposed_names() == [protected, ordinary]
        assert exposed_names() == [ordinary]
        assert agent.tools is definitions
    finally:
        registry.deregister(protected)
        registry.deregister(ordinary)


def test_tool_search_catalog_requires_live_tool_lease():
    import model_tools
    from tools.registry import registry

    protected = "test_surface_catalog_protected"
    registry.register(
        name=protected,
        toolset="plugin-test",
        schema=_schema(protected),
        handler=lambda args, **kwargs: "protected",
        requires_authenticated_gateway=True,
    )
    try:
        def search(context=None):
            return json.loads(
                model_tools.handle_function_call(
                    "tool_search",
                    {"query": protected},
                    enabled_toolsets=["plugin-test"],
                    skip_pre_tool_call_hook=True,
                    skip_tool_request_middleware=True,
                    authenticated_gateway_context=context,
                )
            )

        assert search()["matches"] == []
        with _lease(dispatch_kind="command") as command_context:
            assert search(command_context)["matches"] == []
        with _lease() as tool_context:
            assert [hit["name"] for hit in search(tool_context)["matches"]] == [
                protected
            ]
    finally:
        registry.deregister(protected)


def test_model_dispatch_denies_before_middleware_and_accepts_live_lease(monkeypatch):
    import model_tools
    from agent.agent_runtime_helpers import invoke_tool
    from hermes_cli import middleware
    from tools.registry import registry

    name = "test_protected_model_dispatch"
    seen = []

    def handler(args, *, tool_context, **kwargs):
        seen.append(tool_context)
        return args["value"]

    registry.register(
        name=name,
        toolset="plugin-test",
        schema={
            "name": name,
            "description": "test",
            "parameters": {
                "type": "object",
                "properties": {"value": {"type": "string"}},
            },
        },
        handler=handler,
        requires_authenticated_gateway=True,
    )
    try:
        def middleware_must_not_run(*args, **kwargs):
            raise AssertionError("unauthorized protected call reached middleware")

        monkeypatch.setattr(
            middleware,
            "apply_tool_request_middleware",
            middleware_must_not_run,
        )
        denied = json.loads(model_tools.handle_function_call(name, {"value": "denied"}))
        assert denied["error_type"] == "authenticated_gateway_required"
        denied = json.loads(
            invoke_tool(
                object(),
                name,
                {"value": "denied"},
                "task",
            )
        )
        assert denied["error_type"] == "authenticated_gateway_required"
        assert seen == []

        from types import SimpleNamespace

        with _lease() as live:
            agent = SimpleNamespace(
                _authenticated_gateway_context=live,
                _memory_manager=None,
                session_id="session",
                valid_tool_names=None,
                enabled_toolsets=None,
                disabled_toolsets=None,
            )
            assert invoke_tool(
                agent,
                name,
                {"value": "accepted"},
                "task",
                pre_tool_block_checked=True,
                skip_tool_request_middleware=True,
            ) == "accepted"
            assert seen == [live]
    finally:
        registry.deregister(name)


@pytest.mark.parametrize("mode", ["sequential", "concurrent"])
def test_executor_denies_protected_tool_before_all_extension_observers(
    monkeypatch,
    mode,
):
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent import tool_executor
    from tools.registry import registry

    name = f"test_executor_protected_{mode}"
    observed = []
    handler_calls = []
    registry.register(
        name=name,
        toolset="plugin-test",
        schema=_schema(name),
        handler=lambda args, **kwargs: handler_calls.append((args, kwargs)) or "ok",
        requires_authenticated_gateway=True,
    )
    try:
        def request_middleware(_agent, **kwargs):
            observed.append("request_middleware")
            return kwargs["function_args"], []

        monkeypatch.setattr(
            tool_executor,
            "_apply_tool_request_middleware_for_agent",
            request_middleware,
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.resolve_pre_tool_block",
            lambda *args, **kwargs: observed.append("pre_tool_hook"),
        )
        monkeypatch.setattr(
            tool_executor,
            "_emit_terminal_post_tool_call",
            lambda *args, **kwargs: observed.append("post_tool_hook"),
        )
        monkeypatch.setattr(
            tool_executor,
            "maybe_persist_tool_result",
            lambda *, content, **kwargs: observed.append("persist_result") or content,
        )

        agent = MagicMock()
        agent._authenticated_gateway_context = None
        agent._interrupt_requested = False
        agent._tool_guardrails.before_call.side_effect = lambda *args: (
            observed.append("guardrail")
            or SimpleNamespace(allows_execution=True)
        )
        agent._checkpoint_mgr.enabled = False
        agent.tool_delay = 0
        agent.quiet_mode = True
        agent.tool_progress_mode = "off"
        agent.verbose_logging = False
        agent.tool_progress_callback = lambda *args, **kwargs: observed.append(
            "progress_callback"
        )
        agent.tool_start_callback = lambda *args, **kwargs: observed.append(
            "start_callback"
        )
        agent.tool_complete_callback = lambda *args, **kwargs: observed.append(
            "complete_callback"
        )
        agent._should_emit_quiet_tool_messages.return_value = False
        agent._should_start_quiet_spinner.return_value = False
        agent._tool_result_content_for_active_model.side_effect = (
            lambda _name, result: result
        )
        agent._touch_activity.side_effect = lambda *args, **kwargs: observed.append(
            "activity"
        )
        agent._append_guardrail_observation.side_effect = (
            lambda *args, **kwargs: observed.append("guardrail_observation")
            or args[2]
        )
        agent._subdirectory_hints.check_tool_call.side_effect = (
            lambda *args, **kwargs: observed.append("subdirectory_hints")
        )
        agent._tool_worker_threads = set()
        agent._tool_worker_threads_lock = threading.Lock()
        agent._invoke_tool.side_effect = lambda tool_name, args, *rest, **kwargs: (
            registry.dispatch(tool_name, args)
        )

        tool_call = SimpleNamespace(
            id=f"call-{mode}",
            function=SimpleNamespace(name=name, arguments='{"value": "denied"}'),
        )
        assistant_message = SimpleNamespace(tool_calls=[tool_call])
        messages = []

        if mode == "sequential":
            tool_executor.execute_tool_calls_sequential(
                agent,
                assistant_message,
                messages,
                "task",
                finalize=False,
            )
        else:
            tool_executor.execute_tool_calls_concurrent(
                agent,
                assistant_message,
                messages,
                "task",
                finalize=False,
            )

        assert observed == []
        assert handler_calls == []
        assert len(messages) == 1
        denial = json.loads(messages[0]["content"])
        assert denial["error_type"] == "authenticated_gateway_required"
    finally:
        registry.deregister(name)


@pytest.mark.parametrize("mode", ["sequential", "concurrent"])
def test_protected_executor_claims_lease_before_request_middleware(
    monkeypatch,
    mode,
):
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent import tool_executor
    from gateway.authenticated_dispatch import revoke_authenticated_gateway_dispatch
    from tools.registry import registry

    name = f"test_pipeline_claim_{mode}"
    middleware_entered = threading.Event()
    release_middleware = threading.Event()
    revocation_finished = threading.Event()
    handler_calls = []
    registry.register(
        name=name,
        toolset="plugin-test",
        schema=_schema(name),
        handler=lambda args, **kwargs: handler_calls.append(args["value"]) or "ok",
        requires_authenticated_gateway=True,
    )
    try:
        def blocking_middleware(_agent, **kwargs):
            middleware_entered.set()
            assert release_middleware.wait(timeout=2)
            return kwargs["function_args"], []

        monkeypatch.setattr(
            tool_executor,
            "_apply_tool_request_middleware_for_agent",
            blocking_middleware,
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.resolve_pre_tool_block",
            lambda *args, **kwargs: None,
        )

        agent = MagicMock()
        agent._interrupt_requested = False
        agent._tool_guardrails.before_call.return_value = SimpleNamespace(
            allows_execution=True
        )
        agent._checkpoint_mgr.enabled = False
        agent._memory_manager = None
        agent.tool_delay = 0
        agent.quiet_mode = True
        agent.tool_progress_mode = "off"
        agent.verbose_logging = False
        agent.tool_progress_callback = None
        agent.tool_start_callback = None
        agent.tool_complete_callback = None
        agent._should_emit_quiet_tool_messages.return_value = False
        agent._should_start_quiet_spinner.return_value = False
        agent._tool_result_content_for_active_model.side_effect = (
            lambda _name, result: result
        )
        agent._subdirectory_hints.check_tool_call.return_value = None
        agent._append_guardrail_observation.side_effect = (
            lambda _name, _args, result, failed=False: result
        )
        agent._invoke_tool.side_effect = lambda tool_name, args, *rest, **kwargs: (
            registry.dispatch(
                tool_name,
                args,
                authenticated_gateway_context=agent._authenticated_gateway_context,
                authenticated_gateway_tool_call_id=rest[1],
            )
        )
        agent._tool_worker_threads = set()
        agent._tool_worker_threads_lock = threading.Lock()

        tool_call = SimpleNamespace(
            id=f"call-{mode}",
            function=SimpleNamespace(name=name, arguments='{"value": "accepted"}'),
        )
        assistant_message = SimpleNamespace(tool_calls=[tool_call])
        messages = []

        with _lease() as live:
            agent._authenticated_gateway_context = live
            executor = (
                tool_executor.execute_tool_calls_sequential
                if mode == "sequential"
                else tool_executor.execute_tool_calls_concurrent
            )
            execution_thread = threading.Thread(
                target=executor,
                args=(agent, assistant_message, messages, "task"),
                kwargs={"finalize": False},
            )
            execution_thread.start()
            assert middleware_entered.wait(timeout=2)

            def revoke():
                revoke_authenticated_gateway_dispatch(live)
                revocation_finished.set()

            revoker = threading.Thread(target=revoke)
            revoker.start()
            # Revocation closes admission immediately and must not block the
            # gateway control path while an already-admitted handler drains.
            assert revocation_finished.wait(timeout=1)
            release_middleware.set()
            execution_thread.join(timeout=3)
            revoker.join(timeout=3)

            assert not execution_thread.is_alive()
            assert not revoker.is_alive()
            assert revocation_finished.is_set()
            assert handler_calls == ["accepted"], messages
            assert messages[-1]["content"] == "ok"
    finally:
        release_middleware.set()
        registry.deregister(name)


def test_concurrent_partial_submission_failure_never_waits_for_started_worker(
    monkeypatch,
):
    import concurrent.futures
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent import tool_executor
    from tools.registry import registry

    names = ["test-submit-failure-1", "test-submit-failure-2"]
    for name in names:
        registry.register(
            name=name,
            toolset="plugin-test",
            schema=_schema(name),
            handler=lambda args, **kwargs: "ok",
            requires_authenticated_gateway=True,
        )

    shutdown_calls = []

    class PartialFailureExecutor:
        def __init__(self, *, max_workers):
            self.submissions = 0

        def submit(self, fn, *args, **kwargs):
            self.submissions += 1
            if self.submissions == 1:
                return concurrent.futures.Future()
            raise RuntimeError("synthetic partial submission failure")

        def shutdown(self, *, wait, cancel_futures):
            shutdown_calls.append((wait, cancel_futures))

    try:
        monkeypatch.setattr(
            "tools.daemon_pool.DaemonThreadPoolExecutor",
            PartialFailureExecutor,
        )
        monkeypatch.setattr(
            tool_executor,
            "_apply_tool_request_middleware_for_agent",
            lambda _agent, **kwargs: (kwargs["function_args"], []),
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.resolve_pre_tool_block",
            lambda *args, **kwargs: None,
        )

        agent = MagicMock()
        agent._interrupt_requested = False
        agent._tool_guardrails.before_call.return_value = SimpleNamespace(
            allows_execution=True
        )
        agent._checkpoint_mgr.enabled = False
        agent._memory_manager = None
        agent.tool_delay = 0
        agent.quiet_mode = True
        agent.tool_progress_mode = "off"
        agent.verbose_logging = False
        agent.tool_progress_callback = None
        agent.tool_start_callback = None
        agent.tool_complete_callback = None
        agent._should_emit_quiet_tool_messages.return_value = False
        agent._should_start_quiet_spinner.return_value = False
        agent._subdirectory_hints.check_tool_call.return_value = None
        agent._tool_worker_threads = set()
        agent._tool_worker_threads_lock = threading.Lock()

        tool_calls = [
            SimpleNamespace(
                id=f"call-{index}",
                function=SimpleNamespace(name=name, arguments="{}"),
            )
            for index, name in enumerate(names)
        ]
        assistant_message = SimpleNamespace(tool_calls=tool_calls)

        with _lease() as live:
            agent._authenticated_gateway_context = live
            with pytest.raises(
                RuntimeError,
                match="synthetic partial submission failure",
            ):
                tool_executor.execute_tool_calls_concurrent(
                    agent,
                    assistant_message,
                    [],
                    "task",
                    finalize=False,
                )

        assert shutdown_calls == [(False, True)]
    finally:
        for name in names:
            registry.deregister(name)


def test_sequential_policy_block_closes_claim_before_block_callback(monkeypatch):
    import threading
    from contextlib import contextmanager
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent import tool_executor
    from gateway import authenticated_dispatch
    from tools.registry import registry

    name = "test-sequential-blocked-claim-cleanup"
    registry.register(
        name=name,
        toolset="plugin-test",
        schema=_schema(name),
        handler=lambda args, **kwargs: "unexpected",
        requires_authenticated_gateway=True,
    )
    claims = []
    callback_active_states = []
    original_use = authenticated_dispatch.use_authenticated_gateway_tool_dispatch

    @contextmanager
    def tracking_use(*args, **kwargs):
        with original_use(*args, **kwargs) as claim:
            claims.append(claim)
            yield claim

    try:
        monkeypatch.setattr(
            authenticated_dispatch,
            "use_authenticated_gateway_tool_dispatch",
            tracking_use,
        )
        monkeypatch.setattr(
            tool_executor,
            "_apply_tool_request_middleware_for_agent",
            lambda _agent, **kwargs: (kwargs["function_args"], []),
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.resolve_pre_tool_block",
            lambda *args, **kwargs: "blocked by policy",
        )
        monkeypatch.setattr(
            tool_executor,
            "_emit_terminal_post_tool_call",
            lambda *args, **kwargs: callback_active_states.append(claims[-1].active),
        )

        agent = MagicMock()
        agent._interrupt_requested = False
        agent._tool_guardrails.before_call.return_value = SimpleNamespace(
            allows_execution=True
        )
        agent._checkpoint_mgr.enabled = False
        agent._memory_manager = None
        agent.tool_delay = 0
        agent.quiet_mode = True
        agent.tool_progress_mode = "off"
        agent.verbose_logging = False
        agent.tool_progress_callback = None
        agent.tool_start_callback = None
        agent.tool_complete_callback = None
        agent._should_emit_quiet_tool_messages.return_value = False
        agent._should_start_quiet_spinner.return_value = False
        agent._tool_result_content_for_active_model.side_effect = (
            lambda _name, result: result
        )
        agent._append_guardrail_observation.side_effect = (
            lambda _name, _args, result, failed=False: result
        )
        agent._subdirectory_hints.check_tool_call.return_value = None
        agent._tool_worker_threads = set()
        agent._tool_worker_threads_lock = threading.Lock()

        tool_call = SimpleNamespace(
            id="call-blocked",
            function=SimpleNamespace(name=name, arguments="{}"),
        )
        with _lease() as live:
            agent._authenticated_gateway_context = live
            tool_executor.execute_tool_calls_sequential(
                agent,
                SimpleNamespace(tool_calls=[tool_call]),
                [],
                "task",
                finalize=False,
            )

        assert callback_active_states == [False]
        assert claims[0].active is False
    finally:
        registry.deregister(name)


def test_agent_turn_binds_restores_rejects_stale_and_excludes_codex(monkeypatch):
    from contextlib import contextmanager

    import agent.aux_accounting as aux_accounting
    import agent.auxiliary_client as auxiliary_client
    import agent.conversation_loop as conversation_loop
    import agent.portal_tags as portal_tags
    from run_agent import AIAgent

    observed = []

    def fake_run(agent, *args, **kwargs):
        observed.append(getattr(agent, "_authenticated_gateway_context", "missing"))
        return {"final_response": "ok"}

    @contextmanager
    def fake_scope(value):
        yield

    monkeypatch.setattr(conversation_loop, "run_conversation", fake_run)
    monkeypatch.setattr(aux_accounting, "set_accounting_context", lambda *a: object())
    monkeypatch.setattr(aux_accounting, "reset_accounting_context", lambda token: None)
    monkeypatch.setattr(portal_tags, "set_conversation_context", lambda value: object())
    monkeypatch.setattr(portal_tags, "reset_conversation_context", lambda token: None)
    monkeypatch.setattr(auxiliary_client, "scoped_runtime_main", fake_scope)

    class DummyAgent:
        api_mode = "openai"
        _session_db = None
        session_id = "session"
        _authenticated_gateway_context = "prior"

        @staticmethod
        def _conversation_root_id():
            return "root"

    agent = DummyAgent()
    with _lease() as live:
        assert AIAgent.run_conversation(
            agent,
            "hello",
            authenticated_gateway_context=live,
        ) == {"final_response": "ok"}
        assert observed[-1] is live
        assert agent._authenticated_gateway_context == "prior"

    with pytest.raises(PermissionError):
        AIAgent.run_conversation(
            agent,
            "stale",
            authenticated_gateway_context=live,
        )
    assert agent._authenticated_gateway_context == "prior"

    def fail_turn_setup(value):
        raise RuntimeError("setup failed")

    monkeypatch.setattr(portal_tags, "set_conversation_context", fail_turn_setup)
    with _lease() as setup_live:
        with pytest.raises(RuntimeError, match="setup failed"):
            AIAgent.run_conversation(
                agent,
                "setup failure",
                authenticated_gateway_context=setup_live,
            )
    assert agent._authenticated_gateway_context == "prior"
    monkeypatch.setattr(portal_tags, "set_conversation_context", lambda value: object())

    agent.api_mode = "codex_app_server"
    with _lease() as codex_live:
        AIAgent.run_conversation(
            agent,
            "codex",
            authenticated_gateway_context=codex_live,
        )
    assert observed[-1] is None
    assert agent._authenticated_gateway_context == "prior"


@pytest.mark.parametrize("mode", ["sequential", "concurrent"])
def test_executor_forwards_live_context_after_middleware(monkeypatch, mode):
    import threading
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from agent import tool_executor
    from tools.registry import registry

    name = f"test_executor_live_{mode}"
    middleware_calls = []
    handler_calls = []

    def handler(args, *, tool_context, **kwargs):
        handler_calls.append((args, tool_context))
        return args["value"]

    registry.register(
        name=name,
        toolset="plugin-test",
        schema=_schema(name, {"value": {"type": "string"}}),
        handler=handler,
        requires_authenticated_gateway=True,
    )
    try:
        def request_middleware(_agent, **kwargs):
            middleware_calls.append((kwargs["function_name"], kwargs["function_args"]))
            return {"value": "after-middleware"}, [
                {"plugin": "test", "middleware": "rewrite"}
            ]

        monkeypatch.setattr(
            tool_executor,
            "_apply_tool_request_middleware_for_agent",
            request_middleware,
        )
        monkeypatch.setattr(
            "hermes_cli.plugins.resolve_pre_tool_block",
            lambda *args, **kwargs: None,
        )

        agent = MagicMock()
        agent._interrupt_requested = False
        agent._tool_guardrails.before_call.return_value = SimpleNamespace(
            allows_execution=True
        )
        agent._checkpoint_mgr.enabled = False
        agent._memory_manager = None
        agent.tool_delay = 0
        agent.quiet_mode = True
        agent.tool_progress_mode = "off"
        agent.verbose_logging = False
        agent.tool_progress_callback = None
        agent.tool_start_callback = None
        agent.tool_complete_callback = None
        agent._should_emit_quiet_tool_messages.return_value = False
        agent._should_start_quiet_spinner.return_value = False
        agent._append_guardrail_observation.side_effect = (
            lambda _name, _args, result, **kwargs: result
        )
        agent._tool_result_content_for_active_model.side_effect = (
            lambda _name, result: result
        )
        agent._subdirectory_hints.check_tool_call.return_value = None
        agent._context_engine_tool_names = []
        agent._memory_manager = None
        agent._tool_worker_threads = set()
        agent._tool_worker_threads_lock = threading.Lock()
        agent.session_id = "session"
        agent.valid_tool_names = None
        agent.enabled_toolsets = None
        agent.disabled_toolsets = None

        tool_call = SimpleNamespace(
            id=f"call-{mode}",
            function=SimpleNamespace(name=name, arguments='{"value": "before"}'),
        )
        assistant_message = SimpleNamespace(tool_calls=[tool_call])
        messages = []

        with _lease() as live:
            agent._authenticated_gateway_context = live

            def dispatch(tool_name, args, *rest, **kwargs):
                tool_call_id = kwargs.get("tool_call_id")
                if tool_call_id is None and len(rest) > 1:
                    tool_call_id = rest[1]
                return registry.dispatch(
                    tool_name,
                    args,
                    authenticated_gateway_context=live,
                    authenticated_gateway_tool_call_id=tool_call_id,
                )

            agent._invoke_tool.side_effect = dispatch
            monkeypatch.setattr(
                tool_executor,
                "_ra",
                lambda: SimpleNamespace(handle_function_call=dispatch),
            )

            if mode == "sequential":
                tool_executor.execute_tool_calls_sequential(
                    agent,
                    assistant_message,
                    messages,
                    "task",
                    finalize=False,
                )
            else:
                tool_executor.execute_tool_calls_concurrent(
                    agent,
                    assistant_message,
                    messages,
                    "task",
                    finalize=False,
                )

            assert handler_calls == [({"value": "after-middleware"}, live)]

        assert middleware_calls == [(name, {"value": "before"})]
        assert messages[0]["content"] == "after-middleware"
    finally:
        registry.deregister(name)


def test_gateway_turn_requires_explicit_authenticated_decision():
    from types import SimpleNamespace

    from gateway.authenticated_dispatch import authenticated_gateway_turn

    agent = SimpleNamespace(authenticated_gateway_tool_dispatch_version=1)
    source = SimpleNamespace(
        platform="telegram",
        user_id="user",
        chat_id="chat",
        thread_id="thread",
        message_id="message",
    )

    with authenticated_gateway_turn(agent, source) as context:
        assert context is None


def test_gateway_turn_rejects_missing_or_mismatched_trigger_message_id():
    from types import SimpleNamespace

    from gateway.authenticated_dispatch import authenticated_gateway_turn

    agent = SimpleNamespace(authenticated_gateway_tool_dispatch_version=1)
    source = SimpleNamespace(
        platform="telegram",
        user_id="user",
        chat_id="chat",
        thread_id=None,
        message_id="source-message",
    )

    for event_message_id in (None, "different-message"):
        with authenticated_gateway_turn(
            agent,
            source,
            authenticated_gateway_request=True,
            event_message_id=event_message_id,
        ) as context:
            assert context is None


def test_gateway_turn_uses_event_message_id_when_source_has_none():
    from types import SimpleNamespace

    from gateway.authenticated_dispatch import authenticated_gateway_turn

    agent = SimpleNamespace(authenticated_gateway_tool_dispatch_version=1)
    source = SimpleNamespace(
        platform="telegram",
        user_id="user",
        chat_id="chat",
        thread_id=None,
        message_id=None,
    )

    with authenticated_gateway_turn(
        agent,
        source,
        authenticated_gateway_request=True,
        event_message_id="event-message",
    ) as context:
        assert context is not None
        assert context.message_id == "event-message"


def test_gateway_turn_issues_only_for_exact_capability_and_complete_source():
    from types import SimpleNamespace

    from gateway.authenticated_dispatch import (
        authenticated_gateway_turn,
        validate_authenticated_gateway_dispatch,
    )

    source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        user_id="user-1",
        chat_id="chat-1",
        thread_id="thread-1",
        message_id="message-1",
    )
    capable_agent = SimpleNamespace(
        authenticated_gateway_tool_dispatch_version=1,
    )
    with authenticated_gateway_turn(
        capable_agent,
        source,
        authenticated_gateway_request=True,
        event_message_id="message-1",
    ) as live:
        assert validate_authenticated_gateway_dispatch(live)
        assert live.platform == "telegram"
        assert live.user_id == "user-1"
        assert live.chat_id == "chat-1"
        assert live.thread_id == "thread-1"
        assert live.message_id == "message-1"
    assert not validate_authenticated_gateway_dispatch(live)

    for unsupported in (
        SimpleNamespace(),
        SimpleNamespace(authenticated_gateway_tool_dispatch_version=True),
        SimpleNamespace(authenticated_gateway_tool_dispatch_version="1"),
        SimpleNamespace(authenticated_gateway_tool_dispatch_version=2),
    ):
        with authenticated_gateway_turn(
            unsupported,
            source,
            authenticated_gateway_request=True,
        ) as denied:
            assert denied is None

    incomplete_source = SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        user_id=None,
        chat_id="chat-1",
        thread_id=None,
        message_id="message-1",
    )
    with authenticated_gateway_turn(
        capable_agent,
        incomplete_source,
        authenticated_gateway_request=True,
    ) as denied:
        assert denied is None
