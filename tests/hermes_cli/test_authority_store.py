import pytest
from unittest.mock import patch, MagicMock

from hermes_cli import authority_store


# ---------------------------------------------------------------------------
# Authority store capability contract
# ---------------------------------------------------------------------------


def test_sqlite_declares_single_host_fenced_authority_contract():
    result = authority_store.status(
        {
            "agentic": {
                "authority_store": {
                    "backend": "sqlite",
                    "deployment_scope": "single_host",
                }
            }
        }
    )
    assert result["transactional"] is True
    assert result["atomic_event_claims"] is True
    assert result["resource_fencing"] is True
    assert result["multi_host"] is False
    assert result["ready"] is True


def test_sqlite_multi_host_configuration_fails_closed():
    with pytest.raises(
        authority_store.AuthorityStoreConfigurationError,
        match="cannot safely coordinate multi-host",
    ):
        authority_store.validate_topology(
            {
                "agentic": {
                    "authority_store": {
                        "backend": "sqlite",
                        "deployment_scope": "multi_host",
                    }
                }
            }
        )


def test_postgres_declares_multi_host_fenced_authority_contract():
    result = authority_store.status(
        {
            "agentic": {
                "authority_store": {
                    "backend": "postgres",
                    "deployment_scope": "multi_host",
                }
            }
        }
    )
    assert result["transactional"] is True
    assert result["multi_host"] is True
    assert result["resource_fencing"] is True
    assert result["ready"] is True


def test_unknown_backend_never_silently_falls_back_to_sqlite():
    # Use a backend name that will never exist; must fail closed, never
    # silently degrade to SQLite.
    with pytest.raises(
        authority_store.AuthorityStoreConfigurationError,
        match="not installed",
    ):
        authority_store.validate_topology(
            {
                "agentic": {
                    "authority_store": {
                        "backend": "cassandra_nonexistent",
                        "deployment_scope": "multi_host",
                    }
                }
            }
        )


# ---------------------------------------------------------------------------
# Readiness findings for authority store
# ---------------------------------------------------------------------------


def test_authority_store_findings_sqlite_returns_no_findings():
    """SQLite backend always reachable — no findings expected."""
    from hermes_cli.business_readiness import authority_store_findings

    with patch.dict("os.environ", {}, clear=False):
        # Ensure no postgres env vars
        import os
        for key in ("AUTHORITY_POSTGRES_URL", "DATABASE_URL"):
            os.environ.pop(key, None)

        findings = authority_store_findings({})

    assert findings == []


def test_authority_store_findings_postgres_unreachable():
    """Postgres configured but unreachable → authority_store_unreachable finding."""
    from hermes_cli.business_readiness import authority_store_findings

    with patch.dict("os.environ", {"AUTHORITY_POSTGRES_URL": "postgresql://bad:bad@nonexistent/db"}):
        findings = authority_store_findings({})

    codes = [f.code for f in findings]
    assert "authority_store_unreachable" in codes


def test_authority_store_findings_postgres_schema_outdated():
    """Postgres reachable but schema version too low → outdated finding."""
    from hermes_cli.business_readiness import authority_store_findings

    mock_conn = MagicMock()

    with patch.dict("os.environ", {"AUTHORITY_POSTGRES_URL": "postgresql://x/y"}):
        with patch("hermes_cli.postgres_authority.connect", return_value=mock_conn):
            with patch("hermes_cli.postgres_authority.get_schema_version", return_value=0):
                findings = authority_store_findings({})

    codes = [f.code for f in findings]
    assert "authority_store_schema_outdated" in codes


def test_authority_store_findings_postgres_schema_current():
    """Postgres reachable and schema current → no findings."""
    from hermes_cli.business_readiness import authority_store_findings
    from hermes_cli.postgres_authority import SCHEMA_VERSION

    mock_conn = MagicMock()

    with patch.dict("os.environ", {"AUTHORITY_POSTGRES_URL": "postgresql://x/y"}):
        with patch("hermes_cli.postgres_authority.connect", return_value=mock_conn):
            with patch("hermes_cli.postgres_authority.get_schema_version", return_value=SCHEMA_VERSION):
                findings = authority_store_findings({})

    assert findings == []


def test_authority_store_findings_driver_missing():
    """psycopg not installed → driver_missing finding, no traceback."""
    from hermes_cli.business_readiness import authority_store_findings

    with patch.dict("os.environ", {"AUTHORITY_POSTGRES_URL": "postgresql://x/y"}):
        with patch("hermes_cli.postgres_authority.connect", side_effect=ImportError("no psycopg")):
            findings = authority_store_findings({})

    codes = [f.code for f in findings]
    assert "authority_store_driver_missing" in codes
