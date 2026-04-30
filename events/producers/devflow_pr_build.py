"""Emitter helpers for DEVFLOW_PR_* and DEVFLOW_BUILD_* event types.

Added 2026-04-30 alongside the new event types so any future producer
(GitHub poller, DevFlow API extension, webhook receiver, manual trigger)
can emit type-correct events without re-deriving the schema. The
helpers are deliberately thin -- one wrapper around ``EventBus.emit``
per event type, with a small ``_require`` check so a missing run_id
doesn't reach the bus and dead-letter at the bridge.

State-transition detection is bundled in ``PrBuildStateTracker`` so a
polling caller (cron job, reconciler) can observe(state) on every tick
and have the tracker emit only when the observed state differs from
the last persisted state. The tracker reads/writes its JSON state file
on each call, so a fresh instance picks up where the previous tick
left off -- safe across cron-boundary process restarts.

This module does NOT implement a polling producer of its own. Where
PR/build state actually lives (GitHub vs. DevFlow API extension vs. a
mailbox feed) is a separate decision; this module just gives that
producer a stable interface to call.

Spec: docs/superpowers/specs/2026-04-30-devflow-pr-build-events.md.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Iterable, List, Optional

from events.bus import EventBus
from events.schema import EventType, Priority

logger = logging.getLogger(__name__)


class DevflowProducerPayloadError(ValueError):
    """Raised when a required field is missing from an emitter call.

    Mirrors ``BridgePayloadError`` in ``hermes_to_devflow.py`` so the
    failure mode is identical at producer and consumer side: a clear
    ValueError that the caller can catch and dead-letter without
    surprising the bus.
    """


def _require(name: str, value: Any) -> None:
    if value in (None, "", [], {}):
        raise DevflowProducerPayloadError(f"missing required field: {name}")


def _emit(
    bus: EventBus,
    event_type: EventType,
    source: str,
    payload: dict,
    correlation_id: Optional[str],
) -> str:
    return bus.emit(
        event_type=event_type,
        source=source,
        payload=payload,
        priority=event_type.default_priority,
        correlation_id=correlation_id,
    )


# ---------------------------------------------------------------- PR events

def emit_pr_opened(
    bus: EventBus,
    *,
    run_id: str,
    pr_number: int,
    repo: str,
    title: str,
    author: str,
    branch: Optional[str] = None,
    base_branch: Optional[str] = None,
    url: Optional[str] = None,
    source: str = "devflow-pr-build",
    **extra: Any,
) -> str:
    """Emit DEVFLOW_PR_OPENED. NORMAL priority -- batched if firehose
    verbosity is significant_only, otherwise delivered immediately."""
    _require("run_id", run_id)
    _require("pr_number", pr_number)
    _require("repo", repo)
    _require("title", title)
    _require("author", author)
    payload = {
        "run_id": run_id,
        "pr_number": pr_number,
        "repo": repo,
        "title": title,
        "author": author,
    }
    if branch:
        payload["branch"] = branch
    if base_branch:
        payload["base_branch"] = base_branch
    if url:
        payload["url"] = url
    payload.update(extra)
    return _emit(bus, EventType.DEVFLOW_PR_OPENED, source, payload, run_id)


def emit_pr_merged(
    bus: EventBus,
    *,
    run_id: str,
    pr_number: int,
    repo: str,
    title: str,
    merged_by: Optional[str] = None,
    commits_count: Optional[int] = None,
    url: Optional[str] = None,
    source: str = "devflow-pr-build",
    **extra: Any,
) -> str:
    """Emit DEVFLOW_PR_MERGED. HIGH priority -- always surfaces in
    significant_only verbosity; cross-checked by tests to land in
    devflow_firehose so successful merges are visible without paging
    the user."""
    _require("run_id", run_id)
    _require("pr_number", pr_number)
    _require("repo", repo)
    _require("title", title)
    payload = {
        "run_id": run_id,
        "pr_number": pr_number,
        "repo": repo,
        "title": title,
    }
    if merged_by:
        payload["merged_by"] = merged_by
    if commits_count is not None:
        payload["commits_count"] = commits_count
    if url:
        payload["url"] = url
    payload.update(extra)
    return _emit(bus, EventType.DEVFLOW_PR_MERGED, source, payload, run_id)


def emit_pr_closed(
    bus: EventBus,
    *,
    run_id: str,
    pr_number: int,
    repo: str,
    title: str,
    closed_by: Optional[str] = None,
    reason: Optional[str] = None,
    url: Optional[str] = None,
    source: str = "devflow-pr-build",
    **extra: Any,
) -> str:
    """Emit DEVFLOW_PR_CLOSED (closed without merge). NORMAL priority --
    closed-no-merge is informational; surface in firehose at default
    verbosity. ``reason`` is free-form (``superseded`` / ``stale`` /
    ``invalid``) so future filtering can distinguish abandons from
    intentional rejects."""
    _require("run_id", run_id)
    _require("pr_number", pr_number)
    _require("repo", repo)
    _require("title", title)
    payload = {
        "run_id": run_id,
        "pr_number": pr_number,
        "repo": repo,
        "title": title,
    }
    if closed_by:
        payload["closed_by"] = closed_by
    if reason:
        payload["reason"] = reason
    if url:
        payload["url"] = url
    payload.update(extra)
    return _emit(bus, EventType.DEVFLOW_PR_CLOSED, source, payload, run_id)


def emit_pr_review_requested(
    bus: EventBus,
    *,
    run_id: str,
    pr_number: int,
    repo: str,
    title: str,
    reviewers: Optional[Iterable[str]] = None,
    url: Optional[str] = None,
    source: str = "devflow-pr-build",
    **extra: Any,
) -> str:
    """Emit DEVFLOW_PR_REVIEW_REQUESTED. HIGH priority -- a human-
    action signal (``please review this``) routes to devflow_decisions,
    NOT firehose. Mirrors how interview_signal / offer_signal route to
    jobflow_decisions."""
    _require("run_id", run_id)
    _require("pr_number", pr_number)
    _require("repo", repo)
    _require("title", title)
    payload = {
        "run_id": run_id,
        "pr_number": pr_number,
        "repo": repo,
        "title": title,
    }
    if reviewers:
        payload["reviewers"] = list(reviewers)
    if url:
        payload["url"] = url
    payload.update(extra)
    return _emit(bus, EventType.DEVFLOW_PR_REVIEW_REQUESTED, source, payload, run_id)


# ---------------------------------------------------------------- build events

def emit_build_started(
    bus: EventBus,
    *,
    run_id: str,
    build_id: str,
    build_name: str,
    repo: str,
    branch: Optional[str] = None,
    commit_sha: Optional[str] = None,
    url: Optional[str] = None,
    source: str = "devflow-pr-build",
    **extra: Any,
) -> str:
    """Emit DEVFLOW_BUILD_STARTED. LOW priority -- batched per
    TelegramNotifier's low-priority window (5 min) so a noisy CI
    that starts every commit doesn't flood firehose."""
    _require("run_id", run_id)
    _require("build_id", build_id)
    _require("build_name", build_name)
    _require("repo", repo)
    payload = {
        "run_id": run_id,
        "build_id": build_id,
        "build_name": build_name,
        "repo": repo,
    }
    if branch:
        payload["branch"] = branch
    if commit_sha:
        payload["commit_sha"] = commit_sha
    if url:
        payload["url"] = url
    payload.update(extra)
    return _emit(bus, EventType.DEVFLOW_BUILD_STARTED, source, payload, run_id)


