from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import requests

from hermes_cli.lanes.impls import tihna_rss

FIXTURES = Path(__file__).parent / "fixtures" / "tihna_rss"
NOW = datetime(2026, 7, 26, 12, 0, tzinfo=timezone.utc)


class Response:
    def __init__(self, payload: bytes, *, headers=None):
        self.content = payload
        self.headers = headers or {}

    def raise_for_status(self):
        return None


@pytest.fixture(autouse=True)
def clear_cache():
    tihna_rss._reset_cache_for_tests()
    yield
    tihna_rss._reset_cache_for_tests()


def _payload(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def _get(payload: bytes):
    return lambda *args, **kwargs: Response(payload)


def test_fetch_valid_atom_returns_entries():
    entries = tihna_rss.fetch_feed(
        "https://example.test/atom",
        now=NOW,
        http_get=_get(_payload("valid_atom.xml")),
    )
    assert len(entries) == 2
    assert entries[0]["title"].startswith("Binaural stimulation")


def test_fetch_valid_rss2_returns_entries():
    entries = tihna_rss.fetch_feed(
        "https://example.test/rss",
        now=NOW,
        http_get=_get(_payload("valid_rss2.xml")),
    )
    assert len(entries) == 1
    assert entries[0]["link"].endswith("rss-item-1")


def test_fetch_malformed_xml_returns_empty_and_logs_metric():
    metrics = []
    entries = tihna_rss.fetch_feed_from_bytes(
        _payload("malformed.xml"),
        feed_url="https://example.test/broken",
        now=NOW,
        metric=lambda name, value: metrics.append((name, value)),
    )
    assert entries == []
    assert metrics == [("feed_fetch_error", 1.0)]


def test_fetch_zero_entries_returns_empty():
    assert tihna_rss.fetch_feed_from_bytes(
        _payload("empty.xml"),
        feed_url="https://example.test/empty",
        now=NOW,
    ) == []


def test_fetch_entries_missing_pub_date_skipped():
    assert tihna_rss.fetch_feed_from_bytes(
        _payload("missing_pub_date.xml"),
        feed_url="https://example.test/no-date",
        now=NOW,
    ) == []


def test_fetch_oversize_response_rejected_at_5mb():
    response = Response(
        b"small",
        headers={
            "Content-Length": str(tihna_rss.MAX_RESPONSE_BYTES + 1)
        },
    )
    metrics = []
    assert tihna_rss.fetch_feed(
        "https://example.test/large",
        now=NOW,
        http_get=lambda *args, **kwargs: response,
        sleep=lambda _seconds: None,
        metric=lambda name, value: metrics.append((name, value)),
    ) == []
    assert metrics == [("feed_fetch_error", 1.0)]


def test_fetch_timeout_10s_enforced():
    timeouts = []

    def timeout(*args, **kwargs):
        timeouts.append(kwargs["timeout"])
        raise requests.Timeout("synthetic")

    tihna_rss.fetch_feed(
        "https://example.test/timeout",
        now=NOW,
        http_get=timeout,
        sleep=lambda _seconds: None,
    )
    assert timeouts == [10, 10]


def test_fetch_retry_once_on_transient_error():
    attempts = []
    sleeps = []

    def sometimes(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.ConnectionError("synthetic")
        return Response(_payload("valid_atom.xml"))

    entries = tihna_rss.fetch_feed(
        "https://example.test/retry",
        now=NOW,
        http_get=sometimes,
        sleep=sleeps.append,
    )
    assert len(attempts) == 2
    assert sleeps == [2.0]
    assert len(entries) == 2


def test_fetch_records_feed_fetch_error_metric_on_final_failure():
    metrics = []

    def fail(*args, **kwargs):
        raise requests.ConnectionError("synthetic")

    tihna_rss.fetch_feed(
        "https://example.test/fail",
        now=NOW,
        http_get=fail,
        sleep=lambda _seconds: None,
        metric=lambda name, value: metrics.append((name, value)),
    )
    assert metrics == [("feed_fetch_error", 1.0)]


def test_sanitize_strips_html_tags():
    result = tihna_rss.sanitize_text(
        "<p>Hello <strong>world</strong></p>",
        limit=4000,
    )
    assert result == "Hello world"


def test_sanitize_caps_body_at_4000_chars():
    result = tihna_rss.sanitize_text("x" * 5000, limit=4000)
    assert len(result) == 4000
    assert result.endswith("…")


def test_sanitize_caps_title_at_300_chars():
    result = tihna_rss.sanitize_text("y" * 500, limit=300)
    assert len(result) == 300
    assert result.endswith("…")


def test_external_id_deterministic_across_reruns():
    entry = {"id": "one", "link": "https://example.test/one"}
    assert tihna_rss.external_id_for(
        "https://feed.test/rss", entry
    ) == tihna_rss.external_id_for("https://feed.test/rss", entry)


def test_external_id_differs_across_feeds_with_same_entry():
    entry = {"id": "one", "link": "https://example.test/one"}
    assert tihna_rss.external_id_for(
        "https://a.test/rss", entry
    ) != tihna_rss.external_id_for("https://b.test/rss", entry)
