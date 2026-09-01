"""Indexed executable shards for :mod:`tools.mcp_tool`."""

from importlib import import_module
from pathlib import Path
import sys

_SHARD_NAMES = (
    "bootstrap_content",
    "server_lifecycle",
    "server_transport",
    "result_handlers",
    "registration",
    "shutdown",
)
_CLASS_SOURCE_MODULES = {
    "MCPServerTask": "tools.mcp_tool_shards.server_transport",
    "_MCPServerTaskTransportMixin": "tools.mcp_tool_shards.server_transport",
    "SamplingHandler": "tools.mcp_tool_shards.server_lifecycle",
    "ElicitationHandler": "tools.mcp_tool_shards.server_lifecycle",
    "_MCPServerTaskLifecycleMixin": "tools.mcp_tool_shards.server_lifecycle",
}
SHARD_INDEX = []


def install(namespace: dict[str, object]) -> None:
    """Load every shard into the original ``tools.mcp_tool`` namespace."""
    SHARD_INDEX.clear()
    for shard_name in _SHARD_NAMES:
        module = import_module(f"{__name__}.{shard_name}")
        module.install(namespace)
        SHARD_INDEX.append(
            (
                shard_name,
                str(Path(module.__file__).resolve()),
                tuple(module.EXPORTED_NAMES),
            )
        )
    # Functions and their globals stay in the facade namespace so existing
    # imports and monkeypatch targets remain stable.  Classes retain the
    # defining shard module as their introspection source, because
    # inspect.getsource() resolves class source through ``__module__``.
    for name, source_module in _CLASS_SOURCE_MODULES.items():
        cls = namespace.get(name)
        if isinstance(cls, type):
            cls.__module__ = source_module
            setattr(sys.modules[source_module], name, cls)
