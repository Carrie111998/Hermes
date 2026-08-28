"""Tests for the cross-provider subscription-usage fan-out."""

import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import patch

import pytest

from agent import provider_usage
from agent.provider_usage_types import (
    STATE_NETWORK_ERROR,
    STATE_NO_USAGE_ENDPOINT,
    STATE_NOT_AUTHENTICATED,
    STATE_OK,
    UNIT_COUNT,
    UNIT_CURRENCY,
    UNIT_PERCENT,
    ProviderUsage,
    UsageWindow,
    to_decimal,
)


@pytest.fixture(autouse=True)
def _clear_detect_memo():
    """Detection is memoized per process — a stale memo would leak across tests."""
    provider_usage._reset_detect_memo()
    yield
    provider_usage._reset_detect_memo()


# ── The unit model ─────────────────────────────────────────────────────────


def test_percent_windows_report_their_own_number():
    window = UsageWindow(label="5h", unit=UNIT_PERCENT, used=Decimal("28"))

    assert window.used_percent == 28.0


def test_a_count_window_derives_its_percentage_from_limit_and_remaining():
    window = UsageWindow(label="chat", unit=UNIT_COUNT, limit=Decimal(200), remaining=Decimal(150))

    assert window.used_percent == 25.0


def test_remaining_without_a_limit_has_no_percentage():
    # Inventing one would paint a bar that means nothing.
    window = UsageWindow(label="credits", unit=UNIT_CURRENCY, remaining=Decimal("12.5"))

    assert window.used_percent is None


def test_money_survives_the_round_trip_as_a_decimal_string():
    window = UsageWindow(
        label="credits", unit=UNIT_CURRENCY, remaining=Decimal("12.34"), currency="USD"
    )

    assert window.to_payload()["remaining"] == "12.34"


@pytest.mark.parametrize(
    "raw,expected",
    [("100", Decimal(100)), (12.5, Decimal("12.5")), ("", None), (None, None), (True, None), ("x", None)],
)
def test_provider_numbers_are_coerced_without_guessing(raw, expected):
    assert to_decimal(raw) == expected


# ── Detection ──────────────────────────────────────────────────────────────


def test_detection_reads_the_persisted_pool_without_seeding():
    with (
        patch.object(provider_usage, "_credential_pool", return_value={"kimi-coding": [{"id": "a"}]}),
        patch.object(provider_usage, "_registry", return_value={}),
        patch.object(provider_usage, "_has_env_credential", return_value=False),
        patch.object(provider_usage, "_has_credential_file", return_value=False),
    ):
        assert provider_usage.detect_providers(use_memo=False) == ["kimi-coding"]


def test_a_pruned_pool_key_is_not_evidence_of_a_credential():
    # Real state on a live machine: the key survives with an empty list after
    # its entries are pruned. Counting the key alone reports a logged-out
    # provider as authenticated.
    with (
        patch.object(provider_usage, "_credential_pool", return_value={"anthropic": []}),
        patch.object(provider_usage, "_registry", return_value={}),
        patch.object(provider_usage, "_has_env_credential", return_value=False),
        patch.object(provider_usage, "_has_credential_file", return_value=False),
    ):
        assert provider_usage.detect_providers(use_memo=False) == []


def test_openrouter_is_a_candidate_even_though_it_is_not_in_the_registry():
    # It is hardcoded into the pool seeder ahead of the registry lookup, so a
    # registry-only sweep drops it silently.
    with (
        patch.object(provider_usage, "_credential_pool", return_value={}),
        patch.object(provider_usage, "_registry", return_value={}),
    ):
        assert "openrouter" in provider_usage.candidate_providers()


def test_detection_never_touches_the_credential_pool():
    # load_pool() is not side-effect free — the Copilot branch exchanges a raw
    # gh token and writes state. Detection must stay a read.
    import agent.credential_pool as pool_mod

    with patch.object(pool_mod, "load_pool", side_effect=AssertionError("load_pool called")):
        provider_usage.detect_providers(use_memo=False)


