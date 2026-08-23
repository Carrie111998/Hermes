"""Sensitive Kanban pre-tool final-argument policy."""
from __future__ import annotations

import os


def _on_pre_tool_call(**_payload):
    if os.environ.get("HERMES_KANBAN_SENSITIVE") != "1":
        return None
    from hermes_cli.kanban_sensitive import (
        assert_sensitive_worker_context,
        validate_final_tool_args,
    )

    assert_sensitive_worker_context()
    return {
        "action": "validate",
        "validator": validate_final_tool_args,
        "policy": "kanban_sensitive",
    }


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", _on_pre_tool_call)
