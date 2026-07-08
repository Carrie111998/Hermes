"""SR-535: head sampling of infrastructure OTel spans at the export boundary.

Unit tests for obs.otel_tracing's _InfraSamplingSpanProcessor and its config
plumbing. No opentelemetry import required — the processor is duck-typed by
design (see the class docstring), so fakes stand in for ReadableSpan and the
wrapped BatchSpanProcessor.
"""

from types import SimpleNamespace

import pytest

from obs.otel_tracing import (
    _INFRA_PREFIXES_DEFAULT,
    _INFRA_SAMPLE_DEFAULT,
    _infra_sampling_config,
    _InfraSamplingSpanProcessor,
    _span_is_error,
    _wrap_with_infra_sampling,
)


# ---- fakes -----------------------------------------------------------------

class _FakeProcessor:
    """Records delegated calls; stands in for BatchSpanProcessor."""

    def __init__(self):
        self.started = []
        self.ended = []
        self.shutdown_called = False
        self.flush_calls = []

    def on_start(self, span, parent_context=None):
        self.started.append(span)

    def on_end(self, span):
        self.ended.append(span)

    def shutdown(self):
        self.shutdown_called = True

    def force_flush(self, timeout_millis=30000):
        self.flush_calls.append(timeout_millis)
        return True


def _span(name, trace_id=0, is_ok=True, events=()):
    """Minimal ReadableSpan stand-in: name, context.trace_id, status, events."""
    return SimpleNamespace(
        name=name,
        context=SimpleNamespace(trace_id=trace_id),
        status=SimpleNamespace(is_ok=is_ok),
        events=list(events),
    )


# trace_ids on either side of a 0.5 bound (lower 64 bits compared to
# ratio * 2**64, same scheme as OTel's TraceIdRatioBased).
_TID_LOW = 1  # far below any non-zero bound -> kept
_TID_HIGH = (1 << 64) - 1  # at the very top -> dropped for any ratio < 1


# ---- config parsing --------------------------------------------------------

def test_config_defaults(monkeypatch):
    monkeypatch.delenv("HERMES_OTEL_SUBSCRIBER_SAMPLE", raising=False)
    monkeypatch.delenv("HERMES_OTEL_INFRA_SPAN_PREFIXES", raising=False)
    prefixes, ratio = _infra_sampling_config()
    assert prefixes == (_INFRA_PREFIXES_DEFAULT,) == ("subscriber.handle",)
    assert ratio == _INFRA_SAMPLE_DEFAULT == 0.02


def test_config_env_overrides(monkeypatch):
    monkeypatch.setenv("HERMES_OTEL_SUBSCRIBER_SAMPLE", "0.1")
    monkeypatch.setenv(
        "HERMES_OTEL_INFRA_SPAN_PREFIXES",
        "subscriber.handle, cron.run_job:cron-stale-monitor ,",
    )
    prefixes, ratio = _infra_sampling_config()
    assert prefixes == ("subscriber.handle", "cron.run_job:cron-stale-monitor")
    assert ratio == 0.1


def test_config_bad_ratio_falls_back_to_default(monkeypatch):
    monkeypatch.setenv("HERMES_OTEL_SUBSCRIBER_SAMPLE", "not-a-float")
    _, ratio = _infra_sampling_config()
    assert ratio == _INFRA_SAMPLE_DEFAULT


@pytest.mark.parametrize("raw,expected", [("-0.5", 0.0), ("7", 1.0)])
def test_config_ratio_clamped(monkeypatch, raw, expected):
    monkeypatch.setenv("HERMES_OTEL_SUBSCRIBER_SAMPLE", raw)
    _, ratio = _infra_sampling_config()
    assert ratio == expected


# ---- sampling decisions ----------------------------------------------------

def test_non_infra_spans_always_exported_even_at_ratio_zero():
    fake = _FakeProcessor()
    proc = _InfraSamplingSpanProcessor(fake, ("subscriber.handle",), 0.0)
    for name in ("cron.run_job:tailor-cron-triage", "scout.scan", "agent.invoke"):
        proc.on_end(_span(name, trace_id=_TID_HIGH))
    assert [s.name for s in fake.ended] == [
        "cron.run_job:tailor-cron-triage", "scout.scan", "agent.invoke",
    ]


def test_infra_span_dropped_above_bound_kept_below():
    fake = _FakeProcessor()
    proc = _InfraSamplingSpanProcessor(fake, ("subscriber.handle",), 0.5)
    kept = _span("subscriber.handle:mailbox-translator", trace_id=_TID_LOW)
    dropped = _span("subscriber.handle:mailbox-translator", trace_id=_TID_HIGH)
    proc.on_end(kept)
    proc.on_end(dropped)
    assert fake.ended == [kept]


