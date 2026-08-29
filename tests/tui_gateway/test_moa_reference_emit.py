"""Tests for the TUI gateway relaying MoA reference events to the client.

When a MoA preset is the active model, the agent's tool_progress_callback emits
``moa.reference`` (one per reference model, before the aggregator acts) and a
single ``moa.aggregating`` marker. ``_on_tool_progress`` must forward these to
the Ink/desktop client as labelled events so each reference renders like a
thinking block tagged with its source model.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture()
def server():
    # Mocks are scoped to the initial import only (see
    # tests/tui_gateway/test_protocol.py for the rationale).
    with patch.dict(
        "sys.modules",
        {
            "hermes_constants": MagicMock(
                get_hermes_home=MagicMock(return_value="/tmp/hermes_test_moa_emit")
            ),
            "hermes_cli.env_loader": MagicMock(),
            "hermes_cli.banner": MagicMock(),
            "hermes_state": MagicMock(),
        },
    ):
        import importlib

        mod = importlib.import_module("tui_gateway.server")

    yield mod
    mod._sessions.clear()


@pytest.fixture()
def emits(server, monkeypatch):
    captured: list = []
    monkeypatch.setattr(
        server,
        "_emit",
        lambda event, sid, payload=None: captured.append((event, sid, payload)),
    )
    monkeypatch.setattr(server, "_tool_progress_enabled", lambda sid: True)
    return captured


def test_moa_reference_relayed_with_label_and_index(server, emits):
    server._on_tool_progress(
        "sid-1",
        "moa.reference",
        "openrouter:openai/gpt-5.5",
        "Paris is the capital of France.",
        None,
        moa_index=1,
        moa_count=2,
    )

    assert len(emits) == 1
    event, sid, payload = emits[0]
    assert event == "moa.reference"
    assert sid == "sid-1"
    assert payload["label"] == "openrouter:openai/gpt-5.5"
    assert payload["text"] == "Paris is the capital of France."
    assert payload["index"] == 1
    assert payload["count"] == 2


def test_moa_progress_preserves_stable_slot_and_terminal_status(server, emits):
    server._on_tool_progress(
        "sid-1",
        "moa.progress",
        "openrouter:model-b",
        None,
        None,
        moa_refs_done=1,
        moa_refs_total=3,
        moa_index=2,
        moa_status="failed",
    )

    assert emits == [
        (
            "moa.progress",
            "sid-1",
            {
                "label": "openrouter:model-b",
                "refs_done": 1,
                "refs_total": 3,
                "index": 2,
                "status": "failed",
            },
        )
    ]


def test_moa_reference_phase_relays_complete_roster(server, emits):
    server._on_tool_progress(
        "sid-1",
        "moa.phase",
        "openrouter:aggregator",
        None,
        None,
        moa_phase="reference",
        moa_refs_done=0,
        moa_refs_total=10,
        moa_advisors=[f"openrouter:model-{index}" for index in range(10)],
        moa_concurrency=8,
        moa_fanout="every_n:3",
        moa_guidance_reused=False,
    )

    event, sid, payload = emits[0]
    assert (event, sid) == ("moa.phase", "sid-1")
    assert payload["advisors"] == [f"openrouter:model-{index}" for index in range(10)]
    assert payload["concurrency"] == 8
    assert payload["fanout"] == "every_n:3"
    assert payload["guidance_reused"] is False


