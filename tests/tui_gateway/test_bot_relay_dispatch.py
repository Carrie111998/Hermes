"""Dispatch isolation for durable bot-relay lease control."""

from __future__ import annotations

import tui_gateway.server as srv


class _ImmediateExecutor:
    def __init__(self) -> None:
        self.calls = 0

    def submit(self, fn):
        self.calls += 1
        fn()


class _Transport:
    def __init__(self) -> None:
        self.messages: list[dict] = []

    def write(self, message: dict) -> bool:
        self.messages.append(message)
        return True


def test_lease_control_has_reserved_workers_when_deliver_pool_is_saturated(
    monkeypatch,
) -> None:
    general = _ImmediateExecutor()
    control = _ImmediateExecutor()
    transport = _Transport()
    monkeypatch.setattr(srv, "_pool", general)
    monkeypatch.setattr(srv, "_relay_control_pool", control)
    monkeypatch.setattr(
        srv,
        "handle_request",
        lambda request: {"id": request["id"], "result": {"ok": True}},
    )

    renew = {
        "id": "renew",
        "method": "bot_relay.outbox.renew",
        "params": {},
    }
    deliver = {
        "id": "deliver",
        "method": "bot_relay.deliver",
        "params": {},
    }

    assert srv.dispatch(renew, transport) is None
    assert control.calls == 1
    assert general.calls == 0
    assert transport.messages[-1]["id"] == "renew"

    assert srv.dispatch(deliver, transport) is None
    assert control.calls == 1
    assert general.calls == 1
    assert transport.messages[-1]["id"] == "deliver"
