from __future__ import annotations

import pytest

from hermes_cli.postgres_hot_target import (
    TargetConfigurationError,
    parse_target_dsn,
)


@pytest.mark.parametrize(
    ("dsn", "password", "port"),
    [
        ("postgresql://user:p%40ss%2Fword@127.0.0.1/db", "p@ss/word", 5432),
        ("postgres://user:empty@localhost:5544/db", "empty", 5544),
        ("postgresql://user:secret@[::1]/db", "secret", 5432),
    ],
)
def test_loopback_target_decodes_components(
    dsn: str,
    password: str,
    port: int,
) -> None:
    target = parse_target_dsn(dsn, allow_insecure_loopback=True)

    assert target.password == password
    assert target.port == port
    assert target.ssl is False
    assert password not in repr(target)


@pytest.mark.parametrize(
    "dsn",
    [
        "",
        "https://user:password@localhost/db",
        "postgresql://user@localhost/db",
        "postgresql://user:password@localhost",
        "postgresql://user:password@localhost:0/db",
        "postgresql://user:password@localhost:65536/db",
        "postgresql://user:password@localhost:not-a-port/db",
        "postgresql://user:password@localhost/db#fragment",
        "postgresql://user:password@localhost/db?unknown=value",
        "postgresql://user:password@localhost/db?sslmode=verify-full&sslmode=verify-full&sslrootcert=x",
        "postgresql://user:password@localhost/db?sslmode=disable",
        "postgresql://user:password@localhost/db?sslmode=verify-full&sslrootcert=/missing",
    ],
)
def test_invalid_or_insecure_target_is_rejected_with_sanitized_error(dsn: str) -> None:
    with pytest.raises(TargetConfigurationError) as error:
        parse_target_dsn(dsn)

    assert str(error.value) == "invalid target configuration"
    assert "password" not in repr(error.value)


@pytest.mark.parametrize("bad", ["%ZZ", "%FF", "%"])
def test_malformed_percent_encoding_is_sanitized(bad: str) -> None:
    with pytest.raises(TargetConfigurationError) as error:
        parse_target_dsn(
            f"postgresql://user:{bad}@localhost/database",
            allow_insecure_loopback=True,
        )

    assert bad not in str(error.value)


def test_loopback_plaintext_requires_explicit_opt_in() -> None:
    dsn = "postgresql://user:secret@localhost/database"

    with pytest.raises(TargetConfigurationError):
        parse_target_dsn(dsn)

    assert parse_target_dsn(dsn, allow_insecure_loopback=True).host == "localhost"


def test_loopback_opt_in_rejects_mixed_tls_options() -> None:
    with pytest.raises(TargetConfigurationError):
        parse_target_dsn(
            "postgresql://user:secret@localhost/database?sslmode=verify-full&sslrootcert=/tmp/ca.pem",
            allow_insecure_loopback=True,
        )
