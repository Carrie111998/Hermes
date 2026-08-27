"""Stdlib-only fail-closed AST inventory for recipient-visible transports."""

from __future__ import annotations

import ast
import hashlib

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
# Reviewed direct-call diagnostics: inbound/control HTTP, non-publishing uploads,
# fixed lifecycle notices, or already-sealed adapter/facade payloads. Line
# offsets are retained only to make review reports easy to compare; exemption
# authority comes exclusively from structural identity plus canonical AST digest.
_REVIEWED_DIRECT_TRANSPORT_DIAGNOSTICS = frozenset({
    "gateway/delivery.py:89:send:send",
    "gateway/platforms/bluebubbles.py:257:_api_post:post",
    "gateway/platforms/qqbot/adapter.py:2384:_api_request:request",
    "gateway/platforms/signal.py:995:_rpc:post",
    "gateway/platforms/weixin.py:415:_do:post",
    "gateway/platforms/whatsapp_cloud.py:637:send_typing:post",
    "gateway/platforms/whatsapp_cloud.py:712:_post_interactive:post",
    "gateway/platforms/yuanbao.py:530:fetch:post",
    "gateway/platforms/yuanbao_media.py:416:get_cos_credentials:post",
    "gateway/run.py:24956:_send_restart_notification:send",
    "gateway/run.py:25037:_send_home_channel_startup_notifications:send",
    "gateway/run.py:25044:_send_home_channel_startup_notifications:send",
    "gateway/run.py:25130:_send_session_db_warning_notifications:send",
    "gateway/run.py:25137:_send_session_db_warning_notifications:send",
    "gateway/run.py:28498:_run_agent_via_proxy:post",
    "gateway/run.py:30542:_run_agent_inner:edit_message",
    "gateway/run.py:30576:_run_agent_inner:edit_message",
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
    "plugins/platforms/line/adapter.py:550:loading:post",
    "plugins/platforms/photon/adapter.py:2655:_sidecar_call:post",
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
    "tools/send_message_tool.py:2357:_send_qqbot:post",
    "tools/send_message_tool.py:2365:_send_qqbot:post",
    "tools/send_message_tool.py:2373:_send_qqbot:post",
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
    "tools/send_message_tool.py:1856:post_api:post",
    "tools/send_message_tool.py:1984:_post:post",
})

