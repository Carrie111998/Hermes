"""Discord REST pagination conformance helpers.

Pure logic only -- no network I/O. Implements the parameter shapes the
Discord REST API expects for paginated list endpoints:

* ``before`` / ``after`` / ``around`` are mutually exclusive (Discord
  rejects requests that send more than one of them).
* ``limit`` must be within 1..100 (Discord's minimum and maximum;
  Discord's own default is 100).
* A full page (``returned_count == limit``) means more results may exist.
* Forward continuation after a ``before``/``around`` page flips to
  ``after`` keyed on the last item's id.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Union

__all__ = [
    "Page",
    "PaginationError",
    "has_more_page",
    "next_page_params",
    "page_params",
]

#: Discord's allowed limit range for paginated list endpoints.
MIN_LIMIT = 1
MAX_LIMIT = 100

#: Discord's own default page size (used when no limit is supplied).
DISCORD_DEFAULT_LIMIT = 100


class PaginationError(ValueError):
    """Raised for invalid pagination parameter combinations."""


@dataclass
class Page:
    """A single page of results from a paginated Discord REST endpoint."""

    items: List[Any] = field(default_factory=list)
    has_more: bool = False


def page_params(
    before: Optional[Union[str, int]] = None,
    after: Optional[Union[str, int]] = None,
    around: Optional[Union[str, int]] = None,
    limit: int = 50,
) -> Dict[str, Any]:
    """Build validated query params for a Discord paginated list request.

    Args:
        before: Snowflake id; return messages before this id.
        after: Snowflake id; return messages after this id.
        around: Snowflake id; return messages around this id.
        limit: Max number of results; clamped to Discord's 1..100 range.

    Returns:
        A dict suitable for ``requests.get(..., params=...)``, e.g.
        ``{"before": "123", "limit": 50}``. Only the given cursor key is
        included; ``limit`` is always present.

    Raises:
        PaginationError: if more than one of ``before``/``after``/``around``
            is supplied.
    """
    given = [
        name
        for name, value in (("before", before), ("after", after), ("around", around))
        if value is not None
    ]
    if len(given) > 1:
        raise PaginationError(
            "before, after, and around are mutually exclusive; "
            f"got: {', '.join(given)}"
        )

    clamped_limit = max(MIN_LIMIT, min(MAX_LIMIT, int(limit)))

    params: Dict[str, Any] = {"limit": clamped_limit}
    if before is not None:
        params["before"] = before
    elif after is not None:
        params["after"] = after
    elif around is not None:
        params["around"] = around
    return params


def has_more_page(returned_count: int, limit: int) -> bool:
    """Return True when a full page was returned, so more may exist.

    Discord pagination heuristic: if the API returned exactly ``limit``
    items, the page is full and there is likely another page after it;
    a partial (or empty) page means the end was reached.
    """
    return returned_count == limit


def next_page_params(current_params: Dict[str, Any], last_id: Union[str, int]) -> Dict[str, Any]:
    """Build params for the next (forward) page after ``current_params``.

    Forward continuation uses ``after=<last item id>``. When the current
    page was fetched with ``before`` or ``around`` (or no cursor), the
    continuation flips to ``after``; when the current params already use
    ``after``, there is no valid forward continuation and a
    ``PaginationError`` is raised.

    Args:
        current_params: Params previously returned by :func:`page_params`
            (or an equivalent dict with a ``limit`` key).
        last_id: Snowflake id of the last item on the current page.

    Returns:
        ``{"after": last_id, "limit": <current limit>}``.

    Raises:
        PaginationError: if ``current_params`` already contains ``after``.
    """
    if "after" in current_params:
        raise PaginationError(
            "cannot continue forward from params that already use 'after'"
        )

    limit = current_params.get("limit", 50)
    return {"after": last_id, "limit": limit}
