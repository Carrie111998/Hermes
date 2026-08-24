import pytest

from agent.tbs_ceo_ready_guard import SYNTHETIC_FLAG, build_continuation_nudge, response_needs_continuation
from agent.turn_finalizer import _drop_verification_continuation_scaffolding


def test_tbs_not_ceo_ready_without_hard_stop_continues():
    text = (
        "Status: NOT CEO-READY / ROUTING INCOMPLETE. "
        "Next safe step: run QA and revise the deliverable."
    )

    assert response_needs_continuation(text, user_message="TBS routing work") is True
    assert build_continuation_nudge(text, user_message="TBS routing work")


def test_tbs_not_ceo_ready_with_approval_boundary_stops():
    text = (
        "Status: NOT CEO-READY as an implemented control change. "
        "Close blockers/open questions: skill patch requires Dave approval."
    )

    assert response_needs_continuation(text, user_message="TBS routing work") is False
    assert build_continuation_nudge(text, user_message="TBS routing work") is None


def test_tbs_auth_blocked_stops():
    text = "Status: NOT CEO-READY / AUTH BLOCKED until login is completed."

    assert response_needs_continuation(text, user_message="TBS client work") is False


def test_non_tbs_not_ready_text_does_not_trigger():
    text = "This generic draft is NOT CEO-READY, but it is outside the requested operating model."

    assert response_needs_continuation(text, user_message="generic writing") is False


def test_ceo_ready_text_does_not_trigger():
    text = "Status: CEO-ready and verified."

    assert response_needs_continuation(text, user_message="TBS routing work") is False


def test_tbs_guard_scaffolding_is_dropped_from_live_history():
    messages = [
        {"role": "user", "content": "do TBS routing work"},
        {"role": "assistant", "content": "interim", SYNTHETIC_FLAG: True},
        {"role": "user", "content": "continue", SYNTHETIC_FLAG: True},
        {"role": "assistant", "content": "final CEO-ready answer"},
    ]

    _drop_verification_continuation_scaffolding(messages)

    assert messages == [
        {"role": "user", "content": "do TBS routing work"},
        {"role": "assistant", "content": "final CEO-ready answer"},
    ]
