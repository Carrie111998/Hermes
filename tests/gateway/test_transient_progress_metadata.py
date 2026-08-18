"""Stable metadata contract for transport-level transient progress."""

from gateway.run import _transient_progress_metadata


def test_transient_progress_metadata_preserves_routing_fields():
    metadata = _transient_progress_metadata({"thread_id": "topic-1"})

    assert metadata == {"thread_id": "topic-1", "transient_progress": True}


def test_transient_progress_metadata_does_not_mutate_input():
    routing = {"thread_id": "topic-1"}

    metadata = _transient_progress_metadata(routing)

    assert routing == {"thread_id": "topic-1"}
    assert metadata is not routing