def test_infra_span_sampling_is_deterministic_per_trace_id():
    fake = _FakeProcessor()
    proc = _InfraSamplingSpanProcessor(fake, ("subscriber.handle",), 0.02)
    span = _span("subscriber.handle:audit-logger", trace_id=_TID_HIGH)
    for _ in range(10):
        proc.on_end(span)
    assert fake.ended == []  # same trace_id -> same decision every time


def test_error_status_infra_span_always_exported():
    fake = _FakeProcessor()
    proc = _InfraSamplingSpanProcessor(fake, ("subscriber.handle",), 0.0)
    err = _span("subscriber.handle:telegram-notifier", trace_id=_TID_HIGH,
                is_ok=False)
    proc.on_end(err)
    assert fake.ended == [err]


def test_exception_event_infra_span_always_exported():
    fake = _FakeProcessor()
    proc = _InfraSamplingSpanProcessor(fake, ("subscriber.handle",), 0.0)
    exc_span = _span("subscriber.handle:memory-writer", trace_id=_TID_HIGH,
                     events=[SimpleNamespace(name="exception")])
    proc.on_end(exc_span)
    assert fake.ended == [exc_span]


def test_uninspectable_span_fails_open():
    fake = _FakeProcessor()
    proc = _InfraSamplingSpanProcessor(fake, ("subscriber.handle",), 0.0)
    # No context attribute at all -> exported rather than lost.
    weird = SimpleNamespace(name="subscriber.handle:x", status=None, events=None)
    proc.on_end(weird)
    assert fake.ended == [weird]


def test_ratio_one_keeps_everything():
    fake = _FakeProcessor()
    proc = _InfraSamplingSpanProcessor(fake, ("subscriber.handle",), 1.0)
    span = _span("subscriber.handle:whatsapp-escalator", trace_id=_TID_HIGH)
    proc.on_end(span)
    assert fake.ended == [span]


# ---- delegation + wiring ---------------------------------------------------

def test_processor_delegates_lifecycle_calls():
    fake = _FakeProcessor()
    proc = _InfraSamplingSpanProcessor(fake, ("subscriber.handle",), 0.0)
    span = _span("subscriber.handle:x", trace_id=_TID_HIGH)
    proc.on_start(span)
    assert fake.started == [span]  # on_start is never filtered
    proc.shutdown()
    assert fake.shutdown_called
    assert proc.force_flush(1234) is True
    assert fake.flush_calls == [1234]


def test_on_ending_hook_delegated_when_present():
    """OTel >=1.34 calls _on_ending on every processor; must not AttributeError."""
    fake = _FakeProcessor()
    fake._on_ending_calls = []
    fake._on_ending = fake._on_ending_calls.append
    proc = _InfraSamplingSpanProcessor(fake, ("subscriber.handle",), 0.0)
    span = _span("subscriber.handle:x", trace_id=_TID_HIGH)
    proc._on_ending(span)
    assert fake._on_ending_calls == [span]


def test_on_ending_hook_noop_when_wrapped_lacks_it():
    proc = _InfraSamplingSpanProcessor(
        _FakeProcessor(), ("subscriber.handle",), 0.0
    )
    proc._on_ending(_span("subscriber.handle:x"))  # must not raise


def test_wrap_returns_original_when_sampling_off(monkeypatch):
    fake = _FakeProcessor()
    monkeypatch.setenv("HERMES_OTEL_SUBSCRIBER_SAMPLE", "1.0")
    assert _wrap_with_infra_sampling(fake) is fake
    monkeypatch.setenv("HERMES_OTEL_SUBSCRIBER_SAMPLE", "0.02")
    monkeypatch.setenv("HERMES_OTEL_INFRA_SPAN_PREFIXES", " , ")
    assert _wrap_with_infra_sampling(fake) is fake


def test_wrap_enabled_by_default(monkeypatch):
    fake = _FakeProcessor()
    monkeypatch.delenv("HERMES_OTEL_SUBSCRIBER_SAMPLE", raising=False)
    monkeypatch.delenv("HERMES_OTEL_INFRA_SPAN_PREFIXES", raising=False)
    wrapped = _wrap_with_infra_sampling(fake)
    assert isinstance(wrapped, _InfraSamplingSpanProcessor)
    assert wrapped._wrapped is fake


# ---- _span_is_error edge cases ----------------------------------------------

def test_span_is_error_variants():
    assert _span_is_error(_span("x", is_ok=False))
    assert _span_is_error(_span("x", events=[SimpleNamespace(name="exception")]))
    assert not _span_is_error(_span("x"))
    assert not _span_is_error(SimpleNamespace(name="x"))  # no status/events attrs
