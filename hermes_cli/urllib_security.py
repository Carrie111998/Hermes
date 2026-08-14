"""Security policy for credential-bearing stdlib urllib requests."""

from __future__ import annotations

import copy
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from typing import Any

# Headers safe to forward to a different origin. Everything else is dropped:
# custom provider headers routinely carry credentials under arbitrary names.
_CROSS_ORIGIN_SAFE_HEADERS = frozenset({"accept", "user-agent"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_URL_RESPONSE_READ_CHUNK_BYTES = 64 * 1024


class URLResponseBodyTooLarge(ValueError):
    """Raised when a urllib response exceeds its caller-provided byte cap."""


def _response_content_length(response: Any) -> int | None:
    """Return a valid non-negative Content-Length, when one is available."""
    headers = getattr(response, "headers", None)
    if headers is None:
        info = getattr(response, "info", None)
        if callable(info):
            try:
                headers = info()
            except Exception:  # noqa: BLE001 - an invalid hint is ignored
                headers = None
    if headers is None:
        return None
    try:
        raw_length = headers.get("Content-Length")
    except Exception:  # noqa: BLE001 - an invalid hint is ignored
        return None
    if raw_length is None:
        return None
    try:
        length = int(raw_length)
    except (TypeError, ValueError):
        return None
    return length if length >= 0 else None


def _read_url_response_bytes_limited(response: Any, *, max_bytes: int) -> bytes:
    """Read a urllib-style response while retaining at most ``max_bytes``.

    Content-Length is an early rejection hint only. The running limit remains
    authoritative for missing, invalid, or dishonest headers. Reads are kept
    small so the retained buffer and each response allocation are bounded.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")

    content_length = _response_content_length(response)
    if content_length is not None and content_length > max_bytes:
        raise URLResponseBodyTooLarge(
            f"response exceeds {max_bytes} bytes"
        )

    body = bytearray()
    while True:
        remaining_with_sentinel = max_bytes + 1 - len(body)
        read_size = min(
            _URL_RESPONSE_READ_CHUNK_BYTES,
            remaining_with_sentinel,
        )
        chunk = response.read(read_size)
        if not chunk:
            return bytes(body)
        if len(chunk) > max_bytes - len(body):
            raise URLResponseBodyTooLarge(
                f"response exceeds {max_bytes} bytes"
            )
        body.extend(chunk)


class _RedirectResponseReadLimiter:
    """Clamp only the redirect handler's otherwise-unbounded drain."""

    def __init__(self, response: Any, *, max_bytes: int) -> None:
        self._response = response
        self._max_bytes = max_bytes
        self._limit_active = True

    def disable_limit(self) -> None:
        # If urllib raises an HTTPError before draining (for example a POST
        # 307), the exception retains this proxy as its response body. Restore
        # ordinary reads before the exception reaches its caller.
        self._limit_active = False

    def read(self, amount: int | None = None) -> bytes:
        if not self._limit_active:
            if amount is None:
                return self._response.read()
            return self._response.read(amount)

        if amount is None or amount < 0 or amount > self._max_bytes:
            amount = self._max_bytes
        return self._response.read(amount)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._response, name)


def url_origin(url: str) -> tuple[str, str, int | None]:
    """Return a normalized (scheme, hostname, effective port) origin."""
    parsed = urllib.parse.urlparse(url)
    scheme = (parsed.scheme or "").lower()
    # Accessing ``parsed.port`` validates malformed/non-numeric ports. Let the
    # ValueError fail the request closed instead of collapsing it to a default.
    port = parsed.port
    return (
        scheme,
        (parsed.hostname or "").lower().rstrip("."),
        port if port is not None else _DEFAULT_PORTS.get(scheme),
    )


class SafeCredentialRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Preserve request headers only while redirects stay on one origin."""

    def __init__(
        self,
        original_url: str,
        *,
        cross_origin_safe_headers: Iterable[str] = _CROSS_ORIGIN_SAFE_HEADERS,
        max_redirect_response_bytes: int | None = None,
    ) -> None:
        if (
            max_redirect_response_bytes is not None
            and max_redirect_response_bytes < 0
        ):
            raise ValueError(
                "max_redirect_response_bytes must be non-negative"
            )
        self._original_origin = url_origin(original_url)
        self._cross_origin_safe_headers = frozenset(
            str(name).lower() for name in cross_origin_safe_headers
        )
        self._max_redirect_response_bytes = max_redirect_response_bytes

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Let urllib enforce status/method semantics first (notably 307/308).
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None

        resolved_url = urllib.parse.urljoin(req.full_url, newurl)
        if url_origin(resolved_url) != self._original_origin:
            # Use an allowlist rather than guessing credential header names.
            # normalize_extra_headers permits arbitrary secret-bearing names.
            for name, _value in list(redirected.header_items()):
                if name.lower() not in self._cross_origin_safe_headers:
                    redirected.remove_header(name)
        return redirected

    def http_error_302(self, req, fp, code, msg, headers):
        """Follow redirects without an unbounded discarded-body read."""
        if self._max_redirect_response_bytes is None:
            return super().http_error_302(req, fp, code, msg, headers)

        limited_response = _RedirectResponseReadLimiter(
            fp,
            max_bytes=self._max_redirect_response_bytes,
        )
        try:
            # The stdlib still owns Location parsing, method rewriting, loop
            # detection, closing the intermediate response, and opening the
            # next request. Its no-argument `read()` is the only operation the
            # proxy changes.
            return super().http_error_302(
                req,
                limited_response,
                code,
                msg,
                headers,
            )
        finally:
            limited_response.disable_limit()

    # HTTPRedirectHandler binds these aliases at class-definition time. Repeat
    # them here so every supported status reaches the bounded override.
    http_error_301 = http_error_303 = http_error_307 = http_error_308 = (
        http_error_302
    )


class _CrossOriginRequestSanitizer(urllib.request.BaseHandler):
    """Strip headers after installed request processors have run."""

    # Request processors run in ascending order. Keep this last so an installed
    # cookie/auth/instrumentation processor cannot re-add a secret after the
    # redirect handler sanitizes the new Request.
    # Infinity is greater than every finite handler order. If an installed
    # processor also uses infinity, stable sorting keeps this appended handler
    # after it, so sanitization still owns the final request boundary.
    handler_order = float("inf")  # type: ignore[assignment]

    def __init__(self, original_url: str) -> None:
        self._original_origin = url_origin(original_url)

    def _sanitize(self, request: urllib.request.Request):
        if url_origin(request.full_url) != self._original_origin:
            for name, _value in list(request.header_items()):
                if name.lower() not in _CROSS_ORIGIN_SAFE_HEADERS:
                    request.remove_header(name)
        return request

    http_request = _sanitize
    https_request = _sanitize


def _secure_opener_from_installed_policy(
    original_url: str,
    *,
    max_redirect_response_bytes: int | None = None,
):
    """Clone the installed opener's handlers, replacing redirect policy only."""
    installed = getattr(urllib.request, "_opener", None)
    if installed is None:
        installed = urllib.request.build_opener()

    handlers = [
        copy.copy(handler)
        for handler in getattr(installed, "handlers", ())
        if not isinstance(handler, urllib.request.HTTPRedirectHandler)
    ]
    handlers.append(
        SafeCredentialRedirectHandler(
            original_url,
            max_redirect_response_bytes=max_redirect_response_bytes,
        )
    )
    handlers.append(_CrossOriginRequestSanitizer(original_url))
    secured = urllib.request.build_opener(*handlers)
    # OpenerDirector injects addheaders after request processors, which would
    # bypass the sanitizer on redirects. Carry them on the initial request
    # instead, then leave the rebuilt opener's late-injection list empty.
    setattr(
        secured,
        "_hermes_initial_addheaders",
        list(getattr(installed, "addheaders", ())),
    )
    secured.addheaders = []
    return secured


def open_credentialed_url(
    request: urllib.request.Request,
    *,
    timeout: float,
    opener_factory: Callable[..., Any] | None = None,
    max_redirect_response_bytes: int | None = None,
):
    """Open a request without forwarding credentials across origins.

    The default preserves an application-installed opener's proxy, TLS,
    cookies, custom protocol handlers, and instrumentation while replacing its
    redirect handler. ``opener_factory`` is an explicit test seam; security is
    never disabled based on global ``urlopen`` identity.
    """
    if opener_factory is None:
        opener = _secure_opener_from_installed_policy(
            request.full_url,
            max_redirect_response_bytes=max_redirect_response_bytes,
        )
        for name, value in getattr(opener, "_hermes_initial_addheaders", ()):
            if not request.has_header(name):
                request.add_header(name, value)
    else:
        opener = opener_factory(
            SafeCredentialRedirectHandler(
                request.full_url,
                max_redirect_response_bytes=max_redirect_response_bytes,
            )
        )
    return opener.open(request, timeout=timeout)


def read_credentialed_url_bytes_limited(
    request: urllib.request.Request,
    *,
    timeout: float,
    max_bytes: int,
    opener_factory: Callable[..., Any] | None = None,
) -> bytes:
    """Open and fully read a credential-safe URL within one byte budget.

    The same cap applies to every followed redirect response and to the final
    response. Redirect method rewriting, loop detection, installed opener
    policy, and cross-origin credential stripping remain owned by urllib and
    :class:`SafeCredentialRedirectHandler`.
    """
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    with open_credentialed_url(
        request,
        timeout=timeout,
        opener_factory=opener_factory,
        max_redirect_response_bytes=max_bytes,
    ) as response:
        return _read_url_response_bytes_limited(
            response,
            max_bytes=max_bytes,
        )


__all__ = [
    "SafeCredentialRedirectHandler",
    "URLResponseBodyTooLarge",
    "open_credentialed_url",
    "read_credentialed_url_bytes_limited",
    "url_origin",
]
