"""computer_use must not fail open when no CLI approval callback is wired.

``_request_approval`` used to ``return None`` (allow) whenever
``_approval_callback`` was unset, on the reasoning that "Gateway approval is
handled one layer out via the normal tool-approval infra". Nothing routes
``computer_use`` into that infra: ``ToolRegistry.dispatch`` calls the handler
directly, ``model_tools``' pre-dispatch hook covers ACP *edit* approval only,
and ``tools.approval``'s own gate is command-shaped, so it never sees a tool
call that carries no command string. The only reachable path into
``request_tool_approval`` is a plugin ``pre_tool_call`` rule naming the tool,
which the default install does not have (issue #87724).

So every non-CLI entry point — gateway, cron, ``-q``, a bare script — could
click, drag, type and raise windows on a real desktop with no human asked.
These tests pin the routed behavior at the dispatch level rather than against
the callback, since the callback is exactly the thing that is absent in the
contexts that matter.
"""

import json

import pytest

import tools.approval as approval


@pytest.fixture(autouse=True)
def _isolate_approval_state(monkeypatch):
    """Clean session key, empty allowlists, no yolo, no thread callback.

    Mirrors ``tests/tools/test_request_tool_approval.py`` so both entry
    points into the shared gate are set up identically.
    """
    monkeypatch.setattr(
        approval, "get_current_session_key",
        lambda default="default": "test-session",
    )
    monkeypatch.setattr(approval, "is_approved", lambda sk, pk: False)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: False)
    monkeypatch.setattr(approval, "_YOLO_MODE_FROZEN", False, raising=False)
    monkeypatch.setattr(
        "tools.terminal_tool._get_approval_callback", lambda: None, raising=False
    )
    yield


def _headless(monkeypatch):
    """No interactive CLI, no gateway, no cron: the bare-script context."""
    monkeypatch.setattr(approval, "_is_interactive_cli", lambda: False)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: False)
    monkeypatch.setattr(
        approval, "_is_single_query_approval_context", lambda: False, raising=False
    )


class _RecordingBackend:
    """Minimal backend that records the calls it is asked to make."""

    def __init__(self):
        self.calls = []

    def start(self):
        pass

    def stop(self):
        pass

    def is_available(self):
        return True

    def click(self, **kw):
        from tools.computer_use.backend import ActionResult

        self.calls.append(("click", kw))

        return ActionResult(ok=True, action="click")


@pytest.fixture
def backend():
    from tools.computer_use import tool as cu_tool

    be = _RecordingBackend()
    cu_tool.reset_backend_for_tests()
    cu_tool._backend = be
    yield be
    cu_tool.reset_backend_for_tests()


def _click(args=None):
    """Dispatch a click the way the model does: through the registry."""
    import tools.computer_use_tool  # noqa: F401  (registers the tool)
    from tools.registry import registry

    payload = {"action": "click", "element": 3}
    payload.update(args or {})
    out = registry.dispatch("computer_use", payload)

    return json.loads(out) if isinstance(out, str) else out


def test_registry_dispatch_is_gated_when_no_human_is_present(backend, monkeypatch):
    """The reported bug, at the layer the reporter checked.

    Nothing between the model and ``handle_computer_use`` asks anyone, so if
    this dispatch reaches the backend, the click happened on a real desktop
    with nobody consulted.
    """
    _headless(monkeypatch)

    parsed = _click()

    assert [name for name, _ in backend.calls] == [], (
        "a destructive action reached the backend with no approval callback "
        "registered and no human present to approve it"
    )
    assert "error" in parsed
    assert "approval" in parsed["error"].lower() or "BLOCKED" in parsed["error"]


def test_cron_deny_mode_blocks_the_click(backend, monkeypatch):
    """Parity with dangerous shell commands, which cron_mode already governs."""
    _headless(monkeypatch)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: True)
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "deny")

    parsed = _click()

    assert [name for name, _ in backend.calls] == []
    assert "cron" in parsed["error"].lower()


def test_cron_approve_mode_allows_the_click(backend, monkeypatch):
    """The other half of the same knob: an operator who opted in still runs.

    Worth pinning next to the deny case, because a fail-closed change that
    also broke ``cron_mode: approve`` would look correct from the deny test
    alone.
    """
    _headless(monkeypatch)
    monkeypatch.setattr(approval, "_is_cron_approval_context", lambda: True)
    monkeypatch.setattr(approval, "_get_cron_approval_mode", lambda: "approve")

    parsed = _click()

    assert [name for name, _ in backend.calls] == ["click"]
    assert "error" not in parsed


