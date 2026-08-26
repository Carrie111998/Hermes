"""Fetchers added 2026-08-22: opencode-go, anthropic2, grok (CDP scrape).

Mirrors the fake-client pattern of tests/agent/test_account_usage.py: httpx
is patched through the module attribute so no test touches the network.
"""

from datetime import datetime, timezone

import pytest

from agent import account_usage


@pytest.fixture(autouse=True)
def _no_real_credential_profiles(monkeypatch):
    """Make every test in this module blind to the developer's real profiles.

    ``_fetch_anthropic_account_usage`` prefers the PINNED ``~/.claude-anthropic1``
    profile and only falls back to ``resolve_anthropic_token()`` when it is
    absent (commit e3b034b5cf). Nothing here stubbed the anthropic1 reader, so
    on a box that HAS that profile the fetcher read the real credential file,
    ignored the test's patched token, and failed -- printing the live
    ``sk-ant-oat01`` access token into the pytest assertion diff. The suite
    passed or failed according to whose machine it ran on.

    Defaulting both readers to "no profile" fixes three things at once: the
    tests stop depending on the host, a real secret can no longer reach test
    output, and a test that WANTS a profile has to say so (``_install_profile``
    and the explicit monkeypatches below still win, since function-level
    patches are applied after this fixture).
    """
    monkeypatch.setattr(
        account_usage, "_read_anthropic1_credentials", lambda config_dir=None: None
    )
    monkeypatch.setattr(
        account_usage, "_read_anthropic2_credentials", lambda config_dir=None: None
    )


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
# Anthropic second subscription (isolated login profile)
# ---------------------------------------------------------------------------


def _anthropic_payload():
    return {
        "five_hour": {"utilization": 0.62, "resets_at": "2026-08-22T20:00:00Z"},
        "seven_day": {"utilization": 31.0},
    }


class _ProfileStub:
    """Stands in for the isolated profile's credential file."""

    def __init__(self, oauth):
        self.oauth = oauth


def _install_profile(monkeypatch, tmp_path, oauth):
    stub = _ProfileStub(oauth)
    monkeypatch.setattr(account_usage, "_read_anthropic2_credentials",
                       lambda config_dir=None: stub.oauth)
    return stub


def _future_expiry_ms():
    import time

    return int(time.time() * 1000) + 24 * 3600 * 1000


def test_anthropic2_reads_isolated_profile_and_shares_window_mapping(
    monkeypatch, tmp_path
):
    calls = []
    _patch_client(monkeypatch, _anthropic_payload(), calls)
    _install_profile(
        monkeypatch,
        tmp_path,
        {"accessToken": "sk-ant-oat01-anthropic2-token",
         "refreshToken": "rt-live", "expiresAt": _future_expiry_ms()},
    )

    snap = account_usage._fetch_anthropic2_account_usage()

    assert snap is not None and snap.available
    assert snap.provider == "anthropic2"
    assert [(w.label, round(w.used_percent)) for w in snap.windows] == [
        ("Current session", 62), ("Current week", 31),
    ]
    auth = calls[0]["headers"]["Authorization"]
    assert auth == "Bearer sk-ant-oat01-anthropic2-token"


def test_anthropic2_unconfigured_returns_none(monkeypatch, tmp_path):
    # No isolated-profile credentials file → unconfigured row, not an error.
    monkeypatch.setattr(account_usage, "_read_anthropic2_credentials",
                       lambda config_dir=None: None)

    assert account_usage._fetch_anthropic2_account_usage() is None


def test_anthropic2_non_oauth_token_reports_unavailable(monkeypatch, tmp_path):
    # An sk-ant-api key in the profile must NOT reach the oauth usage endpoint.
    _install_profile(
        monkeypatch,
        tmp_path,
        {"accessToken": "sk-ant-api03-notoauth",
         "refreshToken": "rt", "expiresAt": _future_expiry_ms()},
    )

    snap = account_usage._fetch_anthropic2_account_usage()

    assert snap is not None and not snap.available
    assert "auth login" in snap.unavailable_reason


def test_anthropic2_expired_token_refreshes_and_persists_pair(
    monkeypatch, tmp_path
):
    # Expired access token → refresh flow runs, rotated pair is written back
    # to the profile (single-use rotating refresh token), usage succeeds with
    # the NEW access token.
    import time

    payload = {
        "five_hour": {"utilization": 0.10},
        "seven_day": {"utilization": 5.0},
    }
    calls = []
    _patch_client(monkeypatch, payload, calls)

    stored = {}

    def fake_read(config_dir=None):
        return stored.get("creds")

    def fake_write(access, refresh, expires_ms, *, scopes=None):
        stored["creds"] = {"accessToken": access, "refreshToken": refresh,
                           "expiresAt": expires_ms,
                           **({"scopes": scopes} if scopes is not None else {})}

    old_exp = int(time.time() * 1000) - 1000  # already expired
    original = {"accessToken": "sk-ant-oat01-old", "refreshToken": "rt-old",
                "expiresAt": old_exp, "scopes": ["user:inference"]}
    stored["creds"] = dict(original)
    monkeypatch.setattr(account_usage, "_read_anthropic2_credentials", fake_read)
    monkeypatch.setattr(account_usage, "_write_anthropic2_credentials", fake_write)
    monkeypatch.setattr(
        account_usage, "refresh_anthropic_oauth_pure",
        lambda rt: (
            pytest.fail("must use the profile's own refresh token")
            if rt != "rt-old"
            else {"access_token": "sk-ant-oat01-new",
                  "refresh_token": "rt-new",
                  "expires_at_ms": _future_expiry_ms()}
        ),
    )

    snap = account_usage._fetch_anthropic2_account_usage()

    assert snap is not None and snap.available
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-ant-oat01-new"
    # Rotated pair persisted, original scopes preserved.
    assert stored["creds"]["accessToken"] == "sk-ant-oat01-new"
    assert stored["creds"]["refreshToken"] == "rt-new"
    assert stored["creds"]["scopes"] == ["user:inference"]


