"""Post-loop turn finalization for ``run_conversation``.

Extracted from ``agent/conversation_loop.py`` as part of the god-file
decomposition campaign (``~/.hermes/plans/god-file-decomposition.md``, Phase 1
step 4 — the post-loop ``TurnFinalizer`` seam). ``run_conversation``'s tail
(everything after the main tool-calling ``while`` loop) is lifted here verbatim:
budget-exhaustion summary, trajectory save, session persist, turn diagnostics,
response transforms, result-dict assembly, steer drain, and the memory/skill
review trigger.

Behavior-neutral: the body is moved unchanged. All ``agent.*`` side effects fire
exactly as before; only the post-loop *locals* are passed in as keyword args, and
the assembled ``result`` dict is returned to ``run_conversation`` which returns it
to the caller. The function is synchronous with a single return — mirroring the
region it replaces (no awaits, no early returns).

Module ``logger`` is imported lazily inside the body (``from
agent.conversation_loop import logger``) so this module never imports
``agent.conversation_loop`` at import time -> no import cycle, and the log records
keep the exact logger name (``"agent.conversation_loop"``).
"""

from __future__ import annotations

from contextlib import ExitStack
import os
import uuid

from agent.codex_responses_adapter import _summarize_user_message_for_log
from agent.message_content import flatten_message_text


def _is_pure_tool_call_tail(msg: dict) -> bool:
    """An assistant row with ``tool_calls`` but no visible text content of its own.

    Such a row satisfies the role check (``tail role == "assistant"``) while
    carrying none of the delivered answer — see the #43849/#44100 invariant
    block in :func:`finalize_turn`. Uses :func:`flatten_message_text` so that
    multimodal (list-type) content is evaluated by its text parts, not just
    its type.
    """
    if not msg.get("tool_calls"):
        return False
    return not flatten_message_text(msg.get("content")).strip()


# Verification continuation scaffolding flags: verify-on-stop / pre_verify
# inject a synthetic user nudge to keep the agent going one more turn.
# These nudges must be stripped from returned/live history to avoid
# role-alternation breaks and poisoning the resumed transcript. The
# assistant response is real content and is not flagged. (#65919 §7)
_VERIFICATION_CONTINUATION_FLAGS = (
    "_verification_stop_synthetic",
    "_pre_verify_synthetic",
)


def _drop_verification_continuation_scaffolding(messages) -> None:
    """Remove verification-continuation nudge messages from *messages* in place.

    Only the synthetic nudges carry these flags, so this strips just the
    nudges while preserving the real attempted-final-answer that was
    persisted to state.db.
    """
    messages[:] = [
        m for m in messages
        if not (isinstance(m, dict) and any(m.get(f) for f in _VERIFICATION_CONTINUATION_FLAGS))
    ]


