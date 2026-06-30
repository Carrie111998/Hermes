"""Frozen HL-AOS classification / taint source for Hermes sessions.

This module is the single canonical location the A1 dispatch guard reads
HL-AOS classification from at model-dispatch time.  It intentionally does
NOT fall back to arbitrary agent attributes — only the values produced by
the frozen HL-AOS taint pipeline (the agent-level attribute and the
matching ``runtime_metadata`` key) are trusted inputs.  The dispatch guard
records the source provenance so auditors can see whether a decision was
made with a certified taint input, or under fail-closed policy for an
uncertified session.

Fail-closed invariant: when no frozen taint is present for a session the
caller must NOT silently assume a public classification.  The guard must
see an empty string from :func:`read_hl_aos_classification` and apply the
fail-closed rule for its route category.
"""

from __future__ import annotations

from typing import Any

HL_AOS_TAINT_ATTR: str = "hl_aos_taint_classification"
HL_AOS_METADATA_KEY: str = "hl_aos_taint_classification"
HL_AOS_TAINT_SOURCE_LABEL: str = "hl_aos_frozen"
HL_AOS_UNCLASSIFIED_LABEL: str = "unclassified"


def read_hl_aos_classification(agent: Any) -> str:
    """Return the HL-AOS frozen classification for ``agent``, upper-cased.

    Source priority:

    1. ``agent.hl_aos_taint_classification`` — the frozen session taint
       attribute set by the HL-AOS taint pipeline on session bootstrap.
    2. ``agent.runtime_metadata["hl_aos_taint_classification"]`` — the
       same taint surfaced through the session runtime-metadata map.

    If neither source is present, returns the empty string.  The A1
    dispatch guard is expected to apply its fail-closed policy for the
    route category when the returned classification is empty — never to
    treat emptiness as ``C0_PUBLIC``.
    """
    direct = getattr(agent, HL_AOS_TAINT_ATTR, None)
    if direct is not None and str(direct).strip():
        return str(direct).strip().upper()
    metadata = getattr(agent, "runtime_metadata", None)
    if isinstance(metadata, dict):
        indirect = metadata.get(HL_AOS_METADATA_KEY)
        if indirect is not None and str(indirect).strip():
            return str(indirect).strip().upper()
    return ""


def classification_source(agent: Any) -> str:
    """Return the provenance label for the current HL-AOS taint on ``agent``.

    Returns :data:`HL_AOS_TAINT_SOURCE_LABEL` (``"hl_aos_frozen"``) when a
    certified HL-AOS taint is present and :data:`HL_AOS_UNCLASSIFIED_LABEL`
    (``"unclassified"``) otherwise.  Used by :mod:`hermes_cli.a1_guard` to
    record whether each dispatch decision rested on a frozen taint.
    """
    return HL_AOS_TAINT_SOURCE_LABEL if read_hl_aos_classification(agent) else HL_AOS_UNCLASSIFIED_LABEL
