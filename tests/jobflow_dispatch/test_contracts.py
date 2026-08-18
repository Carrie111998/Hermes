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
            ("SUBMIT_REQUEST", "applier", ("cron.jobflow.applier",)),
            ("SUBMIT_CONFIRM", "applier", ("cron.jobflow.applier",)),
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


class TestMessageKeyNormalisation:
    """One physical file must produce ONE ledger key, whatever built it.

    MailboxWatcher derives its key from ``Path.relative_to()``, which on
    Windows yields backslashes; the reconciler assembles its own from the
    destination and filename. Two spellings of the same file mean two ledger
    rows, and the reconciler re-dispatches work the subscriber already
    claimed — duplicate model calls with no error anywhere.
    """

    def test_separators_are_normalised(self):
        from jobflow_dispatch.contracts import message_key

        assert message_key("tailor" + chr(92) + "inbox" + chr(92) + "x.json") == "tailor/inbox/x.json"
        assert message_key("tailor/inbox/x.json") == "tailor/inbox/x.json"

    def test_watcher_and_reconciler_spellings_agree(self):
        from pathlib import Path

        from jobflow_dispatch.contracts import message_key

        watcher_style = str(Path("tailor") / "inbox" / "x.json")   # OS-native
        reconciler_style = "tailor/inbox/x.json"
        assert message_key(watcher_style) == message_key(reconciler_style)

    def test_mixed_separators_collapse(self):
        from jobflow_dispatch.contracts import message_key

        assert message_key("tailor" + chr(92) + "inbox/x.json") == "tailor/inbox/x.json"

    @pytest.mark.parametrize("bad", (None, "", "   ", 7))
    def test_unusable_keys_are_rejected(self, bad):
        from jobflow_dispatch.contracts import message_key

        with pytest.raises(ValueError, match="message_key"):
            message_key(bad)


class TestTrackerIsDeliberatelyUnrouted:
    """Measured decision, not an oversight — keep it from being "fixed" later.

    The tracker absorbs ~467 messages/7d in ~71 bursts using ~5.4 runs/day.
    Per-burst activation would raise that to ~10/day, so event dispatch is a
    REGRESSION for this worker. Its traffic is also largely handled without a
    model already: APPROVAL_INTENT / STATE_TRANSITION_INTENT go to the
    IntentApplier, and the operator PIPELINE_UPDATE mirrors are drained every
    5 minutes by the zero-LLM tracker-operator-drain script.

    See docs/operations/jobflow-event-dispatch.md for the measurement.
    """

    def test_no_route_targets_the_tracker_worker(self):
        from jobflow_dispatch.contracts import ROUTES

        destinations = {dest for _mtype, dest in ROUTES}
        assert "tracker" not in destinations

        targets = {aid for aids in ROUTES.values() for aid in aids}
        assert not any("tracker" in aid for aid in targets), sorted(targets)

    @pytest.mark.parametrize(
        "message_type",
        ("PIPELINE_UPDATE", "SCOUT_DISCOVERY", "VIP_DISCOVERY",
         "APPROVAL_INTENT", "STATE_TRANSITION_INTENT", "STATUS_REQUEST"),
    )
    def test_tracker_traffic_wakes_nobody(self, message_type):
        from jobflow_dispatch.contracts import route_mailbox

        assert route_mailbox(message_type, "tracker", {}) == ()


class TestApplierRoutesOnTheTypeThatActuallyArrives:
    """SUBMIT_REQUEST is the applier's only observed inbound work.

    The lane was routed solely on SUBMIT_CONFIRM and QUESTION_ANSWER, neither
    of which any component produces — every message in ``applier/inbox`` since
    July is a SUBMIT_REQUEST. The lane was therefore unreachable on the event
    path AND in the reconciler, which builds its scanned types from ROUTES.

    That is why the 2026-08-17 shadow gate could report 100% recall while the
    applier logged zero dispatches: zero was structural, not idle. No soak of
    any length would have produced a single applier line.
    """

    def test_submit_request_wakes_the_applier(self):
        assert route_mailbox("SUBMIT_REQUEST", "applier", {}) == (
            "cron.jobflow.applier",
        )

    def test_the_reconciler_recognises_it_too(self):
        """Both paths must agree; the reconciler derives its types from ROUTES."""
        from jobflow_dispatch.reconcile import _TYPES, DESTINATIONS, _message_type

        assert "SUBMIT_REQUEST" in _TYPES
        assert "applier" in DESTINATIONS
        assert _message_type(
            "20260720T100927Z_SUBMIT_REQUEST_main_8446590b.json"
        ) == "SUBMIT_REQUEST"

    def test_submit_request_elsewhere_still_wakes_nobody(self):
        """The destination stays authoritative — this is not a global unlock."""
        for destination in ("matcher", "tailor", "researcher", "tracker", "main"):
            assert route_mailbox("SUBMIT_REQUEST", destination, {}) == ()


class TestEveryRouteHasAnEventPath:
    """A route whose type is not mirrored can never fire.

    ``MailboxWatcher`` emits events only for ``MIRRORED_MESSAGE_TYPES``, so a
    route keyed on anything else is dead on the event path — and dead in the
    reconciler too, since it derives its scanned types from the same table.
    Nothing reports this at runtime: the lane just reads zero forever, which
    looks like an idle worker rather than an unreachable one.

    Two types are knowingly unmirrored. Widening the watcher also changes
    notification delivery, so it stays a deliberate separate decision.
    """

    #: Routed types with no event path. Each is a standing decision, not a bug.
    KNOWN_UNMIRRORED = {"RESEARCH_REQUEST", "QUESTION_ANSWER"}

    def test_routed_types_are_mirrored_or_recorded_as_dead(self):
        from events.producers.mailbox_watcher import MIRRORED_MESSAGE_TYPES
        from jobflow_dispatch.contracts import ROUTES

        routed = {mtype for mtype, _dest in ROUTES}
        dead = sorted(routed - set(MIRRORED_MESSAGE_TYPES) - self.KNOWN_UNMIRRORED)
        assert dead == [], (
            f"routes with no event path: {dead}. Either add the type to "
            "MailboxWatcher.MIRRORED_MESSAGE_TYPES, or record it in "
            "KNOWN_UNMIRRORED with the reason it stays dark."
        )

    def test_allowlist_shrinks_when_a_type_becomes_mirrored(self):
        """Otherwise the allowlist rots into a permanent false exemption."""
        from events.producers.mailbox_watcher import MIRRORED_MESSAGE_TYPES

        mirrored_now = sorted(self.KNOWN_UNMIRRORED & set(MIRRORED_MESSAGE_TYPES))
        assert mirrored_now == [], (
            f"{mirrored_now} is mirrored now — drop it from KNOWN_UNMIRRORED"
        )

    def test_allowlist_only_names_types_that_are_routed(self):
        """A stale entry protects nothing and misleads the next reader."""
        from jobflow_dispatch.contracts import ROUTES

        routed = {mtype for mtype, _dest in ROUTES}
        stale = sorted(self.KNOWN_UNMIRRORED - routed)
        assert stale == [], f"allowlist names unrouted types: {stale}"
