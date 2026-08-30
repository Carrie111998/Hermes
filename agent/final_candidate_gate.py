"""Required Host gate for an assistant's no-tool final candidate.

The core owns only lifecycle mechanics.  A registered Host owns the policy
that decides whether the candidate may finish, needs one more tool-loop pass,
or must be replaced with a deterministic boundary response.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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
    if action == "CONTINUE":
        context = result.get("context")
        revision = result.get("state_revision")
        digest = result.get("pending_sha256")
        if (
            not isinstance(context, str)
            or not context.strip()
            or not isinstance(revision, int)
            or isinstance(revision, bool)
            or revision < 0
            or not isinstance(digest, str)
            or _SHA256.fullmatch(digest) is None
        ):
            raise FinalCandidateGateError(
                "assistant final-candidate CONTINUE is invalid"
            )
        if remaining_iterations <= 0:
            raise FinalCandidateGateError(
                "assistant final-candidate gate requested CONTINUE without budget"
            )
        return {
            "action": "CONTINUE",
            "context": context.strip(),
            "state_revision": revision,
            "pending_sha256": digest,
        }
    if action == "REPLACE":
        content = result.get("content")
        reason_code = result.get("reason_code")
        if (
            not isinstance(content, str)
            or not content.strip()
            or not isinstance(reason_code, str)
            or not reason_code.strip()
        ):
            raise FinalCandidateGateError(
                "assistant final-candidate REPLACE is invalid"
            )
        return {
            "action": "REPLACE",
            "content": content,
            "reason_code": reason_code,
        }
    raise FinalCandidateGateError("assistant final-candidate action is invalid")


def continuation_messages(
    assistant_message: Mapping[str, Any], directive: Mapping[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build append-only, role-valid, non-durable continuation scaffolding."""
    candidate = dict(assistant_message)
    candidate["finish_reason"] = "host_candidate_continue"
    candidate["_final_candidate_synthetic"] = True
    nudge = {
        "role": "user",
        "content": directive["context"],
        "_final_candidate_synthetic": True,
    }
    return candidate, nudge


__all__ = [
    "FinalCandidateGateError",
    "continuation_messages",
    "evaluate_final_candidate",
]