def emit_build_succeeded(
    bus: EventBus,
    *,
    run_id: str,
    build_id: str,
    build_name: str,
    repo: str,
    duration_seconds: Optional[float] = None,
    branch: Optional[str] = None,
    commit_sha: Optional[str] = None,
    url: Optional[str] = None,
    source: str = "devflow-pr-build",
    **extra: Any,
) -> str:
    """Emit DEVFLOW_BUILD_SUCCEEDED. NORMAL priority -- delivered
    immediately. Carries duration so the firehose body shows wall-clock
    cost (operators tracking CI bloat)."""
    _require("run_id", run_id)
    _require("build_id", build_id)
    _require("build_name", build_name)
    _require("repo", repo)
    payload = {
        "run_id": run_id,
        "build_id": build_id,
        "build_name": build_name,
        "repo": repo,
    }
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
    if branch:
        payload["branch"] = branch
    if commit_sha:
        payload["commit_sha"] = commit_sha
    if url:
        payload["url"] = url
    payload.update(extra)
    return _emit(bus, EventType.DEVFLOW_BUILD_SUCCEEDED, source, payload, run_id)


def emit_build_failed(
    bus: EventBus,
    *,
    run_id: str,
    build_id: str,
    build_name: str,
    repo: str,
    duration_seconds: Optional[float] = None,
    error_summary: str = "",
    exit_code: Optional[int] = None,
    branch: Optional[str] = None,
    commit_sha: Optional[str] = None,
    url: Optional[str] = None,
    source: str = "devflow-pr-build",
    **extra: Any,
) -> str:
    """Emit DEVFLOW_BUILD_FAILED. HIGH priority -- routes to
    devflow_decisions (someone needs to look at this build). Carries
    error_summary so the firehose body has actionable context without
    a deep link."""
    _require("run_id", run_id)
    _require("build_id", build_id)
    _require("build_name", build_name)
    _require("repo", repo)
    payload = {
        "run_id": run_id,
        "build_id": build_id,
        "build_name": build_name,
        "repo": repo,
        "error_summary": error_summary,
    }
    if duration_seconds is not None:
        payload["duration_seconds"] = duration_seconds
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if branch:
        payload["branch"] = branch
    if commit_sha:
        payload["commit_sha"] = commit_sha
    if url:
        payload["url"] = url
    payload.update(extra)
    return _emit(bus, EventType.DEVFLOW_BUILD_FAILED, source, payload, run_id)


# ---------------------------------------------------------------- state tracker

