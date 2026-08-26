"""Internal wire aliases that must never be registered as real tools."""

TOOL_SEARCH_WIRE_ALIAS = "hermes_tool_search"
RESERVED_WIRE_TOOL_NAMES = frozenset({TOOL_SEARCH_WIRE_ALIAS})


def reject_reserved_wire_tool_name(name: str) -> None:
    """Fail when a public tool tries to claim an internal wire alias."""
    if name in RESERVED_WIRE_TOOL_NAMES:
        raise ValueError(
            f"Tool name {name!r} is a reserved wire alias and cannot be registered"
        )
