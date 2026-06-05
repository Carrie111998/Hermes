"""Streaming probes: SSE content deltas and reasoning_content.

Covers the recent gateway change that streams model reasoning as
``delta.reasoning_content`` on /v1/chat/completions. Streaming-of-content is a
hard assertion (it must work everywhere); reasoning is opt-in and
model-dependent, so its probe validates *shape when present* and skips when the
backend emits none — it never fails for absence.
"""

from __future__ import annotations

import json

import pytest

from .constants import MODEL, STEER
from .http_client import chat_delta

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(0)]


def _collect_deltas(gateway, payload) -> list[dict]:
    deltas = []
    for data in gateway.stream("/v1/chat/completions", payload):
        try:
            deltas.append(chat_delta(json.loads(data)))
        except json.JSONDecodeError:
            pytest.fail(f"non-JSON SSE chunk: {data[:200]!r}")
    return deltas


def test_chat_stream_yields_content(gateway):
    deltas = _collect_deltas(
        gateway,
        {
            "model": MODEL,
            "messages": [
                {"role": "system", "content": STEER},
                {"role": "user", "content": "Reply with exactly the word PONG."},
            ],
        },
    )
    text = "".join(d.get("content") or "" for d in deltas)
    assert deltas, "stream produced no chunks"
    assert text.strip(), f"stream produced no content deltas: {deltas[:5]}"


def test_reasoning_content_shape(gateway):
    if not gateway.provider.spec.reasoning:
        pytest.skip(f"{gateway.provider.id} not flagged as reasoning-capable")

    deltas = _collect_deltas(
        gateway,
        {
            "model": MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": "Think step by step, then give the result of 17 * 23.",
                }
            ],
        },
    )
    reasoning = [d["reasoning_content"] for d in deltas if d.get("reasoning_content")]
    if not reasoning:
        pytest.skip(f"{gateway.provider.id} emitted no reasoning_content for this prompt")
    assert all(isinstance(r, str) for r in reasoning), "reasoning_content must be string deltas"
