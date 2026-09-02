"""Behavior contracts for generic gateway event lifecycle sidecars."""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

import pytest

from gateway.event_sidecars import (
    CORRELATED_MESSAGE_ITEMS_KEY,
    add_post_turn_cleanup_callback,
    correlated_event_message_ids,
    merge_event_sidecars,
    replace_correlated_event_text,
    run_post_turn_cleanup_callbacks,
)


def _event(message_id: str, text: str) -> SimpleNamespace:
    return SimpleNamespace(
        message_id=message_id,
        text=text,
        metadata={
            CORRELATED_MESSAGE_ITEMS_KEY: [
                {"message_id": message_id, "text": text}
            ]
        },
    )


def test_merge_replace_and_collect_preserve_component_order():
    first = _event("1", "first")
    second = _event("2", "second")
    callbacks = []
    add_post_turn_cleanup_callback(first, lambda: callbacks.append("first"))
    add_post_turn_cleanup_callback(second, lambda: callbacks.append("second"))

    merge_event_sidecars(first, second)

    assert correlated_event_message_ids(first) == {"1", "2"}
    assert replace_correlated_event_text(first, "2", "corrected") is True
    assert first.text == "first\ncorrected"
    assert len(first._post_turn_cleanup_callbacks) == 2


@pytest.mark.asyncio
async def test_cleanup_callbacks_are_bounded_and_failure_isolated():
    event = _event("1", "first")
    completed = []

    async def too_slow():
        await asyncio.sleep(1)

    add_post_turn_cleanup_callback(event, too_slow)
    add_post_turn_cleanup_callback(event, lambda: completed.append("after-timeout"))
    add_post_turn_cleanup_callback(event, lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    add_post_turn_cleanup_callback(event, lambda: completed.append("after-error"))

    await run_post_turn_cleanup_callbacks(
        event,
        timeout=0.01,
        logger=logging.getLogger(__name__),
        platform_name="test",
    )

    assert completed == ["after-timeout", "after-error"]