# Structural exemption authority. The legacy fingerprint set above is retained
# only as a reviewed call inventory and for line-oriented diagnostics. A call is
# exempt only when its enclosing canonical function AST matches this digest and
# the declared proof kind validates mechanically.
_EXEMPTION_FUNCTION_CONTRACTS = {
    ('gateway/delivery.py', 'send'): ('4c252ad4dc25f97d59f33adb4d5504e56c8417accf35c8d00cd755851c75a1a3', 'sealed-helper'),
    ('gateway/platforms/bluebubbles.py', '_api_post'): ('5317343b0bcb47226a6880f4ff2b0471e6947fb585e4b7986c9fc38895da6d6f', 'sealed-helper'),
    ('gateway/platforms/qqbot/adapter.py', '_api_request'): ('83502953c83555505b4705ad2a2b9e018773b853467171b743003a4f314207fc', 'sealed-helper'),
    ('gateway/platforms/signal.py', '_rpc'): ('1fc42bc2c1bc05793acd419677317c807d1bc454cba8c2b88cf3abbeb0f14789', 'sealed-helper'),
    ('gateway/platforms/weixin.py', '_do'): ('5f5a629202f1c4e9a66ba11449ca0b7517bf58b103bae341dcad12e3e12b4ce1', 'sealed-helper'),
    ('gateway/platforms/whatsapp_cloud.py', 'send_typing'): ('c1f57b092d27bbba6f81b85bfcaa66349cd58ec417e94ed7b8c0770526267e10', 'sealed-helper'),
    ('gateway/platforms/whatsapp_cloud.py', '_post_interactive'): ('420dbf6d3b02051752721679b106563c079babb69c3634bd563905d6bb26d7db', 'sealed-helper'),
    ('gateway/platforms/yuanbao.py', 'send_text'): ('4893e3c1ee675d959a8bd660252b914c063384180327c0c677a82cf75e464888', 'sealed-helper'),
    ('gateway/platforms/yuanbao.py', 'fetch'): ('92b4dced13509009f3f351635685b135e0d3c53d345b526364be339e3c854116', 'sealed-helper'),
    ('gateway/platforms/yuanbao_media.py', 'get_cos_credentials'): ('39b9b95df231a79fe6e2db45d51841ba4e4305084786b0cf44e8c410a9a45340', 'sealed-helper'),
    ('gateway/run.py', '_send_restart_notification'): ('178a67647d6869591626812cbc5f212abaf350baefcb282ddbd622039b85bdec', 'constant'),
    ('gateway/run.py', '_send_home_channel_startup_notifications'): ('aec4eeafe054cdafd2a677e5fc8f37a0be55ecb39c31151e54e6a015164e481a', 'sealed-helper'),
    ('gateway/run.py', '_send_session_db_warning_notifications'): ('3207fcb95be1d9ff4a3d69dc035c74c2d6c2c4ab39b29740402bc1a4d3906df3', 'sealed-helper'),
    ('gateway/run.py', '_run_agent_via_proxy'): ('4c0850bb3f750e2b30f38d16de1090535c7ee2df05d2e72cdf9b92418223e802', 'sealed-helper'),
    ('gateway/run.py', '_run_agent_inner'): ('8ab7a04e3f0eaa12216634d77d12aa26cf10098af60746d7c824eec808d57be8', 'sealed-helper'),
    ('gateway/run.py', '_deliver_bg_review_message'): ('73865b4a2cd4e04a921659cfcdf3c56cf7ec35f31dabd1241cce8eb3e61ad491', 'sealed-helper'),
    ('plugins/platforms/dingtalk/adapter.py', '_standalone_send'): ('949034dc0d0577bd2a3373b036958c6feb05e549b2bac956e7fb9aae74e0a2e9', 'terminal-envelope'),
    ('plugins/platforms/discord/adapter.py', '_standalone_send'): ('d8cb8a59ba6ef5b6288689721bce55082c335b362ea0efcdfd9011ed0b301d00', 'terminal-envelope'),
    ('plugins/platforms/discord/adapter.py', '_send_to_forum'): ('197d6b998ce59fc4506e683ed72ba98a61f83f5c15d8efe37683dc2f7524c21e', 'sealed-helper'),
    ('plugins/platforms/discord/adapter.py', '_edit_overflow_split'): ('2db27316c8f32762483774ac4cae57859ce33775f9298a9dd278317170a8591f', 'sealed-helper'),
    ('plugins/platforms/discord/adapter.py', '_send_file_attachment'): ('429b51b0bcb024401b8a7f5e33cb8ca0c135e3a98d1bae2a9e1d74cb2fcb32b5', 'sealed-helper'),
    ('plugins/platforms/discord/adapter.py', 'send_multiple_images'): ('7583bc84c11422e19205240950c03692ba1408c9595089b09dca9c345c03ed32', 'sealed-helper'),
    ('plugins/platforms/discord/adapter.py', '_skill_handler'): ('49ca7bfdda70b9fcb0e536a1b414964ae1fa7fbde54fe220c475f54bd9b26d9d', 'sealed-helper'),
    ('plugins/platforms/discord/adapter.py', '_auto_create_thread'): ('9550287a99f0359794416d6b0ae6dd8f476363bcb2895aff45e0c66e25738e68', 'sealed-helper'),
    ('plugins/platforms/discord/adapter.py', 'send_clarify'): ('77211fd6b37049dc07be6cf7d27359bbf1ede05d8ca09ebaf7d3ad849bc6cded', 'sealed-helper'),
    ('plugins/platforms/discord/adapter.py', 'send_update_prompt'): ('9eb8de69f16debd5d745313bd1d239be756f9bdea6e8d349f3addf758aadba85', 'sealed-helper'),
    ('plugins/platforms/google_chat/adapter.py', '_standalone_send'): ('412428e7975ebe2b6dbad6824d0dcfbb95b78f9d3b04361845d48e8999ad6945', 'terminal-envelope'),
    ('plugins/platforms/homeassistant/adapter.py', '_standalone_send'): ('8bf149fda95f1a05baf68d0a32d9d17976a1b0e198c393c5dbe05f7c20883408', 'terminal-envelope'),
    ('plugins/platforms/line/adapter.py', 'loading'): ('7fa5580edb6254622034ce562f310bdbd22a9930a43bacc34a5427f974972d12', 'sealed-helper'),
    ('plugins/platforms/mattermost/adapter.py', '_standalone_send'): ('c241da3f694af09980d66466fd0dcdef16613423517d7e2e8ca0b3fd04ebea06', 'terminal-envelope'),
    ('plugins/platforms/mattermost/adapter.py', '_api_post'): ('907b19afc91170e9107a908bce9e472ba3788329f09a845b2730da2911ac81ad', 'sealed-helper'),
    ('plugins/platforms/ntfy/adapter.py', '_standalone_send'): ('acdcdf6e81208e8c5a37e740057decc38653e30ca36fcde59f0c7b025205c080', 'terminal-envelope'),
    ('plugins/platforms/photon/adapter.py', '_sidecar_call'): ('a4506f8e12a4a3d17b217e537f25ecb280e03b9e1f2227e2bd748e214e997407', 'sealed-helper'),
    ('plugins/platforms/simplex/adapter.py', '_standalone_send'): ('83bc0debcef2ef07c027156cf62f18d449f89762ab9f741e2ed5a016daf91f4c', 'terminal-envelope'),
    ('plugins/platforms/simplex/adapter.py', '_send_ws'): ('c4f6de022f2dd3047ae78cea24f1f526280043fd182e0ac2d82cf5de286570f3', 'sealed-helper'),
    ('plugins/platforms/simplex/adapter.py', '_send_command'): ('efa550f64231314a651ee80b7ebe2aba654ade54f554f343148cd05c91245778', 'sealed-helper'),
    ('plugins/platforms/slack/adapter.py', '_send_slash_ephemeral'): ('75aebaa4b60f39cc98e7142383620d5b81c2a350ffe0e1a00304d7dec475bc1a', 'sealed-helper'),
    ('plugins/platforms/slack/adapter.py', '_post_ephemeral_fallback'): ('49b1b7ec446fe9b4d8e128e08ad216ad7702139fed6b024a8271feb3d7d07d2f', 'sealed-helper'),
    ('plugins/platforms/slack/adapter.py', '_resolve_slack_user_dm'): ('f0a55f4e5febc2e090fab038f23e055da9102cc0266675ae431663dfa6d551c6', 'sealed-helper'),
    ('plugins/platforms/slack/adapter.py', '_standalone_send'): ('5f1bf0da22898a85420cce36927a361742ff9ccfe6255503960ce579e6315778', 'terminal-envelope'),
    ('plugins/platforms/teams/adapter.py', '_send_card'): ('894ab3e56645b188c80259487f06c183905435bd3e25c08d327303af4fb47fc5', 'sealed-helper'),
    ('plugins/platforms/teams/adapter.py', '_write_summary_via_incoming_webhook'): ('9895652de26efd5471f49c9e7d17ed0921fec5cb1f2bec0bf93983c4992c4383', 'sealed-helper'),
    ('plugins/platforms/telegram/adapter.py', '_setup_dm_topics'): ('adc9cc6daf33e371e2a49b24cf79e3bcc899283e639817bbdc81ee527428e88f', 'sealed-helper'),
    ('plugins/platforms/telegram/adapter.py', '_edit_overflow_split'): ('d11c8f9f16ec4e35451d540dd6d848adb6c83b5f6b799f2eeca6163b9eeb4189', 'sealed-helper'),
    ('plugins/platforms/whatsapp/adapter.py', '_send_media_to_bridge'): ('2f571e4c529ef56f834dfc89569d4adf7041d79c85c96013ed18dda2f6b43078', 'sealed-helper'),
    ('plugins/platforms/whatsapp/adapter.py', '_standalone_send'): ('60214e396f7c591034ab0739f48290485e17cb18b1cb02516a36e0eb9314a9ff', 'terminal-envelope'),
    ('tools/send_message_tool.py', 'post_api'): ('696fdfbea53c18cf798911fc164409c40c939e7164753fe995212fbc69928fc3', 'sealed-helper'),
    ('tools/send_message_tool.py', '_post'): ('409468730ba8bdfca8e9fc441d159d8a90e74542361474848462204b8844687c', 'terminal-envelope'),
    ('tools/send_message_tool.py', '_send_qqbot'): ('5d24a2dd55a130d1e356ef673c80703344165b11ee5850ad8bab60993fe38612', 'terminal-envelope'),
}
_DIRECT_POLICY_FUNCTION_CONTRACTS = {
    ('gateway/delivery.py', '_deliver_to_platform'): ('53f805221af01ad02ae3c4de2cad250c82c82a0064e483c3a37c37671f8b3512', 'sealed-helper'),
    ('plugins/platforms/discord/adapter.py', '_create_thread'): ('07ebf1c5752a9971474d2e7a868bc0163ef7a12c70ec52fe5fcc0eece31eaf35', 'terminal-policy'),
    ('plugins/platforms/matrix/adapter.py', '_send_simple_message'): ('c4aaeae42f4438dfdc1b74b95192fbe16ce46352ce12c9e66e02349d28ac4e22', 'terminal-policy'),
    ('plugins/platforms/photon/adapter.py', '_standalone_send'): ('a193fe694c8e986ccf91ebf4832fcb672e58ecd27b430417a3625684d57f8507', 'terminal-policy'),
    ('tools/send_message_tool.py', '_send_telegram'): ('f0fd3ae1e1ecaa1d4aed2d15cfcf048513e4262cf35feaf0fa2261fe11b04402', 'terminal-policy'),
}
_SEND_TOOL_TERMINAL_DIGEST = "d3b2464035c58db09551ac37c5b13ad251a47cf1b39b70fa3a08b6237519a348"
_POLICY_FUNCTIONS = frozenset({
    "apply_terminal_outbound_text_policy",
    "apply_terminal_outbound_payload_policy",
})
_REVIEWED_STRUCTURAL_CALLS = frozenset(
    (path, function, operation)
    for fingerprint in _REVIEWED_DIRECT_TRANSPORT_DIAGNOSTICS
    for path, _line, function, operation in [fingerprint.split(":")]
)

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