def test_detection_covers_every_provider_the_live_pool_can_serve():
    """Parity guard against the bug class this module exists to avoid.

    Detection enumerates one namespace (auth.json / env / files) and the
    fetcher looks up another (the credential pool). If detection is ever the
    smaller set, a provider silently disappears from the panel with no error.
    A superset is safe: an over-detected provider just reports a typed state.
    """
    from agent.credential_pool import load_pool

    detected = set(provider_usage.detect_providers(use_memo=False))
    live = set()
    for provider in provider_usage.candidate_providers():
        try:
            if load_pool(provider).entries():
                live.add(provider)
        except Exception:
            continue

    assert live <= detected, f"pool has credentials detection missed: {sorted(live - detected)}"


# ── Fetch + isolation ──────────────────────────────────────────────────────


class _Profile:
    display_name = "Demo"
    usage_ttl = 60

    def __init__(self, result=None, error=None):
        self._result = result
        self._error = error

    def fetch_usage(self, *, credential=None, base_url=None, timeout=8.0):
        if self._error:
            raise self._error
        return self._result


def _usage(provider="demo", windows=(UsageWindow(label="5h", unit=UNIT_PERCENT, used=Decimal(10)),)):
    return ProviderUsage(provider=provider, display_name="Demo", windows=tuple(windows))


def test_one_failing_provider_never_hides_the_others():
    profiles = {"good": _Profile(_usage("good")), "bad": _Profile(error=RuntimeError("boom"))}

    with (
        patch.object(provider_usage, "_profile", side_effect=lambda name: profiles[name]),
        patch.object(provider_usage, "_read_cache", return_value={}),
        patch.object(provider_usage, "_store"),
    ):
        results = {u.provider: u for u in provider_usage.collect_usage(providers=["good", "bad"])}

    assert results["good"].state == STATE_OK
    assert results["bad"].state == STATE_NETWORK_ERROR
    assert results["bad"].message


def test_a_provider_with_no_credential_and_no_answer_reads_as_logged_out():
    with (
        patch.object(provider_usage, "_profile", return_value=_Profile(None)),
        patch.object(provider_usage, "_read_cache", return_value={}),
        patch.object(provider_usage, "_store"),
    ):
        (result,) = provider_usage.collect_usage(providers=["demo"])

    assert result.state == STATE_NOT_AUTHENTICATED


def test_a_credentialled_provider_with_no_answer_reads_as_no_endpoint():
    class _Cred:
        access_token = "t"
        base_url = None

    class _Pool:
        def peek(self):
            return _Cred()

    with (
        patch.object(provider_usage, "_profile", return_value=_Profile(None)),
        patch.object(provider_usage, "_read_cache", return_value={}),
        patch.object(provider_usage, "_store"),
        patch("agent.credential_pool.load_pool", return_value=_Pool()),
    ):
        (result,) = provider_usage.collect_usage(providers=["demo"])

    assert result.state == STATE_NO_USAGE_ENDPOINT


# ── Cache / TTL / stale-while-revalidate ───────────────────────────────────


def _cache_with(provider, age_seconds, state=STATE_OK):
    usage = ProviderUsage(
        provider=provider,
        display_name="Demo",
        windows=(UsageWindow(label="5h", unit=UNIT_PERCENT, used=Decimal(10)),),
        state=state,
        fetched_at=datetime.now(timezone.utc),
    )
    return {provider: {"stored_at": time.time() - age_seconds, "usage": usage.to_payload()}}


def test_a_fresh_cache_entry_is_served_without_a_fetch():
    profile = _Profile(_usage())

    with (
        patch.object(provider_usage, "_profile", return_value=profile),
        patch.object(provider_usage, "_read_cache", return_value=_cache_with("demo", 5)),
        patch.object(provider_usage, "_fetch_many") as fetch,
    ):
        (result,) = provider_usage.collect_usage(providers=["demo"])

    fetch.assert_not_called()
    assert result.stale is False
    assert result.windows[0].used_percent == 10.0


def test_an_expired_entry_is_refetched_and_the_fresh_answer_wins():
    with (
        patch.object(provider_usage, "_profile", return_value=_Profile(_usage("demo"))),
        patch.object(provider_usage, "_read_cache", return_value=_cache_with("demo", 9_999)),
        patch.object(provider_usage, "_store"),
    ):
        (result,) = provider_usage.collect_usage(providers=["demo"])

    assert result.stale is False
    assert result.state == STATE_OK


