"""Monitor-mode cron support — hash-suppressed change detection.

A monitor job attaches a cheap *monitor source* (``monitor_script`` or
``monitor_url``) to an ordinary LLM cron job. Each tick the scheduler runs
the source FIRST and compares a hash of its exact output bytes against the
hash stored from the last agent-triggering tick:

* unchanged → the agent run is suppressed entirely (no LLM, no delivery);
  the tick is recorded as a silent ``no_change`` run.
* changed (or first run) → a "MONITOR CHANGE DETECTED" context block —
  unified diff of old vs new output (capped) plus the new output — is
  injected into the prompt and the agent runs normally.
* source failure → treated as an ERROR, never as a change. The stored hash
  is left untouched so a source that recovers to its previous output still
  suppresses.

Output is compared as EXACT BYTES — no timestamp stripping or whitespace
normalization. Monitor scripts should emit stable output (sort results,
omit "generated at" lines) or every tick will look like a change.

State lives in two places, both durable across scheduler restarts:

* ``job["monitor_state"]`` in jobs.json — ``last_output_hash`` +
  ``last_changed_at`` (additive JSON fields, no migration needed);
* ``OUTPUT_DIR/<job_id>/monitor_last_output.txt`` — the previous output
  text, kept only so the next change can render a diff.

Inspired by: ChatGPT Work monitor tasks (idea-level, docs-only);
enabler: #80774.

Failure-retry semantics (opt-in, ``monitor_retry_on_failure``):

* DEFAULT (field absent/False — legacy): the new hash is committed at
  DETECTION time, before the agent runs. A failed agent run consumes the
  change; the next tick sees the same output as ``no_change`` and stays
  silent. At-most-once alerting per change.
* OPT-IN (``monitor_retry_on_failure: true``): detection stashes the new
  hash in a process-local PENDING slot instead of committing it, and
  ``run_job`` commits it only when the agent run completes successfully
  (``commit_monitor_change``). A failed agent run leaves the stored hash
  untouched, so the next tick re-detects the SAME change and retries the
  agent — at-least-once alerting per change. Opt-in because replay can
  duplicate side effects for jobs whose agent acts on the world; jobs
  that do not opt in keep byte-identical legacy behavior.
* Commit boundary: the hash commits on AGENT-RUN SUCCESS inside
  ``run_job`` — BEFORE delivery. A delivery error afterwards never
  un-commits (the agent already acted on the change; replaying it would
  duplicate side effects, and delivery failure is tracked separately via
  ``last_delivery_error``). A wake-gate skip (``wakeAgent: false``) does
  NOT commit — the agent never ran, so the change stays pending.
* Crash/restart: the pending slot is process-local, so a scheduler crash
  between detection and commit simply re-detects the change on the next
  tick (the stored hash was never touched) — correct by construction.
  A no_change tick (output reverted to the committed output) CLEARS the
  pending slot so an evaporated change cannot be committed stale later.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import threading
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)

# Cap for the unified diff injected into the prompt.
MAX_DIFF_CHARS = 4000
# Cap for the new-output block injected into the prompt (mirrors the 8k
# context_from truncation in cron/scheduler.py).
MAX_OUTPUT_CHARS = 8000
# Bounded GET limits for monitor_url sources.
URL_TIMEOUT_SECONDS = 30
MAX_URL_BYTES = 262_144  # 256 KiB

_SNAPSHOT_FILENAME = "monitor_last_output.txt"


# Process-local pending commits for ``monitor_retry_on_failure`` jobs:
# job_id -> (new_hash, output). Detection STASHES here instead of
# committing; ``commit_monitor_change`` persists after a successful agent
# run. Process-local by design (see module docstring): a crash simply
# re-detects the change next tick. Guarded by a lock because the
# scheduler fires jobs from a parallel thread pool.
_PENDING_LOCK = threading.Lock()
_PENDING_COMMITS: dict[str, tuple[str, str]] = {}


def job_retries_monitor_on_failure(job: dict) -> bool:
    """True when the job opted in to commit-on-success monitor semantics."""
    return bool(job.get("monitor_retry_on_failure"))


def commit_monitor_change(job_id: str) -> bool:
    """Persist a pending monitor change after a successful agent run.

    No-op (returns False) when the job has no pending change — legacy
    jobs commit at detection time and never populate the slot. Idempotent:
    a second call after a successful commit finds an empty slot.
    """
    with _PENDING_LOCK:
        pending = _PENDING_COMMITS.pop(str(job_id or ""), None)
    if pending is None:
        return False
    new_hash, output = pending
    _persist_monitor_state(str(job_id), new_hash, output)
    return True


def clear_pending_monitor_change(job_id: str) -> None:
    """Drop a pending change without committing (evaporated change)."""
    with _PENDING_LOCK:
        _PENDING_COMMITS.pop(str(job_id or ""), None)


@dataclass
class MonitorOutcome:
    """Result of one monitor-source evaluation."""

    ok: bool
    changed: bool = False
    first_run: bool = False
    context_block: Optional[str] = None
    error: Optional[str] = None


def hash_monitor_output(output: str) -> str:
    """Hash the monitor output as exact UTF-8 bytes (no normalization)."""
    return hashlib.sha256(output.encode("utf-8", errors="replace")).hexdigest()


def build_monitor_diff(old: str, new: str) -> str:
    """Unified diff of old vs new monitor output, capped at MAX_DIFF_CHARS."""
    diff = "\n".join(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="previous",
            tofile="current",
            lineterm="",
        )
    )
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n... [diff truncated]"
    return diff


def _snapshot_path(job_id: str):
    from cron.jobs import _job_output_dir

    return _job_output_dir(job_id) / _SNAPSHOT_FILENAME


def _read_last_output(job_id: str) -> str:
    try:
        path = _snapshot_path(job_id)
        if path.exists():
            return path.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("Monitor: failed to read last output for %r: %s", job_id, exc)
    return ""


def _write_last_output(job_id: str, output: str) -> None:
    try:
        path = _snapshot_path(job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
    except Exception as exc:
        logger.warning("Monitor: failed to persist last output for %r: %s", job_id, exc)


def _fetch_monitor_url(url: str) -> tuple[bool, str]:
    """Bounded GET of a monitor URL. Returns (ok, body-or-error)."""
    import urllib.request

    if not str(url).lower().startswith(("http://", "https://")):
        return False, f"monitor_url must be http(s): {url!r}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "hermes-cron-monitor"})
        with urllib.request.urlopen(req, timeout=URL_TIMEOUT_SECONDS) as resp:  # nosec B310 — scheme checked above
            body = resp.read(MAX_URL_BYTES + 1)
        if len(body) > MAX_URL_BYTES:
            body = body[:MAX_URL_BYTES]
        return True, body.decode("utf-8", errors="replace")
    except Exception as exc:
        return False, f"monitor_url fetch failed: {exc}"


def _run_monitor_source(job: dict) -> tuple[bool, str]:
    """Run the job's monitor source (script or URL). Returns (ok, output)."""
    monitor_script = (job.get("monitor_script") or "").strip()
    if monitor_script:
        # Same containment + interpreter rules as the existing `script` field.
        from cron.scheduler import _run_job_script

        workdir = (job.get("workdir") or "").strip() or None
        return _run_job_script(monitor_script, workdir=workdir)
    monitor_url = (job.get("monitor_url") or "").strip()
    if monitor_url:
        return _fetch_monitor_url(monitor_url)
    return False, "monitor job has neither monitor_script nor monitor_url"


