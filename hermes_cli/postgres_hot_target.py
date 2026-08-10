"""Cross-platform PostgreSQL target configuration for the hot shadow runtime.

This module intentionally imports no migration or POSIX-only implementation.
It is safe to import on native Windows before the opt-in runtime decides whether
network activity is enabled.
"""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes, urlsplit


class TargetConfigurationError(ValueError):
    """Stable, deliberately non-sensitive target configuration failure."""


@dataclass(frozen=True, repr=False)
class TargetConfig:
    """Validated connection fields with a redacted representation."""

    host: str
    port: int
    user: str
    password: str
    database: str
    ssl: ssl.SSLContext | bool

    def __repr__(self) -> str:
        return "TargetConfig(password=<redacted>)"


def _strict_unquote(value: str) -> str:
    """Decode a URL component while rejecting malformed percent escapes."""
    if any(
        value[index] == "%"
        and (
            index + 2 >= len(value)
            or any(
                char not in "0123456789abcdefABCDEF"
                for char in value[index + 1 : index + 3]
            )
        )
        for index in range(len(value))
    ):
        raise ValueError("bad escape")
    return unquote_to_bytes(value).decode("utf-8", "strict")


def parse_target_dsn(
    dsn: str,
    *,
    allow_insecure_loopback: bool = False,
) -> TargetConfig:
    """Parse the narrow supported DSN grammar without exposing secret text.

    Remote targets require ``sslmode=verify-full`` and an existing CA file.
    Plaintext is accepted only for explicit loopback test/development targets.
    Every failure is collapsed to one sanitized exception.
    """
    try:
        parsed = urlsplit(dsn)
        if (
            parsed.scheme not in {"postgres", "postgresql"}
            or parsed.fragment
            or parsed.netloc.count("@") != 1
        ):
            raise ValueError("invalid URL")

        host = parsed.hostname
        port = 5432 if parsed.port is None else parsed.port
        username = parsed.username
        password = parsed.password
        if not 1 <= port <= 65535:
            raise ValueError("invalid port")
        if (
            not host
            or username is None
            or password is None
            or parsed.path.count("/") != 1
            or not parsed.path[1:]
        ):
            raise ValueError("missing component")

        user, decoded_password, database = (
            _strict_unquote(component)
            for component in (username, password, parsed.path[1:])
        )

        pairs = (
            []
            if not parsed.query
            else [part.split("=", 1) for part in parsed.query.split("&")]
        )
        if any(len(pair) != 2 or not pair[0] for pair in pairs):
            raise ValueError("bad query")
        options = {
            _strict_unquote(key): _strict_unquote(value) for key, value in pairs
        }
        if len(options) != len(pairs) or set(options) - {"sslmode", "sslrootcert"}:
            raise ValueError("bad query")

        loopback = host.lower() in {"localhost", "127.0.0.1", "::1"}
        if loopback and allow_insecure_loopback:
            if options:
                raise ValueError("TLS options on loopback")
            return TargetConfig(
                host,
                port,
                user,
                decoded_password,
                database,
                False,
            )

        if options.get("sslmode") != "verify-full" or not options.get(
            "sslrootcert"
        ):
            raise ValueError("TLS required")
        ca_path = Path(options["sslrootcert"])
        if not ca_path.is_file():
            raise ValueError("CA unavailable")
        context = ssl.create_default_context(cafile=str(ca_path))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        return TargetConfig(
            host,
            port,
            user,
            decoded_password,
            database,
            context,
        )
    except (OSError, UnicodeError, ValueError, ssl.SSLError, TypeError):
        raise TargetConfigurationError("invalid target configuration") from None