def test_anthropic2_refresh_failure_reports_unavailable(monkeypatch, tmp_path):
    import time

    old_exp = int(time.time() * 1000) - 1000
    _install_profile(
        monkeypatch,
        tmp_path,
        {"accessToken": "sk-ant-oat01-old", "refreshToken": "rt-old",
         "expiresAt": old_exp},
    )
    monkeypatch.setattr(
        account_usage, "refresh_anthropic_oauth_pure",
        lambda rt: (_ for _ in ()).throw(RuntimeError("invalid_grant")),
    )

    snap = account_usage._fetch_anthropic2_account_usage()

    assert snap is not None and not snap.available
    assert "auth login" in snap.unavailable_reason


def test_primary_anthropic_falls_back_to_the_ambient_token_when_unpinned(monkeypatch):
    """The UNPINNED path: no ~/.claude-anthropic1, so resolve_anthropic_token() serves.

    Renamed from test_primary_anthropic_fetcher_still_works_after_refactor, which
    predated the pinning in e3b034b5cf and no longer described the function: with a
    pinned profile present the fetcher never consults resolve_anthropic_token at all.
    The autouse fixture supplies the "unpinned" precondition this now names.
    """
    calls = []
    _patch_client(monkeypatch, _anthropic_payload(), calls)
    monkeypatch.setattr(
        account_usage, "resolve_anthropic_token", lambda: "sk-ant-oat01-primary"
    )

    snap = account_usage._fetch_anthropic_account_usage()

    assert snap is not None and snap.available
    assert snap.provider == "anthropic"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-ant-oat01-primary"


def test_primary_anthropic_prefers_the_pinned_profile_over_the_ambient_token(
    monkeypatch,
):
    """The DEFAULT path, and the whole reason pinning exists.

    resolve_anthropic_token() ultimately reads ~/.claude, which follows whichever
    account the desktop app is signed into. On 2026-08-23 14:18 the primary flipped
    from diegodearagao to diegodearagaous and this row silently became a duplicate of
    "Claude 2" for ~13 hours. If the ambient token is ever preferred again, this test
    fails: it makes the two sources disagree and pins which one wins.
    """
    calls = []
    _patch_client(monkeypatch, _anthropic_payload(), calls)
    monkeypatch.setattr(
        account_usage, "_read_anthropic1_credentials",
        lambda config_dir=None: {
            "accessToken": "sk-ant-oat01-pinned",
            "refreshToken": "rt-pinned",
            "expiresAt": _future_expiry_ms(),
        },
    )

    def _must_not_be_used():
        raise AssertionError(
            "ambient resolve_anthropic_token() consulted despite a pinned profile"
        )

    monkeypatch.setattr(
        account_usage, "resolve_anthropic_token", _must_not_be_used
    )

    snap = account_usage._fetch_anthropic_account_usage()

    assert snap is not None and snap.available
    assert snap.provider == "anthropic"
    assert calls[0]["headers"]["Authorization"] == "Bearer sk-ant-oat01-pinned"


def test_no_test_in_this_module_can_read_a_real_credential_profile():
    """Guards the autouse fixture itself.

    The defect being prevented is not a wrong assertion -- it is a live
    ``sk-ant-oat01`` access token reaching pytest's assertion diff because a
    fetcher fell through to the developer's home directory. If someone removes
    the fixture, this fails rather than silently re-arming that.
    """
    assert account_usage._read_anthropic1_credentials() is None
    assert account_usage._read_anthropic2_credentials() is None


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


# ---------------------------------------------------------------------------
# Gemini (CDP scrape via agent/gemini_session.py) -- added 2026-08-23


class _FakeGeminiSession:
    """Stands in for agent.gemini_session.fetch_gemini_budget_usage."""

    def __init__(self, result):
        self.result = result
        self.kwargs = None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return self.result


def test_gemini_maps_pct_and_budget_to_monthly_window(monkeypatch):
    fake = _FakeGeminiSession((14.9896, 250.0))
    import agent.gemini_session as gemini_session
    monkeypatch.setattr(gemini_session, "fetch_gemini_budget_usage", fake)

    snap = account_usage._fetch_gemini_account_usage()

    assert fake.kwargs["timeout"] == account_usage._DEFAULT_USAGE_TIMEOUT
    assert snap.provider == "gemini"
    assert snap.source == "web_scrape"
    window = snap.windows[0]
    assert window.label == "Monthly"
    assert window.used_percent == pytest.approx(14.9896)
    assert "$250 budget" in (window.detail or "")


def test_gemini_no_data_returns_none(monkeypatch):
    import agent.gemini_session as gemini_session
    monkeypatch.setattr(
        gemini_session, "fetch_gemini_budget_usage", lambda **kw: None
    )

    assert account_usage._fetch_gemini_account_usage() is None


def test_gemini_dispatched_from_fetch_account_usage(monkeypatch):
    import agent.gemini_session as gemini_session
    monkeypatch.setattr(
        gemini_session, "fetch_gemini_budget_usage", lambda **kw: (10.0, 100.0)
    )

    snap = account_usage.fetch_account_usage("gemini")
    assert snap is not None
    assert snap.provider == "gemini"