def job_has_monitor(job: dict) -> bool:
    return bool((job.get("monitor_script") or "").strip() or (job.get("monitor_url") or "").strip())


def check_monitor(job: dict) -> MonitorOutcome:
    """Run the monitor source and decide whether the agent should run.

    On change (or first run) the persistence boundary depends on the
    job's ``monitor_retry_on_failure`` flag:

    * LEGACY (flag absent/False): the new hash + snapshot are persisted
      BEFORE the agent runs — detection time is the state boundary, so a
      failed agent run doesn't re-alert on the same content forever.
    * OPT-IN (flag True): the new hash + output are stashed in the
      process-local pending slot instead; ``commit_monitor_change``
      persists them only after the agent run completes successfully, so
      a failed run leaves the change retryable on the next tick.

    On source failure nothing is persisted (and any pending change is
    left in place — the retry is still owed).
    """
    job_id = str(job.get("id") or "")
    ok, output = _run_monitor_source(job)
    if not ok:
        return MonitorOutcome(ok=False, error=output)

    new_hash = hash_monitor_output(output)
    raw_state = job.get("monitor_state")
    state = raw_state if isinstance(raw_state, dict) else {}
    last_hash = state.get("last_output_hash")

    if last_hash is not None and new_hash == last_hash:
        # Unchanged — the previously detected change (if any) evaporated:
        # the source returned to the committed output, so a stale pending
        # commit must not be persisted later by an unrelated success.
        clear_pending_monitor_change(job_id)
        return MonitorOutcome(ok=True, changed=False)

    first_run = last_hash is None
    old_output = "" if first_run else _read_last_output(job_id)

    shown_output = output
    if len(shown_output) > MAX_OUTPUT_CHARS:
        shown_output = shown_output[:MAX_OUTPUT_CHARS] + "\n... [output truncated]"

    if first_run:
        context_block = (
            "## Monitor Baseline (first run)\n\n"
            "This is the first observation of the monitored source — there is "
            "no previous output to diff against.\n\n"
            f"### Current output\n\n```\n{shown_output}\n```"
        )
    else:
        diff = build_monitor_diff(old_output, output)
        context_block = (
            "## MONITOR CHANGE DETECTED\n\n"
            "The monitored source's output changed since the last run.\n\n"
            f"### Diff (previous → current)\n\n```diff\n{diff}\n```\n\n"
            f"### Current output\n\n```\n{shown_output}\n```"
        )

    if job_retries_monitor_on_failure(job):
        # Commit-on-success: stash the detected change; run_job persists it
        # via commit_monitor_change only after the agent completes
        # successfully. A failed agent run leaves the stored hash untouched
        # so the next tick re-detects THIS change and retries.
        with _PENDING_LOCK:
            _PENDING_COMMITS[job_id] = (new_hash, output)
    else:
        # Legacy: detection time is the state boundary — persist now.
        _persist_monitor_state(job_id, new_hash, output)
    return MonitorOutcome(
        ok=True, changed=True, first_run=first_run, context_block=context_block
    )


def _persist_monitor_state(job_id: str, new_hash: str, output: str) -> None:
    from cron.jobs import _hermes_now, update_job

    _write_last_output(job_id, output)
    try:
        update_job(
            job_id,
            {
                "monitor_state": {
                    "last_output_hash": new_hash,
                    "last_changed_at": _hermes_now().isoformat(),
                }
            },
        )
    except Exception as exc:
        logger.warning("Monitor: failed to persist state for %r: %s", job_id, exc)