def finalize_turn(
    agent,
    *,
    final_response,
    api_call_count,
    interrupted,
    failed,
    messages,
    conversation_history,
    effective_task_id,
    turn_id,
    user_message,
    original_user_message,
    _should_review_memory,
    _turn_exit_reason,
    _pending_verification_response=None,
    _pending_verification_response_previewed=False,
):
    """Run the post-loop finalization and return the turn ``result`` dict.

    Lifted verbatim from ``run_conversation`` (the region after the main agent
    loop). See module docstring.
    """
    from agent.conversation_loop import logger

    try:
        from agent.kanban_auto_handoff import AUTO_HANDOFF_EXIT_REASON
    except Exception:
        AUTO_HANDOFF_EXIT_REASON = "kanban_auto_handoff_requested"
    auto_handoff_requested = str(_turn_exit_reason) == AUTO_HANDOFF_EXIT_REASON
    kanban_terminal_transition = (
        str(_turn_exit_reason) == "kanban_terminal_transition"
    )
    verified_managed_lane = bool(
        getattr(agent, "_managed_short_task_bootstrap_verified", False)
    )
    # A planned checkpoint summary exists only to transport work into the next
    # bounded worker.  It must not look like a user-completed turn to plugins,
    # context engines, or session-end memory extractors.  The source worker
    # will consume this flag during process teardown while still closing its
    # providers normally.
    agent._suppress_session_end_learning = bool(
        verified_managed_lane
        and (auto_handoff_requested or kanban_terminal_transition)
    )
    auto_handoff_result = None
    deferred_handoff_direction = None
    deferred_handoff_control = None

    def _admit_existing_handoff_events(
        events: list[dict],
        *,
        source_task_id: str,
    ) -> None:
        """Give pre-window receipts complete replayable identities in order."""
        if not events:
            return
        from agent.kanban_auto_handoff import stage_pending_handoff_control

        run_text = (os.environ.get("HERMES_KANBAN_RUN_ID") or "").strip()
        if not run_text.isdigit():
            raise RuntimeError("handoff control requires the active run id")
        winner_kind = ""
        winner_id = ""
        for index, event in enumerate(events):
            kind = str(event.get("kind") or "steer")
            phase = "before_commit" if index == 0 else "after_terminal"
            if winner_kind == "stop" and kind != "stop":
                phase = "superseded"
            control = {
                "control_id": str(event["control_id"]),
                "source_task_id": source_task_id,
                "target_task_id": source_task_id,
                "kind": kind,
                "message": str(event.get("message") or ""),
                "phase": phase,
            }
            if phase == "before_commit":
                control.update(
                    expected_run_id=int(run_text),
                    expected_worker_pid=os.getpid(),
                )
            elif phase == "superseded":
                control["superseded_by_control_id"] = winner_id
            event["durable_control"] = control
            event["durable_phase"] = phase
            event["source_task_id"] = source_task_id
            event["target_task_id"] = source_task_id
            event["pending_handoff_control"] = stage_pending_handoff_control(
                control,
                error="awaiting durable confirmation",
            )
            event["supervisor_pending"] = True
            if phase == "superseded":
                event["state"] = "consumed"
                event["superseded_by_control_id"] = winner_id
            elif kind == "stop":
                winner_kind = "stop"
                winner_id = str(event["control_id"])
            elif not winner_kind:
                winner_kind = kind
                winner_id = str(event["control_id"])
        agent._auto_handoff_control_winner_kind = winner_kind
        agent._auto_handoff_control_winner_id = winner_id
        agent._auto_handoff_control_phase = "veto_pending"
        agent._auto_handoff_control_target = source_task_id

    budget_exhausted = (
        api_call_count >= agent.max_iterations
        or agent.iteration_budget.remaining <= 0
    )
    budget_fallback_eligible = (
        budget_exhausted
        and not interrupted
        and not failed
        and str(_turn_exit_reason) in {"unknown", "budget_exhausted"}
    )
    continuation_budget_exhausted = (
        final_response is None
        and bool(_pending_verification_response)
        and budget_fallback_eligible
    )

    # The hard 90/90 fallback is also a managed terminal transition. Open the
    # same control window used by a planned checkpoint before either reusing a
    # held verification response or making the final summary request. This
    # lets Stop/redirect/steer linearize against timeout accounting instead of
    # being accepted into memory and applied only after the DB commit.
    _budget_source_task_id = (
        os.environ.get("HERMES_KANBAN_TASK") or ""
    ).strip()
    _managed_budget_worker = False
    if _budget_source_task_id and verified_managed_lane:
        try:
            from agent.kanban_auto_handoff import resolve_policy

            _managed_budget_worker = bool(
                resolve_policy(
                    None,
                    max_iterations=agent.max_iterations,
                    task_id=_budget_source_task_id,
                ).enabled
            )
        except Exception:
            _managed_budget_worker = False
    _budget_control_lock = getattr(agent, "_auto_handoff_control_lock", None)
    _budget_redirect_lock = getattr(agent, "_pending_redirect_lock", None)
    _budget_steer_lock = getattr(agent, "_pending_steer_lock", None)
    _budget_model_request_active = getattr(agent, "_model_request_active", None)
    _budget_message_count = len(messages)
    _budget_prior_receipts: list[dict] = []
    _hard_budget_control_window = bool(
        _budget_source_task_id
        and _managed_budget_worker
        and budget_fallback_eligible
        and final_response is None
        and (continuation_budget_exhausted or not auto_handoff_requested)
    )
    if _hard_budget_control_window:
        agent._suppress_session_end_learning = True
        with ExitStack() as _budget_summary_gate:
            if _budget_control_lock is not None:
                _budget_summary_gate.enter_context(_budget_control_lock)
                agent._auto_handoff_control_phase = "budget_summarizing"
                agent._auto_handoff_control_source = _budget_source_task_id
                agent._auto_handoff_control_target = None
                agent._auto_handoff_control_winner_kind = ""
                agent._auto_handoff_control_winner_id = ""
            if _budget_redirect_lock is not None:
                _budget_summary_gate.enter_context(_budget_redirect_lock)
            if _budget_steer_lock is not None:
                _budget_summary_gate.enter_context(_budget_steer_lock)
                # Snapshot the durable direction receipts while the control
                # phase and steer slot are held by the same linearization
                # window. A steer that wins before this point is represented
                # here; one that arrives after it is queued directly against
                # ``budget_summarizing``. There is no gap where it can be
                # accepted into memory but omitted from the commit decision.
                _budget_prior_receipts = [
                    dict(receipt)
                    for receipt in (
                        getattr(agent, "_pending_steer_receipts", []) or []
                    )
                ]
            else:
                _receipt_snapshot = getattr(
                    agent, "_snapshot_unconsumed_steer_receipts", None
                )
                if callable(_receipt_snapshot):
                    try:
                        _budget_prior_receipts = list(
                            _receipt_snapshot() or []
                        )
                    except Exception:
                        _budget_prior_receipts = []
            if _budget_control_lock is not None:
                agent._auto_handoff_control_events = [
                    {
                        "control_id": str(
                            receipt.get("control_id")
                            or f"hc_{uuid.uuid4().hex}"
                        ),
                        "seq": index,
                        "kind": str(receipt.get("kind") or "steer"),
                        "message": str(receipt.get("message") or ""),
                        "delivery_slot": str(
                            receipt.get("delivery_slot") or "steer"
                        ),
                        "phase": "budget_summarizing",
                        "persisted": False,
                    }
                    for index, receipt in enumerate(
                        _budget_prior_receipts,
                        start=1,
                    )
                ]
                agent._auto_handoff_control_seq = len(
                    agent._auto_handoff_control_events
                )
                _admit_existing_handoff_events(
                    agent._auto_handoff_control_events,
                    source_task_id=_budget_source_task_id,
                )
            if _budget_model_request_active is not None:
                _budget_model_request_active.set()

    iteration_limit_fallback = False
    preserved_verification_fallback = False
    _budget_summary_error = None
    if continuation_budget_exhausted:
        # A verification/continuation gate deliberately withheld a composed
        # answer, then consumed the remaining budget before producing a newer
        # one. Preserve that exact answer instead of replacing it with another
        # fallible model call. The explicit pending value is the provenance
        # guard: unrelated error/recovery exits can never enter this branch.
        final_response = _pending_verification_response
        # Mark the turn as previewed only when the reused candidate was
        # actually streamed to the user as interim content. (#65919 review:
        # response-loss blocker)
        if _pending_verification_response_previewed:
            agent._response_was_previewed = True
        _turn_exit_reason = f"max_iterations_reached({api_call_count}/{agent.max_iterations})"
        iteration_limit_fallback = True
        preserved_verification_fallback = True
    elif final_response is None and auto_handoff_requested and not interrupted and not failed:
        # The Kanban soft limit deliberately leaves hard-budget headroom. Use
        # one tool-less request to turn the bounded worker history into a
        # durable checkpoint, then continue through a fresh child worker.
        agent._emit_status(
            "🔄 正在整理本段进展，准备自动交接。"
        )
        checkpoint_message_count = len(messages)
        _redirect_lock = getattr(agent, "_pending_redirect_lock", None)
        _steer_lock = getattr(agent, "_pending_steer_lock", None)
        _model_request_active = getattr(agent, "_model_request_active", None)
        _control_lock = getattr(agent, "_auto_handoff_control_lock", None)
        _source_task_id = (os.environ.get("HERMES_KANBAN_TASK") or "").strip()
        _checkpoint_prior_receipts: list[dict] = []
        with ExitStack() as _summary_start_gate:
            if _control_lock is not None:
                _summary_start_gate.enter_context(_control_lock)
                agent._auto_handoff_control_phase = "summarizing"
                agent._auto_handoff_control_source = _source_task_id
                agent._auto_handoff_control_target = None
                agent._auto_handoff_control_winner_kind = ""
                agent._auto_handoff_control_winner_id = ""
            if _redirect_lock is not None:
                _summary_start_gate.enter_context(_redirect_lock)
            if _steer_lock is not None:
                _summary_start_gate.enter_context(_steer_lock)
                _checkpoint_prior_receipts = [
                    dict(receipt)
                    for receipt in (
                        getattr(agent, "_pending_steer_receipts", []) or []
                    )
                ]
            else:
                _receipt_snapshot = getattr(
                    agent, "_snapshot_unconsumed_steer_receipts", None
                )
                if callable(_receipt_snapshot):
                    try:
                        _checkpoint_prior_receipts = list(
                            _receipt_snapshot() or []
                        )
                    except Exception:
                        _checkpoint_prior_receipts = []
            if _control_lock is not None:
                # Every ordinary Stop/redirect/steer acceptance now creates a
                # structured receipt under the same control lock. Seed the
                # checkpoint event stream from those pre-window receipts so a
                # later control cannot make the earlier direction disappear.
                agent._auto_handoff_control_events = [
                    {
                        "control_id": str(
                            receipt.get("control_id")
                            or f"hc_{uuid.uuid4().hex}"
                        ),
                        "seq": index,
                        "kind": str(receipt.get("kind") or "steer"),
                        "message": str(receipt.get("message") or ""),
                        "delivery_slot": str(
                            receipt.get("delivery_slot") or "steer"
                        ),
                        "phase": "summarizing",
                        "persisted": False,
                    }
                    for index, receipt in enumerate(
                        _checkpoint_prior_receipts,
                        start=1,
                    )
                ]
                agent._auto_handoff_control_seq = len(
                    agent._auto_handoff_control_events
                )
                _admit_existing_handoff_events(
                    agent._auto_handoff_control_events,
                    source_task_id=_source_task_id,
                )
            if _model_request_active is not None:
                _model_request_active.set()

        _summary_error = None
        try:
            final_response = agent._handle_max_iterations(
                messages,
                api_call_count,
                summary_request=(
                    "This is a planned short-task checkpoint, not the end of the overall "
                    "task. Without calling tools, write a concise handoff for a fresh "
                    "worker. Include: original objective, work completed, files or state "
                    "changed, verification already run and its result, exact next step, "
                    "and any genuine blocker. Do not claim the overall task is complete."
                ),
                status_label=(
                    f"🔄 Short-task checkpoint ({api_call_count}/{agent.max_iterations}). "
                    "Preparing handoff summary..."
                ),
            )
        except Exception as exc:
            # Redirect/stop may intentionally abort the provisional summary.
            # Decide under the control gate below before treating it as failure.
            _summary_error = exc
        try:
            from agent.kanban_auto_handoff import create_successor_and_close

            _veto_control = None
            _primary_veto_event = None
            with ExitStack() as _handoff_gate:
                if _control_lock is not None:
                    _handoff_gate.enter_context(_control_lock)
                    if agent._auto_handoff_control_phase != "veto_pending":
                        agent._auto_handoff_control_phase = "committing"
                if _redirect_lock is not None:
                    _handoff_gate.enter_context(_redirect_lock)
                if _steer_lock is not None:
                    _handoff_gate.enter_context(_steer_lock)
                try:
                    _pending_redirect = getattr(agent, "_pending_redirect", None)
                    _pending_steer = getattr(agent, "_pending_steer", None)
                    _hard_or_redirect_interrupt = bool(
                        getattr(agent, "_interrupt_requested", False)
                    )
                    _events = sorted(
                        list(getattr(agent, "_auto_handoff_control_events", []) or []),
                        key=lambda item: int(item.get("seq", 0)),
                    )
                    if (
                        _hard_or_redirect_interrupt
                        or _pending_redirect
                        or _pending_steer
                        or _events
                    ):
                        _kinds = [str(item.get("kind") or "") for item in _events]
                        if "stop" in _kinds or (
                            _hard_or_redirect_interrupt
                            and not _pending_redirect
                            and "redirect" not in _kinds
                        ):
                            _control_kind = "stop"
                        elif "redirect" in _kinds or _pending_redirect:
                            _control_kind = "redirect"
                        else:
                            _control_kind = "steer"
                        _parts = [
                            str(item.get("message") or "").strip()
                            for item in _events
                            if str(item.get("message") or "").strip()
                        ]
                        if not _events:
                            for _fallback_text in (
                                _pending_redirect,
                                _pending_steer,
                            ):
                                _clean = str(_fallback_text or "").strip()
                                if _clean and _clean not in _parts:
                                    _parts.append(_clean)
                        _chosen_event = next(
                            (
                                item
                                for item in _events
                                if str(item.get("kind") or "")
                                == _control_kind
                            ),
                            _events[0] if _events else None,
                        )
                        _control_message = "\n\n".join(_parts)
                        _veto_control = {
                            "control_id": (
                                _chosen_event.get("control_id")
                                if _chosen_event is not None
                                else f"hc_{uuid.uuid4().hex}"
                            ),
                            "kind": _control_kind,
                            "message": _control_message,
                        }
                        _primary_veto_event = next(
                            (
                                item
                                for item in _events
                                if (item.get("durable_control") or {}).get(
                                    "phase"
                                )
                                == "before_commit"
                            ),
                            None,
                        )
                        if _primary_veto_event is None:
                            _primary_veto_event = {
                                "control_id": str(_veto_control["control_id"]),
                                "seq": 1,
                                "kind": str(_veto_control["kind"]),
                                "message": str(_veto_control["message"]),
                                "phase": "summarizing",
                                "persisted": False,
                            }
                            _admit_existing_handoff_events(
                                [_primary_veto_event],
                                source_task_id=_source_task_id,
                            )
                            _events.append(_primary_veto_event)
                            agent._auto_handoff_control_events.append(
                                _primary_veto_event
                            )
                        if _control_lock is not None:
                            agent._auto_handoff_control_phase = "veto_pending"
                            agent._auto_handoff_control_target = _source_task_id
                        interrupted = True
                        _turn_exit_reason = "interrupted_by_user_direction"
                        final_response = None
                        del messages[checkpoint_message_count:]
                    else:
                        if _summary_error is not None:
                            raise _summary_error
                        try:
                            auto_handoff_result = create_successor_and_close(
                                policy=getattr(
                                    agent, "_kanban_auto_handoff_policy", None
                                ),
                                summary=final_response
                                or "No checkpoint summary was returned.",
                                api_call_count=api_call_count,
                                max_iterations=agent.max_iterations,
                            )
                        except Exception:
                            if _control_lock is not None:
                                agent._auto_handoff_control_phase = "commit_failed"
                                agent._auto_handoff_control_target = _source_task_id
                            raise
                        if _control_lock is not None:
                            if auto_handoff_result.get("status") == "handed_off":
                                agent._auto_handoff_control_phase = "committed"
                                agent._auto_handoff_control_target = (
                                    auto_handoff_result.get("successor_task_id")
                                )
                            elif auto_handoff_result.get("status") == "safety_limit":
                                agent._auto_handoff_control_phase = "safety_limited"
                                agent._auto_handoff_control_target = _source_task_id
                            else:
                                agent._auto_handoff_control_phase = "commit_failed"
                                agent._auto_handoff_control_target = _source_task_id
                finally:
                    if _model_request_active is not None:
                        _model_request_active.clear()
                if _veto_control is not None:
                    # Persist before releasing the same control lock that chose
                    # veto. A second stop/redirect cannot slip into a phase
                    # where the self exit-gate is not yet present.
                    from agent.kanban_auto_handoff import (
                        confirm_pending_handoff_control,
                        persist_worker_handoff_control,
                        stage_pending_handoff_control,
                    )

                    if _primary_veto_event is None:
                        raise RuntimeError(
                            "handoff veto has no admitted primary receipt"
                        )
                    _durable_control = dict(
                        _primary_veto_event["durable_control"]
                    )
                    _control_result = persist_worker_handoff_control(
                        _durable_control,
                        attempts=2,
                    )
                    if _control_result.get("status") not in {
                        "recorded", "already_recorded"
                    }:
                        deferred_handoff_control = stage_pending_handoff_control(
                            _durable_control,
                            error=str(
                                _control_result.get("error")
                                or "user direction could not be persisted"
                            ),
                        )
                        agent._pending_redirect = None
                        agent._pending_steer = None
                        _moved_ids = {
                            str(item.get("control_id"))
                            for item in _events
                            if item.get("control_id")
                        }
                        agent._pending_steer_receipts = [
                            receipt
                            for receipt in (
                                getattr(agent, "_pending_steer_receipts", [])
                                or []
                            )
                            if str(receipt.get("control_id")) not in _moved_ids
                        ]
                        raise RuntimeError(
                            _control_result.get("error")
                            or "user direction could not be persisted"
                        )
                    confirm_pending_handoff_control(_durable_control)
                    _primary_veto_event["persisted"] = True
                    _primary_veto_event["supervisor_pending"] = False
                    agent._pending_redirect = None
                    agent._pending_steer = None
                    _consumed_ids = {
                        str(item.get("control_id"))
                        for item in _events
                        if item.get("control_id")
                    }
                    agent._pending_steer_receipts = [
                        receipt
                        for receipt in (
                            getattr(agent, "_pending_steer_receipts", []) or []
                        )
                        if str(receipt.get("control_id"))
                        not in _consumed_ids
                    ]
                    if _control_lock is not None:
                        agent._auto_handoff_control_phase = "vetoed"
                        agent._auto_handoff_control_target = _source_task_id
                    auto_handoff_result = {
                        "status": "cancelled_by_user_direction",
                        "task_id": _source_task_id,
                        "control_id": _veto_control["control_id"],
                        "control_kind": _veto_control["kind"],
                        "persisted": True,
                    }
            status = auto_handoff_result.get("status")
            if status == "handed_off":
                successor_id = auto_handoff_result.get("successor_task_id")
                _turn_exit_reason = f"kanban_auto_handoff(successor={successor_id})"
                agent._emit_status(
                    "✅ 本段工作已收口，系统正在自动接续下一段。"
                )
            elif status == "safety_limit":
                _turn_exit_reason = "kanban_auto_handoff_safety_limit"
                final_response = (
                    (final_response or "").rstrip()
                    + "\n\n自动接力已达到本次设置的安全上限，工作已暂停，等待你确认是否继续。"
                ).strip()
            elif status == "cancelled_by_user_direction":
                pass
            else:
                failed = True
                _turn_exit_reason = f"kanban_auto_handoff_{status}"
                final_response = (
                    (final_response or "").rstrip()
                    + "\n\n自动接力暂时未能安全完成，当前工作没有被误报为完成。"
                    "请稍后查看状态或重试。"
                ).strip()
        except Exception as exc:
            if _model_request_active is not None:
                if _redirect_lock is not None:
                    with _redirect_lock:
                        _model_request_active.clear()
                else:
                    _model_request_active.clear()
            if _control_lock is not None:
                with _control_lock:
                    if _veto_control is not None:
                        agent._auto_handoff_control_phase = "commit_failed"
                        agent._auto_handoff_control_target = _source_task_id
                    elif agent._auto_handoff_control_phase not in {
                        "commit_failed", "safety_limited", "vetoed"
                    }:
                        agent._auto_handoff_control_phase = "idle"
                        agent._auto_handoff_control_target = None
            failed = True
            _turn_exit_reason = "kanban_auto_handoff_failed"
            auto_handoff_result = {"status": "failed", "error": str(exc)}
            final_response = (
                (final_response or "").rstrip()
                + "\n\n自动接力的进度保存失败，当前工作没有被误报为完成。"
                "系统已停止继续接力，避免重复执行。"
            ).strip()
            logger.warning("Kanban automatic handoff failed", exc_info=True)
    elif final_response is None and budget_fallback_eligible:
        # Budget exhausted — ask the model for a summary via one extra
        # API call with tools stripped.  _handle_max_iterations injects a
        # user message and makes a single toolless request.
        _turn_exit_reason = f"max_iterations_reached({api_call_count}/{agent.max_iterations})"
        logger.info(
            "Iteration budget exhausted (%s/%s); requesting summary",
            api_call_count,
            agent.max_iterations,
        )
        agent._emit_status(
            "⚠️ 本轮处理已达到预设上限，正在整理当前进展。"
        )
        if not agent.quiet_mode:
            agent._safe_print(
                "\n⚠️  本轮处理已达到预设上限，正在整理当前进展。"
            )
        try:
            final_response = agent._handle_max_iterations(
                messages, api_call_count
            )
        except Exception as exc:
            # A concurrent redirect may intentionally cancel this provisional
            # request. Resolve the queued control under the commit gate below
            # before deciding whether the exception is fatal.
            if _hard_budget_control_window:
                _budget_summary_error = exc
            else:
                raise
        iteration_limit_fallback = True

    if iteration_limit_fallback:
        # If running as a kanban worker, signal the dispatcher that the
        # worker could not complete (rather than treating it as a
        # protocol violation). This applies whether the user-facing fallback
        # came from the summary call or an explicitly pending continuation;
        # both exhausted the task budget and must advance the failure circuit.
        #
        # We route through ``_record_task_failure(outcome="timed_out")``
        # rather than ``kanban_block`` so this counts toward the dispatcher's
        # consecutive-failure circuit breaker (#29747 gap 2).
        _kanban_task = os.environ.get("HERMES_KANBAN_TASK")
        if _kanban_task and _hard_budget_control_window:
            _budget_veto_control = None
            _budget_primary_event = None
            try:
                from hermes_cli import kanban_db as _kb
                from agent.kanban_auto_handoff import (
                    confirm_pending_handoff_control,
                    persist_worker_handoff_control,
                    stage_pending_handoff_control,
                    worker_failure_limit,
                )

                _run_text = (
                    os.environ.get("HERMES_KANBAN_RUN_ID") or ""
                ).strip()
                _claim_lock = (
                    os.environ.get("HERMES_KANBAN_CLAIM_LOCK") or ""
                ).strip()
                if not _run_text.isdigit() or not _claim_lock:
                    raise RuntimeError(
                        "managed budget finalization requires exact run identity"
                    )
                with ExitStack() as _budget_commit_gate:
                    if _budget_control_lock is not None:
                        _budget_commit_gate.enter_context(_budget_control_lock)
                        if agent._auto_handoff_control_phase != "veto_pending":
                            agent._auto_handoff_control_phase = "budget_committing"
                    if _budget_redirect_lock is not None:
                        _budget_commit_gate.enter_context(_budget_redirect_lock)
                    if _budget_steer_lock is not None:
                        _budget_commit_gate.enter_context(_budget_steer_lock)
                    try:
                        _pending_redirect = getattr(
                            agent, "_pending_redirect", None
                        )
                        _pending_steer = getattr(agent, "_pending_steer", None)
                        _hard_or_redirect_interrupt = bool(
                            getattr(agent, "_interrupt_requested", False)
                        )
                        _events = sorted(
                            list(
                                getattr(
                                    agent,
                                    "_auto_handoff_control_events",
                                    [],
                                )
                                or []
                            ),
                            key=lambda item: int(item.get("seq", 0)),
                        )
                        if (
                            _hard_or_redirect_interrupt
                            or _pending_redirect
                            or _pending_steer
                            or _events
                        ):
                            _kinds = [
                                str(item.get("kind") or "") for item in _events
                            ]
                            if "stop" in _kinds or (
                                _hard_or_redirect_interrupt
                                and not _pending_redirect
                                and "redirect" not in _kinds
                            ):
                                _control_kind = "stop"
                            elif "redirect" in _kinds or _pending_redirect:
                                _control_kind = "redirect"
                            else:
                                _control_kind = "steer"
                            _parts = [
                                str(item.get("message") or "").strip()
                                for item in _events
                                if str(item.get("message") or "").strip()
                            ]
                            if not _events:
                                for _fallback_text in (
                                    _pending_redirect,
                                    _pending_steer,
                                ):
                                    _clean = str(
                                        _fallback_text or ""
                                    ).strip()
                                    if _clean and _clean not in _parts:
                                        _parts.append(_clean)
                            _chosen_event = next(
                                (
                                    item
                                    for item in _events
                                    if str(item.get("kind") or "")
                                    == _control_kind
                                ),
                                _events[0] if _events else None,
                            )
                            _budget_veto_control = {
                                "control_id": (
                                    _chosen_event.get("control_id")
                                    if _chosen_event is not None
                                    else f"hc_{uuid.uuid4().hex}"
                                ),
                                "kind": _control_kind,
                                "message": "\n\n".join(_parts),
                            }
                            _budget_primary_event = next(
                                (
                                    item
                                    for item in _events
                                    if (item.get("durable_control") or {}).get(
                                        "phase"
                                    )
                                    == "before_commit"
                                ),
                                None,
                            )
                            if _budget_primary_event is None:
                                _budget_primary_event = {
                                    "control_id": str(
                                        _budget_veto_control["control_id"]
                                    ),
                                    "seq": 1,
                                    "kind": str(
                                        _budget_veto_control["kind"]
                                    ),
                                    "message": str(
                                        _budget_veto_control["message"]
                                    ),
                                    "phase": "budget_summarizing",
                                    "persisted": False,
                                }
                                _admit_existing_handoff_events(
                                    [_budget_primary_event],
                                    source_task_id=_budget_source_task_id,
                                )
                                _events.append(_budget_primary_event)
                                agent._auto_handoff_control_events.append(
                                    _budget_primary_event
                                )
                            interrupted = True
                            _turn_exit_reason = "interrupted_by_user_direction"
                            final_response = None
                            del messages[_budget_message_count:]

                            if _budget_primary_event is None:
                                raise RuntimeError(
                                    "budget veto has no admitted primary receipt"
                                )
                            _durable_control = dict(
                                _budget_primary_event["durable_control"]
                            )
                            _control_result = (
                                persist_worker_handoff_control(
                                    _durable_control,
                                    attempts=2,
                                )
                            )
                            if _control_result.get("status") not in {
                                "recorded",
                                "already_recorded",
                            }:
                                deferred_handoff_control = (
                                    stage_pending_handoff_control(
                                        _durable_control,
                                        error=str(
                                            _control_result.get("error")
                                            or "user direction could not be persisted"
                                        ),
                                    )
                                )
                                agent._pending_redirect = None
                                agent._pending_steer = None
                                _moved_ids = {
                                    str(item.get("control_id"))
                                    for item in _events
                                    if item.get("control_id")
                                }
                                agent._pending_steer_receipts = [
                                    receipt
                                    for receipt in (
                                        getattr(
                                            agent,
                                            "_pending_steer_receipts",
                                            [],
                                        )
                                        or []
                                    )
                                    if str(receipt.get("control_id"))
                                    not in _moved_ids
                                ]
                                raise RuntimeError(
                                    _control_result.get("error")
                                    or "user direction could not be persisted"
                                )
                            confirm_pending_handoff_control(_durable_control)
                            _budget_primary_event["persisted"] = True
                            _budget_primary_event["supervisor_pending"] = False
                            agent._pending_redirect = None
                            agent._pending_steer = None
                            _persisted_receipt_ids = {
                                str(item.get("control_id"))
                                for item in _events
                                if item.get("control_id")
                            }
                            _receipts = getattr(
                                agent, "_pending_steer_receipts", []
                            ) or []
                            agent._pending_steer_receipts = [
                                receipt
                                for receipt in _receipts
                                if str(receipt.get("control_id"))
                                not in _persisted_receipt_ids
                            ]
                            if _budget_control_lock is not None:
                                agent._auto_handoff_control_phase = "vetoed"
                                agent._auto_handoff_control_target = (
                                    _budget_source_task_id
                                )
                        else:
                            if _budget_summary_error is not None:
                                raise _budget_summary_error
                            _conn = _kb.connect()
                            try:
                                _failure_result = (
                                    _kb._record_managed_task_failure_exact(
                                        _conn,
                                        _kanban_task,
                                        error=(
                                            "Iteration budget exhausted "
                                            f"({api_call_count}/"
                                            f"{agent.max_iterations}) — task "
                                            "could not complete within the "
                                            "allowed iterations"
                                        ),
                                        outcome="timed_out",
                                        summary=(
                                            (final_response or "").strip()
                                            or None
                                        ),
                                        expected_run_id=int(_run_text),
                                        expected_worker_pid=os.getpid(),
                                        expected_claim_lock=_claim_lock,
                                        failure_limit=worker_failure_limit(
                                            strict=True
                                        ),
                                        event_payload_extra={
                                            "budget_used": api_call_count,
                                            "budget_max": agent.max_iterations,
                                        },
                                    )
                                )
                            finally:
                                _conn.close()
                            if _failure_result.get("status") == "recorded":
                                if _budget_control_lock is not None:
                                    agent._auto_handoff_control_phase = (
                                        "budget_finalized"
                                    )
                                    agent._auto_handoff_control_target = (
                                        _budget_source_task_id
                                    )
                                logger.info(
                                    "recorded budget-exhausted failure for "
                                    "task %s (%d/%d)",
                                    _kanban_task,
                                    api_call_count,
                                    agent.max_iterations,
                                )
                            elif _failure_result.get("status") == "superseded":
                                if _budget_control_lock is not None:
                                    agent._auto_handoff_control_phase = "vetoed"
                                    agent._auto_handoff_control_target = (
                                        _budget_source_task_id
                                    )
                                interrupted = True
                                final_response = None
                                del messages[_budget_message_count:]
                                _turn_exit_reason = "kanban_state_superseded"
                            else:
                                raise RuntimeError(
                                    "managed budget failure was not recorded"
                                )
                    finally:
                        if _budget_model_request_active is not None:
                            _budget_model_request_active.clear()
            except Exception:
                if _budget_model_request_active is not None:
                    _budget_model_request_active.clear()
                if _budget_control_lock is not None:
                    with _budget_control_lock:
                        agent._auto_handoff_control_phase = "commit_failed"
                        agent._auto_handoff_control_target = (
                            _budget_source_task_id
                        )
                failed = True
                final_response = None
                _turn_exit_reason = "kanban_budget_finalize_failed"
                logger.warning(
                    "Failed to safely finalize budget-exhausted task %s",
                    _kanban_task,
                    exc_info=True,
                )
        elif _kanban_task:
            # Compatibility path for legacy/default workers that were not
            # launched through the Phase-1 start barrier. They retain the
            # historical run-closing semantics and never create an exit gate.
            try:
                from hermes_cli import kanban_db as _kb
                from agent.kanban_auto_handoff import worker_failure_limit

                _conn = _kb.connect()
                try:
                    _kb._record_task_failure(
                        _conn,
                        _kanban_task,
                        error=(
                            "Iteration budget exhausted "
                            f"({api_call_count}/{agent.max_iterations}) — "
                            "task could not complete within the allowed "
                            "iterations"
                        ),
                        outcome="timed_out",
                        summary=((final_response or "").strip() or None),
                        failure_limit=worker_failure_limit(strict=False),
                        release_claim=True,
                        end_run=True,
                        event_payload_extra={
                            "budget_used": api_call_count,
                            "budget_max": agent.max_iterations,
                        },
                    )
                    logger.info(
                        "recorded legacy budget-exhausted failure for task "
                        "%s (%d/%d)",
                        _kanban_task,
                        api_call_count,
                        agent.max_iterations,
                    )
                finally:
                    _conn.close()
            except Exception:
                logger.warning(
                    "Failed to record budget-exhausted failure for task %s",
                    _kanban_task,
                    exc_info=True,
                )

    managed_budget_terminal_transition = bool(
        iteration_limit_fallback and _hard_budget_control_window
    )
    terminal_learning_suppressed = bool(
        verified_managed_lane
        and (
            auto_handoff_requested
            or kanban_terminal_transition
            or managed_budget_terminal_transition
            or getattr(agent, "_managed_short_task_bootstrap", False)
        )
    )

    # Determine if conversation completed successfully
    normal_text_response = str(_turn_exit_reason).startswith("text_response(")
    if kanban_terminal_transition:
        completed = not failed and not interrupted
    elif auto_handoff_requested:
        completed = bool(
            final_response is not None
            and not failed
            and auto_handoff_result is not None
            and auto_handoff_result.get("status") == "handed_off"
        )
    else:
        completed = (
            final_response is not None
            and not failed
            and (
                api_call_count < agent.max_iterations
                or normal_text_response
            )
        )

    # Preflight can seed the display count before the provider receives the
    # request. Roll that estimate back only when an interrupt wins the race
    # before any successful provider response. Compaction state remains owned
    # by the real-usage/post-compaction path, including its ``-1`` sentinel.
    # Guard rules (test-double density on this path is high):
    #  - snapshot is type-pinned to a real int — MagicMock agents auto-create
    #    truthy Mock attributes that must never arm the rollback;
    #  - the received-response flag is pinned to ``is not True`` — its real
    #    domain is True/False, and only a literal True means a provider
    #    response completed;
    #  - the compressor method gets a getattr+callable guard — SimpleNamespace
    #    compressor doubles and plugin context engines lack it.
    _preflight_snapshot = getattr(
        agent, "_turn_preflight_display_snapshot", None
    )
    if (
        interrupted is True
        and isinstance(_preflight_snapshot, int)
        and not isinstance(_preflight_snapshot, bool)
        and getattr(agent, "_turn_received_provider_response", False) is not True
        and getattr(agent, "context_compressor", None) is not None
    ):
        _rollback_fn = getattr(
            agent.context_compressor,
            "rollback_interrupted_preflight_display_tokens",
            None,
        )
        if callable(_rollback_fn):
            _rollback_fn(_preflight_snapshot)

    # Post-loop cleanup must never lose the response.  Trajectory save,
    # resource teardown, and session persistence all touch fallible
    # surfaces — file I/O / JSON serialization (_save_trajectory), remote
    # VM/browser teardown over the network (_cleanup_task_resources), and
    # SQLite writes (_persist_session).  A raise from any of them used to
    # propagate straight out of run_conversation, discarding the partial
    # final_response the caller is waiting for (subprocess wrappers saw an
    # empty stdout with no traceback — #8049).  Each step is now guarded
    # independently so one failure can't skip the others, and any errors
    # are surfaced on the result dict via ``cleanup_errors`` rather than
    # killing the turn.
    _cleanup_errors = []

    # Save trajectory if enabled.  ``user_message`` may be a multimodal
    # list of parts; the trajectory format wants a plain string.
    try:
        agent._save_trajectory(messages, _summarize_user_message_for_log(user_message), completed)
    except Exception as _save_err:
        _cleanup_errors.append(f"save_trajectory: {_save_err}")
        logger.error("finalize_turn: _save_trajectory failed: %s", _save_err, exc_info=True)

    # Clean up VM and browser for this task after conversation completes
    try:
        agent._cleanup_task_resources(effective_task_id)
    except Exception as _cleanup_err:
        _cleanup_errors.append(f"cleanup_task_resources: {_cleanup_err}")
        logger.error("finalize_turn: _cleanup_task_resources failed: %s", _cleanup_err, exc_info=True)

    # Persist session to both JSON log and SQLite only after private retry
    # scaffolding has been removed. Otherwise a later user "continue" turn
    # can replay assistant("(empty)") / recovery nudges and fall into the
    # same empty-response loop again.
    try:
        agent._drop_trailing_empty_response_scaffolding(messages)

        # Drop verification-continuation nudges (synthetic user messages)
        # from the live history before the tail-assistant check — only the
        # nudges need stripping; the assistant candidate persists in
        # state.db. (#65919 §7)
        _drop_verification_continuation_scaffolding(messages)

        # When the turn was interrupted and the last message is a tool
        # result, append a synthetic assistant message to close the
        # tool-call sequence. Without this, the session persists a
        # ``tool → user`` alternation that strict providers (Gemini,
        # Claude) reject, causing them to hallucinate a continuation of
        # the user's message on the next turn (#48879).
        #
        # ``_drop_trailing_empty_response_scaffolding`` only rewinds the
        # tool tail when an empty-response scaffolding flag is present; a
        # clean ``/stop`` interrupt after a successful tool sets no such
        # flag, so the tool result survives as the tail and we close it
        # here instead. On an interrupt ``final_response`` is typically
        # empty, so fall back to an explicit placeholder rather than
        # persisting an empty-content assistant turn.
        if interrupted:
            from agent.message_sanitization import close_interrupted_tool_sequence
            close_interrupted_tool_sequence(messages, final_response)

        # Some recovery/fallback paths return a real final_response without
        # adding a closing assistant message to the transcript (e.g. the
        # partial-stream and prior-turn-content recovery ``break`` sites in
        # ``conversation_loop``). If persisted as-is, the durable session can
        # end at a tool/user message even though the caller — and the gateway
        # platform — already saw a completed assistant response. The next turn
        # then replays a user-only backlog and the model re-answers every
        # "unanswered" message. Close the durable turn at the source, at the
        # single chokepoint every recovery ``break`` flows through, so the
        # invariant "delivered final_response ⇒ assistant row in transcript"
        # holds regardless of which path produced it. (#43849 / #44100)
        #
        # Compare content (not just role) so a verification candidate that
        # matches the final response is not duplicated at budget
        # exhaustion. (#65919 §7)
        if final_response and not interrupted:
            try:
                _tail = messages[-1] if messages else None
            except Exception:
                _tail = None
            _tail_role = _tail.get("role") if isinstance(_tail, dict) else None
            if _tail_role != "assistant":
                # Tail is not an assistant row — append the final response
                # so the durable turn closes with the answer (#43849/#44100).
                messages.append({"role": "assistant", "content": final_response})
            elif isinstance(_tail, dict) and _tail.get("content") != final_response and _is_pure_tool_call_tail(_tail):
                # The tail IS an assistant row, but a *pure tool-call turn*:
                # tool_calls with no text of its own. The role check alone
                # leaves the #43849/#44100 invariant unmet — the user saw a
                # response that never reached the transcript, and the next turn
                # replays the user backlog and re-answers it (the very symptom
                # this block was added for). Fill that row's empty content
                # instead of appending, so the durable turn ends with the answer
                # without disturbing the tool-call structure or creating an
                # assistant→assistant pair.
                #
                # The ``content != final_response`` guard prevents filling when
                # the tail already carries the final response text (verification
                # candidate collapse — the provisional answer was persisted and
                # reused as the terminal response, #65919 §7).
                _tail["content"] = final_response
                # The row may have already been flushed to SQLite by the
                # incremental tool-call persist (conversation_loop.py:4990),
                # which stamps ``_DB_PERSISTED_MARKER`` so subsequent flushes
                # skip it. Pop the marker so the next ``_persist_session``
                # re-writes the filled content to the durable store —
                # otherwise ``/resume`` reloads ``content=""`` and the bug
                # resurfaces cross-session.
                _tail.pop("_db_persisted", None)

        # The model has completed its request, so replace API-local
        # voice/model/skill guidance with the clean user input before writing the
        # final durable snapshot and returning the continuation history. Earlier
        # turn-start flushes use the DB-only override because their messages are
        # still needed for the API request; this finalizer runs after that request
        # is complete (#48677 / #63766).
        _apply_override = getattr(agent, "_apply_persist_user_message_override", None)
        if callable(_apply_override):
            _apply_override(messages)
        agent._persist_session(messages, conversation_history)
    except Exception as _persist_err:
        _cleanup_errors.append(f"persist_session: {_persist_err}")
        logger.error("finalize_turn: _persist_session failed: %s", _persist_err, exc_info=True)

    # ── Turn-exit diagnostic log ─────────────────────────────────────
    # Always logged at INFO so agent.log captures WHY every turn ended.
    # When the last message is a tool result (agent was mid-work), log
    # at WARNING — this is the "just stops" scenario users report.
    _last_msg_role = messages[-1].get("role") if messages else None
    _last_tool_name = None
    if _last_msg_role == "tool":
        # Walk back to find the assistant message with the tool call
        for _m in reversed(messages):
            if _m.get("role") == "assistant" and _m.get("tool_calls"):
                _tcs = _m["tool_calls"]
                if _tcs and isinstance(_tcs[0], dict):
                    _last_tool_name = _tcs[-1].get("function", {}).get("name")
                break

    _turn_tool_count = sum(
        1 for m in messages
        if isinstance(m, dict) and m.get("role") == "assistant" and m.get("tool_calls")
    )
    _resp_len = len(final_response) if final_response else 0
    _budget_used = agent.iteration_budget.used if agent.iteration_budget else 0
    _budget_max = agent.iteration_budget.max_total if agent.iteration_budget else 0

    _diag_msg = (
        "Turn ended: reason=%s model=%s api_calls=%d/%d budget=%d/%d "
        "tool_turns=%d last_msg_role=%s response_len=%d session=%s"
    )
    _diag_args = (
        _turn_exit_reason, agent.model, api_call_count, agent.max_iterations,
        _budget_used, _budget_max,
        _turn_tool_count, _last_msg_role, _resp_len,
        agent.session_id or "none",
    )

    if (
        _last_msg_role == "tool"
        and not interrupted
        and not kanban_terminal_transition
        and not managed_budget_terminal_transition
    ):
        # Agent was mid-work — this is the "just stops" case.
        logger.warning(
            "Turn ended with pending tool result (agent may appear stuck). "
            + _diag_msg + " last_tool=%s",
            *_diag_args, _last_tool_name,
        )
    else:
        logger.info(_diag_msg, *_diag_args)

    # File-mutation verifier footer.
    # If one or more ``write_file`` / ``patch`` calls failed during this
    # turn and were never superseded by a successful write to the same
    # path, append an advisory footer to the assistant response.  This
    # catches the specific case — reported by Ben Eng (#15524-adjacent)
    # — where a model issues a batch of parallel patches, half of them
    # fail with "Could not find old_string", and the model summarises
    # the turn claiming every file was edited.  The user then has to
    # manually run ``git status`` to catch the lie.  With this footer
    # the truth is surfaced on every turn, so over-claiming is
    # structurally impossible past the model.
    #
    # Gate: only applied when a real text response exists for this
    # turn and the user didn't interrupt.  Empty/interrupted turns
    # already have other surface text that shouldn't be augmented.
    if final_response and not interrupted:
        try:
            _failed = getattr(agent, "_turn_failed_file_mutations", None) or {}
            if _failed and agent._file_mutation_verifier_enabled():
                footer = agent._format_file_mutation_failure_footer(_failed)
                if footer:
                    final_response = final_response.rstrip() + "\n\n" + footer
        except Exception as _ver_err:
            logger.debug("file-mutation verifier footer failed: %s", _ver_err)

    # Turn-completion explainer.
    # When a turn ends abnormally after substantive work — empty content
    # after retries, a partial/truncated stream, a still-pending tool
    # result, or an iteration/budget limit — the user otherwise gets a
    # blank or fragmentary response box with no consolidated reason why
    # the agent stopped (#34452).  Surface a single user-visible
    # explanation derived from ``_turn_exit_reason``, mirroring the
    # file-mutation verifier footer pattern above.
    #
    # Gate carefully so healthy turns stay quiet:
    #   - ``text_response(...)`` exits never produce an explanation
    #     (handled inside the formatter), so a terse ``Done.`` is silent.
    #   - We only ACT when there is no genuinely usable reply this turn:
    #     an empty response, the "(empty)" terminal sentinel, or a
    #     suspiciously short partial fragment with no terminating
    #     punctuation (e.g. "The").  A real short answer keeps its text.
    if (
        not interrupted
        and not kanban_terminal_transition
        and not managed_budget_terminal_transition
    ):
        try:
            if agent._turn_completion_explainer_enabled():
                _stripped = (final_response or "").strip()
                _is_empty_terminal = _stripped == "" or _stripped == "(empty)"
                # A short fragment that is not a normal text_response exit
                # and lacks sentence-ending punctuation is treated as a
                # truncated partial (the "The" case from #34452).
                _is_partial_fragment = (
                    not _is_empty_terminal
                    and not preserved_verification_fallback
                    and not str(_turn_exit_reason).startswith("text_response")
                    and len(_stripped) <= 24
                    and _stripped[-1:] not in {".", "!", "?", "。", "！", "？", "`", ")"}
                )
                _is_partial_stream_recovery = (
                    str(_turn_exit_reason) == "partial_stream_recovery"
                )
                if (
                    _is_empty_terminal
                    or _is_partial_fragment
                    or _is_partial_stream_recovery
                ):
                    _explanation = agent._format_turn_completion_explanation(
                        _turn_exit_reason
                    )
                    if _explanation:
                        if _is_empty_terminal:
                            # Replace the bare "(empty)"/blank sentinel with
                            # the actionable explanation.
                            final_response = _explanation
                        else:
                            # Keep the partial fragment, append the reason so
                            # the user sees both what arrived and why it
                            # stopped.
                            final_response = (
                                _stripped + "\n\n" + _explanation
                            )
        except Exception as _exp_err:
            logger.debug("turn-completion explainer failed: %s", _exp_err)

    _response_transformed = False

    # Plugin hook: transform_llm_output
    # Fired once per turn after the tool-calling loop completes.
    # Plugins can transform the LLM's output text before it's returned.
    # First hook to return a string wins; None/empty return leaves text unchanged.
    if (
        final_response
        and not interrupted
        and not terminal_learning_suppressed
    ):
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            _transform_results = _invoke_hook(
                "transform_llm_output",
                response_text=final_response,
                session_id=agent.session_id or "",
                model=agent.model,
                platform=getattr(agent, "platform", None) or "",
            )
            for _hook_result in _transform_results:
                if isinstance(_hook_result, str) and _hook_result:
                    final_response = _hook_result
                    _response_transformed = True
                    break  # First non-empty string wins
        except Exception as exc:
            logger.warning("transform_llm_output hook failed: %s", exc)

    # Plugin hook: post_llm_call
    # Fired once per turn after the tool-calling loop completes.
    # Plugins can use this to persist conversation data (e.g. sync
    # to an external memory system).
    if (
        final_response
        and not interrupted
        and not terminal_learning_suppressed
    ):
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            _invoke_hook(
                "post_llm_call",
                session_id=agent.session_id,
                task_id=effective_task_id,
                turn_id=turn_id,
                user_message=original_user_message,
                assistant_response=final_response,
                conversation_history=list(messages),
                model=agent.model,
                platform=getattr(agent, "platform", None) or "",
            )
        except Exception as exc:
            logger.warning("post_llm_call hook failed: %s", exc)

    # Context engine observation hook: notify the active engine that this
    # turn has finished, with the finalized transcript. Complements the
    # per-request select_context() hook (selection before the request;
    # observation after the turn). No-op default, fail-open.
    if not terminal_learning_suppressed:
        try:
            from agent.conversation_loop import _notify_context_engine_turn_complete
            # Forward the turn's canonical usage when the host has it. The loop
            # stashes the most recent API response's usage dict (the same
            # canonical buckets fed to ``update_from_response``) on the agent as
            # ``_last_turn_usage``. It is ``None`` on turns that never reached a
            # provider response (early failure / interrupt), which is exactly the
            # contract: real usage when available, ``None`` otherwise.
            _turn_usage = getattr(agent, "_last_turn_usage", None)
            _notify_context_engine_turn_complete(
                agent,
                messages,
                usage=_turn_usage,
                logger=logger,
                turn_id=turn_id,
                task_id=effective_task_id,
                api_call_count=api_call_count,
                interrupted=interrupted,
                failed=failed,
                turn_exit_reason=_turn_exit_reason,
            )
        except Exception as exc:
            logger.warning("on_turn_complete notification failed: %s", exc)

    # Extract reasoning from the CURRENT turn only.  Walk backwards
    # but stop at the user message that started this turn — anything
    # earlier is from a prior turn and must not leak into the reasoning
    # box (confusing stale display; #17055).  Within the current turn
    # we still want the *most recent* non-empty reasoning: many
    # providers (Claude thinking, DeepSeek v4, Codex Responses) emit
    # reasoning on the tool-call step and leave the final-answer step
    # with reasoning=None, so picking only the last assistant would
    # silently drop legitimate same-turn reasoning.
    last_reasoning = None
    for msg in reversed(messages):
        if msg.get("role") == "user":
            break  # turn boundary — don't cross into prior turns
        if msg.get("role") == "assistant" and msg.get("reasoning"):
            last_reasoning = msg["reasoning"]
            break

    # Build result with interrupt info if applicable
    result = {
        "final_response": final_response,
        "last_reasoning": last_reasoning,
        "messages": messages,
        "api_calls": api_call_count,
        "completed": completed,
        "turn_exit_reason": _turn_exit_reason,
        "failed": failed,
        "partial": False,  # True only when stopped due to invalid tool calls
        "interrupted": interrupted,
        "response_transformed": _response_transformed,
        "response_previewed": getattr(agent, "_response_was_previewed", False),
        "model": agent.model,
        "provider": agent.provider,
        "base_url": agent.base_url,
        "input_tokens": agent.session_input_tokens,
        "output_tokens": agent.session_output_tokens,
        "cache_read_tokens": agent.session_cache_read_tokens,
        "cache_write_tokens": agent.session_cache_write_tokens,
        "reasoning_tokens": agent.session_reasoning_tokens,
        "prompt_tokens": agent.session_prompt_tokens,
        "completion_tokens": agent.session_completion_tokens,
        "total_tokens": agent.session_total_tokens,
        "last_prompt_tokens": getattr(agent.context_compressor, "last_prompt_tokens", 0) or 0,
        "estimated_cost_usd": agent.session_estimated_cost_usd,
        "cost_status": agent.session_cost_status,
        "cost_source": agent.session_cost_source,
        # Requested service tier (from request_overrides.extra_body), for
        # billing audits by callers like `hermes -z --usage-file`.
        "service_tier": (
            (getattr(agent, "request_overrides", {}) or {}).get("extra_body") or {}
        ).get("service_tier"),
        "session_id": agent.session_id,
    }
    if auto_handoff_result is not None:
        result["auto_handoff"] = auto_handoff_result
    if deferred_handoff_control is not None:
        result["pending_handoff_control"] = deferred_handoff_control
    if agent._tool_guardrail_halt_decision is not None:
        result["guardrail"] = agent._tool_guardrail_halt_decision.to_metadata()
    # Surface any post-loop cleanup failures so the caller can distinguish a
    # clean turn from one whose trajectory/session/resource teardown raised
    # (the response is still returned either way — #8049).
    if _cleanup_errors:
        result["cleanup_errors"] = _cleanup_errors
    # If a /steer landed after the final assistant turn (no more tool
    # batches to drain into), hand it back to the caller so it can be
    # delivered as the next user turn instead of being silently lost.
    _leftover_steer = agent._drain_pending_steer()
    _deferred_parts = [
        text
        for text in (deferred_handoff_direction, _leftover_steer)
        if text
    ]
    if _deferred_parts:
        result["pending_steer"] = "\n".join(_deferred_parts)
    agent._response_was_previewed = False

    # Include interrupt message if one triggered the interrupt
    if interrupted and agent._interrupt_message:
        result["interrupt_message"] = agent._interrupt_message

    # Clear interrupt state after handling
    agent.clear_interrupt()

    # Clear stream callback so it doesn't leak into future calls
    agent._stream_callback = None

    # Check skill trigger NOW — based on how many tool iterations THIS turn used.
    _should_review_skills = False
    if (not terminal_learning_suppressed
            and agent._skill_nudge_interval > 0
            and agent._iters_since_skill >= agent._skill_nudge_interval
            and "skill_manage" in agent.valid_tool_names):
        _should_review_skills = True
        agent._iters_since_skill = 0

    # External memory provider: sync the completed turn + queue next prefetch.
    if not terminal_learning_suppressed:
        agent._sync_external_memory_for_turn(
            original_user_message=original_user_message,
            final_response=final_response,
            interrupted=interrupted,
            messages=messages,
        )

    # Background memory/skill review — runs AFTER the response is delivered
    # so it never competes with the user's task for model attention.
    if (
        final_response
        and not interrupted
        and not terminal_learning_suppressed
        and (_should_review_memory or _should_review_skills)
    ):
        try:
            agent._spawn_background_review(
                messages_snapshot=list(messages),
                review_memory=_should_review_memory,
                review_skills=_should_review_skills,
            )
        except Exception:
            pass  # Background review is best-effort

    # Note: Memory provider on_session_end() + shutdown_all() are NOT
    # called here — run_conversation() is called once per user message in
    # multi-turn sessions. Shutting down after every turn would kill the
    # provider before the second message. Actual session-end cleanup is
    # handled by the CLI (atexit / /reset) and gateway (session expiry /
    # _reset_session).

    # Plugin hook: on_session_end
    # Fired at the very end of every run_conversation call.
    # Plugins can use this for cleanup, flushing buffers, etc.
    if not terminal_learning_suppressed:
        try:
            from hermes_cli.plugins import invoke_hook as _invoke_hook
            _invoke_hook(
                "on_session_end",
                session_id=agent.session_id,
                task_id=effective_task_id,
                turn_id=turn_id,
                completed=completed,
                interrupted=interrupted,
                model=agent.model,
                platform=getattr(agent, "platform", None) or "",
            )
        except Exception as exc:
            logger.warning("on_session_end hook failed: %s", exc)

    agent._turn_preflight_display_snapshot = None
    agent._turn_received_provider_response = False

    return result
