"""Pagination helpers for MCP list responses."""

import logging

logger = logging.getLogger(__name__)

_MCP_LIST_MAX_PAGES = 50


async def _paginate_full_list(list_method, items_attr: str, server_name: str):
    """Drain a paginated MCP ``list_*`` call by following ``nextCursor``.

    The MCP spec allows servers to paginate ``tools/list``,
    ``resources/list``, and ``prompts/list`` responses via an opaque
    ``nextCursor`` token. The Python SDK's ``ClientSession.list_*`` methods
    fetch exactly one page per call, so a client that never passes the
    cursor back silently sees only the first page — on a paginated server
    every tool/resource/prompt past page 1 would be invisible to the agent.

    Args:
        list_method: Bound ``session.list_tools`` / ``list_resources`` /
            ``list_prompts`` coroutine function.
        items_attr: Result attribute holding the page's items
            (``"tools"``, ``"resources"``, or ``"prompts"``).
        server_name: For log messages.

    Returns:
        Combined list of items across all pages. Callers must hold the
        server's ``_rpc_lock`` for the duration so pages come from a
        consistent snapshot.
    """
    items: list = []
    cursor = None
    for _ in range(_MCP_LIST_MAX_PAGES):
        result = await (list_method(cursor=cursor) if cursor else list_method())
        items.extend(getattr(result, items_attr, None) or [])
        cursor = getattr(result, "nextCursor", None)
        # Per the MCP spec the cursor is an opaque string; anything else
        # (including mock objects in tests) means "no more pages".
        if not isinstance(cursor, str) or not cursor:
            break
    else:
        logger.warning(
            "MCP server '%s': %s pagination exceeded %d pages; "
            "truncating at %d items",
            server_name, items_attr, _MCP_LIST_MAX_PAGES, len(items),
        )
    return items
