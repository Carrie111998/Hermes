"""Tests for events.producers.agent_source_mapping.canonical_agent_source.

The mapping function normalises cron-job-name and mailbox source-agent
values into a single canonical agent identity, so the failure-cluster
detector and downstream Telegram dedup can treat both producers'
``AGENT_FAILURE_CLUSTER`` events as a single signal.

Background: see ``profiles/critic/workspace/watchdog-dedup-proposal-2026-04-29.md``
(Option A). The Applier triage 2026-04-29 found cron and mailbox paths
emitting cluster events with different ``source`` strings
(``'jobflow-applier'`` vs ``'applier'``) for the same underlying failure,
doubling Telegram noise on ``#watchdog_alerts``.
"""

import pytest

from events.producers.agent_source_mapping import canonical_agent_source


class TestCanonicalAgentSourceCronJobNames:
    """Cron-emitter side: cron job names from ``profiles/main/cron/jobs.json``
    must collapse to the canonical agent name that the mailbox path uses."""

    @pytest.mark.parametrize("raw,expected", [
        # jobflow-X[-...] -> X
        ("jobflow-applier", "applier"),
        ("jobflow-tailor", "tailor"),
        ("jobflow-scout", "scout"),
        ("jobflow-notifier", "notifier"),
        ("jobflow-matcher", "matcher"),
        ("jobflow-archiver", "archiver"),
        # jobflow-tracker-* family
        ("jobflow-tracker-cycle", "tracker"),
        ("jobflow-tracker-followup", "tracker"),
        ("jobflow-tracker-weekly", "tracker"),
        # jobflow-matcher-shadow* family
        ("jobflow-matcher-shadow", "matcher"),
        ("jobflow-matcher-shadow-diff", "matcher"),
    ])
    def test_jobflow_prefix_strips_to_canonical(self, raw, expected):
        assert canonical_agent_source(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        # sentinel-vip-* family collapses to 'sentinel'
        ("sentinel-vip-evening", "sentinel"),
        ("sentinel-vip-midday", "sentinel"),
        ("sentinel-vip-morning", "sentinel"),
        # critic-* family collapses to 'critic'
        ("critic-proposal-emit", "critic"),
        ("critic-reasoning-effort-tune", "critic"),
        ("critic-skill-review", "critic"),
        ("critic-weekly-retro", "critic"),
        # curator-*, scribe-*, devflow-*, jaum-*
        ("curator-nightly", "curator"),
        ("scribe-am", "scribe"),
        ("scribe-pm", "scribe"),
        ("scribe-weekly", "scribe"),
        ("devflow-bridge", "devflow"),
        ("devflow-standup", "devflow"),
        ("jaum-daytime-relay", "jaum"),
        ("jaum-inbox-sweeper", "jaum"),
    ])
    def test_canonical_prefix_dash_collapses(self, raw, expected):
        assert canonical_agent_source(raw) == expected


class TestCanonicalAgentSourceMailboxNames:
    """Mailbox-translator side: ``source_agent`` values are typically
    already-canonical short names. They must pass through unchanged."""

    @pytest.mark.parametrize("name", [
        "applier", "tailor", "scout", "matcher", "tracker",
        "notifier", "sentinel", "critic", "curator", "scribe",
        "devflow", "jaum", "main",
    ])
    def test_already_canonical_passes_through(self, name):
        assert canonical_agent_source(name) == name


class TestCanonicalAgentSourceMultiSegmentNames:
    """Some agent identities legitimately contain a hyphen
    (``cv-handler``, ``learning-loop``, ``postgres-sync``). The mapper
    must NOT split them on the first dash."""

    @pytest.mark.parametrize("name", [
        "cv-handler",
        "learning-loop",
        "postgres-sync",
    ])
    def test_multi_segment_canonical_passes_through(self, name):
        assert canonical_agent_source(name) == name

    @pytest.mark.parametrize("raw,expected", [
        # If a multi-segment canonical is a prefix of a longer cron name,
        # the longer name should still collapse to the canonical.
        ("learning-loop-extra", "learning-loop"),
        ("cv-handler-fallback", "cv-handler"),
        # And the jobflow- prefix should respect multi-segment canonicals.
        ("jobflow-cv-handler", "cv-handler"),
    ])
    def test_multi_segment_canonical_collapses_longer(self, raw, expected):
        assert canonical_agent_source(raw) == expected


class TestCanonicalAgentSourceUnknown:
    """Unknown / malformed inputs: never collapse, never raise."""

    @pytest.mark.parametrize("value,expected", [
        ("", "unknown"),
        (None, "unknown"),
        ("   ", "unknown"),
    ])
    def test_empty_or_none_returns_unknown(self, value, expected):
        assert canonical_agent_source(value) == expected

    def test_unknown_singleton_returns_verbatim(self):
        # 'foo' is not in the canonical set; do not invent a mapping.
        assert canonical_agent_source("foo") == "foo"

    def test_unknown_compound_returns_verbatim(self):
        # 'mystery-agent' has 'mystery' which isn't canonical;
        # do NOT collapse to 'mystery'.
        assert canonical_agent_source("mystery-agent") == "mystery-agent"

    def test_pipeline_drift_audit_returns_verbatim(self):
        # 'Pipeline Drift Audit' is a real cron name but not an agent.
        # It must not match any canonical mapping.
        assert canonical_agent_source("Pipeline Drift Audit") == (
            "Pipeline Drift Audit"
        )


class TestCanonicalAgentSourceIdempotent:
    """Calling the mapper twice on the same input must give the same
    result. This is what makes the mapper safe to apply at both the
    record() call site AND the bus-emit call site without double-collapse."""

    @pytest.mark.parametrize("raw", [
        "jobflow-applier",
        "sentinel-vip-evening",
        "applier",
        "learning-loop",
        "unknown",
        "Pipeline Drift Audit",
    ])
    def test_idempotent(self, raw):
        once = canonical_agent_source(raw)
        twice = canonical_agent_source(once)
        assert once == twice


class TestCanonicalAgentSourceProposalExamples:
    """Direct mirror of the examples enumerated in the proposal doc
    (see ``profiles/critic/workspace/watchdog-dedup-proposal-2026-04-29.md``,
    section 'Option A')."""

    @pytest.mark.parametrize("raw,expected", [
        ("jobflow-applier", "applier"),
        ("applier", "applier"),
        ("jobflow-tailor", "tailor"),
        ("tailor", "tailor"),
        ("sentinel-vip-evening", "sentinel"),
        ("sentinel-vip-midday", "sentinel"),
        ("jaum-daytime-relay", "jaum"),
    ])
    def test_proposal_examples(self, raw, expected):
        assert canonical_agent_source(raw) == expected
