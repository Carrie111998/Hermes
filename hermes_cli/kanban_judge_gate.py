"""Shared goal-mode pre-completion judge gate for kanban workers.

Both completion paths — the ``kanban_complete`` tool
(``tools/kanban_tools.py``) and the terminal ``hermes kanban complete``
(``hermes_cli/kanban.py``) — must apply the same gate, or a worker can
bypass one by using the other (Issue #38367).

Why this module exists
----------------------
``goals.judge_goal`` returns a 5-tuple
``(verdict, reason, parse_failed, wait_directive, transport_failed)``.
When the judge API is unreachable it returns::

    ("continue", "judge error: <ExcType>", False, None, True)

That is a deliberate *fail-open* signal: in the ``/goal`` loop
(``goals.py:1446``) ``continue`` merely means "take another turn", and the
caller inspects ``transport_failed`` to auto-pause on a persistently broken
judge.  Both kanban gates, however, discarded the flag and read only
``verdict != "done"`` — so a judge outage was indistinguishable from a
substantive rejection and the completion was refused.  The worker then burned
its remaining turn budget and the run ended ``blocked`` with the card's work
finished but never recorded.

Two guards, therefore:

1. **Transport failures fail open.** An unreachable judge must never block a
   completion; that is what the judge itself intends.  Genuine ``continue``
   verdicts from a *reachable* judge still fail closed.
2. **Bounded rejections.** A reachable judge that keeps rejecting an
   otherwise-finished card (e.g. a recurring report card whose body has no
   scoreable acceptance criteria, so the judge reads the summary
   "self-referentially") would otherwise loop until the turn budget dies, once
   per respawn, forever.  After ``max_rejections`` the gate allows completion
   and stamps an auditable override event instead of wedging the board.
"""

from __future__ import annotations

import logging
import os
from typing import Any, NamedTuple, Optional

logger = logging.getLogger(__name__)

#: Judge rejections tolerated on a card before the gate allows completion with
#: an override. Auditable: it writes a ``judge_override`` event and sets
#: ``bypass=BYPASS_CEILING`` so callers can stamp metadata.
#:
#: NOTE: counted over the card's LIFETIME, not consecutively — see
#: ``_count_rejections``. A card that accrued rejections in an earlier run
#: carries them forward, so a contested card can arrive at a later run already
#: at its ceiling. Scoping this to ``run_id`` is the known next improvement.
DEFAULT_MAX_REJECTIONS = 2

EVENT_REJECTED = "judge_rejected"
EVENT_OVERRIDE = "judge_override"
EVENT_TRANSPORT = "judge_transport_failed"
EVENT_PARSE_FAILED = "judge_parse_failed"
EVENT_DISABLED = "judge_gate_disabled"
EVENT_NO_JUDGE = "judge_unavailable"

# Bypass kinds, stamped onto metadata["judge_gate"]["bypass"]. Each must match
# the event row written alongside it — an audit trail that contradicts itself
# is worse than none, because it invites confident wrong conclusions.
BYPASS_TRANSPORT = "transport"        # judge unreachable
BYPASS_PARSE = "parse_failed"         # judge reachable, reply unusable
BYPASS_CEILING = "ceiling"            # rejection ceiling reached
BYPASS_UNRECORDABLE = "unrecordable"  # rejection could not be persisted
BYPASS_DISABLED = "disabled_by_env"   # HERMES_KANBAN_JUDGE_GATE=0
BYPASS_NO_JUDGE = "no_judge"          # no auxiliary judge configured


