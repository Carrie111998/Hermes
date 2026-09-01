"""Per-attempt recovery bookkeeping for the conversation turn loop.

The inner retry loop in ``run_conversation`` (``while retry_count <
max_retries``) makes several distinct recovery attempts on a single model API
call: a credential-pool 429 retry, a per-provider OAuth refresh (codex,
anthropic, nous, copilot), a long-context compression restart, a length-
continuation restart, and a handful of format-recovery branches (thinking-
signature stripping, multimodal-tool-content stripping, llama.cpp grammar
fallback, image shrink, invalid-encrypted-content, 1M-beta header).

Each of those branches is guarded by a one-shot boolean so it fires at most
once per attempt. They used to be ~16 bare ``*_attempted`` / ``has_retried_*``
/ ``restart_with_*`` locals declared inline before the loop and threaded
through its 2,400-line body. ``TurnRetryState`` collapses them into one object
the loop mutates in place (``state.codex_auth_retry_attempted = True``), giving
the recovery bookkeeping a single named, testable home.

Loop-control variables (``retry_count``, ``max_retries``,
``max_compression_attempts``) intentionally stay as plain locals — they are the
``while`` mechanics, not recovery bookkeeping, and putting them on the object
would add indirection without clarifying anything.

This module is dependency-free so it can be unit-tested in isolation and
imported by the turn loop without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from enum import Enum


class RecoveryOutcome(str, Enum):
    """The only ways a completed provider-attempt can leave the retry loop."""

    SUCCESS = "success"
    REBUILD = "rebuild"
    COMPRESS = "compress"
    INTERRUPT = "interrupt"
    TERMINATE = "terminate"


@dataclass(frozen=True)
class RecoveryDecision:
    """A recovery transition selected after one provider-attempt."""

    outcome: RecoveryOutcome
    reason: str = ""


@dataclass
class TurnRetryState:
    """One-shot recovery guards + restart signals for a single API-call attempt.

    A fresh instance is created for each iteration of the outer turn loop
    (once per ``api_call_count``). Each guard fires its recovery branch at most
    once; the ``restart_with_*`` signals are read by the loop after the attempt
    to decide whether to rebuild the request and retry.
    """

    # ── Per-provider OAuth / credential refresh guards ───────────────────
    codex_auth_retry_attempted: bool = False
    anthropic_auth_retry_attempted: bool = False
    nous_auth_retry_attempted: bool = False
    nous_paid_entitlement_refresh_attempted: bool = False
    copilot_auth_retry_attempted: bool = False
    # Copilot surfaces a stale/degraded credential as a 400
    # ``model_not_available_for_integrator`` / ``model_not_supported`` instead
    # of a clean 401 (e.g. a raw OAuth token seeded when the token exchange
    # degraded at startup, routing the request to the restricted
    # ``copilot-language-server`` integrator). Guard a single-shot forced
    # re-exchange + client rebuild for that case, separate from the 401 guard
    # so both can fire within one attempt if needed.
    copilot_stale_cred_retry_attempted: bool = False
    vertex_auth_retry_attempted: bool = False

    # ── Format / payload recovery guards ─────────────────────────────────
    thinking_sig_retry_attempted: bool = False
    invalid_encrypted_content_retry_attempted: bool = False
    native_compaction_reject_retry_attempted: bool = False
    image_shrink_retry_attempted: bool = False
    multimodal_tool_content_retry_attempted: bool = False
    oauth_1m_beta_retry_attempted: bool = False
    llama_cpp_grammar_retry_attempted: bool = False

    # ── Transport / rate-limit recovery ──────────────────────────────────
    primary_recovery_attempted: bool = False
    has_retried_429: bool = False

    # ── Auth-failure provider failover ───────────────────────────────────
    # Set once we've escalated a persistent 401/403 (after the per-provider
    # credential-refresh attempt above failed) to the fallback chain, so we
    # don't loop on the same auth failover within one attempt.
    auth_failover_attempted: bool = False

    # ── Restart signals (read by the outer loop after the attempt) ───────
    restart_with_compressed_messages: bool = False
    restart_with_length_continuation: bool = False
    # Set when a content-filter stream stall (e.g. MiniMax "new_sensitive")
    # has been escalated to the fallback chain: the partial-stream content
    # was rolled back off ``messages`` and the loop should re-issue the API
    # call against the newly-activated provider (#32421).
    restart_with_rebuilt_messages: bool = False
    # A user correction cancelled the in-flight provider request. The outer
    # loop must append a role-safe checkpoint + user message, rebuild the API
    # payload, and retry the same logical iteration.
    restart_with_redirected_messages: bool = False

    def __iter__(self):
        # Convenience for debugging / tests: iterate (name, value) pairs.
        for f in fields(self):
            yield f.name, getattr(self, f.name)

    def request_rebuild(
        self, *, reason: str = "fallback", reset_primary_recovery: bool = False
    ) -> None:
        """Schedule a fresh provider request after fallback or a redirect."""
        if reason == "redirect":
            self.restart_with_redirected_messages = True
            return
        self.restart_with_rebuilt_messages = True
        if reset_primary_recovery:
            self.primary_recovery_attempted = False

    def request_compression(self) -> None:
        """Schedule a fresh request after compaction rebuilt the transcript."""
        self.restart_with_compressed_messages = True

    def request_length_continuation(self) -> None:
        """Schedule a fresh request with the appended continuation nudge."""
        self.restart_with_length_continuation = True

    def decision(
        self, *, interrupted: bool, has_response: bool
    ) -> RecoveryDecision:
        """Classify the post-attempt transition without mutating state.

        Priority exactly mirrors the former tail of ``run_conversation``: a
        redirected user correction wins, then a terminal interrupt, then a
        compacted/rebuilt request, and only then a missing response terminal.
        """
        if self.restart_with_redirected_messages:
            return RecoveryDecision(RecoveryOutcome.REBUILD, "redirect")
        if interrupted:
            return RecoveryDecision(RecoveryOutcome.INTERRUPT)
        if self.restart_with_compressed_messages:
            return RecoveryDecision(RecoveryOutcome.COMPRESS, "compression")
        if self.restart_with_rebuilt_messages:
            return RecoveryDecision(RecoveryOutcome.REBUILD, "fallback")
        if self.restart_with_length_continuation:
            return RecoveryDecision(RecoveryOutcome.REBUILD, "length")
        if not has_response:
            return RecoveryDecision(RecoveryOutcome.TERMINATE, "no_response")
        return RecoveryDecision(RecoveryOutcome.SUCCESS)

    def consume(self, decision: RecoveryDecision) -> None:
        """Clear the single restart signal consumed by ``decision``."""
        if decision.reason == "redirect":
            self.restart_with_redirected_messages = False
        elif decision.reason == "compression":
            self.restart_with_compressed_messages = False
        elif decision.reason == "fallback":
            self.restart_with_rebuilt_messages = False
        elif decision.reason == "length":
            self.restart_with_length_continuation = False
