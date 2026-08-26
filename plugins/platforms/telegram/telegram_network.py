"""Telegram-specific network helpers.

Provides a hostname-preserving fallback transport for networks where
api.telegram.org resolves to an endpoint that is unreachable from the current
host. The transport keeps the logical request host and TLS SNI as
api.telegram.org while retrying the TCP connection against one or more fallback
IPv4 addresses.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from typing import Iterable, Optional

import httpx

logger = logging.getLogger(__name__)

_TELEGRAM_API_HOST = "api.telegram.org"

# DNS-over-HTTPS providers used to discover Telegram API IPs that may differ
# from the (potentially unreachable) IP returned by the local system resolver.
_DOH_TIMEOUT = 4.0  # seconds — bounded so connect() isn't noticeably delayed

# Number of consecutive *hard application-layer* failures (``ReadError`` /
# ``RemoteProtocolError``) recorded against a sticky IP before the transport
# demotes it and walks the full IP ladder again (#95159). Module-level so
# tests pin shared state instead of mutable per-instance attributes.
# Timeout-class exceptions (``httpx.TimeoutException`` subclasses, incl.
# ``ReadTimeout`` and ``PoolTimeout``) deliberately do NOT count toward this
# threshold — see ``TelegramFallbackTransport._record_sticky_failure``.
_STICKY_FAILURE_THRESHOLD = 2

_DOH_PROVIDERS: list[dict] = [
    {
        "url": "https://dns.google/resolve",
        "params": {"name": _TELEGRAM_API_HOST, "type": "A"},
        "headers": {},
    },
    {
        "url": "https://cloudflare-dns.com/dns-query",
        "params": {"name": _TELEGRAM_API_HOST, "type": "A"},
        "headers": {"Accept": "application/dns-json"},
    },
]

# Last-resort IPv4 Telegram Bot API endpoints in 149.154.160.0/20
# (same seed used by OpenClaw). Used when DoH is blocked AND as the
# first-try connect targets so a blackholed IPv6 AAAA for the hostname
# cannot pin initialize() (#87015).
SEED_FALLBACK_IPS: list[str] = ["149.154.166.110", "149.154.167.220"]
_UNSET = object()


def _resolve_proxy_url(target_hosts=None) -> str | None:
    # Delegate to shared implementation (env vars + macOS system proxy detection)
    from gateway.platforms.base import resolve_proxy_url
    return resolve_proxy_url("TELEGRAM_PROXY", target_hosts=target_hosts)


class TelegramFallbackTransport(httpx.AsyncBaseTransport):
    """Reach Telegram Bot API via known IPv4 literals first, hostname last.

    Requests still target https://api.telegram.org/... logically (Host + SNI
    stay on the hostname). TCP connects to a known A-record IP first so a
    blackholed IPv6 AAAA cannot pin initialize(). Equivalent to
    ``curl --resolve api.telegram.org:443:<ip>``. The dual-stack hostname
    is last resort for IPv6-only networks.
    """

    # Bound every pool. httpx defaults to 100 connections per pool, so a wedged
    # endpoint plus the seed IPs can outgrow the process file-descriptor limit
    # on its own (#63311).
    _POOL_LIMITS = httpx.Limits(max_connections=8, max_keepalive_connections=4)

    def __init__(self, fallback_ips: Iterable[str], **transport_kwargs):
        self._fallback_ips = list(dict.fromkeys(_normalize_fallback_ips(fallback_ips)))
        proxy_url = _resolve_proxy_url(target_hosts=[_TELEGRAM_API_HOST, *self._fallback_ips])
        if proxy_url and "proxy" not in transport_kwargs:
            transport_kwargs["proxy"] = proxy_url
        transport_kwargs.setdefault("limits", self._POOL_LIMITS)
        self._transport_kwargs = transport_kwargs
        self._primary = httpx.AsyncHTTPTransport(**transport_kwargs)
        self._primary_lock = asyncio.Lock()
        self._primary_closed = False
        # Built on demand and discarded on failure — see _reset_fallback.
        self._fallbacks: dict[str, httpx.AsyncHTTPTransport] = {}
        self._fallback_lock = asyncio.Lock()
        # ``_UNSET`` vs ``None`` vs ``str``: unset / sticky hostname / sticky IPv4.
        # ``None`` cannot mean both "no sticky yet" and "sticky dual-stack
        # hostname" (#87015).
        self._sticky_ip: object = _UNSET
        self._sticky_lock = asyncio.Lock()
        # Per-IP wedged-IP failure counter (#95159). The transport already
        # rotates sticky on a single ``httpx.ConnectTimeout`` / ``ConnectError``
        # (``_is_retryable_connect_error``), but a sticky IP can also become
        # unusable at the *application* layer — hard wedge signals:
        # ``ReadError``, ``RemoteProtocolError`` (peer accepted the TCP
        # connection, then broke it mid-request/response). Those exceptions are
        # re-raised immediately without ever touching ``_sticky_ip``, so the
        # next getUpdates long-poll hammers the same dead path again and
        # PTB's ``network_retry_loop`` (which only re-issues getUpdates when
        # the previous call *returns*) never advances. Track those failures
        # here and demote the sticky IP once ``_STICKY_FAILURE_THRESHOLD``
        # consecutive hard failures pile up against it; the next request then
        # walks the full IP ladder again. The counter is reset on a
        # successful response so a transient edge blip does not poison the
        # sticky path for the rest of the connection's lifetime.
        #
        # Taxonomy notes: HTTP 5xx never appears in this counter — httpx
        # returns a Response for error statuses instead of raising at the
        # transport layer. Timeout-class exceptions are excluded too: a
        # ``ReadTimeout`` on getUpdates is the *normal* long-poll outcome
        # whenever the client read timeout is tighter than Telegram's poll
        # window, and ``PoolTimeout`` is local pool exhaustion, not evidence
        # of a wedged IP (see ``_record_sticky_failure``).
        self._sticky_failure_counts: dict[str, int] = {}
        self._sticky_failure_lock = asyncio.Lock()

    async def _get_fallback(self, ip: str) -> httpx.AsyncHTTPTransport:
        async with self._fallback_lock:
            transport = self._fallbacks.get(ip)
            if transport is None:
                transport = httpx.AsyncHTTPTransport(**self._transport_kwargs)
                self._fallbacks[ip] = transport
            return transport

    async def _reset_primary(self, transport: httpx.AsyncHTTPTransport) -> None:
        # Retryable primary failures can leave half-closed sockets in the pool;
        # replace and close the failed generation before trying fallback.
        async with self._primary_lock:
            if self._primary_closed or transport is not self._primary:
                return
            self._primary = httpx.AsyncHTTPTransport(**self._transport_kwargs)
        try:
            await transport.aclose()
        except Exception as exc:
            logger.debug("[Telegram] Error closing primary transport: %s", exc)

    async def _reset_fallback(self, ip: str) -> None:
        """Discard a failed fallback pool so its dead sockets are released.

        A connect that reaches ESTABLISHED and is then closed by the peer leaves
        its socket in CLOSE_WAIT inside the pool. Retaining the poisoned pool
        leaks one descriptor per retry until the process hits its file limit and
        can no longer accept connections or resolve DNS (#63311).
        """
        async with self._fallback_lock:
            transport = self._fallbacks.pop(ip, None)
        if transport is None:
            return
        try:
            await transport.aclose()
        except Exception as exc:  # closing a broken pool must never mask the real error
            logger.debug("[Telegram] Error closing fallback transport %s: %s", ip, exc)

    def _attempt_order(self) -> list[Optional[str]]:
        """IPv4 literals first; dual-stack hostname last.

        A blackholed IPv6 path to ``api.telegram.org`` never errors — Happy
        Eyeballs waits on AAAA until the OS TCP timeout, which can pin the
        event loop so ``_await_with_thread_deadline`` never fires (#87015).
        Known A-record IPs connect over IPv4 immediately. The hostname is
        kept as a last resort for IPv6-only networks.
        """
        order: list[Optional[str]] = []
        if self._sticky_ip is not _UNSET:
            sticky = self._sticky_ip
            order.append(sticky if sticky is None else str(sticky))
        for ip in self._fallback_ips:
            if ip not in order:
                order.append(ip)
        if None not in order:
            order.append(None)
        return order

    async def _record_sticky_failure(self, ip: str | None, exc: Exception) -> None:
        """Record a wedged-IP failure and demote sticky once the threshold is crossed.

        Only *hard* application-layer wedge signals count toward demotion:
        ``httpx.ReadError`` and ``httpx.RemoteProtocolError`` (the peer
        accepted the TCP connection but then broke it mid-request/response).
        Connect-level failures never reach this method —
        ``_is_retryable_connect_error`` already rotates sticky on a single
        ``httpx.ConnectTimeout`` / ``ConnectError`` — and a sticky IP can
        therefore stay pinned to a wedged application-layer path forever
        (#95159). Hard failures are tracked per IP and sticky is demoted
        once the counter crosses ``_STICKY_FAILURE_THRESHOLD`` so the next
        ``handle_async_request`` call walks the full ladder again. PTB's
        ``network_retry_loop`` then re-issues ``getUpdates`` against the
        fresh attempt and the gateway exits its silent outage without a
        process restart.

        Deliberately NOT counted (neither incremented nor cleared):

        * Every ``httpx.TimeoutException`` subclass. A ``ReadTimeout``/
          ``WriteTimeout`` on getUpdates is the *normal* long-poll outcome
          whenever the configured client read timeout is tighter than
          Telegram's poll window (and also what benign packet loss looks
          like), so counting it would demote healthy IPs and force a full
          ladder walk plus duplicated getUpdates traffic against every
          candidate. They are ignored entirely rather than clearing the
          counter so that interleaved hard failures still accumulate toward
          the threshold.
        * ``httpx.PoolTimeout`` in particular: it signals *local* pool
          exhaustion on the shared connection pool (too many concurrent
          requests), not a wedged remote IP. Demoting here punishes healthy
          IPs exactly at peak load and resetting their pool mid-burst would
          amplify the exhaustion — so it is logged and nothing else.
        """
        if ip is None:
            # Dual-stack hostname path uses its own reset on primary failure.
            return
        if isinstance(exc, httpx.TimeoutException):
            # Timeout-class failure: not a wedge signal. Neither counts nor
            # clears the per-IP counter; the sticky path stays untouched.
            if isinstance(exc, httpx.PoolTimeout):
                logger.warning(
                    "[Telegram] Local connection-pool exhaustion (%s) on %s "
                    "— local condition, not a wedged IP; sticky and counter "
                    "untouched",
                    type(exc).__name__,
                    ip,
                )
            else:
                logger.debug(
                    "[Telegram] Sticky path %s timed out (%s) — normal "
                    "long-poll outcome, not a wedge signal; counter untouched",
                    ip,
                    type(exc).__name__,
                )
            return
        async with self._sticky_failure_lock:
            count = self._sticky_failure_counts.get(ip, 0) + 1
            self._sticky_failure_counts[ip] = count
            if count < _STICKY_FAILURE_THRESHOLD:
                logger.debug(
                    "[Telegram] Sticky path %s wedged-IP failure %d/%d — still sticky",
                    ip,
                    count,
                    _STICKY_FAILURE_THRESHOLD,
                )
                return
            # Threshold crossed — drop sticky so the next request walks
            # the full ladder. Do NOT reset the counter here: the next
            # attempt must reach a working IP, call _record_sticky_success,
            # and clear the counter only after a confirmed round-trip. This
            # avoids a re-attempt against the same wedged IP in the same
            # burst.
            async with self._sticky_lock:
                if self._sticky_ip == ip:
                    self._sticky_ip = _UNSET
                    logger.warning(
                        "[Telegram] Sticky path %s wedged after %d "
                        "consecutive hard application-layer failures; "
                        "demoting to walk the full IP ladder (#95159)",
                        ip,
                        count,
                    )
            # Reset the failed pool so its dead sockets are released, just
            # like the connect-failure branch does. Without this the same
            # poisoned httpx pool is reused on the next attempt and the
            # wedge persists even after sticky is cleared.
            await self._reset_fallback(ip)

    def _record_sticky_success(self, ip: str) -> None:
        """Clear the per-IP wedged-counter after a successful round-trip.

        A successful response means the IP is healthy again. Clear the
        counter so future transient blips don't accumulate and falsely
        demote a recovered IP.
        """
        if ip is None:
            return
        # Synchronous map pop is safe: the lock only guards mutation from
        # _record_sticky_failure on the same transport instance and a
        # benign race (an extra failure counted) is bounded by the
        # threshold reset below.
        self._sticky_failure_counts.pop(ip, None)

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != _TELEGRAM_API_HOST or not self._fallback_ips:
            return await self._primary.handle_async_request(request)

        attempt_order = self._attempt_order()

        last_error: Exception | None = None
        for ip in attempt_order:
            candidate = request if ip is None else _rewrite_request_for_ip(request, ip)
            transport = self._primary if ip is None else await self._get_fallback(ip)
            try:
                response = await transport.handle_async_request(candidate)
                # Confirmed round-trip — clear the wedged-IP counter for
                # this IP so a one-off edge blip does not poison the
                # sticky path for the rest of the connection's lifetime
                # (#95159).
                self._record_sticky_success(ip if ip is not None else None)
                if self._sticky_ip is _UNSET or self._sticky_ip != ip:
                    async with self._sticky_lock:
                        if self._sticky_ip is _UNSET or self._sticky_ip != ip:
                            self._sticky_ip = ip
                            if ip is not None:
                                log = logger.warning if last_error is not None else logger.info
                                log(
                                    "[Telegram] Using sticky IPv4 Telegram API path %s "
                                    "(dual-stack hostname tried last — #87015)",
                                    ip,
                                )
                return response
            except Exception as exc:
                last_error = exc
                if not _is_retryable_connect_error(exc):
                    # Non-connect failure on a sticky IP can leave the
                    # gateway silently dead (#95159): the exception
                    # bubbles up without ever touching ``_sticky_ip``, so
                    # the next ``getUpdates`` long-poll hits the same
                    # dead path again. Record it so the transport demotes
                    # the IP once enough consecutive *hard* wedge signals
                    # pile up — timeout-class exceptions (``ReadTimeout``,
                    # ``PoolTimeout``, ...) are ignored inside, since they
                    # are normal long-poll outcomes or local pool
                    # exhaustion rather than wedge evidence. Connect-level
                    # failures already rotate sticky below; do not
                    # double-count them.
                    await self._record_sticky_failure(
                        ip if ip is not None else None, exc
                    )
                    raise
                if self._sticky_ip is not _UNSET and ip == self._sticky_ip:
                    async with self._sticky_lock:
                        if self._sticky_ip is not _UNSET and self._sticky_ip == ip:
                            self._sticky_ip = _UNSET
                            logger.warning(
                                "[Telegram] Sticky Telegram path %s failed; "
                                "re-walking IPv4 literals before the hostname",
                                ip if ip is not None else "api.telegram.org",
                            )
                if ip is None:
                    await self._reset_primary(transport)
                    logger.warning(
                        "[Telegram] Dual-stack api.telegram.org path failed (%s)",
                        exc,
                    )
                    continue
                logger.warning("[Telegram] IPv4 Telegram API IP %s failed: %s", ip, exc)
                await self._reset_fallback(ip)
                continue

        if last_error is None:
            raise RuntimeError("All Telegram fallback IPs exhausted but no error was recorded")
        raise last_error

    async def aclose(self) -> None:
        async with self._primary_lock:
            self._primary_closed = True
            primary = self._primary
        await primary.aclose()
        async with self._fallback_lock:
            transports = list(self._fallbacks.values())
            self._fallbacks.clear()
        for transport in transports:
            await transport.aclose()


def _normalize_fallback_ips(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        raw = str(value).strip()
        if not raw:
            continue
        try:
            addr = ipaddress.ip_address(raw)
        except ValueError:
            logger.warning("Ignoring invalid Telegram fallback IP: %r", raw)
            continue
        if addr.version != 4:
            logger.warning("Ignoring non-IPv4 Telegram fallback IP: %s", raw)
            continue
        if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified:
            logger.warning("Ignoring private/internal Telegram fallback IP: %s", raw)
            continue
        normalized.append(str(addr))
    return normalized


def parse_fallback_ip_env(value: str | None) -> list[str]:
    if not value:
        return []
    parts = [part.strip() for part in value.split(",")]
    return _normalize_fallback_ips(parts)


def _resolve_system_dns() -> set[str]:
    """Return the IPv4 addresses that the OS resolver gives for api.telegram.org."""
    try:
        results = socket.getaddrinfo(_TELEGRAM_API_HOST, 443, socket.AF_INET)
        return {addr[4][0] for addr in results}
    except Exception:
        return set()


async def _query_doh_provider(
    client: httpx.AsyncClient, provider: dict
) -> list[str]:
    """Query one DoH provider and return A-record IPs."""
    try:
        resp = await client.get(
            provider["url"], params=provider["params"], headers=provider["headers"]
        )
        resp.raise_for_status()
        data = resp.json()
        ips: list[str] = []
        for answer in data.get("Answer", []):
            if answer.get("type") != 1:  # A record
                continue
            raw = answer.get("data", "").strip()
            try:
                ipaddress.ip_address(raw)
                ips.append(raw)
            except ValueError:
                continue
        return ips
    except Exception as exc:
        logger.debug("DoH query to %s failed: %s", provider["url"], exc)
        return []


async def discover_fallback_ips() -> list[str]:
    """Auto-discover Telegram API IPs via DNS-over-HTTPS.

    Resolves api.telegram.org through Google and Cloudflare DoH and returns all
    unique A records.  IPs that match the local system resolver are kept rather
    than excluded: in many networks the system-DNS IP is the most reliable path
    to api.telegram.org and a transient primary-path failure should be retried
    against the same address via the IP-rewrite path before the seed list is
    consulted (#14520).  Falls back to a hardcoded seed list only when DoH
    yields no usable answers.
    """
    async with httpx.AsyncClient(timeout=httpx.Timeout(_DOH_TIMEOUT)) as client:
        doh_tasks = [_query_doh_provider(client, p) for p in _DOH_PROVIDERS]
        system_dns_task = asyncio.ensure_future(asyncio.to_thread(_resolve_system_dns))
        results = await asyncio.gather(*doh_tasks, return_exceptions=True)

    # The system-resolver leg runs socket.getaddrinfo in a worker thread with
    # no timeout of its own — a wedged OS resolver (broken VPN/DNS) can sit for
    # minutes. Its result only feeds the no-usable-answers log line below, so
    # it must never gate discovery: bound it and move on (#63309). The DoH legs
    # are already bounded by the client timeout above.
    system_ips: set[str] = set()
    try:
        system_result = await asyncio.wait_for(system_dns_task, timeout=_DOH_TIMEOUT)
        if isinstance(system_result, set):
            system_ips = system_result
    except Exception:
        logger.debug("System-DNS resolution for %s did not complete in time", _TELEGRAM_API_HOST)

    doh_ips: list[str] = []
    for r in results:
        if isinstance(r, list):
            doh_ips.extend(r)

    # Deduplicate preserving order
    seen: set[str] = set()
    candidates: list[str] = []
    for ip in doh_ips:
        if ip not in seen:
            seen.add(ip)
            candidates.append(ip)

    # Validate through existing normalization
    validated = _normalize_fallback_ips(candidates)

    if validated:
        logger.debug("Discovered Telegram fallback IPs via DoH: %s", ", ".join(validated))
        return validated

    logger.info(
        "DoH discovery yielded no usable IPs (system DNS: %s); using seed fallback IPs %s",
        ", ".join(system_ips) or "unknown",
        ", ".join(SEED_FALLBACK_IPS),
    )
    return list(SEED_FALLBACK_IPS)


def _rewrite_request_for_ip(request: httpx.Request, ip: str) -> httpx.Request:
    original_host = request.url.host or _TELEGRAM_API_HOST
    url = request.url.copy_with(host=ip)
    headers = request.headers.copy()
    headers["host"] = original_host
    extensions = dict(request.extensions)
    extensions["sni_hostname"] = original_host
    return httpx.Request(
        method=request.method,
        url=url,
        headers=headers,
        stream=request.stream,
        extensions=extensions,
    )


def _is_retryable_connect_error(exc: Exception) -> bool:
    return isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError))