class GateDecision(NamedTuple):
    """Outcome of the pre-completion judge gate.

    ``allow``  — let the completion proceed.
    ``reason`` — judge reason text (empty when the gate did not run).
    ``bypass`` — one of the ``BYPASS_*`` constants when the completion was
    allowed WITHOUT judge approval, else None. Callers stamp it onto
    ``metadata["judge_gate"]`` so a completion the judge never blessed is
    never indistinguishable from one it did.
    """

    allow: bool
    reason: str = ""
    #: Why the gate allowed this WITHOUT judge approval, or None when the
    #: judge genuinely approved. A single explicit field rather than derived
    #: booleans: deriving it is how a garbage judge reply ended up stamped
    #: "transport" and an env-disabled gate ended up stamped "unrecordable",
    #: each contradicting its own event row.
    bypass: Optional[str] = None

    def bypass_kind(self) -> Optional[str]:
        """Why the gate let this through without judge approval, if it did."""
        return self.bypass

    @property
    def override(self) -> bool:
        """True when the rejection ceiling was reached."""
        return self.bypass == BYPASS_CEILING

    @property
    def transport(self) -> bool:
        """True when the judge could not be reached."""
        return self.bypass == BYPASS_TRANSPORT


def gate_enabled() -> bool:
    """False when ``HERMES_KANBAN_JUDGE_GATE`` explicitly disables the gate."""
    env = os.environ.get("HERMES_KANBAN_JUDGE_GATE")
    if env is None:
        return True
    return env.strip().lower() not in {"0", "false", "no", "off"}


def max_rejections() -> int:
    """Rejection ceiling, overridable via ``HERMES_KANBAN_JUDGE_MAX_REJECTIONS``.

    A value of 0 disables the ceiling (reject forever — the old behaviour).
    """
    raw = os.environ.get("HERMES_KANBAN_JUDGE_MAX_REJECTIONS")
    if raw is None or not raw.strip():
        return DEFAULT_MAX_REJECTIONS
    try:
        value = int(raw.strip())
    except ValueError:
        logger.warning(
            "HERMES_KANBAN_JUDGE_MAX_REJECTIONS=%r is not an integer; using %d",
            raw, DEFAULT_MAX_REJECTIONS,
        )
        return DEFAULT_MAX_REJECTIONS
    return max(0, value)


def judge_available() -> bool:
    """True when an auxiliary goal-judge client is configured and reachable."""
    try:
        from agent.auxiliary_client import get_text_auxiliary_client
        client, model = get_text_auxiliary_client("goal_judge")
        return client is not None and bool(model)
    except Exception:  # pragma: no cover - defensive
        return False


def _count_rejections(conn: Any, task_id: str) -> int:
    """Number of ``judge_rejected`` events recorded for this card.

    Returns 0 for anything that is not a genuine integer count. ``COUNT(*)``
    always yields an ``int`` from sqlite, so a non-int here means we are not
    talking to a real connection (a ``MagicMock`` stand-in coerces to 1 via
    ``__int__``, which would trip the ceiling on the very first rejection and
    silently disable the gate).
    """
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = ?",
            (task_id, EVENT_REJECTED),
        ).fetchone()
    except Exception:  # pragma: no cover - defensive
        logger.debug("judge gate: could not count rejections for %s", task_id,
                     exc_info=True)
        return 0
    if not row:
        return 0
    value = row[0]
    if isinstance(value, bool) or not isinstance(value, int):
        logger.debug(
            "judge gate: non-integer rejection count %r for %s; treating as 0",
            value, task_id,
        )
        return 0
    return max(0, value)


def _record(kb: Any, conn: Any, task_id: str, kind: str,
            payload: Optional[dict] = None) -> bool:
    """Append a gate event. Never raises; returns whether the write landed.

    The return value matters for rejections specifically. The ceiling is
    counted from persisted ``judge_rejected`` rows, so if the write silently
    fails (locked DB, read-only fs, exhausted busy-timeout) the counter never
    advances and the escape hatch never opens — reject-forever, the exact
    wedge this module exists to prevent. Callers treat an unrecordable
    rejection as a reason to fail open.
    """
    try:
        with kb.write_txn(conn):
            kb._append_event(conn, task_id, kind, payload)
        return True
    except Exception:  # pragma: no cover - defensive
        logger.warning("judge gate: could not record %s for %s", kind, task_id,
                       exc_info=True)
        return False


