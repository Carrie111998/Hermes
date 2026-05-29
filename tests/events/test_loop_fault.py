import pytest
from events.schema import EventType, Priority


class _FakeBus:
    def __init__(self):
        self.emitted = []

    def emit(self, *, event_type, source, payload, priority=None, **kw):
        self.emitted.append((event_type, source, payload, priority))
        return "evt-id"


def _make_exc():
    try:
        raise TypeError("'NoneType' object is not iterable")
    except TypeError as exc:
        return exc


def test_emit_agent_loop_fault_emits_high_priority_event():
    from events.loop_fault import emit_agent_loop_fault, reset_rate_cap
    reset_rate_cap()
    bus = _FakeBus()
    emit_agent_loop_fault(_make_exc(), source_hint="jobflow-scout",
                          phase="stream_accumulation", provider="openai-codex",
                          model="gpt-5.5", status_code=None, bus=bus)
    assert len(bus.emitted) == 1
    et, source, payload, priority = bus.emitted[0]
    assert et is EventType.AGENT_LOOP_FAULT
    assert priority is Priority.HIGH
    assert source == "scout"  # canonical_agent_source('jobflow-scout')
    assert payload["exception_type"] == "TypeError"
    assert "NoneType" in payload["message"]
    assert payload["provider"] == "openai-codex"
    assert payload["phase"] == "stream_accumulation"
    assert "traceback_tail" in payload and payload["traceback_tail"]


def test_emit_ignores_classification_always_fires():
    from events.loop_fault import emit_agent_loop_fault, reset_rate_cap
    reset_rate_cap()
    bus = _FakeBus()
    emit_agent_loop_fault(ValueError("boom"), source_hint="main", phase="x", bus=bus)
    assert len(bus.emitted) == 1


def test_per_process_rate_cap_bounds_volume():
    from events.loop_fault import emit_agent_loop_fault, reset_rate_cap, _RATE_CAP_MAX
    reset_rate_cap()
    bus = _FakeBus()
    for _ in range(100):
        emit_agent_loop_fault(_make_exc(), source_hint="jobflow-scout",
                              phase="stream_accumulation", bus=bus)
    assert len(bus.emitted) <= _RATE_CAP_MAX


def test_emit_never_raises_even_if_bus_explodes():
    from events.loop_fault import emit_agent_loop_fault, reset_rate_cap
    reset_rate_cap()

    class _Boom:
        def emit(self, **kw):
            raise RuntimeError("bus down")

    emit_agent_loop_fault(_make_exc(), source_hint="x", phase="y", bus=_Boom())
