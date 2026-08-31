"""Required Host gate for an assistant's no-tool final candidate.

The core owns only lifecycle mechanics.  A registered Host owns the policy
that decides whether the candidate may finish or must be replaced with a
deterministic boundary response.
"""

from __future__ import annotations

from typing import Any, Mapping


class FinalCandidateGateError(RuntimeError):
    """A deterministic Host contract failure that must never retry the provider."""


def evaluate_final_candidate(
    *,
    response_text: str,
    session_id: str,
    task_id: str,
    turn_id: str,
    model: str,
    platform: str,
    finish_reason: str,
    iteration: int,
    max_iterations: int,
    remaining_iterations: int,
) -> dict[str, Any] | None:
    """Invoke and validate the optional single-owner final-candidate gate."""
    from hermes_cli.lifecycle import invoke_required_hook

    result = invoke_required_hook(
        "assistant_final_candidate_gate",
        response_text=response_text,
        session_id=session_id,
        task_id=task_id,
        turn_id=turn_id,
        model=model,
        platform=platform,
        finish_reason=finish_reason,
        iteration=iteration,
        max_iterations=max_iterations,
        remaining_iterations=remaining_iterations,
        streaming=False,
    )
    if result is None:
        return None
    if not isinstance(result, Mapping):
        raise FinalCandidateGateError(
            "assistant final-candidate gate returned no directive"
        )
    action = result.get("action")
    if action == "ALLOW":
        content = result.get("content")
        if content is not None and (
            not isinstance(content, str) or not content.strip()
        ):
            raise FinalCandidateGateError(
                "assistant final-candidate ALLOW content is invalid"
            )
        return {
            "action": "ALLOW",
            **({"content": content} if content is not None else {}),
        }
    if action == "REPLACE":
        content = result.get("content")
        reason_code = result.get("reason_code")
        if (
            not isinstance(content, str)
            or not content.strip()
        ):
            raise FinalCandidateGateError(
                "assistant final-candidate REPLACE is invalid"
            )
        return {
            "action": "REPLACE",
            "content": content,
            **(
                {"reason_code": reason_code}
                if isinstance(reason_code, str) and reason_code.strip()
                else {}
            ),
        }
    raise FinalCandidateGateError(
        "assistant final-candidate action must be ALLOW or REPLACE"
    )


__all__ = [
    "FinalCandidateGateError",
    "evaluate_final_candidate",
]
