"""Canonical agent-source mapping for failure-cluster deduplication.

Both ``CronEventEmitter`` (cron exit codes) and ``MailboxTranslator``
(structured ERROR mailbox messages) emit ``AGENT_FAILURE_CLUSTER`` events
when their respective ``FailureClusterDetector`` instances cross the
3-in-a-row threshold.  Without normalisation, the same underlying agent
failure surfaces under two different ``source`` strings — for example
``'jobflow-applier'`` from the cron path and ``'applier'`` from the
mailbox path — defeating the cluster detector's per-source state and
doubling the Telegram noise on ``#watchdog_alerts``.

This module is the single source of truth for collapsing both styles of
input into one canonical agent identity.

Background:
    profiles/critic/workspace/watchdog-dedup-proposal-2026-04-29.md
    profiles/critic/workspace/cluster-triage-applier-2026-04-29.md
"""

from typing import Optional

# Canonical agent identities recognised across Hermes.  When a producer
# emits a cluster event, the source field MUST equal one of these (or be
# returned verbatim if the producer is some genuinely-unknown caller —
# see Rule 5 in canonical_agent_source).
#
# The set captures the actual cron + mailbox surface as of 2026-04-29:
# every entry in profiles/main/cron/jobs.json reduces to one of these,
# and every ``source_agent`` value emitted by mailbox messages already
# uses one of these short names.
_CANONICAL_AGENTS = frozenset({
    # Job-pipeline agents
    "applier",
    "archiver",
    "matcher",
    "notifier",
    "scout",
    "sentinel",
    "tailor",
    "tracker",
    # Hermes core / meta
    "critic",
    "curator",
    "devflow",
    "evey",
    "jaum",
    "main",
    "scribe",
    "watchdog",
    # Multi-segment canonical identities — must NOT split on the first dash
    "cv-handler",
    "learning-loop",
    "mempalace",
    "postgres-sync",
})

_JOBFLOW_PREFIX = "jobflow-"


def canonical_agent_source(raw: Optional[str]) -> str:
    """Return the canonical agent identity for a raw cron-job or mailbox
    source-agent value.

    Examples:
        ``'jobflow-applier'`` -> ``'applier'``
        ``'applier'`` -> ``'applier'``
        ``'sentinel-vip-evening'`` -> ``'sentinel'``
        ``'critic-weekly-retro'`` -> ``'critic'``
        ``'jobflow-tracker-cycle'`` -> ``'tracker'``
        ``'jobflow-cv-handler'`` -> ``'cv-handler'``
        ``'learning-loop'`` -> ``'learning-loop'``
        ``'learning-loop-extra'`` -> ``'learning-loop'``
        ``''`` / ``None`` / ``'   '`` -> ``'unknown'``
        ``'mystery-agent'`` -> ``'mystery-agent'`` (no false collapse)

    The function is idempotent: ``f(f(x)) == f(x)`` for every ``x``.
    """
    if raw is None:
        return "unknown"
    raw = raw.strip()
    if not raw:
        return "unknown"

    # Rule 1: already canonical — return verbatim.  Covers both short
    # names ('applier') and multi-segment canonicals ('cv-handler').
    if raw in _CANONICAL_AGENTS:
        return raw

    # Rule 2: 'jobflow-<X>[-...]' — strip the prefix and collapse the
    # remainder to its canonical agent.  Look for a 2-segment match
    # before a 1-segment match so 'jobflow-cv-handler' returns
    # 'cv-handler', not 'cv'.
    if raw.startswith(_JOBFLOW_PREFIX):
        rest = raw[len(_JOBFLOW_PREFIX):]
        match = _longest_canonical_prefix(rest)
        if match:
            return match
        # Fall back: first segment of the remainder.  Preserves data
        # rather than discarding (e.g., 'jobflow-foo' -> 'foo').
        first = rest.split("-", 1)[0]
        return first or raw

    # Rule 3: '<canonical>-...' — collapse to the canonical when the
    # leading segment(s) form a known agent.  Multi-segment canonicals
    # take precedence over single-segment to keep 'learning-loop-extra'
    # collapsing to 'learning-loop' rather than dying off as 'learning'.
    match = _longest_canonical_prefix(raw)
    if match:
        return match

    # Rule 4: leave verbatim.  Unknown shapes (e.g., 'Pipeline Drift Audit',
    # ad-hoc ops cron names) must never collapse onto an existing agent.
    return raw


def _longest_canonical_prefix(value: str) -> Optional[str]:
    """Return the longest canonical agent that is a dash-separated prefix
    of ``value``, or None if no canonical prefix matches.

    'cv-handler-fallback' -> 'cv-handler'
    'sentinel-vip-evening' -> 'sentinel'
    'learning-loop' (already canonical, but exposed via Rule 1) -> 'learning-loop'
    'foo-bar' -> None
    """
    parts = value.split("-")
    # Try longest-first so multi-segment canonicals beat single-segment.
    for segment_count in range(len(parts), 0, -1):
        candidate = "-".join(parts[:segment_count])
        if candidate in _CANONICAL_AGENTS:
            # Don't accept a same-length match unless it consumes the
            # whole input — otherwise this would say 'sentinel' is a
            # prefix of 'sentinel' and break Rule 1's already-canonical
            # branch.  Rule 1 handles that case; this helper is only
            # called for strict prefixes (length < len(parts)) OR exact
            # match.  Both are fine to return.
            return candidate
    return None