def test_gateway_context_queues_a_pending_approval(backend, monkeypatch):
    """The behavior the old comment promised but nothing delivered.

    With no notify callback attached (an API-server session with no chat
    surface), the shared gate queues the action for ``/approve`` review
    instead of running it.
    """
    _headless(monkeypatch)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: True)
    submitted = []
    monkeypatch.setattr(
        approval, "submit_pending",
        lambda session_key, data: submitted.append((session_key, data)),
    )

    _click()

    assert [name for name, _ in backend.calls] == []
    assert submitted, "the gateway never saw the action it was said to gate"
    assert submitted[0][1]["pattern_key"] == (
        "plugin_rule:computer_use:click:background"
    )


def test_foreground_keeps_its_own_approval_scope(backend, monkeypatch):
    """#67052's separation must survive the trip through the shared gate.

    A background approval must not silently authorize the foreground form of
    the same action, so the two have to reach ``request_tool_approval`` under
    different allowlist keys.
    """
    _headless(monkeypatch)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: True)
    submitted = []
    monkeypatch.setattr(
        approval, "submit_pending",
        lambda session_key, data: submitted.append(data["pattern_key"]),
    )

    _click()
    _click({"delivery_mode": "foreground"})

    assert submitted == [
        "plugin_rule:computer_use:click:background",
        "plugin_rule:computer_use:click:foreground",
    ]


def test_bring_to_front_is_gated_separately_from_the_input(backend, monkeypatch):
    """``focus_app(raise_window=True)`` takes the second rung.

    It is the rung most obviously wrong to auto-allow headlessly: a visible
    focus change with, by definition, nobody watching the screen. The input
    rung is pre-approved here so the run actually reaches the second one.
    """
    _headless(monkeypatch)
    monkeypatch.setattr(approval, "_is_gateway_approval_context", lambda: True)
    monkeypatch.setattr(
        approval, "is_approved",
        lambda sk, pk: pk == "plugin_rule:computer_use:focus_app:background",
    )
    submitted = []
    monkeypatch.setattr(
        approval, "submit_pending",
        lambda session_key, data: submitted.append(data["pattern_key"]),
    )

    import tools.computer_use_tool  # noqa: F401
    from tools.registry import registry

    registry.dispatch(
        "computer_use", {"action": "focus_app", "app": "Mail", "raise_window": True}
    )

    assert submitted == ["plugin_rule:computer_use:bring_to_front:background"], (
        "raising a window inherited the input approval instead of taking its "
        "own rung"
    )


def test_yolo_still_bypasses_the_gate(backend, monkeypatch):
    """No change for a user who explicitly opted into unattended operation."""
    _headless(monkeypatch)
    monkeypatch.setattr(approval, "is_current_session_yolo_enabled", lambda: True)

    parsed = _click()

    assert [name for name, _ in backend.calls] == ["click"]
    assert "error" not in parsed


def test_registered_cli_callback_path_is_unchanged(backend, monkeypatch):
    """The interactive CLI is untouched: its callback still decides.

    ``request_tool_approval`` is only consulted on the branch that used to
    return None, so a denial here must still come back as the callback's
    verdict and never reach the shared gate.
    """
    from tools.computer_use import tool as cu_tool

    _headless(monkeypatch)
    monkeypatch.setattr(
        approval, "request_tool_approval",
        lambda *a, **k: pytest.fail("registered callback must decide on its own"),
    )
    cu_tool.set_approval_callback(lambda action, args, summary: "deny")

    parsed = _click()

    assert [name for name, _ in backend.calls] == []
    assert parsed["error"] == "denied by user"


def test_gate_failure_is_not_consent(backend, monkeypatch):
    """An approval gate that cannot run must block, not shrug and continue."""
    _headless(monkeypatch)

    def _explode(*a, **k):
        raise RuntimeError("approval store unreadable")

    monkeypatch.setattr(approval, "request_tool_approval", _explode)

    parsed = _click()

    assert [name for name, _ in backend.calls] == []
    assert "approval gate unavailable" in parsed["error"]
