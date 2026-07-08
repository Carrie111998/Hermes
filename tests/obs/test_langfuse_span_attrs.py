"""SR-535 remainder: Langfuse-semantic grouping attributes on OTel spans.

Covers obs.otel_tracing.stamp_langfuse_attributes (the shared helper) and
cron.scheduler._stamp_cron_langfuse (the cron emitter's stamping policy).
No opentelemetry import required — spans are duck-typed fakes, matching the
sampler tests in test_infra_span_sampling.py.

Attribute names are pinned to Langfuse's OTel property mapping
(langfuse.com/docs/opentelemetry/get-started): langfuse.session.id,
langfuse.user.id, langfuse.trace.tags (string[]).
"""

from obs.otel_tracing import (
    LANGFUSE_SESSION_ID_ATTR,
    LANGFUSE_TAGS_ATTR,
    LANGFUSE_USER_ID_ATTR,
    stamp_langfuse_attributes,
)


class _RecordingSpan:
    def __init__(self):
        self.attrs = {}

    def set_attribute(self, key, value):
        self.attrs[key] = value


class _ExplodingSpan:
    def set_attribute(self, key, value):
        raise RuntimeError("boom")


# ---- attribute-name contract (doc-pinned; do not change casually) ----------

def test_attribute_names_match_langfuse_otel_mapping():
    assert LANGFUSE_SESSION_ID_ATTR == "langfuse.session.id"
    assert LANGFUSE_USER_ID_ATTR == "langfuse.user.id"
    assert LANGFUSE_TAGS_ATTR == "langfuse.trace.tags"


# ---- stamp_langfuse_attributes ----------------------------------------------

def test_stamps_all_three_fields():
    span = _RecordingSpan()
    stamp_langfuse_attributes(
        span, session_id="job:2026-07-08", user_id="main", tags=["cron", "main"]
    )
    assert span.attrs == {
        "langfuse.session.id": "job:2026-07-08",
        "langfuse.user.id": "main",
        "langfuse.trace.tags": ["cron", "main"],
    }


def test_falsy_fields_are_skipped():
    span = _RecordingSpan()
    stamp_langfuse_attributes(span, session_id=None, user_id="", tags=[])
    assert span.attrs == {}


def test_tags_coerced_to_nonempty_strings():
    span = _RecordingSpan()
    stamp_langfuse_attributes(span, tags=["scout", 7, None, ""])
    assert span.attrs == {"langfuse.trace.tags": ["scout", "7"]}


def test_all_empty_tags_sets_nothing():
    span = _RecordingSpan()
    stamp_langfuse_attributes(span, tags=[None, ""])
    assert span.attrs == {}


def test_session_and_user_coerced_to_str():
    span = _RecordingSpan()
    stamp_langfuse_attributes(span, session_id=123, user_id=456)
    assert span.attrs["langfuse.session.id"] == "123"
    assert span.attrs["langfuse.user.id"] == "456"


def test_never_raises_on_broken_span():
    stamp_langfuse_attributes(
        _ExplodingSpan(), session_id="s", user_id="u", tags=["t"]
    )  # must not raise


def test_never_raises_on_span_without_set_attribute():
    stamp_langfuse_attributes(object(), session_id="s")  # must not raise


# ---- cron emitter policy (_stamp_cron_langfuse) ------------------------------

def _expected_today():
    from cron.scheduler import _hermes_now
    return _hermes_now().date().isoformat()


def test_cron_stamp_with_explicit_job_profile():
    from cron.scheduler import _stamp_cron_langfuse

    span = _RecordingSpan()
    job = {"id": "abc123", "profile": "financier"}
    _stamp_cron_langfuse(span, job, "markets-daily")
    assert span.attrs["langfuse.session.id"] == f"markets-daily:{_expected_today()}"
    assert span.attrs["langfuse.user.id"] == "financier"
    assert span.attrs["langfuse.trace.tags"] == ["cron", "financier"]


def test_cron_stamp_falls_back_to_active_profile(monkeypatch):
    import hermes_cli.profiles as profiles
    from cron.scheduler import _stamp_cron_langfuse

    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "main")
    span = _RecordingSpan()
    _stamp_cron_langfuse(span, {"id": "abc123", "profile": ""}, "nightly-gate")
    assert span.attrs["langfuse.user.id"] == "main"
    assert span.attrs["langfuse.trace.tags"] == ["cron", "main"]


def test_cron_stamp_profile_lookup_failure_defaults(monkeypatch):
    import hermes_cli.profiles as profiles
    from cron.scheduler import _stamp_cron_langfuse

    def _boom():
        raise RuntimeError("no HERMES_HOME")

    monkeypatch.setattr(profiles, "get_active_profile_name", _boom)
    span = _RecordingSpan()
    _stamp_cron_langfuse(span, {"id": "abc123"}, "nightly-gate")
    assert span.attrs["langfuse.user.id"] == "default"
    assert span.attrs["langfuse.trace.tags"] == ["cron", "default"]
