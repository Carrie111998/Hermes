"""Optionality guarantees for the autopilot engine on a Council-less, CMX-less host.

These tests pin the two graceful-degradation promises the engine makes so it can
ship standalone on an upstream install that has *neither* the ``council`` package
nor a CMX verbatim store:

  1. Council is a DYNAMIC dependency. When ``council`` is not importable,
     ``ensure_council_importable`` returns ``False`` (never raises), and
     ``judge_completion`` transparently degrades to a single ``auxiliary_client``
     reviewer pass. A total judge failure fails OPEN (deliver) so a missing
     reviewer never wedges a run in an infinite loop.

  2. CMX is an OPTIONAL audit oracle. The provenance ladder is cmx → lcm →
     state.db. With no cmx.db and no lcm.db present, ``supports_claim`` degrades
     to the state.db floor (or reports ``reachable=False`` when even that is
     absent) instead of raising, so the audit probe never crashes the engine.

Everything here runs offline; no network, no council, no cmx.
"""

from __future__ import annotations

import importlib
import sys

import pytest

from agent.autopilot import council_gate as cg
from agent.autopilot import provenance as prov


# --------------------------------------------------------------------------- #
# 1. Council optional: not importable -> aux fail-open, never raises           #
# --------------------------------------------------------------------------- #
def test_council_not_importable_on_bare_host():
    """On a host without the council package, detection returns False, no raise.

    This is the real upstream condition (no monkeypatch): ``council`` is simply
    not on sys.path. ``ensure_council_importable`` must report that honestly
    rather than raising ImportError up into the turn.
    """
    if "council" in sys.modules:
        pytest.skip("council package is installed in this environment")
    with pytest.raises(ImportError):
        importlib.import_module("council")
    # The gate swallows the ImportError and reports False.
    assert cg.ensure_council_importable() is False


def test_judge_completion_degrades_to_aux_when_council_absent(monkeypatch):
    """With council unavailable, judge_completion uses the aux reviewer seam."""
    monkeypatch.setattr(cg, "ensure_council_importable", lambda *a, **k: False)
    monkeypatch.setattr(
        cg,
        "_aux_call",
        lambda msgs, *, model, max_tokens, timeout: (
            '{"complete": true, "confidence": 0.9, "reason": "looks done"}'
        ),
    )
    v = cg.judge_completion("ship the feature", "did the work", "final answer")
    assert v.complete is True
    assert v.source == "aux"


def test_judge_completion_fails_open_when_no_reviewer(monkeypatch):
    """No council AND a broken aux backend must fail OPEN (deliver), never raise."""
    monkeypatch.setattr(cg, "ensure_council_importable", lambda *a, **k: False)

    def _boom(*a, **k):
        raise RuntimeError("no aux backend on this host")

    monkeypatch.setattr(cg, "_aux_call", _boom)
    v = cg.judge_completion("goal", "work", "final")
    # Fail-open: a missing reviewer must not wedge the run in a loop.
    assert v.complete is True
    assert v.source == "fallback"


# --------------------------------------------------------------------------- #
# 2. CMX optional: no cmx.db / lcm.db -> degrade to state.db floor, no raise    #
# --------------------------------------------------------------------------- #
def test_provenance_degrades_when_no_cmx_or_lcm(monkeypatch, tmp_path):
    """Point the cmx and lcm rungs at nonexistent files: the ladder must not raise.

    With context.engine=compressor (the default), there is no cmx store to read.
    The provenance oracle must fall through to state.db, or report an honest
    ``reachable=False`` downgrade when no rung is present, rather than crashing
    the audit probe.
    """
    # cmx + lcm rungs point at files that do not exist.
    monkeypatch.setenv("CMX_DB_PATH", str(tmp_path / "does-not-exist-cmx.db"))
    monkeypatch.setenv("LCM_DB_PATH", str(tmp_path / "does-not-exist-lcm.db"))
    # state rung also absent -> full downgrade (reachable=False), still no raise.
    monkeypatch.setenv("HERMES_STATE_DB", str(tmp_path / "does-not-exist-state.db"))

    result = prov.supports_claim(["some evidence term"], session_id="sid-x")
    assert result.supported is False
    assert result.reachable is False
    # No cmx/lcm/state rung was reachable, so none were even tried past open.
    assert "cmx" not in (result.backends_tried or [])


def test_provenance_reads_state_floor_when_present(monkeypatch, tmp_path):
    """When only the state.db floor exists, the ladder reaches it (cmx optional)."""
    import sqlite3

    state_db = tmp_path / "state.db"
    conn = sqlite3.connect(str(state_db))
    try:
        conn.execute(
            "CREATE TABLE messages (session_id TEXT, role TEXT, content TEXT)"
        )
        conn.execute(
            "INSERT INTO messages VALUES (?, ?, ?)",
            ("sid-state", "tool", "PASS: the widget renders and the counter increments"),
        )
        conn.commit()
    finally:
        conn.close()

    monkeypatch.setenv("CMX_DB_PATH", str(tmp_path / "no-cmx.db"))
    monkeypatch.setenv("LCM_DB_PATH", str(tmp_path / "no-lcm.db"))
    monkeypatch.setenv("HERMES_STATE_DB", str(state_db))

    result = prov.supports_claim(
        ["counter", "increments"], session_id="sid-state"
    )
    # The state.db floor was reachable even with cmx + lcm absent.
    assert result.reachable is True
    assert "state" in (result.backends_tried or [])
