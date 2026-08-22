"""Surface-neutral goal-turn protocol for embedders (Stage 1 of the HAC extraction).

Why this exists
---------------
``GoalManager`` (:mod:`hermes_cli.goals`) is the semantic owner of the
persistent-goal loop, and every in-tree surface drives it directly: the gateway
at ``gateway/run.py`` (``_post_turn_goal_continuation``), the TUI gateway, and
the CLI. An EMBEDDER — a console that runs Hermes as a child process and owns
its own turn loop — has no such entry point, so it must reach in from outside:
resolve the manager, call ``evaluate_after_turn``, and reassemble a decision
envelope itself.

Doing that across a process boundary is where embedded goal loops actually
fail. Measured in one embedder's production telemetry over 5,001 mission events:
the operator-facing noise was almost entirely bridge faults, not judgments about
the work — ``no active goal`` (51), judge transport errors (33), raw
``RuntimeError`` (80), and ``invalid turn count`` (10). None of those are
decisions; they are an outside caller failing to keep a goal it cannot see.

This module is the supported entry point, so that logic lives in ONE place,
next to the state it describes:

    from hermes_cli.goal_session import run_goal_turn
    envelope = run_goal_turn("sess-123", "evaluate", response=answer)

It is deliberately transport-free: no argparse, no JSON on stdout, no exit
codes. A caller in-process gets a dict; a caller over a gateway or a subprocess
serializes that same dict. The envelope shape is stable and versioned.

What it fixes relative to reaching in from outside
--------------------------------------------------
1. **``goal_not_found`` on evaluate is impossible when an objective is known.**
   ``evaluate`` accepts ``goal=`` and starts the goal itself if the session has
   none, rather than returning an error the caller has to recognize and repair
   with a second round-trip. The repair is reported honestly in
   ``envelope["restarted"]`` so an audit trail does not silently claim
   continuity that never existed.
2. **A judge outage is classified once, here.** It arrives from the judge as an
   ordinary ``failed``/``paused``, which reads identically to "this work is
   wrong". Embedders that cannot tell them apart park missions behind a button
   for an infrastructure fault. The envelope tags it ``failure_kind =
   'judge_unavailable'`` so an embedder may self-heal an outage while a real
   ``failed`` verdict still reaches the operator immediately.
3. **The turn counters are internally consistent by construction**, because the
   state is read after the evaluation that advanced it rather than being
   re-derived by a caller comparing two snapshots.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Iterable, Optional

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1

ACTIONS = ("start", "evaluate", "resume", "pause", "cancel", "status")

# Verdicts an embedder may act on. Anything else is a protocol violation and is
# normalized to ``failed`` rather than passed through — an unrecognized verdict
# driving another agent turn is how a loop runs on a decision nobody made.
#
# ``inactive`` is deliberately present. ``evaluate_after_turn`` returns it when
# the goal is not active — a goal that was completed, cleared, or paused between
# turns — and that is an ORDINARY, expected condition, not a malformed verdict.
# Normalizing it to ``failed`` (as this module first did) turns a quiet
# "nothing to supervise" into a mission failure the operator has to resume:
# production showed three such resumes within minutes of the first deployment,
# each reading "The goal supervisor returned an unrecognized verdict".
_KNOWN_VERDICTS = frozenset({"done", "continue", "wait", "waiting", "failed", "inactive"})


def _state_payload(state: Any, *, status: Optional[str] = None) -> Dict[str, Any]:
    return {
        "status": status or state.status,
        "turns_used": int(state.turns_used),
        "max_turns": int(state.max_turns),
    }


def _error(session_id: str, code: str, message: str) -> Dict[str, Any]:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": session_id,
        "state": None,
        "decision": None,
        "error": {"code": code, "message": message},
    }


def _judge_unavailable(state: Any, decision: Dict[str, Any]) -> bool:
    """Detect a judge OUTAGE from every source that can report one.

    The judge's failure surfaces either as a ``paused_reason`` on the state or
    as an already-resolved reason on the decision, and only ever as a
    ``judge error:`` prefix. Checking one spelling silently returns the loop.
    """
    candidates = (getattr(state, "paused_reason", "") or "", decision.get("reason") or "")
    return any(str(item).strip().startswith("judge error:") for item in candidates)


def _judge_detail(state: Any, decision: Dict[str, Any]) -> str:
    raw = str(getattr(state, "paused_reason", "") or decision.get("reason") or "")
    return raw.removeprefix("judge error:").strip() or "unknown error"


def run_goal_turn(
    session_id: str,
    action: str = "evaluate",
    *,
    goal: str = "",
    response: str = "",
    contract: Optional[Dict[str, str]] = None,
    gates: Iterable[str] = (),
    max_turns: Optional[int] = None,
    reason: str = "",
    default_max_turns: int = 90,
) -> Dict[str, Any]:
    """Drive one goal-protocol action for ``session_id`` and return an envelope.

    ``action`` is one of :data:`ACTIONS`. ``evaluate`` is the interesting one:
    it grades ``response`` and returns the decision that may drive another turn.

    Passing ``goal`` alongside ``evaluate`` makes the call SELF-HEALING — if the
    session holds no goal (a cancelled turn, a cleared session, a crash between
    turns), it is started from that objective instead of failing. The envelope
    then carries ``restarted: True``.

    Never raises for an ordinary protocol condition: a missing goal, an unknown
    action, or an unavailable judge all come back as a structured envelope. Only
    a genuine programming error propagates.
    """
    session_id = str(session_id or "").strip()
    if not session_id:
        return _error("", "invalid_session", "A session id is required")
    if action not in ACTIONS:
        return _error(session_id, "invalid_action", f"Unknown goal action {action!r}")

    try:
        from hermes_cli.goals import GoalManager
    except Exception as exc:  # noqa: BLE001 — an embedder must get an envelope, not a traceback
        return _error(session_id, "goals_unavailable", f"goals module unavailable: {exc}")

    manager = GoalManager(session_id, default_max_turns=int(max_turns or default_max_turns))
    restarted = False

    def _start() -> Any:
        built = None
        if contract:
            from hermes_cli.goals import GoalContract

            fields = {key: str(contract.get(key) or "").strip() for key in
                      ("outcome", "verification", "constraints", "boundaries", "stop_when")}
            if any(fields.values()):
                built = GoalContract(**fields)
        state = manager.set(goal, max_turns=max_turns, contract=built)
        # Gates are appended after `set` because add_gate requires an active
        # goal. They run at the turn boundary BEFORE the judge and short-circuit
        # it on failure, which is the deterministic half of "done".
        for command in (str(item).strip() for item in gates):
            if command:
                manager.add_gate(command)
        return state

    if action == "start":
        if not str(goal or "").strip():
            return _error(session_id, "invalid_goal", "A goal is required to start")
        return {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": session_id,
            "state": _state_payload(_start()),
            "decision": None,
            "error": None,
            "restarted": False,
        }

    if manager.state is None:
        # The self-healing path. An embedder that knows the objective should
        # never have to recognize `goal_not_found`, issue a `start`, and retry —
        # that round-trip is the single commonest bridge fault in production.
        if action == "evaluate" and str(goal or "").strip():
            _start()
            restarted = True
            logger.info("goal_session: restarted a missing goal for session %s", session_id)
        else:
            return _error(session_id, "goal_not_found", "No persistent goal exists for this session")

    if action in {"resume", "pause", "cancel", "status"}:
        if action == "resume":
            state = manager.resume(reset_budget=False)
        elif action == "pause":
            state = manager.pause(reason or "user-paused")
        elif action == "cancel":
            state = manager.state
            manager.clear()
            return {
                "protocol_version": PROTOCOL_VERSION,
                "session_id": session_id,
                "state": _state_payload(state, status="cancelled"),
                "decision": None,
                "error": None,
                "restarted": restarted,
            }
        else:
            state = manager.state
        assert state is not None
        return {
            "protocol_version": PROTOCOL_VERSION,
            "session_id": session_id,
            "state": _state_payload(state),
            "decision": None,
            "error": None,
            "restarted": restarted,
        }

    # --- evaluate -----------------------------------------------------------
    try:
        from hermes_cli.goals import gather_background_processes

        background = gather_background_processes()
    except Exception:  # noqa: BLE001 — the live process list is an optimization
        background = None

    decision = manager.evaluate_after_turn(response, background_processes=background)
    state = manager.state
    assert state is not None

    verdict = str(decision.get("verdict") or "failed")
    status = str(decision.get("status") or state.status)
    should_continue = bool(decision.get("should_continue"))
    continuation = decision.get("continuation_prompt")
    failure_kind = ""

    if _judge_unavailable(state, decision):
        # An OUTAGE, not a verdict about the work. Reported as `failed` so a
        # caller that ignores `failure_kind` still stops safely, but tagged so a
        # caller that understands it may continue unsupervised rather than
        # parking a healthy mission behind a button.
        verdict, status, should_continue, continuation = "failed", "failed", False, None
        failure_kind = "judge_unavailable"
        decision["reason"] = (
            f"The goal supervisor is unavailable ({_judge_detail(state, decision)}); "
            "progress cannot be verified."
        )
    elif verdict == "inactive":
        # There is nothing left to supervise. Resolve it against the goal's own
        # status rather than reporting a verdict no embedder can act on: a goal
        # that reached `done` between turns IS done, and one that was paused is
        # a pause. Neither is a failure, and neither should cost the operator a
        # Resume.
        should_continue, continuation = False, None
        if str(state.status) == "paused":
            verdict, status = "hard_pause", "hard_paused"
            decision["reason"] = decision.get("reason") or "The goal is paused; there is nothing to supervise."
        else:
            verdict, status = "done", "done"
            decision["reason"] = decision.get("reason") or "The goal is no longer active; nothing remains to supervise."
    elif status == "paused":
        verdict, status, should_continue, continuation = "hard_pause", "hard_paused", False, None
    elif verdict == "waiting":
        status = "active"
    elif verdict not in _KNOWN_VERDICTS:
        decision["reason"] = f"The goal supervisor returned an unrecognized verdict ({verdict!r})"
        verdict, status, should_continue, continuation = "failed", "failed", False, None

    return {
        "protocol_version": PROTOCOL_VERSION,
        "session_id": session_id,
        "state": _state_payload(state, status=status),
        "decision": {
            "verdict": verdict,
            "reason": str(decision.get("reason") or "No goal decision reason was returned"),
            "should_continue": should_continue,
            "continuation_prompt": continuation,
            "failure_kind": failure_kind,
        },
        "error": None,
        "restarted": restarted,
    }
