"""Responses API transcript-boundary policy.

Normal agent runs return an authoritative full transcript. Legacy integrations
and tests may still return an unmarked current-turn suffix, so that compatibility
path remains conservative and separate from the explicit full-transcript contract.
"""

from __future__ import annotations

from typing import Any, Dict, List

from agent.turn_context import reanchor_current_turn_user_idx

FULL_TRANSCRIPT_MODE = "full"

_SEMANTIC_KEYS = (
    "role",
    "content",
    "tool_calls",
    "tool_call_id",
    "name",
    "function_call",
)
_MESSAGE_ROLES = frozenset({"system", "developer", "user", "assistant", "tool"})


def semantic_message(message: Any) -> Any:
    """Project a message to fields that define transcript identity."""
    if not isinstance(message, dict) or message.get("role") not in _MESSAGE_ROLES:
        return None
    return {key: message[key] for key in _SEMANTIC_KEYS if key in message}


def semantic_prefix_matches(messages: List[Any], expected: List[Any]) -> bool:
    """Return whether ``messages`` begins with ``expected`` semantically."""
    if len(messages) < len(expected):
        return False
    for actual, wanted in zip(messages, expected):
        actual_semantic = semantic_message(actual)
        wanted_semantic = semantic_message(wanted)
        if (
            actual_semantic is None
            or wanted_semantic is None
            or actual_semantic != wanted_semantic
        ):
            return False
    return True


def response_messages_turn_start_index(
    conversation_history: List[Dict[str, Any]],
    user_message: Any,
    result: Dict[str, Any],
) -> int:
    """Return the first assistant/tool row belonging to the current turn.

    Full transcripts use the agent core's canonical current-user re-anchoring
    policy. Unmarked legacy results retain suffix/full-prefix compatibility.
    """
    agent_messages = result.get("messages") if isinstance(result, dict) else None
    if not isinstance(agent_messages, list) or not agent_messages:
        return 0

    if result.get("_transcript_mode") == FULL_TRANSCRIPT_MODE:
        current_user_idx = reanchor_current_turn_user_idx(agent_messages, user_message)
        return current_user_idx + 1 if current_user_idx >= 0 else len(agent_messages)

    prior = list(conversation_history)
    current_user = {"role": "user", "content": user_message}
    expected_with_user = prior + [current_user]
    if semantic_prefix_matches(agent_messages, expected_with_user):
        return len(expected_with_user)
    if prior and semantic_prefix_matches(agent_messages, prior):
        return len(prior)

    # Legacy compressed mocks historically signal an authoritative transcript
    # without the newer explicit mode marker.
    if result.get("_compressed"):
        current_user_idx = reanchor_current_turn_user_idx(agent_messages, user_message)
        return current_user_idx + 1 if current_user_idx >= 0 else len(agent_messages)

    # An assistant/tool-only result is the supported legacy suffix shape.
    if not any(
        isinstance(message, dict) and message.get("role") == "user"
        for message in agent_messages
    ):
        return 0

    # An unmarked transcript with user rows is ambiguous. Preserve the historical
    # fail-closed behavior: expose only its final assistant, never historical tools.
    for index in range(len(agent_messages) - 1, -1, -1):
        message = agent_messages[index]
        if isinstance(message, dict) and message.get("role") == "assistant":
            return index
    return len(agent_messages)


def build_response_conversation_history(
    conversation_history: List[Dict[str, Any]],
    user_message: Any,
    result: Dict[str, Any],
    final_response: Any,
) -> List[Dict[str, Any]]:
    """Build stored history under the explicit full/legacy suffix contract."""
    agent_messages = result.get("messages") if isinstance(result, dict) else None
    if isinstance(agent_messages, list):
        if result.get("_transcript_mode") == FULL_TRANSCRIPT_MODE:
            return list(agent_messages)
        if agent_messages:
            turn_start = response_messages_turn_start_index(
                conversation_history, user_message, result
            )
            if turn_start or result.get("_compressed"):
                return list(agent_messages)
            return [
                *conversation_history,
                {"role": "user", "content": user_message},
                *agent_messages,
            ]

    return [
        *conversation_history,
        {"role": "user", "content": user_message},
        {"role": "assistant", "content": final_response},
    ]
