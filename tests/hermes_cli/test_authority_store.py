import pytest

from hermes_cli import authority_store


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


def test_unknown_backend_never_silently_falls_back_to_sqlite():
    with pytest.raises(
        authority_store.AuthorityStoreConfigurationError,
        match="not installed",
    ):
        authority_store.validate_topology(
            {
                "agentic": {
                    "authority_store": {
                        "backend": "postgres",
                        "deployment_scope": "multi_host",
                    }
                }
            }
        )
