"""Minimal reproduction plugin for pre_tool_call block directive bug.

This plugin blocks ALL terminal tool calls unconditionally.
If the block directive is respected, terminal commands will fail with
"BLOCKED by test-block plugin". If the bug is present, terminal commands
execute normally and post_tool_call fires with the same tool name.

Related: https://github.com/NousResearch/hermes-agent/issues/73338
         https://github.com/NousResearch/hermes-agent/issues/41045
"""


def pre_block(tool_name: str, args: dict = None, **kwargs):
    """Block ALL terminal calls unconditionally — for testing only."""
    if tool_name == "terminal":
        return {"action": "block", "message": "BLOCKED by test-block plugin"}
    return None


def post_audit(tool_name: str, args: dict = None, result: str = None, **kwargs):
    """If this fires after a block, the block was ignored."""
    pass  # No side effects — test verifies via return value, not log


def register(ctx):
    ctx.register_hook("pre_tool_call", pre_block)
    ctx.register_hook("post_tool_call", post_audit)
