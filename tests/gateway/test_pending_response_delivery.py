"""Regression coverage for stale-final suppression while interrupting a busy turn."""

from types import SimpleNamespace

from gateway.run import _should_deliver_completed_response_before_pending


def test_interrupt_mode_drops_completed_prior_final_for_pending_human_event():
    """A late-arriving human follow-up must not receive the prior turn's final."""
    pending = SimpleNamespace(internal=False)

    assert not _should_deliver_completed_response_before_pending(
        was_interrupted=False,
        pending_event=pending,
        busy_input_mode="interrupt",
    )


def test_queue_mode_still_delivers_completed_prior_final_before_followup():
    """Queue mode retains the deliberate ordered-delivery behavior."""
    pending = SimpleNamespace(internal=False)

    assert _should_deliver_completed_response_before_pending(
        was_interrupted=False,
        pending_event=pending,
        busy_input_mode="queue",
    )


def test_internal_followup_does_not_suppress_completed_prior_final():
    """Async internal events are not human supersession requests."""
    pending = SimpleNamespace(internal=True)

    assert _should_deliver_completed_response_before_pending(
        was_interrupted=False,
        pending_event=pending,
        busy_input_mode="interrupt",
    )
