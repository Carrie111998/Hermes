"""Stdlib-only fail-closed AST inventory for recipient-visible transports."""

from __future__ import annotations

import ast

_DIRECT_PUBLISH_OPERATIONS = frozenset({
    "send", "send_message", "send_text", "send_media", "publish", "post",
    "request", "create_forum_topic", "create_thread", "edit_message",
    "update_message", "send_outbound", "send_follow_up",
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
    "gateway/run.py:25036:_send_home_channel_startup_notifications:send",
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
})


def scan_terminal_transport_inventory(source: str, *, relative_path: str) -> list[str]:
    """Return direct recipient-visible transport calls outside a closed boundary."""
    if relative_path in {"gateway/platforms/base.py", "tools/send_message_tool.py"}:
        return []
    tree = ast.parse(source)
    module_has_adapter = any(
        isinstance(node, ast.ClassDef)
        and any("BasePlatformAdapter" in ast.unparse(base) for base in node.bases)
        for node in tree.body
    )
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(tree):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent

    def enclosing(node: ast.AST, kind):
        cursor = parents.get(node)
        while cursor is not None and not isinstance(cursor, kind):
            cursor = parents.get(cursor)
        return cursor

    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        operation = node.func.attr
        if operation not in _DIRECT_PUBLISH_OPERATIONS:
            continue
        argument_names = {
            child.id.lower()
            for argument in (*node.args, *(kw.value for kw in node.keywords))
            for child in ast.walk(argument)
            if isinstance(child, ast.Name)
        }
        keyword_names = {str(kw.arg or "").lower() for kw in node.keywords}
        if not ((argument_names | keyword_names) & _TRANSPORT_VISIBLE_NAMES):
            continue
        receiver = node.func.value
        receiver_name = receiver.id if isinstance(receiver, ast.Name) else None
        if (
            receiver_name in {"self", "adapter", "transport"}
            and operation in {"send", "edit_message", "send_outbound", "send_follow_up"}
        ):
            continue
        function = enclosing(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        if function is None:
            violations.append(f"{relative_path}:{node.lineno}:<module>:{operation}")
            continue
        function_source = ast.get_source_segment(source, function) or ""
        if (
            "apply_terminal_outbound_text_policy" in function_source
            or "apply_terminal_outbound_payload_policy" in function_source
            or "_send_outbound_frame" in function_source
        ):
            continue
        owner = enclosing(function, ast.ClassDef)
        parameter_names = {
            arg.arg for arg in (
                *function.args.posonlyargs, *function.args.args,
                *function.args.kwonlyargs,
            )
        }
        is_adapter = module_has_adapter or (
            owner is not None
            and any("BasePlatformAdapter" in ast.unparse(base) for base in owner.bases)
        )
        if (
            is_adapter
            and parameter_names & _TRANSPORT_TARGET_NAMES
            and parameter_names & _TRANSPORT_VISIBLE_NAMES
        ):
            continue
        fingerprint = f"{relative_path}:{node.lineno}:{function.name}:{operation}"
        if fingerprint not in _REVIEWED_DIRECT_TRANSPORT_EXEMPTIONS:
            violations.append(fingerprint)
    return sorted(violations)