def test_refresh_skips_a_fresh_cache_entry():
    with (
        patch.object(provider_usage, "_profile", return_value=_Profile(_usage("demo"))),
        patch.object(provider_usage, "_read_cache", return_value=_cache_with("demo", 1)),
        patch.object(provider_usage, "_fetch_many", return_value={"demo": _usage("demo")}) as fetch,
        patch.object(provider_usage, "_store"),
    ):
        provider_usage.collect_usage(providers=["demo"], refresh=True)

    fetch.assert_called_once()


def test_a_failed_refresh_never_overwrites_good_cached_numbers(tmp_path):
    # Otherwise a transient blip blanks the panel and leaves no floor to fall
    # back to on the next call.
    cache_file = tmp_path / "provider_usage_cache.json"
    with patch.object(provider_usage, "_cache_path", return_value=cache_file):
        provider_usage._store({"demo": _usage("demo")})
        provider_usage._store(
            {"demo": ProviderUsage(provider="demo", state=STATE_NETWORK_ERROR)}
        )

    stored = json.loads(cache_file.read_text())

    assert stored["demo"]["usage"]["state"] == STATE_OK


def test_the_cache_file_is_written_atomically(tmp_path):
    cache_file = tmp_path / "nested" / "provider_usage_cache.json"
    with patch.object(provider_usage, "_cache_path", return_value=cache_file):
        provider_usage._store({"demo": _usage("demo")})

    assert json.loads(cache_file.read_text())["demo"]["usage"]["provider"] == "demo"
    # atomic_json_write names its scratch file ".{stem}_*.tmp", and glob skips
    # dotfiles — match the real prefix or the assertion can never fail.
    assert not list(cache_file.parent.glob(".provider_usage_cache_*"))


# ── Payload ────────────────────────────────────────────────────────────────


def test_the_rpc_payload_is_json_safe_and_fails_open():
    with patch.object(provider_usage, "collect_usage", side_effect=RuntimeError("nope")):
        payload = provider_usage.usage_payload()

    assert payload == {"ok": True, "available": False, "providers": []}


def test_the_rpc_payload_serialises_windows_for_the_wire():
    with patch.object(provider_usage, "collect_usage", return_value=[_usage("demo")]):
        payload = provider_usage.usage_payload()

    json.dumps(payload)  # must not raise
    assert payload["available"] is True
    assert payload["providers"][0]["windows"][0]["used_percent"] == 10.0


def test_a_failed_refresh_falls_back_to_the_numbers_it_was_holding():
    """The stale floor is the point — losing it on a blip blanks the panel.

    `_store` protects the cache file; this protects what the caller gets back.
    Without it, an expired entry plus one dropped connection returns
    `state=network_error, windows=()` and the panel goes empty even though
    perfectly good numbers were in hand a moment earlier.
    """
    cache = _cache_with("demo", 9_999)

    with (
        patch.object(provider_usage, "_profile", return_value=_Profile(_usage("demo"))),
        patch.object(provider_usage, "_read_cache", return_value=cache),
        patch.object(
            provider_usage,
            "_fetch_many",
            return_value={"demo": ProviderUsage(provider="demo", state=STATE_NETWORK_ERROR)},
        ),
        patch.object(provider_usage, "_store"),
    ):
        (result,) = provider_usage.collect_usage(providers=["demo"])

    assert result.state == STATE_OK
    assert result.stale is True
    assert result.windows[0].used_percent == 10.0


def test_a_successful_refresh_clears_the_stale_mark():
    cache = _cache_with("demo", 9_999)

    with (
        patch.object(provider_usage, "_profile", return_value=_Profile(_usage("demo"))),
        patch.object(provider_usage, "_read_cache", return_value=cache),
        patch.object(provider_usage, "_store"),
    ):
        (result,) = provider_usage.collect_usage(providers=["demo"])

    assert result.stale is False


def test_a_failure_with_no_floor_still_reports_the_failure():
    # Nothing cached to fall back to — the typed state must survive.
    with (
        patch.object(provider_usage, "_profile", return_value=_Profile(_usage("demo"))),
        patch.object(provider_usage, "_read_cache", return_value={}),
        patch.object(
            provider_usage,
            "_fetch_many",
            return_value={"demo": ProviderUsage(provider="demo", state=STATE_NETWORK_ERROR)},
        ),
        patch.object(provider_usage, "_store"),
    ):
        (result,) = provider_usage.collect_usage(providers=["demo"])

    assert result.state == STATE_NETWORK_ERROR
