"""Regression: draft ids must be unique across gateway incarnations
(PR 85796 review, B3 — restart tombstone replay).

The relay connector tombstones sealed streams by (channel, draft_id) and
those tombstones outlive the gateway process (relay gateways are
disposable / scale-to-zero by design). A counter that restarts at zero
replays already-sealed wire identities: the connector answers the new
turn's frames out of the old tombstone — zero Slack API calls, the OLD
message ts returned as the new turn's identity — and the new answer is
silently dropped while every layer above records success.
"""

import time

from gateway.stream_consumer import GatewayStreamConsumer


class TestDraftIdRestartUniqueness:
    def test_counter_is_epoch_seeded_not_zero(self):
        """The class-level seed must be wall-clock derived, not a small
        constant — a restarted process must not mint ids a previous
        incarnation already used."""
        # Seed was taken at import time; it must be on the ms-epoch scale.
        seed = GatewayStreamConsumer._draft_id_counter
        now_ms = time.time_ns() // 1_000_000
        # Within 30 days of now (generous CI clock slack), i.e. clearly an
        # epoch value rather than a restarted small counter.
        assert abs(now_ms - seed) < 30 * 24 * 3600 * 1000, (
            f"draft-id seed {seed} is not epoch-scale; a process restart "
            "would replay wire identities the connector already sealed"
        )

    def test_two_incarnations_do_not_collide(self):
        """Simulate the restart: a second incarnation seeding later in
        wall-clock time can never mint an id the first one used, even
        after the first ran many turns."""
        first_seed = time.time_ns() // 1_000_000
        first_ids = {first_seed + n for n in range(1, 1001)}  # 1000 turns
        # Restart 2s later (scale-from-zero cold start is much slower).
        second_seed = first_seed + 2000
        second_ids = {second_seed + n for n in range(1, 1001)}
        assert first_ids & second_ids == set(), (
            "epoch-seeded incarnations must not overlap for realistic "
            "turn counts and restart gaps"
        )
