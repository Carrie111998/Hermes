"""Source-level reproduction for delegated approval routing.

This intentionally pins the current defective behavior as investigation evidence;
it is not the desired regression contract.  The implementation plan beside this
test specifies the RED tests that should replace it when the resolver is built.
"""

from __future__ import annotations

import contextvars
import threading

from agent.delegation_context import delegated_child_context
from tools import approval


def test_delegated_child_approval_reuses_parent_user_route(monkeypatch):
    """A child request is queued/notified as if it were the parent user prompt."""
    parent_session_key = "parent-harvis-session"
    child_session_id = "child-quinn-session"
    notified: list[dict] = []
    result: dict = {}

    monkeypatch.setattr(approval, "_get_approval_timeout", lambda: 5)
    approval._gateway_queues.clear()
    approval._gateway_notify_cbs.clear()

    session_token = approval.set_current_session_key(parent_session_key)
    try:
        parent_user_notify = notified.append
        approval.register_gateway_notify(parent_session_key, parent_user_notify)
        inherited_context = contextvars.copy_context()

        def child_wait() -> None:
            with delegated_child_context(child_session_id):
                # The child has its own durable session id, but approval routing
                # still resolves to the parent's approval session key.
                assert approval.get_current_session_key() == parent_session_key
                result.update(
                    approval._await_gateway_decision(
                        approval.get_current_session_key(),
                        parent_user_notify,
                        {
                            "command": "PYTHONPATH=. python -m unittest tests.safe_review",
                            "description": "scanner HIGH false positive in isolated review",
                            "pattern_key": "python-module-execution",
                            "pattern_keys": ["python-module-execution"],
                        },
                    )
                )

        thread = threading.Thread(
            target=lambda: inherited_context.run(child_wait), daemon=True
        )
        thread.start()

        for _ in range(200):
            if approval.has_blocking_approval(parent_session_key):
                break
            threading.Event().wait(0.005)
        assert approval.has_blocking_approval(parent_session_key)
        assert notified == [
            {
                "command": "PYTHONPATH=. python -m unittest tests.safe_review",
                "description": "scanner HIGH false positive in isolated review",
                "pattern_key": "python-module-execution",
                "pattern_keys": ["python-module-execution"],
            }
        ]

        # Only the same parent session-keyed user response API can release it.
        assert approval.resolve_gateway_approval(parent_session_key, "deny") == 1
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result["resolved"] is True
        assert result["choice"] == "deny"
    finally:
        approval.unregister_gateway_notify(parent_session_key)
        approval.reset_current_session_key(session_token)
        approval._gateway_queues.clear()
        approval._gateway_notify_cbs.clear()
