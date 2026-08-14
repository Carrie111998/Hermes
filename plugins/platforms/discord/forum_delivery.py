"""Discord forum partial-delivery truth (feature T4).

Pure decision logic for reporting what actually happened when a long
response is delivered to a Discord forum thread as a starter post plus
continuation chunks.  The outcome NEVER collapses to an unconditional
success: callers always receive the created object id and the
retry/dedup truth so that retry loops can deduplicate before re-posting.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PartialOutcome:
    """Truth about a partial forum delivery attempt.

    Attributes:
        created_object_id: ID of the forum thread/post that was actually
            created, if known.  ``None`` when the object was not created
            or its ID could not be recovered.
        delivered: True when enough was delivered to treat the attempt
            as successful (starter-only deliveries count).
        retryable: True when a retry is safe/needed.
        error: Human-readable reason when the attempt was not delivered.
    """

    created_object_id: str | None
    delivered: bool
    retryable: bool
    error: str | None


def compute_partial_outcome(
    *,
    starter_post_succeeded: bool,
    continuation_chunk_count: int,
    created_object_id: str | None,
) -> PartialOutcome:
    """Compute the delivery truth for a partial forum delivery.

    All arguments are keyword-only to keep call sites explicit.

    Rules:
      * Starter succeeded, chunks > 0, object id known:
        delivered (partial but enough), not retryable.
      * Starter succeeded, chunks > 0, object id unknown:
        NOT delivered; retryable, but the caller must deduplicate
        before retrying because the created object may exist.
      * Starter succeeded, no chunks: delivered (starter only).
      * Starter failed: not delivered, retryable.
    """
    if not starter_post_succeeded:
        return PartialOutcome(
            created_object_id=created_object_id,
            delivered=False,
            retryable=True,
            error="starter post failed",
        )

    if continuation_chunk_count > 0:
        if created_object_id is None:
            return PartialOutcome(
                created_object_id=None,
                delivered=False,
                retryable=True,
                error="created object id unknown; must dedup before retry",
            )
        return PartialOutcome(
            created_object_id=created_object_id,
            delivered=True,
            retryable=False,
            error=None,
        )

    # Only the starter post was delivered.
    return PartialOutcome(
        created_object_id=created_object_id,
        delivered=True,
        retryable=False,
        error=None,
    )
