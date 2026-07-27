from __future__ import annotations

from hermes_cli.setup import setup_agentic_settings


def test_setup_agentic_defaults_to_advisor_autonomy(monkeypatch):
    config = {}
    choices = iter([0, 0, 1, 0])
    answers = iter(
        [
            "crm.write,email.send",
            "crm,agentmail",
            "company.delete",
            "web,terminal",
            "research",
            "ceo@agentmail.to",
            "2500",
            "1000",
            "sole_proprietorship",
            "US-PA",
            "120",
            "Bootstrap Labs",
            "Build a sustainable software business",
            "Earn the first verified customer revenue",
            (
                '[{"verifier":"accounting.revenue_at_least",'
                '"params":{"amount_minor":1000,"currency":"USD"}},'
                '{"verifier":"accounting.books_balanced","params":{}}]'
            ),
            "capital exhausted,objective expires",
            "180",
        ]
    )
    monkeypatch.setattr(
        "hermes_cli.setup.prompt_choice", lambda *args, **kwargs: next(choices)
    )
    monkeypatch.setattr(
        "hermes_cli.setup.prompt", lambda *args, **kwargs: next(answers)
    )
    monkeypatch.setattr("hermes_cli.setup.save_config", lambda value: None)
    monkeypatch.setattr("hermes_cli.setup._bootstrap_agentic_business", lambda value: None)

    setup_agentic_settings(config)

    charter = config["agentic"]
    assert charter["enabled"] is True
    assert charter["operating_mode"] == "autonomous"
    assert charter["operator_role"] == "advisor"
    assert charter["security"]["require_runtime_baseline"] is True
    assert charter["runtime_host"] == "gateway"
    assert charter["allowed_capabilities"] == ["crm.write", "email.send"]
    assert charter["allowed_systems"] == ["crm", "agentmail"]
    assert charter["solo_founder"]["toolsets"] == ["web", "terminal"]
    assert charter["solo_founder"]["skills"] == ["research"]
    assert (
        charter["communications"]["email"]["inbox_id"]
        == "ceo@agentmail.to"
    )
    assert charter["max_autonomous_risk"] == "medium"
    assert charter["allow_irreversible"] is False
    assert charter["max_action_spend_minor"] == 2500
    assert charter["finance"]["initial_capital_minor"] == 1000
    assert charter["finance"]["tax_profile"]["legal_entity_type"] == "sole_proprietorship"
    assert charter["finance"]["tax_profile"]["jurisdictions"] == ["US-PA"]
    assert charter["permit_ttl_seconds"] == 120
    assert charter["initial_mandate"]["organization_name"] == "Bootstrap Labs"
    assert charter["initial_mandate"]["success_criteria"] == [
        {
            "verifier": "accounting.revenue_at_least",
            "params": {"amount_minor": 1000, "currency": "USD"},
        },
        {"verifier": "accounting.books_balanced", "params": {}},
    ]
    assert charter["initial_mandate"]["duration_days"] == 180


def test_setup_can_explicitly_keep_agentic_operation_disabled(monkeypatch):
    config = {}
    monkeypatch.setattr(
        "hermes_cli.setup.prompt_choice", lambda *args, **kwargs: 3
    )
    monkeypatch.setattr("hermes_cli.setup.save_config", lambda value: None)

    setup_agentic_settings(config)

    assert config["agentic"]["enabled"] is False
