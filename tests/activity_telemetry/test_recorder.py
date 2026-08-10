import logging

from activity_telemetry.recorder import ActivityRecorder
from activity_telemetry.schema import OutcomeLayers


def test_recorder_swallowing_store_failure_never_raises(caplog):
    class BrokenStore:
        def record_usage(self, *args, **kwargs):
            raise RuntimeError("db unavailable with secret=" + "must-not-appear")

    recorder = ActivityRecorder(store=BrokenStore(), run_id="r")
    with caplog.at_level(logging.WARNING):
        recorder.record_response(
            "anthropic",
            "claude-opus-5",
            {"input_tokens": 3, "request_count": 1},
        )
    assert "must-not-appear" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_finish_process_success_remains_semantically_unknown(tmp_path):
    recorder = ActivityRecorder.open(
        tmp_path / "activity.db",
        run_id="r",
        correlation_id="r",
        activity_id="x",
        policy_version=1,
        trigger_source="test",
        profile="main",
        effective_hermes_home=str(tmp_path),
        requested_provider="deepseek",
        requested_model="deepseek-v4-pro",
    )
    assert recorder.finish(OutcomeLayers(process="succeeded")) == "unknown"


def test_response_forwards_only_canonical_usage_fields():
    calls = []

    class SpyStore:
        def record_usage(self, *args):
            calls.append(args)

    recorder = ActivityRecorder(SpyStore(), "r")
    usage = {
        "input_tokens": 11,
        "cache_read_tokens": 22,
        "cache_write_tokens": 33,
        "output_tokens": 44,
        "reasoning_tokens": 55,
        "request_count": 2,
        "prompt_tokens": 66,
        "total_tokens": 77,
        "raw_usage": {"authorization": "not-forwarded"},
    }
    recorder.record_response("provider", "model", usage)
    _, route, delta = calls[0]
    assert (route.provider, route.model) == ("provider", "model")
    assert delta.model_calls == 2
    assert delta.uncached_input_tokens == 11
    assert delta.cache_read_tokens == 22
    assert delta.cache_write_tokens == 33
    assert delta.output_tokens == 44
    assert delta.reasoning_tokens == 55


def test_tool_retry_session_and_finish_failures_are_best_effort(caplog):
    class BrokenStore:
        def link_session(self, *args):
            raise OSError("sensitive detail")

        def record_usage(self, *args):
            raise RuntimeError("sensitive detail")

        def finish(self, *args, **kwargs):
            raise ValueError("sensitive detail")

    recorder = ActivityRecorder(BrokenStore(), "r")
    with caplog.at_level(logging.WARNING):
        assert recorder.link_session("s") is None
        assert recorder.record_tool_call(2) is None
        assert recorder.record_retry(3) is None
        assert recorder.finish(OutcomeLayers(process="failed")) == "unknown"
    assert "sensitive detail" not in caplog.text
    assert "OSError" in caplog.text
    assert "RuntimeError" in caplog.text
    assert "ValueError" in caplog.text