# Mapping from observed PR state strings to the emitter that should
# fire on transition. Strings are normalised (lowercased) before lookup
# so producers can pass through GitHub / DevFlow API values unchanged.
_PR_STATE_EMITTERS = {
    "open": "devflow.pr_opened",
    "opened": "devflow.pr_opened",
    "merged": "devflow.pr_merged",
    "closed": "devflow.pr_closed",
    "review_requested": "devflow.pr_review_requested",
    "ready_for_review": "devflow.pr_review_requested",
}

_BUILD_STATE_EMITTERS = {
    "running": "devflow.build_started",
    "started": "devflow.build_started",
    "in_progress": "devflow.build_started",
    "queued": "devflow.build_started",
    "succeeded": "devflow.build_succeeded",
    "passed": "devflow.build_succeeded",
    "success": "devflow.build_succeeded",
    "failed": "devflow.build_failed",
    "failure": "devflow.build_failed",
    "errored": "devflow.build_failed",
}


class PrBuildStateTracker:
    """Persists last-observed state per (entity_kind, repo, id) so a
    polling producer can fire transition events without duplicating.

    Storage shape (JSON):
        {
            "pr:hermes-agent:42": "open",
            "build:hermes-agent:b1": "succeeded"
        }

    The file is read on every observe() call (so a fresh tracker after
    a cron-boundary process restart sees prior state) and written back
    only when state changed.
    """

    def __init__(self, state_path: Path):
        self._state_path = Path(state_path)

    def _load(self) -> dict:
        if not self._state_path.exists():
            return {}
        try:
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(
                "PrBuildStateTracker: state file unreadable (%s); treating as empty",
                e,
            )
            return {}

    def _save(self, state: dict) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(state, indent=2), encoding="utf-8")

    def observe_pr(
        self,
        bus: EventBus,
        *,
        repo: str,
        pr_number: int,
        state: str,
        run_id: str,
        title: str,
        author: Optional[str] = None,
        **extra: Any,
    ) -> Optional[str]:
        """Record the current observed PR state. If it differs from the
        last persisted state, emit the transition event and return its
        type_string. If no transition, return None.

        ``state`` accepts the values in ``_PR_STATE_EMITTERS``; unknown
        states are persisted but do not fire an event (so the next
        recognisable transition still triggers).
        """
        key = f"pr:{repo}:{pr_number}"
        normalised = (state or "").lower().strip()
        emit_target = _PR_STATE_EMITTERS.get(normalised)

        store = self._load()
        prior = store.get(key)
        if prior == normalised:
            return None

        store[key] = normalised
        self._save(store)

        if emit_target is None:
            return None

        # Map to the right helper. ``author`` is required by emit_pr_opened
        # but not by the others; default to "unknown" so a polling
        # producer that lacks the field still gets a fire.
        if emit_target == "devflow.pr_opened":
            emit_pr_opened(
                bus, run_id=run_id, pr_number=pr_number, repo=repo,
                title=title, author=author or "unknown", **extra,
            )
        elif emit_target == "devflow.pr_merged":
            emit_pr_merged(
                bus, run_id=run_id, pr_number=pr_number, repo=repo,
                title=title, **extra,
            )
        elif emit_target == "devflow.pr_closed":
            emit_pr_closed(
                bus, run_id=run_id, pr_number=pr_number, repo=repo,
                title=title, **extra,
            )
        elif emit_target == "devflow.pr_review_requested":
            emit_pr_review_requested(
                bus, run_id=run_id, pr_number=pr_number, repo=repo,
                title=title, **extra,
            )
        return emit_target

    def observe_build(
        self,
        bus: EventBus,
        *,
        repo: str,
        build_id: str,
        state: str,
        run_id: str,
        build_name: str,
        **extra: Any,
    ) -> Optional[str]:
        """Record the current observed build state. If it differs from
        the last persisted state, emit the transition event and return
        its type_string. If no transition, return None."""
        key = f"build:{repo}:{build_id}"
        normalised = (state or "").lower().strip()
        emit_target = _BUILD_STATE_EMITTERS.get(normalised)

        store = self._load()
        prior = store.get(key)
        if prior == normalised:
            return None

        store[key] = normalised
        self._save(store)

        if emit_target is None:
            return None

        if emit_target == "devflow.build_started":
            emit_build_started(
                bus, run_id=run_id, build_id=build_id, build_name=build_name,
                repo=repo, **extra,
            )
        elif emit_target == "devflow.build_succeeded":
            emit_build_succeeded(
                bus, run_id=run_id, build_id=build_id, build_name=build_name,
                repo=repo, **extra,
            )
        elif emit_target == "devflow.build_failed":
            # error_summary is required-shaped; let extra override the default.
            extra.setdefault("error_summary", "")
            emit_build_failed(
                bus, run_id=run_id, build_id=build_id, build_name=build_name,
                repo=repo, **extra,
            )
        return emit_target


__all__ = [
    "DevflowProducerPayloadError",
    "PrBuildStateTracker",
    "emit_pr_opened",
    "emit_pr_merged",
    "emit_pr_closed",
    "emit_pr_review_requested",
    "emit_build_started",
    "emit_build_succeeded",
    "emit_build_failed",
]
