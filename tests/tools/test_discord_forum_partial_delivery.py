"""Tests for Discord forum partial-delivery truth (feature T4)."""

import pytest

from plugins.platforms.discord.forum_delivery import (
    PartialOutcome,
    compute_partial_outcome,
)


def test_starter_and_chunks_with_known_id_is_delivered_but_partial():
    outcome = compute_partial_outcome(
        starter_post_succeeded=True,
        continuation_chunk_count=3,
        created_object_id="thread-42",
    )
    assert isinstance(outcome, PartialOutcome)
    assert outcome.delivered is True
    assert outcome.retryable is False
    assert outcome.created_object_id == "thread-42"
    assert outcome.error is None


def test_created_object_id_missing_requires_dedup_before_retry():
    outcome = compute_partial_outcome(
        starter_post_succeeded=True,
        continuation_chunk_count=2,
        created_object_id=None,
    )
    assert outcome.delivered is False
    assert outcome.retryable is True
    assert outcome.created_object_id is None
    assert "dedup" in (outcome.error or "").lower()


def test_starter_only_is_delivered():
    outcome = compute_partial_outcome(
        starter_post_succeeded=True,
        continuation_chunk_count=0,
        created_object_id="thread-7",
    )
    assert outcome.delivered is True
    assert outcome.retryable is False
    assert outcome.created_object_id == "thread-7"
    assert outcome.error is None


def test_starter_only_with_unknown_id_still_counts_as_delivered():
    # Nothing was created after the starter, so there is nothing to
    # deduplicate on retry; the starter-only delivery is complete.
    outcome = compute_partial_outcome(
        starter_post_succeeded=True,
        continuation_chunk_count=0,
        created_object_id=None,
    )
    assert outcome.delivered is True
    assert outcome.retryable is False


def test_starter_failed_is_not_delivered_and_retryable():
    outcome = compute_partial_outcome(
        starter_post_succeeded=False,
        continuation_chunk_count=5,
        created_object_id=None,
    )
    assert outcome.delivered is False
    assert outcome.retryable is True
    assert outcome.created_object_id is None
    assert outcome.error is not None


def test_starter_failed_with_known_id_never_reports_success():
    outcome = compute_partial_outcome(
        starter_post_succeeded=False,
        continuation_chunk_count=4,
        created_object_id="thread-99",
    )
    assert outcome.delivered is False
    assert outcome.retryable is True


def test_arguments_are_keyword_only():
    with pytest.raises(TypeError):
        compute_partial_outcome(True, 0, None)


@pytest.mark.parametrize(
    ("starter", "chunks", "obj_id"),
    [
        (True, 0, None),
        (True, 0, "t-1"),
        (True, 1, None),
        (True, 1, "t-2"),
        (True, 7, "t-3"),
        (False, 0, None),
        (False, 3, "t-4"),
        (False, 3, None),
    ],
)
def test_never_unconditional_success_invariant(starter, chunks, obj_id):
    """Every outcome carries the id and the retry/dedup truth.

    There is no bare ``success`` flag: ``delivered`` and ``retryable``
    are always explicit, and a successful outcome that posted
    continuation chunks must carry the created object id so dedup is
    possible before any retry.
    """
    outcome = compute_partial_outcome(
        starter_post_succeeded=starter,
        continuation_chunk_count=chunks,
        created_object_id=obj_id,
    )
    assert isinstance(outcome, PartialOutcome)
    # The outcome surface exposes only truth-bearing fields.
    assert not hasattr(outcome, "success")
    assert isinstance(outcome.delivered, bool)
    assert isinstance(outcome.retryable, bool)
    # The outcome always carries the created object id truth.
    assert outcome.created_object_id == obj_id
    if outcome.delivered and chunks > 0:
        # Partial-but-enough success must still know what was created.
        assert outcome.created_object_id is not None
        assert outcome.retryable is False
    if not starter:
        assert outcome.delivered is False
        assert outcome.retryable is True
