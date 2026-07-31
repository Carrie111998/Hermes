# Tool Engineering Guide

Root [`AGENTS.md`](../AGENTS.md) still applies. This file owns built-in tools,
the registry, toolsets, and tool schemas.

## Footprint decision

Before creating a core tool, apply the root Footprint Ladder. Prefer existing
code, a CLI command plus skill, a service-gated tool, a standalone plugin, or
an MCP server. Core tools are the last resort because every schema is paid for
on every model call.

## Dependency chain

`tools/registry.py` is the low-dependency registry. Tool modules register at
import. `model_tools.py` triggers discovery and dispatch; agent entry points
depend on that layer.

Built-in tools require both:

1. a `tools/*.py` module with `registry.register(...)`; and
2. membership in `_HERMES_CORE_TOOLS` or another toolset in `toolsets.py`.

Auto-discovery does not expose an unlisted tool. Handlers return JSON strings.
Use `check_fn` and `requires_env` so unavailable service tools disappear.

## Schemas and state

- Schema paths use `display_hermes_home()` because schemas are user-visible.
- Persistent state uses `get_hermes_home()`.
- Do not hardcode a reference to a tool from another toolset in static schema
  prose. The referenced tool may be disabled. Conditional cross-tool guidance
  is assembled in `model_tools.get_tool_definitions()`.
- Instruction-loading tools must not offer pagination or lazy-reading escape
  hatches that let the model stop after page one.

Agent-level todo and memory tools are intercepted by `run_agent.py`; follow
their existing dispatch boundary rather than duplicating it.

## Toolsets

`toolsets.py::TOOLSETS` is the single catalog. Platform adapters choose a base
toolset and configuration adds/removes toolsets. Workflow-gated or mutating
toolsets remain explicit opt-ins; do not silently include them under wildcard
selection.

Public references:

- [`website/docs/developer-guide/adding-tools.md`](../website/docs/developer-guide/adding-tools.md)
- [`website/docs/reference/toolsets-reference.md`](../website/docs/reference/toolsets-reference.md)
