"""Transport failure classification shared by auxiliary fallback paths.

These predicates intentionally remain independent of the auxiliary client so
sync and async fallback paths use one failure vocabulary. A connection error
only says that fallback is warranted; an endpoint-unreachable error is the
stronger claim that every model behind the URL should be skipped.
"""

from __future__ import annotations

import ssl
from typing import Optional


def is_timeout_error(exc: Exception) -> bool:
    """Detect a full-budget request timeout."""
    try:
        from openai import APITimeoutError
        if isinstance(exc, APITimeoutError):
            return True
    except ImportError:
        pass
    if "Timeout" in type(exc).__name__:
        return True
    return "timed out" in str(exc).lower()


def is_endpoint_unreachable_error(exc: Exception) -> bool:
    """Return whether an exception proves its endpoint cannot be reached.

    Certificate/protocol negotiation failures are deterministic properties of
    the endpoint, just like DNS failure and connection refusal. TLS EOF,
    want-read/write, zero-return, and reset-driven closes are transient
    model-scoped blips and must not suppress retries or sibling deployments.
    """
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    for _ in range(5):
        if current is None or id(current) in seen:
            break
        seen.add(id(current))
        error_type = type(current).__name__.lower()
        text = str(current).lower()

        # A reset/EOF is a transport blip even when an SSL error is nested
        # beneath an httpx/httpcore wrapper. Check this before generic SSLError.
        if isinstance(current, (
            ssl.SSLEOFError,
            ssl.SSLWantReadError,
            ssl.SSLWantWriteError,
            ssl.SSLZeroReturnError,
        )) or any(
            marker in text
            for marker in (
                "connection reset",
                "connection aborted",
                "eof occurred",
                "unexpected eof",
                "peer closed connection",
                "incomplete chunked read",
                "response ended prematurely",
                "remoteprotocolerror",
                "localprotocolerror",
            )
        ):
            return False

        if isinstance(current, (ssl.SSLCertVerificationError, ssl.CertificateError)):
            return True
        if isinstance(current, ssl.SSLError):
            # Generic SSLError is endpoint-scoped only when its text proves a
            # deterministic certificate or protocol-negotiation failure.
            if any(
                marker in text
                for marker in (
                    "certificate verify failed",
                    "certificate verification failed",
                    "self signed certificate",
                    "hostname mismatch",
                    "tls handshake",
                    "ssl handshake",
                    "wrong version number",
                    "wrong version",
                    "protocol version",
                    "unsupported protocol",
                    "no suitable signature algorithm",
                    "unknown ca",
                    "bad certificate",
                    "certificate has expired",
                    "unable to get local issuer",
                    "certificateverifyfailed",
                )
            ):
                return True
            return False
        if error_type in {"sslcertverificationerror", "certificateerror"}:
            return True
        if any(
            marker in text
            for marker in (
                "certificate verify failed",
                "certificate verification failed",
                "self signed certificate",
                "hostname mismatch",
                "tls handshake",
                "ssl handshake",
                "wrong version number",
                "wrong version",
                "protocol version",
                "unsupported protocol",
                "no suitable signature algorithm",
                "unknown ca",
                "bad certificate",
                "certificate has expired",
                "unable to get local issuer",
                "certificateverifyfailed",
            )
        ):
            return True
        if error_type in {
            "connectionrefusederror",
            "gaierror",
            "addressnotavailable",
        }:
            return True
        if error_type in {"connecttimeout", "connecttimeouterror"}:
            return True
        if error_type == "connecterror" and (
            "all connection attempts failed" in text
            or "failed to establish a new connection" in text
            or "cannot connect" in text
            or "connect timeout" in text
        ):
            return True
        if any(
            marker in text
            for marker in (
                "connection refused",
                "name or service not known",
                "temporary failure in name resolution",
                "getaddrinfo failed",
                "nodename nor servname provided",
                "dns failure",
                "no route to host",
                "network is unreachable",
            )
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_transient_transport_error(exc: Exception) -> bool:
    """Return whether a transport failure is retryable on the same target."""
    if is_connection_error(exc):
        return not is_endpoint_unreachable_error(exc)
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    return isinstance(status, int) and (status == 408 or 500 <= status < 600)


def is_connection_error(exc: Exception) -> bool:
    """Return whether an exception is a transport error worth falling back."""
    try:
        from openai import APIConnectionError, APITimeoutError
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return True
    except ImportError:
        pass
    err_type = type(exc).__name__
    if any(kw in err_type for kw in (
        "Connection", "Connect", "Timeout", "DNS", "SSL", "ReadError",
        "WriteError", "RemoteProtocol", "LocalProtocol",
    )):
        return True
    err_lower = str(exc).lower()
    if any(kw in err_lower for kw in (
        "connection refused", "name or service not known",
        "no route to host", "network is unreachable", "timed out",
        "connection reset", "all connection attempts failed",
        "nodename nor servname provided", "getaddrinfo failed",
        "failed to establish a new connection", "incomplete chunked read",
        "peer closed connection", "response ended prematurely",
        "unexpected eof", "remoteprotocolerror", "localprotocolerror",
        "certificate verify failed", "certificate verification failed",
        "tls handshake", "ssl handshake", "self signed certificate",
        "hostname mismatch", "wrong version number", "unknown ca",
        "bad certificate", "certificate has expired",
        "unable to get local issuer", "certificateverifyfailed",
    )):
        return True
    return False