def _function_digest(function: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    canonical = ast.dump(function, annotate_fields=True, include_attributes=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _is_exact_policy_call(node: ast.AST) -> bool:
    return bool(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in _POLICY_FUNCTIONS
    )


def _assigned_names(target: ast.AST) -> set[str]:
    return {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}


def _content_names(node: ast.AST) -> set[str]:
    return {
        child.id
        for child in ast.walk(node)
        if isinstance(child, ast.Name)
        and child.id.lower().lstrip("_")
        in {
            "body", "caption", "card", "chunk", "chunks", "content", "envelope",
            "formatted", "gated_envelope", "message", "name", "options", "payload",
            "seed_content", "starter_content", "status", "text", "thread_name", "title",
        }
    }


def _top_level_statement(
    node: ast.AST,
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    parents: dict[ast.AST, ast.AST],
) -> ast.stmt | None:
    cursor = node
    while parents.get(cursor) is not function:
        cursor = parents.get(cursor)
        if cursor is None:
            return None
    return cursor if isinstance(cursor, ast.stmt) else None


def _terminal_policy_result_dominates(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Prove a real policy result dominates every content transport in a function."""
    parents: dict[ast.AST, ast.AST] = {}
    for parent in ast.walk(function):
        for child in ast.iter_child_nodes(parent):
            parents[child] = parent
    top_index = {statement: index for index, statement in enumerate(function.body)}
    policy_assignments: list[tuple[int, set[str], ast.AST]] = []
    for call in ast.walk(function):
        if not _is_exact_policy_call(call):
            continue
        assignment = parents.get(call)
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
            continue
        top = _top_level_statement(assignment, function, parents)
        if top is not assignment or top not in top_index:
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        policy_assignments.append(
            (top_index[top], set().union(*(_assigned_names(target) for target in targets)), call)
        )
    if len(policy_assignments) != 1:
        return False
    policy_index, protected, _ = policy_assignments[0]

    # Conservative fixed-point data flow. Only the top-level policy assignment
    # can seed protection; derived values may be created in branches/loops only
    # when every content dependency is already protected.
    changed = True
    while changed:
        changed = False
        for statement in ast.walk(function):
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                if statement.lineno <= getattr(policy_assignments[0][2], "end_lineno", 0):
                    continue
                value = statement.value
                targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
                names = set().union(*(_assigned_names(target) for target in targets))
                dependencies = _content_names(value) if value is not None else set()
                if dependencies and dependencies <= protected and not names <= protected:
                    protected.update(names)
                    changed = True
            elif isinstance(statement, (ast.For, ast.AsyncFor)):
                dependencies = _content_names(statement.iter)
                names = _assigned_names(statement.target)
                if dependencies and dependencies <= protected and not names <= protected:
                    protected.update(names)
                    changed = True

    for statement in ast.walk(function):
        if not isinstance(statement, (ast.Assign, ast.AnnAssign)):
            continue
        if statement.lineno <= getattr(policy_assignments[0][2], "end_lineno", 0):
            continue
        value = statement.value
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        names = set().union(*(_assigned_names(target) for target in targets))
        dependencies = _content_names(value) if value is not None else set()
        if names & protected and dependencies and not dependencies <= protected:
            return False

    saw_transport = False
    for call in ast.walk(function):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        if call.func.attr not in _DIRECT_PUBLISH_OPERATIONS:
            continue
        payloads = [
            keyword.value
            for keyword in call.keywords
            if str(keyword.arg or "").lower() in _TRANSPORT_VISIBLE_NAMES
        ]
        if not payloads:
            payloads = [argument for argument in call.args if _content_names(argument)]
        if not payloads:
            continue
        saw_transport = True
        top = _top_level_statement(call, function, parents)
        if top not in top_index or top_index[top] <= policy_index:
            return False
        for payload in payloads:
            dependencies = _content_names(payload)
            if dependencies and not dependencies <= protected:
                return False
    return saw_transport


def _real_policy_assignment_precedes_transports(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    parents = {
        child: parent
        for parent in ast.walk(function)
        for child in ast.iter_child_nodes(parent)
    }
    policy_calls = [call for call in ast.walk(function) if _is_exact_policy_call(call)]
    if len(policy_calls) != 1:
        return False
    policy_call = policy_calls[0]
    assignment = parents.get(policy_call)
    if not isinstance(assignment, (ast.Assign, ast.AnnAssign)):
        return False
    top = _top_level_statement(assignment, function, parents)
    if top is not assignment:
        return False
    transport_verbs = {"create", "deliver", "edit", "post", "publish", "send", "transmit", "update"}
    transports = []
    for call in ast.walk(function):
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
            continue
        tokens = {
            token
            for token in call.func.attr.lower().replace("-", "_").split("_")
            if token
        }
        if call.func.attr in _DIRECT_PUBLISH_OPERATIONS or tokens & transport_verbs:
            transports.append(call)
    if assignment.end_lineno is None:
        return False
    return bool(transports) and all(call.lineno > assignment.end_lineno for call in transports)


def _constant_payload_contract(
    function: ast.FunctionDef | ast.AsyncFunctionDef,
    call: ast.Call,
) -> bool:
    constants: set[str] = set()
    for statement in function.body:
        if statement.lineno >= call.lineno:
            break
        if not isinstance(statement, ast.Assign):
            continue
        names = set().union(*(_assigned_names(target) for target in statement.targets))
        if isinstance(statement.value, ast.Constant):
            constants.update(names)
        else:
            constants.difference_update(names)
    payloads = [
        keyword.value
        for keyword in call.keywords
        if str(keyword.arg or "").lower() in _TRANSPORT_VISIBLE_NAMES
    ]
    if not payloads and len(call.args) >= 3:
        payloads = [call.args[2]]
    return bool(payloads) and all(
        isinstance(payload, ast.Constant)
        or (isinstance(payload, ast.Name) and payload.id in constants)
        for payload in payloads
    )


def validate_sealed_transport_exemptions(sources: dict[str, bytes]) -> None:
    """Validate digest-bound exemptions and the real canonical policy envelope."""
    caller_path = "tools/send_message_tool.py"
    raw = sources.get(caller_path)
    if raw is None:
        raise RuntimeError(f"sealed transport caller source missing: {caller_path}")
    try:
        tree = ast.parse(raw.decode("utf-8"))
    except (UnicodeDecodeError, SyntaxError) as exc:
        raise RuntimeError(f"sealed transport caller source invalid: {caller_path}") from exc
    callers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_send_to_platform"
    ]
    caller = callers[0] if len(callers) == 1 else None
    if (
        caller is None
        or _function_digest(caller) != _SEND_TOOL_TERMINAL_DIGEST
        or not _terminal_policy_result_dominates(caller)
    ):
        raise RuntimeError(
            "sealed transport caller is not terminally gated: "
            "tools/send_message_tool.py:_send_to_platform"
        )

    for (path, function_name), (expected_digest, proof) in _DIRECT_POLICY_FUNCTION_CONTRACTS.items():
        raw = sources.get(path)
        if raw is None:
            continue
        source_tree = ast.parse(raw.decode("utf-8"))
        matches = [
            node
            for node in ast.walk(source_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
            and _function_digest(node) == expected_digest
        ]
        if len(matches) != 1:
            raise RuntimeError(f"transport exemption digest mismatch: {path}:{function_name}")
        if proof == "terminal-policy" and not _real_policy_assignment_precedes_transports(matches[0]):
            raise RuntimeError(f"terminal policy exemption proof failed: {path}:{function_name}")
        if proof not in {"terminal-policy", "sealed-helper"}:
            raise RuntimeError(f"unknown transport exemption proof: {path}:{function_name}")

    for (path, function_name), (expected_digest, proof) in _EXEMPTION_FUNCTION_CONTRACTS.items():
        raw = sources.get(path)
        if raw is None:
            continue
        source_tree = ast.parse(raw.decode("utf-8"))
        matches = [
            node
            for node in ast.walk(source_tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == function_name
            and _function_digest(node) == expected_digest
        ]
        if len(matches) != 1:
            raise RuntimeError(
                f"transport exemption digest mismatch: {path}:{function_name}"
            )
        if proof not in {"constant", "terminal-envelope", "sealed-helper"}:
            raise RuntimeError(f"unknown transport exemption proof: {path}:{function_name}")
        if proof == "constant":
            reviewed_calls = [
                node for node in ast.walk(matches[0])
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in _DIRECT_PUBLISH_OPERATIONS
            ]
            if not reviewed_calls or not all(
                _constant_payload_contract(matches[0], call) for call in reviewed_calls
            ):
                raise RuntimeError(f"constant transport exemption proof failed: {path}:{function_name}")


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
            explicitly_gated = _terminal_policy_result_dominates(caller)
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
        operation_tokens = {
            token for token in operation.lower().replace("-", "_").split("_") if token
        }
        transport_verb = bool(
            operation_tokens
            & {"deliver", "edit", "post", "publish", "send", "transmit", "transport", "update"}
        )
        receiver = node.func.value
        receiver_text = ast.unparse(receiver).lower()
        receiver_tokens = {
            token for token in receiver_text.replace(".", "_").split("_") if token
        }
        structurally_transport_shaped = bool(
            isinstance(parents.get(node), ast.Await)
            and (
                has_visible_content
                or (
                    transport_verb
                    and call_names & _TRANSPORT_TARGET_NAMES
                    and len(argument_names - _TRANSPORT_TARGET_NAMES) > 0
                )
            )
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
        if not (known_publish_operation and has_visible_content) and not structurally_transport_shaped:
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
        direct_contract = _DIRECT_POLICY_FUNCTION_CONTRACTS.get(
            (relative_path, function.name)
        )
        if direct_contract is not None:
            expected_digest, proof = direct_contract
            if (
                _function_digest(function) == expected_digest
                and (
                    proof == "sealed-helper"
                    or (
                        proof == "terminal-policy"
                        and _real_policy_assignment_precedes_transports(function)
                    )
                )
            ):
                continue
        if _terminal_policy_result_dominates(function):
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
        structural_identity = (relative_path, function.name, operation)
        contract = _EXEMPTION_FUNCTION_CONTRACTS.get((relative_path, function.name))
        if structural_identity not in _REVIEWED_STRUCTURAL_CALLS or contract is None:
            violations.append(fingerprint)
            continue
        expected_digest, proof = contract
        if _function_digest(function) != expected_digest:
            violations.append(fingerprint)
            continue
        if proof == "constant" and not _constant_payload_contract(function, node):
            violations.append(fingerprint)
            continue
        private_structural = {
            (item.split(":")[0], item.split(":")[2], item.split(":")[3])
            for item in _SEALED_PRIVATE_TRANSPORT_EXEMPTIONS
        }
        if structural_identity in private_structural and not private_adapter_helper_is_sealed(function):
            violations.append(fingerprint)
    return sorted(violations)
