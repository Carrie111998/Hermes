from hermes_cli.business_security import evaluate_security_readiness


def test_production_autonomy_fails_closed_without_isolation_and_vault():
    result = evaluate_security_readiness(
        {
            "terminal": {"backend": "local"},
            "security": {"redact_secrets": True},
            "agentic": {
                "security": {
                    "enforce_isolated_execution": True,
                    "require_external_secret_manager": True,
                },
                "finance": {"payments": {"custody_model": "non_custodial"}},
            },
        }
    )
    assert not result.ready
    assert any("isolated backend" in item for item in result.violations)
    assert any("secret manager" in item for item in result.violations)
