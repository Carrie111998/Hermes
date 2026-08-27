"""Regression tests for hermes-agent #95159.

Telegram getUpdates wedge: shielded sticky-IP reconnect path can leave the
gateway silently deaf. The previous ``TelegramFallbackTransport`` only
rotated sticky IP on ``httpx.ConnectTimeout`` / ``httpx.ConnectError``
(see ``_is_retryable_connect_error``); connect attempts that hung
indefinitely or returned *hard* application-layer failures
(``ReadError`` / ``RemoteProtocolError`` after a successful TCP connect)
kept the same IP as sticky indefinitely. With only one IP working in the
seed list, the next
``getUpdates`` long-poll attempted the wedged IP, never returned, and PTB's
``network_retry_loop`` — which only re-issues ``getUpdates`` when the
previous call *returns* — never advanced.

These tests pin the recovery contract: when the sticky IP fails to make
forward progress within a bounded number of attempts, the transport must
demote it from sticky, walk the IPv4 ladder again, and reconnect. A
working IP anywhere in the seed list must be reachable within a small,
bounded backoff — recovery must not require a process restart.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest

import plugins.platforms.telegram.telegram_network as tnet


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _FakeTransport(httpx.AsyncBaseTransport):
    """Records calls and returns/raises based on a host→action mapping."""

    def __init__(self, calls, behavior):
        self.calls = calls
        self.behavior = behavior
        self.closed = False

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(
            {
                "url_host": request.url.host,
                "host_header": request.headers.get("host"),
                "sni_hostname": request.extensions.get("sni_hostname"),
                "path": request.url.path,
            }
        )
        action = self.behavior.get(request.url.host, "ok")
        if action == "timeout":
            raise httpx.ConnectTimeout("timed out")
        if action == "connect_error":
            raise httpx.ConnectError("connect error")
        if isinstance(action, Exception):
            raise action
        return httpx.Response(200, request=request, text="ok")

    async def aclose(self) -> None:
        self.closed = True


def _factory(calls, behavior, created=None):
    """Build an ``AsyncHTTPTransport`` factory whose instances record calls.

    Pass ``created`` (a list) to collect every instantiated transport so
    tests can assert pools were/weren't closed or replaced.
    """

    def f(**_kwargs):
        transport = _FakeTransport(calls, behavior)
        if created is not None:
            created.append(transport)
        return transport

    return f


def _telegram_request(path="/botTOKEN/getUpdates"):
    return httpx.Request("GET", f"https://api.telegram.org{path}")


# ---------------------------------------------------------------------------
# Regression: getUpdates-equivalent long-poll must recover after a wedge
# ---------------------------------------------------------------------------


class TestGetUpdatesLongPollRecovers:
    """Simulate the production wedge: getUpdates fails repeatedly on the
    sticky IP, then recovers once the IP heals.

    The original bug (#95159): a sticky IP that fails with a non-connect
    error left the transport pinned to that IP. The next call — even
    after the IP became healthy — would still be tried first and fail
    the same way, looping forever. With a transient edge blip, recovery
    required waiting for an explicit demotion event that never fired,
    so the gateway was stuck on a wedged IP for hours.

    Post-fix: per-IP wedged-counter (``_STICKY_FAILURE_THRESHOLD``)
    demotes sticky after consecutive *hard* application-layer failures
    (``ReadError`` / ``RemoteProtocolError``), so the next call walks the
    ladder from scratch.
    """

    @pytest.mark.asyncio
    async def test_sticky_demoted_after_consecutive_app_layer_failures(self, monkeypatch):
        calls = []
        # Initially both .220 and .221 are healthy.
        behavior = {
            "149.154.167.220": "ok",
            "149.154.167.221": "ok",
        }
        monkeypatch.setattr(
            tnet.httpx, "AsyncHTTPTransport", _factory(calls, behavior)
        )

        transport = tnet.TelegramFallbackTransport(
            ["149.154.167.220", "149.154.167.221"]
        )

        # Establish .220 as sticky. IPv4-first -> .220 succeeds, sticky=.220.
        resp = await transport.handle_async_request(_telegram_request())
        assert resp.status_code == 200
        assert transport._sticky_ip == "149.154.167.220"

        # Now wedge .220 at the application layer (ReadError after TCP
        # succeeds). Each subsequent call has sticky=.220 raised
        # ReadError. Pre-fix the transport would re-raise without
        # touching _sticky_ip, leaving it pinned to .220. Post-fix the
        # per-IP wedged counter demotes sticky after the threshold so
        # the next call walks the ladder from .221 first.
        behavior["149.154.167.220"] = httpx.ReadError("wedge")

        # Fire `threshold` requests against the wedge. Sticky is only
        # demoted *after* the threshold is crossed (we want to absorb a
        # single transient blip without re-walking the IP ladder).
        last_sticky = transport._sticky_ip
        for i in range(tnet._STICKY_FAILURE_THRESHOLD):
            try:
                await transport.handle_async_request(_telegram_request())
            except httpx.ReadError:
                pass

        # After the threshold has been crossed, sticky must NOT still
        # be the wedged IP. The next call must walk the ladder from the
        # remaining IPv4 candidates.
        assert transport._sticky_ip != "149.154.167.220", (
            f"Sticky IP still .220 after "
            f"{tnet._STICKY_FAILURE_THRESHOLD} consecutive wedge "
            "failures — the wedge is not being demoted (#95159)."
        )

    @pytest.mark.asyncio
    async def test_long_poll_recovers_when_sticky_heals(self, monkeypatch):
        """End-to-end: wedge clears, then the IP heals, recovery is automatic."""
        calls = []
        behavior = {
            "149.154.167.220": "ok",
            "149.154.167.221": "ok",
        }
        monkeypatch.setattr(
            tnet.httpx, "AsyncHTTPTransport", _factory(calls, behavior)
        )

        transport = tnet.TelegramFallbackTransport(
            ["149.154.167.220", "149.154.167.221"]
        )

        # Establish sticky=.220.
        resp = await transport.handle_async_request(_telegram_request())
        assert resp.status_code == 200
        assert transport._sticky_ip == "149.154.167.220"

        # Wedge .220. Fire threshold calls. Sticky demotes.
        behavior["149.154.167.220"] = httpx.ReadError("wedge")
        for _ in range(tnet._STICKY_FAILURE_THRESHOLD):
            try:
                await transport.handle_async_request(_telegram_request())
            except httpx.ReadError:
                pass

        # The sticky path must NOT still be .220.
        assert transport._sticky_ip != "149.154.167.220"

        # Heal .220.
        behavior["149.154.167.220"] = "ok"

        # Next request: recovery. .220 is healthy, so the transport must
        # reach .220 either directly or via the IP ladder. Either way,
        # sticky must end up on .220 again (the working IPv4).
        resp = await transport.handle_async_request(_telegram_request())
        assert resp.status_code == 200
        assert transport._sticky_ip == "149.154.167.220"


# ---------------------------------------------------------------------------
# Regression: connect-error path still rotates sticky immediately
# ---------------------------------------------------------------------------


class TestConnectErrorStillWorks:
    """Pre-fix behavior must be preserved: a single ConnectError on the
    sticky IP must still rotate sticky (existing contract)."""

    @pytest.mark.asyncio
    async def test_connect_error_clears_sticky_immediately(self, monkeypatch):
        calls = []
        behavior = {
            "149.154.167.220": "ok",
            "149.154.167.221": "ok",
        }
        monkeypatch.setattr(
            tnet.httpx, "AsyncHTTPTransport", _factory(calls, behavior)
        )

        transport = tnet.TelegramFallbackTransport(
            ["149.154.167.220", "149.154.167.221"]
        )

        # First call succeeds with .220 (IPv4-first).
        resp = await transport.handle_async_request(_telegram_request())
        assert resp.status_code == 200
        assert transport._sticky_ip == "149.154.167.220"

        # Make .220 raise ConnectError. Pre-existing behavior rotates
        # sticky on ConnectError immediately (no threshold needed).
        behavior["149.154.167.220"] = "connect_error"
        resp = await transport.handle_async_request(_telegram_request())
        assert resp.status_code == 200
        # The transport walked to .221 — sticky is now .221.
        assert transport._sticky_ip == "149.154.167.221"


# ---------------------------------------------------------------------------
# Regression: bounded recovery — a single working IP must be reachable fast
# ---------------------------------------------------------------------------


class TestBoundedRecovery:
    """#95159: recovery within a bounded number of attempts.

    With only one IP working in the seed list, recovery must walk the
    full ladder within one ``handle_async_request`` call. The transport
    must NOT require the caller to retry multiple times to escape a
    wedged sticky.
    """

    @pytest.mark.asyncio
    async def test_recovery_under_one_second(self, monkeypatch):
        calls = []
        # .220 is wedged forever; .221 always works.
        behavior = {
            "149.154.167.220": "connect_error",
            "149.154.167.221": "ok",
        }
        monkeypatch.setattr(
            tnet.httpx, "AsyncHTTPTransport", _factory(calls, behavior)
        )

        transport = tnet.TelegramFallbackTransport(
            ["149.154.167.220", "149.154.167.221"]
        )

        started = time.monotonic()
        resp = await transport.handle_async_request(_telegram_request())
        elapsed = time.monotonic() - started

        assert resp.status_code == 200
        assert elapsed < 1.0, (
            f"Transport took {elapsed:.2f}s to recover — too slow for "
            "the per-call recovery contract (#95159)."
        )
        assert transport._sticky_ip == "149.154.167.221"

    @pytest.mark.asyncio
    async def test_no_sticky_when_all_ips_fail(self, monkeypatch):
        """When every IPv4 fails, sticky must remain UNSET so the next
        request walks the full IPv4 ladder again rather than commit to
        one dead IP. (The dual-stack hostname may still be reachable.)"""
        calls = []
        behavior = {
            "149.154.167.220": "connect_error",
            "149.154.167.221": "connect_error",
        }
        # No behavior entry for "api.telegram.org" -> returns "ok" -> no raise.
        monkeypatch.setattr(
            tnet.httpx, "AsyncHTTPTransport", _factory(calls, behavior)
        )

        transport = tnet.TelegramFallbackTransport(
            ["149.154.167.220", "149.154.167.221"]
        )

        # Walk the ladder: .220 fail, .221 fail, hostname ok.
        resp = await transport.handle_async_request(_telegram_request())
        # Hostname is reached last, succeeds.
        assert resp.status_code == 200
        # _sticky_ip is the hostname (None), not a wedged IPv4.
        assert transport._sticky_ip is None

        # Now wedge the hostname too, all paths fail.
        behavior["api.telegram.org"] = "connect_error"
        with pytest.raises(httpx.ConnectError):
            await transport.handle_async_request(_telegram_request())

        # Sticky must be UNSET after a full ladder failure so the next
        # call walks the full ladder again.
        assert transport._sticky_ip is tnet._UNSET


# ---------------------------------------------------------------------------
# Direct contract pin: _record_sticky_failure helper
# ---------------------------------------------------------------------------


class TestRecordStickyFailureHelper:
    """Pin the per-IP wedged-counter helper.

    External health verifiers (and the adapter's polling progress
    watchdog) can rely on ``_record_sticky_failure``: given a *hard*
    application-layer exception it must increment a per-IP failure
    counter, demote the IP from sticky once the threshold is exceeded,
    and the counter is reset on a successful response.
    """

    @pytest.mark.asyncio
    async def test_threshold_demotes_sticky(self):
        transport = tnet.TelegramFallbackTransport(["149.154.167.220"])
        transport._sticky_ip = "149.154.167.220"

        # Under threshold: still sticky.
        for _ in range(tnet._STICKY_FAILURE_THRESHOLD - 1):
            await transport._record_sticky_failure(
                "149.154.167.220", httpx.ReadError("wedge")
            )
        assert transport._sticky_ip == "149.154.167.220"

        # Crossing the threshold: sticky is cleared.
        await transport._record_sticky_failure(
            "149.154.167.220", httpx.ReadError("wedge")
        )
        assert transport._sticky_ip is tnet._UNSET

    def test_success_clears_failure_counter(self):
        transport = tnet.TelegramFallbackTransport(["149.154.167.220"])
        transport._sticky_ip = "149.154.167.220"

        # Seed the counter via two failures.
        transport._sticky_failure_counts["149.154.167.220"] = 2

        transport._record_sticky_success("149.154.167.220")
        assert "149.154.167.220" not in transport._sticky_failure_counts

    def test_hostname_path_is_ignored(self):
        """``None`` (dual-stack hostname) is not subject to the counter —
        it has its own reset path on primary failure."""
        transport = tnet.TelegramFallbackTransport(["149.154.167.220"])
        # Should be a no-op; counter dict untouched.
        asyncio.run(
            transport._record_sticky_failure(None, httpx.ReadError("wedge"))
        )
        assert transport._sticky_failure_counts == {}


# ---------------------------------------------------------------------------
# Timeout-class failures must NOT demote sticky (#95234 review)
# ---------------------------------------------------------------------------


class TestTimeoutClassFailuresDoNotDemote:
    """A ``ReadTimeout`` on getUpdates is the *normal* long-poll outcome
    whenever the configured client read timeout is tighter than Telegram's
    poll window (and also what benign packet loss looks like), and
    ``PoolTimeout`` is *local* pool exhaustion on the shared connection
    pool, not a wedged remote IP. Counting either toward the hard-failure
    threshold demoted healthy IPs and forced full ladder walks with
    duplicated getUpdates traffic against every candidate.

    Contract: timeout-class exceptions are ignored entirely — no counter
    increment, no counter clear, no pool reset, and the sticky IP stays
    pinned. Only hard wedge signals (``ReadError`` /
    ``RemoteProtocolError``) drive demotion.
    """

    @pytest.mark.asyncio
    async def test_read_timeout_never_demotes_nor_counts(self, monkeypatch):
        calls = []
        created = []
        behavior = {
            "149.154.167.220": "ok",
            "149.154.167.221": "ok",
        }
        monkeypatch.setattr(
            tnet.httpx, "AsyncHTTPTransport", _factory(calls, behavior, created)
        )
        transport = tnet.TelegramFallbackTransport(
            ["149.154.167.220", "149.154.167.221"]
        )

        # Establish .220 as sticky.
        resp = await transport.handle_async_request(_telegram_request())
        assert resp.status_code == 200
        assert transport._sticky_ip == "149.154.167.220"

        # Client read timeout tighter than Telegram's poll window: every
        # long-poll "fails" with ReadTimeout even though the path is
        # healthy. Fire well past the hard-failure threshold.
        behavior["149.154.167.220"] = httpx.ReadTimeout("long-poll window exceeded")
        for _ in range(tnet._STICKY_FAILURE_THRESHOLD + 3):
            try:
                await transport.handle_async_request(_telegram_request())
            except httpx.ReadTimeout:
                pass

        assert transport._sticky_ip == "149.154.167.220", (
            "ReadTimeout demoted the sticky IP — timeout-class failures "
            "must not count toward wedge demotion"
        )
        # Neither counted nor cleared: the counter stays empty.
        assert transport._sticky_failure_counts.get("149.154.167.220", 0) == 0
        # The pool must NOT have been reset mid-burst.
        assert "149.154.167.220" in transport._fallbacks
        assert all(not t.closed for t in created)

    @pytest.mark.asyncio
    async def test_pool_timeout_neither_demotes_nor_resets_pool(self, monkeypatch):
        calls = []
        created = []
        behavior = {
            "149.154.167.220": "ok",
            "149.154.167.221": "ok",
        }
        monkeypatch.setattr(
            tnet.httpx, "AsyncHTTPTransport", _factory(calls, behavior, created)
        )
        transport = tnet.TelegramFallbackTransport(
            ["149.154.167.220", "149.154.167.221"]
        )

        resp = await transport.handle_async_request(_telegram_request())
        assert resp.status_code == 200
        assert transport._sticky_ip == "149.154.167.220"
        fallback_before = transport._fallbacks["149.154.167.220"]

        # Local pool exhaustion under load burst: must be log-only.
        behavior["149.154.167.220"] = httpx.PoolTimeout("pool exhausted")
        for _ in range(tnet._STICKY_FAILURE_THRESHOLD + 3):
            try:
                await transport.handle_async_request(_telegram_request())
            except httpx.PoolTimeout:
                pass

        assert transport._sticky_ip == "149.154.167.220", (
            "PoolTimeout demoted the sticky IP — local pool exhaustion "
            "must not count toward wedge demotion"
        )
        assert transport._sticky_failure_counts.get("149.154.167.220", 0) == 0
        # Pool must NOT have been discarded/reset: same instance retained.
        assert transport._fallbacks.get("149.154.167.220") is fallback_before
        assert all(not t.closed for t in created)

    @pytest.mark.asyncio
    async def test_remote_protocol_error_still_demotes_at_threshold(
        self, monkeypatch
    ):
        """Hard wedge signals keep their pre-review contract: threshold
        consecutive occurrences demote sticky AND reset the failed pool."""
        calls = []
        created = []
        behavior = {
            "149.154.167.220": "ok",
            "149.154.167.221": "ok",
        }
        monkeypatch.setattr(
            tnet.httpx, "AsyncHTTPTransport", _factory(calls, behavior, created)
        )
        transport = tnet.TelegramFallbackTransport(
            ["149.154.167.220", "149.154.167.221"]
        )

        resp = await transport.handle_async_request(_telegram_request())
        assert transport._sticky_ip == "149.154.167.220"

        behavior["149.154.167.220"] = httpx.RemoteProtocolError(
            "peer closed connection without sending complete message body"
        )
        for _ in range(tnet._STICKY_FAILURE_THRESHOLD):
            try:
                await transport.handle_async_request(_telegram_request())
            except httpx.RemoteProtocolError:
                pass

        assert transport._sticky_ip != "149.154.167.220", (
            "RemoteProtocolError did not demote sticky at threshold — "
            "hard wedge signals must still drive demotion"
        )
        # Hard failures still reset the poisoned pool (#63311 contract).
        assert "149.154.167.220" not in transport._fallbacks

    @pytest.mark.asyncio
    async def test_timeout_does_not_clear_hard_failure_evidence(self, monkeypatch):
        """Chosen semantics: timeouts neither count NOR clear the counter.

        A wedged IP whose ReadErrors interleave with benign ReadTimeouts
        must still reach the demotion threshold; otherwise a flapping
        wedge could avoid demotion indefinitely.
        """
        calls = []
        behavior = {
            "149.154.167.220": "ok",
            "149.154.167.221": "ok",
        }
        monkeypatch.setattr(
            tnet.httpx, "AsyncHTTPTransport", _factory(calls, behavior)
        )
        transport = tnet.TelegramFallbackTransport(
            ["149.154.167.220", "149.154.167.221"]
        )

        resp = await transport.handle_async_request(_telegram_request())
        assert transport._sticky_ip == "149.154.167.220"

        # ReadError -> ReadTimeout -> ReadError must demote: the timeout
        # in the middle leaves the accumulated hard-failure evidence intact.
        behavior["149.154.167.220"] = httpx.ReadError("wedge")
        with pytest.raises(httpx.ReadError):
            await transport.handle_async_request(_telegram_request())
        assert transport._sticky_ip == "149.154.167.220"  # below threshold

        behavior["149.154.167.220"] = httpx.ReadTimeout("long-poll window exceeded")
        with pytest.raises(httpx.ReadTimeout):
            await transport.handle_async_request(_telegram_request())
        assert transport._sticky_ip == "149.154.167.220"  # ignored entirely

        behavior["149.154.167.220"] = httpx.ReadError("wedge")
        with pytest.raises(httpx.ReadError):
            await transport.handle_async_request(_telegram_request())
        assert transport._sticky_ip != "149.154.167.220", (
            "Interleaved ReadTimeout cleared hard-failure evidence — "
            "timeouts must neither count nor clear the counter"
        )

    @pytest.mark.asyncio
    async def test_success_clears_counter_end_to_end(self, monkeypatch):
        """Existing behavior kept: a confirmed round-trip clears the
        counter, so one post-recovery blip does not immediately demote."""
        calls = []
        behavior = {
            "149.154.167.220": "ok",
            "149.154.167.221": "ok",
        }
        monkeypatch.setattr(
            tnet.httpx, "AsyncHTTPTransport", _factory(calls, behavior)
        )
        transport = tnet.TelegramFallbackTransport(
            ["149.154.167.220", "149.154.167.221"]
        )

        resp = await transport.handle_async_request(_telegram_request())
        assert transport._sticky_ip == "149.154.167.220"

        # One hard failure below threshold...
        behavior["149.154.167.220"] = httpx.ReadError("blip")
        with pytest.raises(httpx.ReadError):
            await transport.handle_async_request(_telegram_request())
        assert transport._sticky_ip == "149.154.167.220"

        # ...then success clears the counter...
        behavior["149.154.167.220"] = "ok"
        resp = await transport.handle_async_request(_telegram_request())
        assert resp.status_code == 200
        assert transport._sticky_failure_counts.get("149.154.167.220", 0) == 0

        # ...so the next single blip must NOT instantly demote.
        behavior["149.154.167.220"] = httpx.ReadError("blip")
        with pytest.raises(httpx.ReadError):
            await transport.handle_async_request(_telegram_request())
        assert transport._sticky_ip == "149.154.167.220", (
            "Counter was not cleared by the intervening success — a single "
            "post-recovery blip would falsely demote a healthy sticky IP"
        )