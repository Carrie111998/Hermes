"""Unit tests for the shared session activity observation contract."""

from agent.session_activity import (
    ActivityProvenance,
    bound_activity_description,
    build_activity_snapshot,
    normalize_activity_provenance,
)


def test_bound_activity_description_truncates():
    long = "x" * 200
    out = bound_activity_description(long)
    assert len(out) == 120
    assert out.endswith("…")


def test_normalize_activity_provenance_defaults_to_unknown():
    assert normalize_activity_provenance(None) is ActivityProvenance.UNKNOWN
    assert normalize_activity_provenance("") is ActivityProvenance.UNKNOWN
    assert normalize_activity_provenance("not-a-real-source") is ActivityProvenance.UNKNOWN
    assert normalize_activity_provenance("agent.activity") is ActivityProvenance.UNKNOWN
    assert (
        normalize_activity_provenance(ActivityProvenance.AGENT_COMPRESSION)
        is ActivityProvenance.AGENT_COMPRESSION
    )
    assert (
        normalize_activity_provenance("agent.compression_timeout")
        is ActivityProvenance.AGENT_COMPRESSION_TIMEOUT
    )


def test_build_activity_snapshot_includes_compat_aliases():
    snap = build_activity_snapshot(
        last_activity_at=100.0,
        last_activity_description="starting API call #1",
        last_activity_provenance=ActivityProvenance.UNKNOWN,
        now=110.0,
        extra={"api_call_count": 1},
    )
    assert snap["last_activity_at"] == 100.0
    assert snap["last_activity_description"] == "starting API call #1"
    assert snap["last_activity_provenance"] == "unknown"
    assert snap["seconds_since_activity"] == 10.0
    assert snap["last_activity_ts"] == 100.0
    assert snap["last_activity_desc"] == "starting API call #1"
    assert snap["description"] == "starting API call #1"
    assert snap["api_call_count"] == 1
    assert "phase" not in snap
    assert "last_progress_at" not in snap


def test_build_activity_snapshot_maps_missing_provenance_to_unknown():
    snap = build_activity_snapshot(
        last_activity_at=1.0,
        last_activity_description="starting new turn (cached)",
        last_activity_provenance=None,
        now=2.0,
    )
    assert snap["last_activity_provenance"] == "unknown"
