"""Semantic Graph plugin registration (no DB I/O at import time)."""

from __future__ import annotations

import logging
from typing import Any

from . import schemas
from .cli import register_cli
from .runtime import SemanticGraphRuntime

logger = logging.getLogger("hermes.plugins.semantic_graph")

TOOLSET = "semantic_graph"

_TOOLS = (
    ("semantic_graph_status", "handle_status", schemas.STATUS_SCHEMA, "🕸️"),
    ("semantic_graph_begin_run", "handle_begin_run", schemas.BEGIN_RUN_SCHEMA, "▶️"),
    ("semantic_graph_ingest", "handle_ingest", schemas.INGEST_SCHEMA, "📥"),
    (
        "semantic_graph_submit_fragment",
        "handle_submit_fragment",
        schemas.SUBMIT_FRAGMENT_SCHEMA,
        "🧩",
    ),
    ("semantic_graph_search", "handle_search", schemas.SEARCH_SCHEMA, "🔎"),
    ("semantic_graph_get", "handle_get", schemas.GET_SCHEMA, "📄"),
    ("semantic_graph_finalize", "handle_finalize", schemas.FINALIZE_SCHEMA, "✅"),
    (
        "semantic_graph_evaluate_output",
        "handle_evaluate_output",
        schemas.EVALUATE_OUTPUT_SCHEMA,
        "⚖️",
    ),
    ("semantic_graph_feedback", "handle_feedback", schemas.FEEDBACK_SCHEMA, "🗳️"),
    ("semantic_graph_export", "handle_export", schemas.EXPORT_SCHEMA, "📤"),
)


def register(ctx: Any) -> None:
    try:
        llm = ctx.llm
    except Exception:
        llm = None

    runtime = SemanticGraphRuntime(llm=llm)

    for name, handler_attr, schema, emoji in _TOOLS:
        ctx.register_tool(
            name=name,
            toolset=TOOLSET,
            schema=schema,
            handler=getattr(runtime, handler_attr),
            check_fn=runtime.check_available,
            emoji=emoji,
        )

    ctx.register_hook("pre_llm_call", runtime.on_pre_llm_call)
    ctx.register_hook("post_llm_call", runtime.on_post_llm_call)
    ctx.register_hook("post_tool_call", runtime.on_post_tool_call)
    ctx.register_hook("subagent_start", runtime.on_subagent_start)
    ctx.register_hook("subagent_stop", runtime.on_subagent_stop)
    ctx.register_hook("on_session_finalize", runtime.on_session_finalize)

    register_cli(ctx, runtime)
    logger.debug("semantic-graph plugin registered")
