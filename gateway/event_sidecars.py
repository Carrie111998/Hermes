"""Generic lifecycle sidecars carried by gateway message events.

Transport adapters sometimes need state that must survive event batching but
must not alter the public ``MessageEvent`` schema: correlated component IDs
for edit supersession and one-shot cleanup callbacks for ephemeral artifacts.
This module owns that state so platform plugins do not grow special cases in
the base adapter or gateway runner.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from typing import Any, Callable


CORRELATED_MESSAGE_ITEMS_KEY = "correlated_message_items"
_POST_TURN_CLEANUP_ATTR = "_post_turn_cleanup_callbacks"


def merge_correlated_message_items(existing: Any, incoming: Any) -> None:
    """Append ``incoming`` correlation components to ``existing`` in order."""
    existing_metadata = dict(getattr(existing, "metadata", None) or {})
    incoming_metadata = dict(getattr(incoming, "metadata", None) or {})
    incoming_items = incoming_metadata.get(CORRELATED_MESSAGE_ITEMS_KEY)
    if not isinstance(incoming_items, list):
        return

    existing_items = existing_metadata.get(CORRELATED_MESSAGE_ITEMS_KEY)
    if not isinstance(existing_items, list):
        existing_items = []
    existing_metadata[CORRELATED_MESSAGE_ITEMS_KEY] = [
        *existing_items,
        *incoming_items,
    ]
    existing.metadata = existing_metadata


def add_post_turn_cleanup_callback(event: Any, callback: Callable[[], Any]) -> None:
    """Attach one cleanup callback to run after the event's turn completes."""
    callbacks = list(getattr(event, _POST_TURN_CLEANUP_ATTR, []) or [])
    callbacks.append(callback)
    setattr(event, _POST_TURN_CLEANUP_ATTR, callbacks)


def merge_event_sidecars(existing: Any, incoming: Any) -> None:
    """Merge correlation and cleanup sidecars when two events are combined."""
    merge_correlated_message_items(existing, incoming)
    for callback in list(getattr(incoming, _POST_TURN_CLEANUP_ATTR, []) or []):
        if callable(callback):
            add_post_turn_cleanup_callback(existing, callback)


def replace_correlated_event_text(
    event: Any,
    message_id: str,
    new_text: str,
) -> bool:
    """Replace one correlated component while preserving the aggregate event."""
    metadata = getattr(event, "metadata", None) or {}
    components = metadata.get(CORRELATED_MESSAGE_ITEMS_KEY, [])
    if isinstance(components, list):
        for component in components:
            if not isinstance(component, dict):
                continue
            if str(component.get("message_id")) != str(message_id):
                continue
            component["text"] = new_text
            event.text = "\n".join(
                str(item.get("text", ""))
                for item in components
                if isinstance(item, dict)
            )
            return True

    if str(getattr(event, "message_id", "")) == str(message_id):
        event.text = new_text
        return True
    return False


def correlated_event_message_ids(event: Any) -> set[str]:
    """Return every stable message ID represented by an aggregate event."""
    metadata = getattr(event, "metadata", None) or {}
    components = metadata.get(CORRELATED_MESSAGE_ITEMS_KEY, [])
    message_ids = {
        str(component.get("message_id"))
        for component in components
        if isinstance(component, dict) and component.get("message_id") is not None
    } if isinstance(components, list) else set()
    message_id = getattr(event, "message_id", None)
    if message_id is not None:
        message_ids.add(str(message_id))
    return message_ids


async def run_post_turn_cleanup_callbacks(
    event: Any,
    *,
    timeout: float,
    logger: logging.Logger,
    platform_name: str,
) -> None:
    """Run event cleanup callbacks with bounded, failure-isolated semantics."""
    for callback in list(getattr(event, _POST_TURN_CLEANUP_ATTR, []) or []):
        if not callable(callback):
            continue
        try:
            result = callback()
            if inspect.isawaitable(result):
                await asyncio.wait_for(result, timeout=timeout)
        except (asyncio.TimeoutError, Exception):
            logger.debug(
                "[%s] Post-turn artifact cleanup failed",
                platform_name,
                exc_info=True,
            )
