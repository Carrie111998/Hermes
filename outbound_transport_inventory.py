"""Stdlib-only fail-closed AST inventory for recipient-visible transports."""

from __future__ import annotations

import ast

_DIRECT_PUBLISH_OPERATIONS = frozenset({
    "send", "send_message", "send_text", "send_media", "publish", "post",
    "request", "create_forum_topic", "create_thread", "edit_message",
    "update_message", "send_outbound", "send_follow_up", "standalone_sender_fn",
})
_TRANSPORT_VISIBLE_NAMES = frozenset({
    "content", "text", "message", "name", "title", "caption", "payload",
    "body", "card", "blocks", "options", "thread_name", "starter_message",
    "seed_content", "status",
})
_TRANSPORT_TARGET_NAMES = frozenset({
    "chat_id", "channel_id", "parent_chat_id", "user_id", "to_account",
})
# Exact reviewed direct-call inventory: inbound/control HTTP, non-publishing
# uploads, fixed lifecycle notices, or already-sealed adapter/facade payloads.
# Line identity is deliberate: moving or adding a call forces security review.
_REVIEWED_DIRECT_TRANSPORT_EXEMPTIONS = frozenset({
    "gateway/delivery.py:89:send:send",
    "gateway/platforms/bluebubbles.py:257:_api_post:post",
    "gateway/platforms/qqbot/adapter.py:2384:_api_request:request",
    "gateway/platforms/signal.py:995:_rpc:post",
    "gateway/platforms/weixin.py:415:_do:post",
    "gateway/platforms/whatsapp_cloud.py:637:send_typing:post",
    "gateway/platforms/whatsapp_cloud.py:712:_post_interactive:post",
    "gateway/platforms/yuanbao.py:530:fetch:post",
    "gateway/platforms/yuanbao_media.py:416:get_cos_credentials:post",
    "gateway/run.py:25029:_send_home_channel_startup_notifications:send",
    "gateway/run.py:25036:_send_home_channel_startup_notifications:send",
    "gateway/run.py:25122:_send_session_db_warning_notifications:send",
    "gateway/run.py:25129:_send_session_db_warning_notifications:send",
    "gateway/run.py:28490:_run_agent_via_proxy:post",
    "gateway/run.py:30534:_run_agent_inner:edit_message",
    "gateway/run.py:30568:_run_agent_inner:edit_message",
    "gateway/run.py:5915:_deliver_bg_review_message:send",
    "plugins/platforms/discord/adapter.py:3618:_send_to_forum:create_thread",
    "plugins/platforms/discord/adapter.py:3637:_send_to_forum:send",
    "plugins/platforms/discord/adapter.py:3915:_edit_overflow_split:send",
    "plugins/platforms/discord/adapter.py:3925:_edit_overflow_split:send",
    "plugins/platforms/discord/adapter.py:4152:send_multiple_images:send",
    "plugins/platforms/discord/adapter.py:6304:_skill_handler:send_message",
    "plugins/platforms/discord/adapter.py:7256:_auto_create_thread:create_thread",
    "plugins/platforms/discord/adapter.py:7265:_auto_create_thread:send",
    "plugins/platforms/discord/adapter.py:7268:_auto_create_thread:create_thread",
    "plugins/platforms/discord/adapter.py:7736:send_clarify:send",
    "plugins/platforms/discord/adapter.py:7778:send_update_prompt:send",
    "plugins/platforms/mattermost/adapter.py:189:_api_post:post",
    "plugins/platforms/photon/adapter.py:2654:_sidecar_call:post",
    "plugins/platforms/simplex/adapter.py:756:_send_ws:send",
    "plugins/platforms/simplex/adapter.py:777:_send_command:send",
    "plugins/platforms/slack/adapter.py:1839:_send_slash_ephemeral:post",
    "plugins/platforms/slack/adapter.py:9401:_resolve_slack_user_dm:post",
    "plugins/platforms/teams/adapter.py:271:_write_summary_via_incoming_webhook:post",
    "plugins/platforms/telegram/adapter.py:4030:_setup_dm_topics:send_message",
    # Standalone publishers below are reached only after the canonical terminal
    # envelope in tools/send_message_tool.py:_send_to_platform. Exact call-site
    # identity keeps that reviewed relationship fail-closed on source movement.
    "plugins/platforms/dingtalk/adapter.py:1753:_standalone_send:post",
    "plugins/platforms/discord/adapter.py:10141:_standalone_send:post",
    "plugins/platforms/discord/adapter.py:10179:_standalone_send:post",
    "plugins/platforms/discord/adapter.py:10205:_standalone_send:post",
    "plugins/platforms/google_chat/adapter.py:3659:_standalone_send:post",
    "plugins/platforms/homeassistant/adapter.py:537:_standalone_send:post",
    "plugins/platforms/mattermost/adapter.py:1134:_standalone_send:post",
    "plugins/platforms/ntfy/adapter.py:568:_standalone_send:post",
    "plugins/platforms/simplex/adapter.py:1284:_standalone_send:send",
    "plugins/platforms/slack/adapter.py:9721:_standalone_send:post",
    "plugins/platforms/whatsapp/adapter.py:1738:_standalone_send:post",
    "plugins/platforms/whatsapp/adapter.py:1781:_standalone_send:post",
    "tools/send_message_tool.py:2345:_send_qqbot:post",
    "tools/send_message_tool.py:2353:_send_qqbot:post",
    "tools/send_message_tool.py:2361:_send_qqbot:post",
    # Private adapter transports are sealed by the named metaclass-wrapped
    # public/legacy caller; line movement requires re-review of that call graph.
    "plugins/platforms/discord/adapter.py:4014:_send_file_attachment:send",
    "plugins/platforms/slack/adapter.py:1894:_post_ephemeral_fallback:chat_postEphemeral",
    "plugins/platforms/teams/adapter.py:1088:_send_card:send",
    "plugins/platforms/telegram/adapter.py:5912:_edit_overflow_split:edit_message_text",
    "plugins/platforms/telegram/adapter.py:5925:_edit_overflow_split:edit_message_text",
    "plugins/platforms/telegram/adapter.py:5931:_edit_overflow_split:edit_message_text",
    "plugins/platforms/telegram/adapter.py:5980:_edit_overflow_split:send_message",
    "plugins/platforms/telegram/adapter.py:6003:_edit_overflow_split:send_message",
    "plugins/platforms/whatsapp/adapter.py:1062:_send_media_to_bridge:post",
    "gateway/platforms/yuanbao.py:4831:send_text:send_text",
    # Non-publishing Slack directory/control requests and Signal JSON-RPC are
    # exact reviewed exceptions (the latter's publishing caller is enveloped).
    "tools/send_message_tool.py:1844:post_api:post",
    "tools/send_message_tool.py:1972:_post:post",
})

