"""Native-app redirect policy for the dashboard OAuth broker.

Loopback redirects remain available for desktop clients. Operators may also
configure exact private-use callback URIs for installed apps. Configuration is
parsed once by ``start_server`` and stored as an immutable set on app state;
request validation never reloads or normalises configured values.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit

_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*$")
_FORBIDDEN_PRIVATE_SCHEMES = frozenset(
    {"http", "https", "file", "data", "javascript"}
)


class NativeRedirectConfigurationError(ValueError):
    """Raised when an operator-configured native callback is unsafe."""


def parse_native_redirect_uris(raw: object) -> frozenset[str]:
    """Validate an operator callback list and return an exact-match set.

    Empty/unset configuration returns an empty set, preserving the historical
    loopback-only policy. Strings are rejected instead of being treated as an
    iterable of characters so a YAML scalar cannot silently broaden policy.
    """
    if raw is None or raw == []:
        return frozenset()
    if not isinstance(raw, list):
        raise NativeRedirectConfigurationError(
            "dashboard.oauth.native_redirect_uris must be a YAML list"
        )

    callbacks: set[str] = set()
    for index, value in enumerate(raw):
        if not isinstance(value, str) or not value:
            raise NativeRedirectConfigurationError(
                "dashboard.oauth.native_redirect_uris entries must be "
                f"non-empty strings (entry {index})"
            )
        if value != value.strip() or any(
            ord(char) <= 0x20 or ord(char) == 0x7F for char in value
        ):
            raise NativeRedirectConfigurationError(
                "dashboard.oauth.native_redirect_uris entries must not "
                f"contain whitespace or control characters (entry {index})"
            )

        parsed = urlsplit(value)
        scheme = parsed.scheme
        if (
            not _SCHEME_RE.fullmatch(scheme)
            or "." not in scheme
            or scheme.lower() in _FORBIDDEN_PRIVATE_SCHEMES
        ):
            raise NativeRedirectConfigurationError(
                "dashboard.oauth.native_redirect_uris entries require a "
                f"dotted private-use scheme (entry {index})"
            )
        if parsed.netloc:
            raise NativeRedirectConfigurationError(
                "dashboard.oauth.native_redirect_uris entries must not "
                f"contain an authority or userinfo (entry {index})"
            )
        if not parsed.path.startswith("/"):
            raise NativeRedirectConfigurationError(
                "dashboard.oauth.native_redirect_uris entries require an "
                f"absolute path (entry {index})"
            )
        if parsed.query or parsed.fragment:
            raise NativeRedirectConfigurationError(
                "dashboard.oauth.native_redirect_uris entries must not "
                f"contain a query or fragment (entry {index})"
            )
        callbacks.add(value)
    return frozenset(callbacks)


def configured_native_redirect_uris(config: object) -> frozenset[str]:
    """Extract and validate ``dashboard.oauth.native_redirect_uris``."""
    if not isinstance(config, dict):
        return frozenset()
    dashboard = config.get("dashboard")
    if not isinstance(dashboard, dict):
        return frozenset()
    oauth = dashboard.get("oauth")
    if not isinstance(oauth, dict):
        return frozenset()
    return parse_native_redirect_uris(oauth.get("native_redirect_uris"))


def is_loopback_redirect_uri(raw: str) -> bool:
    """Return whether ``raw`` is an RFC 8252 literal-loopback callback."""
    if not raw:
        return False
    parsed = urlsplit(raw)
    if parsed.scheme != "http":
        return False
    try:
        host = (parsed.hostname or "").lower()
        parsed.port
    except ValueError:
        return False
    return host in {"127.0.0.1", "::1"}


def is_allowed_native_redirect_uri(
    raw: str, configured: frozenset[str]
) -> bool:
    """Apply loopback policy plus exact configured private-use matching."""
    return is_loopback_redirect_uri(raw) or raw in configured
