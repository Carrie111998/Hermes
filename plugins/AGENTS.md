# Plugin Engineering Guide

Root [`AGENTS.md`](../AGENTS.md) still applies. This file owns plugin boundaries
and in-tree plugin policy.

## Core boundary

Plugins do not patch `run_agent.py`, `cli.py`, `gateway/run.py`,
`hermes_cli/main.py`, or other plugin-specific core branches. When a real
consumer needs a missing capability, widen a generic hook, ABC, or context
surface and consume it through the plugin API.

Third-party products and new memory backends ship as standalone plugin
repositories installed under the active Hermes home or via entry points. The
existing in-tree providers are maintained precedent, not permission to add
more vendor integrations.

## Discovery systems

Do not conflate the registries:

- General plugins are discovered by `hermes_cli/plugins.py` and expose
  `register(ctx)` for hooks, tools, and CLI commands.
- Memory providers implement `agent/memory_provider.py::MemoryProvider` and
  are orchestrated by `agent/memory_manager.py`.
- Model-provider plugins register `ProviderProfile` through the lazy discovery
  in `providers/__init__.py`; general discovery records but does not import
  them.

Discovery timing matters: code that reads general plugin state without first
importing `model_tools.py` must call the idempotent `discover_plugins()`
explicitly.

User model-provider plugins override bundled providers with the same name.
Keep that last-writer-wins behavior and the bundled/user/legacy scan order.

## State and setup

Plugin state is profile-scoped through `get_hermes_home()`. Setup flows use the
existing config and secret surfaces; do not add plugin-specific environment
knobs for non-secret behavior.

Authoring references:

- [`website/docs/developer-guide/plugins/index.md`](../website/docs/developer-guide/plugins/index.md)
- [`website/docs/developer-guide/model-provider-plugin.md`](../website/docs/developer-guide/model-provider-plugin.md)
- [`plugins/model-providers/README.md`](model-providers/README.md)
