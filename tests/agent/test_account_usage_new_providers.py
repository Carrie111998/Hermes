"""Fetchers added 2026-08-22: opencode-go, anthropic2, grok (CDP scrape).

Mirrors the fake-client pattern of tests/agent/test_account_usage.py: httpx
is patched through the module attribute so no test touches the network.
"""

from datetime import datetime, timezone

import pytest

from agent import account_usage


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, calls, payloads):
        self.calls = calls
        self.payloads = list(payloads)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"url": url, "headers": headers})
        return _FakeResponse(self.payloads.pop(0))


def _patch_client(monkeypatch, payload, calls):
    monkeypatch.setattr(
        account_usage.httpx, "Client", lambda timeout: _FakeClient(calls, [payload])
    )


# ---------------------------------------------------------------------------
# OpenCode Go
# ---------------------------------------------------------------------------


def _ocg_payload():
    return {
        "usage": {
            "rolling": {"status": "ok", "percent": 12, "resetsAt": "2026-08-22T18:00:00Z"},
            "weekly": {"status": "rate-limited", "percent": 100,
                       "resetsAt": "2026-08-24T00:00:00Z"},
            "monthly": {"status": "ok", "percent": 45, "resetsAt": "2026-09-01T00:00:00Z"},
        }
    }


def test_opencode_go_maps_three_windows(monkeypatch):
    calls = []
    _patch_client(monkeypatch, _ocg_payload(), calls)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "ocg-test-key")

    snap = account_usage._fetch_opencode_go_account_usage()

    assert snap is not None and snap.available
    assert snap.provider == "opencode-go"
    labels = [(w.label, w.used_percent) for w in snap.windows]
    assert labels == [("Rolling", 12.0), ("Weekly", 100.0), ("Monthly", 45.0)]
    # resetsAt parsed to aware UTC datetimes
    assert all(w.reset_at is not None for w in snap.windows)
    assert snap.windows[0].reset_at.tzinfo is timezone.utc
    # Cloudflare: product UA, not python's default
    assert calls[0]["headers"]["User-Agent"] == "opencode/1.0"
    assert calls[0]["url"] == "https://opencode.ai/zen/go/v1/usage"
    assert calls[0]["headers"]["Authorization"] == "Bearer ocg-test-key"


def test_opencode_go_explicit_key_beats_env(monkeypatch):
    calls = []
    _patch_client(monkeypatch, _ocg_payload(), calls)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "env-key")

    account_usage._fetch_opencode_go_account_usage(api_key="explicit-key")

    assert calls[0]["headers"]["Authorization"] == "Bearer explicit-key"


def test_opencode_go_unconfigured_returns_none(monkeypatch):
    monkeypatch.delenv("OPENCODE_GO_API_KEY", raising=False)

    assert account_usage._fetch_opencode_go_account_usage() is None


def test_opencode_go_skips_missing_percent_fields(monkeypatch):
    payload = {"usage": {"rolling": {"status": "ok", "percent": 7}}}
    calls = []
    _patch_client(monkeypatch, payload, calls)
    monkeypatch.setenv("OPENCODE_GO_API_KEY", "k")

    snap = account_usage._fetch_opencode_go_account_usage()

    assert [(w.label, w.used_percent) for w in snap.windows] == [("Rolling", 7.0)]


# ---------------------------------------------------------------------------
# Anthropic second subscription
# ---------------------------------------------------------------------------


def _anthropic_payload():
    return {
        "five_hour": {"utilization": 0.62, "resets_at": "2026-08-22T20:00:00Z"},
        "seven_day": {"utilization": 31.0},
    }


def test_anthropic2_reads_env_token_and_shares_window_mapping(monkeypatch):
    calls = []
    _patch_client(monkeypatch, _anthropic_payload(), calls)
    monkeypatch.setenv("ANTHROPIC2_OAUTH_TOKEN", "sk-ant-oat01-anthropic2-token")

    snap = account_usage._fetch_anthropic2_account_usage()

    assert snap is not None and snap.available
    assert snap.provider == "anthropic2"
    assert [(w.label, round(w.used_percent)) for w in snap.windows] == [
        ("Current session", 62), ("Current week", 31),
    ]
    auth = calls[0]["headers"]["Authorization"]
    assert auth == "Bearer sk-ant-oat01-anthropic2-token"


def test_anthropic2_unconfigured_returns_none(monkeypatch):
    monkeypatch.delenv("ANTHROPIC2_OAUTH_TOKEN", raising=False)

    assert account_usage._fetch_anthropic2_account_usage() is None


def test_anthropic2_non_oauth_token_reports_unavailable(monkeypatch):
    # An sk-ant-api key must NOT be sent to the oauth usage endpoint.
    monkeypatch.setenv("ANTHROPIC2_OAUTH_TOKEN", "sk-ant-api03-notoauth")

    snap = account_usage._fetch_anthropic2_account_usage()

    assert snap is not None and not snap.available
    assert snap.unavailable_reason


def test_primary_anthropic_fetcher_still_works_after_refactor(monkeypatch):
    """The refactor into _fetch_anthropic_usage_with_token must not regress it."""
    calls = []
    _patch_client(monkeypatch, _anthropic_payload(), calls)
    monkeypatch.setattr(
        account_usage, "resolve_anthropic_token", lambda: "sk-ant-oat01-primary"
    )

    snap = account_usage._fetch_anthropic_account_usage()

    assert snap is not None and snap.available
    assert snap.provider == "anthropic"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-ant-oat01-primary"


# ---------------------------------------------------------------------------
# Grok via CDP page-context fetch
# ---------------------------------------------------------------------------


class _FakeGrokSession:
    """Stands in for agent.grok_session.fetch_grok_rate_limits."""

    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.result


def test_grok_maps_remaining_over_total_to_used_pct(monkeypatch):
    fake = _FakeGrokSession((25.0, 100.0, datetime(2026, 8, 22, 21, 0, tzinfo=timezone.utc)))
    import agent.grok_session as grok_session
    monkeypatch.setattr(grok_session, "fetch_grok_rate_limits", fake)

    snap = account_usage._fetch_grok_account_usage()

    assert fake.kwargs["timeout"] == account_usage._DEFAULT_USAGE_TIMEOUT
    assert snap.provider == "xai"
    assert snap.source == "web_scrape"
    window = snap.windows[0]
    assert window.label == "Grok window"
    assert window.used_percent == pytest.approx(75.0)
    assert window.reset_at is not None


def test_grok_no_data_returns_none(monkeypatch):
    import agent.grok_session as grok_session
    monkeypatch.setattr(grok_session, "fetch_grok_rate_limits", lambda **kw: None)

    assert account_usage._fetch_grok_account_usage() is None


def test_grok_zero_total_returns_none(monkeypatch):
    import agent.grok_session as grok_session
    monkeypatch.setattr(
        grok_session, "fetch_grok_rate_limits", lambda **kw: (0.0, 0.0, None)
    )

    assert account_usage._fetch_grok_account_usage() is None
