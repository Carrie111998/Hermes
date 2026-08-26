"""Regression test for the quiet-gated tool-name repair notice (#95803).

The auto-repair notice was the block's only bare ``print()`` — every
neighbouring diagnostic goes through ``agent._vprint`` — so ``--quiet``
runs still emitted it, and it became the single loudest line in headless
logs (one deployment measured 4.8K lines/24h). The notice must route
through the same gate.
"""

from __future__ import annotations

import inspect

import agent.conversation_loop as cl


def test_repair_notice_routes_through_vprint_not_print():
    source = inspect.getsource(cl)
    needle = "Auto-repaired tool name"
    idx = source.index(needle)
    # Take the enclosing statement block around the notice text.
    window = source[max(0, idx - 300): idx + 200]
    assert "agent._vprint(" in window, (
        "the repair notice must go through agent._vprint so --quiet and "
        "suppress_status_output gate it (#95803)"
    )
    # The ungated bare-print form must be gone entirely.
    assert "print(f\"{needle}" not in source, (
        "no bare print of the repair notice may remain (#95803)"
    )