# Unlike non-publishing/control exceptions, these private helpers carry real
# recipient-visible content. Their exact SDK calls are exempt only while every
# in-module caller remains a metaclass-wrapped adapter API or explicitly gated.
_SEALED_PRIVATE_TRANSPORT_EXEMPTIONS = frozenset({
    "plugins/platforms/discord/adapter.py:4014:_send_file_attachment:send",
    "plugins/platforms/slack/adapter.py:1894:_post_ephemeral_fallback:chat_postEphemeral",
    "plugins/platforms/teams/adapter.py:1088:_send_card:send",
    "plugins/platforms/telegram/adapter.py:5912:_edit_overflow_split:edit_message_text",
    "plugins/platforms/telegram/adapter.py:5925:_edit_overflow_split:edit_message_text",
    "plugins/platforms/telegram/adapter.py:5931:_edit_overflow_split:edit_message_text",
    "plugins/platforms/telegram/adapter.py:5980:_edit_overflow_split:send_message",
    "plugins/platforms/telegram/adapter.py:6003:_edit_overflow_split:send_message",
    "plugins/platforms/whatsapp/adapter.py:1062:_send_media_to_bridge:post",
})


def scan_terminal_transport_inventory(source: str, *, relative_path: str) -> list[str]:
    """Return direct recipient-visible transport calls outside a closed boundary."""
    if relative_path == "gateway/platforms/base.py":
        return []
    tree = ast.parse(source)
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def enclosing(node: ast.AST, kind):
        cursor = parents.get(node)
        while cursor is not None and not isinstance(cursor, kind):
            cursor = parents.get(cursor)
        return cursor

    def private_adapter_helper_is_sealed(
        function: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> bool:
        if not isinstance(function, ast.AsyncFunctionDef) or not function.name.startswith("_"):
            return False
        callers: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
        for candidate in ast.walk(tree):
            if (
                isinstance(candidate, ast.Call)
                and isinstance(candidate.func, ast.Attribute)
                and candidate.func.attr == function.name
                and isinstance(candidate.func.value, ast.Name)
                and candidate.func.value.id == "self"
            ):
                caller = enclosing(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
                if isinstance(caller, (ast.FunctionDef, ast.AsyncFunctionDef)) and caller is not function:
                    callers.append(caller)
        if not callers:
            return False
        for caller in callers:
            caller_owner = enclosing(caller, ast.ClassDef)
            caller_source = ast.get_source_segment(source, caller) or ""
            wrapped_adapter_api = bool(
                isinstance(caller, ast.AsyncFunctionDef)
                and isinstance(caller_owner, ast.ClassDef)
                and any(
                    "BasePlatformAdapter" in ast.unparse(base)
                    for base in caller_owner.bases
                )
                and (
                    not caller.name.startswith("_")
                    or caller.name in {"_post_interactive", "_send_media"}
                )
            )
            explicitly_gated = bool(
                "apply_terminal_outbound_text_policy" in caller_source
                or "apply_terminal_outbound_payload_policy" in caller_source
            )
            if not (wrapped_adapter_api or explicitly_gated):
                return False
        return True

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        operation = node.func.attr
        argument_names = {
            child.id.lower()
            for argument in (*node.args, *(kw.value for kw in node.keywords))
            for child in ast.walk(argument)
            if isinstance(child, ast.Name)
        }
        keyword_names = {str(kw.arg or "").lower() for kw in node.keywords}
        call_names = argument_names | keyword_names
        has_visible_content = bool(call_names & _TRANSPORT_VISIBLE_NAMES)
        known_publish_operation = operation in _DIRECT_PUBLISH_OPERATIONS
        receiver = node.func.value
        receiver_text = ast.unparse(receiver).lower()
        receiver_tokens = {
            token for token in receiver_text.replace(".", "_").split("_") if token
        }
        structurally_transport_shaped = bool(
            isinstance(parents.get(node), ast.Await)
            and has_visible_content
            and (
                call_names & _TRANSPORT_TARGET_NAMES
                or receiver_tokens & {"channel", "webhook"}
            )
            and receiver_tokens
            & {
                "api", "bot", "channel", "client", "connection", "http",
                "session", "socket", "transport", "webhook",
            }
        )
        if not has_visible_content or not (
            known_publish_operation or structurally_transport_shaped
        ):
            continue
        receiver_name = receiver.id if isinstance(receiver, ast.Name) else None
        if (
            receiver_name in {"self", "adapter"}
            and operation in _DIRECT_PUBLISH_OPERATIONS
        ):
            continue
        function = enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if function is None:
            violations.append(f"{relative_path}:{node.lineno}:<module>:{operation}")
            continue
        assert isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef))
        function_source = ast.get_source_segment(source, function) or ""
        if (
            "apply_terminal_outbound_text_policy" in function_source
            or "apply_terminal_outbound_payload_policy" in function_source
            or "_send_outbound_frame" in function_source
        ):
            continue
        owner = enclosing(function, ast.ClassDef)
        is_adapter_method = (
            owner is not None
            and any("BasePlatformAdapter" in ast.unparse(base) for base in owner.bases)
        )
        if (
            is_adapter_method
            and isinstance(function, ast.AsyncFunctionDef)
            and (
                not function.name.startswith("_")
                or function.name in {"_post_interactive", "_send_media"}
            )
        ):
            continue
        fingerprint = f"{relative_path}:{node.lineno}:{function.name}:{operation}"
        if fingerprint not in _REVIEWED_DIRECT_TRANSPORT_EXEMPTIONS:
            violations.append(fingerprint)
        elif fingerprint in _SEALED_PRIVATE_TRANSPORT_EXEMPTIONS:
            if not private_adapter_helper_is_sealed(function):
                violations.append(fingerprint)
    return sorted(violations)