def evaluate(kb: Any, conn: Any, task: Any, response_text: str, *,
             task_id: str) -> GateDecision:
    """Decide whether a goal-mode card may complete.

    ``kb`` is the ``kanban_db`` module, ``conn`` an open connection, ``task``
    the row from ``kb.get_task``, ``response_text`` the worker's summary (or
    result) — the evidence the judge scores against the card body — and
    ``task_id`` the card id.  The id is passed explicitly rather than read
    off ``task`` because callers already hold it and task rows are stubbed in
    tests; depending on ``task.id`` made the gate fail on partial rows.

    Never raises: any internal failure fails open, because a broken gate must
    not be able to strand finished work.
    """
    if task is None or not getattr(task, "goal_mode", False):
        return GateDecision(allow=True)
    if not gate_enabled():
        # Audited like every other bypass. HERMES_KANBAN_JUDGE_GATE is
        # settable by the very worker being gated, so a silent skip here
        # would be the one way to complete a goal_mode card with no trace
        # that the judge was never consulted.
        logger.warning("judge gate disabled via env; allowing completion of %s",
                       task_id)
        _record(kb, conn, task_id, EVENT_DISABLED,
                {"env": "HERMES_KANBAN_JUDGE_GATE"})
        return GateDecision(allow=True, reason="judge gate disabled via env",
                            bypass=BYPASS_DISABLED)
    if not judge_available():
        # No judge configured at all — historical fail-open behaviour, but it
        # must not be SILENT. This is the default-firing branch: if the
        # auxiliary credentials ever change, the whole gate becomes a no-op,
        # and without a trace nobody would notice.
        logger.warning("no goal judge configured; allowing completion of %s",
                       task_id)
        _record(kb, conn, task_id, EVENT_NO_JUDGE, None)
        return GateDecision(allow=True, reason="no goal judge configured",
                            bypass=BYPASS_NO_JUDGE)

    title = getattr(task, "title", "") or ""
    goal = f"{title}\n\n{getattr(task, 'body', '') or ''}".strip()
    response = (response_text or "").strip()

    # Blank evidence must be REFUSED, not waved through. judge_goal
    # short-circuits locally on an empty response (goals.py:894-896) and
    # returns "continue" without contacting the judge, so we cannot let that
    # reach the rejection path as if it were a verdict — but allowing it is
    # far worse: it would mean `hermes kanban complete <id>` with no flags
    # closes any goal_mode card with the judge never consulted, i.e. the gate
    # would reward omitting evidence.
    #
    # (An earlier version of this guard allowed blank evidence, justified by
    # "bulk closes can't carry a summary". That was wrong: the multi-id guard
    # at kanban.py:1975 refuses only --summary/--metadata, and --result is
    # deliberately excluded precisely so it CAN apply to all ids — see its
    # help text. Evidence is always expressible.)
    #
    # No judge_rejected event is recorded: the judge was never consulted, so
    # this must not consume a slot of the rejection ceiling.
    if not response:
        logger.info("judge gate: no evidence supplied for %s — refusing", task_id)
        return GateDecision(allow=False, reason="no completion evidence supplied")

    # An empty goal is the card's fault, not the worker's — judge_goal returns
    # "skipped" for it, which is not a finding. Nothing to judge against.
    if not goal:
        logger.info("judge gate: card %s has no title/body to judge against",
                    task_id)
        return GateDecision(allow=True, reason="card has no acceptance criteria",
                            bypass=BYPASS_NO_JUDGE)

    # Redact here rather than at the call sites: this is the only place that
    # ships card text to an auxiliary LLM, and the CLI path (hermes_cli/kanban.py)
    # has no redaction of its own — before this gate existed it made no network
    # call at all. Doing it by construction keeps both callers covered.
    try:
        from agent.redact import redact_sensitive_text
        goal = redact_sensitive_text(goal, force=True)
        response = redact_sensitive_text(response, force=True)
    except Exception:  # pragma: no cover - redaction must never block work
        logger.warning("judge gate: redaction unavailable for %s", task_id,
                       exc_info=True)

    try:
        from hermes_cli.goals import judge_goal
        verdict, reason, parse_failed, _wait, transport_failed = judge_goal(
            goal=goal, last_response=response,
        )
    except Exception as exc:
        # judge_goal swallows its own errors; if it ever raises, fail open
        # rather than wedge the worker.
        logger.warning("goal judge check failed, allowing completion: %s", exc,
                       exc_info=True)
        return GateDecision(allow=True, reason=f"judge error: {type(exc).__name__}",
                            bypass=BYPASS_TRANSPORT)

    if transport_failed:
        # The judge could not be reached. It signals this by returning
        # verdict="continue"; treating that as a rejection is what stranded
        # finished cards. Fail open, loudly.
        logger.warning(
            "goal judge unreachable for %s (%s) — allowing completion",
            task_id, reason,
        )
        _record(kb, conn, task_id, EVENT_TRANSPORT, {"reason": reason})
        return GateDecision(allow=True, reason=reason, bypass=BYPASS_TRANSPORT)

    if parse_failed:
        # The judge was reached but its reply was empty or non-JSON
        # (goals.py:721,747) — the model is broken, not the card. It signals
        # this with verdict="continue" too, so reading the verdict alone
        # repeats the exact bug this module was written to fix, one tuple
        # element over. Fail open.
        logger.warning(
            "goal judge reply unusable for %s (%s) — allowing completion",
            task_id, reason,
        )
        _record(kb, conn, task_id, EVENT_PARSE_FAILED, {"reason": reason})
        return GateDecision(allow=True, reason=reason, bypass=BYPASS_PARSE)

    if verdict == "done":
        return GateDecision(allow=True, reason=reason)

    # Only an explicit "continue" from a reachable, parsing judge is a real
    # rejection. "wait" means the judge thinks work is parked on something
    # async — the kanban loop already normalises it away (goals.py:1738-1742)
    # because workers have no wait-barrier. "skipped" means it declined to
    # judge. Neither is a finding about the card, so neither may block it.
    if verdict != "continue":
        logger.info(
            "judge gate: non-substantive verdict %r for %s — allowing completion",
            verdict, task_id,
        )
        return GateDecision(allow=True, reason=reason)

    ceiling = max_rejections()
    prior = _count_rejections(conn, task_id)
    attempt = prior + 1
    # Strictly greater: `ceiling` is the number of rejections actually
    # tolerated, so ceiling=2 rejects twice and overrides on the third
    # attempt. (With >= it rejected only once before yielding, which an audit
    # fairly described as "nags once, then yields to retry".)
    if ceiling and attempt > ceiling:
        logger.warning(
            "goal judge rejected %s %d time(s) (ceiling %d) — allowing "
            "completion with override: %s",
            task_id, attempt, ceiling, reason,
        )
        _record(kb, conn, task_id, EVENT_OVERRIDE,
                {"reason": reason, "rejections": attempt, "ceiling": ceiling})
        return GateDecision(allow=True, reason=reason, bypass=BYPASS_CEILING)

    if not _record(kb, conn, task_id, EVENT_REJECTED,
                   {"reason": reason, "attempt": attempt, "ceiling": ceiling}):
        # We cannot persist this rejection, so we cannot count toward the
        # ceiling either — blocking now would block forever. Fail open and
        # say why.
        logger.warning(
            "goal judge rejected %s but the rejection could not be recorded; "
            "allowing completion rather than risking a permanent wedge",
            task_id,
        )
        return GateDecision(allow=True, reason=reason, bypass=BYPASS_UNRECORDABLE)
    return GateDecision(allow=False, reason=reason)


def rejection_message(tid: str, decision: GateDecision) -> str:
    """Operator/worker-facing text for a denied completion."""
    return (
        f"Goal completion rejected by judge: {decision.reason}. "
        f"To proceed, either: (1) provide explicit acceptance evidence in "
        f"your summary matching the task's criteria, or (2) create "
        f"continuation tasks with parents=[{tid}] and keep this task alive."
    )
