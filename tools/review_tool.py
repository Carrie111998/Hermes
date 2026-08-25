"""Parent-only model-facing bridge to the built-in review engine."""

from tools.registry import registry, tool_error


REVIEW_CURRENT_WORK_SCHEMA = {
    "name": "review_current_work",
    "description": (
        "Dispatch the existing full-tool Hermes reviewer against the current "
        "work. Use at earned security, migration, architecture, provider-boundary, "
        "or other consequential review points. If dispatched asynchronously, this is "
        "a hard boundary for the current tool batch and the review returns to "
        "the parent conversation when complete."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": "Optional narrow review focus or gate-specific question.",
            },
        },
        "required": [],
    },
}


def _parent_context_required(args, **_kwargs):
    """Registry fallback; the agent loop owns the parent-aware dispatch."""
    return tool_error(
        "review_current_work requires the parent agent loop context and cannot "
        "run as a generic tool call."
    )


registry.register(
    name="review_current_work",
    toolset="review",
    schema=REVIEW_CURRENT_WORK_SCHEMA,
    handler=_parent_context_required,
    emoji="⚖",
)
