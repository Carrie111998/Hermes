"""Routing contracts for event-driven JobFlow activation.

Routes are derived from observed mailbox traffic, not from guesswork: the
message type is the filename prefix and the destination is the inbox
directory. The suffix on a filename is the SENDER (``SCORE_REQUEST_tracker``
is tracker *asking* for a score), so routing must never key on it.

The costly mistake this table prevents is waking a semantic worker for a
result. ``main/inbox`` carries ERROR, SCORE_RESULT, NOTIFICATION and
SCORE_BATCH_SUMMARY — high volume, zero actionable work.
"""

from __future__ import annotations

import pytest

from jobflow_dispatch.contracts import Activation, route_mailbox


class TestActionableRequests:
    @pytest.mark.parametrize(
        ("message_type", "destination", "expected"),
        (
            ("SCORE_REQUEST", "matcher", ("cron.jobflow.matcher",)),
            ("TAILOR_REQUEST", "tailor", ("jobflow.tailor.generate",)),
            ("TAILOR_MODULE_REQUEST", "tailor", ("jobflow.tailor.generate",)),
            ("RESEARCH_REQUEST", "researcher", ("cron.jobflow.researcher",)),
        ),
    )
    def test_requests_route_to_their_worker(self, message_type, destination, expected):
        assert route_mailbox(message_type, destination, {}) == expected


class TestNonActionable:
    @pytest.mark.parametrize(
        ("message_type", "destination"),
        (
            ("NOTIFICATION", "main"),
            ("ERROR", "main"),
            ("SCORE_RESULT", "main"),
            ("SCORE_BATCH_SUMMARY", "main"),
            ("PIPELINE_UPDATE", "main"),
            ("TAILOR_COMPLETE", "main"),
            ("DEVFLOW_APPROVAL_REQUEST", "main"),
        ),
    )
    def test_results_and_notifications_wake_nobody(self, message_type, destination):
        assert route_mailbox(message_type, destination, {}) == ()

    def test_unknown_type_wakes_nobody(self):
        assert route_mailbox("SOMETHING_NEW", "matcher", {}) == ()

    def test_unknown_destination_wakes_nobody(self):
        """A known request type in the wrong inbox is misrouted, not work."""
        assert route_mailbox("SCORE_REQUEST", "curator", {}) == ()


class TestDestinationIsAuthoritative:
    def test_sender_suffix_is_never_used_for_routing(self):
        """`SCORE_REQUEST_tracker` in tailor/inbox must not wake the matcher."""
        assert route_mailbox("SCORE_REQUEST", "tailor", {}) == ()

    def test_same_type_different_destination_differs(self):
        assert route_mailbox("TAILOR_REQUEST", "tailor", {}) == ("jobflow.tailor.generate",)
        assert route_mailbox("TAILOR_REQUEST", "matcher", {}) == ()


class TestNormalisation:
    def test_case_and_whitespace_are_normalised(self):
        assert route_mailbox("  score_request ", " Matcher ", {}) == ("cron.jobflow.matcher",)

    @pytest.mark.parametrize("bad", (None, "", "   ", 42))
    def test_malformed_inputs_wake_nobody(self, bad):
        assert route_mailbox(bad, "matcher", {}) == ()
        assert route_mailbox("SCORE_REQUEST", bad, {}) == ()


class TestActivation:
    def test_activation_is_frozen_and_validated(self):
        a = Activation(
            activity_id="cron.jobflow.matcher", profile="main",
            message_key="matcher/inbox/m1.json", correlation_id="c1",
            reason="mailbox_message",
        )
        assert a.activity_id == "cron.jobflow.matcher"
        with pytest.raises(AttributeError):
            a.activity_id = "other"

    @pytest.mark.parametrize("field", ["activity_id", "profile", "message_key", "reason"])
    def test_blank_identity_is_rejected(self, field):
        values = dict(activity_id="a", profile="main", message_key="k",
                      correlation_id="c", reason="r")
        values[field] = "  "
        with pytest.raises(ValueError, match=field):
            Activation(**values)

    def test_every_routed_activity_exists_in_the_policy_registry(self):
        """A route naming an activity with no policy is a silent coverage hole."""
        from activity_policy.registry import ActivityRegistry
        from jobflow_dispatch.contracts import ROUTES

        registry = ActivityRegistry.load_default()
        routed = {aid for targets in ROUTES.values() for aid in targets}
        missing = sorted(a for a in routed if a not in registry.policies)
        assert missing == [], f"routes reference unknown activities: {missing}"
