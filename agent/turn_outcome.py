"""End-of-turn outcome evaluation (Layer 0 of failure tracing).

Runs after a turn finishes and declares whether the WORK held up — the third
failure category Hermes has never had. ``finalize_turn`` already distinguishes
"the machinery broke" (``failed``) from "the loop ended with text"
(``completed``); this module adds "the work didn't hold up", attributed to the
skills that ran.

Mechanism:
  1. Mechanical layer first (Layer 1, ``tools/skill_verify`` + the existing
     per-turn file-mutation state). A verifier FAIL is foreclosed: it is
     recorded for that skill and no aux judgment can overturn it (down-only).
  2. Signal-gated aux call (the "residue" judge): only when a verifier FAILed,
     when a used skill had no verifier (unverified residue — the common case),
     or when configured ``run: always``. The aux prompt is seeded with the
     verdict report so it can't ignore a mechanical fail line.
  3. Attribution is dumb-recorder: mechanical FAILs always land on their
     skill; the eval's extra failure points merge in (union). Environmental
     reads live in the reason string, never in the verdict — the curator
     review is the arbiter, not this recorder.
  4. Best-effort everywhere: any failure here must never break the turn.

The seam core exposes is the returned ``TurnOutcome`` (attached to the session
result dict by ``finalize_turn``). The ACSS Hypothesize consumer reads that
seam from the edge — it is not built into this module or the turn loop.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Union

logger = logging.getLogger(__name__)

_AUX_TASK = "outcome"

# A pass is only recorded (and only eligible to clear `needs_review`) when the
# eval declares success at or above this confidence. A weak pass — no verifier,
# low-confidence eval success — must not clear a needs-review flag on its own.
_PASS_CONFIDENCE_THRESHOLD = 0.6


@dataclass
class TurnOutcome:
    task_succeeded: bool  # did the WORK hold up (not "did the loop end")
    confidence: float  # 0..1
    failure_points: List[str]  # skill names to blame, [] when none attributable
    reason: str  # merged verifier+eval text; feeds curator review / ACSS


def _default_outcome_config() -> Dict[str, Any]:
    """Read ``auxiliary.outcome`` from config. {} when unavailable."""
    try:
        from agent.auxiliary_client import _get_auxiliary_task_config

        cfg = _get_auxiliary_task_config(_AUX_TASK)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:
        return {}


def _resolve_skill_dirs(
    skills_used_this_turn: Union[Iterable[str], Mapping[str, Path]]
) -> List[tuple[str, Path]]:
    """Normalize used skills to ``(name, skill_dir)`` pairs.

    Accepts either a mapping (name → dir, from the turn accumulator) or a
    plain iterable of names (resolved via the skill_usage index).
    """
    if isinstance(skills_used_this_turn, Mapping):
        return [(str(name), Path(d)) for name, d in skills_used_this_turn.items()]
    from tools.skill_usage import _find_skill_dir

    pairs: List[tuple[str, Path]] = []
    for name in skills_used_this_turn:
        d = _find_skill_dir(str(name))
        if d is not None:
            pairs.append((str(name), d))
    return pairs


def _run_skill_verifier(
    skill_name: str, skill_dir: Path, task_cwd: Path
) -> tuple[str, str]:
    """Run one skill's verifier. Returns (verdict, reason); verdict is one of
    ``pass`` | ``fail`` | ``skip``. ``skip`` covers not-opted-in, no verify
    block, not curation-eligible, applicability-gated-out, or a broken check.
    """
    try:
        from tools.skill_verify import run_verification

        outcome = run_verification(skill_name, skill_dir, task_cwd)
    except Exception as e:
        logger.debug("turn_outcome verifier error for %s: %s", skill_name, e, exc_info=True)
        return ("skip", "")
    if outcome is None:
        return ("skip", "")
    return ("pass" if outcome.success else "fail", outcome.reason or "")


def _build_prompt(
    user_message: Optional[str],
    final_response: Optional[str],
    verdict_report: str,
    file_previews: str,
    tool_error_count: int,
) -> str:
    lines = [
        "Evaluate whether the task Hermes just completed actually held up.",
        "The work can look finished while being semantically wrong. Judge the WORK,",
        "not whether the loop ended, and only blame skills you can justify.",
        "",
        f"Task: {user_message or '(none)'}",
        f"Final response: {(final_response or '').strip()[:2000]}",
        f"Tool-call errors this turn: {tool_error_count}",
        "Per-skill mechanical verifier verdicts (pass/fail/skip):",
        verdict_report or "  (none)",
    ]
    if file_previews:
        lines.append(f"Failed file mutations: {file_previews}")
    lines.append(
        'Reply with strict JSON: {"task_succeeded": bool, "confidence": 0-1, '
        '"failure_points": [skill names that caused the failure], '
        '"reason": "short explanation"}.'
    )
    return "\n".join(lines)


def _default_aux_eval(prompt: str) -> Optional[Dict[str, Any]]:
    """Real aux-client path. Best-effort: no client / any error → None."""
    try:
        from agent.auxiliary_client import get_text_auxiliary_client

        client, model = get_text_auxiliary_client(task=_AUX_TASK)
        if client is None or not model:
            return None
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=200,
        )
        content = getattr(getattr(resp, "choices", [{}])[0].message, "content", "") or ""
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except Exception as e:
        logger.debug("outcome aux eval failed: %s", e, exc_info=True)
        return None


def _record(skill_name: str, success: bool, reason: str = "") -> None:
    """Best-effort write to the usage sidecar. Never raises into the turn."""
    try:
        from tools.skill_usage import bump_outcome

        bump_outcome(skill_name, success, reason=reason or None)
    except Exception as e:
        logger.debug("turn_outcome: failed to record %s: %s", skill_name, e, exc_info=True)


def evaluate_turn_outcome(
    *,
    skills_used_this_turn: Union[Iterable[str], Mapping[str, Path]] = (),
    task_cwd: Optional[Union[str, Path]] = None,
    final_response: Optional[str] = None,
    user_message: Optional[str] = None,
    failed: bool = False,
    interrupted: bool = False,
    exit_reason: Optional[str] = None,
    file_mutation_state: Optional[Mapping[str, Any]] = None,
    tool_error_count: int = 0,
    outcome_config: Optional[Mapping[str, Any]] = None,
    _aux_eval: Optional[Callable[[str], Optional[Mapping[str, Any]]]] = None,
) -> Optional[TurnOutcome]:
    """Evaluate whether the finished turn's work held up.

    Returns None when there is nothing to record: feature disabled, the turn
    was interrupted, no signal triggered the eval, or no judgment could be
    produced. Never raises — this runs at end-of-turn and must not break it.
    """
    try:
        cfg = (
            dict(outcome_config)
            if outcome_config is not None
            else _default_outcome_config()
        )
        if not cfg.get("enabled"):
            return None

        if interrupted:
            return None  # user-stopped turns are not work failures
        if failed:
            # Infra-failed turn: an outcome, but no skill is to blame.
            return TurnOutcome(
                task_succeeded=False,
                confidence=1.0,
                failure_points=[],
                reason=f"infra failure: {exit_reason or 'unknown'}",
            )

        cwd = Path(task_cwd) if task_cwd is not None else Path.cwd()
        run_mode = str(cfg.get("run") or "auto")

        # ── Mechanical layer first ──────────────────────────────────────────
        skill_dirs = _resolve_skill_dirs(skills_used_this_turn)
        verdicts = {
            name: _run_skill_verifier(name, d, cwd) for name, d in skill_dirs
        }
        fail_verdicts = [(n, r) for n, (v, r) in verdicts.items() if v == "fail"]
        skip_names = {n for n, (v, r) in verdicts.items() if v == "skip"}

        fm = file_mutation_state or {}
        has_mechanical_fail = bool(fail_verdicts) or bool(fm)
        has_residue = bool(skip_names)

        should_eval = run_mode == "always" or has_mechanical_fail or has_residue
        if not should_eval:
            # All used skills verified clean and nothing failed — no residue to
            # judge, nothing to record.
            return None

        # ── Signal-gated aux judgment ───────────────────────────────────────
        verdict_report = "\n".join(
            f"  - {n}: {v}{f' ({r})' if r else ''}" for n, (v, r) in verdicts.items()
        )
        file_previews = "; ".join(
            f"{k}: {str(v.get('error_preview') or '')[:200]}" for k, v in fm.items()
        )
        prompt = _build_prompt(
            user_message,
            final_response,
            verdict_report,
            file_previews,
            tool_error_count,
        )
        aux_data: Optional[Mapping[str, Any]] = None
        if _aux_eval is not None:
            try:
                aux_data = _aux_eval(prompt)
            except Exception as e:
                logger.debug("turn_outcome: injected aux eval raised: %s", e, exc_info=True)
                aux_data = None
        else:
            aux_data = _default_aux_eval(prompt)

        eval_succeeded: Optional[bool] = None
        eval_confidence: Optional[float] = None
        eval_points: List[str] = []
        eval_reason = ""
        if isinstance(aux_data, dict):
            if "task_succeeded" in aux_data:
                eval_succeeded = bool(aux_data.get("task_succeeded"))
            conf = aux_data.get("confidence")
            if isinstance(conf, (int, float)):
                eval_confidence = float(min(max(conf, 0.0), 1.0))
            fp = aux_data.get("failure_points")
            if isinstance(fp, (list, tuple)):
                eval_points = [str(x) for x in fp]
            eval_reason = str(aux_data.get("reason") or "")

        # ── Verdict resolution ──────────────────────────────────────────────
        if has_mechanical_fail:
            # Down-only: a mechanical FAIL is foreclosed regardless of the eval.
            task_succeeded = False
            confidence = 1.0
        elif eval_succeeded is not None:
            task_succeeded = eval_succeeded
            confidence = eval_confidence if eval_confidence is not None else 0.5
        else:
            # Only unverified residue and no aux judgment available — nothing
            # to record.
            return None

        # ── Attribution (dumb recorder) + persistence ───────────────────────
        mechanical_points = [n for n, _ in fail_verdicts]
        fail_reasons = {n: r for n, r in fail_verdicts}
        failure_points = list(dict.fromkeys(mechanical_points + eval_points))
        for s in failure_points:
            # Mechanical fails carry their verifier's reason; eval-attributed
            # points carry the eval's merged reason text.
            _record(s, False, fail_reasons.get(s) or eval_reason)
        if task_succeeded and confidence >= _PASS_CONFIDENCE_THRESHOLD:
            for name, _d in skill_dirs:
                _record(name, True)

        # ── Reason corpus ───────────────────────────────────────────────────
        parts = []
        for n, r in fail_verdicts:
            parts.append(f"verifier ({n}): {r or 'failed'}")
        if fm:
            parts.append(f"file-mutation: {file_previews}")
        if eval_reason:
            parts.append(eval_reason)
        reason = "; ".join(parts)
        if not reason:
            reason = "task did not hold up" if not task_succeeded else "ok"

        return TurnOutcome(
            task_succeeded=task_succeeded,
            confidence=confidence,
            failure_points=failure_points,
            reason=reason,
        )
    except Exception as e:
        logger.debug("evaluate_turn_outcome failed: %s", e, exc_info=True)
        return None
