"""Regression: leaked approval callbacks must not poison later tests.

``tools.computer_use.tool`` keeps the approval callback in a ContextVar
plus the per-session unlock stores as module-globals. Without the autouse
reset fixture in ``tests/conftest.py``, a test that installs a callback
and "forgets" it changes the behavior of every later computer-use test in
the same context: a raising callback becomes ``verdict = "deny"``
(dispatch tests see an empty backend call list), a blocking callback
hangs the run. The pair below simulates the forgetful test and asserts
the next test still sees the pristine fail-closed denial.
"""

import json


def _install_backend(cu_tool):
    class _RecordingBackend:
        def __init__(self):
            self.calls = []

        def start(self):
            pass

        def stop(self):
            pass

        def is_available(self):
            return True

        def click(self, **kw):
            self.calls.append(("click", kw))
            from tools.computer_use.backend import ActionResult

            return ActionResult(ok=True, action="click")

        def capture(self, mode="som", app=None):
            from tools.computer_use.backend import CaptureResult

            return CaptureResult(
                mode=mode, width=1, height=1, png_b64=None, elements=[],
                app="X", window_title="",
            )

    backend = _RecordingBackend()
    cu_tool.reset_backend_for_tests()
    cu_tool._backend = backend
    return backend


def test_a_forgets_a_poisoned_approval_callback():
    """Simulates the polluter: installs a callback with the LEGACY
    two-argument signature and deliberately does not reset it."""
    from tools.computer_use import tool as cu_tool

    def stale_two_arg_callback(action, args):  # wrong arity on purpose
        return "approve_once"

    token = cu_tool.set_approval_callback(stale_two_arg_callback)
    assert token is not None
    # deliberately no reset — the autouse fixture must clean this up


def test_b_still_fails_closed_without_a_callback():
    """Without the isolation fixture this fails differently: the stale
    callback raises (arity), ``_request_approval`` converts that into a
    "denied by user", and the error never carries the pristine
    ``approval_unavailable`` code. With the fixture, pristine state is
    fail-closed: no callback wired means the destructive action is denied
    with ``approval_unavailable`` and the backend is never touched.
    """
    from tools.computer_use import tool as cu_tool

    backend = _install_backend(cu_tool)
    result = cu_tool.handle_computer_use({"action": "click", "element": 3})
    call_names = [c[0] for c in backend.calls]
    assert "click" not in call_names, (
        f"leaked approval callback or stale grant dispatched the click: {result!r}"
    )
    payload = json.loads(result) if isinstance(result, str) else result
    assert isinstance(payload, dict)
    assert payload.get("code") == "approval_unavailable", (
        f"expected the pristine fail-closed denial, got: {result!r}"
    )


def _grant_rule_key(session_id, action):
    """The exact allowlist key the shared-gate bridge derives for a grant."""
    from tools.computer_use.tool import _shared_approval_rule_key

    return f"plugin_rule:{_shared_approval_rule_key(session_id, action, {})}"


def test_headless_context_never_uses_another_contexts_responder():
    """Session-owned callback identity: a concurrent context with no callback
    of its own must not route its mutation through another context's
    responder. It enters the shared headless approval path and fails closed
    (no interactive user or gateway in the test env)."""
    import threading

    from tools.computer_use import tool as cu_tool

    backend = _install_backend(cu_tool)
    invoked = []
    responder_ready = threading.Event()
    decision_made = threading.Event()

    def responder(action, args, summary):
        invoked.append(action)
        return "approve_once"

    def interactive_context():
        token = cu_tool.set_approval_callback(responder)
        try:
            responder_ready.set()
            # stay alive with the callback installed while the headless
            # context decides, so a global slot would be observable
            decision_made.wait(timeout=5)
        finally:
            cu_tool.reset_approval_callback(token)

    thread = threading.Thread(target=interactive_context)
    thread.start()
    assert responder_ready.wait(timeout=5)

    result = cu_tool.handle_computer_use({"action": "click", "element": 3})
    decision_made.set()
    thread.join(timeout=5)

    assert not thread.is_alive()
    assert invoked == [], (
        "headless context routed through another context's responder"
    )
    assert [c[0] for c in backend.calls] == [], f"click dispatched: {result!r}"
    payload = json.loads(result)
    assert payload.get("code") == "approval_unavailable"


def test_interactive_grant_does_not_authorize_cron_run(monkeypatch):
    """A grant earned in an interactive approval session is scoped to that
    session: a cron context (cron_mode defaults to deny) reusing the same
    computer_use session_id must still be denied."""
    from tools import approval as approval_mod
    from tools.computer_use import tool as cu_tool

    backend = _install_backend(cu_tool)
    monkeypatch.setattr(approval_mod, "get_current_session_key",
                        lambda *a, **k: "interactive-1")
    key = _grant_rule_key("cu-session-1", "click")
    approval_mod.approve_session("interactive-1", key)
    approval_mod.approve_permanent(key)
    result = cu_tool.handle_computer_use(
        {"action": "click", "element": 3}, session_id="cu-session-1")
    granted = json.loads(result) if isinstance(result, str) else result
    assert not granted.get("code") and not granted.get("error"), (
        f"grant did not authorize its own session: {result!r}"
    )

    backend.calls.clear()
    monkeypatch.setattr(approval_mod, "get_current_session_key",
                        lambda *a, **k: "cron-1")
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    result = cu_tool.handle_computer_use(
        {"action": "click", "element": 3}, session_id="cu-session-1")
    assert [c[0] for c in backend.calls] == [], (
        f"cron run used the interactive session's grant: {result!r}"
    )
    payload = json.loads(result)
    assert "cron" in payload.get("error", "").lower()
    assert payload.get("code") == "approval_unavailable"


def test_interactive_grant_does_not_authorize_single_query_run(monkeypatch):
    """Same scoping for single-query (-q) sessions: the grant stays with the
    session that earned it, and single_query_mode (default deny) governs."""
    from tools import approval as approval_mod
    from tools.computer_use import tool as cu_tool

    backend = _install_backend(cu_tool)
    monkeypatch.setattr(approval_mod, "get_current_session_key",
                        lambda *a, **k: "interactive-2")
    key = _grant_rule_key("cu-session-2", "type")
    approval_mod.approve_session("interactive-2", key)
    approval_mod.approve_permanent(key)
    result = cu_tool.handle_computer_use(
        {"action": "type", "text": "hi"}, session_id="cu-session-2")
    granted = json.loads(result) if isinstance(result, str) else result
    assert not granted.get("code") and not granted.get("error"), (
        f"grant did not authorize its own session: {result!r}"
    )

    backend.calls.clear()
    monkeypatch.setattr(approval_mod, "get_current_session_key",
                        lambda *a, **k: "single-query-1")
    monkeypatch.setenv("HERMES_SINGLE_QUERY_SESSION", "1")
    result = cu_tool.handle_computer_use(
        {"action": "type", "text": "hi"}, session_id="cu-session-2")
    assert [c[0] for c in backend.calls] == [], (
        f"single-query run used the interactive session's grant: {result!r}"
    )
    payload = json.loads(result)
    assert "single-query" in payload.get("error", "").lower()
    assert payload.get("code") == "approval_unavailable"
